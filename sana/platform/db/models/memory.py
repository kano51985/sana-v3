"""User-memory storage and replay-safe legacy migration mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sana.platform.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MemoryItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memory_items_tenant_id_id"),
        Index("ix_memory_items_tenant_user_updated", "tenant_id", "user_id", "updated_at"),
        Index("ix_memory_items_tenant_content_hash", "tenant_id", "content_hash"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    memory_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEmbedding(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "memory_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "memory_item_id",
            "model",
            "model_version",
            name="uq_memory_embeddings_item_model_version",
        ),
        Index("ix_memory_embeddings_tenant_item", "tenant_id", "memory_item_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    memory_item_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(ARRAY(Float), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MigrationLedger(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "migration_ledger"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_system",
            "source_id",
            "migration_version",
            name="uq_migration_ledger_source_version",
        ),
        Index("ix_migration_ledger_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    source_id: Mapped[str] = mapped_column(String(500), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("memory_items.id", ondelete="SET NULL"))
    migration_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LegacyArchive(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "legacy_archives"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_system", "archive_hash", name="uq_legacy_archives_source_hash"),
        Index("ix_legacy_archives_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    archive_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
