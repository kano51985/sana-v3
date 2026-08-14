from __future__ import annotations

from uuid import uuid4

import pytest

from sana.platform.db.uow import TenantUnitOfWork


class FakeTransaction:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeSession:
    def __init__(self) -> None:
        self.transaction = FakeTransaction()
        self.executed = []
        self.closed = False

    async def begin(self) -> FakeTransaction:
        return self.transaction

    async def execute(self, statement, parameters=None):
        self.executed.append((str(statement), parameters))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_uow_sets_transaction_local_tenant_and_commits() -> None:
    tenant_id = uuid4()
    session = FakeSession()
    uow = TenantUnitOfWork(lambda: session, tenant_id)

    async with uow:
        statement, parameters = session.executed[0]
        assert "set_config('app.tenant_id'" in statement
        assert parameters == {"tenant_id": str(tenant_id)}
        await uow.commit()

    assert session.transaction.committed
    assert not session.transaction.rolled_back
    assert session.closed


@pytest.mark.asyncio
async def test_uow_rolls_back_when_scope_exits_without_commit() -> None:
    session = FakeSession()
    uow = TenantUnitOfWork(lambda: session, uuid4())

    async with uow:
        pass

    assert session.transaction.rolled_back
    assert not session.transaction.committed
    assert session.closed


@pytest.mark.asyncio
async def test_uow_rolls_back_on_exception() -> None:
    session = FakeSession()
    uow = TenantUnitOfWork(lambda: session, uuid4())

    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            raise RuntimeError("boom")

    assert session.transaction.rolled_back
    assert session.closed
