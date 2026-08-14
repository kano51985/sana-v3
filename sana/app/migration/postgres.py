"""PostgreSQL migration store with tenant scope and atomic ledger writes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.app.migration.service import (
    ArchiveCommand,
    LedgerEntry,
    MemoryImportCommand,
)
from sana.platform.db.models.memory import (
    LegacyArchive,
    MemoryEmbedding,
    MemoryItem,
    MigrationLedger,
)


class MigrationWriteConflict(RuntimeError):
    """The same source identity was already migrated with different material."""


class PostgresMigrationStore:
    """Persist each target row and its ledger row in the same transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ledger_entry(
        self,
        tenant_id: UUID,
        source_system: str,
        source_id: str,
        migration_version: str,
    ) -> LedgerEntry | None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant(session, tenant_id)
                row = await session.scalar(
                    self._ledger_query(
                        tenant_id,
                        source_system,
                        source_id,
                        migration_version,
                    )
                )
                if row is None:
                    return None
                return LedgerEntry(row.source_hash, row.status)

    async def import_memory(self, command: MemoryImportCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant(session, command.tenant_id)
                await self._lock_source(
                    session,
                    command.tenant_id,
                    command.record.source_system,
                    command.record.source_id,
                    command.migration_version,
                )
                if await self._already_finished(session, command, "IMPORTED"):
                    return
                memory_metadata = dict(command.record.metadata)
                memory_metadata["migration"] = {
                    "source_system": command.record.source_system,
                    "source_id_hash": self._hash_identity(command.record.source_id),
                    "source_hash": command.record.source_hash,
                    "migration_version": command.migration_version,
                    "reason": command.record.reason,
                }
                session.add(
                    MemoryItem(
                        id=command.target_id,
                        tenant_id=command.tenant_id,
                        user_id=command.user_id,
                        kind=command.record.kind,
                        content=command.record.content or "",
                        content_hash=command.content_hash,
                        importance=0.5,
                        memory_metadata=memory_metadata,
                        created_at=command.executed_at,
                        updated_at=command.executed_at,
                    )
                )
                session.add(
                    MemoryEmbedding(
                        tenant_id=command.tenant_id,
                        memory_item_id=command.target_id,
                        model=command.embedding.model,
                        model_version=command.embedding.version,
                        dimensions=len(command.embedding.vector),
                        embedding=list(command.embedding.vector),
                        content_hash=command.content_hash,
                        created_at=command.executed_at,
                    )
                )
                session.add(
                    MigrationLedger(
                        tenant_id=command.tenant_id,
                        source_system=command.record.source_system,
                        source_id=command.record.source_id,
                        source_hash=command.record.source_hash,
                        target_id=command.target_id,
                        migration_version=command.migration_version,
                        status="IMPORTED",
                        executed_at=command.executed_at,
                    )
                )

    async def archive(self, command: ArchiveCommand) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant(session, command.tenant_id)
                await self._lock_source(
                    session,
                    command.tenant_id,
                    command.record.source_system,
                    command.record.source_id,
                    command.migration_version,
                )
                if await self._already_finished(session, command, "ARCHIVED"):
                    return
                existing_archive = await session.scalar(
                    select(LegacyArchive).where(
                        LegacyArchive.tenant_id == command.tenant_id,
                        LegacyArchive.source_system == command.record.source_system,
                        LegacyArchive.archive_hash == command.record.source_hash,
                    )
                )
                if existing_archive is None:
                    session.add(
                        LegacyArchive(
                            id=command.archive_id,
                            tenant_id=command.tenant_id,
                            source_system=command.record.source_system,
                            archive_hash=command.record.source_hash,
                            storage_uri=(
                                f"legacy-record://{command.record.source_system}/"
                                f"{self._hash_identity(command.record.source_id)}"
                            ),
                            manifest={
                                "source_hash": command.record.source_hash,
                                "reason": command.record.reason,
                                "metadata": dict(command.record.metadata),
                                "migration_version": command.migration_version,
                            },
                            created_at=command.executed_at,
                        )
                    )
                session.add(
                    MigrationLedger(
                        tenant_id=command.tenant_id,
                        source_system=command.record.source_system,
                        source_id=command.record.source_id,
                        source_hash=command.record.source_hash,
                        target_id=None,
                        migration_version=command.migration_version,
                        status="ARCHIVED",
                        executed_at=command.executed_at,
                    )
                )

    async def _already_finished(
        self,
        session: AsyncSession,
        command: MemoryImportCommand | ArchiveCommand,
        expected_status: str,
    ) -> bool:
        existing = await session.scalar(
            self._ledger_query(
                command.tenant_id,
                command.record.source_system,
                command.record.source_id,
                command.migration_version,
            )
        )
        if existing is None:
            return False
        if (
            existing.source_hash == command.record.source_hash
            and existing.status == expected_status
        ):
            return True
        raise MigrationWriteConflict(
            "Legacy source identity already has a different migration ledger entry"
        )

    @staticmethod
    def _ledger_query(
        tenant_id: UUID,
        source_system: str,
        source_id: str,
        migration_version: str,
    ):
        return select(MigrationLedger).where(
            MigrationLedger.tenant_id == tenant_id,
            MigrationLedger.source_system == source_system,
            MigrationLedger.source_id == source_id,
            MigrationLedger.migration_version == migration_version,
        )

    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    @classmethod
    async def _lock_source(
        cls,
        session: AsyncSession,
        tenant_id: UUID,
        source_system: str,
        source_id: str,
        migration_version: str,
    ) -> None:
        identity = f"{tenant_id}:{source_system}:{source_id}:{migration_version}"
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": identity},
        )

    @staticmethod
    def _hash_identity(value: str) -> str:
        from sana.app.migration.service import canonical_hash

        return canonical_hash(value)
