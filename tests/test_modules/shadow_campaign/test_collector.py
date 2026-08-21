from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from sana.modules.shadow_campaign.collector import (
    GoldAssertionStatus,
    RunSourceSnapshot,
    SourceAttempt,
    SourceCitation,
    SourceClaim,
    SourceEvidence,
    SourceFact,
    SourceFetch,
    SourceInvocation,
    SourceOutbox,
    SourceProviderAttempt,
    SourceQuery,
    SourceStep,
    collect_run_snapshot,
    source_snapshot_digest,
)
from sana.modules.shadow_campaign.domain import ErrorClass
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    CaseCategory,
    GoldAssertion,
    OracleType,
    ShadowCase,
)
from sana.modules.shadow_campaign.policy import CostRate
from sana.modules.shared.errors import InvariantViolation
from sana.modules.orchestration.domain import SearchMode
from sana.modules.evidence.domain import SourceAuthority


NOW = datetime(2026, 8, 15, tzinfo=UTC)
TENANT = UUID("10000000-0000-0000-0000-000000000001")
RUN = UUID("20000000-0000-0000-0000-000000000001")
CONVERSATION = UUID("30000000-0000-0000-0000-000000000001")
RESPONSE = UUID("40000000-0000-0000-0000-000000000001")
MESSAGE = UUID("50000000-0000-0000-0000-000000000001")
FACT = UUID("60000000-0000-0000-0000-000000000001")
QUERY = UUID("70000000-0000-0000-0000-000000000001")
PROVIDER_ATTEMPT = UUID("71000000-0000-0000-0000-000000000001")
STEP = UUID("80000000-0000-0000-0000-000000000001")
ATTEMPT = UUID("90000000-0000-0000-0000-000000000001")
INVOCATION = UUID("a0000000-0000-0000-0000-000000000001")
EVIDENCE = UUID("b0000000-0000-0000-0000-000000000001")
CANDIDATE = UUID("b1000000-0000-0000-0000-000000000001")
VERSION = UUID("b2000000-0000-0000-0000-000000000001")
CHUNK = UUID("b3000000-0000-0000-0000-000000000001")
CLAIM = UUID("c0000000-0000-0000-0000-000000000001")
CITATION = UUID("d0000000-0000-0000-0000-000000000001")
OUTBOX = UUID("e0000000-0000-0000-0000-000000000001")
FETCH = UUID("f0000000-0000-0000-0000-000000000001")
SOURCE_FETCH = UUID("f1000000-0000-0000-0000-000000000001")
SOURCE_RUN = UUID("f2000000-0000-0000-0000-000000000001")


def _case(*, deterministic: bool = True) -> ShadowCase:
    return ShadowCase(
        id="collector-case",
        prompt="What is stable?",
        locale="en",
        expected_mode=SearchMode.FAST,
        category=CaseCategory.VERSION,
        answerability=Answerability.ANSWERABLE,
        minimum_required_facts=1,
        gold_assertions=(
            GoldAssertion(
                "stable-answer",
                "normalized_contains_all",
                ("stable",),
                False,
            ),
        )
        if deterministic
        else (),
        oracle_type=OracleType.DETERMINISTIC if deterministic else OracleType.MANUAL_REQUIRED,
        valid_from=NOW - timedelta(days=1) if deterministic else None,
        valid_until=NOW + timedelta(days=1) if deterministic else None,
        required_source_classes=(SourceAuthority.OFFICIAL,),
        forbidden_query_terms=("private memory",),
        must_not_complete=False,
        tags=("collector",),
        smoke=True,
    )


def _rate() -> CostRate:
    return CostRate(
        "test-rate-v1",
        Decimal("1.0"),
        Decimal("2.0"),
        Decimal("0.006"),
    )


