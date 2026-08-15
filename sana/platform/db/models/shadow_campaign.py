"""Tenant-scoped release campaign, result ledger, and manual review mappings."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sana.platform.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


_MONEY = Numeric(20, 10)


class ShadowCampaignRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shadow_campaigns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_shadow_campaigns_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "creation_idempotency_key",
            name="uq_shadow_campaigns_owner_creation_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_shadow_campaigns_owner",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_smoke_campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_campaigns_parent_smoke",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status IN ('CREATED', 'RUNNING', 'STOPPING', 'PAUSED', "
            "'AWAITING_REVIEW', 'COMPLETED', 'ABORTED')",
            name="status",
        ),
        CheckConstraint(
            "gate_status IN ('PENDING', 'PASS', 'FAIL', 'INSUFFICIENT_SAMPLE')",
            name="gate_status",
        ),
        CheckConstraint(
            "stop_intent IN ('NONE', 'PAUSE', 'ABORT', 'FATAL', 'BUDGET', "
            "'CALL_CEILING')",
            name="stop_intent",
        ),
        CheckConstraint(
            "(parent_smoke_campaign_id IS NULL) = (parent_smoke_decision_hash IS NULL)",
            name="parent_smoke_pair",
        ),
        CheckConstraint(
            "max_runs > 0 AND max_concurrency BETWEEN 1 AND 2 AND "
            "provider_call_admission_ceiling > 0 AND "
            "provider_call_admission_ceiling <= provider_call_structural_ceiling AND "
            "estimated_cost_stop_threshold > 0",
            name="profile_limits",
        ),
        CheckConstraint(
            "planned_count >= 0 AND submitted_count >= 0 AND collected_count >= 0 "
            "AND failed_count >= 0 AND skipped_count >= 0 AND degraded_count >= 0 "
            "AND observed_provider_calls >= 0 AND possibly_billed_call_charge >= 0 "
            "AND reserved_provider_calls >= 0 AND observed_prompt_tokens >= 0 "
            "AND observed_completion_tokens >= 0 AND observed_estimated_cost >= 0 "
            "AND possibly_billed_cost_charge >= 0 AND reserved_estimated_cost >= 0 "
            "AND possibly_billed_count >= 0 AND active_wall_clock_ms >= 0 "
            "AND planned_count <= max_runs AND submitted_count <= max_runs "
            "AND collected_count <= max_runs AND failed_count <= max_runs "
            "AND skipped_count <= max_runs AND degraded_count <= max_runs "
            "AND possibly_billed_count <= max_runs "
            "AND reserved_provider_calls <= provider_call_structural_ceiling "
            "AND version >= 0",
            name="nonnegative_ledger",
        ),
        CheckConstraint(
            "(final_json_uri IS NULL AND final_json_sha256 IS NULL AND "
            "final_markdown_uri IS NULL AND final_markdown_sha256 IS NULL AND "
            "decision_input_hash IS NULL AND decision_hash IS NULL) OR "
            "(final_json_uri IS NOT NULL AND final_json_sha256 IS NOT NULL AND "
            "final_markdown_uri IS NOT NULL AND final_markdown_sha256 IS NOT NULL "
            "AND decision_input_hash IS NOT NULL AND decision_hash IS NOT NULL)",
            name="final_report_binding",
        ),
        Index(
            "ix_shadow_campaigns_owner_created",
            "tenant_id",
            "created_by_user_id",
            "created_at",
        ),
        Index("ix_shadow_campaigns_status", "tenant_id", "status", "updated_at"),
        Index("ix_shadow_campaigns_retention", "retention_until"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    creation_idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    creation_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_smoke_campaign_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    parent_smoke_decision_hash: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="CREATED", server_default="CREATED"
    )
    gate_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    stop_intent: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NONE", server_default="NONE"
    )
    stop_reason: Mapped[str | None] = mapped_column(String(200))

    profile_version: Mapped[str] = mapped_column(String(100), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gate_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    gate_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False)
    review_rubric_version: Mapped[str] = mapped_column(String(100), nullable=False)
    review_rubric_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_rubric_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cost_rate_version: Mapped[str] = mapped_column(String(100), nullable=False)
    cost_rate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_rate_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    candidate_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_source_clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    candidate_image_id: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_oci_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    alembic_head: Mapped[str] = mapped_column(String(100), nullable=False)
    candidate_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_source_clean: Mapped[bool] = mapped_column(Boolean, nullable=False)
    harness_fileset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collector_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    environment_identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    max_runs: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_stop_threshold: Mapped[Decimal] = mapped_column(_MONEY, nullable=False)
    provider_call_admission_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_call_structural_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)

    planned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    submitted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    collected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    degraded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    observed_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    possibly_billed_call_charge: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    observed_prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    observed_completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    observed_estimated_cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    possibly_billed_cost_charge: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    reserved_estimated_cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    possibly_billed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    automatic_gate_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", server_default="PENDING")
    manual_review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", server_default="PENDING")
    final_json_uri: Mapped[str | None] = mapped_column(Text)
    final_json_sha256: Mapped[str | None] = mapped_column(String(64))
    final_markdown_uri: Mapped[str | None] = mapped_column(Text)
    final_markdown_sha256: Mapped[str | None] = mapped_column(String(64))
    decision_input_hash: Mapped[str | None] = mapped_column(String(64))
    decision_hash: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_wall_clock_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ShadowRunResultRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "shadow_run_results"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_shadow_results_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "id",
            name="uq_shadow_results_tenant_campaign_id",
        ),
        UniqueConstraint(
            "campaign_id",
            "case_id",
            "repetition",
            name="uq_shadow_results_campaign_case_repetition",
        ),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            name="uq_shadow_results_tenant_conversation",
        ),
        UniqueConstraint(
            "tenant_id",
            "search_run_id",
            name="uq_shadow_results_tenant_search_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_results_campaign",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_shadow_results_conversation",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "search_run_id"],
            ["search_runs.tenant_id", "search_runs.id"],
            name="fk_shadow_results_search_run",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "scheduling_state IN ('PENDING', 'CLAIMED', 'CONVERSATION_BOUND', "
            "'SUBMITTED', 'COLLECTED', 'FAILED', 'SKIPPED')",
            name="scheduling_state",
        ),
        CheckConstraint(
            "reservation_state IN ('NONE', 'ACTIVE', 'SETTLED', 'RELEASED')",
            name="reservation_state",
        ),
        CheckConstraint(
            "(scheduling_state = 'PENDING' AND conversation_id IS NULL AND "
            "search_run_id IS NULL) OR "
            "(scheduling_state = 'CLAIMED' AND search_run_id IS NULL) OR "
            "(scheduling_state = 'CONVERSATION_BOUND' AND conversation_id IS NOT NULL "
            "AND search_run_id IS NULL) OR "
            "(scheduling_state IN ('SUBMITTED', 'COLLECTED') AND "
            "conversation_id IS NOT NULL AND search_run_id IS NOT NULL) OR "
            "scheduling_state = 'FAILED' OR "
            "(scheduling_state = 'SKIPPED' AND search_run_id IS NULL)",
            name="state_bindings",
        ),
        CheckConstraint(
            "(scheduling_state IN ('CLAIMED', 'CONVERSATION_BOUND') AND "
            "lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(scheduling_state NOT IN ('CLAIMED', 'CONVERSATION_BOUND') AND "
            "lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="lease_binding",
        ),
        CheckConstraint(
            "(reservation_state = 'NONE' AND reserved_provider_calls = 0 AND "
            "reserved_estimated_cost = 0 AND reservation_reserved_at IS NULL AND "
            "budget_settled_at IS NULL AND reservation_released_at IS NULL) OR "
            "(reservation_state = 'ACTIVE' AND reserved_provider_calls > 0 AND "
            "reservation_reserved_at IS NOT NULL AND budget_settled_at IS NULL AND "
            "reservation_released_at IS NULL) OR "
            "(reservation_state = 'SETTLED' AND reservation_reserved_at IS NOT NULL "
            "AND budget_settled_at IS NOT NULL AND reservation_released_at IS NULL "
            "AND source_terminal_at IS NOT NULL AND source_snapshot_digest IS NOT NULL) OR "
            "(reservation_state = 'RELEASED' AND reservation_reserved_at IS NOT NULL "
            "AND budget_settled_at IS NULL AND reservation_released_at IS NOT NULL)",
            name="reservation_lifecycle",
        ),
        CheckConstraint(
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
            name="nonnegative_metrics",
        ),
        Index(
            "ix_shadow_results_claim",
            "tenant_id",
            "campaign_id",
            "scheduling_state",
            "lease_expires_at",
        ),
        Index("ix_shadow_results_run", "tenant_id", "search_run_id"),
        Index(
            "ix_shadow_results_review_selected",
            "tenant_id",
            "campaign_id",
            "manual_review_selected",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    search_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    case_id: Mapped[str] = mapped_column(String(80), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    schedule_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_review_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    locale: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    answerability: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduling_state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", server_default="PENDING")
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conversation_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    submission_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    collector_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    conversation_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    message_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    submission_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    actual_mode: Mapped[str | None] = mapped_column(String(32))
    run_status: Mapped[str | None] = mapped_column(String(32))
    answer_quality: Mapped[str | None] = mapped_column(String(32))
    run_stop_reason: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    minimum_required_facts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fact_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fact_covered: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    fact_gap: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    plan_completeness_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    factual_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    nonfactual_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cited_factual_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    valid_citation_chain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    traceability_violation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gold_assertion_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gold_assertion_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gold_assertion_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    gold_assertion_not_applicable: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    oracle_version: Mapped[str | None] = mapped_column(String(100))
    query_pollution_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    estimated_cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    provider_success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    provider_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    error_class: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(200))
    failed_phase: Mapped[str | None] = mapped_column(String(100))
    stable_skip_reason: Mapped[str | None] = mapped_column(String(100))

    reserved_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_estimated_cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    reservation_state: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE", server_default="NONE")
    settled_observed_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    settled_observed_cost: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    possibly_billed_call_charge: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    possibly_billed_cost_charge: Mapped[Decimal] = mapped_column(_MONEY, nullable=False, default=0, server_default="0")
    reservation_reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_violation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    source_terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_snapshot_digest: Mapped[str | None] = mapped_column(String(64))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collector_schema_version: Mapped[str | None] = mapped_column(String(100))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class ShadowManualReviewRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "shadow_manual_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_shadow_reviews_tenant_id_id"),
        UniqueConstraint(
            "result_id",
            "rubric_version",
            name="uq_shadow_reviews_result_rubric",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["shadow_campaigns.tenant_id", "shadow_campaigns.id"],
            name="fk_shadow_reviews_campaign",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["tenant_id", "reviewer_user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_shadow_reviews_reviewer",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "correctness_verdict IN "
            "('CORRECT', 'MINOR_ERROR', 'MAJOR_ERROR', 'UNREVIEWABLE')",
            name="verdict",
        ),
        CheckConstraint(
            "citation_relevance IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "source_appropriateness IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "freshness IN ('PASS', 'FAIL', 'NOT_APPLICABLE') AND "
            "completeness IN ('PASS', 'FAIL', 'NOT_APPLICABLE')",
            name="scores",
        ),
        CheckConstraint(
            "(actor_type = 'HUMAN' AND reviewer_user_id IS NOT NULL) OR "
            "(actor_type = 'SYSTEM' AND reviewer_user_id IS NULL)",
            name="actor_principal",
        ),
        Index(
            "ix_shadow_reviews_campaign",
            "tenant_id",
            "campaign_id",
            "reviewed_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    campaign_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    result_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(100), nullable=False)
    correctness_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    citation_relevance: Mapped[str] = mapped_column(String(32), nullable=False)
    source_appropriateness: Mapped[str] = mapped_column(String(32), nullable=False)
    freshness: Mapped[str] = mapped_column(String(32), nullable=False)
    completeness: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
