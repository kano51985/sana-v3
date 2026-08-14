from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sana.app.outbox_dispatcher import TenantOutboxPump
from sana.modules.shared.clock import FrozenClock


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeTenantSource:
    def __init__(self, tenant_ids) -> None:
        self.tenant_ids = tenant_ids

    async def active_tenant_ids(self):
        return self.tenant_ids


class FakeOutbox:
    def __init__(self, tenant_id) -> None:
        self.tenant_id = tenant_id

    async def claim_unpublished(self, now, limit):
        return []

    async def mark_published(self, message_id, published_at):
        raise AssertionError("empty outbox")

    async def mark_failed(self, message_id, error):
        raise AssertionError("empty outbox")


class FakeUnitOfWork:
    def __init__(self, tenant_id, commits) -> None:
        self.outbox = FakeOutbox(tenant_id)
        self._tenant_id = tenant_id
        self._commits = commits

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        self._commits.append(self._tenant_id)


@pytest.mark.asyncio
async def test_outbox_pump_enters_and_commits_each_tenant_scope() -> None:
    tenant_ids = (uuid4(), uuid4())
    commits = []
    factory = lambda tenant_id: FakeUnitOfWork(tenant_id, commits)
    pump = TenantOutboxPump(
        FakeTenantSource(tenant_ids),
        factory,
        SimpleNamespace(),
        FrozenClock(NOW),
    )

    cycle = await pump.run_once()

    assert cycle.tenants == 2
    assert cycle.published == cycle.failed == 0
    assert commits == list(tenant_ids)