def _snapshot() -> RunSourceSnapshot:
    return RunSourceSnapshot(
        tenant_id=TENANT,
        run_id=RUN,
        conversation_id=CONVERSATION,
        response_run_id=RESPONSE,
        response_status="SUCCEEDED",
        output_message_id=MESSAGE,
        output_message_role="ASSISTANT",
        output_message_conversation_id=CONVERSATION,
        answer_text="The release is stable.",
        mode="FAST",
        status="SUCCEEDED",
        answer_quality="COMPLETE",
        stop_reason="FACTS_COVERED",
        created_at=NOW,
        hard_deadline_at=NOW + timedelta(seconds=15),
        completed_at=NOW + timedelta(seconds=2),
        version=8,
        budget_max_llm_calls=4,
        recorded_llm_call_count=1,
        recorded_prompt_tokens=100,
        recorded_completion_tokens=50,
        facts=(SourceFact(FACT, True, "VERIFIED", "CURRENT", "HIGH"),),
        queries=(SourceQuery(QUERY, FACT, 1, "direct", "stable release version"),),
        provider_attempts=(
            SourceProviderAttempt(
                PROVIDER_ATTEMPT,
                QUERY,
                "direct",
                "SUCCEEDED",
                NOW,
                NOW + timedelta(milliseconds=50),
            ),
        ),
        steps=(SourceStep(STEP, "synthesize", "SYNTHESIZE", 1, "SUCCEEDED", True),),
        attempts=(SourceAttempt(ATTEMPT, STEP, 1, NOW, NOW + timedelta(seconds=1)),),
        invocations=(
            SourceInvocation(
                INVOCATION,
                STEP,
                ATTEMPT,
                "SYNTHESIZER",
                "deepseek",
                "deepseek-chat",
                1,
                "COMPLETED",
                "BILLED",
                True,
                100,
                50,
                NOW,
                NOW + timedelta(milliseconds=800),
            ),
        ),
        evidence=(
            SourceEvidence(
                EVIDENCE,
                CANDIDATE,
                FACT,
                VERSION,
                CHUNK,
                2,
                8,
                6,
                "SUPPORTS",
                "OFFICIAL",
                "ACCEPTED",
                0.99,
                ("exact",),
                "verifier-v1",
                NOW + timedelta(seconds=1),
            ),
        ),
        claims=(SourceClaim(CLAIM, "FACTUAL", FACT, "VERIFIED", "It is stable."),),
        citations=(
            SourceCitation(CITATION, CLAIM, EVIDENCE, VERSION, CHUNK, 2, 8, 6, True),
        ),
        outbox=(
            SourceOutbox(
                OUTBOX,
                "search_step",
                STEP,
                "STEP_READY_FAST",
                NOW,
                NOW + timedelta(milliseconds=10),
            ),
        ),
    )


def _cache_fetch(decision: str) -> SourceFetch:
    stale = decision == "CACHE_STALE_IF_ERROR"
    return SourceFetch(
        id=FETCH,
        fetcher="document-cache",
        status="SUCCEEDED",
        fetched_at=NOW - timedelta(hours=1),
        decision=decision,
        policy_version="document-reuse-v1",
        strictest_freshness="STABLE",
        source_fetch_artifact_id=SOURCE_FETCH,
        source_run_id=SOURCE_RUN,
        source_document_version_id=VERSION,
        source_fetched_at=NOW - timedelta(hours=1),
        reused_at=NOW,
        reuse_age_seconds=3600,
        live_error_category="TRANSIENT" if stale else None,
        live_error_code="fetch_network_failure" if stale else None,
    )


