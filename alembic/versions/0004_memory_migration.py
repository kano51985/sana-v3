"""Create user-memory and replay-safe migration tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_memory_migration"
down_revision = "0003_search_evidence"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "memory_items",
    "memory_embeddings",
    "migration_ledger",
    "legacy_archives",
)


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("memory_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_items"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_memory_items_tenant_id_id"),
    )
    op.create_index("ix_memory_items_tenant_user_updated", "memory_items", ["tenant_id", "user_id", "updated_at"])
    op.create_index("ix_memory_items_tenant_content_hash", "memory_items", ["tenant_id", "content_hash"])
    op.create_table(
        "memory_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_memory_embeddings"),
        sa.UniqueConstraint("memory_item_id", "model", name="uq_memory_embeddings_item_model"),
    )
    op.create_index("ix_memory_embeddings_tenant_item", "memory_embeddings", ["tenant_id", "memory_item_id"])
    op.create_table(
        "migration_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.String(length=500), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("migration_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["memory_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_migration_ledger"),
        sa.UniqueConstraint("tenant_id", "source_system", "source_id", "migration_version", name="uq_migration_ledger_source_version"),
    )
    op.create_index("ix_migration_ledger_tenant_status", "migration_ledger", ["tenant_id", "status"])
    op.create_table(
        "legacy_archives",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("archive_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_legacy_archives"),
        sa.UniqueConstraint("tenant_id", "source_system", "archive_hash", name="uq_legacy_archives_source_hash"),
    )
    op.create_index("ix_legacy_archives_tenant_created", "legacy_archives", ["tenant_id", "created_at"])
    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
