"""Deterministic gold, sampling, statistics, and release-gate evaluation."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sana.modules.evidence.domain import SourceAuthority
from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.domain import GateStatus, canonical_json_bytes
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    GoldAssertion,
    ShadowManifest,
)
from sana.modules.shadow_campaign.policy import GatePolicy


def nearest_rank_percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        raise ValueError("Percentile requires at least one value")
    if not 1 <= percentile <= 100:
        raise ValueError("Percentile must be between 1 and 100")
    if any(value < 0 for value in values):
        raise ValueError("Latency values cannot be negative")
    ordered = sorted(values)
    rank = math.ceil(percentile * len(ordered) / 100)
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    lower_bps: int
    upper_bps: int


def wilson_interval_bps(successes: int, total: int) -> WilsonInterval:
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("Wilson interval requires 0 <= successes <= total and total > 0")
    with localcontext() as context:
        context.prec = 40
        z = Decimal("1.959963984540054")
        n = Decimal(total)
        proportion = Decimal(successes) / n
        z_squared = z * z
        denominator = Decimal(1) + z_squared / n
        centre = (proportion + z_squared / (Decimal(2) * n)) / denominator
        margin = (
            z
            * (
                proportion * (Decimal(1) - proportion) / n
                + z_squared / (Decimal(4) * n * n)
            ).sqrt()
            / denominator
        )
        lower = max(Decimal(0), centre - margin)
        upper = min(Decimal(1), centre + margin)
        return WilsonInterval(
            lower_bps=int((lower * 10_000).to_integral_value(rounding="ROUND_FLOOR")),
            upper_bps=int((upper * 10_000).to_integral_value(rounding="ROUND_CEILING")),
        )


def _normalized_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not numeric")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Gold assertion value is not numeric") from exc
    if not parsed.is_finite():
        raise ValueError("Gold assertion value must be finite")
    return parsed


def evaluate_gold_assertion(assertion: GoldAssertion, actual: Any) -> bool:
    if assertion.operator == "normalized_contains_all":
        haystack = _normalized_text(actual)
        return all(_normalized_text(item) in haystack for item in assertion.expected)
    if assertion.operator == "normalized_equals":
        return _normalized_text(actual) == _normalized_text(assertion.expected)
    if assertion.operator == "number_in_range":
        value = _decimal(actual)
        return assertion.expected["min"] <= value <= assertion.expected["max"]
    if assertion.operator == "set_contains":
        if not isinstance(actual, Iterable) or isinstance(actual, (str, bytes, bytearray)):
            return False
        actual_values = {_normalized_text(item) for item in actual}
        return all(_normalized_text(item) in actual_values for item in assertion.expected)
    if assertion.operator == "source_class_at_least":
        ranks = {
            SourceAuthority.UNKNOWN: 0,
            SourceAuthority.INDEPENDENT: 1,
            SourceAuthority.OFFICIAL: 2,
        }
        try:
            actual_authority = SourceAuthority(str(actual))
            expected_authority = SourceAuthority(str(assertion.expected))
        except ValueError:
            return False
        return ranks[actual_authority] >= ranks[expected_authority]
    raise ValueError(f"Unsupported gold assertion operator: {assertion.operator}")


@dataclass(frozen=True, slots=True)
class ReviewUnit:
    case_id: str
    repetition: int
    expected_mode: SearchMode
    locale: str
    sort_digest: str


def select_review_units(
    campaign_id: UUID,
    manifest: ShadowManifest,
    *,
    repetitions: int,
    per_stratum: int = 5,
) -> tuple[ReviewUnit, ...]:
    if repetitions < 1 or per_stratum < 1:
        raise ValueError("Review selection limits must be positive")
    selected: list[ReviewUnit] = []
    for mode in (SearchMode.FAST, SearchMode.RESEARCH):
        for locale in ("zh-CN", "en"):
            candidates: list[ReviewUnit] = []
            for case in manifest.cases:
                if (
                    case.expected_mode is not mode
                    or case.locale != locale
                    or case.answerability is not Answerability.ANSWERABLE
                ):
                    continue
                for repetition in range(1, repetitions + 1):
                    encoded = canonical_json_bytes(
                        [str(campaign_id), case.id, repetition]
                    )
                    candidates.append(
                        ReviewUnit(
                            case_id=case.id,
                            repetition=repetition,
                            expected_mode=mode,
                            locale=locale,
                            sort_digest=hashlib.sha256(encoded).hexdigest(),
                        )
                    )
            chosen_case_ids: set[str] = set()
            for candidate in sorted(
                candidates,
                key=lambda item: (item.sort_digest, item.case_id, item.repetition),
            ):
                if candidate.case_id in chosen_case_ids:
                    continue
                chosen_case_ids.add(candidate.case_id)
                selected.append(candidate)
                if len(chosen_case_ids) == per_stratum:
                    break
            if len(chosen_case_ids) != per_stratum:
                raise ValueError(
                    f"Stratum {(mode.value, locale)} cannot provide {per_stratum} review cases"
                )
    return tuple(selected)


def _basis_points(numerator: int, denominator: int) -> int:
    if denominator <= 0 or not 0 <= numerator <= denominator:
        raise ValueError("Ratio requires 0 <= numerator <= denominator and denominator > 0")
    return numerator * 10_000 // denominator


@dataclass(frozen=True, slots=True)
class CampaignMetrics:
    terminal_results: int
    actual_fast_results: int
    actual_research_results: int
    distinct_case_count: int
    unanswerable_case_count: int
    unanswerable_terminal_results: int
    completed_reviews: int
    unreviewable_reviews: int
    valid_gold_case_count: int
    hard_violation_counts: Mapping[str, int] = field(default_factory=dict)
    fast_latency_ms: tuple[int, ...] = ()
    research_latency_ms: tuple[int, ...] = ()
    mode_match_count: int = 0
    mode_total_count: int = 0
    coverage_macro_bps: int = 0
    coverage_stratum_bps: tuple[int, ...] = ()
    gold_pass_count: int = 0
    gold_total_count: int = 0
    gold_macro_bps: int | None = None
    review_correct_count: int = 0
    review_citation_relevance_pass_count: int = 0
    review_source_appropriateness_pass_count: int = 0
    review_freshness_pass_count: int = 0
    review_completeness_pass_count: int = 0
    review_total_count: int = 0
    unanswerable_gap_count: int = 0
    unanswerable_gap_total: int = 0
    degraded_count: int = 0
    infrastructure_failure_count: int = 0
    projected_full_cost_usd: Decimal | None = None
    cost_stop_triggered: bool = False
    call_ceiling_triggered: bool = False

    def __post_init__(self) -> None:
        counters = (
            self.terminal_results,
            self.actual_fast_results,
            self.actual_research_results,
            self.distinct_case_count,
            self.unanswerable_case_count,
            self.unanswerable_terminal_results,
            self.completed_reviews,
            self.unreviewable_reviews,
            self.valid_gold_case_count,
            self.mode_match_count,
            self.mode_total_count,
            self.gold_pass_count,
            self.gold_total_count,
            self.review_correct_count,
            self.review_citation_relevance_pass_count,
            self.review_source_appropriateness_pass_count,
            self.review_freshness_pass_count,
            self.review_completeness_pass_count,
            self.review_total_count,
            self.unanswerable_gap_count,
            self.unanswerable_gap_total,
            self.degraded_count,
            self.infrastructure_failure_count,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Campaign metric counters cannot be negative")
        if any(value < 0 for value in self.fast_latency_ms + self.research_latency_ms):
            raise ValueError("Campaign latencies cannot be negative")
        ratios = (self.coverage_macro_bps,) + self.coverage_stratum_bps
        if self.gold_macro_bps is not None:
            ratios += (self.gold_macro_bps,)
        if any(not 0 <= value <= 10_000 for value in ratios):
            raise ValueError("Campaign ratios must be basis points")
        violations = dict(self.hard_violation_counts)
        if any(not key.strip() or value < 0 for key, value in violations.items()):
            raise ValueError("Hard violation counts must be named and non-negative")
        if self.projected_full_cost_usd is not None and self.projected_full_cost_usd < 0:
            raise ValueError("Projected cost cannot be negative")
        object.__setattr__(self, "hard_violation_counts", MappingProxyType(violations))


@dataclass(frozen=True, slots=True)
class GateRuleResult:
    rule_id: str
    observed: int | str
    threshold: int | str
    sample_size: int
    passed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    status: GateStatus
    decision_state: str
    rules: tuple[GateRuleResult, ...]


class ReleaseGateEvaluator:
    @staticmethod
    def _rule(
        rule_id: str,
        observed: int | Decimal | str,
        threshold: int | Decimal | str,
        sample_size: int,
        passed: bool,
    ) -> GateRuleResult:
        return GateRuleResult(
            rule_id=rule_id,
            observed=str(observed) if isinstance(observed, Decimal) else observed,
            threshold=str(threshold) if isinstance(threshold, Decimal) else threshold,
            sample_size=sample_size,
            passed=passed,
            reason_code=f"{rule_id}_{'passed' if passed else 'failed'}",
        )

    def evaluate(
        self,
        metrics: CampaignMetrics,
        policy: GatePolicy,
        *,
        final: bool,
    ) -> GateDecision:
        rules: list[GateRuleResult] = []
        basic_integrity_errors = sum(
            (
                metrics.actual_fast_results + metrics.actual_research_results
                > metrics.terminal_results,
                metrics.distinct_case_count > metrics.terminal_results,
                metrics.unanswerable_case_count > metrics.distinct_case_count,
                metrics.unanswerable_terminal_results > metrics.terminal_results,
                metrics.valid_gold_case_count > metrics.distinct_case_count,
                metrics.completed_reviews > metrics.terminal_results,
                metrics.unreviewable_reviews > metrics.completed_reviews,
                metrics.mode_match_count > metrics.mode_total_count,
                metrics.gold_pass_count > metrics.gold_total_count,
                metrics.review_total_count > metrics.completed_reviews,
                metrics.review_correct_count > metrics.review_total_count,
                metrics.review_citation_relevance_pass_count > metrics.review_total_count,
                metrics.review_source_appropriateness_pass_count > metrics.review_total_count,
                metrics.review_freshness_pass_count > metrics.review_total_count,
                metrics.review_completeness_pass_count > metrics.review_total_count,
                metrics.unanswerable_gap_count > metrics.unanswerable_gap_total,
                metrics.degraded_count > metrics.terminal_results,
                metrics.infrastructure_failure_count > metrics.terminal_results,
            )
        )
        hard_total = sum(metrics.hard_violation_counts.values()) + basic_integrity_errors
        rules.append(
            self._rule(
                "hard_safety",
                hard_total,
                0,
                metrics.terminal_results,
                hard_total == 0,
            )
        )
        if basic_integrity_errors:
            rules.append(
                self._rule(
                    "metric_integrity",
                    basic_integrity_errors,
                    0,
                    metrics.terminal_results,
                    False,
                )
            )
        for name in sorted(metrics.hard_violation_counts):
            count = metrics.hard_violation_counts[name]
            rules.append(
                self._rule(
                    f"hard_{name}",
                    count,
                    0,
                    metrics.terminal_results,
                    count == 0,
                )
            )
        if hard_total:
            return GateDecision(GateStatus.FAIL, "FINAL_FAIL", tuple(rules))

        terminal_sample_passed = (
            metrics.terminal_results == policy.min_terminal_results
            if policy.require_exact_sample_counts
            else metrics.terminal_results >= policy.min_terminal_results
        )
        fast_sample_passed = (
            metrics.actual_fast_results == policy.min_actual_fast_results
            if policy.require_exact_sample_counts
            else metrics.actual_fast_results >= policy.min_actual_fast_results
        )
        research_sample_passed = (
            metrics.actual_research_results == policy.min_actual_research_results
            if policy.require_exact_sample_counts
            else metrics.actual_research_results >= policy.min_actual_research_results
        )
        distinct_sample_passed = (
            metrics.distinct_case_count == policy.min_distinct_cases
            if policy.require_exact_sample_counts
            else metrics.distinct_case_count >= policy.min_distinct_cases
        )
        sample_rules = (
            self._rule(
                "terminal_sample",
                metrics.terminal_results,
                policy.min_terminal_results,
                metrics.terminal_results,
                terminal_sample_passed,
            ),
            self._rule(
                "actual_fast_sample",
                metrics.actual_fast_results,
                policy.min_actual_fast_results,
                metrics.actual_fast_results,
                fast_sample_passed,
            ),
            self._rule(
                "actual_research_sample",
                metrics.actual_research_results,
                policy.min_actual_research_results,
                metrics.actual_research_results,
                research_sample_passed,
            ),
            self._rule(
                "distinct_case_sample",
                metrics.distinct_case_count,
                policy.min_distinct_cases,
                metrics.distinct_case_count,
                distinct_sample_passed,
            ),
            self._rule(
                "unanswerable_case_sample",
                metrics.unanswerable_case_count,
                policy.min_unanswerable_cases,
                metrics.unanswerable_case_count,
                metrics.unanswerable_case_count >= policy.min_unanswerable_cases,
            ),
            self._rule(
                "unanswerable_run_sample",
                metrics.unanswerable_terminal_results,
                policy.min_unanswerable_results,
                metrics.unanswerable_terminal_results,
                metrics.unanswerable_terminal_results >= policy.min_unanswerable_results,
            ),
            self._rule(
                "review_sample",
                metrics.completed_reviews,
                policy.required_reviews,
                metrics.completed_reviews,
                metrics.completed_reviews >= policy.required_reviews
                and metrics.unreviewable_reviews == 0,
            ),
            self._rule(
                "gold_case_sample",
                metrics.valid_gold_case_count,
                policy.required_gold_cases,
                metrics.valid_gold_case_count,
                metrics.valid_gold_case_count >= policy.required_gold_cases,
            ),
        )
        rules.extend(sample_rules)
        if not all(rule.passed for rule in sample_rules):
            status = GateStatus.INSUFFICIENT_SAMPLE if final else GateStatus.PENDING
            state = "FINAL_INSUFFICIENT_SAMPLE" if final else "PENDING_SAMPLE"
            return GateDecision(status, state, tuple(rules))

        quality_integrity_errors = sum(
            (
                len(metrics.fast_latency_ms) < policy.min_actual_fast_results,
                len(metrics.research_latency_ms) < policy.min_actual_research_results,
                not policy.operational_only and len(metrics.coverage_stratum_bps) != 4,
                not policy.operational_only and metrics.mode_total_count <= 0,
                not policy.operational_only and metrics.gold_total_count <= 0,
                not policy.operational_only and metrics.review_total_count <= 0,
                not policy.operational_only and metrics.unanswerable_gap_total <= 0,
            )
        )
        rules.append(
            self._rule(
                "metric_integrity",
                quality_integrity_errors,
                0,
                metrics.terminal_results,
                quality_integrity_errors == 0,
            )
        )
        if quality_integrity_errors:
            return GateDecision(GateStatus.FAIL, "FINAL_FAIL", tuple(rules))

        fast_p95 = nearest_rank_percentile(metrics.fast_latency_ms, 95)
        research_p95 = nearest_rank_percentile(metrics.research_latency_ms, 95)
        quality_rules: list[GateRuleResult] = [
            self._rule(
                "fast_p95_ms",
                fast_p95,
                policy.fast_latency_p95_ms,
                len(metrics.fast_latency_ms),
                fast_p95 <= policy.fast_latency_p95_ms,
            ),
            self._rule(
                "research_p95_ms",
                research_p95,
                policy.research_latency_p95_ms,
                len(metrics.research_latency_ms),
                research_p95 <= policy.research_latency_p95_ms,
            ),
            self._rule(
                "cost_stop_not_triggered",
                int(metrics.cost_stop_triggered),
                0,
                metrics.terminal_results,
                not metrics.cost_stop_triggered,
            ),
            self._rule(
                "call_ceiling_not_triggered",
                int(metrics.call_ceiling_triggered),
                0,
                metrics.terminal_results,
                not metrics.call_ceiling_triggered,
            ),
        ]
        if policy.require_every_run_within_deadline:
            deadline_breaches = sum(
                value > policy.fast_latency_p95_ms for value in metrics.fast_latency_ms
            ) + sum(
                value > policy.research_latency_p95_ms
                for value in metrics.research_latency_ms
            )
            quality_rules.append(
                self._rule(
                    "all_run_deadlines",
                    deadline_breaches,
                    0,
                    len(metrics.fast_latency_ms) + len(metrics.research_latency_ms),
                    deadline_breaches == 0,
                )
            )
        if policy.max_projected_full_cost_usd is not None:
            cost = metrics.projected_full_cost_usd
            quality_rules.append(
                self._rule(
                    "projected_full_cost",
                    cost if cost is not None else "MISSING",
                    policy.max_projected_full_cost_usd,
                    metrics.terminal_results,
                    cost is not None and cost <= policy.max_projected_full_cost_usd,
                )
            )

        if not policy.operational_only:
            mode_bps = _basis_points(metrics.mode_match_count, metrics.mode_total_count)
            gold_bps = (
                metrics.gold_macro_bps
                if metrics.gold_macro_bps is not None
                else _basis_points(metrics.gold_pass_count, metrics.gold_total_count)
            )
            correct_bps = _basis_points(
                metrics.review_correct_count, metrics.review_total_count
            )
            citation_bps = _basis_points(
                metrics.review_citation_relevance_pass_count,
                metrics.review_total_count,
            )
            source_bps = _basis_points(
                metrics.review_source_appropriateness_pass_count,
                metrics.review_total_count,
            )
            freshness_bps = _basis_points(
                metrics.review_freshness_pass_count, metrics.review_total_count
            )
            completeness_bps = _basis_points(
                metrics.review_completeness_pass_count, metrics.review_total_count
            )
            gap_bps = _basis_points(
                metrics.unanswerable_gap_count, metrics.unanswerable_gap_total
            )
            degraded_bps = _basis_points(metrics.degraded_count, metrics.terminal_results)
            infrastructure_bps = _basis_points(
                metrics.infrastructure_failure_count, metrics.terminal_results
            )
            minimum_stratum = min(metrics.coverage_stratum_bps)
            quality_rules.extend(
                (
                    self._rule(
                        "mode_accuracy",
                        mode_bps,
                        policy.min_mode_accuracy_bps,
                        metrics.mode_total_count,
                        mode_bps >= policy.min_mode_accuracy_bps,
                    ),
                    self._rule(
                        "coverage_macro",
                        metrics.coverage_macro_bps,
                        policy.min_coverage_macro_bps,
                        metrics.distinct_case_count,
                        metrics.coverage_macro_bps >= policy.min_coverage_macro_bps,
                    ),
                    self._rule(
                        "coverage_stratum_min",
                        minimum_stratum,
                        policy.min_coverage_stratum_bps,
                        len(metrics.coverage_stratum_bps),
                        minimum_stratum >= policy.min_coverage_stratum_bps,
                    ),
                    self._rule(
                        "gold_pass_rate",
                        gold_bps,
                        policy.min_gold_pass_bps,
                        metrics.gold_total_count,
                        gold_bps >= policy.min_gold_pass_bps,
                    ),
                    self._rule(
                        "review_correct",
                        correct_bps,
                        policy.min_review_correct_bps,
                        metrics.review_total_count,
                        correct_bps >= policy.min_review_correct_bps,
                    ),
                    self._rule(
                        "review_citation_relevance",
                        citation_bps,
                        policy.min_review_citation_bps,
                        metrics.review_total_count,
                        citation_bps >= policy.min_review_citation_bps,
                    ),
                    self._rule(
                        "review_source_appropriateness",
                        source_bps,
                        policy.min_review_source_bps,
                        metrics.review_total_count,
                        source_bps >= policy.min_review_source_bps,
                    ),
                    self._rule(
                        "review_freshness",
                        freshness_bps,
                        policy.min_review_freshness_bps,
                        metrics.review_total_count,
                        freshness_bps >= policy.min_review_freshness_bps,
                    ),
                    self._rule(
                        "review_completeness",
                        completeness_bps,
                        policy.min_review_completeness_bps,
                        metrics.review_total_count,
                        completeness_bps >= policy.min_review_completeness_bps,
                    ),
                    self._rule(
                        "unanswerable_explicit_gap",
                        gap_bps,
                        policy.min_unanswerable_gap_bps,
                        metrics.unanswerable_gap_total,
                        gap_bps >= policy.min_unanswerable_gap_bps,
                    ),
                    self._rule(
                        "degraded_rate",
                        degraded_bps,
                        policy.max_degraded_bps,
                        metrics.terminal_results,
                        degraded_bps <= policy.max_degraded_bps,
                    ),
                    self._rule(
                        "infrastructure_failure_rate",
                        infrastructure_bps,
                        policy.max_infrastructure_failure_bps,
                        metrics.terminal_results,
                        infrastructure_bps <= policy.max_infrastructure_failure_bps,
                    ),
                )
            )
        else:
            quality_rules.append(
                self._rule(
                    "infrastructure_failure_count",
                    metrics.infrastructure_failure_count,
                    0,
                    metrics.terminal_results,
                    metrics.infrastructure_failure_count == 0,
                )
            )
        rules.extend(quality_rules)
        passed = all(rule.passed for rule in quality_rules)
        return GateDecision(
            GateStatus.PASS if passed else GateStatus.FAIL,
            "FINAL_PASS" if passed else "FINAL_FAIL",
            tuple(rules),
        )
