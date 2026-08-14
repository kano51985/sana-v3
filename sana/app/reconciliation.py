"""PostgreSQL-backed workflow reconciliation and broker recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import exists, func, or_, select, update

from sana.modules.orchestration.domain import SearchMode, StepStatus
from sana.modules.orchestration.outbox import trace_context_to_dict
from sana.modules.orchestration.reconciler import (
    ReconcileAction,
    ReconcileCandidate,
    WorkflowReconciler,
)
from sana.modules.orchestration.repository import step_from_record
from sana.modules.shared.ids import TraceContext
from sana.platform.db.models.orchestration import (
    OutboxEvent,
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.queue.dispatcher import CeleryStepDispatcher, SearchQueue


class ActiveTenantSource(Protocol):
    async def active_tenant_ids(self) -> tuple[UUID, ...]: ...


@dataclass(frozen=True, slots=True)
class DispatchableCandidate:
    candidate: ReconcileCandidate
    queue: SearchQueue


@dataclass(frozen=True, slots=True)
class ReconciliationCycle:
    tenants: int
    recovered: int
    dispatched: int
    failed: int


class TenantReconciliationScanner:
    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        *,
        redelivery_grace_seconds: float = 5.0,
    ) -> None:
        if redelivery_grace_seconds < 0:
            raise ValueError("Redelivery grace cannot be negative")
        self._uow_factory = uow_factory
        self._redelivery_grace = timedelta(seconds=redelivery_grace_seconds)

    async def candidates(
        self,
        tenant_id: UUID,
        now: datetime,
        *,
        limit: int,
    ) -> tuple[DispatchableCandidate, ...]:
        lease_until = (
            select(func.max(StepAttemptRecord.leased_until))
            .where(
                StepAttemptRecord.tenant_id == tenant_id,
                StepAttemptRecord.step_id == SearchStepRecord.id,
                StepAttemptRecord.completed_at.is_(None),
            )
            .correlate(SearchStepRecord)
            .scalar_subquery()
        )
        has_unpublished_outbox = exists(
            select(OutboxEvent.id).where(
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.aggregate_id == SearchStepRecord.id,
                OutboxEvent.published_at.is_(None),
            )
        )
        async with self._uow_factory(tenant_id) as uow:
            statement = (
                select(
                    SearchStepRecord.id,
                    SearchStepRecord.status,
                    SearchStepRecord.retry_at,
                    SearchRunRecord.mode,
                    lease_until.label("leased_until"),
                )
                .join(
                    SearchRunRecord,
                    (SearchRunRecord.tenant_id == SearchStepRecord.tenant_id)
                    & (SearchRunRecord.id == SearchStepRecord.run_id),
                )
                .where(
                    SearchStepRecord.tenant_id == tenant_id,
                    SearchRunRecord.status.in_(("QUEUED", "RUNNING", "WAITING")),
                    ~has_unpublished_outbox,
                    or_(
                        (
                            (SearchStepRecord.status == StepStatus.READY.value)
                            & (
                                SearchStepRecord.updated_at
                                <= now - self._redelivery_grace
                            )
                        ),
                        (
                            (SearchStepRecord.status == StepStatus.RETRY_WAIT.value)
                            & (SearchStepRecord.retry_at <= now)
                        ),
                        (
                            (SearchStepRecord.status == StepStatus.RUNNING.value)
                            & (lease_until <= now)
                        ),
                    ),
                )
                .order_by(SearchStepRecord.updated_at, SearchStepRecord.id)
                .limit(limit)
            )
            rows = (await uow.session.execute(statement)).all()
        return tuple(
            DispatchableCandidate(
                ReconcileCandidate(
                    tenant_id=tenant_id,
                    step_id=row.id,
                    status=StepStatus(row.status),
                    retry_at=row.retry_at,
                    leased_until=row.leased_until,
                ),
                (
                    SearchQueue.RESEARCH
                    if SearchMode(row.mode) is SearchMode.RESEARCH
                    else SearchQueue.FAST
                ),
            )
            for row in rows
        )

    async def make_ready(
        self,
        candidate: ReconcileCandidate,
        now: datetime,
    ) -> bool:
        async with self._uow_factory(candidate.tenant_id) as uow:
            record = await uow.session.scalar(
                select(SearchStepRecord)
                .where(
                    SearchStepRecord.tenant_id == candidate.tenant_id,
                    SearchStepRecord.id == candidate.step_id,
                )
                .with_for_update()
            )
            if record is None:
                return False
            step = step_from_record(record)
            if (
                candidate.status is StepStatus.RETRY_WAIT
                and step.status is StepStatus.RETRY_WAIT
            ):
                if step.retry_at is None or step.retry_at > now:
                    return False
                step.make_ready()
            elif (
                candidate.status is StepStatus.RUNNING
                and step.status is StepStatus.RUNNING
            ):
                current_lease = await uow.session.scalar(
                    select(func.max(StepAttemptRecord.leased_until)).where(
                        StepAttemptRecord.tenant_id == candidate.tenant_id,
                        StepAttemptRecord.step_id == candidate.step_id,
                        StepAttemptRecord.completed_at.is_(None),
                    )
                )
                if current_lease is None or current_lease > now:
                    return False
                await uow.session.execute(
                    update(StepAttemptRecord)
                    .where(
                        StepAttemptRecord.tenant_id == candidate.tenant_id,
                        StepAttemptRecord.step_id == candidate.step_id,
                        StepAttemptRecord.completed_at.is_(None),
                        StepAttemptRecord.leased_until <= now,
                    )
                    .values(
                        completed_at=now,
                        error_type="TRANSIENT",
                        error_code="lease_expired",
                        error_details={
                            "category": "TRANSIENT",
                            "code": "lease_expired",
                            "message": "Worker lease expired before the attempt completed",
                            "retryable": True,
                            "details": {},
                        },
                    )
                )
                step.release_expired_lease()
            else:
                return step.status is StepStatus.READY
            await uow.steps.save(step)
            await uow.commit()
            return True

    async def mark_dispatched(
        self,
        candidate: ReconcileCandidate,
        now: datetime,
    ) -> None:
        """Start a new redelivery grace window without changing Step state."""

        async with self._uow_factory(candidate.tenant_id) as uow:
            await uow.session.execute(
                update(SearchStepRecord)
                .where(
                    SearchStepRecord.tenant_id == candidate.tenant_id,
                    SearchStepRecord.id == candidate.step_id,
                    SearchStepRecord.status == StepStatus.READY.value,
                )
                .values(updated_at=now)
            )
            await uow.commit()


class ReconciliationPump:
    def __init__(
        self,
        tenants: ActiveTenantSource,
        scanner: TenantReconciliationScanner,
        dispatcher: CeleryStepDispatcher,
        policy: WorkflowReconciler | None = None,
    ) -> None:
        self._tenants = tenants
        self._scanner = scanner
        self._dispatcher = dispatcher
        self._policy = policy or WorkflowReconciler()

    async def run_once(
        self,
        now: datetime,
        *,
        limit: int = 100,
    ) -> ReconciliationCycle:
        tenant_ids = await self._tenants.active_tenant_ids()
        recovered = 0
        dispatched = 0
        failed = 0
        for tenant_id in tenant_ids:
            items = await self._scanner.candidates(tenant_id, now, limit=limit)
            for item in items:
                decision = self._policy.decide(item.candidate, now)
                if decision is None:
                    continue
                if decision.action is not ReconcileAction.DISPATCH_READY:
                    if not await self._scanner.make_ready(item.candidate, now):
                        continue
                    recovered += 1
                try:
                    self._dispatcher.dispatch(
                        tenant_id,
                        item.candidate.step_id,
                        trace_context_to_dict(TraceContext.create()),
                        item.queue,
                    )
                except Exception:
                    failed += 1
                else:
                    dispatched += 1
                    try:
                        await self._scanner.mark_dispatched(item.candidate, now)
                    except Exception:
                        failed += 1
        return ReconciliationCycle(
            len(tenant_ids),
            recovered,
            dispatched,
            failed,
        )