def test_collector_builds_traceable_metrics_and_conservative_cost() -> None:
    outcome = collect_run_snapshot(
        _snapshot(),
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.source_snapshot_digest == collect_run_snapshot(
        _snapshot(), _case(), _rate(), oracle_version="shadow-cases-v1"
    ).source_snapshot_digest
    assert len(outcome.source_snapshot_digest) == 64
    assert outcome.latency_ms == 2_000
    assert (outcome.fact_total, outcome.fact_covered, outcome.fact_gap) == (1, 1, 0)
    assert outcome.traceability_violation_count == 0
    assert outcome.valid_citation_chain_count == 1
    assert outcome.gold_assertions[0].status is GoldAssertionStatus.PASS
    assert outcome.usage.observed_provider_calls == 1
    assert outcome.usage.prompt_tokens == 100
    assert outcome.usage.completion_tokens == 50
    assert outcome.usage.observed_estimated_cost == Decimal("0.0002")
    assert outcome.error_class is None
    assert outcome.error_signal_flags == ()


def test_fresh_cache_reuse_is_measured_without_marking_run_degraded() -> None:
    outcome = collect_run_snapshot(
        replace(_snapshot(), fetches=(_cache_fetch("CACHE_FRESH"),)),
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.degraded is False
    assert outcome.error_class is None
    assert outcome.error_signal_flags == ()


def test_stale_if_error_reuse_is_provider_transient_and_degraded() -> None:
    outcome = collect_run_snapshot(
        replace(
            _snapshot(),
            fetches=(_cache_fetch("CACHE_STALE_IF_ERROR"),),
        ),
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.degraded is True
    assert outcome.error_class is ErrorClass.PROVIDER_TRANSIENT
    assert outcome.error_code == "provider_transient_error"
    assert outcome.failed_phase == "FETCH"
    assert "provider_transient" in outcome.error_signal_flags
    assert "fetch_cache_stale_if_error" in outcome.error_signal_flags


def test_fetch_projection_cannot_carry_content_or_network_identifiers() -> None:
    projected_fields = {item.name for item in fields(SourceFetch)}

    assert projected_fields.isdisjoint(
        {
            "url",
            "url_hash",
            "storage_uri",
            "content_hash",
            "response_headers",
            "body",
        }
    )


def test_source_digest_is_independent_of_gold_row_read_order() -> None:
    case = replace(
        _case(),
        gold_assertions=(
            GoldAssertion("z-last", "normalized_contains_all", ("stable",), False),
            GoldAssertion("a-first", "normalized_contains_all", ("stable",), False),
        ),
    )
    snapshot = _snapshot()
    outcome = collect_run_snapshot(
        snapshot,
        case,
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    reordered = replace(outcome, gold_assertions=tuple(reversed(outcome.gold_assertions)))
    assert source_snapshot_digest(snapshot, reordered) == outcome.source_snapshot_digest


@pytest.mark.parametrize(
    ("snapshot", "code"),
    (
        (replace(_snapshot(), status="RUNNING", completed_at=None), "source_not_terminal"),
        (
            replace(
                _snapshot(),
                invocations=(replace(_snapshot().invocations[0], status="STARTED", completed_at=None),),
            ),
            "source_not_sealed",
        ),
        (
            replace(
                _snapshot(),
                outbox=(replace(_snapshot().outbox[0], published_at=None),),
            ),
            "source_outbox_unpublished",
        ),
    ),
)
def test_collector_rejects_unsealed_or_unpublished_sources(
    snapshot: RunSourceSnapshot,
    code: str,
) -> None:
    with pytest.raises(InvariantViolation) as captured:
        collect_run_snapshot(snapshot, _case(), _rate(), oracle_version="shadow-cases-v1")
    assert captured.value.code == code


def test_invalid_citation_chain_is_a_fail_closed_candidate_defect() -> None:
    snapshot = replace(
        _snapshot(),
        citations=(replace(_snapshot().citations[0], quote_matches_evidence=False),),
    )
    outcome = collect_run_snapshot(
        snapshot,
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.traceability_violation_count == 1
    assert outcome.valid_citation_chain_count == 0
    assert outcome.error_class is ErrorClass.CANDIDATE_DEFECT
    assert outcome.error_code == "citation_traceability_violation"
    assert "citation_traceability_violation" in outcome.error_signal_flags


def test_content_is_excluded_from_digest_when_derived_metrics_do_not_change() -> None:
    case = replace(
        _case(deterministic=False),
        required_source_classes=(),
        forbidden_query_terms=(),
    )
    original = _snapshot()
    changed = replace(
        original,
        answer_text="secret token sk-sensitive",
        queries=(replace(original.queries[0], query_text="private raw query"),),
        claims=(replace(original.claims[0], claim_text="private raw answer"),),
    )

    first = collect_run_snapshot(original, case, _rate(), oracle_version=None)
    second = collect_run_snapshot(changed, case, _rate(), oracle_version=None)
    assert first.source_snapshot_digest == second.source_snapshot_digest


def test_possibly_billed_invocation_uses_full_frozen_reserve() -> None:
    invocation = replace(
        _snapshot().invocations[0],
        status="FAILED",
        billing_disposition="POSSIBLY_BILLED",
        prompt_tokens=0,
        completion_tokens=0,
        error_category="TRANSIENT",
    )
    outcome = collect_run_snapshot(
        replace(
            _snapshot(),
            invocations=(invocation,),
            recorded_prompt_tokens=0,
            recorded_completion_tokens=0,
        ),
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.usage.observed_provider_calls == 0
    assert outcome.usage.possibly_billed_call_charge == 1
    assert outcome.usage.possibly_billed_cost_charge == Decimal("0.006")
    assert outcome.error_class is ErrorClass.PROVIDER_TRANSIENT
    assert outcome.degraded is True


def test_failed_run_without_terminal_timestamp_is_collected_with_latency_penalty() -> None:
    snapshot = replace(
        _snapshot(),
        status="FAILED",
        answer_quality="NONE",
        stop_reason="INFRASTRUCTURE_FAILURE",
        completed_at=None,
        response_status="FAILED",
        output_message_id=None,
        output_message_role=None,
        output_message_conversation_id=None,
        answer_text=None,
    )
    outcome = collect_run_snapshot(
        snapshot,
        replace(_case(), required_source_classes=()),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.latency_ms == 15_001
    assert outcome.source_terminal_at == snapshot.hard_deadline_at
    assert outcome.error_class is ErrorClass.INFRASTRUCTURE
    assert "source_terminal_timestamp_invalid" in outcome.error_signal_flags


def test_search_run_and_invocation_usage_mismatch_is_a_candidate_defect() -> None:
    outcome = collect_run_snapshot(
        replace(_snapshot(), recorded_llm_call_count=0),
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.error_class is ErrorClass.CANDIDATE_DEFECT
    assert outcome.error_code == "model_invocation_ledger_mismatch"


def test_expired_oracle_is_audited_as_not_applicable() -> None:
    case = replace(
        _case(),
        valid_from=NOW - timedelta(days=2),
        valid_until=NOW - timedelta(days=1),
    )
    outcome = collect_run_snapshot(
        _snapshot(),
        case,
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.gold_assertions[0].status is GoldAssertionStatus.NOT_APPLICABLE
    assert outcome.gold_assertions[0].reason_code == "oracle_window_invalid"


def test_unknown_collector_schema_version_is_rejected() -> None:
    with pytest.raises(InvariantViolation) as captured:
        collect_run_snapshot(
            _snapshot(),
            _case(),
            _rate(),
            oracle_version="shadow-cases-v1",
            collector_schema_version="shadow-collector-v999",
        )

    assert captured.value.code == "collector_schema_version_mismatch"


def test_unanswerable_source_audit_accepts_rejected_examined_authority() -> None:
    rejected = replace(
        _snapshot().evidence[0],
        verdict="REJECTED",
        confidence=0.0,
    )
    snapshot = replace(
        _snapshot(),
        answer_quality="PARTIAL",
        stop_reason="INSUFFICIENT_EVIDENCE",
        facts=(replace(_snapshot().facts[0], status="OPEN"),),
        evidence=(rejected,),
        claims=(),
        citations=(),
    )
    case = replace(
        _case(deterministic=False),
        answerability=Answerability.INTENTIONALLY_UNANSWERABLE,
        oracle_type=OracleType.NOT_APPLICABLE,
        must_not_complete=True,
    )

    outcome = collect_run_snapshot(snapshot, case, _rate(), oracle_version=None)

    assert "required_source_class_missing" not in outcome.error_signal_flags
    assert outcome.answer_quality == "PARTIAL"


def test_unanswerable_rejected_source_requires_valid_fetch_lineage() -> None:
    rejected = replace(
        _snapshot().evidence[0],
        verdict="REJECTED",
        confidence=0.0,
        document_chain_valid=False,
    )
    snapshot = replace(
        _snapshot(),
        answer_quality="PARTIAL",
        stop_reason="INSUFFICIENT_EVIDENCE",
        facts=(replace(_snapshot().facts[0], status="OPEN"),),
        evidence=(rejected,),
        claims=(),
        citations=(),
    )
    case = replace(
        _case(deterministic=False),
        answerability=Answerability.INTENTIONALLY_UNANSWERABLE,
        oracle_type=OracleType.NOT_APPLICABLE,
        must_not_complete=True,
    )

    outcome = collect_run_snapshot(snapshot, case, _rate(), oracle_version=None)

    assert "required_source_class_missing" in outcome.error_signal_flags


def test_fatal_candidate_signal_overrides_other_error_classes() -> None:
    invocation = replace(
        _snapshot().invocations[0],
        status="FAILED",
        billing_disposition="POSSIBLY_BILLED",
        prompt_tokens=0,
        completion_tokens=0,
        error_category="PERMANENT",
    )
    polluted = replace(
        _snapshot(),
        invocations=(invocation,),
        recorded_prompt_tokens=0,
        recorded_completion_tokens=0,
        queries=(replace(_snapshot().queries[0], query_text="PRIVATE MEMORY leak"),),
    )
    outcome = collect_run_snapshot(
        polluted,
        _case(),
        _rate(),
        oracle_version="shadow-cases-v1",
    )

    assert outcome.error_class is ErrorClass.CANDIDATE_DEFECT
    assert outcome.error_code == "query_pollution"
    assert "permanent_configuration" in outcome.error_signal_flags
