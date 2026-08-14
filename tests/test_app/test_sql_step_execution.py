from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.app.sql_step_execution import SqlStepExecutionStore
from sana.modules.orchestration.domain import (
    ArtifactRef,
    RoutingDecision,
    RunStatus,
    SearchMode,
    SearchRun,
    SearchStep,
    StepStatus,
    StepType,
    StopReason,
)
from sana.modules.orchestration.executor import ExecutionDisposition
from sana.modules.orchestration.lease import LeaseService
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeRuns:
    def __init__(self, run) -> None:
        self.run = run
        self.saved = []

    async def get(self, tenant_id, run_id):
        return self.run if self.run.id == run_id else None

    async def get_for_update(self, tenant_id, run_id):
        return await self.get(tenant_id, run_id)

    async def save(self, run):
        self.saved.append(run.status)
        run.mark_persisted()


class FakeSteps:
    def __init__(self, step) -> None:
        self.step = step
        self.saved = []

    async def get_for_update(self, tenant_id, step_id):
        return self.step if self.step.id == step_id else None

    async def save(self, step):
        self.saved.append(step.status)
        step.mark_persisted()


class FakeAttempts:
    def __init__(self) -> None:
        self.added = []

    async def next_attempt_no(self, tenant_id, step_id):
        return len(self.added) + 1

    async def add(self, attempt):
        self.added.append(attempt)


class FakeEvents:
    def __init__(self) -> None:
        self.items = []

    async def next_sequence(self, tenant_id, run_id):
        return len(self.items) + 1

    async def add(self, event):
        self.items.append(event)


class FakeUow:
    def __init__(self, run, step) -> None:
        self.runs = FakeRuns(run)
        self.steps = FakeSteps(step)
        self.attempts = FakeAttempts()
        self.events = FakeEvents()
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        self.committed = True


class UnusedCompletion:
    async def on_success(self, *args):
        raise AssertionError("claim must not complete")

    async def on_failure(self, *args):
        raise AssertionError("claim must not complete")


class FakeMirror:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def run_and_step():
    tenant_id = uuid4()
    run = SearchRun(
        uuid4(),
        tenant_id,
        uuid4(),
        uuid4(),
        uuid4(),
        RoutingDecision(SearchMode.FAST, ("test",), "search-v1", 1.0),
        SearchPolicy.default().snapshot(SearchMode.FAST, NOW),
    )
    step = SearchStep(
        uuid4(),
        tenant_id,
        run.id,
        "route",
        StepType.ROUTE,
        1,
        ArtifactRef("db://message/1", "a" * 64),
    )
    return run, step


def store(uow, clock, mirror=None):
    return SqlStepExecutionStore(
        lambda tenant_id: uow,
        LeaseService(DeterministicIdFactory("lease")),
        UnusedCompletion(),
        clock,
        DeterministicIdFactory("events"),
        event_mirror=mirror,
    )


@pytest.mark.asyncio
async def test_sql_store_claim_starts_run_leases_step_and_commits_event() -> None:
    run, step = run_and_step()
    uow = FakeUow(run, step)
    mirror = FakeMirror()

    result = await store(uow, FrozenClock(NOW), mirror).claim(
        run.tenant_id,
        step.id,
        "worker-1",
        TraceContext.create(),
    )

    assert result.claimed is not None
    assert run.status is RunStatus.RUNNING
    assert step.status is StepStatus.RUNNING
    assert len(uow.attempts.added) == 1
    assert uow.events.items[0].event_type == "STEP_STARTED"
    assert mirror.events == uow.events.items
    assert uow.committed is True


@pytest.mark.asyncio
async def test_sql_store_rejects_deadline_exhausted_step_before_operation() -> None:
    run, step = run_and_step()
    uow = FakeUow(run, step)
    after_deadline = run.budget.hard_deadline_at + timedelta(seconds=1)

    result = await store(uow, FrozenClock(after_deadline)).claim(
        run.tenant_id,
        step.id,
        "worker-1",
        TraceContext.create(),
    )

    assert result.disposition is ExecutionDisposition.FAILED
    assert step.status is StepStatus.FAILED
    assert run.status is RunStatus.FAILED
    assert run.stop_reason is StopReason.TIME_BUDGET
    assert not uow.attempts.added
