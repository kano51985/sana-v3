"""Atomic message and workflow submission service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.orchestration.domain import RoutingDecision, SearchRun
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import IdFactory

if TYPE_CHECKING:
    from sana.modules.conversation.ports import ConversationUnitOfWorkFactory


class MessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class MessageDraft:
    id: UUID
    tenant_id: UUID
    conversation_id: UUID
    author_user_id: UUID
    role: MessageRole
    content: str
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ResponseRunDraft:
    id: UUID
    tenant_id: UUID
    conversation_id: UUID
    message_id: UUID
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    message_id: UUID
    response_run_id: UUID
    search_run_id: UUID
    status: str
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class SubmitMessageCommand:
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID
    content: str
    idempotency_key: str
    routing: RoutingDecision

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Message content cannot be empty")
        if not self.idempotency_key.strip():
            raise ValueError("Idempotency-Key cannot be empty")


class ConversationService:
    def __init__(
        self,
        uow_factory: "ConversationUnitOfWorkFactory",
        id_factory: IdFactory,
        clock: Clock,
        search_policy: SearchPolicy,
    ) -> None:
        self._uow_factory = uow_factory
        self._ids = id_factory
        self._clock = clock
        self._policy = search_policy

    async def submit_message(self, command: SubmitMessageCommand) -> SubmissionReceipt:
        if command.routing.policy_version != self._policy.version:
            raise InvariantViolation(
                "Routing decision uses a different search policy version",
                code="policy_version_mismatch",
            )

        async with self._uow_factory(command.tenant_id) as uow:
            existing = await uow.conversations.find_submission(
                command.tenant_id,
                command.conversation_id,
                command.idempotency_key,
            )
            if existing is not None:
                return SubmissionReceipt(
                    existing.message_id,
                    existing.response_run_id,
                    existing.search_run_id,
                    existing.status,
                    duplicate=True,
                )

            owned = await uow.conversations.is_owned_by(
                command.tenant_id,
                command.conversation_id,
                command.user_id,
            )
            if not owned:
                raise InvariantViolation(
                    "Conversation does not belong to the authenticated user",
                    code="conversation_not_found",
                )

            created_at = self._clock.now()
            message_id = self._ids.new_uuid()
            response_run_id = self._ids.new_uuid()
            search_run_id = self._ids.new_uuid()
            message = MessageDraft(
                id=message_id,
                tenant_id=command.tenant_id,
                conversation_id=command.conversation_id,
                author_user_id=command.user_id,
                role=MessageRole.USER,
                content=command.content.strip(),
                idempotency_key=command.idempotency_key.strip(),
                created_at=created_at,
            )
            response_run = ResponseRunDraft(
                id=response_run_id,
                tenant_id=command.tenant_id,
                conversation_id=command.conversation_id,
                message_id=message_id,
                status="QUEUED",
                created_at=created_at,
            )
            search_run = SearchRun(
                id=search_run_id,
                tenant_id=command.tenant_id,
                conversation_id=command.conversation_id,
                message_id=message_id,
                response_run_id=response_run_id,
                routing=command.routing,
                budget=self._policy.snapshot(command.routing.mode, created_at),
            )

            await uow.conversations.add_message(message)
            await uow.response_runs.add(response_run)
            await uow.runs.add(search_run)
            await uow.commit()
            return SubmissionReceipt(
                message_id,
                response_run_id,
                search_run_id,
                search_run.status.value,
            )
