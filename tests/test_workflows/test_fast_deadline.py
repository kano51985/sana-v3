import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import (
    ArtifactRef,
    BudgetUsage,
    SearchMode,
    StepStatus,
    StepType,
)
from sana.modules.orchestration.policy import BudgetGuard, SearchPolicy
from sana.modules.orchestration.search_workflow import (
    BudgetReservationLedger,
    BudgetReservation,
    FastSearchGraph,
    FastSearchWorkflow,
)
from sana.modules.orchestration.step_handlers import (
    BoundedStepHandler,
    StepExecutionContext,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class CancelledRun:
    async def is_cancelled(self, tenant_id, run_id) -> bool:
        return True


def graph() -> FastSearchGraph:
    workflow_graph = FastSearchGraph()
    workflow_graph.add_discovery("slow-query", estimated_seconds=2)
    workflow_graph.seal_discovery()
    workflow_graph.add_fetch_pipeline("slow-source", fetch_seconds=2)
    workflow_graph.seal_synthesis()
    return workflow_graph


def test_deadline_cancels_low_value_work_and_preserves_synthesis_reservation() -> None:
    workflow_graph = graph()
    snapshot = SearchPolicy.default().snapshot(SearchMode.FAST, NOW)
    guard = BudgetGuard(snapshot)
    clock = FrozenClock(guard.non_synthesis_deadline)
    discovery = workflow_graph.nodes["discover:slow-query"]
    ledger = BudgetReservationLedger(
        guard,
        BudgetUsage(),
        pending=(
            BudgetReservation(discovery.key, discovery.budget),
        ),
    )
    statuses = {
        "route": StepStatus.SUCCEEDED,
        "plan": StepStatus.SUCCEEDED,
        "discover:slow-query": StepStatus.READY,
    }

    advance = workflow_graph.advance(statuses, guard=guard, clock=clock)
    submitted = FastSearchWorkflow(workflow_graph).reserve_submissions(
        advance,
        ledger,
        clock=clock,
    )

    assert advance.deadline_fallback is True
    assert "discover:slow-query" in advance.cancel
    assert tuple(node.key for node in submitted) == ("synthesize",)
    assert tuple(item.step_key for item in ledger.reservations) == ("synthesize",)


async def test_worker_checks_cancellation_before_external_operation() -> None:
    called = False

    async def operation(context):
        nonlocal called
        called = True
        raise AssertionError("cancelled operation must not run")

    context = StepExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        step_id=uuid4(),
        step_key="fetch:cancelled",
        step_type=StepType.FETCH,
        attempt_id=uuid4(),
        attempt_no=1,
        trace_context=TraceContext.create(),
        deadline_at=NOW + timedelta(seconds=5),
        input_ref=ArtifactRef(
            "memory://input",
            hashlib.sha256(b"input").hexdigest(),
        ),
        cancellation=CancelledRun(),
        clock=FrozenClock(NOW),
    )

    with pytest.raises(TypedError) as captured:
        await BoundedStepHandler(operation).handle(context)

    assert captured.value.category is ErrorCategory.CANCELLED
    assert called is False
