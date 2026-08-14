from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.evidence.evidence_gain import EvidenceGainEstimator
from sana.modules.orchestration.domain import (
    BudgetUsage,
    RoutingDecision,
    SearchMode,
    SearchRun,
)
from sana.modules.orchestration.policy import BudgetExceeded, BudgetGuard, BudgetPhase, SearchPolicy
from sana.modules.orchestration.research_workflow import ResearchWorkflow
from sana.modules.search_planning.domain import (
    FactRequirement,
    FactType,
    NormalizedIntent,
)
from sana.modules.search_planning.expansion import ExpansionPlanner, ExpansionStopReason
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.shared.clock import FrozenClock


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def intent() -> NormalizedIntent:
    return NormalizedIntent(
        "Apex Legends",
        (),
        "en",
        tuple(
            FactRequirement(
                key=key,
                fact_type=fact_type,
                description=key,
                subject=subject,
            )
            for key, fact_type, subject in (
                ("version", FactType.VERSION, "current version"),
                ("patch", FactType.PATCH_NOTES, "patch notes"),
                ("meta", FactType.TEAM_META, "team meta"),
            )
        ),
    )


def assessment(fact: FactRequirement, status: FactCoverage) -> CoverageAssessment:
    return CoverageAssessment(
        uuid4(),
        uuid4(),
        uuid4(),
        fact.key,
        status,
        None,
        (),
        (),
        (),
        (),
        0,
        False,
    )


def research_run() -> SearchRun:
    policy = SearchPolicy.default()
    run = SearchRun(
        id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        message_id=uuid4(),
        response_run_id=uuid4(),
        routing=RoutingDecision(
            SearchMode.RESEARCH,
            ("complete_source_requirement",),
            policy.version,
            0.95,
        ),
        budget=policy.snapshot(SearchMode.RESEARCH, NOW),
        usage=BudgetUsage(),
    )
    run.start(NOW)
    return run


def test_high_expected_gain_creates_only_new_revision_queries_and_steps() -> None:
    normalized = intent()
    initial = QueryCompiler().compile(normalized, SearchMode.RESEARCH)
    gap = normalized.facts[-1]
    gain = EvidenceGainEstimator().estimate(
        gap,
        assessment(gap, FactCoverage.OPEN),
        source_novelty=1,
        query_novelty=1,
        official_source_available=True,
    )

    decision = ExpansionPlanner().plan(
        normalized,
        current_revision=1,
        completed_expansion_rounds=0,
        gap_fact_keys=frozenset({gap.key}),
        gains=(gain,),
        existing_queries=initial,
    )

    assert decision.should_expand is True
    assert decision.plan_revision == 2
    assert decision.reason is ExpansionStopReason.EXPAND
    assert all(query.plan_revision == 2 for query in decision.queries)
    assert not ({query.signature for query in initial} & {query.signature for query in decision.queries})
    assert all(step.plan_revision == 2 for step in decision.steps)
    assert all(step.step_key.startswith("discover:q:2:") for step in decision.steps)


def test_low_expected_gain_and_max_rounds_stop_expansion() -> None:
    normalized = intent()
    gap = normalized.facts[-1]
    low_gain = EvidenceGainEstimator().estimate(
        gap,
        assessment(gap, FactCoverage.COVERED),
        source_novelty=0,
        query_novelty=0,
        official_source_available=False,
    )
    planner = ExpansionPlanner(minimum_expected_gain=0.4)

    low = planner.plan(
        normalized,
        current_revision=1,
        completed_expansion_rounds=0,
        gap_fact_keys=frozenset({gap.key}),
        gains=(low_gain,),
        existing_queries=(),
    )
    capped = planner.plan(
        normalized,
        current_revision=3,
        completed_expansion_rounds=2,
        gap_fact_keys=frozenset({gap.key}),
        gains=(low_gain,),
        existing_queries=(),
    )

    assert low.reason is ExpansionStopReason.LOW_EXPECTED_GAIN
    assert low.should_expand is False
    assert capped.reason is ExpansionStopReason.MAX_ROUNDS
    assert capped.should_expand is False


def test_research_preserves_synthesis_budget_and_caps_two_expansions() -> None:
    run = research_run()
    workflow = ResearchWorkflow()
    guard = BudgetGuard(run.budget)
    near_synthesis = FrozenClock(guard.non_synthesis_deadline)

    assert workflow.can_schedule(
        run,
        BudgetPhase.DISCOVERY,
        clock=near_synthesis,
        estimated_seconds=0.1,
    ) is False
    assert workflow.can_schedule(
        run,
        BudgetPhase.SYNTHESIZE,
        clock=near_synthesis,
        estimated_seconds=7,
    ) is True

    workflow.record_expansion(run)
    workflow.record_expansion(run)
    with pytest.raises(BudgetExceeded):
        workflow.record_expansion(run)
    assert run.usage.expansion_rounds == 2
    assert workflow.hard_deadline_reached(
        run,
        clock=FrozenClock(NOW + timedelta(seconds=120)),
    ) is True


def test_cancelled_research_run_cannot_schedule_more_external_work() -> None:
    run = research_run()
    run.cancel(NOW + timedelta(seconds=1))

    assert ResearchWorkflow.can_schedule(
        run,
        BudgetPhase.DISCOVERY,
        clock=FrozenClock(NOW + timedelta(seconds=1)),
    ) is False
