"""Framework-free values shared by the shadow campaign bounded context."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sana.modules.shared.errors import InvariantViolation


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


_CAMPAIGN_TRANSITIONS = {
    CampaignStatus.CREATED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.ABORTED}
    ),
    CampaignStatus.RUNNING: frozenset(
        {
            CampaignStatus.STOPPING,
            CampaignStatus.AWAITING_REVIEW,
            CampaignStatus.COMPLETED,
        }
    ),
    CampaignStatus.STOPPING: frozenset(
        {
            CampaignStatus.PAUSED,
            CampaignStatus.AWAITING_REVIEW,
            CampaignStatus.COMPLETED,
            CampaignStatus.ABORTED,
        }
    ),
    CampaignStatus.PAUSED: frozenset(
        {CampaignStatus.RUNNING, CampaignStatus.ABORTED}
    ),
    CampaignStatus.AWAITING_REVIEW: frozenset(
        {CampaignStatus.COMPLETED, CampaignStatus.ABORTED}
    ),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.ABORTED: frozenset(),
}


@dataclass(slots=True)
class CampaignLifecycle:
    """Optimistically locked campaign state machine independent of persistence."""

    id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    max_runs: int
    planned_count: int
    _status: CampaignStatus = field(
        default=CampaignStatus.CREATED,
        init=False,
        repr=False,
    )
    _gate_status: GateStatus = field(
        default=GateStatus.PENDING,
        init=False,
        repr=False,
    )
    _stop_intent: StopIntent = field(
        default=StopIntent.NONE,
        init=False,
        repr=False,
    )
    stop_reason: str | None = None
    started_at: datetime | None = None
    review_deadline_at: datetime | None = None
    completed_at: datetime | None = None
    version: int = 0
    _persisted_version: int = field(default=0, init=False, repr=False)

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        tenant_id: UUID,
        created_by_user_id: UUID,
        max_runs: int,
        planned_count: int,
        status: CampaignStatus,
        gate_status: GateStatus,
        stop_intent: StopIntent,
        stop_reason: str | None,
        started_at: datetime | None,
        review_deadline_at: datetime | None,
        completed_at: datetime | None,
        version: int,
    ) -> "CampaignLifecycle":
        campaign = cls(
            id=id,
            tenant_id=tenant_id,
            created_by_user_id=created_by_user_id,
            max_runs=max_runs,
            planned_count=planned_count,
            stop_reason=stop_reason,
            started_at=started_at,
            review_deadline_at=review_deadline_at,
            completed_at=completed_at,
            version=version,
        )
        campaign._status = CampaignStatus(status)
        campaign._gate_status = GateStatus(gate_status)
        campaign._stop_intent = StopIntent(stop_intent)
        campaign._persisted_version = version
        campaign._validate_loaded_state()
        return campaign

    @property
    def status(self) -> CampaignStatus:
        return self._status

    @property
    def gate_status(self) -> GateStatus:
        return self._gate_status

    @property
    def stop_intent(self) -> StopIntent:
        return self._stop_intent

    @property
    def persisted_version(self) -> int:
        return self._persisted_version

    @property
    def is_terminal(self) -> bool:
        return self.status in {CampaignStatus.COMPLETED, CampaignStatus.ABORTED}

    def mark_persisted(self) -> None:
        self._persisted_version = self.version

    def _validate_loaded_state(self) -> None:
        for value, name in (
            (self.started_at, "started_at"),
            (self.review_deadline_at, "review_deadline_at"),
            (self.completed_at, "completed_at"),
        ):
            if value is not None:
                require_aware(value, name)
        if self.version < 0 or self.max_runs < 1 or not 0 <= self.planned_count <= self.max_runs:
            raise InvariantViolation("Campaign planning counters are invalid")
        if self.status is CampaignStatus.CREATED:
            if self.started_at is not None or self.completed_at is not None:
                raise InvariantViolation("Persisted CREATED campaign has run timestamps")
        elif self.status is not CampaignStatus.ABORTED and self.started_at is None:
            raise InvariantViolation("Persisted active campaign has no started_at")
        if self.status is CampaignStatus.STOPPING and (
            self.stop_intent is StopIntent.NONE or not (self.stop_reason or "").strip()
        ):
            raise InvariantViolation("Persisted STOPPING campaign has no stop intent")
        if self.status is CampaignStatus.AWAITING_REVIEW and self.review_deadline_at is None:
            raise InvariantViolation("Persisted review campaign has no deadline")
        if self.is_terminal != (self.completed_at is not None):
            raise InvariantViolation("Persisted campaign terminal timestamp is inconsistent")
        if self.status is CampaignStatus.COMPLETED and self.gate_status is GateStatus.PENDING:
            raise InvariantViolation("Persisted completed campaign has no gate decision")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise InvariantViolation("Persisted campaign timestamps move backwards")

    def _require_not_before_start(self, at: datetime, field_name: str) -> None:
        if self.started_at is not None and at < self.started_at:
            raise InvariantViolation(f"{field_name} cannot precede campaign start")

    def _transition(self, target: CampaignStatus) -> None:
        if target not in _CAMPAIGN_TRANSITIONS[self.status]:
            raise InvariantViolation(
                f"Illegal campaign transition: {self.status.value} -> {target.value}",
                code="illegal_state_transition",
                details={
                    "entity": "campaign",
                    "current": self.status.value,
                    "target": target.value,
                },
            )
        self._status = target
        self.version += 1

    def start(self, at: datetime) -> None:
        require_aware(at, "started_at")
        self._require_not_before_start(at, "started_at")
        if self.planned_count != self.max_runs:
            raise InvariantViolation(
                "Campaign cannot start before every planned run is materialized",
                code="campaign_not_materialized",
            )
        self._transition(CampaignStatus.RUNNING)
        self.started_at = self.started_at or at
        self._stop_intent = StopIntent.NONE
        self.stop_reason = None

    def resume(self, at: datetime) -> None:
        """Resume a drained PAUSED campaign without rewriting its first start."""

        require_aware(at, "resumed_at")
        self._require_not_before_start(at, "resumed_at")
        self._transition(CampaignStatus.RUNNING)
        self._stop_intent = StopIntent.NONE
        self.stop_reason = None
        self.review_deadline_at = None

    def request_stop(self, intent: StopIntent, reason: str) -> None:
        normalized_reason = reason.strip()
        intent = StopIntent(intent)
        if intent is StopIntent.NONE:
            raise InvariantViolation("Stopping a campaign requires a stop intent")
        if not normalized_reason:
            raise InvariantViolation("Stopping a campaign requires a reason")
        self._transition(CampaignStatus.STOPPING)
        self._stop_intent = intent
        self.stop_reason = normalized_reason

    def escalate_stop(self, intent: StopIntent, reason: str) -> None:
        """Upgrade a pending PAUSE drain to a terminal stop intent."""

        normalized_reason = reason.strip()
        intent = StopIntent(intent)
        if self.status is not CampaignStatus.STOPPING:
            raise InvariantViolation(
                "Only a STOPPING campaign can escalate its stop intent",
                code="campaign_not_stopping",
            )
        if self.stop_intent is not StopIntent.PAUSE or intent in {
            StopIntent.NONE,
            StopIntent.PAUSE,
        }:
            raise InvariantViolation(
                "Campaign stop intent cannot be downgraded or replaced",
                code="stop_intent_escalation_invalid",
            )
        if not normalized_reason:
            raise InvariantViolation("Stop escalation requires a reason")
        self._stop_intent = intent
        self.stop_reason = normalized_reason
        self.version += 1

    def settle_stop(self, at: datetime) -> None:
        require_aware(at, "stop settled_at")
        self._require_not_before_start(at, "stop settled_at")
        target = (
            CampaignStatus.PAUSED
            if self.stop_intent is StopIntent.PAUSE
            else CampaignStatus.ABORTED
        )
        self._transition(target)
        if target is CampaignStatus.ABORTED:
            self.completed_at = at

    def await_review(self, at: datetime, *, deadline: datetime) -> None:
        require_aware(at, "awaiting_review_at")
        require_aware(deadline, "review_deadline_at")
        self._require_not_before_start(at, "awaiting_review_at")
        if deadline <= at:
            raise InvariantViolation("Review deadline must be after review start")
        self._transition(CampaignStatus.AWAITING_REVIEW)
        self.review_deadline_at = deadline

    def complete(self, decision: GateStatus, at: datetime) -> None:
        require_aware(at, "completed_at")
        self._require_not_before_start(at, "completed_at")
        decision = GateStatus(decision)
        if decision is GateStatus.PENDING:
            raise InvariantViolation("Campaign completion requires a final gate decision")
        self._transition(CampaignStatus.COMPLETED)
        self._gate_status = decision
        self.completed_at = at

    def abort(self, reason: str, at: datetime) -> None:
        require_aware(at, "completed_at")
        self._require_not_before_start(at, "completed_at")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvariantViolation("Campaign abort requires a reason")
        self._transition(CampaignStatus.ABORTED)
        self._stop_intent = StopIntent.ABORT
        self.stop_reason = normalized_reason
        self.completed_at = at


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
