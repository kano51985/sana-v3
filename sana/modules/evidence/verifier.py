"""Record deterministic verifier decisions without losing source lineage."""

from __future__ import annotations

from datetime import datetime

from sana.modules.evidence.domain import (
    EvidenceCandidate,
    EvidenceVerdict,
    VerifiedEvidence,
)
from sana.modules.shared.ids import IdFactory


class EvidenceVerifier:
    def __init__(self, id_factory: IdFactory, *, verifier_version: str) -> None:
        if not verifier_version.strip():
            raise ValueError("Verifier version cannot be empty")
        self._ids = id_factory
        self._version = verifier_version

    def record(
        self,
        candidate: EvidenceCandidate,
        *,
        verdict: EvidenceVerdict,
        confidence: float,
        reason_codes: tuple[str, ...],
        verified_at: datetime,
    ) -> VerifiedEvidence:
        if not isinstance(candidate, EvidenceCandidate):
            raise TypeError("Verifier requires grounded EvidenceCandidate input")
        return VerifiedEvidence(
            id=self._ids.new_uuid(),
            candidate=candidate,
            verdict=verdict,
            confidence=confidence,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            verifier_version=self._version,
            verified_at=verified_at,
        )
