"""Domain-owned repository and unit-of-work contracts."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sana.modules.conversation.domain import (
    MessageDraft,
    ResponseRunDraft,
    SubmissionReceipt,
)
from sana.modules.orchestration.ports import RunRepository
from sana.modules.orchestration.ports import StepRepository
from sana.modules.orchestration.outbox import OutboxRepository
from sana.modules.orchestration.events import RunEventRepository


class ConversationRepository(Protocol):
    async def lock_owned_by(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
    ) -> bool: ...

    async def is_owned_by(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
    ) -> bool: ...

    async def find_submission(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> SubmissionReceipt | None: ...

    async def add_message(self, message: MessageDraft) -> None: ...


class ResponseRunRepository(Protocol):
    async def add(self, response_run: ResponseRunDraft) -> None: ...


class ConversationUnitOfWork(Protocol):
    conversations: ConversationRepository
    response_runs: ResponseRunRepository
    runs: RunRepository
    steps: StepRepository
    outbox: OutboxRepository
    events: RunEventRepository

    async def __aenter__(self) -> "ConversationUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None: ...

    async def commit(self) -> None: ...


class ConversationUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: UUID) -> ConversationUnitOfWork: ...
