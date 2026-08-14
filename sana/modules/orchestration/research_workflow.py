"""FAST upgrade, RESEARCH deadline, expansion and recovery policies."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.orchestration.domain import (
    AnswerQuality,
    RoutingDecision,
    RunStatus,
    SearchMode,
    SearchRun,
    StepStatus,
    StopReason,
)
from sana.modules.orchestration.policy import BudgetGuard, BudgetPhase, SearchPolicy
from sana.modules.search_planning.domain import Consequence, Freshness, NormalizedIntent
from sana.modules.search_planning.expansion import ExpansionDecision, RevisionStep
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation


@dataclass(frozen=True, slots=True)
class FastUpgradeDecision:
    should_upgrade: bool
    gap_fact_ids: tuple[UUID, ...]
    reason_codes: tuple[str, ...]
    status_by_fact: Mapping[UUID, FactCoverage]

    def __post_init__(self) -> None:
        if self.should_upgrade != bool(self.reason_codes):
            raise ValueError("FAST upgrade decision and reasons disagree")
        object.__setattr__(
            self,
            "status_by_fact",
            MappingProxyType(dict(self.status_by_fact)),
        )


class FastUpgradePolicy:
    def evaluate(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        intent: NormalizedIntent,
        fact_ids: Mapping[str, UUID],
        coverage: Mapping[UUID, CoverageAssessment],
    ) -> FastUpgradeDecision:
        gaps: list[UUID] = []
        reasons: list[str] = []
        statuses: dict[UUID, FactCoverage] = {}
        for fact in intent.facts:
            if not fact.required:
                continue
            try:
                fact_id = fact_ids[fact.key]
            except KeyError as exc:
                raise ValueError(f"Missing persisted ID for fact: {fact.key}") from exc
            assessment = coverage.get(fact_id)
            if assessment is not None and (
                assessment.tenant_id != tenant_id
                or assessment.run_id != run_id
                or assessment.fact_key != fact.key
            ):
                raise ValueError("Coverage assessment tenant/run/fact mismatch")
            status = assessment.status if assessment else FactCoverage.OPEN
            statuses[fact_id] = status
            needs_l2 = (
                fact.consequence is Consequence.HIGH
                or intent.requires_complete_sources
            )
            is_gap = status in {FactCoverage.OPEN, FactCoverage.PARTIAL} or (
                status is FactCoverage.COVERED and needs_l2
            )
            if not is_gap:
                continue
            gaps.append(fact_id)
            if status is FactCoverage.PARTIAL:
                reasons.append("evidence_conflict")
            if status is FactCoverage.OPEN and fact.freshness is not Freshness.STABLE:
                reasons.append("strong_freshness_gap")
            if fact.consequence is Consequence.HIGH and status is not FactCoverage.VERIFIED:
                reasons.append("high_consequence_gap")
            if intent.requires_complete_sources and status is not FactCoverage.VERIFIED:
                reasons.append("complete_coverage_gap")
        reason_codes = tuple(dict.fromkeys(reasons))
        return FastUpgradeDecision(bool(reason_codes), tuple(gaps), reason_codes, statuses)


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    quality: AnswerQuality
    reason: StopReason


class ResearchWorkflow:
    def upgrade_fast_run(
        self,
        run: SearchRun,
        decision: FastUpgradeDecision,
        policy: SearchPolicy,
    ) -> None:
        if not decision.should_upgrade:
            raise ValueError("FAST run has no valuable upgrade reason")
        if policy.version != run.routing.policy_version:
            raise ValueError("Upgrade policy version differs from the run snapshot")
        routing = RoutingDecision(
            SearchMode.RESEARCH,
            tuple(
                dict.fromkeys(
                    (*run.routing.reason_codes, "fast_value_upgrade", *decision.reason_codes)
                )
            ),
            policy.version,
            1.0,
        )
        budget = policy.snapshot(SearchMode.RESEARCH, run.budget.created_at)
        BudgetGuard(budget).validate(run.usage)
        run.upgrade_to_research(routing, budget)

    @staticmethod
    def can_schedule(
        run: SearchRun,
        phase: BudgetPhase,
        *,
        clock: Clock,
        estimated_seconds: float = 0,
    ) -> bool:
        if run.mode is not SearchMode.RESEARCH or run.status not in {
            RunStatus.RUNNING,
            RunStatus.WAITING,
        }:
            return False
        return BudgetGuard(run.budget).can_start(
            phase,
            clock.now(),
            estimated_seconds=estimated_seconds,
        )

    @staticmethod
    def record_expansion(run: SearchRun) -> None:
        if run.mode is not SearchMode.RESEARCH or run.is_terminal:
            raise InvariantViolation("Only an active RESEARCH run can expand")
        usage = run.usage.add(expansion_rounds=1)
        BudgetGuard(run.budget).validate(usage)
        run.record_usage(usage)

    @staticmethod
    def recover_new_steps(
        decision: ExpansionDecision,
        persisted_statuses: Mapping[tuple[int, str], StepStatus],
    ) -> tuple[RevisionStep, ...]:
        if not decision.should_expand:
            return ()
        return tuple(
            step
            for step in decision.steps
            if (step.plan_revision, step.step_key) not in persisted_statuses
        )

    @staticmethod
    def hard_deadline_reached(run: SearchRun, *, clock: Clock) -> bool:
        return clock.now() >= run.budget.hard_deadline_at

    @staticmethod
    def outcome(
        required_fact_statuses: tuple[FactCoverage, ...],
        *,
        stopped_for_low_gain: bool = False,
        deadline_reached: bool = False,
    ) -> ResearchOutcome:
        complete = bool(required_fact_statuses) and all(
            status is FactCoverage.VERIFIED for status in required_fact_statuses
        )
        if complete:
            return ResearchOutcome(AnswerQuality.COMPLETE, StopReason.FACTS_COVERED)
        if deadline_reached:
            return ResearchOutcome(AnswerQuality.PARTIAL, StopReason.TIME_BUDGET)
        if stopped_for_low_gain:
            return ResearchOutcome(
                AnswerQuality.PARTIAL,
                StopReason.INSUFFICIENT_EVIDENCE,
            )
        return ResearchOutcome(AnswerQuality.PARTIAL, StopReason.INSUFFICIENT_EVIDENCE)
