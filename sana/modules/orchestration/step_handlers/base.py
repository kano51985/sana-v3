"""One bounded operation per worker task, with cooperative cancellation checks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sana.modules.orchestration.domain import ArtifactRef, StepType
from sana.modules.orchestration.search_workflow import StepBudgetCost
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


class CancellationProbe(Protocol):
    async def is_cancelled(self, tenant_id: UUID, run_id: UUID) -> bool: ...


@dataclass(frozen=True, slots=True)
class StepExecutionContext:
    tenant_id: UUID
    run_id: UUID
    step_id: UUID
    step_key: str
    step_type: StepType
    attempt_id: UUID
    attempt_no: int
    trace_context: TraceContext
    deadline_at: datetime
    input_ref: ArtifactRef
    cancellation: CancellationProbe
    clock: Clock

    def __post_init__(self) -> None:
        if not self.step_key.strip():
            raise ValueError("Step execution key cannot be empty")
        if self.attempt_no < 1:
            raise ValueError("Step execution attempt number must be positive")
        if self.deadline_at.tzinfo is None or self.deadline_at.utcoffset() is None:
            raise ValueError("Step deadline must be timezone-aware")

    async def checkpoint(self) -> None:
        if await self.cancellation.is_cancelled(self.tenant_id, self.run_id):
            raise TypedError(
                ErrorCategory.CANCELLED,
                "run_cancelled",
                "Run was cancelled",
                retryable=False,
            )
        if self.clock.now() >= self.deadline_at:
            raise TypedError(
                ErrorCategory.BUDGET,
                "step_deadline_exceeded",
                "Step deadline was exhausted",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    output_ref: ArtifactRef
    actual_cost: StepBudgetCost


StepOperation = Callable[[StepExecutionContext], Awaitable[StepExecutionResult]]


class BoundedStepHandler:
    def __init__(self, operation: StepOperation) -> None:
        self._operation = operation

    async def handle(self, context: StepExecutionContext) -> StepExecutionResult:
        await context.checkpoint()
        remaining = (context.deadline_at - context.clock.now()).total_seconds()
        try:
            async with asyncio.timeout(remaining):
                result = await self._operation(context)
                await context.checkpoint()
                return result
        except TimeoutError as exc:
            raise TypedError(
                ErrorCategory.BUDGET,
                "step_deadline_exceeded",
                "Step deadline was exhausted",
                retryable=False,
                cause=exc,
            ) from exc


class StepHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[StepType, BoundedStepHandler] = {}

    def register(self, step_type: StepType, handler: BoundedStepHandler) -> None:
        if step_type in self._handlers:
            raise ValueError(f"Handler already registered for {step_type.value}")
        self._handlers[step_type] = handler

    def resolve(self, step_type: StepType) -> BoundedStepHandler:
        try:
            return self._handlers[step_type]
        except KeyError as exc:
            raise LookupError(f"No handler registered for {step_type.value}") from exc
