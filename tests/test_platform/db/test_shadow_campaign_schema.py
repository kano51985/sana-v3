from pathlib import Path

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from sana.platform.db.base import Base
import sana.platform.db.models  # noqa: F401


def _constraint(table_name: str, name: str):
    return next(
        item
        for item in Base.metadata.tables[table_name].constraints
        if item.name == name
    )


def _column_names(constraint) -> set[str]:
    return {column.name for column in constraint.columns}


def test_conversation_and_claim_compatibility_columns_are_constrained() -> None:
    conversations = Base.metadata.tables["conversations"]
    claims = Base.metadata.tables["answer_claims"]
    facts = Base.metadata.tables["fact_requirements"]

    assert conversations.c.creation_idempotency_key.nullable is True
    assert conversations.c.creation_request_hash.nullable is True
    assert _column_names(
        _constraint("conversations", "uq_conversations_tenant_user_creation_key")
    ) == {"tenant_id", "user_id", "creation_idempotency_key"}
    assert claims.c.claim_kind.nullable is True
    assert claims.c.fact_requirement_id.nullable is True
    assert isinstance(
        _constraint("answer_claims", "ck_answer_claims_kind_fact_binding"),
        CheckConstraint,
    )
    assert _column_names(
        _constraint("fact_requirements", "uq_fact_requirements_tenant_run_id")
    ) == {"tenant_id", "run_id", "id"}
    fact_fk = _constraint("answer_claims", "fk_answer_claims_tenant_run_fact")
    assert isinstance(fact_fk, ForeignKeyConstraint)
    assert fact_fk.deferrable is True
    assert fact_fk.initially == "DEFERRED"


def test_campaign_schema_has_owner_identity_provenance_and_ledger() -> None:
    campaign = Base.metadata.tables["shadow_campaigns"]
    expected = {
        "created_by_user_id",
        "creation_idempotency_key",
        "creation_request_hash",
        "parent_smoke_campaign_id",
        "status",
        "gate_status",
        "stop_intent",
        "profile_snapshot",
        "gate_policy_snapshot",
        "candidate_commit_sha",
        "candidate_image_id",
        "harness_fileset_hash",
        "environment_snapshot",
        "observed_provider_calls",
        "possibly_billed_call_charge",
        "reserved_provider_calls",
        "observed_estimated_cost",
        "reserved_estimated_cost",
        "decision_input_hash",
        "decision_hash",
        "retention_until",
        "version",
    }
    assert expected <= set(campaign.c.keys())
    assert _column_names(
        _constraint("shadow_campaigns", "uq_shadow_campaigns_owner_creation_key")
    ) == {"tenant_id", "created_by_user_id", "creation_idempotency_key"}
    parent_fk = _constraint("shadow_campaigns", "fk_shadow_campaigns_parent_smoke")
    assert isinstance(parent_fk, ForeignKeyConstraint)
    assert parent_fk.deferrable is True
    assert parent_fk.initially == "DEFERRED"
    for forbidden in ("prompt", "answer", "query", "quote", "api_key", "token"):
        assert forbidden not in campaign.c


def test_result_schema_has_recovery_measurement_and_exactly_once_settlement() -> None:
    result = Base.metadata.tables["shadow_run_results"]
    expected = {
        "campaign_id",
        "conversation_id",
        "search_run_id",
        "schedule_ordinal",
        "manual_review_selected",
        "scheduling_state",
        "lease_owner",
        "lease_expires_at",
        "conversation_idempotency_key",
        "message_idempotency_key",
        "submission_request_hash",
        "factual_claim_count",
        "cited_factual_claim_count",
        "traceability_violation_count",
        "reservation_state",
        "reserved_provider_calls",
        "settled_observed_provider_calls",
        "possibly_billed_call_charge",
        "budget_settled_at",
        "source_snapshot_digest",
        "collector_schema_version",
        "stable_skip_reason",
    }
    assert expected <= set(result.c.keys())
    assert _column_names(
        _constraint("shadow_run_results", "uq_shadow_results_campaign_case_repetition")
    ) == {"campaign_id", "case_id", "repetition"}
    for name in (
        "fk_shadow_results_campaign",
        "fk_shadow_results_conversation",
        "fk_shadow_results_search_run",
    ):
        fk = _constraint("shadow_run_results", name)
        assert isinstance(fk, ForeignKeyConstraint)
        assert fk.deferrable is True
        assert fk.initially == "DEFERRED"
    assert isinstance(
        _constraint(
            "shadow_run_results",
            "ck_shadow_run_results_reservation_lifecycle",
        ),
        CheckConstraint,
    )


def test_manual_review_is_structured_and_has_actor_invariant() -> None:
    review = Base.metadata.tables["shadow_manual_reviews"]
    assert {
        "campaign_id",
        "result_id",
        "rubric_version",
        "correctness_verdict",
        "citation_relevance",
        "source_appropriateness",
        "freshness",
        "completeness",
        "reason_codes",
        "actor_type",
        "reviewer_user_id",
        "reviewed_at",
    } <= set(review.c.keys())
    assert _column_names(
        _constraint("shadow_manual_reviews", "uq_shadow_reviews_result_rubric")
    ) == {"result_id", "rubric_version"}
    assert isinstance(
        _constraint(
            "shadow_manual_reviews",
            "ck_shadow_manual_reviews_actor_principal",
        ),
        CheckConstraint,
    )
    for forbidden in ("free_text", "prompt", "answer", "quote", "web_content"):
        assert forbidden not in review.c


def test_0009_is_linear_and_declares_all_new_rls_tables() -> None:
    migration = (
        Path(__file__).parents[3]
        / "alembic"
        / "versions"
        / "0009_shadow_campaign_release_gate.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert 'down_revision = "0008_provider_attempt_identity"' in source
    assert '"shadow_campaigns"' in source
    assert '"shadow_run_results"' in source
    assert '"shadow_manual_reviews"' in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "deferrable=True" in source
