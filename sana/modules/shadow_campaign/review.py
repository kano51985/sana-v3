"""Structured, immutable manual review values and safe source projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sana.modules.shadow_campaign.domain import (
    ReviewActor,
    ReviewVerdict,
    require_aware,
    snapshot_hash,
)


_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_SYSTEM_REASON_CODES = frozenset(
    {"expected_answer_missing", "review_material_unavailable"}
)
HUMAN_REVIEW_REASON_CODES = frozenset(
    {
        "citation_irrelevant",
        "contradiction",
        "incomplete_answer",
        "incorrect_fact",
        "material_missing",
        "missing_detail",
        "source_inappropriate",
        "stale_source",
        "unsupported_claim",
    }
)


class ReviewScore(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    tenant_id: UUID
    campaign_id: UUID
    result_id: UUID
    rubric_version: str
    correctness_verdict: ReviewVerdict
    citation_relevance: ReviewScore
    source_appropriateness: ReviewScore
    freshness: ReviewScore
    completeness: ReviewScore
    reason_codes: tuple[str, ...]
    actor_type: ReviewActor
    reviewer_user_id: UUID | None

    def __post_init__(self) -> None:
        rubric = self.rubric_version.strip()
        if not rubric or len(rubric) > 100:
            raise ValueError("rubric_version must contain between 1 and 100 characters")
        reasons = tuple(dict.fromkeys(item.strip() for item in self.reason_codes))
        if len(reasons) > 20 or any(not _REASON_CODE.fullmatch(item) for item in reasons):
            raise ValueError("Review reason codes must be unique stable identifiers")
        if self.actor_type is ReviewActor.HUMAN:
            if self.reviewer_user_id is None:
                raise ValueError("Human review requires a reviewer principal")
            if any(item not in HUMAN_REVIEW_REASON_CODES for item in reasons):
                raise ValueError("Human review reason is not allowlisted by the rubric")
            if self.correctness_verdict is not ReviewVerdict.CORRECT and not reasons:
                raise ValueError("Non-correct human review requires a reason code")
        elif self.reviewer_user_id is not None:
            raise ValueError("System review cannot carry a reviewer principal")
        scores = (
            self.citation_relevance,
            self.source_appropriateness,
            self.freshness,
            self.completeness,
        )
        if self.actor_type is ReviewActor.SYSTEM:
            if len(reasons) != 1 or reasons[0] not in _SYSTEM_REASON_CODES:
                raise ValueError("System review reason is not allowlisted")
            expected_shape = {
                "expected_answer_missing": (
                    ReviewVerdict.MAJOR_ERROR,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.FAIL,
                ),
                "review_material_unavailable": (
                    ReviewVerdict.UNREVIEWABLE,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.NOT_APPLICABLE,
                    ReviewScore.NOT_APPLICABLE,
                ),
            }[reasons[0]]
            if (self.correctness_verdict, *scores) != expected_shape:
                raise ValueError("System review must use its canonical verdict and scores")
        if self.correctness_verdict is ReviewVerdict.UNREVIEWABLE and any(
            item is not ReviewScore.NOT_APPLICABLE for item in scores
        ):
            raise ValueError("Unreviewable records must use NOT_APPLICABLE scores")
        object.__setattr__(self, "rubric_version", rubric)
        object.__setattr__(self, "reason_codes", reasons)

    @classmethod
    def expected_answer_missing(
        cls,
        *,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
        rubric_version: str,
    ) -> "ReviewSubmission":
        return cls(
            tenant_id,
            campaign_id,
            result_id,
            rubric_version,
            ReviewVerdict.MAJOR_ERROR,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.FAIL,
            ("expected_answer_missing",),
            ReviewActor.SYSTEM,
            None,
        )

    @classmethod
    def material_unavailable(
        cls,
        *,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
        rubric_version: str,
    ) -> "ReviewSubmission":
        return cls(
            tenant_id,
            campaign_id,
            result_id,
            rubric_version,
            ReviewVerdict.UNREVIEWABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ("review_material_unavailable",),
            ReviewActor.SYSTEM,
            None,
        )

    @property
    def sha256(self) -> str:
        return snapshot_hash(self)


@dataclass(frozen=True, slots=True)
class ReviewReceipt:
    result_id: UUID
    review_id: UUID
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ReviewCitationProjection:
    id: UUID
    claim_id: UUID
    verified_evidence_id: UUID
    fact_requirement_id: UUID
    document_version_id: UUID
    document_chunk_id: UUID
    ordinal: int
    verdict: str
    confidence: float
    source_authority: str
    document_fetched_at: datetime
    start_offset: int
    end_offset: int
    label: str
    rendered_url: str
    quote: str

    def __post_init__(self) -> None:
        require_aware(self.document_fetched_at, "document_fetched_at")
        if self.ordinal < 1 or self.end_offset <= self.start_offset:
            raise ValueError("Review Citation projection is invalid")
        if not self.label.strip() or not self.rendered_url.strip() or not self.quote:
            raise ValueError("Review Citation material is incomplete")


@dataclass(frozen=True, slots=True)
class ReviewClaimProjection:
    id: UUID
    claim_kind: str
    fact_requirement_id: UUID | None
    support_status: str
    citations: tuple[ReviewCitationProjection, ...]
    claim_text: str


@dataclass(frozen=True, slots=True)
class ReviewProjection:
    tenant_id: UUID
    campaign_id: UUID
    result_id: UUID
    conversation_id: UUID
    search_run_id: UUID
    case_id: str
    repetition: int
    rubric_version: str
    claims: tuple[ReviewClaimProjection, ...]
    answer_text: str


__all__ = [
    "HUMAN_REVIEW_REASON_CODES",
    "ReviewCitationProjection",
    "ReviewClaimProjection",
    "ReviewProjection",
    "ReviewReceipt",
    "ReviewScore",
    "ReviewSubmission",
]
