from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import StepStatus
from sana.modules.orchestration.reconciler import (
    ReconcileAction,
    ReconcileCandidate,
    WorkflowReconciler,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, candidates) -> None:
        self._candidates = candidates
        self.readied = []
        self.enqueued = []

    async def candidates(self, now, limit):
        return self._candidates[:limit]

    async def make_ready(self, tenant_id, step_id) -> None:
        self.readied.append((tenant_id, step_id))

    async def enqueue(self, tenant_id, step_id) -> None:
        self.enqueued.append((tenant_id, step_id))


@pytest.mark.asyncio
async def test_reconciler_recovers_ready_retry_and_expired_lease_work() -> None:
    tenant = uuid4()
    ready = ReconcileCandidate(tenant, uuid4(), StepStatus.READY)
    retry = ReconcileCandidate(
        tenant,
        uuid4(),
        StepStatus.RETRY_WAIT,
        retry_at=NOW - timedelta(seconds=1),
    )
    expired = ReconcileCandidate(
        tenant,
        uuid4(),
        StepStatus.RUNNING,
        leased_until=NOW - timedelta(seconds=1),
    )
    future = ReconcileCandidate(
        tenant,
        uuid4(),
        StepStatus.RETRY_WAIT,
        retry_at=NOW + timedelta(seconds=30),
    )
    store = FakeStore([ready, retry, expired, future])

    decisions = await WorkflowReconciler().run(store, NOW)

    assert [decision.action for decision in decisions] == [
        ReconcileAction.DISPATCH_READY,
        ReconcileAction.RELEASE_RETRY,
        ReconcileAction.RELEASE_EXPIRED_LEASE,
    ]
    assert store.readied == [
        (tenant, retry.step_id),
        (tenant, expired.step_id),
    ]
    assert store.enqueued == [
        (tenant, ready.step_id),
        (tenant, retry.step_id),
        (tenant, expired.step_id),
    ]


def test_completed_steps_are_never_reconciled() -> None:
    candidate = ReconcileCandidate(uuid4(), uuid4(), StepStatus.SUCCEEDED)
    assert WorkflowReconciler().decide(candidate, NOW) is None
