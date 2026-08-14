"""Migration plan, safe report and idempotent import orchestration."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

from sana.modules.shared.ids import IdFactory


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MigrationDisposition(StrEnum):
    IMPORT = "IMPORT"
    ARCHIVE = "ARCHIVE"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class BackupFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SourceManifest:
    source_system: str
    source_location: str
    source_hash: str
    files: tuple[BackupFile, ...] = ()
    record_count: int = 0


@dataclass(frozen=True, slots=True)
class LegacyRecord:
    source_system: str
    source_id: str
    kind: str
    content: str | None
    source_hash: str
    disposition: MigrationDisposition
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_system.strip() or not self.source_id.strip():
            raise ValueError("Legacy record source identity cannot be empty")
        if len(self.source_hash) != 64:
            raise ValueError("Legacy record requires a SHA-256 source hash")
        if self.disposition is MigrationDisposition.IMPORT and not (
            self.content and self.content.strip()
        ):
            raise ValueError("Imported legacy memory requires recoverable source text")
        if self.disposition is not MigrationDisposition.IMPORT and self.content:
            raise ValueError("Skipped or archived legacy records cannot carry content")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ReaderResult:
    manifest: SourceManifest
    records: tuple[LegacyRecord, ...]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    tenant_id: UUID
    user_id: UUID
    migration_version: str
    records: tuple[LegacyRecord, ...]
    manifests: tuple[SourceManifest, ...]
    conflicts: tuple[str, ...]
    issues: tuple[str, ...]
    plan_hash: str

    def safe_report(self) -> dict[str, Any]:
        source_counts: dict[str, Counter[str]] = {}
        discard_reasons: Counter[str] = Counter()
        for record in self.records:
            source_counts.setdefault(record.source_system, Counter())[
                record.disposition.value
            ] += 1
            if record.disposition is not MigrationDisposition.IMPORT:
                discard_reasons[record.reason] += 1
        return {
            "migration_version": self.migration_version,
            "plan_hash": self.plan_hash,
            "user_mapping": {
                "tenant_id": str(self.tenant_id),
                "user_id": str(self.user_id),
            },
            "source_counts": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(source_counts.items())
            },
            "source_manifests": [
                {
                    "source_system": manifest.source_system,
                    "source_location_hash": canonical_hash(manifest.source_location),
                    "source_hash": manifest.source_hash,
                    "record_count": manifest.record_count,
                    "files": [
                        {
                            "path_hash": canonical_hash(item.path),
                            "size": item.size,
                            "sha256": item.sha256,
                        }
                        for item in manifest.files
                    ],
                }
                for manifest in self.manifests
            ],
            "conflicts": list(self.conflicts),
            "issues": list(self.issues),
            "discard_reasons": dict(sorted(discard_reasons.items())),
        }


class MigrationPlanner:
    def __init__(self, *, migration_version: str = "memory-v1") -> None:
        if not migration_version.strip():
            raise ValueError("Migration version cannot be empty")
        self._version = migration_version

    def build(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        sources: tuple[ReaderResult, ...],
    ) -> MigrationPlan:
        records: list[LegacyRecord] = []
        conflicts: list[str] = []
        issues = tuple(
            sorted(issue for source in sources for issue in source.issues)
        )
        identities: dict[tuple[str, str], str] = {}
        imported_content: dict[str, tuple[str, str]] = {}
        for source in sources:
            for record in source.records:
                identity = (record.source_system, record.source_id)
                previous_hash = identities.get(identity)
                if previous_hash is not None:
                    if previous_hash != record.source_hash:
                        conflicts.append(
                            "source_identity_changed:"
                            f"{record.source_system}:{canonical_hash(record.source_id)}"
                        )
                    continue
                identities[identity] = record.source_hash
                if record.disposition is MigrationDisposition.IMPORT:
                    content_hash = hashlib.sha256(
                        (record.content or "").encode("utf-8")
                    ).hexdigest()
                    previous = imported_content.get(content_hash)
                    if previous is not None:
                        record = replace(
                            record,
                            content=None,
                            disposition=MigrationDisposition.SKIP,
                            reason="duplicate_content",
                            metadata={"duplicate_of": f"{previous[0]}:{previous[1]}"},
                        )
                    else:
                        imported_content[content_hash] = identity
                records.append(record)
        plan_material = {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "migration_version": self._version,
            "records": [
                {
                    "source_system": record.source_system,
                    "source_id": record.source_id,
                    "source_hash": record.source_hash,
                    "disposition": record.disposition.value,
                    "reason": record.reason,
                }
                for record in records
            ],
            "manifests": [
                (result.manifest.source_system, result.manifest.source_hash)
                for result in sources
            ],
            "conflicts": conflicts,
            "issues": issues,
        }
        return MigrationPlan(
            tenant_id,
            user_id,
            self._version,
            tuple(records),
            tuple(result.manifest for result in sources),
            tuple(conflicts),
            issues,
            canonical_hash(plan_material),
        )


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    source_hash: str
    status: str


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    model: str
    version: str
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.version.strip() or not self.vector:
            raise ValueError("Embedding result requires model, version and vector")


class MemoryEmbedder(Protocol):
    async def embed(self, text: str) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class MemoryImportCommand:
    target_id: UUID
    tenant_id: UUID
    user_id: UUID
    record: LegacyRecord
    content_hash: str
    embedding: EmbeddingResult
    migration_version: str
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class ArchiveCommand:
    archive_id: UUID
    tenant_id: UUID
    record: LegacyRecord
    migration_version: str
    executed_at: datetime


class MigrationStore(Protocol):
    """Persist a target row and its ledger row in one atomic transaction."""

    async def ledger_entry(
        self,
        tenant_id: UUID,
        source_system: str,
        source_id: str,
        migration_version: str,
    ) -> LedgerEntry | None: ...

    async def import_memory(self, command: MemoryImportCommand) -> None: ...

    async def archive(self, command: ArchiveCommand) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplyReport:
    imported: int
    archived: int
    skipped_by_plan: int
    skipped_idempotent: int
    conflicts: int


class MigrationService:
    def __init__(self, id_factory: IdFactory) -> None:
        self._ids = id_factory

    async def apply(
        self,
        plan: MigrationPlan,
        *,
        store: MigrationStore,
        embedder: MemoryEmbedder,
        executed_at: datetime,
        backup_manifest_verified: bool,
    ) -> ApplyReport:
        if executed_at.tzinfo is None or executed_at.utcoffset() is None:
            raise ValueError("Migration execution time must be timezone-aware")
        if not backup_manifest_verified or not plan.manifests:
            raise ValueError("A verified read-only backup manifest is required")
        if plan.conflicts or plan.issues:
            raise ValueError("Migration plan conflicts and source issues must be resolved")
        imported = archived = skipped_plan = skipped_idempotent = conflicts = 0
        for record in plan.records:
            if record.disposition is MigrationDisposition.SKIP:
                skipped_plan += 1
                continue
            existing = await store.ledger_entry(
                plan.tenant_id,
                record.source_system,
                record.source_id,
                plan.migration_version,
            )
            if existing is not None:
                if existing.source_hash == record.source_hash and existing.status in {
                    "IMPORTED",
                    "ARCHIVED",
                }:
                    skipped_idempotent += 1
                else:
                    conflicts += 1
                continue
            if record.disposition is MigrationDisposition.ARCHIVE:
                await store.archive(
                    ArchiveCommand(
                        self._ids.new_uuid(),
                        plan.tenant_id,
                        record,
                        plan.migration_version,
                        executed_at,
                    )
                )
                archived += 1
                continue
            content = record.content or ""
            embedding = await embedder.embed(content)
            await store.import_memory(
                MemoryImportCommand(
                    self._ids.new_uuid(),
                    plan.tenant_id,
                    plan.user_id,
                    record,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    embedding,
                    plan.migration_version,
                    executed_at,
                )
            )
            imported += 1
        return ApplyReport(
            imported,
            archived,
            skipped_plan,
            skipped_idempotent,
            conflicts + len(plan.conflicts),
        )
