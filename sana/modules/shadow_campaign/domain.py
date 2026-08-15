"""Framework-free values shared by the shadow campaign bounded context."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID


class CampaignStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    PAUSED = "PAUSED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class GateStatus(StrEnum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


class StopIntent(StrEnum):
    NONE = "NONE"
    PAUSE = "PAUSE"
    ABORT = "ABORT"
    FATAL = "FATAL"
    BUDGET = "BUDGET"
    CALL_CEILING = "CALL_CEILING"


class SchedulingState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    CONVERSATION_BOUND = "CONVERSATION_BOUND"
    SUBMITTED = "SUBMITTED"
    COLLECTED = "COLLECTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ReservationState(StrEnum):
    NONE = "NONE"
    ACTIVE = "ACTIVE"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class ReviewVerdict(StrEnum):
    CORRECT = "CORRECT"
    MINOR_ERROR = "MINOR_ERROR"
    MAJOR_ERROR = "MAJOR_ERROR"
    UNREVIEWABLE = "UNREVIEWABLE"


class ReviewActor(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class ErrorClass(StrEnum):
    CANDIDATE_DEFECT = "CANDIDATE_DEFECT"
    PERMANENT_CONFIGURATION = "PERMANENT_CONFIGURATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    CONTENT_GAP = "CONTENT_GAP"


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def freeze_json(value: Any) -> Any:
    """Return an immutable JSON-like value without changing scalar precision."""

    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Canonical mapping keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        frozen = tuple(freeze_json(item) for item in value)
        return tuple(sorted(frozen, key=lambda item: canonical_json_bytes(item)))
    if value is None or isinstance(value, (str, bool, int, float, Decimal, datetime, UUID, StrEnum)):
        return value
    raise ValueError(f"Unsupported canonical value type: {type(value).__name__}")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal values must be finite")
        return format(value, "f")
    if isinstance(value, datetime):
        require_aware(value, "canonical datetime")
        normalized = value.astimezone(UTC)
        return normalized.isoformat(timespec="auto").replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Float values must be finite")
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Canonical mapping keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise ValueError(f"Unsupported canonical value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_snapshot(value: Any) -> Any:
    """Return the exact JSON-compatible value used for hashing and JSONB storage."""

    return json.loads(canonical_json_bytes(value))


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
