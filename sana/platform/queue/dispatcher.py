"""Outbox-to-Celery delivery with stable task identifiers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from celery import Celery

from sana.modules.orchestration.outbox import (
    OutboxRepository,
    PendingOutboxMessage,
    trace_context_to_dict,
)
from sana.modules.shared.clock import Clock


class SearchQueue(StrEnum):
    FAST = "fast"
    RESEARCH = "research"
    CRAWL = "crawl"
    MAINTENANCE = "maintenance"


class StepDispatcher(Protocol):
    def dispatch(
        self,
        step_id: UUID,
        trace_context: dict[str, Any],
        queue: SearchQueue,
    ) -> str: ...


class CeleryStepDispatcher:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def dispatch(
        self,
        step_id: UUID,
        trace_context: dict[str, Any],
        queue: SearchQueue,
    ) -> str:
        task_id = f"step:{step_id}"
        self._app.send_task(
            "sana.execute_step",
            args=[str(step_id), trace_context],
            task_id=task_id,
            queue=queue.value,
        )
        return task_id


class OutboxDispatcher:
    def __init__(
        self,
        repository: OutboxRepository,
        step_dispatcher: StepDispatcher,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._steps = step_dispatcher
        self._clock = clock

    @staticmethod
    def _queue(event: PendingOutboxMessage) -> SearchQueue:
        mapping = {
            "STEP_READY_FAST": SearchQueue.FAST,
            "STEP_READY_RESEARCH": SearchQueue.RESEARCH,
            "STEP_READY_CRAWL": SearchQueue.CRAWL,
            "STEP_READY_MAINTENANCE": SearchQueue.MAINTENANCE,
        }
        try:
            return mapping[event.message.event_type]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported outbox event: {event.message.event_type}"
            ) from exc

    async def dispatch_batch(self, *, limit: int = 100) -> tuple[int, int]:
        now = self._clock.now()
        events = await self._repository.claim_unpublished(now, limit)
        published = 0
        failed = 0
        for pending in events:
            message = pending.message
            try:
                self._steps.dispatch(
                    UUID(str(message.payload["step_id"])),
                    trace_context_to_dict(message.trace_context),
                    self._queue(pending),
                )
            except Exception as exc:
                failed += 1
                await self._repository.mark_failed(message.id, str(exc))
            else:
                published += 1
                await self._repository.mark_published(message.id, self._clock.now())
        return published, failed
