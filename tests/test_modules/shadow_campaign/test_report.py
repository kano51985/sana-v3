from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from uuid import UUID

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    GateStatus,
    canonical_snapshot,
    snapshot_hash,
)
from sana.modules.shadow_campaign.policy import (
    DOCKER_SMOKE_V1,
    SHADOW_SMOKE_GATE_V1,
    CostRate,
    ReviewRubric,
)
from sana.modules.shadow_campaign.report import (
    CampaignReportBuilder,
    CampaignReportSnapshot,
    DECISION_INPUT_SCHEMA_VERSION,
)


TENANT = UUID("10000000-0000-0000-0000-000000000001")
OWNER = UUID("20000000-0000-0000-0000-000000000001")
CAMPAIGN = UUID("30000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
RUBRIC = ReviewRubric("review-rubric-v1")
RATE = CostRate("cost-v1", Decimal("1"), Decimal("2"), Decimal("0.006"))


def _result(index: int) -> dict[str, object]:
    mode = "FAST" if index <= 3 else "RESEARCH"
    unanswerable = index == 6
    digest = f"{index}" * 64
    return {
        "result_id": UUID(f"40000000-0000-0000-0000-{index:012d}"),
        "search_run_id": UUID(f"50000000-0000-0000-0000-{index:012d}"),
        "case_id": f"case-{index}",
        "repetition": 1,
        "schedule_ordinal": index,
        "manual_review_selected": False,
        "locale": "zh-CN" if index % 2 else "en",
        "category": "no_answer" if unanswerable else "version",
        "answerability": (
            "intentionally_unanswerable" if unanswerable else "answerable"
        ),
        "expected_mode": mode,
        "scheduling_state": "COLLECTED",
        "submission_request_hash": "9" * 64,
        "actual_mode": mode,
        "run_status": "SUCCEEDED",
        "answer_quality": "NONE" if unanswerable else "COMPLETE",
        "run_stop_reason": "INSUFFICIENT_EVIDENCE" if unanswerable else "FACTS_COVERED",
        "latency_ms": 10_000 if mode == "FAST" else 100_000,
        "minimum_required_facts": 1,
        "fact_total": 1,
        "fact_covered": 0 if unanswerable else 1,
        "fact_gap": 1 if unanswerable else 0,
        "plan_completeness_failure": False,
        "factual_claim_count": 0 if unanswerable else 1,
        "nonfactual_claim_count": 1 if unanswerable else 0,
        "cited_factual_claim_count": 0 if unanswerable else 1,
        "valid_citation_chain_count": 0 if unanswerable else 1,
        "traceability_violation_count": 0,
        "gold_assertion_total": 0,
        "gold_assertion_passed": 0,
        "gold_assertion_failed": 0,
        "gold_assertion_not_applicable": 0,
        "oracle_version": None,
        "query_pollution_count": 0,
        "model_call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "estimated_cost": "0",
        "degraded": False,
        "provider_success_count": 0,
        "provider_failure_count": 0,
        "error_class": "CONTENT_GAP" if unanswerable else None,
        "error_code": "content_gap" if unanswerable else None,
        "failed_phase": None,
        "error_signal_flags": ("content_gap",) if unanswerable else (),
        "stable_skip_reason": None,
        "reserved_provider_calls": 0,
        "reserved_estimated_cost": "0",
        "reservation_state": "SETTLED",
        "settled_observed_provider_calls": 0,
        "settled_observed_cost": "0",
        "possibly_billed_call_charge": 0,
        "possibly_billed_cost_charge": "0",
        "budget_violation": False,
        "source_terminal_at": NOW,
        "source_snapshot_digest": digest,
        "current_source_digest": digest,
        "collector_schema_version": "shadow-collector-v1",
    }


def report_snapshot(*, result_count: int = 6) -> CampaignReportSnapshot:
    results = [_result(index) for index in range(1, result_count + 1)]
    profile = DOCKER_SMOKE_V1.snapshot()
    policy = SHADOW_SMOKE_GATE_V1.snapshot()
    campaign = {
        "stop_intent": "NONE",
        "stop_reason": None,
        "profile_version": DOCKER_SMOKE_V1.version,
        "profile_hash": DOCKER_SMOKE_V1.sha256,
        "profile_snapshot": profile,
        "gate_policy_version": SHADOW_SMOKE_GATE_V1.version,
        "gate_policy_hash": SHADOW_SMOKE_GATE_V1.sha256,
        "gate_policy_snapshot": policy,
        "manifest_version": "cases-v1",
        "manifest_hash": "a" * 64,
        "manifest_case_count": 6,
        "repetitions": 1,
        "review_rubric_version": RUBRIC.version,
        "review_rubric_hash": RUBRIC.sha256,
        "review_rubric_snapshot": RUBRIC.snapshot(),
        "cost_rate_version": RATE.version,
        "cost_rate_hash": RATE.sha256,
        "cost_rate_snapshot": RATE.snapshot(),
        "candidate_commit_sha": "b" * 40,
        "candidate_source_clean": True,
        "candidate_image_id": f"candidate@sha256:{'c' * 64}",
        "candidate_oci_revision": "b" * 40,
        "alembic_head": "0010_shadow_collector_audit",
        "candidate_config_hash": "d" * 64,
        "harness_commit_sha": "e" * 40,
        "harness_source_clean": True,
        "harness_fileset_hash": "f" * 64,
        "collector_schema_version": "shadow-collector-v1",
        "environment_identity_hash": "1" * 64,
        "environment_snapshot": {
            "compose_project": "shadow-test",
            "network": "isolated",
            "ignored_private_field": "fixture prompt must not be rendered",
        },
        "max_runs": 6,
        "max_concurrency": 2,
        "estimated_cost_stop_threshold": "0.01",
        "provider_call_admission_ceiling": 32,
        "provider_call_structural_ceiling": 48,
        "review_deadline_at": None,
        "active_wall_clock_ms": 1_000,
        "counts": {
            "planned_count": result_count,
            "submitted_count": result_count,
            "collected_count": result_count,
            "failed_count": 0,
            "skipped_count": 0,
            "degraded_count": 0,
        },
        "ledger": {
            "observed_provider_calls": 0,
            "possibly_billed_call_charge": 0,
            "reserved_provider_calls": 0,
            "observed_prompt_tokens": 0,
            "observed_completion_tokens": 0,
            "observed_estimated_cost": "0",
            "possibly_billed_cost_charge": "0",
            "reserved_estimated_cost": "0",
            "possibly_billed_count": 0,
        },
    }
    payload = {
        "schema": DECISION_INPUT_SCHEMA_VERSION,
        "campaign_id": CAMPAIGN,
        "campaign": campaign,
        "results": results,
        "reviews": [],
        "gold_assertions": [],
        "model_invocations": [],
    }
    return CampaignReportSnapshot(
        TENANT,
        CAMPAIGN,
        OWNER,
        CampaignStatus.RUNNING,
        7,
        NOW,
        None,
        payload,
    )


def test_report_is_canonical_private_and_finalizes_passing_smoke() -> None:
    builder = CampaignReportBuilder()

    first = builder.prepare(report_snapshot())
    second = builder.prepare(report_snapshot())

    assert first.decision.status is GateStatus.PASS
    assert first.finalizable is True
    assert first.json_bytes == second.json_bytes
    assert first.decision_hash == second.decision_hash
    assert first.decision_hash == snapshot_hash(json.loads(first.json_bytes))
    combined = first.json_bytes + first.markdown_bytes
    assert b"fixture prompt" not in combined
    assert b"ignored_private_field" not in combined
    assert b"reviewer_user_id" not in combined


def test_unsealed_campaign_is_pending_and_never_claims_finality() -> None:
    prepared = CampaignReportBuilder().prepare(report_snapshot(result_count=5))

    assert prepared.decision.status is GateStatus.PENDING
    assert prepared.finalizable is False
    assert prepared.finalization_reason is None


def test_paused_campaign_never_turns_a_snapshot_into_a_final_gate() -> None:
    paused = replace(
        report_snapshot(),
        campaign_status=CampaignStatus.PAUSED,
    )

    prepared = CampaignReportBuilder().prepare(paused)

    assert prepared.decision.status is GateStatus.PENDING
    assert prepared.decision.decision_state == "PENDING_EXECUTION"
    assert prepared.finalizable is False


def test_ledger_or_source_drift_is_a_fatal_gate_failure() -> None:
    original = report_snapshot()
    payload = canonical_snapshot(original.decision_input)
    payload["campaign"] = deepcopy(dict(payload["campaign"]))
    payload["campaign"]["counts"]["collected_count"] = 5
    payload["results"] = [deepcopy(dict(item)) for item in payload["results"]]
    payload["results"][0]["current_source_digest"] = "0" * 64
    drifted = CampaignReportSnapshot(
        original.tenant_id,
        original.campaign_id,
        original.owner_user_id,
        original.campaign_status,
        original.campaign_version,
        original.database_now,
        original.review_deadline_at,
        payload,
    )

    prepared = CampaignReportBuilder().prepare(drifted)

    assert prepared.decision.status is GateStatus.FAIL
    assert prepared.finalization_reason == "fatal_safety"
    failed = {
        rule.rule_id for rule in prepared.decision.rules if not rule.passed
    }
    assert "hard_campaign_ledger_mismatch" in failed
    assert "hard_source_snapshot_mismatch" in failed
