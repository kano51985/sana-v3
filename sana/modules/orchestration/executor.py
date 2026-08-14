"""Durable one-Step execution boundary used by Celery handlers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Protocol
from uuid import UUID

from sana.modules.orchestration.step_handlers.base import (
    StepExecutionContext,
    StepExecutionResult,
    StepHandlerRegistry,
)
from sana.modules.orchestration.domain import StepAttempt
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


logger = logging.getLogger(__name__)


class ExecutionDisposition(StrEnum):
    CLAIMED = "CLAIMED"
    IGNORED = "IGNORED"
    SUCCEEDED = "SUCCEEDED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class ClaimedStep:
    attempt: StepAttempt
    context: StepExecutionContext

    @property
    def attempt_id(self) -> UUID:
        return self.attempt.id

    @property
    def attempt_no(self) -> int:
        return self.attempt.attempt_no


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: ClaimedStep | None
    disposition: ExecutionDisposition

    def __post_init__(self) -> None:
        if (self.claimed is None) == (
            self.disposition is ExecutionDisposition.CLAIMED
        ):
            raise ValueError(
                "Claim payload and disposition are inconsistent"
            )

    @classmethod
    def acquired(cls, claimed: ClaimedStep) -> "ClaimResult":
        return cls(claimed, ExecutionDisposition.CLAIMED)

    @classmethod
    def skipped(
        cls,
        disposition: ExecutionDisposition,
    ) -> "ClaimResult":
        if disposition is ExecutionDisposition.CLAIMED:
            raise ValueError("Skipped claim cannot use CLAIMED")
        return cls(None, disposition)


class StepExecutionStore(Protocol):
    async def claim(
        self,
        tenant_id: UUID,
        step_id: UUID,
        worker_id: str,
        trace_context: TraceContext,
    ) -> ClaimResult: ...

    async def succeed(
        self,
        claimed: ClaimedStep,
        result: StepExecutionResult,
        trace_context: TraceContext,
    ) -> ExecutionDisposition: ...

    async def renew(self, claimed: ClaimedStep) -> bool: ...

    async def fail(
        self,
        claimed: ClaimedStep,
        error: TypedError,
        trace_context: TraceContext,
    ) -> ExecutionDisposition: ...


class DurableStepExecutor:
    """Claims in PostgreSQL, runs one bounded operation, then finalizes once."""

    def __init__(
        self,
        store: StepExecutionStore,
        handlers: StepHandlerRegistry,
        *,
        worker_id: str,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self._store = store
        self._handlers = handlers
        self._worker_id = worker_id
        self._heartbeat_seconds = heartbeat_seconds

    async def _heartbeat(self, claimed: ClaimedStep) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            if not await self._store.renew(claimed):
                raise TypedError(
                    ErrorCategory.CANCELLED,
                    "step_lease_lost",
                    "Step lease was lost while the operation was executing",
                    retryable=False,
                )

    async def _execute_operation(self, claimed: ClaimedStep) -> StepExecutionResult:
        handler = self._handlers.resolve(claimed.context.step_type)
        operation = asyncio.create_task(handler.handle(claimed.context))
        heartbeat = asyncio.create_task(self._heartbeat(claimed))
        done, _ = await asyncio.wait(
            (operation, heartbeat),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            return await operation
        operation.cancel()
        with suppress(asyncio.CancelledError):
            await operation
        return await heartbeat

    async def __call__(
        self,
        tenant_id: UUID,
        step_id: UUID,
        trace_context: TraceContext,
    ) -> str:
        claim = await self._store.claim(
            tenant_id,
            step_id,
            self._worker_id,
            trace_context,
        )
        if claim.claimed is None:
            return claim.disposition.value
        claimed = claim.claimed
        try:
            result = await self._execute_operation(claimed)
        except TypedError as error:
            disposition = await self._store.fail(claimed, error, trace_context)
        except LookupError as error:
            disposition = await self._store.fail(
                claimed,
                TypedError(
                    ErrorCategory.PERMANENT,
                    "step_handler_missing",
                    "No operation is configured for the claimed Step type",
                    retryable=False,
                    cause=error,
                ),
                trace_context,
            )
        except Exception as error:
            logger.exception(
                "Unexpected Step operation failure",
                extra={
                    "tenant_id": str(tenant_id),
                    "run_id": str(claimed.context.run_id),
                    "step_id": str(step_id),
                    "step_key": claimed.context.step_key,
                    "step_type": claimed.context.step_type.value,
                },
            )
            disposition = await self._store.fail(
                claimed,
                TypedError(
                    ErrorCategory.INTERNAL,
                    "step_operation_failed",
                    "Step operation failed unexpectedly",
                    retryable=False,
                    cause=error,
                ),
                trace_context,
            )
        else:
            disposition = await self._store.succeed(
                claimed,
                result,
                trace_context,
            )
        return disposition.value
