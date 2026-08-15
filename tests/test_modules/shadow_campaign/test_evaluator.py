from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from sana.modules.shadow_campaign.domain import GateStatus
from sana.modules.shadow_campaign.evaluator import (
    CampaignMetrics,
    ReleaseGateEvaluator,
    evaluate_gold_assertion,
    nearest_rank_percentile,
    select_review_units,
    wilson_interval_bps,
)
from sana.modules.shadow_campaign.manifest import GoldAssertion
from sana.modules.shadow_campaign.policy import (
    SHADOW_FULL_GATE_V2,
    SHADOW_SMOKE_GATE_V1,
)

from .test_manifest import NOW, _encode, _valid_rows
from sana.modules.shadow_campaign.manifest import parse_manifest_bytes


def _passing_metrics() -> CampaignMetrics:
    return CampaignMetrics(
        terminal_results=120,
        actual_fast_results=60,
        actual_research_results=60,
        distinct_case_count=40,
        unanswerable_case_count=8,
        unanswerable_terminal_results=24,
        completed_reviews=20,
        unreviewable_reviews=0,
        valid_gold_case_count=16,
        hard_violation_counts={},
        fast_latency_ms=(10_000,) * 60,
        research_latency_ms=(100_000,) * 60,
        mode_match_count=117,
        mode_total_count=120,
        coverage_macro_bps=8_500,
        coverage_stratum_bps=(7_500, 7_500, 7_500, 7_500),
        gold_pass_count=98,
        gold_total_count=100,
        review_correct_count=18,
        review_citation_relevance_pass_count=19,
        review_source_appropriateness_pass_count=19,
        review_freshness_pass_count=19,
        review_completeness_pass_count=18,
        review_total_count=20,
        unanswerable_gap_count=24,
        unanswerable_gap_total=24,
        degraded_count=12,
        infrastructure_failure_count=1,
        projected_full_cost_usd=Decimal("0.08"),
    )


def test_nearest_rank_and_wilson_are_deterministic() -> None:
    assert nearest_rank_percentile((1, 2, 100), 95) == 100
    assert nearest_rank_percentile((1, 2, 3, 4), 50) == 2
    assert wilson_interval_bps(0, 10).lower_bps == 0
    assert wilson_interval_bps(10, 10).upper_bps == 10_000


def test_gold_assertion_operators_are_allowlisted_and_deterministic() -> None:
    assert evaluate_gold_assertion(
        GoldAssertion("a", "normalized_contains_all", ("HELLO", "world"), False),
        "  hello,   WORLD ",
    )
    assert evaluate_gold_assertion(
        GoldAssertion("a", "normalized_equals", "Ａpex", False),
        "apex",
    )
    assert evaluate_gold_assertion(
        GoldAssertion(
            "a",
            "number_in_range",
            {"min": Decimal("1.5"), "max": Decimal("2.5")},
            False,
        ),
        "2.0",
    )
    assert evaluate_gold_assertion(
        GoldAssertion("a", "set_contains", ("alpha", "beta"), False),
        ["BETA", "alpha", "gamma"],
    )
    assert evaluate_gold_assertion(
        GoldAssertion("a", "source_class_at_least", "INDEPENDENT", False),
        "OFFICIAL",
    )


def test_review_selection_is_stable_and_stratified() -> None:
    manifest = parse_manifest_bytes(_encode(_valid_rows()), now=NOW)
    campaign_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    first = select_review_units(campaign_id, manifest, repetitions=3)
    second = select_review_units(campaign_id, manifest, repetitions=3)

    assert first == second
    assert len(first) == 20
    assert len({unit.case_id for unit in first}) == 20
    strata = {(unit.expected_mode.value, unit.locale) for unit in first}
    assert strata == {
        ("FAST", "zh-CN"),
        ("FAST", "en"),
        ("RESEARCH", "zh-CN"),
        ("RESEARCH", "en"),
    }
    for stratum in strata:
        assert sum(
            (unit.expected_mode.value, unit.locale) == stratum for unit in first
        ) == 5


def test_gate_priority_is_fatal_then_sample_then_quality() -> None:
    evaluator = ReleaseGateEvaluator()
    insufficient = replace(
        _passing_metrics(),
        terminal_results=10,
        actual_fast_results=5,
        actual_research_results=5,
        distinct_case_count=10,
        unanswerable_case_count=1,
        unanswerable_terminal_results=2,
        completed_reviews=0,
        valid_gold_case_count=2,
        fast_latency_ms=(10_000,) * 5,
        research_latency_ms=(100_000,) * 5,
        mode_match_count=10,
        mode_total_count=10,
        gold_pass_count=2,
        gold_total_count=2,
        review_correct_count=0,
        review_citation_relevance_pass_count=0,
        review_source_appropriateness_pass_count=0,
        review_freshness_pass_count=0,
        review_completeness_pass_count=0,
        review_total_count=0,
        unanswerable_gap_count=2,
        unanswerable_gap_total=2,
        degraded_count=1,
        infrastructure_failure_count=0,
    )
    fatal = replace(insufficient, hard_violation_counts={"citation_chain": 1})

    assert evaluator.evaluate(fatal, SHADOW_FULL_GATE_V2, final=True).status is GateStatus.FAIL
    assert (
        evaluator.evaluate(insufficient, SHADOW_FULL_GATE_V2, final=True).status
        is GateStatus.INSUFFICIENT_SAMPLE
    )
    assert (
        evaluator.evaluate(insufficient, SHADOW_FULL_GATE_V2, final=False).status
        is GateStatus.PENDING
    )


def test_full_gate_passes_only_when_every_quality_rule_passes() -> None:
    evaluator = ReleaseGateEvaluator()
    passed = evaluator.evaluate(_passing_metrics(), SHADOW_FULL_GATE_V2, final=True)
    failed = evaluator.evaluate(
        replace(_passing_metrics(), coverage_macro_bps=7_999),
        SHADOW_FULL_GATE_V2,
        final=True,
    )

    assert passed.status is GateStatus.PASS
    assert failed.status is GateStatus.FAIL
    assert any(rule.rule_id == "coverage_macro" and not rule.passed for rule in failed.rules)


def test_gate_fails_closed_for_internally_inconsistent_metrics() -> None:
    decision = ReleaseGateEvaluator().evaluate(
        replace(_passing_metrics(), fast_latency_ms=()),
        SHADOW_FULL_GATE_V2,
        final=True,
    )

    assert decision.status is GateStatus.FAIL
    assert any(rule.rule_id == "metric_integrity" and not rule.passed for rule in decision.rules)


def test_smoke_gate_requires_exact_six_run_shape() -> None:
    metrics = CampaignMetrics(
        terminal_results=6,
        actual_fast_results=3,
        actual_research_results=3,
        distinct_case_count=6,
        unanswerable_case_count=1,
        unanswerable_terminal_results=1,
        completed_reviews=0,
        unreviewable_reviews=0,
        valid_gold_case_count=0,
        fast_latency_ms=(10_000,) * 3,
        research_latency_ms=(100_000,) * 3,
        projected_full_cost_usd=Decimal("0.08"),
    )
    evaluator = ReleaseGateEvaluator()

    assert evaluator.evaluate(metrics, SHADOW_SMOKE_GATE_V1, final=True).status is GateStatus.PASS
    oversized = replace(
        metrics,
        terminal_results=7,
        actual_research_results=4,
        distinct_case_count=7,
        research_latency_ms=(100_000,) * 4,
    )
    assert (
        evaluator.evaluate(oversized, SHADOW_SMOKE_GATE_V1, final=True).status
        is GateStatus.INSUFFICIENT_SAMPLE
    )
