"""Privacy-safe Runner state projections and failure dispositions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ErrorClass,
    GateStatus,
    StopIntent,
    require_aware,
)


def _stable_code(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in normalized)
    ):
        raise ValueError(f"{field_name} must be a stable lowercase identifier")
    return normalized


@dataclass(frozen=True, slots=True)
class CampaignRunSummary:
    id: UUID
    status: CampaignStatus
    gate_status: GateStatus
    profile_version: str
    planned_count: int
    submitted_count: int
    terminal_count: int
    skipped_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class CampaignRunState:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    status: CampaignStatus
    gate_status: GateStatus
    stop_intent: StopIntent
    profile_version: str
    max_runs: int
    pending_count: int
    claimed_count: int
    submitted_count: int
    collected_count: int
    failed_count: int
    skipped_count: int
    active_reservation_count: int
    selected_review_count: int
    completed_review_count: int
    review_deadline_at: datetime | None

    def __post_init__(self) -> None:
        if self.review_deadline_at is not None:
            require_aware(self.review_deadline_at, "review_deadline_at")
        values = (
            self.max_runs,
            self.pending_count,
            self.claimed_count,
            self.submitted_count,
            self.collected_count,
            self.failed_count,
            self.skipped_count,
            self.active_reservation_count,
            self.selected_review_count,
            self.completed_review_count,
        )
        if self.max_runs < 1 or any(value < 0 for value in values):
            raise ValueError("Campaign Runner counters are invalid")

    @property
    def terminal_result_count(self) -> int:
        return self.collected_count + self.failed_count

    @property
    def execution_sealed(self) -> bool:
        return (
            self.terminal_result_count + self.skipped_count == self.max_runs
            and self.pending_count == 0
            and self.claimed_count == 0
            and self.submitted_count == 0
            and self.active_reservation_count == 0
        )

    @property
    def has_inflight_work(self) -> bool:
        return bool(
            self.claimed_count
            or self.submitted_count
            or self.active_reservation_count
        )


@dataclass(frozen=True, slots=True)
class CampaignReviewCandidate:
    result_id: UUID
    conversation_id: UUID
    search_run_id: UUID
    case_id: str
    repetition: int
    answerability: str
    answer_quality: str
    rubric_version: str
    reviewed: bool


@dataclass(frozen=True, slots=True)
class RunnerFailureReceipt:
    result_id: UUID
    possibly_billed: bool
    duplicate: bool


@dataclass(frozen=True, slots=True)
class RunnerFailure:
    error_class: ErrorClass
    error_code: str
    failed_phase: str
    possibly_billed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "error_code",
            _stable_code(self.error_code, "error_code", 200),
        )
        object.__setattr__(
            self,
            "failed_phase",
            _stable_code(self.failed_phase, "failed_phase", 100),
        )
        if not isinstance(self.possibly_billed, bool):
            raise ValueError("possibly_billed must be boolean")


__all__ = [
    "CampaignReviewCandidate",
    "CampaignRunState",
    "CampaignRunSummary",
    "RunnerFailure",
    "RunnerFailureReceipt",
]
