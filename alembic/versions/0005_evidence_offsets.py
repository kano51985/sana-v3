"""Persist exact document offsets for every evidence candidate."""

from alembic import op
import sqlalchemy as sa


revision = "0005_evidence_offsets"
down_revision = "0004_memory_migration"
branch_labels = None
depends_on = None

TENANT_TABLES = ("evidence_candidates",)


def _restore_rls_policy() -> None:
    op.execute('ALTER TABLE "evidence_candidates" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "evidence_candidates" FORCE ROW LEVEL SECURITY')
    op.execute('DROP POLICY IF EXISTS tenant_isolation ON "evidence_candidates"')
    op.execute(
        'CREATE POLICY tenant_isolation ON "evidence_candidates" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column(
        "evidence_candidates",
        sa.Column("start_offset", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "evidence_candidates",
        sa.Column("end_offset", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_evidence_candidates_quote_offsets"),
        "evidence_candidates",
        "start_offset >= 0 AND end_offset >= start_offset",
    )
    op.alter_column("evidence_candidates", "start_offset", server_default=None)
    op.alter_column("evidence_candidates", "end_offset", server_default=None)
    _restore_rls_policy()


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_evidence_candidates_quote_offsets"),
        "evidence_candidates",
        type_="check",
    )
    op.drop_column("evidence_candidates", "end_offset")
    op.drop_column("evidence_candidates", "start_offset")
