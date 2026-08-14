"""Create durable workflow, attempt, outbox and event tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_orchestration_outbox"
down_revision = "0001_identity_conversation"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "search_runs",
    "search_steps",
    "step_attempts",
    "outbox_events",
    "run_events",
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
        "search_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("response_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("route_reason_codes", postgresql.ARRAY(sa.String(length=100)), server_default="{}", nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("route_confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("answer_quality", sa.String(length=32), nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("soft_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("budget_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["response_run_id"], ["response_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_search_runs"),
        sa.UniqueConstraint("response_run_id", name="uq_search_runs_response_run_id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_search_runs_tenant_id_id"),
    )
    op.create_index("ix_search_runs_tenant_status_created", "search_runs", ["tenant_id", "status", "created_at"])
    op.create_index("ix_search_runs_hard_deadline", "search_runs", ["status", "hard_deadline_at"])
    op.create_table(
        "search_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_key", sa.String(length=300), nullable=False),
        sa.Column("step_type", sa.String(length=100), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_search_steps"),
        sa.UniqueConstraint("run_id", "plan_revision", "step_key", name="uq_search_steps_run_revision_key"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_search_steps_tenant_id_id"),
    )
    op.create_index("ix_search_steps_reconcile", "search_steps", ["status", "retry_at", "updated_at"])
    op.create_index("ix_search_steps_tenant_run_status", "search_steps", ["tenant_id", "run_id", "status"])
    op.create_table(
        "step_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["search_steps.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_step_attempts"),
        sa.UniqueConstraint("step_id", "attempt_no", name="uq_step_attempts_step_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_step_attempts_idempotency_key"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_step_attempts_tenant_id_id"),
    )
    op.create_index("ix_step_attempts_lease_scan", "step_attempts", ["leased_until", "completed_at"])
    op.create_index("ix_step_attempts_tenant_run", "step_attempts", ["tenant_id", "run_id"])
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("trace_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=500), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_events_dedupe_key"),
    )
    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["available_at", "created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index("ix_outbox_events_tenant_aggregate", "outbox_events", ["tenant_id", "aggregate_id"])
    op.create_table(
        "run_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_run_events"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
    )
    op.create_index("ix_run_events_tenant_run_sequence", "run_events", ["tenant_id", "run_id", "sequence"])
    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
