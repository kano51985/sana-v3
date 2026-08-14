"""Authoritative workflow events persisted in PostgreSQL."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RunEventData:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("Run event sequence must be positive")
        if not self.event_type.strip():
            raise ValueError("Run event type cannot be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("Run event timestamp must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


class RunEventRepository(Protocol):
    async def add(self, event: RunEventData) -> None: ...

    async def next_sequence(self, tenant_id: UUID, run_id: UUID) -> int: ...
