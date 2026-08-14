"""Pure domain state for resumable search workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from sana.modules.orchestration.transitions import assert_transition
from sana.modules.shared.errors import InvariantViolation, TypedError


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class SearchMode(StrEnum):
    FAST = "FAST"
    RESEARCH = "RESEARCH"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AnswerQuality(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class StopReason(StrEnum):
    FACTS_COVERED = "FACTS_COVERED"
    TIME_BUDGET = "TIME_BUDGET"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    USER_CANCELLED = "USER_CANCELLED"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"


class StepStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class StepType(StrEnum):
    ROUTE = "ROUTE"
    PLAN = "PLAN"
    DISCOVERY = "DISCOVERY"
    SELECT = "SELECT"
    FETCH = "FETCH"
    EXTRACT = "EXTRACT"
    VERIFY = "VERIFY"
    SYNTHESIZE = "SYNTHESIZE"
    COMPLETE = "COMPLETE"


_RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
}

_STEP_TRANSITIONS: Mapping[StepStatus, frozenset[StepStatus]] = {
    StepStatus.READY: frozenset(
        {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED}
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.READY,
            StepStatus.RETRY_WAIT,
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.RETRY_WAIT: frozenset(
        {StepStatus.READY, StepStatus.FAILED, StepStatus.CANCELLED}
    ),
}


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    mode: SearchMode
    reason_codes: tuple[str, ...]
    policy_version: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if any(not code.strip() for code in self.reason_codes):
            raise ValueError("reason_codes cannot contain empty values")


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    policy_version: str
    created_at: datetime
    soft_deadline_at: datetime
    hard_deadline_at: datetime
    synthesis_reserve_seconds: float
    max_queries: int
    max_providers: int
    max_fetches: int
    max_llm_calls: int
    max_expansion_rounds: int
    phase_seconds: Mapping[str, float]

    def __post_init__(self) -> None:
        for name in ("created_at", "soft_deadline_at", "hard_deadline_at"):
            _require_aware(getattr(self, name), name)
        if not self.created_at <= self.soft_deadline_at <= self.hard_deadline_at:
            raise ValueError("Budget deadlines must be ordered from creation to hard stop")
        if self.synthesis_reserve_seconds <= 0:
            raise ValueError("synthesis_reserve_seconds must be positive")
        if any(
            value < 0
            for value in (
                self.max_queries,
                self.max_providers,
                self.max_fetches,
                self.max_llm_calls,
                self.max_expansion_rounds,
            )
        ):
            raise ValueError("Budget limits cannot be negative")
        if any(value < 0 for value in self.phase_seconds.values()):
            raise ValueError("Phase budgets cannot be negative")
        object.__setattr__(
            self,
            "phase_seconds",
            MappingProxyType(dict(self.phase_seconds)),
        )


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    query_count: int = 0
    provider_count: int = 0
    fetch_count: int = 0
    llm_call_count: int = 0
    expansion_rounds: int = 0
    phase_seconds: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.query_count,
                self.provider_count,
                self.fetch_count,
                self.llm_call_count,
                self.expansion_rounds,
            )
        ):
            raise ValueError("Usage counters cannot be negative")
        if any(value < 0 for value in self.phase_seconds.values()):
            raise ValueError("Phase usage cannot be negative")
        object.__setattr__(
            self,
            "phase_seconds",
            MappingProxyType(dict(self.phase_seconds)),
        )

    def add(
        self,
        *,
        queries: int = 0,
        providers: int = 0,
        fetches: int = 0,
        llm_calls: int = 0,
        expansion_rounds: int = 0,
        phase: str | None = None,
        elapsed_seconds: float = 0.0,
    ) -> "BudgetUsage":
        increments = (queries, providers, fetches, llm_calls, expansion_rounds)
        if any(value < 0 for value in increments) or elapsed_seconds < 0:
            raise ValueError("Usage increments cannot be negative")
        phase_usage = dict(self.phase_seconds)
        if phase is not None:
            phase_usage[phase] = phase_usage.get(phase, 0.0) + elapsed_seconds
        elif elapsed_seconds:
            raise ValueError("phase is required when elapsed_seconds is recorded")
        return BudgetUsage(
            query_count=self.query_count + queries,
            provider_count=self.provider_count + providers,
            fetch_count=self.fetch_count + fetches,
            llm_call_count=self.llm_call_count + llm_calls,
            expansion_rounds=self.expansion_rounds + expansion_rounds,
            phase_seconds=phase_usage,
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    uri: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.uri.strip():
            raise ValueError("Artifact URI cannot be empty")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in self.sha256
        ):
            raise ValueError("Artifact sha256 must be a 64-character hex digest")
        object.__setattr__(self, "sha256", self.sha256.lower())


@dataclass(slots=True)
class SearchRun:
    id: UUID
    tenant_id: UUID
    conversation_id: UUID
    message_id: UUID
    response_run_id: UUID
    routing: RoutingDecision
    budget: BudgetSnapshot
    _status: RunStatus = field(default=RunStatus.QUEUED, init=False, repr=False)
    _answer_quality: AnswerQuality = field(
        default=AnswerQuality.NONE,
        init=False,
        repr=False,
    )
    _stop_reason: StopReason | None = field(default=None, init=False, repr=False)
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    version: int = 0
    _persisted_version: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.routing.policy_version != self.budget.policy_version:
            raise ValueError("Routing and budget policy versions must match")

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        tenant_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        response_run_id: UUID,
        routing: RoutingDecision,
        budget: BudgetSnapshot,
        status: RunStatus,
        answer_quality: AnswerQuality,
        stop_reason: StopReason | None,
        usage: BudgetUsage,
        started_at: datetime | None,
        completed_at: datetime | None,
        version: int,
    ) -> "SearchRun":
        run = cls(
            id=id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            response_run_id=response_run_id,
            routing=routing,
            budget=budget,
            usage=usage,
            started_at=started_at,
            completed_at=completed_at,
            version=version,
        )
        run._status = RunStatus(status)
        run._answer_quality = AnswerQuality(answer_quality)
        run._stop_reason = StopReason(stop_reason) if stop_reason is not None else None
        run._persisted_version = version
        run._validate_loaded_state()
        return run

    def _validate_loaded_state(self) -> None:
        if self.status is RunStatus.SUCCEEDED:
            if self.answer_quality is AnswerQuality.NONE or self.stop_reason is None:
                raise InvariantViolation("Persisted successful run has no answer outcome")
            if (
                self.answer_quality is AnswerQuality.COMPLETE
                and self.stop_reason is not StopReason.FACTS_COVERED
            ):
                raise InvariantViolation("Persisted complete run has an invalid stop reason")
            if (
                self.answer_quality is AnswerQuality.PARTIAL
                and self.stop_reason is StopReason.FACTS_COVERED
            ):
                raise InvariantViolation("Persisted partial run has an invalid stop reason")
        elif self.status is RunStatus.CANCELLED:
            if self.stop_reason is not StopReason.USER_CANCELLED:
                raise InvariantViolation("Persisted cancelled run has an invalid stop reason")
        elif self.status is RunStatus.FAILED:
            if self.answer_quality is not AnswerQuality.NONE or self.stop_reason is None:
                raise InvariantViolation("Persisted failed run has an invalid answer outcome")
        elif self.answer_quality is not AnswerQuality.NONE or self.stop_reason is not None:
            raise InvariantViolation("Non-terminal run cannot have a final answer outcome")

    @property
    def mode(self) -> SearchMode:
        return self.routing.mode

    @property
    def status(self) -> RunStatus:
        return self._status

    @property
    def answer_quality(self) -> AnswerQuality:
        return self._answer_quality

    @property
    def stop_reason(self) -> StopReason | None:
        return self._stop_reason

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    @property
    def persisted_version(self) -> int:
        return self._persisted_version

    def mark_persisted(self) -> None:
        self._persisted_version = self.version

    def _transition(self, target: RunStatus) -> None:
        assert_transition(
            self.status,
            target,
            _RUN_TRANSITIONS,
            entity="search_run",
        )
        self._status = target
        self.version += 1

    def start(self, at: datetime) -> None:
        _require_aware(at, "started_at")
        self._transition(RunStatus.RUNNING)
        self.started_at = self.started_at or at

    def wait(self) -> None:
        self._transition(RunStatus.WAITING)

    def resume(self) -> None:
        self._transition(RunStatus.RUNNING)

    def record_usage(self, usage: BudgetUsage) -> None:
        if self.is_terminal:
            raise InvariantViolation("Cannot update usage for a terminal run")
        if any(
            new < old
            for new, old in (
                (usage.query_count, self.usage.query_count),
                (usage.provider_count, self.usage.provider_count),
                (usage.fetch_count, self.usage.fetch_count),
                (usage.llm_call_count, self.usage.llm_call_count),
                (usage.expansion_rounds, self.usage.expansion_rounds),
            )
        ):
            raise InvariantViolation("Run usage counters cannot decrease")
        self.usage = usage
        self.version += 1

    def succeed(
        self,
        quality: AnswerQuality,
        reason: StopReason,
        at: datetime,
    ) -> None:
        _require_aware(at, "completed_at")
        if quality is AnswerQuality.NONE:
            raise InvariantViolation("A successful run must contain an answer")
        if quality is AnswerQuality.COMPLETE and reason is not StopReason.FACTS_COVERED:
            raise InvariantViolation("A complete answer must stop because facts are covered")
        if quality is AnswerQuality.PARTIAL and reason is StopReason.FACTS_COVERED:
            raise InvariantViolation("A partial answer cannot claim all facts are covered")
        if reason is StopReason.USER_CANCELLED:
            raise InvariantViolation("A successful run cannot be user-cancelled")
        self._transition(RunStatus.SUCCEEDED)
        self._answer_quality = quality
        self._stop_reason = reason
        self.completed_at = at

    def fail(self, reason: StopReason, at: datetime) -> None:
        _require_aware(at, "completed_at")
        if reason in {StopReason.FACTS_COVERED, StopReason.USER_CANCELLED}:
            raise InvariantViolation("Failure stop reason is inconsistent")
        self._transition(RunStatus.FAILED)
        self._answer_quality = AnswerQuality.NONE
        self._stop_reason = reason
        self.completed_at = at

    def cancel(self, at: datetime) -> None:
        _require_aware(at, "completed_at")
        self._transition(RunStatus.CANCELLED)
        self._answer_quality = AnswerQuality.NONE
        self._stop_reason = StopReason.USER_CANCELLED
        self.completed_at = at


@dataclass(slots=True)
class SearchStep:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    step_key: str
    step_type: StepType
    plan_revision: int
    input_ref: ArtifactRef
    _status: StepStatus = field(default=StepStatus.READY, init=False, repr=False)
    _output_ref: ArtifactRef | None = field(default=None, init=False, repr=False)
    retry_at: datetime | None = None
    version: int = 0
    _persisted_version: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.step_key.strip():
            raise ValueError("step_key cannot be empty")
        if self.plan_revision < 1:
            raise ValueError("plan_revision must be at least 1")

    @classmethod
    def rehydrate(
        cls,
        *,
        id: UUID,
        tenant_id: UUID,
        run_id: UUID,
        step_key: str,
        step_type: StepType,
        plan_revision: int,
        input_ref: ArtifactRef,
        status: StepStatus,
        output_ref: ArtifactRef | None,
        retry_at: datetime | None,
        version: int,
    ) -> "SearchStep":
        step = cls(
            id=id,
            tenant_id=tenant_id,
            run_id=run_id,
            step_key=step_key,
            step_type=step_type,
            plan_revision=plan_revision,
            input_ref=input_ref,
            retry_at=retry_at,
            version=version,
        )
        step._status = StepStatus(status)
        step._output_ref = output_ref
        step._persisted_version = version
        if step.status is StepStatus.SUCCEEDED and step.output_ref is None:
            raise InvariantViolation("Persisted successful step has no output")
        if step.status is not StepStatus.SUCCEEDED and step.output_ref is not None:
            raise InvariantViolation("Only a successful step may have an output")
        return step

    @property
    def identity_key(self) -> tuple[UUID, int, str]:
        return self.run_id, self.plan_revision, self.step_key

    def _transition(self, target: StepStatus) -> None:
        assert_transition(
            self.status,
            target,
            _STEP_TRANSITIONS,
            entity="search_step",
        )
        self._status = target
        self.version += 1

    @property
    def status(self) -> StepStatus:
        return self._status

    @property
    def output_ref(self) -> ArtifactRef | None:
        return self._output_ref

    @property
    def persisted_version(self) -> int:
        return self._persisted_version

    def mark_persisted(self) -> None:
        self._persisted_version = self.version

    def start(self) -> None:
        self._transition(StepStatus.RUNNING)
        self.retry_at = None

    def release_expired_lease(self) -> None:
        self._transition(StepStatus.READY)

    def retry_later(self, retry_at: datetime) -> None:
        _require_aware(retry_at, "retry_at")
        self._transition(StepStatus.RETRY_WAIT)
        self.retry_at = retry_at

    def make_ready(self) -> None:
        self._transition(StepStatus.READY)
        self.retry_at = None

    def succeed(self, output_ref: ArtifactRef) -> None:
        if self._output_ref is not None:
            raise InvariantViolation("Successful step output is immutable")
        self._transition(StepStatus.SUCCEEDED)
        self._output_ref = output_ref

    def fail(self) -> None:
        self._transition(StepStatus.FAILED)

    def skip(self) -> None:
        self._transition(StepStatus.SKIPPED)

    def cancel(self) -> None:
        self._transition(StepStatus.CANCELLED)


@dataclass(slots=True)
class StepAttempt:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    step_id: UUID
    attempt_no: int
    idempotency_key: str
    lease_owner: str
    leased_until: datetime
    deadline_at: datetime
    started_at: datetime
    input_ref: ArtifactRef
    _completed_at: datetime | None = field(default=None, init=False, repr=False)
    _output_ref: ArtifactRef | None = field(default=None, init=False, repr=False)
    _error: TypedError | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("leased_until", "deadline_at", "started_at"):
            _require_aware(getattr(self, name), name)
        if self.attempt_no < 1:
            raise ValueError("attempt_no must be at least 1")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty")
        if not self.lease_owner.strip():
            raise ValueError("lease_owner cannot be empty")
        if self.leased_until <= self.started_at:
            raise ValueError("leased_until must be after started_at")
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline_at must be after started_at")

    @property
    def is_complete(self) -> bool:
        return self._completed_at is not None

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at

    @property
    def output_ref(self) -> ArtifactRef | None:
        return self._output_ref

    @property
    def error(self) -> TypedError | None:
        return self._error

    def lease_expired(self, at: datetime) -> bool:
        _require_aware(at, "lease_check_at")
        return not self.is_complete and at >= self.leased_until

    def renew_lease(self, leased_until: datetime) -> None:
        _require_aware(leased_until, "leased_until")
        if self.is_complete:
            raise InvariantViolation("Cannot renew a completed attempt lease")
        if leased_until <= self.leased_until:
            raise InvariantViolation("Renewed lease must extend the current lease")
        if leased_until > self.deadline_at:
            raise InvariantViolation("Attempt lease cannot extend beyond its deadline")
        self.leased_until = leased_until

    def _finish(self, at: datetime) -> None:
        _require_aware(at, "completed_at")
        if self.is_complete:
            raise InvariantViolation("Attempt has already completed")
        if at < self.started_at:
            raise InvariantViolation("Attempt cannot complete before it starts")
        self._completed_at = at

    def succeed(self, output_ref: ArtifactRef, at: datetime) -> None:
        self._finish(at)
        self._output_ref = output_ref

    def fail(self, error: TypedError, at: datetime) -> None:
        self._finish(at)
        self._error = error
