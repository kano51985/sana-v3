from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from sana.modules.conversation.domain import (
    ConversationService,
    SubmissionReceipt,
    SubmitMessageCommand,
    normalized_content_sha256,
)
from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeConversationRepository:
    def __init__(self, tenant_id: UUID, conversation_id: UUID, user_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.existing: SubmissionReceipt | None = None
        self.messages = []

    async def is_owned_by(self, tenant_id, conversation_id, user_id) -> bool:
        return (
            tenant_id == self.tenant_id
            and conversation_id == self.conversation_id
            and user_id == self.user_id
        )

    async def lock_owned_by(self, tenant_id, conversation_id, user_id) -> bool:
        return await self.is_owned_by(tenant_id, conversation_id, user_id)

    async def find_submission(self, tenant_id, conversation_id, idempotency_key):
        return self.existing

    async def add_message(self, message) -> None:
        self.messages.append(message)


class CollectingRepository:
    def __init__(self) -> None:
        self.values = []

    async def add(self, value) -> None:
        self.values.append(value)


class FakeUnitOfWork:
    def __init__(self, conversations: FakeConversationRepository) -> None:
        self.conversations = conversations
        self.response_runs = CollectingRepository()
        self.runs = CollectingRepository()
        self.steps = CollectingRepository()
        self.outbox = CollectingRepository()
        self.events = CollectingRepository()
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if not self.committed:
            self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True


def make_service(owned: bool = True):
    tenant_id, conversation_id, user_id = uuid4(), uuid4(), uuid4()
    repo = FakeConversationRepository(tenant_id, conversation_id, user_id)
    if not owned:
        repo.user_id = uuid4()
    uow = FakeUnitOfWork(repo)
    policy = SearchPolicy.default()
    service = ConversationService(
        lambda requested_tenant: uow,
        DeterministicIdFactory("conversation"),
        FrozenClock(NOW),
        policy,
    )
    command = SubmitMessageCommand(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conversation_id,
        content="What happened?",
        idempotency_key="request-1",
        routing=RoutingDecision(
            SearchMode.FAST,
            ("single_fact",),
            policy.version,
            0.95,
        ),
        trace_context=TraceContext.create(DeterministicIdFactory("request-trace")),
    )
    return service, command, uow


@pytest.mark.asyncio
async def test_message_and_both_runs_commit_in_one_unit_of_work() -> None:
    service, command, uow = make_service()

    receipt = await service.submit_message(command)

    assert uow.committed
    assert len(uow.conversations.messages) == 1
    assert len(uow.response_runs.values) == 1
    assert len(uow.runs.values) == 1
    assert len(uow.steps.values) == 1
    assert len(uow.outbox.values) == 1
    assert len(uow.events.values) == 1
    run = uow.runs.values[0]
    assert run.message_id == receipt.message_id
    assert run.response_run_id == receipt.response_run_id
    assert run.tenant_id == command.tenant_id
    assert uow.outbox.values[0].payload == {"step_id": str(uow.steps.values[0].id)}
    assert uow.events.values[0].event_type == "RUN_QUEUED"


@pytest.mark.asyncio
async def test_idempotent_retry_returns_existing_submission_without_writes() -> None:
    service, command, uow = make_service()
    existing = SubmissionReceipt(
        uuid4(),
        uuid4(),
        uuid4(),
        "RUNNING",
        request_hash=normalized_content_sha256(command.content),
    )
    uow.conversations.existing = existing

    receipt = await service.submit_message(command)

    assert receipt.duplicate
    assert receipt.search_run_id == existing.search_run_id
    assert not uow.committed
    assert uow.rolled_back
    assert not uow.conversations.messages


@pytest.mark.asyncio
async def test_idempotent_retry_with_different_content_is_rejected() -> None:
    service, command, uow = make_service()
    uow.conversations.existing = SubmissionReceipt(
        uuid4(),
        uuid4(),
        uuid4(),
        "RUNNING",
        request_hash=normalized_content_sha256("different content"),
    )

    with pytest.raises(InvariantViolation) as error:
        await service.submit_message(command)

    assert error.value.code == "idempotency_conflict"
    assert uow.rolled_back
    assert not uow.conversations.messages


@pytest.mark.asyncio
async def test_cross_user_conversation_is_rejected_and_rolled_back() -> None:
    service, command, uow = make_service(owned=False)

    with pytest.raises(InvariantViolation, match="authenticated user"):
        await service.submit_message(command)

    assert uow.rolled_back
    assert not uow.conversations.messages
