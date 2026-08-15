"""Add shadow campaign release-gate persistence and measurable claims."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_shadow_campaign_gate"
down_revision = "0008_provider_attempt_identity"
branch_labels = None
depends_on = None

TENANT_TABLES = (
    "shadow_campaigns",
    "shadow_run_results",
    "shadow_manual_reviews",
)


def _id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def _tenant_column() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def _json_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def _json_array() -> sa.TextClause:
    return sa.text("'[]'::jsonb")


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _add_compatibility_columns() -> None:
    op.add_column(
        "conversations",
        sa.Column("creation_idempotency_key", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("creation_request_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_conversations_tenant_user_creation_key",
        "conversations",
        ["tenant_id", "user_id", "creation_idempotency_key"],
    )
    op.create_check_constraint(
        op.f("ck_conversations_creation_idempotency_pair"),
        "conversations",
        "(creation_idempotency_key IS NULL) = (creation_request_hash IS NULL)",
    )

    op.create_unique_constraint(
        "uq_fact_requirements_tenant_run_id",
        "fact_requirements",
        ["tenant_id", "run_id", "id"],
    )
    op.add_column(
        "answer_claims",
        sa.Column("claim_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "answer_claims",
        sa.Column(
            "fact_requirement_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_answer_claims_kind_fact_binding"),
        "answer_claims",
        "(claim_kind IS NULL OR claim_kind IN "
        "('FACTUAL', 'UNCERTAINTY', 'COMMENTARY')) AND "
        "(claim_kind IS DISTINCT FROM 'FACTUAL' OR fact_requirement_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_answer_claims_tenant_run_fact",
        "answer_claims",
        "fact_requirements",
        ["tenant_id", "run_id", "fact_requirement_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )


def _create_campaigns() -> None:
    money = sa.Numeric(20, 10)
    op.create_table(
        "shadow_campaigns",
        _id_column(),
        _tenant_column(),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("creation_idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("creation_request_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_smoke_campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_smoke_decision_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="CREATED", nullable=False),
        sa.Column("gate_status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("stop_intent", sa.String(length=32), server_default="NONE", nullable=False),
        sa.Column("stop_reason", sa.String(length=200), nullable=True),
        sa.Column("profile_version", sa.String(length=100), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("gate_policy_version", sa.String(length=100), nullable=False),
        sa.Column("gate_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("gate_policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_version", sa.String(length=100), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_case_count", sa.Integer(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("review_rubric_version", sa.String(length=100), nullable=False),
        sa.Column("review_rubric_hash", sa.String(length=64), nullable=False),
        sa.Column("review_rubric_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("cost_rate_version", sa.String(length=100), nullable=False),
        sa.Column("cost_rate_hash", sa.String(length=64), nullable=False),
        sa.Column("cost_rate_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("candidate_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("candidate_source_clean", sa.Boolean(), nullable=False),
        sa.Column("candidate_image_id", sa.String(length=200), nullable=False),
        sa.Column("candidate_oci_revision", sa.String(length=64), nullable=False),
        sa.Column("alembic_head", sa.String(length=100), nullable=False),
        sa.Column("candidate_config_hash", sa.String(length=64), nullable=False),
        sa.Column("harness_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("harness_source_clean", sa.Boolean(), nullable=False),
        sa.Column("harness_fileset_hash", sa.String(length=64), nullable=False),
        sa.Column("collector_schema_version", sa.String(length=100), nullable=False),
        sa.Column("environment_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("environment_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("max_runs", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_stop_threshold", money, nullable=False),
        sa.Column("provider_call_admission_ceiling", sa.Integer(), nullable=False),
        sa.Column("provider_call_structural_ceiling", sa.Integer(), nullable=False),
        sa.Column("planned_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("submitted_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("collected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("degraded_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("possibly_billed_call_charge", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("observed_completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("observed_estimated_cost", money, server_default="0", nullable=False),
        sa.Column("possibly_billed_cost_charge", money, server_default="0", nullable=False),
        sa.Column("reserved_estimated_cost", money, server_default="0", nullable=False),
        sa.Column("possibly_billed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("automatic_gate_status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("manual_review_status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("final_json_uri", sa.Text(), nullable=True),
        sa.Column("final_json_sha256", sa.String(length=64), nullable=True),
        sa.Column("final_markdown_uri", sa.Text(), nullable=True),
        sa.Column("final_markdown_sha256", sa.String(length=64), nullable=True),
        sa.Column("decision_input_hash", sa.String(length=64), nullable=True),
        sa.Column("decision_hash", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_wall_clock_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "created_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_shadow_campaigns_owner",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_smoke_campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_campaigns_parent_smoke",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shadow_campaigns"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_shadow_campaigns_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "creation_idempotency_key",
            name="uq_shadow_campaigns_owner_creation_key",
        ),
        sa.CheckConstraint(
            "status IN ('CREATED', 'RUNNING', 'STOPPING', 'PAUSED', "
            "'AWAITING_REVIEW', 'COMPLETED', 'ABORTED')",
            name=op.f("ck_shadow_campaigns_status"),
        ),
        sa.CheckConstraint(
            "gate_status IN ('PENDING', 'PASS', 'FAIL', 'INSUFFICIENT_SAMPLE')",
            name=op.f("ck_shadow_campaigns_gate_status"),
        ),
        sa.CheckConstraint(
            "stop_intent IN ('NONE', 'PAUSE', 'ABORT', 'FATAL', 'BUDGET', "
            "'CALL_CEILING')",
            name=op.f("ck_shadow_campaigns_stop_intent"),
        ),
        sa.CheckConstraint(
            "(parent_smoke_campaign_id IS NULL) = (parent_smoke_decision_hash IS NULL)",
            name=op.f("ck_shadow_campaigns_parent_smoke_pair"),
        ),
        sa.CheckConstraint(
            "max_runs > 0 AND max_concurrency BETWEEN 1 AND 2 AND "
            "provider_call_admission_ceiling > 0 AND "
            "provider_call_admission_ceiling <= provider_call_structural_ceiling AND "
            "estimated_cost_stop_threshold > 0",
            name=op.f("ck_shadow_campaigns_profile_limits"),
        ),
        sa.CheckConstraint(
            "planned_count >= 0 AND submitted_count >= 0 AND collected_count >= 0 "
            "AND failed_count >= 0 AND skipped_count >= 0 AND degraded_count >= 0 "
            "AND observed_provider_calls >= 0 AND possibly_billed_call_charge >= 0 "
            "AND reserved_provider_calls >= 0 AND observed_prompt_tokens >= 0 "
            "AND observed_completion_tokens >= 0 AND observed_estimated_cost >= 0 "
            "AND possibly_billed_cost_charge >= 0 AND reserved_estimated_cost >= 0 "
            "AND possibly_billed_count >= 0 AND active_wall_clock_ms >= 0 "
            "AND version >= 0",
            name=op.f("ck_shadow_campaigns_nonnegative_ledger"),
        ),
        sa.CheckConstraint(
            "(final_json_uri IS NULL AND final_json_sha256 IS NULL AND "
            "final_markdown_uri IS NULL AND final_markdown_sha256 IS NULL AND "
            "decision_input_hash IS NULL AND decision_hash IS NULL) OR "
            "(final_json_uri IS NOT NULL AND final_json_sha256 IS NOT NULL AND "
            "final_markdown_uri IS NOT NULL AND final_markdown_sha256 IS NOT NULL "
            "AND decision_input_hash IS NOT NULL AND decision_hash IS NOT NULL)",
            name=op.f("ck_shadow_campaigns_final_report_binding"),
        ),
    )
    op.create_index(
        "ix_shadow_campaigns_owner_created",
        "shadow_campaigns",
        ["tenant_id", "created_by_user_id", "created_at"],
    )
    op.create_index(
        "ix_shadow_campaigns_status",
        "shadow_campaigns",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_shadow_campaigns_retention",
        "shadow_campaigns",
        ["retention_until"],
    )


def _create_results() -> None:
    money = sa.Numeric(20, 10)
    op.create_table(
        "shadow_run_results",
        _id_column(),
        _tenant_column(),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("search_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("case_id", sa.String(length=80), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("schedule_ordinal", sa.Integer(), nullable=False),
        sa.Column("manual_review_selected", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("locale", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("answerability", sa.String(length=32), nullable=False),
        sa.Column("expected_mode", sa.String(length=32), nullable=False),
        sa.Column("scheduling_state", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversation_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("submission_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("collector_attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conversation_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("message_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("submission_request_hash", sa.String(length=64), nullable=False),
        sa.Column("actual_mode", sa.String(length=32), nullable=True),
        sa.Column("run_status", sa.String(length=32), nullable=True),
        sa.Column("answer_quality", sa.String(length=32), nullable=True),
        sa.Column("run_stop_reason", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("minimum_required_facts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fact_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fact_covered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fact_gap", sa.Integer(), server_default="0", nullable=False),
        sa.Column("plan_completeness_failure", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("factual_claim_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("nonfactual_claim_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cited_factual_claim_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("valid_citation_chain_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("traceability_violation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gold_assertion_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gold_assertion_passed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gold_assertion_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gold_assertion_not_applicable", sa.Integer(), server_default="0", nullable=False),
        sa.Column("oracle_version", sa.String(length=100), nullable=True),
        sa.Column("query_pollution_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("model_call_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("estimated_cost", money, server_default="0", nullable=False),
        sa.Column("degraded", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("provider_success_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provider_failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=200), nullable=True),
        sa.Column("failed_phase", sa.String(length=100), nullable=True),
        sa.Column("stable_skip_reason", sa.String(length=100), nullable=True),
        sa.Column("reserved_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_estimated_cost", money, server_default="0", nullable=False),
        sa.Column("reservation_state", sa.String(length=32), server_default="NONE", nullable=False),
        sa.Column("settled_observed_provider_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("settled_observed_cost", money, server_default="0", nullable=False),
        sa.Column("possibly_billed_call_charge", sa.Integer(), server_default="0", nullable=False),
        sa.Column("possibly_billed_cost_charge", money, server_default="0", nullable=False),
        sa.Column("reservation_reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_violation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source_terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_snapshot_digest", sa.String(length=64), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collector_schema_version", sa.String(length=100), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_results_campaign",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_shadow_results_conversation",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "search_run_id"],
            ["search_runs.tenant_id", "search_runs.id"],
            name="fk_shadow_results_search_run",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shadow_run_results"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_shadow_results_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "campaign_id", "id", name="uq_shadow_results_tenant_campaign_id"
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "case_id",
            "repetition",
            name="uq_shadow_results_campaign_case_repetition",
        ),
        sa.CheckConstraint(
            "scheduling_state IN ('PENDING', 'CLAIMED', 'CONVERSATION_BOUND', "
            "'SUBMITTED', 'COLLECTED', 'FAILED', 'SKIPPED')",
            name=op.f("ck_shadow_run_results_scheduling_state"),
        ),
        sa.CheckConstraint(
            "reservation_state IN ('NONE', 'ACTIVE', 'SETTLED', 'RELEASED')",
            name=op.f("ck_shadow_run_results_reservation_state"),
        ),
        sa.CheckConstraint(
            "(scheduling_state = 'PENDING' AND conversation_id IS NULL AND "
            "search_run_id IS NULL) OR "
            "(scheduling_state = 'CLAIMED' AND search_run_id IS NULL) OR "
            "(scheduling_state = 'CONVERSATION_BOUND' AND conversation_id IS NOT NULL "
            "AND search_run_id IS NULL) OR "
            "(scheduling_state IN ('SUBMITTED', 'COLLECTED') AND "
            "conversation_id IS NOT NULL AND search_run_id IS NOT NULL) OR "
            "scheduling_state = 'FAILED' OR "
            "(scheduling_state = 'SKIPPED' AND search_run_id IS NULL)",
            name=op.f("ck_shadow_run_results_state_bindings"),
        ),
        sa.CheckConstraint(
            "(scheduling_state IN ('CLAIMED', 'CONVERSATION_BOUND') AND "
            "lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(scheduling_state NOT IN ('CLAIMED', 'CONVERSATION_BOUND') AND "
            "lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_shadow_run_results_lease_binding"),
        ),
        sa.CheckConstraint(
            "(reservation_state = 'NONE' AND reserved_provider_calls = 0 AND "
            "reserved_estimated_cost = 0 AND reservation_reserved_at IS NULL AND "
            "budget_settled_at IS NULL AND reservation_released_at IS NULL) OR "
            "(reservation_state = 'ACTIVE' AND reserved_provider_calls > 0 AND "
            "reservation_reserved_at IS NOT NULL AND budget_settled_at IS NULL AND "
            "reservation_released_at IS NULL) OR "
            "(reservation_state = 'SETTLED' AND reservation_reserved_at IS NOT NULL "
            "AND budget_settled_at IS NOT NULL AND reservation_released_at IS NULL) OR "
            "(reservation_state = 'RELEASED' AND reservation_reserved_at IS NOT NULL "
            "AND budget_settled_at IS NULL AND reservation_released_at IS NOT NULL)",
            name=op.f("ck_shadow_run_results_reservation_lifecycle"),
        ),
        sa.CheckConstraint(
            "repetition > 0 AND schedule_ordinal > 0 AND conversation_attempt_count >= 0 "
            "AND submission_attempt_count >= 0 AND collector_attempt_count >= 0 "
            "AND minimum_required_facts >= 0 AND fact_total >= 0 AND fact_covered >= 0 "
            "AND fact_gap >= 0 AND factual_claim_count >= 0 AND nonfactual_claim_count >= 0 "
            "AND cited_factual_claim_count >= 0 AND valid_citation_chain_count >= 0 "
            "AND traceability_violation_count >= 0 AND gold_assertion_total >= 0 "
            "AND gold_assertion_passed >= 0 AND gold_assertion_failed >= 0 "
            "AND gold_assertion_not_applicable >= 0 AND query_pollution_count >= 0 "
            "AND model_call_count >= 0 AND prompt_tokens >= 0 AND completion_tokens >= 0 "
            "AND estimated_cost >= 0 AND provider_success_count >= 0 "
            "AND provider_failure_count >= 0 AND reserved_provider_calls >= 0 "
            "AND reserved_estimated_cost >= 0 AND settled_observed_provider_calls >= 0 "
            "AND settled_observed_cost >= 0 AND possibly_billed_call_charge >= 0 "
            "AND possibly_billed_cost_charge >= 0 AND version >= 0",
            name=op.f("ck_shadow_run_results_nonnegative_metrics"),
        ),
    )
    op.create_index(
        "ix_shadow_results_claim",
        "shadow_run_results",
        ["tenant_id", "campaign_id", "scheduling_state", "lease_expires_at"],
    )
    op.create_index(
        "ix_shadow_results_run",
        "shadow_run_results",
        ["tenant_id", "search_run_id"],
    )
    op.create_index(
        "ix_shadow_results_review_selected",
        "shadow_run_results",
        ["tenant_id", "campaign_id", "manual_review_selected"],
    )


def _create_reviews() -> None:
    op.create_table(
        "shadow_manual_reviews",
        _id_column(),
        _tenant_column(),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rubric_version", sa.String(length=100), nullable=False),
        sa.Column("correctness_verdict", sa.String(length=32), nullable=False),
        sa.Column("citation_relevance", sa.String(length=32), nullable=False),
        sa.Column("source_appropriateness", sa.String(length=32), nullable=False),
        sa.Column("freshness", sa.String(length=32), nullable=False),
        sa.Column("completeness", sa.String(length=32), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), server_default=_json_array(), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("reviewer_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_reviews_campaign",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "result_id"],
            [
                "shadow_run_results.tenant_id",
                "shadow_run_results.campaign_id",
                "shadow_run_results.id",
            ],
            name="fk_shadow_reviews_result",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "reviewer_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_shadow_reviews_reviewer",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_shadow_manual_reviews"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_shadow_reviews_tenant_id_id"),
        sa.UniqueConstraint(
            "result_id", "rubric_version", name="uq_shadow_reviews_result_rubric"
        ),
        sa.CheckConstraint(
            "correctness_verdict IN "
            "('CORRECT', 'MINOR_ERROR', 'MAJOR_ERROR', 'UNREVIEWABLE')",
            name=op.f("ck_shadow_manual_reviews_verdict"),
        ),
        sa.CheckConstraint(
            "citation_relevance IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "source_appropriateness IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "freshness IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "completeness IN ('PASS', 'FAIL', 'NOT_APPLICABLE')",
            name=op.f("ck_shadow_manual_reviews_scores"),
        ),
        sa.CheckConstraint(
            "(actor_type = 'HUMAN' AND reviewer_user_id IS NOT NULL) OR "
            "(actor_type = 'SYSTEM' AND reviewer_user_id IS NULL)",
            name=op.f("ck_shadow_manual_reviews_actor_principal"),
        ),
    )
    op.create_index(
        "ix_shadow_reviews_campaign",
        "shadow_manual_reviews",
        ["tenant_id", "campaign_id", "reviewed_at"],
    )


def upgrade() -> None:
    _add_compatibility_columns()
    _create_campaigns()
    _create_results()
    _create_reviews()
    for table in TENANT_TABLES:
        _enable_rls(table)


def downgrade() -> None:
    op.drop_table("shadow_manual_reviews")
    op.drop_table("shadow_run_results")
    op.drop_table("shadow_campaigns")
    op.drop_constraint(
        "fk_answer_claims_tenant_run_fact",
        "answer_claims",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_answer_claims_kind_fact_binding"),
        "answer_claims",
        type_="check",
    )
    op.drop_column("answer_claims", "fact_requirement_id")
    op.drop_column("answer_claims", "claim_kind")
    op.drop_constraint(
        "uq_fact_requirements_tenant_run_id",
        "fact_requirements",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_conversations_creation_idempotency_pair"),
        "conversations",
        type_="check",
    )
    op.drop_constraint(
        "uq_conversations_tenant_user_creation_key",
        "conversations",
        type_="unique",
    )
    op.drop_column("conversations", "creation_request_hash")
    op.drop_column("conversations", "creation_idempotency_key")
