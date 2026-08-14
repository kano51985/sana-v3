"""Answer values keep factual prose separate from validation and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ClaimKind(StrEnum):
    FACTUAL = "FACTUAL"
    UNCERTAINTY = "UNCERTAINTY"
    COMMENTARY = "COMMENTARY"


class ClaimSupport(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"
    GROUNDED = "GROUNDED"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"
    UNCONFIRMED = "UNCONFIRMED"


class UnsupportedClaimPolicy(StrEnum):
    DROP = "DROP"
    WEAKEN = "WEAKEN"
    MARK_UNCONFIRMED = "MARK_UNCONFIRMED"


@dataclass(frozen=True, slots=True)
class ProposedClaim:
    claim_key: str
    text: str
    fact_requirement_id: UUID | None
    evidence_ids: tuple[UUID, ...] = ()
    kind: ClaimKind = ClaimKind.FACTUAL

    def __post_init__(self) -> None:
        if not self.claim_key.strip() or not self.text.strip():
            raise ValueError("Proposed claim key and text cannot be empty")
        if self.kind is ClaimKind.FACTUAL and self.fact_requirement_id is None:
            raise ValueError("Factual claims require a fact requirement")


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    claim_key: str
    text: str
    fact_requirement_id: UUID | None
    kind: ClaimKind
    support: ClaimSupport
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not self.claim_key.strip() or not self.text.strip():
            raise ValueError("Answer claim key and text cannot be empty")
        if self.kind is ClaimKind.FACTUAL and self.fact_requirement_id is None:
            raise ValueError("Factual claims require a fact requirement")
        if self.support in {
            ClaimSupport.GROUNDED,
            ClaimSupport.VERIFIED,
            ClaimSupport.CONFLICTED,
        } and not self.evidence_ids:
            raise ValueError("Supported claims require evidence identifiers")


@dataclass(frozen=True, slots=True)
class Citation:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    answer_claim_id: UUID
    verified_evidence_id: UUID
    document_version_id: UUID
    document_chunk_id: UUID
    ordinal: int
    label: str
    rendered_url: str
    quote: str
    start_offset: int
    end_offset: int

    def __post_init__(self) -> None:
        if self.ordinal < 1 or not self.label.strip() or not self.rendered_url.strip():
            raise ValueError("Citation ordinal, label and URL are required")
        if not self.quote or self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("Citation quote offsets are invalid")
        if self.end_offset - self.start_offset != len(self.quote):
            raise ValueError("Citation quote offsets do not match quote")


@dataclass(frozen=True, slots=True)
class DraftAnswer:
    tenant_id: UUID
    run_id: UUID
    claims: tuple[AnswerClaim, ...]


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    claim_key: str
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ValidatedAnswer:
    tenant_id: UUID
    run_id: UUID
    claims: tuple[AnswerClaim, ...]
    citations: tuple[Citation, ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def factual_traceability_rate(self) -> float:
        factual = tuple(claim for claim in self.claims if claim.kind is ClaimKind.FACTUAL)
        if not factual:
            return 1.0
        cited_claims = {citation.answer_claim_id for citation in self.citations}
        return sum(claim.id in cited_claims for claim in factual) / len(factual)
