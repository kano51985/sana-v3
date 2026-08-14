"""Transactional outbox values and repository contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID

from sana.modules.shared.ids import TraceContext


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: UUID
    tenant_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: Mapping[str, Any]
    trace_context: TraceContext
    dedupe_key: str
    available_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("available_at", "created_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if not self.aggregate_type.strip() or not self.event_type.strip():
            raise ValueError("Outbox aggregate and event types cannot be empty")
        if not self.dedupe_key.strip():
            raise ValueError("Outbox dedupe_key cannot be empty")
        if set(self.payload) != {"step_id"}:
            raise ValueError("Step outbox payload must contain only step_id")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class PendingOutboxMessage:
    message: OutboxMessage
    publish_attempts: int = 0


class OutboxRepository(Protocol):
    async def add(self, message: OutboxMessage) -> None: ...

    async def claim_unpublished(
        self,
        now: datetime,
        limit: int,
    ) -> list[PendingOutboxMessage]: ...

    async def mark_published(self, message_id: UUID, published_at: datetime) -> None: ...

    async def mark_failed(self, message_id: UUID, error: str) -> None: ...


def trace_context_to_dict(context: TraceContext) -> dict[str, Any]:
    return {
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "trace_flags": context.trace_flags,
        "baggage": dict(context.baggage),
    }


def trace_context_from_dict(payload: Mapping[str, Any]) -> TraceContext:
    return TraceContext(
        trace_id=str(payload["trace_id"]),
        span_id=str(payload["span_id"]),
        trace_flags=str(payload.get("trace_flags", "01")),
        baggage={
            str(key): str(value)
            for key, value in dict(payload.get("baggage", {})).items()
        },
    )
