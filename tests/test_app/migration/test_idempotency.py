from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from sana.app.migration.service import (
    ArchiveCommand,
    EmbeddingResult,
    LedgerEntry,
    LegacyRecord,
    MemoryImportCommand,
    MigrationDisposition,
    MigrationPlanner,
    MigrationService,
    ReaderResult,
    SourceManifest,
    canonical_hash,
)
from sana.modules.shared.ids import DeterministicIdFactory


TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


class FakeEmbedder:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, text: str) -> EmbeddingResult:
        self.inputs.append(text)
        return EmbeddingResult("fixture-embedder", "v2", (0.1, 0.2))


class FakeAtomicStore:
    def __init__(self) -> None:
        self.ledger: dict[tuple[UUID, str, str, str], LedgerEntry] = {}
        self.imported: list[MemoryImportCommand] = []
        self.archived: list[ArchiveCommand] = []

    async def ledger_entry(
        self,
        tenant_id: UUID,
        source_system: str,
        source_id: str,
        migration_version: str,
    ) -> LedgerEntry | None:
        return self.ledger.get((tenant_id, source_system, source_id, migration_version))

    async def import_memory(self, command: MemoryImportCommand) -> None:
        self.imported.append(command)
        key = (
            command.tenant_id,
            command.record.source_system,
            command.record.source_id,
            command.migration_version,
        )
        self.ledger[key] = LedgerEntry(command.record.source_hash, "IMPORTED")

    async def archive(self, command: ArchiveCommand) -> None:
        self.archived.append(command)
        key = (
            command.tenant_id,
            command.record.source_system,
            command.record.source_id,
            command.migration_version,
        )
        self.ledger[key] = LedgerEntry(command.record.source_hash, "ARCHIVED")


def _plan():
    imported = LegacyRecord(
        "chroma_memory",
        "text",
        "memory",
        "recoverable source text",
        canonical_hash("recoverable source text"),
        MigrationDisposition.IMPORT,
        "recoverable_source_text_reembed",
    )
    archived = LegacyRecord(
        "chroma_memory",
        "vector",
        "legacy_vector_archive",
        None,
        canonical_hash("legacy-vector"),
        MigrationDisposition.ARCHIVE,
        "vector_without_source_text",
        {"legacy_vector_hash": canonical_hash([0.1, 0.2])},
    )
    skipped = LegacyRecord(
        "user_profile",
        "model_config",
        "excluded_configuration",
        None,
        canonical_hash("secret-config"),
        MigrationDisposition.SKIP,
        "excluded_configuration",
    )
    records = (imported, archived, skipped)
    return MigrationPlanner().build(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        sources=(
            ReaderResult(
                SourceManifest(
                    "fixture",
                    "readonly://fixture",
                    canonical_hash("fixture"),
                    record_count=len(records),
                ),
                records,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_repeated_apply_does_not_duplicate_or_reembed() -> None:
    store = FakeAtomicStore()
    embedder = FakeEmbedder()
    service = MigrationService(DeterministicIdFactory("migration"))
    plan = _plan()
    now = datetime(2026, 8, 14, tzinfo=UTC)

    first = await service.apply(
        plan,
        store=store,
        embedder=embedder,
        executed_at=now,
        backup_manifest_verified=True,
    )
    second = await service.apply(
        plan,
        store=store,
        embedder=embedder,
        executed_at=now,
        backup_manifest_verified=True,
    )

    assert first.imported == 1
    assert first.archived == 1
    assert first.skipped_by_plan == 1
    assert second.skipped_idempotent == 2
    assert second.skipped_by_plan == 1
    assert embedder.inputs == ["recoverable source text"]
    assert len(store.imported) == 1
    assert store.imported[0].embedding.model == "fixture-embedder"
    assert store.imported[0].embedding.version == "v2"
    assert len(store.archived) == 1


@pytest.mark.asyncio
async def test_apply_requires_timezone_and_verified_backup_manifest() -> None:
    service = MigrationService(DeterministicIdFactory())
    store = FakeAtomicStore()
    embedder = FakeEmbedder()

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.apply(
            _plan(),
            store=store,
            embedder=embedder,
            executed_at=datetime(2026, 8, 14),
            backup_manifest_verified=True,
        )
    with pytest.raises(ValueError, match="verified read-only backup"):
        await service.apply(
            _plan(),
            store=store,
            embedder=embedder,
            executed_at=datetime(2026, 8, 14, tzinfo=UTC),
            backup_manifest_verified=False,
        )
