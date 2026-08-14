"""Async transaction scope with PostgreSQL-local tenant context."""

from __future__ import annotations

from types import TracebackType
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.platform.db.repositories import (
    SqlAttemptRepository,
    SqlConversationRepository,
    SqlOutboxRepository,
    SqlResponseRunRepository,
    SqlRunRepository,
    SqlRunEventRepository,
    SqlStepRepository,
)


class TenantUnitOfWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self.tenant_id = tenant_id
        self._session: AsyncSession | None = None
        self._transaction = None
        self._finished = False

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Unit of work has not been entered")
        return self._session

    async def __aenter__(self) -> "TenantUnitOfWork":
        if self._session is not None:
            raise RuntimeError("Unit of work cannot be entered twice")
        self._session = self._session_factory()
        self._transaction = await self._session.begin()
        await self._session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(self.tenant_id)},
        )
        self.conversations = SqlConversationRepository(self._session, self.tenant_id)
        self.response_runs = SqlResponseRunRepository(self._session, self.tenant_id)
        self.runs = SqlRunRepository(self._session, self.tenant_id)
        self.steps = SqlStepRepository(self._session, self.tenant_id)
        self.outbox = SqlOutboxRepository(self._session, self.tenant_id)
        self.attempts = SqlAttemptRepository(self._session, self.tenant_id)
        self.events = SqlRunEventRepository(self._session, self.tenant_id)
        return self

    async def commit(self) -> None:
        if self._transaction is None or self._finished:
            raise RuntimeError("Unit of work has no active transaction")
        await self._transaction.commit()
        self._finished = True

    async def rollback(self) -> None:
        if self._transaction is None or self._finished:
            return
        await self._transaction.rollback()
        self._finished = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if not self._finished:
                await self.rollback()
        finally:
            if self._session is not None:
                await self._session.close()


class TenantUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self, tenant_id: UUID) -> TenantUnitOfWork:
        return TenantUnitOfWork(self._session_factory, tenant_id)
