"""Deterministic release-gate inputs, aggregation, and immutable reports."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from typing import Any, cast
from uuid import UUID

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    GateStatus,
    canonical_json_bytes,
    canonical_snapshot,
    freeze_json,
    require_aware,
    snapshot_hash,
)
from sana.modules.shadow_campaign.evaluator import (
    CampaignMetrics,
    GateDecision,
    ReleaseGateEvaluator,
    nearest_rank_percentile,
    wilson_interval_bps,
)
from sana.modules.shadow_campaign.policy import GateKind, GatePolicy
from sana.modules.shared.errors import InvariantViolation


REPORT_SCHEMA_VERSION = "shadow-campaign-report-v1"
DECISION_INPUT_SCHEMA_VERSION = "shadow-decision-input-v1"

_TERMINAL_RESULT_STATES = frozenset({"COLLECTED", "FAILED"})
_SEALED_RESULT_STATES = frozenset({"COLLECTED", "FAILED", "SKIPPED"})
_TERMINAL_INVOCATION_STATES = frozenset(
    {"COMPLETED", "FAILED", "ABANDONED", "REUSED"}
)
_EXPLICIT_GAP_STOP_REASONS = frozenset(
    {"FACT_GAPS_REMAIN", "INSUFFICIENT_EVIDENCE", "NO_SUPPORTED_FACTS"}
)
_SAMPLE_RULES_EXCEPT_REVIEW = frozenset(
    {
        "terminal_sample",
        "actual_fast_sample",
        "actual_research_sample",
        "distinct_case_sample",
        "unanswerable_case_sample",
        "unanswerable_run_sample",
        "gold_case_sample",
    }
)
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "api_loopback",
        "compose_project",
        "config_hash",
        "container_images",
        "initial_queue_depth",
        "network",
        "network_id",
        "resource_limits",
        "topology_hash",
        "volume_ids",
        "worker_concurrency",
    }
)
_STABLE_CODE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,199}$")
_MONEY_QUANTUM = Decimal("0.0000000001")


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be a string-keyed mapping")
    return value


def _rows(value: object, field_name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(_mapping(item, field_name) for item in value)


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise ValueError(f"{field_name} must be an integer") from error
    if parsed < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return parsed


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be numeric") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return parsed


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _bps(numerator: int, denominator: int) -> int:
    return numerator * 10_000 // denominator if denominator else 0


def _fraction_bps(value: Fraction) -> int:
    return value.numerator * 10_000 // value.denominator


def _percentiles(values: Sequence[int], threshold: int) -> dict[str, int]:
    if not values:
        return {
            "sample_size": 0,
            "p50_ms": 0,
            "p95_ms": 0,
            "max_ms": 0,
            "deadline_breach_count": 0,
        }
    return {
        "sample_size": len(values),
        "p50_ms": nearest_rank_percentile(values, 50),
        "p95_ms": nearest_rank_percentile(values, 95),
        "max_ms": max(values),
        "deadline_breach_count": sum(value > threshold for value in values),
    }


def gate_policy_from_snapshot(value: Mapping[str, Any]) -> GatePolicy:
    expected = {item.name for item in fields(GatePolicy)}
    if set(value) != expected:
        raise InvariantViolation(
            "Frozen GatePolicy snapshot has an unexpected schema",
            code="gate_policy_snapshot_invalid",
        )
    converted = dict(value)
    try:
        converted["kind"] = GateKind(str(converted["kind"]))
        if converted["max_projected_full_cost_usd"] is not None:
            converted["max_projected_full_cost_usd"] = Decimal(
                str(converted["max_projected_full_cost_usd"])
            )
        return GatePolicy(**converted)
    except (TypeError, ValueError, InvalidOperation) as error:
        raise InvariantViolation(
            "Frozen GatePolicy snapshot is invalid",
            code="gate_policy_snapshot_invalid",
        ) from error


@dataclass(frozen=True, slots=True)
class CampaignReportSnapshot:
    tenant_id: UUID
    campaign_id: UUID
    owner_user_id: UUID
    campaign_status: CampaignStatus
    campaign_version: int
    database_now: datetime
    review_deadline_at: datetime | None
    decision_input: Mapping[str, Any]
    existing_final_binding: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        require_aware(self.database_now, "database_now")
        if self.review_deadline_at is not None:
            require_aware(self.review_deadline_at, "review_deadline_at")
        if self.campaign_version < 0:
            raise ValueError("campaign_version cannot be negative")
        payload = _mapping(self.decision_input, "decision_input")
        if payload.get("schema") != DECISION_INPUT_SCHEMA_VERSION:
            raise ValueError("Decision input schema is unsupported")
        if str(payload.get("campaign_id")) != str(self.campaign_id):
            raise ValueError("Decision input Campaign identity does not match")
        object.__setattr__(self, "decision_input", freeze_json(payload))
        if self.existing_final_binding is not None:
            object.__setattr__(
                self,
                "existing_final_binding",
                freeze_json(_mapping(self.existing_final_binding, "final binding")),
            )

    @property
    def decision_input_hash(self) -> str:
        return snapshot_hash(self.decision_input)


@dataclass(frozen=True, slots=True)
class PreparedCampaignReport:
    tenant_id: UUID
    campaign_id: UUID
    campaign_status: CampaignStatus
    campaign_version: int
    decision_input_hash: str
    decision_hash: str
    decision: GateDecision
    automatic_gate_status: str
    manual_review_status: str
    finalizable: bool
    finalization_reason: str | None
    json_bytes: bytes
    markdown_bytes: bytes


@dataclass(frozen=True, slots=True)
class FinalReportBinding:
    tenant_id: UUID
    campaign_id: UUID
    owner_user_id: UUID
    expected_campaign_status: CampaignStatus
    expected_campaign_version: int
    decision_input_hash: str
    decision_hash: str
    gate_status: GateStatus
    automatic_gate_status: str
    manual_review_status: str
    finalization_reason: str
    json_uri: str
    json_sha256: str
    markdown_uri: str
    markdown_sha256: str


@dataclass(frozen=True, slots=True)
class FinalReportReceipt:
    campaign_id: UUID
    gate_status: GateStatus
    decision_hash: str
    json_uri: str
    json_sha256: str
    markdown_uri: str
    markdown_sha256: str
    duplicate: bool


class CampaignReportBuilder:
    """Aggregate an allowlisted decision input without database or network access."""

    def __init__(self, evaluator: ReleaseGateEvaluator | None = None) -> None:
        self._evaluator = evaluator or ReleaseGateEvaluator()

    def prepare(self, snapshot: CampaignReportSnapshot) -> PreparedCampaignReport:
        payload = snapshot.decision_input
        campaign = _mapping(payload.get("campaign"), "campaign")
        results = _rows(payload.get("results"), "results")
        reviews = _rows(payload.get("reviews"), "reviews")
        gold = _rows(payload.get("gold_assertions"), "gold_assertions")
        invocations = _rows(payload.get("model_invocations"), "model_invocations")
        policy_snapshot = _mapping(
            campaign.get("gate_policy_snapshot"),
            "gate_policy_snapshot",
        )
        policy = gate_policy_from_snapshot(policy_snapshot)
        self._verify_frozen_assets(campaign)

        metrics, aggregates = self._aggregate(
            campaign,
            results,
            reviews,
            gold,
            invocations,
            policy,
        )
        provisional = self._evaluator.evaluate(metrics, policy, final=False)
        finalizable, finalization_reason = self._finalization_state(
            snapshot,
            campaign,
            results,
            metrics,
            provisional,
            policy,
        )
        decision = (
            self._evaluator.evaluate(metrics, policy, final=True)
            if finalizable
            else provisional
        )
        if not finalizable:
            if self._only_review_is_pending(decision):
                decision = GateDecision(
                    GateStatus.PENDING,
                    "PENDING_REVIEW",
                    decision.rules,
                )
            elif decision.status is not GateStatus.PENDING:
                decision = GateDecision(
                    GateStatus.PENDING,
                    "PENDING_EXECUTION",
                    decision.rules,
                )

        automatic_status = self._automatic_status(decision)
        manual_status = self._manual_status(metrics, policy, snapshot)
        report_payload = self._report_payload(
            snapshot,
            campaign,
            results,
            decision,
            aggregates,
            policy,
        )
        json_bytes = canonical_json_bytes(report_payload)
        decision_hash = hashlib.sha256(json_bytes).hexdigest()
        markdown_bytes = self._markdown(report_payload).encode("utf-8")
        return PreparedCampaignReport(
            tenant_id=snapshot.tenant_id,
            campaign_id=snapshot.campaign_id,
            campaign_status=snapshot.campaign_status,
            campaign_version=snapshot.campaign_version,
            decision_input_hash=snapshot.decision_input_hash,
            decision_hash=decision_hash,
            decision=decision,
            automatic_gate_status=automatic_status,
            manual_review_status=manual_status,
            finalizable=finalizable,
            finalization_reason=finalization_reason,
            json_bytes=json_bytes,
            markdown_bytes=markdown_bytes,
        )

    @staticmethod
    def _verify_frozen_assets(campaign: Mapping[str, Any]) -> None:
        for prefix in ("profile", "gate_policy", "review_rubric", "cost_rate"):
            snapshot = _mapping(campaign.get(f"{prefix}_snapshot"), f"{prefix}_snapshot")
            if snapshot_hash(snapshot) != campaign.get(f"{prefix}_hash"):
                raise InvariantViolation(
                    "Frozen Campaign policy asset hash does not match",
                    code="campaign_policy_snapshot_mismatch",
                    details={"asset": prefix},
                )

    def _aggregate(
        self,
        campaign: Mapping[str, Any],
        results: tuple[Mapping[str, Any], ...],
        reviews: tuple[Mapping[str, Any], ...],
        gold: tuple[Mapping[str, Any], ...],
        invocations: tuple[Mapping[str, Any], ...],
        policy: GatePolicy,
    ) -> tuple[CampaignMetrics, dict[str, Any]]:
        terminal = tuple(
            item for item in results if item.get("scheduling_state") in _TERMINAL_RESULT_STATES
        )
        actual_fast = tuple(item for item in terminal if item.get("actual_mode") == "FAST")
        actual_research = tuple(
            item for item in terminal if item.get("actual_mode") == "RESEARCH"
        )
        unanswerable = tuple(
            item
            for item in terminal
            if item.get("answerability") == "intentionally_unanswerable"
        )
        review_by_result = {str(item.get("result_id")): item for item in reviews}
        selected = tuple(item for item in results if bool(item.get("manual_review_selected")))
        completed_reviews = tuple(
            review_by_result[str(item.get("result_id"))]
            for item in selected
            if str(item.get("result_id")) in review_by_result
        )

        coverage_macro, coverage_strata = self._coverage(terminal)
        gold_macro_bps, valid_gold_cases = self._gold_macro(terminal, gold)
        gold_valid = tuple(item for item in gold if item.get("status") in {"PASS", "FAIL"})
        latency_fast, latency_research = self._latencies(terminal, policy)
        ledger_mismatch_count, ledger_summary = self._ledger_reconciliation(
            campaign,
            results,
            invocations,
        )
        hard = self._hard_violations(
            campaign,
            terminal,
            results,
            reviews,
            gold,
            invocations,
            ledger_mismatch_count,
        )
        error_classes = Counter(
            str(item.get("error_class"))
            for item in terminal
            if item.get("error_class") is not None
        )
        skip_reasons = Counter(
            self._safe_code(item.get("stable_skip_reason"), "invalid_skip_reason")
            for item in results
            if item.get("scheduling_state") == "SKIPPED"
            and item.get("stable_skip_reason") is not None
        )
        mode_rows = tuple(item for item in terminal if item.get("actual_mode") is not None)
        correct_reviews = sum(
            item.get("correctness_verdict") == "CORRECT" for item in completed_reviews
        )
        unreviewable_reviews = sum(
            item.get("correctness_verdict") == "UNREVIEWABLE"
            for item in completed_reviews
        )
        explicit_gaps = sum(
            item.get("answer_quality") != "COMPLETE"
            and (
                _integer(item.get("fact_gap", 0), "fact_gap") > 0
                or item.get("run_stop_reason") in _EXPLICIT_GAP_STOP_REASONS
            )
            for item in unanswerable
        )
        projected_cost: Decimal | None = None
        if policy.kind is GateKind.SMOKE and terminal:
            stored_ledger = _mapping(campaign.get("ledger"), "campaign ledger")
            incurred = _decimal(
                stored_ledger.get("observed_estimated_cost", "0"),
                "observed_estimated_cost",
            ) + _decimal(
                stored_ledger.get("possibly_billed_cost_charge", "0"),
                "possibly_billed_cost_charge",
            )
            projected_cost = incurred * Decimal(120) / Decimal(len(terminal))

        metrics = CampaignMetrics(
            terminal_results=len(terminal),
            actual_fast_results=len(actual_fast),
            actual_research_results=len(actual_research),
            distinct_case_count=len({str(item.get("case_id")) for item in terminal}),
            unanswerable_case_count=len(
                {str(item.get("case_id")) for item in unanswerable}
            ),
            unanswerable_terminal_results=len(unanswerable),
            completed_reviews=len(completed_reviews),
            unreviewable_reviews=unreviewable_reviews,
            valid_gold_case_count=len(valid_gold_cases),
            hard_violation_counts=hard,
            fast_latency_ms=latency_fast,
            research_latency_ms=latency_research,
            mode_match_count=sum(
                item.get("expected_mode") == item.get("actual_mode")
                for item in mode_rows
            ),
            mode_total_count=len(mode_rows),
            coverage_macro_bps=coverage_macro,
            coverage_stratum_bps=coverage_strata,
            gold_pass_count=sum(item.get("status") == "PASS" for item in gold_valid),
            gold_total_count=len(gold_valid),
            gold_macro_bps=gold_macro_bps,
            review_correct_count=correct_reviews,
            review_citation_relevance_pass_count=sum(
                item.get("citation_relevance") == "PASS" for item in completed_reviews
            ),
            review_source_appropriateness_pass_count=sum(
                item.get("source_appropriateness") == "PASS"
                for item in completed_reviews
            ),
            review_freshness_pass_count=sum(
                item.get("freshness") == "PASS" for item in completed_reviews
            ),
            review_completeness_pass_count=sum(
                item.get("completeness") == "PASS" for item in completed_reviews
            ),
            review_total_count=len(completed_reviews),
            unanswerable_gap_count=explicit_gaps,
            unanswerable_gap_total=len(unanswerable),
            degraded_count=sum(bool(item.get("degraded")) for item in terminal),
            infrastructure_failure_count=sum(
                item.get("error_class") == "INFRASTRUCTURE"
                or "infrastructure_failure" in item.get("error_signal_flags", ())
                for item in terminal
            ),
            projected_full_cost_usd=projected_cost,
            cost_stop_triggered=campaign.get("stop_intent") == "BUDGET",
            call_ceiling_triggered=campaign.get("stop_intent") == "CALL_CEILING",
        )
        intervals: dict[str, Any] = {}
        for name, numerator, denominator in (
            ("mode_accuracy", metrics.mode_match_count, metrics.mode_total_count),
            ("gold_assertions", metrics.gold_pass_count, metrics.gold_total_count),
            ("review_correct", metrics.review_correct_count, metrics.review_total_count),
            ("unanswerable_gap", explicit_gaps, len(unanswerable)),
        ):
            if denominator:
                interval = wilson_interval_bps(numerator, denominator)
                intervals[name] = canonical_snapshot(interval)
        aggregates = {
            "counts": {
                "planned": len(results),
                "submitted": sum(item.get("search_run_id") is not None for item in results),
                "terminal": len(terminal),
                "collected": sum(
                    item.get("scheduling_state") == "COLLECTED" for item in results
                ),
                "failed": sum(item.get("scheduling_state") == "FAILED" for item in results),
                "skipped": sum(item.get("scheduling_state") == "SKIPPED" for item in results),
                "degraded": metrics.degraded_count,
            },
            "latency": {
                "FAST": _percentiles(latency_fast, policy.fast_latency_p95_ms),
                "RESEARCH": _percentiles(
                    latency_research,
                    policy.research_latency_p95_ms,
                ),
            },
            "coverage": {
                "case_macro_bps": coverage_macro,
                "stratum_bps": list(coverage_strata),
            },
            "gold": {
                "case_macro_bps": gold_macro_bps,
                "valid_case_count": len(valid_gold_cases),
                "pass_count": metrics.gold_pass_count,
                "total_count": metrics.gold_total_count,
            },
            "reviews": {
                "selected": len(selected),
                "completed": len(completed_reviews),
                "unreviewable": unreviewable_reviews,
                "major_error": sum(
                    item.get("correctness_verdict") == "MAJOR_ERROR"
                    for item in completed_reviews
                ),
                "reason_codes": dict(
                    sorted(
                        Counter(
                            self._safe_code(code, "invalid_review_reason")
                            for item in completed_reviews
                            for code in item.get("reason_codes", ())
                        ).items()
                    )
                ),
            },
            "ledger": ledger_summary,
            "error_classes": dict(sorted(error_classes.items())),
            "skip_reasons": dict(sorted(skip_reasons.items())),
            "confidence_intervals_95_bps": intervals,
        }
        return metrics, aggregates

    @staticmethod
    def _coverage(
        terminal: tuple[Mapping[str, Any], ...],
    ) -> tuple[int, tuple[int, int, int, int]]:
        case_values: dict[str, list[Fraction]] = defaultdict(list)
        stratum_case_values: dict[tuple[str, str], dict[str, list[Fraction]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        for item in terminal:
            if item.get("answerability") != "answerable":
                continue
            denominator = max(
                _integer(item.get("fact_total", 0), "fact_total"),
                _integer(
                    item.get("minimum_required_facts", 0),
                    "minimum_required_facts",
                ),
            )
            covered = _integer(item.get("fact_covered", 0), "fact_covered")
            value = Fraction(min(covered, denominator), denominator) if denominator else Fraction(0)
            case_id = str(item.get("case_id"))
            case_values[case_id].append(value)
            stratum_case_values[
                (str(item.get("expected_mode")), str(item.get("locale")))
            ][case_id].append(value)

        def macro(groups: Mapping[str, list[Fraction]]) -> int:
            if not groups:
                return 0
            case_means = tuple(sum(values, Fraction()) / len(values) for values in groups.values())
            return _fraction_bps(sum(case_means, Fraction()) / len(case_means))

        order = (("FAST", "zh-CN"), ("FAST", "en"), ("RESEARCH", "zh-CN"), ("RESEARCH", "en"))
        strata = cast(
            tuple[int, int, int, int],
            tuple(macro(stratum_case_values[key]) for key in order),
        )
        return macro(case_values), strata

    @staticmethod
    def _gold_macro(
        terminal: tuple[Mapping[str, Any], ...],
        gold: tuple[Mapping[str, Any], ...],
    ) -> tuple[int, frozenset[str]]:
        result_by_id = {str(item.get("result_id")): item for item in terminal}
        per_result: dict[str, list[bool]] = defaultdict(list)
        for item in gold:
            result_id = str(item.get("result_id"))
            if result_id in result_by_id and item.get("status") in {"PASS", "FAIL"}:
                per_result[result_id].append(item.get("status") == "PASS")
        per_case: dict[str, list[Fraction]] = defaultdict(list)
        for result_id, values in per_result.items():
            per_case[str(result_by_id[result_id].get("case_id"))].append(
                Fraction(sum(values), len(values))
            )
        if not per_case:
            return 0, frozenset()
        case_means = tuple(
            sum(values, Fraction()) / len(values) for values in per_case.values()
        )
        return (
            _fraction_bps(sum(case_means, Fraction()) / len(case_means)),
            frozenset(per_case),
        )

    @staticmethod
    def _latencies(
        terminal: tuple[Mapping[str, Any], ...],
        policy: GatePolicy,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        grouped: dict[str, list[int]] = {"FAST": [], "RESEARCH": []}
        for item in terminal:
            mode = str(item.get("actual_mode") or item.get("expected_mode"))
            if mode not in grouped:
                continue
            raw = item.get("latency_ms")
            fallback = (
                policy.fast_latency_p95_ms
                if mode == "FAST"
                else policy.research_latency_p95_ms
            ) + 1
            grouped[mode].append(fallback if raw is None else _integer(raw, "latency_ms"))
        return tuple(grouped["FAST"]), tuple(grouped["RESEARCH"])

    @staticmethod
    def _ledger_reconciliation(
        campaign: Mapping[str, Any],
        results: tuple[Mapping[str, Any], ...],
        invocations: tuple[Mapping[str, Any], ...],
    ) -> tuple[int, dict[str, Any]]:
        stored_counts = _mapping(campaign.get("counts"), "campaign counts")
        stored_ledger = _mapping(campaign.get("ledger"), "campaign ledger")
        active = tuple(item for item in results if item.get("reservation_state") == "ACTIVE")
        settled = tuple(item for item in results if item.get("reservation_state") == "SETTLED")
        derived_counts = {
            "planned_count": len(results),
            "submitted_count": sum(item.get("search_run_id") is not None for item in results),
            "collected_count": sum(item.get("scheduling_state") == "COLLECTED" for item in results),
            "failed_count": sum(item.get("scheduling_state") == "FAILED" for item in results),
            "skipped_count": sum(item.get("scheduling_state") == "SKIPPED" for item in results),
            "degraded_count": sum(
                item.get("scheduling_state") in _TERMINAL_RESULT_STATES
                and bool(item.get("degraded"))
                for item in results
            ),
        }
        derived_ledger: dict[str, int | Decimal] = {
            "observed_provider_calls": sum(
                _integer(item.get("settled_observed_provider_calls", 0), "settled calls")
                for item in settled
            ),
            "possibly_billed_call_charge": sum(
                _integer(item.get("possibly_billed_call_charge", 0), "possibly billed calls")
                for item in settled
            ),
            "reserved_provider_calls": sum(
                _integer(item.get("reserved_provider_calls", 0), "reserved calls")
                for item in active
            ),
            "observed_prompt_tokens": sum(
                _integer(item.get("prompt_tokens", 0), "prompt tokens") for item in settled
            ),
            "observed_completion_tokens": sum(
                _integer(item.get("completion_tokens", 0), "completion tokens")
                for item in settled
            ),
            "observed_estimated_cost": sum(
                (_decimal(item.get("settled_observed_cost", "0"), "settled cost") for item in settled),
                Decimal(0),
            ),
            "possibly_billed_cost_charge": sum(
                (_decimal(item.get("possibly_billed_cost_charge", "0"), "possibly billed cost") for item in settled),
                Decimal(0),
            ),
            "reserved_estimated_cost": sum(
                (_decimal(item.get("reserved_estimated_cost", "0"), "reserved cost") for item in active),
                Decimal(0),
            ),
            "possibly_billed_count": sum(
                _integer(item.get("possibly_billed_call_charge", 0), "possibly billed calls") > 0
                or _decimal(item.get("possibly_billed_cost_charge", "0"), "possibly billed cost") > 0
                for item in settled
            ),
        }
        mismatches: list[str] = []
        for key, value in derived_counts.items():
            if _integer(stored_counts.get(key, 0), key) != value:
                mismatches.append(key)
        for key, value in derived_ledger.items():
            stored_value = stored_ledger.get(key, 0)
            if isinstance(value, Decimal):
                if _decimal(stored_value, key) != value:
                    mismatches.append(key)
            elif _integer(stored_value, key) != value:
                mismatches.append(key)

        invocation_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for item in invocations:
            invocation_by_run[str(item.get("run_id"))].append(item)
        rate_snapshot = _mapping(
            campaign.get("cost_rate_snapshot"),
            "cost_rate_snapshot",
        )
        prompt_rate = _decimal(
            rate_snapshot.get("prompt_per_million_usd", "0"),
            "prompt_per_million_usd",
        )
        completion_rate = _decimal(
            rate_snapshot.get("completion_per_million_usd", "0"),
            "completion_per_million_usd",
        )
        possibly_reserve = _decimal(
            rate_snapshot.get("possibly_billed_run_reserve_usd", "0"),
            "possibly_billed_run_reserve_usd",
        )
        for item in settled:
            if item.get("search_run_id") is None:
                result_id = str(item.get("result_id"))
                valid_unknown_outbound = (
                    item.get("scheduling_state") == "FAILED"
                    and _integer(
                        item.get("settled_observed_provider_calls", 0),
                        "settled calls",
                    )
                    == 0
                    and _integer(item.get("prompt_tokens", 0), "prompt tokens") == 0
                    and _integer(
                        item.get("completion_tokens", 0),
                        "completion tokens",
                    )
                    == 0
                    and _integer(item.get("model_call_count", 0), "model_call_count")
                    == 0
                    and _integer(
                        item.get("possibly_billed_call_charge", 0),
                        "possibly billed calls",
                    )
                    == _integer(
                        item.get("reserved_provider_calls", 0),
                        "reserved calls",
                    )
                    and _decimal(
                        item.get("possibly_billed_cost_charge", "0"),
                        "possibly billed cost",
                    )
                    == _decimal(
                        item.get("reserved_estimated_cost", "0"),
                        "reserved cost",
                    )
                )
                if not valid_unknown_outbound:
                    mismatches.append(f"result:{result_id}:unknown_outbound_settlement")
                continue
            run_items = invocation_by_run.get(str(item.get("search_run_id")), [])
            billed = tuple(
                row
                for row in run_items
                if bool(row.get("provider_called"))
                and row.get("billing_disposition") == "BILLED"
            )
            possibly = tuple(
                row
                for row in run_items
                if bool(row.get("provider_called"))
                and row.get("billing_disposition") == "POSSIBLY_BILLED"
            )
            result_id = str(item.get("result_id"))
            billed_prompt_tokens = sum(
                _integer(row.get("prompt_tokens", 0), "prompt tokens")
                for row in billed
            )
            billed_completion_tokens = sum(
                _integer(row.get("completion_tokens", 0), "completion tokens")
                for row in billed
            )
            invocation_cost = _money(
                (
                    Decimal(billed_prompt_tokens) * prompt_rate
                    + Decimal(billed_completion_tokens) * completion_rate
                )
                / Decimal(1_000_000)
            )
            if len(billed) != _integer(
                item.get("settled_observed_provider_calls", 0), "settled calls"
            ):
                mismatches.append(f"result:{result_id}:observed_calls")
            if billed_prompt_tokens != _integer(
                item.get("prompt_tokens", 0), "prompt tokens"
            ):
                mismatches.append(f"result:{result_id}:prompt_tokens")
            if billed_completion_tokens != _integer(
                item.get("completion_tokens", 0), "completion tokens"
            ):
                mismatches.append(f"result:{result_id}:completion_tokens")
            if len(possibly) != _integer(
                item.get("possibly_billed_call_charge", 0), "possibly billed calls"
            ):
                mismatches.append(f"result:{result_id}:possibly_billed_calls")
            if len(billed) + len(possibly) != _integer(
                item.get("model_call_count", 0), "model_call_count"
            ):
                mismatches.append(f"result:{result_id}:model_call_count")
            if invocation_cost != _decimal(
                item.get("settled_observed_cost", "0"),
                "settled_observed_cost",
            ):
                mismatches.append(f"result:{result_id}:observed_cost")
            expected_possible_cost = _money(possibly_reserve) if possibly else Decimal(0)
            if expected_possible_cost != _decimal(
                item.get("possibly_billed_cost_charge", "0"),
                "possibly_billed_cost_charge",
            ):
                mismatches.append(f"result:{result_id}:possibly_billed_cost")
        for item in results:
            state = str(item.get("scheduling_state"))
            reservation = str(item.get("reservation_state"))
            if state == "COLLECTED" and reservation != "SETTLED":
                mismatches.append(f"result:{item.get('result_id')}:collector_settlement")
            elif (
                state == "FAILED"
                and item.get("search_run_id") is not None
                and reservation != "SETTLED"
            ):
                mismatches.append(f"result:{item.get('result_id')}:failed_settlement")
        terminal_run_ids = {
            str(item.get("search_run_id"))
            for item in results
            if item.get("scheduling_state") in _TERMINAL_RESULT_STATES
            and item.get("search_run_id") is not None
        }
        orphan_started = sum(
            str(item.get("run_id")) in terminal_run_ids
            and item.get("status") not in _TERMINAL_INVOCATION_STATES
            for item in invocations
        )
        if orphan_started:
            mismatches.append("orphan_started_model_invocations")
        summary = {
            "matched": not mismatches,
            "mismatch_count": len(mismatches),
            "mismatch_fields": sorted(mismatches),
            "stored_counts": dict(stored_counts),
            "derived_counts": derived_counts,
            "stored_ledger": dict(stored_ledger),
            "derived_ledger": derived_ledger,
            "orphan_started_model_invocations": orphan_started,
        }
        return len(mismatches), summary

    @staticmethod
    def _hard_violations(
        campaign: Mapping[str, Any],
        terminal: tuple[Mapping[str, Any], ...],
        all_results: tuple[Mapping[str, Any], ...],
        reviews: tuple[Mapping[str, Any], ...],
        gold: tuple[Mapping[str, Any], ...],
        invocations: tuple[Mapping[str, Any], ...],
        ledger_mismatch_count: int,
    ) -> dict[str, int]:
        stored_ledger = _mapping(campaign.get("ledger"), "campaign ledger")
        exposure = (
            _integer(stored_ledger.get("observed_provider_calls", 0), "observed calls")
            + _integer(
                stored_ledger.get("possibly_billed_call_charge", 0),
                "possibly billed calls",
            )
            + _integer(stored_ledger.get("reserved_provider_calls", 0), "reserved calls")
        )
        admission_ceiling = _integer(
            campaign.get("provider_call_admission_ceiling", 0),
            "provider_call_admission_ceiling",
        )
        signals = [
            str(signal)
            for item in terminal
            for signal in item.get("error_signal_flags", ())
        ]
        source_mismatch = sum(
            item.get("scheduling_state") == "COLLECTED"
            and item.get("source_snapshot_digest") != item.get("current_source_digest")
            for item in terminal
        )
        orphan_started = sum(
            item.get("status") not in _TERMINAL_INVOCATION_STATES
            for item in invocations
            if str(item.get("run_id"))
            in {
                str(result.get("search_run_id"))
                for result in terminal
                if result.get("search_run_id") is not None
            }
        )
        selected_result_ids = {
            str(item.get("result_id"))
            for item in all_results
            if bool(item.get("manual_review_selected"))
        }
        gold_by_result: dict[str, Counter[str]] = defaultdict(Counter)
        for item in gold:
            gold_by_result[str(item.get("result_id"))][str(item.get("status"))] += 1
        violations = {
            "citation_traceability": sum(
                _integer(item.get("traceability_violation_count", 0), "traceability")
                for item in terminal
            ),
            "source_snapshot_mismatch": source_mismatch,
            "complete_without_evidence": sum(
                item.get("answer_quality") == "COMPLETE"
                and (
                    _integer(item.get("fact_covered", 0), "fact_covered") == 0
                    or _integer(
                        item.get("valid_citation_chain_count", 0),
                        "valid_citation_chain_count",
                    )
                    == 0
                )
                for item in terminal
            ),
            "plan_completeness": sum(
                bool(item.get("plan_completeness_failure")) for item in terminal
            ),
            "query_pollution": sum(
                _integer(item.get("query_pollution_count", 0), "query pollution")
                for item in terminal
            ),
            "model_call_budget": sum(
                bool(item.get("budget_violation"))
                or "model_call_budget_exceeded" in item.get("error_signal_flags", ())
                for item in terminal
            ),
            "campaign_call_exposure": int(exposure > admission_ceiling),
            "campaign_ledger_mismatch": ledger_mismatch_count,
            "orphan_started_model_invocation": orphan_started,
            "idempotency_payload_mismatch": sum(
                signal in {"submission_payload_mismatch", "idempotency_payload_mismatch"}
                for signal in signals
            ),
            "critical_gold_assertion": sum(
                bool(item.get("critical")) and item.get("status") == "FAIL"
                for item in gold
            ),
            "manual_major_error": sum(
                item.get("correctness_verdict") == "MAJOR_ERROR" for item in reviews
            ),
            "review_binding": sum(
                str(item.get("result_id")) not in selected_result_ids
                or item.get("rubric_version")
                != campaign.get("review_rubric_version")
                for item in reviews
            ),
            "gold_audit_mismatch": sum(
                _integer(item.get("gold_assertion_total", 0), "gold total")
                != sum(gold_by_result[str(item.get("result_id"))].values())
                or _integer(item.get("gold_assertion_passed", 0), "gold passed")
                != gold_by_result[str(item.get("result_id"))]["PASS"]
                or _integer(item.get("gold_assertion_failed", 0), "gold failed")
                != gold_by_result[str(item.get("result_id"))]["FAIL"]
                or _integer(
                    item.get("gold_assertion_not_applicable", 0),
                    "gold not applicable",
                )
                != gold_by_result[str(item.get("result_id"))]["NOT_APPLICABLE"]
                for item in terminal
            ),
            "permanent_configuration": sum(
                item.get("error_class") == "PERMANENT_CONFIGURATION" for item in terminal
            ),
            "collector_schema_mismatch": sum(
                item.get("scheduling_state") == "COLLECTED"
                and item.get("collector_schema_version")
                != campaign.get("collector_schema_version")
                for item in terminal
            ),
            "provenance": int(
                not bool(campaign.get("candidate_source_clean"))
                or not bool(campaign.get("harness_source_clean"))
            ),
            "report_structure": sum(
                (
                    item.get("error_code") is not None
                    and not _STABLE_CODE.fullmatch(str(item.get("error_code")))
                )
                or (
                    item.get("stable_skip_reason") is not None
                    and not _STABLE_CODE.fullmatch(
                        str(item.get("stable_skip_reason"))
                    )
                )
                for item in all_results
            )
            + sum(
                not _STABLE_CODE.fullmatch(str(code))
                for item in reviews
                for code in item.get("reason_codes", ())
            )
            + sum(
                not _STABLE_CODE.fullmatch(str(signal))
                for item in terminal
                for signal in item.get("error_signal_flags", ())
            ),
        }
        return violations

    @staticmethod
    def _finalization_state(
        snapshot: CampaignReportSnapshot,
        campaign: Mapping[str, Any],
        results: tuple[Mapping[str, Any], ...],
        metrics: CampaignMetrics,
        provisional: GateDecision,
        policy: GatePolicy,
    ) -> tuple[bool, str | None]:
        # PAUSE is a non-terminal operator checkpoint even when the partial
        # snapshot contains a hard candidate defect. Persist the defect in the
        # provisional report, but never bind a final gate or consume resume.
        if snapshot.campaign_status is CampaignStatus.PAUSED or campaign.get(
            "stop_intent"
        ) == "PAUSE":
            return False, None
        hard_failure = any(
            rule.rule_id == "hard_safety" and not rule.passed
            for rule in provisional.rules
        )
        if hard_failure:
            return True, "fatal_safety"
        if snapshot.campaign_status in {
            CampaignStatus.COMPLETED,
            CampaignStatus.ABORTED,
        }:
            return True, "terminal_campaign"
        max_runs = _integer(campaign.get("max_runs", 0), "max_runs")
        execution_sealed = (
            len(results) == max_runs
            and all(item.get("scheduling_state") in _SEALED_RESULT_STATES for item in results)
            and not any(item.get("reservation_state") == "ACTIVE" for item in results)
        )
        if not execution_sealed:
            return False, None
        if any(
            rule.rule_id in _SAMPLE_RULES_EXCEPT_REVIEW and not rule.passed
            for rule in provisional.rules
        ):
            return True, "sealed_insufficient_sample"
        if policy.required_reviews == 0:
            return True, "operational_gate_complete"
        if metrics.unreviewable_reviews:
            return True, "unreviewable_sample"
        if metrics.completed_reviews >= policy.required_reviews:
            return True, "review_complete"
        if (
            snapshot.review_deadline_at is not None
            and snapshot.database_now >= snapshot.review_deadline_at
        ):
            return True, "review_deadline_expired"
        if campaign.get("stop_intent") in {"ABORT", "FATAL", "BUDGET", "CALL_CEILING"}:
            return True, "controlled_stop"
        return False, None

    @staticmethod
    def _only_review_is_pending(decision: GateDecision) -> bool:
        failed_sample_rules = {
            rule.rule_id
            for rule in decision.rules
            if not rule.passed
            and rule.rule_id
            in _SAMPLE_RULES_EXCEPT_REVIEW | {"review_sample"}
        }
        return failed_sample_rules == {"review_sample"}

    @staticmethod
    def _automatic_status(decision: GateDecision) -> str:
        automatic_failures = tuple(
            rule
            for rule in decision.rules
            if not rule.passed
            and not rule.rule_id.startswith("review_")
            and rule.rule_id not in {"hard_safety", "hard_manual_major_error"}
        )
        if any(rule.rule_id.startswith("hard_") for rule in automatic_failures):
            return "FAIL"
        if automatic_failures:
            if decision.status is GateStatus.PENDING:
                return "PENDING"
            if decision.status is GateStatus.INSUFFICIENT_SAMPLE:
                return "INSUFFICIENT_SAMPLE"
            return "FAIL"
        return "PASS"

    @staticmethod
    def _manual_status(
        metrics: CampaignMetrics,
        policy: GatePolicy,
        snapshot: CampaignReportSnapshot,
    ) -> str:
        if policy.required_reviews == 0:
            return "NOT_REQUIRED"
        if metrics.unreviewable_reviews:
            return "INSUFFICIENT_SAMPLE"
        if metrics.hard_violation_counts.get("manual_major_error", 0):
            return "FAIL"
        if metrics.completed_reviews >= policy.required_reviews:
            return "COMPLETE"
        if (
            snapshot.review_deadline_at is not None
            and snapshot.database_now >= snapshot.review_deadline_at
        ):
            return "INSUFFICIENT_SAMPLE"
        return "PENDING"

    @staticmethod
    def _safe_environment(value: object) -> dict[str, Any]:
        source = _mapping(value, "environment_snapshot")
        return {
            key: canonical_snapshot(source[key])
            for key in sorted(source)
            if key in _SAFE_ENVIRONMENT_KEYS
        }

    @staticmethod
    def _safe_code(value: object, replacement: str) -> str:
        rendered = str(value)
        return rendered if _STABLE_CODE.fullmatch(rendered) else replacement

    def _report_payload(
        self,
        snapshot: CampaignReportSnapshot,
        campaign: Mapping[str, Any],
        results: tuple[Mapping[str, Any], ...],
        decision: GateDecision,
        aggregates: Mapping[str, Any],
        policy: GatePolicy,
    ) -> dict[str, Any]:
        diagnostics = [
            {
                "result_id": item.get("result_id"),
                "search_run_id": item.get("search_run_id"),
                "case_id": item.get("case_id"),
                "repetition": item.get("repetition"),
                "scheduling_state": item.get("scheduling_state"),
                "expected_mode": item.get("expected_mode"),
                "actual_mode": item.get("actual_mode"),
                "run_status": item.get("run_status"),
                "answer_quality": item.get("answer_quality"),
                "latency_ms": item.get("latency_ms"),
                "fact_total": item.get("fact_total"),
                "fact_covered": item.get("fact_covered"),
                "fact_gap": item.get("fact_gap"),
                "traceability_violation_count": item.get(
                    "traceability_violation_count"
                ),
                "gold_assertion_passed": item.get("gold_assertion_passed"),
                "gold_assertion_failed": item.get("gold_assertion_failed"),
                "model_call_count": item.get("model_call_count"),
                "degraded": item.get("degraded"),
                "error_class": item.get("error_class"),
                "error_code": self._safe_code(
                    item.get("error_code"),
                    "invalid_error_code",
                )
                if item.get("error_code") is not None
                else None,
                "stable_skip_reason": self._safe_code(
                    item.get("stable_skip_reason"),
                    "invalid_skip_reason",
                )
                if item.get("stable_skip_reason") is not None
                else None,
            }
            for item in sorted(
                results,
                key=lambda row: (
                    _integer(row.get("schedule_ordinal", 0), "schedule_ordinal"),
                    str(row.get("result_id")),
                ),
            )
        ]
        return {
            "schema": REPORT_SCHEMA_VERSION,
            "campaign": {
                "campaign_id": snapshot.campaign_id,
                "profile_version": campaign.get("profile_version"),
                "profile_hash": campaign.get("profile_hash"),
                "profile_snapshot": campaign.get("profile_snapshot"),
                "gate_policy_version": policy.version,
                "gate_policy_hash": campaign.get("gate_policy_hash"),
                "gate_policy_snapshot": campaign.get("gate_policy_snapshot"),
                "manifest_version": campaign.get("manifest_version"),
                "manifest_hash": campaign.get("manifest_hash"),
                "manifest_case_count": campaign.get("manifest_case_count"),
                "repetitions": campaign.get("repetitions"),
                "review_rubric_version": campaign.get("review_rubric_version"),
                "review_rubric_hash": campaign.get("review_rubric_hash"),
                "review_rubric_snapshot": campaign.get("review_rubric_snapshot"),
                "cost_rate_version": campaign.get("cost_rate_version"),
                "cost_rate_hash": campaign.get("cost_rate_hash"),
                "cost_rate_snapshot": campaign.get("cost_rate_snapshot"),
                "candidate_commit_sha": campaign.get("candidate_commit_sha"),
                "candidate_source_clean": campaign.get("candidate_source_clean"),
                "candidate_image_id": campaign.get("candidate_image_id"),
                "candidate_oci_revision": campaign.get("candidate_oci_revision"),
                "candidate_config_hash": campaign.get("candidate_config_hash"),
                "alembic_head": campaign.get("alembic_head"),
                "harness_commit_sha": campaign.get("harness_commit_sha"),
                "harness_source_clean": campaign.get("harness_source_clean"),
                "harness_fileset_hash": campaign.get("harness_fileset_hash"),
                "collector_schema_version": campaign.get("collector_schema_version"),
                "environment_identity_hash": campaign.get(
                    "environment_identity_hash"
                ),
                "environment": self._safe_environment(
                    campaign.get("environment_snapshot", {})
                ),
            },
            "decision_input_hash": snapshot.decision_input_hash,
            "decision": {
                "status": decision.status,
                "state": decision.decision_state,
                "rules": [
                    {
                        "rule_id": rule.rule_id,
                        "observed": rule.observed,
                        "threshold": rule.threshold,
                        "sample_size": rule.sample_size,
                        "status": "PASS" if rule.passed else "FAIL",
                        "reason_code": rule.reason_code,
                    }
                    for rule in decision.rules
                ],
            },
            "aggregates": canonical_snapshot(aggregates),
            "results": diagnostics,
            "scope_statement": "controlled baseline, not production load proof",
        }

    @staticmethod
    def _markdown(payload: Mapping[str, Any]) -> str:
        campaign = _mapping(payload["campaign"], "report campaign")
        decision = _mapping(payload["decision"], "report decision")
        aggregates = _mapping(payload["aggregates"], "report aggregates")
        counts = _mapping(aggregates["counts"], "report counts")
        lines = [
            "# Shadow Campaign Release Gate",
            "",
            f"- Campaign: `{campaign['campaign_id']}`",
            f"- Decision: **{decision['status']}** (`{decision['state']}`)",
            f"- Decision input: `{payload['decision_input_hash']}`",
            f"- Candidate: `{campaign['candidate_commit_sha']}`",
            f"- Harness: `{campaign['harness_commit_sha']}`",
            f"- Policy: `{campaign['gate_policy_version']}`",
            "- Scope: controlled baseline, not production load proof",
            "",
            "## Sample",
            "",
            "| Planned | Submitted | Terminal | Collected | Failed | Skipped | Degraded |",
            "|---:|---:|---:|---:|---:|---:|---:|",
            "| {planned} | {submitted} | {terminal} | {collected} | {failed} | {skipped} | {degraded} |".format(
                **counts
            ),
            "",
            "## Gate rules",
            "",
            "| Rule | Observed | Threshold | Sample | Status | Reason |",
            "|---|---:|---:|---:|---|---|",
        ]
        for rule in decision["rules"]:
            row = _mapping(rule, "gate rule")
            lines.append(
                "| `{rule_id}` | {observed} | {threshold} | {sample_size} | {status} | `{reason_code}` |".format(
                    **row,
                )
            )
        lines.extend(("", "JSON is the canonical decision payload; this Markdown is a deterministic view.", ""))
        return "\n".join(lines)


__all__ = [
    "CampaignReportBuilder",
    "CampaignReportSnapshot",
    "DECISION_INPUT_SCHEMA_VERSION",
    "FinalReportBinding",
    "FinalReportReceipt",
    "PreparedCampaignReport",
    "REPORT_SCHEMA_VERSION",
    "gate_policy_from_snapshot",
]
