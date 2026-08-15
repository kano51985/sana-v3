"""Long-running tenant-aware transactional outbox dispatcher."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.app.settings import SanaSettings
from sana.app.reconciliation import ReconciliationPump, TenantReconciliationScanner
from sana.modules.shared.clock import Clock, SystemClock
from sana.platform.db.models.identity import Tenant
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.queue.celery_app import create_celery_app
from sana.platform.queue.dispatcher import CeleryStepDispatcher, OutboxDispatcher


logger = logging.getLogger(__name__)


class TenantSource(Protocol):
    async def active_tenant_ids(self) -> tuple[UUID, ...]: ...


class DatabaseTenantSource:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def active_tenant_ids(self) -> tuple[UUID, ...]:
        async with self._sessions() as session:
            statement = select(Tenant.id).where(Tenant.status == "ACTIVE").order_by(
                Tenant.id
            )
            return tuple((await session.scalars(statement)).all())


@dataclass(frozen=True, slots=True)
class DispatchCycle:
    tenants: int
    published: int
    failed: int


class TenantOutboxPump:
    def __init__(
        self,
        tenants: TenantSource,
        uow_factory: TenantUnitOfWorkFactory,
        step_dispatcher: CeleryStepDispatcher,
        clock: Clock,
    ) -> None:
        self._tenants = tenants
        self._uow_factory = uow_factory
        self._steps = step_dispatcher
        self._clock = clock

    async def run_once(self, *, batch_size: int = 100) -> DispatchCycle:
        tenant_ids = await self._tenants.active_tenant_ids()
        published = 0
        failed = 0
        for tenant_id in tenant_ids:
            async with self._uow_factory(tenant_id) as uow:
                dispatcher = OutboxDispatcher(uow.outbox, self._steps, self._clock)
                sent, errors = await dispatcher.dispatch_batch(limit=batch_size)
                await uow.commit()
                published += sent
                failed += errors
        return DispatchCycle(len(tenant_ids), published, failed)


async def run_dispatcher(*, once: bool = False) -> None:
    settings = SanaSettings()
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    tenants = DatabaseTenantSource(sessions)
    step_dispatcher = CeleryStepDispatcher(
        create_celery_app(settings.celery_broker_url)
    )
    pump = TenantOutboxPump(
        tenants,
        TenantUnitOfWorkFactory(sessions),
        step_dispatcher,
        SystemClock(),
    )
    reconciler = ReconciliationPump(
        tenants,
        TenantReconciliationScanner(
            TenantUnitOfWorkFactory(sessions),
            redelivery_grace_seconds=(
                settings.reconciliation_redelivery_grace_seconds
            ),
        ),
        step_dispatcher,
    )
    try:
        while True:
            recovery = await reconciler.run_once(
                SystemClock().now(),
                limit=settings.outbox_batch_size,
            )
            if recovery.failed:
                logger.warning(
                    "Reconciliation cycle contained broker failures",
                    extra={
                        "failed": recovery.failed,
                        "dispatched": recovery.dispatched,
                    },
                )
            if recovery.sealed_model_invocations:
                logger.warning(
                    "Reconciliation sealed orphaned model invocations",
                    extra={
                        "sealed_model_invocations": (
                            recovery.sealed_model_invocations
                        )
                    },
                )
            cycle = await pump.run_once(batch_size=settings.outbox_batch_size)
            if cycle.failed:
                logger.warning(
                    "Outbox cycle contained broker failures",
                    extra={"failed": cycle.failed, "published": cycle.published},
                )
            if once:
                return
            await asyncio.sleep(settings.outbox_poll_interval_seconds)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_dispatcher(once=args.once))


if __name__ == "__main__":
    main()
