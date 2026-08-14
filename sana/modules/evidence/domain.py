"""Evidence values preserve the complete path back to fetched source text."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID


class EvidenceLevel(StrEnum):
    L0_DISCOVERY = "L0_DISCOVERY"
    L1_GROUNDED = "L1_GROUNDED"
    L2_VERIFIED = "L2_VERIFIED"


class SupportType(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


class SourceAuthority(StrEnum):
    OFFICIAL = "OFFICIAL"
    INDEPENDENT = "INDEPENDENT"
    UNKNOWN = "UNKNOWN"


class EvidenceVerdict(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DiscoveryEvidence:
    """Unfetched search metadata. It can never become a citation."""

    tenant_id: UUID
    run_id: UUID
    search_hit_id: UUID
    fact_requirement_id: UUID
    url: str
    title: str
    snippet: str
    level: EvidenceLevel = field(default=EvidenceLevel.L0_DISCOVERY, init=False)

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("Discovery evidence URL cannot be empty")


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    document_id: UUID
    document_version_id: UUID
    document_chunk_id: UUID
    url: str
    source_identity: str
    authority: SourceAuthority = SourceAuthority.UNKNOWN

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Evidence source requires an HTTP(S) URL")
        if not self.source_identity.strip():
            raise ValueError("Evidence source identity cannot be empty")
        object.__setattr__(self, "source_identity", self.source_identity.strip().lower())

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    fact_requirement_id: UUID
    source: EvidenceSource
    quote: str
    quote_hash: str
    support_type: SupportType
    candidate_score: float
    start_offset: int
    end_offset: int
    level: EvidenceLevel = field(default=EvidenceLevel.L1_GROUNDED, init=False)

    def __post_init__(self) -> None:
        if not self.quote:
            raise ValueError("Evidence quote cannot be empty")
        if self.quote_hash != hashlib.sha256(self.quote.encode("utf-8")).hexdigest():
            raise ValueError("Evidence quote hash does not match quote")
        if self.start_offset < 0 or self.end_offset <= self.start_offset:
            raise ValueError("Evidence quote offsets are invalid")
        if self.end_offset - self.start_offset != len(self.quote):
            raise ValueError("Evidence quote offsets do not match quote length")
        if not 0 <= self.candidate_score <= 1:
            raise ValueError("Evidence candidate score must be between zero and one")


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    id: UUID
    candidate: EvidenceCandidate
    verdict: EvidenceVerdict
    confidence: float
    reason_codes: tuple[str, ...]
    verifier_version: str
    verified_at: datetime
    level: EvidenceLevel = EvidenceLevel.L1_GROUNDED

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("Evidence confidence must be between zero and one")
        if not self.verifier_version.strip():
            raise ValueError("Evidence verifier version cannot be empty")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("Evidence verification time must be timezone-aware")
        if self.level is EvidenceLevel.L0_DISCOVERY:
            raise ValueError("Discovery evidence cannot be verified")
        if (
            self.level is EvidenceLevel.L2_VERIFIED
            and self.verdict is not EvidenceVerdict.ACCEPTED
        ):
            raise ValueError("Rejected evidence cannot have L2 status")

    @property
    def tenant_id(self) -> UUID:
        return self.candidate.tenant_id

    @property
    def run_id(self) -> UUID:
        return self.candidate.run_id

    @property
    def fact_requirement_id(self) -> UUID:
        return self.candidate.fact_requirement_id

    @property
    def source(self) -> EvidenceSource:
        return self.candidate.source

    @property
    def support_type(self) -> SupportType:
        return self.candidate.support_type
