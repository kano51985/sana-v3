"""Atomic message and workflow submission service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.orchestration.domain import (
    ArtifactRef,
    RoutingDecision,
    SearchMode,
    SearchRun,
    SearchStep,
    StepType,
)
from sana.modules.orchestration.outbox import OutboxMessage
from sana.modules.orchestration.events import RunEventData
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import IdFactory, TraceContext

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
    request_hash: str | None = None


def normalized_content_sha256(content: str) -> str:
    normalized = content.strip()
    if not normalized:
        raise ValueError("Message content cannot be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmitMessageCommand:
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID
    content: str
    idempotency_key: str
    routing: RoutingDecision
    trace_context: TraceContext

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Message content cannot be empty")
        if not 1 <= len(self.idempotency_key.strip()) <= 200:
            raise ValueError("Idempotency-Key must contain between 1 and 200 characters")


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

        normalized_content = command.content.strip()
        normalized_key = command.idempotency_key.strip()
        request_hash = normalized_content_sha256(normalized_content)
        async with self._uow_factory(command.tenant_id) as uow:
            owned = await uow.conversations.lock_owned_by(
                command.tenant_id,
                command.conversation_id,
                command.user_id,
            )
            if not owned:
                raise InvariantViolation(
                    "Conversation does not belong to the authenticated user",
                    code="conversation_not_found",
                )
            existing = await uow.conversations.find_submission(
                command.tenant_id,
                command.conversation_id,
                normalized_key,
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise InvariantViolation(
                        "Idempotency-Key was already used for different content",
                        code="idempotency_conflict",
                    )
                return SubmissionReceipt(
                    existing.message_id,
                    existing.response_run_id,
                    existing.search_run_id,
                    existing.status,
                    duplicate=True,
                    request_hash=request_hash,
                )

            created_at = self._clock.now()
            message_id = self._ids.new_uuid()
            response_run_id = self._ids.new_uuid()
            search_run_id = self._ids.new_uuid()
            step_id = self._ids.new_uuid()
            outbox_id = self._ids.new_uuid()
            run_event_id = self._ids.new_uuid()
            message = MessageDraft(
                id=message_id,
                tenant_id=command.tenant_id,
                conversation_id=command.conversation_id,
                author_user_id=command.user_id,
                role=MessageRole.USER,
                content=normalized_content,
                idempotency_key=normalized_key,
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
            route_step = SearchStep(
                id=step_id,
                tenant_id=command.tenant_id,
                run_id=search_run_id,
                step_key="route",
                step_type=StepType.ROUTE,
                plan_revision=1,
                input_ref=ArtifactRef(
                    uri=f"db://messages/{message_id}",
                    sha256=request_hash,
                ),
            )
            event_type = (
                "STEP_READY_FAST"
                if command.routing.mode is SearchMode.FAST
                else "STEP_READY_RESEARCH"
            )
            outbox_message = OutboxMessage(
                id=outbox_id,
                tenant_id=command.tenant_id,
                aggregate_type="search_step",
                aggregate_id=step_id,
                event_type=event_type,
                payload={"step_id": str(step_id)},
                trace_context=command.trace_context,
                dedupe_key=f"step-ready:{step_id}",
                available_at=created_at,
                created_at=created_at,
            )
            queued_event = RunEventData(
                id=run_event_id,
                tenant_id=command.tenant_id,
                run_id=search_run_id,
                sequence=1,
                event_type="RUN_QUEUED",
                payload={"mode": command.routing.mode.value},
                created_at=created_at,
            )

            await uow.conversations.add_message(message)
            await uow.response_runs.add(response_run)
            await uow.runs.add(search_run)
            await uow.steps.add(route_step)
            await uow.outbox.add(outbox_message)
            await uow.events.add(queued_event)
            await uow.commit()
            return SubmissionReceipt(
                message_id,
                response_run_id,
                search_run_id,
                search_run.status.value,
                request_hash=request_hash,
            )
