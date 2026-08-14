from datetime import datetime, timezone

import pytest

from sana.modules.orchestration.domain import BudgetUsage, SearchMode, StepStatus
from sana.modules.orchestration.policy import BudgetExceeded, BudgetGuard, SearchPolicy
from sana.modules.orchestration.search_workflow import (
    BudgetReservationLedger,
    FastSearchGraph,
    FastSearchWorkflow,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.orchestration.step_handlers import (
    FastStepOperations,
    build_fast_handler_registry,
)
from sana.modules.orchestration.domain import StepType


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def graph() -> FastSearchGraph:
    workflow_graph = FastSearchGraph()
    workflow_graph.add_discovery("release-notes")
    workflow_graph.add_discovery("official-status")
    workflow_graph.seal_discovery()
    workflow_graph.add_fetch_pipeline("official")
    workflow_graph.add_fetch_pipeline("publisher")
    workflow_graph.seal_synthesis()
    return workflow_graph


def test_fast_graph_exposes_parallel_work_only_to_the_scheduler() -> None:
    workflow_graph = graph()
    guard = BudgetGuard(SearchPolicy.default().snapshot(SearchMode.FAST, NOW))
    clock = FrozenClock(NOW)

    first = workflow_graph.advance({}, guard=guard, clock=clock)
    assert tuple(node.key for node in first.submit) == ("route",)

    after_route = workflow_graph.advance(
        {"route": StepStatus.SUCCEEDED},
        guard=guard,
        clock=clock,
    )
    assert tuple(node.key for node in after_route.submit) == ("plan",)

    after_plan = workflow_graph.advance(
        {
            "route": StepStatus.SUCCEEDED,
            "plan": StepStatus.SUCCEEDED,
        },
        guard=guard,
        clock=clock,
    )
    assert {node.key for node in after_plan.submit} == {
        "discover:release-notes",
        "discover:official-status",
    }
    assert all(node.dependencies == ("plan",) for node in after_plan.submit)


def test_budget_is_reserved_before_a_parallel_batch_is_returned_for_submission() -> None:
    workflow_graph = graph()
    snapshot = SearchPolicy.default().snapshot(SearchMode.FAST, NOW)
    guard = BudgetGuard(snapshot)
    clock = FrozenClock(NOW)
    ledger = BudgetReservationLedger(guard, BudgetUsage())
    workflow = FastSearchWorkflow(workflow_graph)
    advance = workflow_graph.advance(
        {"route": StepStatus.SUCCEEDED, "plan": StepStatus.SUCCEEDED},
        guard=guard,
        clock=clock,
    )

    submitted = workflow.reserve_submissions(advance, ledger, clock=clock)

    assert len(submitted) == 2
    assert ledger.projected_usage.query_count == 2
    assert ledger.projected_usage.provider_count == 2
    assert {item.step_key for item in ledger.reservations} == {
        "discover:release-notes",
        "discover:official-status",
    }


def test_actual_usage_replaces_reservation_and_returns_unused_time() -> None:
    workflow_graph = graph()
    guard = BudgetGuard(SearchPolicy.default().snapshot(SearchMode.FAST, NOW))
    ledger = BudgetReservationLedger(guard, BudgetUsage())
    node = workflow_graph.nodes["discover:release-notes"]
    ledger.reserve(node.key, node.budget, now=NOW)

    usage = ledger.complete(
        node.key,
        node.budget,
        elapsed_seconds=0.25,
    )

    assert ledger.reservations == ()
    assert usage.query_count == 1
    assert usage.provider_count == 1
    assert usage.phase_seconds["discovery"] == 0.25


def test_dynamic_graph_snapshot_round_trips_for_worker_recovery() -> None:
    original = graph()

    recovered = FastSearchGraph.from_dict(original.to_dict())

    assert recovered.to_dict() == original.to_dict()
    assert tuple(recovered.nodes) == tuple(original.nodes)


def test_parallel_reservation_is_atomic_when_batch_exceeds_budget() -> None:
    workflow_graph = FastSearchGraph()
    for index in range(3):
        workflow_graph.add_discovery(f"query-{index}")
    workflow_graph.seal_discovery()
    workflow_graph.seal_synthesis()
    guard = BudgetGuard(SearchPolicy.default().snapshot(SearchMode.FAST, NOW))
    ledger = BudgetReservationLedger(guard, BudgetUsage())
    advance = workflow_graph.advance(
        {"route": StepStatus.SUCCEEDED, "plan": StepStatus.SUCCEEDED},
        guard=guard,
        clock=FrozenClock(NOW),
    )

    with pytest.raises(BudgetExceeded):
        FastSearchWorkflow(workflow_graph).reserve_submissions(
            advance,
            ledger,
            clock=FrozenClock(NOW),
        )

    assert ledger.reservations == ()


def test_every_fast_step_type_has_an_explicit_worker_handler() -> None:
    async def unused_operation(context):
        raise AssertionError("registry construction must not execute operations")

    operations = FastStepOperations(*([unused_operation] * 8))

    registry = build_fast_handler_registry(operations)

    for step_type in (
        StepType.ROUTE,
        StepType.PLAN,
        StepType.DISCOVERY,
        StepType.SELECT,
        StepType.FETCH,
        StepType.EXTRACT,
        StepType.VERIFY,
        StepType.SYNTHESIZE,
    ):
        assert registry.resolve(step_type) is not None
