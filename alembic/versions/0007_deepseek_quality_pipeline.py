"""Add durable model audit and complete evidence lineage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_deepseek_quality_pipeline"
down_revision = "0006_merge_evidence_memory_heads"
branch_labels = None
depends_on = None

TENANT_TABLES = ("model_invocations",)


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
        "model_invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reused_from_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("call_no", sa.Integer(), nullable=False),
        sa.Column("logical_call_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("billing_disposition", sa.String(length=32), nullable=False),
        sa.Column("provider_called", sa.Boolean(), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("span_id", sa.String(length=16), nullable=False),
        sa.Column("prompt_template_version", sa.String(length=100), nullable=False),
        sa.Column("parser_schema_version", sa.String(length=100), nullable=False),
        sa.Column("output_format", sa.String(length=32), nullable=False),
        sa.Column("thinking_mode", sa.String(length=32), nullable=False),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("output_chars", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_artifact_uri", sa.Text(), nullable=True),
        sa.Column("output_artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("provider_response_id", sa.String(length=200), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["search_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["step_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reused_from_id"], ["model_invocations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_model_invocations"),
        sa.UniqueConstraint("attempt_id", "role", "call_no", name="uq_model_invocations_attempt_role_call"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_model_invocations_tenant_id_id"),
    )
    op.create_index(
        "ix_model_invocations_tenant_run_started",
        "model_invocations",
        ["tenant_id", "run_id", "started_at"],
    )
    op.create_index(
        "ix_model_invocations_logical_call",
        "model_invocations",
        ["tenant_id", "logical_call_key", "status"],
    )
    op.create_index(
        "ix_model_invocations_open_attempt",
        "model_invocations",
        ["tenant_id", "attempt_id", "status"],
    )
    _enable_rls("model_invocations")

    op.add_column("evidence_candidates", sa.Column("source_identity", sa.String(length=500), nullable=True))
    op.add_column("evidence_candidates", sa.Column("source_authority", sa.String(length=32), nullable=True))
    op.execute(
        """
        UPDATE evidence_candidates AS ec
        SET source_identity = COALESCE(NULLIF(lower(d.source_host), ''), 'unknown'),
            source_authority = 'UNKNOWN'
        FROM document_versions AS dv
        JOIN documents AS d ON d.id = dv.document_id AND d.tenant_id = dv.tenant_id
        WHERE ec.document_version_id = dv.id AND ec.tenant_id = dv.tenant_id
        """
    )
    op.execute(
        "UPDATE evidence_candidates SET source_identity = 'unknown' "
        "WHERE source_identity IS NULL"
    )
    op.execute(
        "UPDATE evidence_candidates SET source_authority = 'UNKNOWN' "
        "WHERE source_authority IS NULL"
    )
    op.alter_column("evidence_candidates", "source_identity", nullable=False)
    op.alter_column("evidence_candidates", "source_authority", nullable=False)

    op.add_column("citations", sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("citations", sa.Column("document_chunk_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("citations", sa.Column("quote", sa.Text(), nullable=True))
    op.add_column("citations", sa.Column("start_offset", sa.Integer(), nullable=True))
    op.add_column("citations", sa.Column("end_offset", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE citations AS c
        SET document_version_id = ec.document_version_id,
            document_chunk_id = ec.document_chunk_id,
            quote = ec.quote,
            start_offset = ec.start_offset,
            end_offset = ec.end_offset
        FROM verified_evidence AS ve
        JOIN evidence_candidates AS ec
          ON ec.id = ve.candidate_id AND ec.tenant_id = ve.tenant_id
        WHERE c.verified_evidence_id = ve.id AND c.tenant_id = ve.tenant_id
        """
    )
    for column in (
        "document_version_id",
        "document_chunk_id",
        "quote",
        "start_offset",
        "end_offset",
    ):
        op.alter_column("citations", column, nullable=False)
    op.create_foreign_key(
        "fk_citations_document_version_id_document_versions",
        "citations",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_citations_document_chunk_id_document_chunks",
        "citations",
        "document_chunks",
        ["document_chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_citations_document_chunk_id_document_chunks",
        "citations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_citations_document_version_id_document_versions",
        "citations",
        type_="foreignkey",
    )
    for column in (
        "end_offset",
        "start_offset",
        "quote",
        "document_chunk_id",
        "document_version_id",
    ):
        op.drop_column("citations", column)
    op.drop_column("evidence_candidates", "source_authority")
    op.drop_column("evidence_candidates", "source_identity")
    op.drop_table("model_invocations")
