from datetime import datetime, timezone
from uuid import uuid4

from sana.modules.evidence.coverage import FactCoverage
from sana.modules.orchestration.domain import (
    AnswerQuality,
    RoutingDecision,
    RunStatus,
    SearchMode,
    SearchRun,
    StopReason,
    StepStatus,
)
from sana.modules.orchestration.policy import BudgetGuard, SearchPolicy
from sana.modules.orchestration.search_workflow import FastSearchGraph, FastSearchWorkflow
from sana.modules.shared.clock import FrozenClock


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def run() -> SearchRun:
    policy = SearchPolicy.default()
    search_run = SearchRun(
        id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        message_id=uuid4(),
        response_run_id=uuid4(),
        routing=RoutingDecision(
            SearchMode.FAST,
            ("single_or_low_complexity_fact",),
            policy.version,
            0.9,
        ),
        budget=policy.snapshot(SearchMode.FAST, NOW),
    )
    search_run.start(NOW)
    return search_run


def test_ordinary_fast_gap_returns_partial_without_automatic_upgrade() -> None:
    outcome = FastSearchWorkflow(FastSearchGraph()).outcome(
        (FactCoverage.COVERED, FactCoverage.OPEN),
        deadline_fallback=False,
    )

    assert outcome.quality is AnswerQuality.PARTIAL
    assert outcome.reason is StopReason.INSUFFICIENT_EVIDENCE
    assert outcome.request_research_upgrade is False


def test_partial_outcome_finishes_fast_run_without_extending_deadline() -> None:
    search_run = run()
    workflow = FastSearchWorkflow(FastSearchGraph())
    outcome = workflow.outcome((FactCoverage.OPEN,), deadline_fallback=True)

    workflow.finish(search_run, outcome, clock=FrozenClock(NOW))

    assert search_run.status is RunStatus.SUCCEEDED
    assert search_run.answer_quality is AnswerQuality.PARTIAL
    assert search_run.stop_reason is StopReason.TIME_BUDGET
    assert search_run.mode is SearchMode.FAST


def test_all_required_facts_covered_returns_complete() -> None:
    outcome = FastSearchWorkflow(FastSearchGraph()).outcome(
        (FactCoverage.COVERED, FactCoverage.VERIFIED),
        deadline_fallback=False,
    )

    assert outcome.quality is AnswerQuality.COMPLETE
    assert outcome.reason is StopReason.FACTS_COVERED


def test_failed_fetch_is_skipped_forward_to_partial_synthesis() -> None:
    workflow_graph = FastSearchGraph()
    workflow_graph.add_discovery("query")
    workflow_graph.seal_discovery()
    workflow_graph.add_fetch_pipeline("source")
    workflow_graph.seal_synthesis()
    guard = BudgetGuard(SearchPolicy.default().snapshot(SearchMode.FAST, NOW))
    clock = FrozenClock(NOW)
    statuses = {
        "route": StepStatus.SUCCEEDED,
        "plan": StepStatus.SUCCEEDED,
        "discover:query": StepStatus.SUCCEEDED,
        "select": StepStatus.SUCCEEDED,
        "fetch:source": StepStatus.FAILED,
    }

    after_fetch = workflow_graph.advance(statuses, guard=guard, clock=clock)
    assert after_fetch.skip == ("extract:source",)

    statuses["extract:source"] = StepStatus.SKIPPED
    after_extract = workflow_graph.advance(statuses, guard=guard, clock=clock)
    assert after_extract.skip == ("verify:source",)

    statuses["verify:source"] = StepStatus.SKIPPED
    after_verify = workflow_graph.advance(statuses, guard=guard, clock=clock)
    assert tuple(node.key for node in after_verify.submit) == ("synthesize",)


def test_select_runs_after_discovery_failures_to_preserve_partial_results() -> None:
    workflow_graph = FastSearchGraph()
    workflow_graph.add_discovery("failed")
    workflow_graph.add_discovery("succeeded")
    workflow_graph.seal_discovery()
    workflow_graph.seal_synthesis()
    guard = BudgetGuard(SearchPolicy.default().snapshot(SearchMode.FAST, NOW))

    advance = workflow_graph.advance(
        {
            "route": StepStatus.SUCCEEDED,
            "plan": StepStatus.SUCCEEDED,
            "discover:failed": StepStatus.FAILED,
            "discover:succeeded": StepStatus.SUCCEEDED,
        },
        guard=guard,
        clock=FrozenClock(NOW),
    )

    assert tuple(node.key for node in advance.submit) == ("select",)
