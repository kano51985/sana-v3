"""PostgreSQL Step lease/finalization adapter for DurableStepExecutor."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Protocol
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy import func, select

from sana.modules.orchestration.domain import (
    RunStatus,
    SearchRun,
    SearchStep,
    StepStatus,
    StepType,
    StopReason,
)
from sana.modules.orchestration.events import RunEventData
from sana.modules.orchestration.executor import (
    ClaimedStep,
    ClaimResult,
    ExecutionDisposition,
)
from sana.modules.orchestration.lease import LeaseService
from sana.modules.orchestration.policy import BudgetGuard, BudgetPhase
from sana.modules.orchestration.step_handlers import (
    StepExecutionContext,
    StepExecutionResult,
)
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import IdFactory, TraceContext
from sana.platform.db.models.orchestration import StepAttemptRecord
from sana.platform.db.uow import TenantUnitOfWork, TenantUnitOfWorkFactory
from sana.platform.events.redis_stream import RedisEventStream, StreamEvent


logger = logging.getLogger(__name__)


class StepCompletionHook(Protocol):
    """Mutate aggregates/add successor records; never commit or save base records."""

    async def on_success(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        result: StepExecutionResult,
        trace_context: TraceContext,
    ) -> None: ...

    async def on_failure(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        error: TypedError,
        disposition: ExecutionDisposition,
        trace_context: TraceContext,
    ) -> None: ...


class RedisEventMirror:
    def __init__(self, stream: RedisEventStream) -> None:
        self._stream = stream

    async def publish(self, event: RunEventData) -> None:
        await self._stream.publish(
            event.tenant_id,
            event.run_id,
            StreamEvent(
                sequence=event.sequence,
                event_type=event.event_type,
                payload=dict(event.payload),
                created_at=event.created_at,
            ),
        )


class SqlStepExecutionStore:
    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        lease_service: LeaseService,
        completion_hook: StepCompletionHook,
        clock: Clock,
        id_factory: IdFactory,
        *,
        event_mirror: RedisEventMirror | None = None,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 10.0,
        max_attempts: int = 3,
        fetch_max_attempts: int = 2,
        lease_extension_seconds: float = 30.0,
    ) -> None:
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Retry delays are invalid")
        if max_attempts < 1 or not 1 <= fetch_max_attempts <= max_attempts:
            raise ValueError("Retry attempt limits are invalid")
        if lease_extension_seconds <= 0:
            raise ValueError("Lease extension must be positive")
        self._uow_factory = uow_factory
        self._leases = lease_service
        self._completion = completion_hook
        self._clock = clock
        self._ids = id_factory
        self._mirror = event_mirror
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._max_attempts = max_attempts
        self._fetch_max_attempts = fetch_max_attempts
        self._lease_extension_seconds = lease_extension_seconds

    def _attempt_limit(self, step_type: StepType) -> int:
        # A failed source has sibling fallbacks, so one network retry is enough.
        # Other steps retain one additional retry for transient infrastructure.
        return (
            self._fetch_max_attempts
            if step_type is StepType.FETCH
            else self._max_attempts
        )

    async def _add_event(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        event_type: str,
        payload: dict,
    ) -> RunEventData:
        event = RunEventData(
            id=self._ids.new_uuid(),
            tenant_id=run.tenant_id,
            run_id=run.id,
            sequence=await uow.events.next_sequence(run.tenant_id, run.id),
            event_type=event_type,
            payload=payload,
            created_at=self._clock.now(),
        )
        await uow.events.add(event)
        return event

    async def _publish(self, event: RunEventData | None) -> None:
        if event is None or self._mirror is None:
            return
        try:
            await self._mirror.publish(event)
        except RedisError:
            logger.warning(
                "Redis event mirror unavailable; PostgreSQL event remains authoritative",
                extra={"run_id": str(event.run_id), "sequence": event.sequence},
            )

    async def is_cancelled(self, tenant_id: UUID, run_id: UUID) -> bool:
        async with self._uow_factory(tenant_id) as uow:
            run = await uow.runs.get(tenant_id, run_id)
            return run is None or run.is_terminal

    @staticmethod
    def _phase_for_step(step_type: StepType) -> BudgetPhase:
        if step_type in {StepType.ROUTE, StepType.PLAN}:
            return BudgetPhase.ROUTE_PLAN
        if step_type in {StepType.DISCOVERY, StepType.SELECT}:
            return BudgetPhase.DISCOVERY
        if step_type in {StepType.FETCH, StepType.EXTRACT}:
            return BudgetPhase.FETCH_EXTRACT
        if step_type is StepType.VERIFY:
            return BudgetPhase.VERIFY
        return BudgetPhase.SYNTHESIZE

    async def claim(
        self,
        tenant_id: UUID,
        step_id: UUID,
        worker_id: str,
        trace_context: TraceContext,
    ) -> ClaimResult:
        event: RunEventData | None = None
        now = self._clock.now()
        async with self._uow_factory(tenant_id) as uow:
            step = await uow.steps.get_for_update(tenant_id, step_id)
            if step is None or step.status is not StepStatus.READY:
                return ClaimResult.skipped(ExecutionDisposition.IGNORED)
            run = await uow.runs.get_for_update(tenant_id, step.run_id)
            if run is None:
                return ClaimResult.skipped(ExecutionDisposition.STALE)
            if run.is_terminal:
                step.cancel()
                await uow.steps.save(step)
                await uow.commit()
                return ClaimResult.skipped(ExecutionDisposition.CANCELLED)
            if now >= run.budget.hard_deadline_at:
                step.start()
                step.fail()
                run.fail(StopReason.TIME_BUDGET, now)
                await uow.steps.save(step)
                await uow.runs.save(run)
                event = await self._add_event(
                    uow,
                    run,
                    "STEP_DEADLINE_EXCEEDED",
                    {"step_id": str(step.id), "step_key": step.step_key},
                )
                await uow.commit()
                disposition = ClaimResult.skipped(ExecutionDisposition.FAILED)
            else:
                execution_deadline = BudgetGuard(run.budget).deadline_for(
                    self._phase_for_step(step.step_type)
                )
                if now >= execution_deadline:
                    if run.status is RunStatus.QUEUED:
                        run.start(now)
                    elif run.status is RunStatus.WAITING:
                        run.resume()
                    step.start()
                    step.fail()
                    error = TypedError(
                        ErrorCategory.BUDGET,
                        "phase_deadline_exceeded",
                        "Step phase deadline was exhausted before admission",
                        retryable=False,
                    )
                    await self._completion.on_failure(
                        uow,
                        run,
                        step,
                        error,
                        ExecutionDisposition.FAILED,
                        trace_context,
                    )
                    await uow.steps.save(step)
                    await uow.runs.save(run)
                    event = await self._add_event(
                        uow,
                        run,
                        "STEP_PHASE_DEADLINE_EXCEEDED",
                        {
                            "step_id": str(step.id),
                            "step_key": step.step_key,
                            "phase": self._phase_for_step(step.step_type).value,
                        },
                    )
                    await uow.commit()
                    disposition = ClaimResult.skipped(ExecutionDisposition.FAILED)
                    # Skip lease creation and never enter the external operation.
                    execution_deadline = None
                if execution_deadline is None:
                    pass
                else:
                    if run.status is RunStatus.QUEUED:
                        run.start(now)
                    elif run.status is RunStatus.WAITING:
                        run.resume()
                    attempt = self._leases.claim(
                        step,
                        attempt_no=await uow.attempts.next_attempt_no(
                            tenant_id,
                            step.id,
                        ),
                        worker_id=worker_id,
                        now=now,
                        deadline_at=execution_deadline,
                    )
                    await uow.attempts.add(attempt)
                    await uow.steps.save(step)
                    await uow.runs.save(run)
                    event = await self._add_event(
                        uow,
                        run,
                        "STEP_STARTED",
                        {
                            "step_id": str(step.id),
                            "step_key": step.step_key,
                            "step_type": step.step_type.value,
                            "attempt_no": attempt.attempt_no,
                        },
                    )
                    await uow.commit()
                    disposition = ClaimResult.acquired(
                        ClaimedStep(
                            attempt,
                            StepExecutionContext(
                                tenant_id=tenant_id,
                                run_id=step.run_id,
                                step_id=step.id,
                                step_key=step.step_key,
                                step_type=step.step_type,
                                attempt_id=attempt.id,
                                attempt_no=attempt.attempt_no,
                                trace_context=trace_context,
                                deadline_at=execution_deadline,
                                input_ref=step.input_ref,
                                cancellation=self,
                                clock=self._clock,
                            ),
                        )
                    )
        await self._publish(event)
        return disposition

    async def _is_current_attempt(
        self,
        uow: TenantUnitOfWork,
        claimed: ClaimedStep,
    ) -> bool:
        record = await uow.session.scalar(
            select(StepAttemptRecord)
            .where(
                StepAttemptRecord.tenant_id == claimed.context.tenant_id,
                StepAttemptRecord.id == claimed.attempt_id,
            )
            .with_for_update()
        )
        if record is None or record.completed_at is not None:
            return False
        latest = await uow.session.scalar(
            select(func.max(StepAttemptRecord.attempt_no)).where(
                StepAttemptRecord.tenant_id == claimed.context.tenant_id,
                StepAttemptRecord.step_id == claimed.context.step_id,
            )
        )
        return (
            int(latest or 0) == claimed.attempt_no
            and record.lease_owner == claimed.attempt.lease_owner
        )

    async def succeed(
        self,
        claimed: ClaimedStep,
        result: StepExecutionResult,
        trace_context: TraceContext,
    ) -> ExecutionDisposition:
        now = self._clock.now()
        event: RunEventData | None = None
        async with self._uow_factory(claimed.context.tenant_id) as uow:
            step = await uow.steps.get_for_update(
                claimed.context.tenant_id,
                claimed.context.step_id,
            )
            if (
                step is None
                or step.status is not StepStatus.RUNNING
                or not await self._is_current_attempt(uow, claimed)
            ):
                return ExecutionDisposition.STALE
            run = await uow.runs.get_for_update(
                claimed.context.tenant_id,
                claimed.context.run_id,
            )
            if run is None:
                return ExecutionDisposition.STALE
            if run.is_terminal:
                cancellation = TypedError(
                    ErrorCategory.CANCELLED,
                    "run_terminal",
                    "Run became terminal while the Step was executing",
                    retryable=False,
                )
                claimed.attempt.fail(cancellation, now)
                step.cancel()
                await uow.attempts.complete(claimed.attempt)
                await uow.steps.save(step)
                event = await self._add_event(
                    uow,
                    run,
                    "STEP_CANCELLED",
                    {"step_id": str(step.id)},
                )
                await uow.commit()
                disposition = ExecutionDisposition.CANCELLED
            else:
                claimed.attempt.succeed(result.output_ref, now)
                step.succeed(result.output_ref)
                elapsed = max(
                    0.0,
                    (now - claimed.attempt.started_at).total_seconds(),
                )
                run.record_usage(
                    result.actual_cost.apply(
                        run.usage,
                        elapsed_seconds=elapsed,
                    )
                )
                await self._completion.on_success(
                    uow,
                    run,
                    step,
                    result,
                    trace_context,
                )
                await uow.attempts.complete(claimed.attempt)
                await uow.steps.save(step)
                await uow.runs.save(run)
                event = await self._add_event(
                    uow,
                    run,
                    "STEP_SUCCEEDED",
                    {
                        "step_id": str(step.id),
                        "step_key": step.step_key,
                        "attempt_no": claimed.attempt_no,
                    },
                )
                await uow.commit()
                disposition = ExecutionDisposition.SUCCEEDED
        await self._publish(event)
        return disposition

    async def renew(self, claimed: ClaimedStep) -> bool:
        now = self._clock.now()
        async with self._uow_factory(claimed.context.tenant_id) as uow:
            step = await uow.steps.get_for_update(
                claimed.context.tenant_id,
                claimed.context.step_id,
            )
            if (
                step is None
                or step.status is not StepStatus.RUNNING
                or not await self._is_current_attempt(uow, claimed)
                or now >= claimed.context.deadline_at
            ):
                return False
            renewed_until = min(
                claimed.context.deadline_at,
                now + timedelta(seconds=self._lease_extension_seconds),
            )
            if renewed_until <= claimed.attempt.leased_until:
                return True
            claimed.attempt.renew_lease(renewed_until)
            await uow.attempts.renew(claimed.attempt)
            await uow.commit()
            return True

    async def fail(
        self,
        claimed: ClaimedStep,
        error: TypedError,
        trace_context: TraceContext,
    ) -> ExecutionDisposition:
        now = self._clock.now()
        event: RunEventData | None = None
        async with self._uow_factory(claimed.context.tenant_id) as uow:
            step = await uow.steps.get_for_update(
                claimed.context.tenant_id,
                claimed.context.step_id,
            )
            if (
                step is None
                or step.status is not StepStatus.RUNNING
                or not await self._is_current_attempt(uow, claimed)
            ):
                return ExecutionDisposition.STALE
            run = await uow.runs.get_for_update(
                claimed.context.tenant_id,
                claimed.context.run_id,
            )
            if run is None:
                return ExecutionDisposition.STALE

            claimed.attempt.fail(error, now)
            if run.is_terminal or error.category is ErrorCategory.CANCELLED:
                step.cancel()
                disposition = ExecutionDisposition.CANCELLED
                event_type = "STEP_CANCELLED"
            else:
                delay = min(
                    self._retry_max_seconds,
                    self._retry_base_seconds * (2 ** (claimed.attempt_no - 1)),
                )
                retry_at = now + timedelta(seconds=delay)
                if (
                    error.retryable
                    and claimed.attempt_no < self._attempt_limit(step.step_type)
                    and retry_at < run.budget.hard_deadline_at
                ):
                    step.retry_later(retry_at)
                    disposition = ExecutionDisposition.RETRY_SCHEDULED
                    event_type = "STEP_RETRY_SCHEDULED"
                else:
                    step.fail()
                    disposition = ExecutionDisposition.FAILED
                    event_type = "STEP_FAILED"

            await self._completion.on_failure(
                uow,
                run,
                step,
                error,
                disposition,
                trace_context,
            )
            await uow.attempts.complete(claimed.attempt)
            await uow.steps.save(step)
            await uow.runs.save(run)
            event = await self._add_event(
                uow,
                run,
                event_type,
                {
                    "step_id": str(step.id),
                    "step_key": step.step_key,
                    "attempt_no": claimed.attempt_no,
                    "error_category": error.category.value,
                    "error_code": error.code,
                },
            )
            await uow.commit()
        await self._publish(event)
        return disposition
