"""Batch model verification constrained by deterministic evidence gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from sana.modules.evidence.builder import EvidenceBuilder
from sana.modules.evidence.candidate_selector import SelectedCandidate
from sana.modules.evidence.domain import (
    EvidenceCandidate,
    EvidenceSource,
    EvidenceVerdict,
    SourceAuthority,
    SupportType,
    VerifiedEvidence,
)
from sana.modules.evidence.verifier import EvidenceVerifier
from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelMessage,
    ModelResult,
    ModelRole,
)
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import DeterministicIdFactory


_ALLOWED_REASON_CODES = frozenset(
    {
        "direct_support",
        "direct_contradiction",
        "current_source",
        "explicit_value",
        "definition_match",
        "context_match",
    }
)


class VerificationGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


@dataclass(frozen=True, slots=True)
class ProposedVerification:
    fact_id: UUID
    candidate_id: UUID
    support_type: SupportType
    quote: str
    confidence: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedBatch:
    evidence: tuple[VerifiedEvidence, ...]
    degraded: bool
    degradation_code: str | None = None


class VerificationParser:
    def __init__(self, candidates: tuple[SelectedCandidate, ...]) -> None:
        self._candidates = {item.id: item for item in candidates}

    def parse(self, text: str) -> tuple[ProposedVerification, ...]:
        payload = json.loads(text)
        verdicts = payload["verdicts"]
        if not isinstance(verdicts, list) or len(verdicts) > len(self._candidates):
            raise ValueError("verdicts must be a bounded list")
        parsed: list[ProposedVerification] = []
        seen: set[UUID] = set()
        for raw in verdicts:
            if not isinstance(raw, dict):
                raise ValueError("verdict must be an object")
            candidate_id = UUID(str(raw["candidate_id"]))
            fact_id = UUID(str(raw["fact_id"]))
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.fact_id != fact_id:
                raise ValueError("verdict references an unknown candidate/fact pair")
            if candidate_id in seen:
                continue
            quote = str(raw["quote"])
            if not quote or len(quote) > 600 or quote not in candidate.chunk.text:
                raise ValueError("verdict quote is not an exact candidate span")
            confidence = float(raw["confidence"])
            if not 0 <= confidence <= 1:
                raise ValueError("verdict confidence is out of range")
            reasons = tuple(dict.fromkeys(map(str, raw.get("reason_codes", ()))))
            if not reasons or set(reasons) - _ALLOWED_REASON_CODES:
                raise ValueError("verdict contains unsupported reason codes")
            parsed.append(
                ProposedVerification(
                    fact_id,
                    candidate_id,
                    SupportType(str(raw["support_type"])),
                    quote,
                    confidence,
                    reasons,
                )
            )
            seen.add(candidate_id)
        return tuple(parsed)

    def repair_instruction(self, error: Exception) -> str:
        return (
            "Return only JSON with a verdicts array. Use only supplied fact_id and "
            "candidate_id values, an exact quote, SUPPORTS or CONTRADICTS, confidence "
            "0..1, and allowlisted reason_codes. Omit unsupported candidates. "
            f"Validation error: {error}"
        )


class ModelEvidenceVerifier:
    def __init__(self, gateway: VerificationGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def _messages(candidates: tuple[SelectedCandidate, ...]) -> tuple[ModelMessage, ...]:
        payload = {
            "candidates": [
                {
                    "fact_id": str(item.fact_id),
                    "candidate_id": str(item.id),
                    "fact": item.fact.description,
                    "quote": item.quote,
                }
                for item in candidates
            ],
            "allowed_reason_codes": sorted(_ALLOWED_REASON_CODES),
        }
        return (
            ModelMessage(
                MessageRole.SYSTEM,
                "Judge factual support using only supplied exact excerpts. Return one "
                "JSON object with a verdicts array. Every verdict must contain only "
                "fact_id, candidate_id, support_type, quote, confidence, and "
                "reason_codes. Use exact supplied IDs and quote spans; support_type is "
                "SUPPORTS or CONTRADICTS. Do not infer source authority and omit "
                "candidates that do not directly support or contradict the fact.",
            ),
            ModelMessage(
                MessageRole.USER,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _record(
        candidate: SelectedCandidate,
        proposed: ProposedVerification,
        *,
        run_id: UUID,
        verified_at: datetime,
        verifier_version: str,
    ) -> VerifiedEvidence:
        quote_start = candidate.chunk.text.index(proposed.quote)
        builder = EvidenceBuilder(
            DeterministicIdFactory(f"{run_id}:{candidate.id}:candidate")
        )
        grounded = builder.build(
            tenant_id=candidate.version.tenant_id,
            run_id=run_id,
            fact_requirement_id=candidate.fact_id,
            document_version=candidate.version,
            document_chunk_id=candidate.chunk_id,
            document_chunk=candidate.chunk,
            document_id=candidate.document_id,
            source_url=candidate.url,
            source_identity=candidate.source_identity,
            support_type=proposed.support_type,
            quote=proposed.quote,
            quote_start_in_chunk=quote_start,
            candidate_score=candidate.score,
            authority=candidate.source_authority,
        )
        grounded = replace(grounded, id=candidate.id)
        verified = EvidenceVerifier(
            DeterministicIdFactory(f"{run_id}:{candidate.id}:verified"),
            verifier_version=verifier_version,
        ).record(
            grounded,
            verdict=EvidenceVerdict.ACCEPTED,
            confidence=proposed.confidence,
            reason_codes=("exact_source_span", *proposed.reason_codes),
            verified_at=verified_at,
        )
        return replace(verified, id=uuid5(candidate.id, f"verified:{verifier_version}"))

    @staticmethod
    def _fallback(
        candidates: tuple[SelectedCandidate, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
    ) -> tuple[VerifiedEvidence, ...]:
        evidence: list[VerifiedEvidence] = []
        for candidate in candidates:
            subject = candidate.fact.subject.casefold()
            folded = candidate.quote.casefold()
            if candidate.score <= 0 or (subject not in folded and len(subject) > 2):
                continue
            proposed = ProposedVerification(
                candidate.fact_id,
                candidate.id,
                SupportType.SUPPORTS,
                candidate.quote,
                min(0.49, max(0.2, candidate.score)),
                ("context_match",),
            )
            evidence.append(
                ModelEvidenceVerifier._record(
                    candidate,
                    proposed,
                    run_id=run_id,
                    verified_at=verified_at,
                    verifier_version="lexical-fallback-v1",
                )
            )
        return tuple(evidence)

    async def verify(
        self,
        candidates: tuple[SelectedCandidate, ...],
        *,
        invocation_context: ModelInvocationContext,
        deadline: datetime,
        verified_at: datetime,
    ) -> VerifiedBatch:
        if not candidates:
            return VerifiedBatch((), False)
        parser = VerificationParser(candidates)
        try:
            result = await self._gateway.generate(
                ModelRole.VERIFIER,
                self._messages(candidates),
                deadline=deadline,
                budget=ModelCallBudget(2, 24_000),
                parser=parser,
                invocation_context=invocation_context,
            )
            if not isinstance(result.parsed, tuple):
                raise TypeError("Verifier output did not contain parsed verdicts")
            by_id = {candidate.id: candidate for candidate in candidates}
            evidence = tuple(
                self._record(
                    by_id[item.candidate_id],
                    item,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                    verifier_version="deepseek-verifier-v1",
                )
                for item in result.parsed
            )
            return VerifiedBatch(evidence, False)
        except (TypedError, ValueError, TypeError, KeyError):
            return VerifiedBatch(
                self._fallback(
                    candidates,
                    run_id=invocation_context.run_id,
                    verified_at=verified_at,
                ),
                True,
                "model_verifier_fallback",
            )

    @classmethod
    def deterministic(
        cls,
        candidates: tuple[SelectedCandidate, ...],
        *,
        run_id: UUID,
        verified_at: datetime,
    ) -> VerifiedBatch:
        return VerifiedBatch(
            cls._fallback(
                candidates,
                run_id=run_id,
                verified_at=verified_at,
            ),
            False,
        )


def evidence_to_payload(evidence: VerifiedEvidence) -> dict[str, Any]:
    candidate = evidence.candidate
    return {
        "candidate_id": str(candidate.id),
        "verified_id": str(evidence.id),
        "fact_id": str(candidate.fact_requirement_id),
        "document_id": str(candidate.source.document_id),
        "document_version_id": str(candidate.source.document_version_id),
        "document_chunk_id": str(candidate.source.document_chunk_id),
        "quote": candidate.quote,
        "quote_hash": candidate.quote_hash,
        "start_offset": candidate.start_offset,
        "end_offset": candidate.end_offset,
        "support_type": candidate.support_type.value,
        "candidate_score": candidate.candidate_score,
        "source_identity": candidate.source.source_identity,
        "source_authority": candidate.source.authority.value,
        "verdict": evidence.verdict.value,
        "confidence": evidence.confidence,
        "reason_codes": list(evidence.reason_codes),
        "verifier_version": evidence.verifier_version,
        "verified_at": evidence.verified_at.isoformat(),
        "url": candidate.source.url,
    }


def evidence_from_payload(
    payload: dict[str, Any],
    *,
    tenant_id: UUID,
    run_id: UUID,
) -> VerifiedEvidence:
    source = EvidenceSource(
        UUID(str(payload["document_id"])),
        UUID(str(payload["document_version_id"])),
        UUID(str(payload["document_chunk_id"])),
        str(payload["url"]),
        str(payload["source_identity"]),
        SourceAuthority(str(payload["source_authority"])),
    )
    candidate = EvidenceCandidate(
        UUID(str(payload["candidate_id"])),
        tenant_id,
        run_id,
        UUID(str(payload["fact_id"])),
        source,
        str(payload["quote"]),
        str(payload["quote_hash"]),
        SupportType(str(payload["support_type"])),
        float(payload["candidate_score"]),
        int(payload["start_offset"]),
        int(payload["end_offset"]),
    )
    return VerifiedEvidence(
        UUID(str(payload["verified_id"])),
        candidate,
        EvidenceVerdict(str(payload["verdict"])),
        float(payload["confidence"]),
        tuple(map(str, payload["reason_codes"])),
        str(payload["verifier_version"]),
        datetime.fromisoformat(str(payload["verified_at"])),
    )


__all__ = [
    "ModelEvidenceVerifier",
    "ProposedVerification",
    "VerificationParser",
    "VerifiedBatch",
    "evidence_from_payload",
    "evidence_to_payload",
]
