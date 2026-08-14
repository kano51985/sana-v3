"""UUID factories and transport-neutral trace context."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, NewType, Protocol
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL


TenantId = NewType("TenantId", UUID)
UserId = NewType("UserId", UUID)
ConversationId = NewType("ConversationId", UUID)
MessageId = NewType("MessageId", UUID)
RunId = NewType("RunId", UUID)
StepId = NewType("StepId", UUID)
AttemptId = NewType("AttemptId", UUID)

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


class IdFactory(Protocol):
    def new_uuid(self) -> UUID:
        """Return a globally unique identifier."""

    def new_span_id(self) -> str:
        """Return a lowercase 64-bit hexadecimal span identifier."""


class RandomIdFactory:
    def new_uuid(self) -> UUID:
        return uuid4()

    def new_span_id(self) -> str:
        return secrets.token_hex(8)


@dataclass(slots=True)
class DeterministicIdFactory:
    """Predictable factory for tests and replay fixtures."""

    seed: str = "sana-test"
    _counter: int = field(default=0, init=False)

    def _next(self, kind: str) -> UUID:
        self._counter += 1
        return uuid5(NAMESPACE_URL, f"{self.seed}:{kind}:{self._counter}")

    def new_uuid(self) -> UUID:
        return self._next("uuid")

    def new_span_id(self) -> str:
        return self._next("span").hex[:16]


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    trace_flags: str = "01"
    baggage: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        trace_id = self.trace_id.lower()
        span_id = self.span_id.lower()
        if not _TRACE_ID_PATTERN.fullmatch(trace_id) or set(trace_id) == {"0"}:
            raise ValueError("trace_id must be a non-zero 32-character hex string")
        if not _SPAN_ID_PATTERN.fullmatch(span_id) or set(span_id) == {"0"}:
            raise ValueError("span_id must be a non-zero 16-character hex string")
        if not re.fullmatch(r"[0-9a-fA-F]{2}", self.trace_flags):
            raise ValueError("trace_flags must be a two-character hex string")
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "span_id", span_id)
        object.__setattr__(self, "trace_flags", self.trace_flags.lower())
        object.__setattr__(self, "baggage", MappingProxyType(dict(self.baggage)))

    @classmethod
    def create(cls, factory: IdFactory | None = None) -> "TraceContext":
        id_factory = factory or RandomIdFactory()
        return cls(
            trace_id=id_factory.new_uuid().hex,
            span_id=id_factory.new_span_id(),
        )

    def child(self, factory: IdFactory | None = None) -> "TraceContext":
        id_factory = factory or RandomIdFactory()
        return TraceContext(
            trace_id=self.trace_id,
            span_id=id_factory.new_span_id(),
            trace_flags=self.trace_flags,
            baggage=self.baggage,
        )

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"
