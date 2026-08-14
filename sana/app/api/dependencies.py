"""Request dependencies and application service contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from sana.modules.conversation.domain import ConversationService
from sana.modules.identity.domain import Principal
from sana.modules.identity.ports import AuthProvider
from sana.modules.orchestration.domain import RoutingDecision
from sana.modules.shared.errors import TypedError


class MessageRouter(Protocol):
    def route(self, message: str) -> RoutingDecision: ...


@dataclass(frozen=True, slots=True)
class RunView:
    id: UUID
    conversation_id: UUID
    message_id: UUID
    mode: str
    status: str
    answer_quality: str
    stop_reason: str | None
    soft_deadline_at: datetime
    hard_deadline_at: datetime
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class EvidenceView:
    fact_key: str
    verdict: str
    confidence: float
    quote: str
    source_url: str


@dataclass(frozen=True, slots=True)
class EventView:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RunApplicationService(Protocol):
    async def get(self, principal: Principal, run_id: UUID) -> RunView | None: ...

    async def cancel(self, principal: Principal, run_id: UUID) -> RunView | None: ...

    async def evidence(
        self,
        principal: Principal,
        run_id: UUID,
    ) -> list[EvidenceView] | None: ...


class RunEventService(Protocol):
    def subscribe(
        self,
        principal: Principal,
        run_id: UUID,
        after_sequence: int,
    ) -> AsyncIterator[EventView]: ...


@dataclass(slots=True)
class AppContainer:
    auth_provider: AuthProvider
    conversation_service: ConversationService
    router: MessageRouter
    run_service: RunApplicationService
    event_service: RunEventService


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


async def require_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token is required",
        )
    token = authorization[7:].strip()
    try:
        return await get_container(request).auth_provider.authenticate(token)
    except TypedError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
        ) from exc
