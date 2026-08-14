from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sana.modules.orchestration.outbox import OutboxMessage, PendingOutboxMessage
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext
from sana.platform.queue.dispatcher import OutboxDispatcher, SearchQueue


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def make_message() -> OutboxMessage:
    step_id = uuid4()
    return OutboxMessage(
        id=uuid4(),
        tenant_id=uuid4(),
        aggregate_type="search_step",
        aggregate_id=step_id,
        event_type="STEP_READY_FAST",
        payload={"step_id": str(step_id)},
        trace_context=TraceContext.create(DeterministicIdFactory("outbox")),
        dedupe_key=f"step-ready:{step_id}",
        available_at=NOW,
        created_at=NOW,
    )


class FakeOutboxRepository:
    def __init__(self, messages) -> None:
        self.messages = messages
        self.published = []
        self.failed = []

    async def claim_unpublished(self, now, limit):
        return self.messages[:limit]

    async def mark_published(self, message_id, published_at) -> None:
        self.published.append((message_id, published_at))

    async def mark_failed(self, message_id, error) -> None:
        self.failed.append((message_id, error))


class FakeStepDispatcher:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = []

    def dispatch(self, step_id, trace_context, queue):
        self.calls.append((step_id, trace_context, queue))
        if self.failure:
            raise self.failure
        return f"step:{step_id}"


def test_outbox_payload_cannot_smuggle_execution_parameters() -> None:
    message = make_message()
    with pytest.raises(ValueError, match="only step_id"):
        OutboxMessage(
            id=message.id,
            tenant_id=message.tenant_id,
            aggregate_type=message.aggregate_type,
            aggregate_id=message.aggregate_id,
            event_type=message.event_type,
            payload={"step_id": str(message.aggregate_id), "prompt": "unsafe"},
            trace_context=message.trace_context,
            dedupe_key=message.dedupe_key,
            available_at=message.available_at,
            created_at=message.created_at,
        )


@pytest.mark.asyncio
async def test_dispatcher_marks_event_only_after_broker_accepts_it() -> None:
    message = make_message()
    repository = FakeOutboxRepository([PendingOutboxMessage(message)])
    steps = FakeStepDispatcher()
    dispatcher = OutboxDispatcher(repository, steps, FrozenClock(NOW))

    assert await dispatcher.dispatch_batch() == (1, 0)
    assert repository.published == [(message.id, NOW)]
    assert not repository.failed
    assert steps.calls[0][2] is SearchQueue.FAST


@pytest.mark.asyncio
async def test_dispatcher_leaves_failed_event_retriable() -> None:
    message = make_message()
    repository = FakeOutboxRepository([PendingOutboxMessage(message)])
    dispatcher = OutboxDispatcher(
        repository,
        FakeStepDispatcher(RuntimeError("broker down")),
        FrozenClock(NOW),
    )

    assert await dispatcher.dispatch_batch() == (0, 1)
    assert not repository.published
    assert repository.failed == [(message.id, "broker down")]
