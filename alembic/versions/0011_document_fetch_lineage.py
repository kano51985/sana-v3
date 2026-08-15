"""Record run-local fetch observations for content-stable document versions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_document_fetch_lineage"
down_revision = "0010_shadow_collector_audit"
branch_labels = None
depends_on = None

TENANT_TABLES = ("document_version_fetches",)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_fetch_artifacts_tenant_id_id",
        "fetch_artifacts",
        ["tenant_id", "id"],
    )
    op.create_table(
        "document_version_fetches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "fetch_artifact_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["search_runs.tenant_id", "search_runs.id"],
            name="fk_document_version_fetches_run",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_document_version_fetches_version",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "fetch_artifact_id"],
            ["fetch_artifacts.tenant_id", "fetch_artifacts.id"],
            name="fk_document_version_fetches_fetch",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_version_fetches"),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_document_version_fetches_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "document_version_id",
            "fetch_artifact_id",
            name="uq_document_version_fetches_run_version_fetch",
        ),
    )
    op.create_index(
        "ix_document_version_fetches_tenant_run",
        "document_version_fetches",
        ["tenant_id", "run_id"],
    )
    op.create_index(
        "ix_document_version_fetches_tenant_version",
        "document_version_fetches",
        ["tenant_id", "document_version_id"],
    )
    op.execute('ALTER TABLE "document_version_fetches" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "document_version_fetches" FORCE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY tenant_isolation ON "document_version_fetches" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table("document_version_fetches")
    op.drop_constraint(
        "uq_fetch_artifacts_tenant_id_id",
        "fetch_artifacts",
        type_="unique",
    )
