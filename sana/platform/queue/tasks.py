"""Thin Celery entry point that delegates all decisions to a step handler."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from sana.modules.orchestration.outbox import trace_context_from_dict
from sana.modules.shared.ids import TraceContext
from sana.platform.queue.celery_app import celery_app


StepHandler = Callable[[UUID, TraceContext], Any | Awaitable[Any]]
_step_handler: StepHandler | None = None


def configure_step_handler(handler: StepHandler) -> None:
    global _step_handler
    _step_handler = handler


@celery_app.task(
    bind=True,
    name="sana.execute_step",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def execute_step(self, step_id: str, trace_context: dict[str, Any]) -> Any:
    del self
    if _step_handler is None:
        raise RuntimeError("Sana step handler has not been configured")
    result = _step_handler(UUID(step_id), trace_context_from_dict(trace_context))
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result
