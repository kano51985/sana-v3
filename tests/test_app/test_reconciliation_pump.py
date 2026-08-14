from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.app.reconciliation import DispatchableCandidate, ReconciliationPump
from sana.modules.orchestration.domain import StepStatus
from sana.modules.orchestration.reconciler import ReconcileCandidate
from sana.platform.queue.dispatcher import SearchQueue


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeTenants:
    def __init__(self, tenant_id) -> None:
        self.tenant_id = tenant_id

    async def active_tenant_ids(self):
        return (self.tenant_id,)


class FakeScanner:
    def __init__(self, items) -> None:
        self.items = items
        self.released = []
        self.marked = []

    async def candidates(self, tenant_id, now, *, limit):
        return self.items

    async def make_ready(self, candidate, now):
        self.released.append(candidate.step_id)
        return True

    async def mark_dispatched(self, candidate, now):
        self.marked.append((candidate.step_id, now))


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls = []

    def dispatch(self, tenant_id, step_id, trace, queue):
        self.calls.append((tenant_id, step_id, trace, queue))


@pytest.mark.asyncio
async def test_reconciliation_releases_expired_lease_before_redis_redelivery() -> None:
    tenant_id = uuid4()
    step_id = uuid4()
    candidate = ReconcileCandidate(
        tenant_id,
        step_id,
        StepStatus.RUNNING,
        leased_until=NOW - timedelta(seconds=1),
    )
    scanner = FakeScanner((DispatchableCandidate(candidate, SearchQueue.RESEARCH),))
    dispatcher = FakeDispatcher()
    pump = ReconciliationPump(FakeTenants(tenant_id), scanner, dispatcher)

    cycle = await pump.run_once(NOW)

    assert cycle.recovered == cycle.dispatched == 1
    assert cycle.failed == 0
    assert scanner.released == [step_id]
    assert scanner.marked == [(step_id, NOW)]
    assert dispatcher.calls[0][0:2] == (tenant_id, step_id)
    assert dispatcher.calls[0][3] is SearchQueue.RESEARCH


@pytest.mark.asyncio
async def test_ready_step_is_redis_redelivered_without_state_rewrite() -> None:
    tenant_id = uuid4()
    step_id = uuid4()
    candidate = ReconcileCandidate(tenant_id, step_id, StepStatus.READY)
    scanner = FakeScanner((DispatchableCandidate(candidate, SearchQueue.FAST),))
    dispatcher = FakeDispatcher()

    cycle = await ReconciliationPump(
        FakeTenants(tenant_id), scanner, dispatcher
    ).run_once(NOW)

    assert cycle.recovered == 0
    assert cycle.dispatched == 1
    assert scanner.released == []
    assert scanner.marked == [(step_id, NOW)]
