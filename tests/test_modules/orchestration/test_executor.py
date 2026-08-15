import hashlib
from datetime import datetime, timedelta, timezone
import asyncio
from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import ArtifactRef, StepAttempt, StepType
from sana.modules.orchestration.executor import (
    ClaimedStep,
    ClaimResult,
    DurableStepExecutor,
    ExecutionDisposition,
)
from sana.modules.orchestration.search_workflow import StepBudgetCost
from sana.modules.orchestration.policy import BudgetPhase
from sana.modules.orchestration.step_handlers import (
    BoundedStepHandler,
    StepExecutionContext,
    StepExecutionResult,
    StepHandlerRegistry,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class NotCancelled:
    async def is_cancelled(self, tenant_id, run_id):
        return False


class FakeStore:
    def __init__(self, claim) -> None:
        self.claim_result = claim
        self.successes = []
        self.failures = []
        self.renewals = 0
        self.renew_result = True

    async def claim(self, tenant_id, step_id, worker_id, trace_context):
        return self.claim_result

    async def succeed(self, claimed, result, trace_context):
        self.successes.append((claimed, result))
        return ExecutionDisposition.SUCCEEDED

    async def renew(self, claimed):
        self.renewals += 1
        return self.renew_result

    async def fail(self, claimed, error, trace_context):
        self.failures.append((claimed, error))
        if error.category is ErrorCategory.CANCELLED:
            return ExecutionDisposition.CANCELLED
        if error.retryable:
            return ExecutionDisposition.RETRY_SCHEDULED
        return ExecutionDisposition.FAILED


def claimed_step(step_type=StepType.FETCH):
    context = StepExecutionContext(
        uuid4(),
        uuid4(),
        uuid4(),
        "fetch:official",
        step_type,
        uuid4(),
        1,
        TraceContext.create(),
        NOW + timedelta(seconds=10),
        ArtifactRef("artifact://input", "a" * 64),
        NotCancelled(),
        FrozenClock(NOW),
    )
    attempt = StepAttempt(
        uuid4(),
        context.tenant_id,
        context.run_id,
        context.step_id,
        1,
        f"{context.step_id}:1",
        "worker-1",
        NOW + timedelta(seconds=5),
        context.deadline_at,
        NOW,
        context.input_ref,
    )
    return ClaimedStep(attempt, context)


@pytest.mark.asyncio
async def test_duplicate_delivery_is_ignored_before_operation_runs() -> None:
    store = FakeStore(ClaimResult.skipped(ExecutionDisposition.IGNORED))
    registry = StepHandlerRegistry()
    executor = DurableStepExecutor(store, registry, worker_id="worker-1")

    result = await executor(uuid4(), uuid4(), TraceContext.create())

    assert result == "IGNORED"
    assert not store.successes and not store.failures


@pytest.mark.asyncio
async def test_claimed_step_executes_one_registered_operation_and_finalizes() -> None:
    claimed = claimed_step()
    output = ArtifactRef(
        "artifact://output",
        hashlib.sha256(b"output").hexdigest(),
    )

    async def operation(context):
        assert context is claimed.context
        return StepExecutionResult(
            output,
            StepBudgetCost(BudgetPhase.FETCH_EXTRACT, fetches=1),
        )

    registry = StepHandlerRegistry()
    registry.register(StepType.FETCH, BoundedStepHandler(operation))
    store = FakeStore(ClaimResult.acquired(claimed))

    result = await DurableStepExecutor(
        store, registry, worker_id="worker-1"
    )(uuid4(), uuid4(), TraceContext.create())

    assert result == "SUCCEEDED"
    assert store.successes[0][1].output_ref == output


@pytest.mark.asyncio
async def test_typed_transient_failure_is_handed_to_durable_retry_policy() -> None:
    claimed = claimed_step()

    async def operation(context):
        raise TypedError(
            ErrorCategory.TRANSIENT,
            "provider_timeout",
            "provider timed out",
        )

    registry = StepHandlerRegistry()
    registry.register(StepType.FETCH, BoundedStepHandler(operation))
    store = FakeStore(ClaimResult.acquired(claimed))

    result = await DurableStepExecutor(
        store, registry, worker_id="worker-1"
    )(uuid4(), uuid4(), TraceContext.create())

    assert result == "RETRY_SCHEDULED"
    assert store.failures[0][1].code == "provider_timeout"


@pytest.mark.asyncio
async def test_unexpected_operation_error_is_sanitized_before_persistence() -> None:
    claimed = claimed_step()

    async def operation(context):
        raise RuntimeError("secret provider detail")

    registry = StepHandlerRegistry()
    registry.register(StepType.FETCH, BoundedStepHandler(operation))
    store = FakeStore(ClaimResult.acquired(claimed))

    result = await DurableStepExecutor(
        store, registry, worker_id="worker-1"
    )(uuid4(), uuid4(), TraceContext.create())

    assert result == "FAILED"
    persisted = store.failures[0][1]
    assert persisted.code == "step_operation_failed"
    assert "secret" not in persisted.message


@pytest.mark.asyncio
async def test_lost_lease_cancels_slow_operation_before_stale_finalize() -> None:
    claimed = claimed_step()
    cancelled = asyncio.Event()

    async def operation(context):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    registry = StepHandlerRegistry()
    registry.register(StepType.FETCH, BoundedStepHandler(operation))
    store = FakeStore(ClaimResult.acquired(claimed))
    store.renew_result = False

    result = await DurableStepExecutor(
        store,
        registry,
        worker_id="worker-1",
        heartbeat_seconds=0.001,
    )(uuid4(), uuid4(), TraceContext.create())

    assert result == "CANCELLED"
    assert cancelled.is_set()
    assert store.failures[0][1].code == "step_lease_lost"
