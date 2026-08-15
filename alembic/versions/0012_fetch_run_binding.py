"""Enforce that every document-version observation uses a fetch from its run."""

from alembic import op


revision = "0012_fetch_run_binding"
down_revision = "0011_document_fetch_lineage"
branch_labels = None
depends_on = None

TENANT_TABLES: tuple[str, ...] = ()


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_fetch_artifacts_tenant_run_id",
        "fetch_artifacts",
        ["tenant_id", "run_id", "id"],
    )
    op.drop_constraint(
        "fk_document_version_fetches_fetch",
        "document_version_fetches",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_document_version_fetches_fetch",
        "document_version_fetches",
        "fetch_artifacts",
        ["tenant_id", "run_id", "fetch_artifact_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_document_version_fetches_fetch",
        "document_version_fetches",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_document_version_fetches_fetch",
        "document_version_fetches",
        "fetch_artifacts",
        ["tenant_id", "fetch_artifact_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint(
        "uq_fetch_artifacts_tenant_run_id",
        "fetch_artifacts",
        type_="unique",
    )
