"""Create search planning, retrieval, evidence and citation tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_search_evidence"
down_revision = "0002_orchestration_outbox"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "fact_requirements",
    "query_specs",
    "provider_attempts",
    "search_hits",
    "fetch_artifacts",
    "documents",
    "document_versions",
    "document_chunks",
    "evidence_candidates",
    "verified_evidence",
    "answer_claims",
    "citations",
)


def _tenant_column() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def _id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


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
        "fact_requirements",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_key", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("freshness", sa.String(length=32), nullable=False),
        sa.Column("consequence", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_fact_requirements"),
        sa.UniqueConstraint("run_id", "fact_key", name="uq_fact_requirements_run_key"),
    )
    op.create_index("ix_fact_requirements_tenant_run_status", "fact_requirements", ["tenant_id", "run_id", "status"])
    op.create_table(
        "query_specs",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_requirement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("query_key", sa.String(length=200), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("provider_class", sa.String(length=100), nullable=False),
        sa.Column("locale", sa.String(length=32), nullable=True),
        sa.Column("freshness_days", sa.Integer(), nullable=True),
        sa.Column("query_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_requirement_id"], ["fact_requirements.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_query_specs"),
        sa.UniqueConstraint("run_id", "plan_revision", "query_key", name="uq_query_specs_run_revision_key"),
    )
    op.create_index("ix_query_specs_tenant_run_revision", "query_specs", ["tenant_id", "run_id", "plan_revision"])
    op.create_table(
        "provider_attempts",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_spec_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["query_spec_id"], ["query_specs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_provider_attempts"),
        sa.UniqueConstraint("query_spec_id", "attempt_no", name="uq_provider_attempts_query_number"),
    )
    op.create_index("ix_provider_attempts_tenant_run_started", "provider_attempts", ["tenant_id", "run_id", "started_at"])
    op.create_table(
        "search_hits",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_attempt_id"], ["provider_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_search_hits"),
        sa.UniqueConstraint("provider_attempt_id", "url_hash", name="uq_search_hits_attempt_url"),
    )
    op.create_index("ix_search_hits_tenant_run_score", "search_hits", ["tenant_id", "run_id", "score"])
    op.create_table(
        "fetch_artifacts",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("search_hit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("fetcher", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(length=200), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["search_hit_id"], ["search_hits.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_fetch_artifacts"),
        sa.UniqueConstraint("run_id", "url_hash", "attempt_no", name="uq_fetch_artifacts_run_url_attempt"),
    )
    op.create_index("ix_fetch_artifacts_tenant_run_status", "fetch_artifacts", ["tenant_id", "run_id", "status"])
    op.create_table(
        "documents",
        _id_column(),
        _tenant_column(),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("canonical_url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_host", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("tenant_id", "canonical_url_hash", name="uq_documents_tenant_url"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id_id"),
    )
    op.create_index("ix_documents_tenant_updated", "documents", ["tenant_id", "updated_at"])
    op.create_table(
        "document_versions",
        _id_column(),
        _tenant_column(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fetch_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("text_length", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fetch_artifact_id"], ["fetch_artifacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "content_hash", name="uq_document_versions_document_hash"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_document_versions_tenant_id_id"),
    )
    op.create_index("ix_document_versions_tenant_document_fetched", "document_versions", ["tenant_id", "document_id", "fetched_at"])
    op.create_table(
        "document_chunks",
        _id_column(),
        _tenant_column(),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunks_version_ordinal"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_document_chunks_tenant_id_id"),
    )
    op.create_index("ix_document_chunks_tenant_version", "document_chunks", ["tenant_id", "document_version_id"])
    op.create_table(
        "evidence_candidates",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_requirement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("support_type", sa.String(length=32), nullable=False),
        sa.Column("candidate_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_requirement_id"], ["fact_requirements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_chunk_id"], ["document_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_candidates"),
        sa.UniqueConstraint("fact_requirement_id", "document_chunk_id", "quote_hash", name="uq_evidence_candidates_fact_chunk_quote"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_evidence_candidates_tenant_id_id"),
    )
    op.create_index("ix_evidence_candidates_tenant_run_fact", "evidence_candidates", ["tenant_id", "run_id", "fact_requirement_id"])
    op.create_table(
        "verified_evidence",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("verifier_version", sa.String(length=100), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["evidence_candidates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_verified_evidence"),
        sa.UniqueConstraint("candidate_id", name="uq_verified_evidence_candidate"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_verified_evidence_tenant_id_id"),
    )
    op.create_index("ix_verified_evidence_tenant_run_verdict", "verified_evidence", ["tenant_id", "run_id", "verdict"])
    op.create_table(
        "answer_claims",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_key", sa.String(length=200), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("support_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_answer_claims"),
        sa.UniqueConstraint("run_id", "claim_key", name="uq_answer_claims_run_key"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_answer_claims_tenant_id_id"),
    )
    op.create_index("ix_answer_claims_tenant_run", "answer_claims", ["tenant_id", "run_id"])
    op.create_table(
        "citations",
        _id_column(),
        _tenant_column(),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("answer_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verified_evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("rendered_url", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["answer_claim_id"], ["answer_claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["verified_evidence_id"], ["verified_evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_citations"),
        sa.UniqueConstraint("answer_claim_id", "ordinal", name="uq_citations_claim_ordinal"),
    )
    op.create_index("ix_citations_tenant_run", "citations", ["tenant_id", "run_id"])
    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
