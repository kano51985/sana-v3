"""Model prose generation constrained by deterministic claims and citations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sana.modules.answer.citation_validator import CitationValidator
from sana.modules.answer.domain import (
    ClaimKind,
    ProposedClaim,
    UnsupportedClaimPolicy,
    ValidatedAnswer,
)
from sana.modules.answer.synthesizer import ClaimSynthesizer
from sana.modules.evidence.coverage import CoverageAssessment
from sana.modules.evidence.domain import EvidenceVerdict, VerifiedEvidence
from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelMessage,
    ModelResult,
    ModelRole,
)
from sana.modules.search_planning.domain import FactRequirement
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import DeterministicIdFactory


class SynthesisGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


@dataclass(frozen=True, slots=True)
class ConstrainedSynthesisResult:
    answer: ValidatedAnswer
    degraded: bool
    degradation_code: str | None = None


class ProposedClaimParser:
    _FIELDS = frozenset({"claim_key", "text", "fact_id", "evidence_ids"})
    _NUMERIC_IDENTIFIER = re.compile(r"\d+(?:[.-]\d+)*")

    def __init__(
        self,
        facts: dict[UUID, FactRequirement],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> None:
        self._facts = facts
        self._evidence_fact = {
            item.id: item.fact_requirement_id
            for item in evidence
            if item.verdict is EvidenceVerdict.ACCEPTED
        }
        self._evidence_quote = {
            item.id: item.candidate.quote
            for item in evidence
            if item.verdict is EvidenceVerdict.ACCEPTED
        }

    @classmethod
    def _numeric_identifiers(cls, fact: FactRequirement) -> tuple[str, ...]:
        values = cls._NUMERIC_IDENTIFIER.findall(
            f"{fact.key} {fact.description} {fact.subject}"
        )
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _contains_identifier(text: str, identifier: str) -> bool:
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])",
                text,
                re.I,
            )
        )

    def _self_contained_text(
        self,
        fact: FactRequirement,
        claim_text: str,
        evidence_ids: tuple[UUID, ...],
    ) -> str:
        missing = tuple(
            identifier
            for identifier in self._numeric_identifiers(fact)
            if not self._contains_identifier(claim_text, identifier)
        )
        if not missing:
            return claim_text
        cited_quotes = tuple(self._evidence_quote[value] for value in evidence_ids)
        if any(
            not any(
                self._contains_identifier(quote, identifier)
                for quote in cited_quotes
            )
            for identifier in missing
        ):
            raise ValueError(
                "claim omitted a numeric identifier not grounded by citations"
            )
        normalized = f"{', '.join(missing)}: {claim_text}"
        if len(normalized) > 1_200:
            raise ValueError("self-contained claim is too long")
        return normalized

    def parse(self, text: str) -> tuple[ProposedClaim, ...]:
        payload = json.loads(text)
        claims = payload["claims"]
        if not isinstance(claims, list) or len(claims) > len(self._facts) * 2:
            raise ValueError("claims must be a bounded list")
        parsed: list[ProposedClaim] = []
        seen: set[str] = set()
        for raw in claims:
            if not isinstance(raw, dict) or set(raw) - self._FIELDS:
                raise ValueError("claim contains unsupported fields")
            claim_key = str(raw["claim_key"])
            if not claim_key.strip() or claim_key in seen:
                raise ValueError("claim keys must be non-empty and unique")
            fact_id = UUID(str(raw["fact_id"]))
            if fact_id not in self._facts:
                raise ValueError("claim references an unknown fact")
            claim_text = str(raw["text"]).strip()
            if not claim_text or len(claim_text) > 1_200:
                raise ValueError("claim text is empty or too long")
            evidence_ids = tuple(
                dict.fromkeys(UUID(str(value)) for value in raw.get("evidence_ids", ()))
            )
            if not evidence_ids:
                raise ValueError("factual claim must reference accepted evidence")
            if any(self._evidence_fact.get(value) != fact_id for value in evidence_ids):
                raise ValueError("claim references unavailable or cross-fact evidence")
            claim_text = self._self_contained_text(
                self._facts[fact_id],
                claim_text,
                evidence_ids,
            )
            parsed.append(
                ProposedClaim(
                    claim_key,
                    claim_text,
                    fact_id,
                    evidence_ids,
                    ClaimKind.FACTUAL,
                )
            )
            seen.add(claim_key)
        expected_fact_ids = set(self._evidence_fact.values())
        claimed_fact_ids = {
            item.fact_requirement_id for item in parsed if item.evidence_ids
        }
        if not expected_fact_ids <= claimed_fact_ids:
            raise ValueError("claims omitted facts with accepted evidence")
        return tuple(parsed)

    def repair_instruction(self, error: Exception) -> str:
        return (
            "Return only JSON with a claims array. Each claim may contain only "
            "claim_key, text, fact_id, and evidence_ids. Use only supplied IDs; do "
            "not emit URLs, citation labels, support status, or answer quality. Emit "
            "at least one claim with evidence_ids for every fact that has accepted "
            "evidence. "
            f"Validation error: {error}"
        )


class ConstrainedModelSynthesizer:
    def __init__(self, gateway: SynthesisGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def _messages(
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> tuple[ModelMessage, ...]:
        payload = {
            "facts": [
                {
                    "fact_id": str(fact_id),
                    "key": fact.key,
                    "description": fact.description,
                    "coverage": coverage[fact_id].status.value,
                    "evidence_ids": [
                        str(value) for value in coverage[fact_id].evidence_ids
                    ],
                }
                for fact_id, fact in facts.items()
            ],
            "evidence": [
                {
                    "evidence_id": str(item.id),
                    "fact_id": str(item.fact_requirement_id),
                    "quote": item.candidate.quote,
                }
                for item in evidence
                if item.verdict is EvidenceVerdict.ACCEPTED
            ],
        }
        return (
            ModelMessage(
                MessageRole.SYSTEM,
                "Write concise factual claims using only supplied accepted evidence. "
                "Keep each claim self-contained by preserving requested numeric "
                "identifiers when they also appear in its cited evidence. "
                "Return one JSON object with a claims array. Every claim must contain "
                "only claim_key, text, fact_id, and evidence_ids, using exact supplied "
                "IDs. Never emit URLs, citation labels, support labels, or answer "
                "quality. Return an empty claims array when no accepted evidence "
                "supports a fact.",
            ),
            ModelMessage(
                MessageRole.USER,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _fallback(
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> tuple[ProposedClaim, ...]:
        by_id = {item.id: item for item in evidence}
        proposals: list[ProposedClaim] = []
        for fact_id, fact in facts.items():
            evidence_ids = tuple(
                value for value in coverage[fact_id].supporting_ids if value in by_id
            )
            if not evidence_ids:
                continue
            quote = by_id[evidence_ids[0]].candidate.quote.replace("\n", " ").strip()
            proposals.append(
                ProposedClaim(
                    fact.key,
                    quote[:1_200],
                    fact_id,
                    evidence_ids,
                    ClaimKind.FACTUAL,
                )
            )
        return tuple(proposals)

    async def synthesize(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
        invocation_context: ModelInvocationContext,
        deadline: datetime,
    ) -> ConstrainedSynthesisResult:
        if not any(item.verdict is EvidenceVerdict.ACCEPTED for item in evidence):
            return self.deterministic(
                tenant_id=tenant_id,
                run_id=run_id,
                facts=facts,
                coverage=coverage,
                evidence=evidence,
            )
        parser = ProposedClaimParser(facts, evidence)
        degraded = False
        degradation_code = None
        try:
            result = await self._gateway.generate(
                ModelRole.SYNTHESIZER,
                self._messages(facts, coverage, evidence),
                deadline=deadline,
                budget=ModelCallBudget(2, 24_000),
                parser=parser,
                invocation_context=invocation_context,
            )
            if not isinstance(result.parsed, tuple):
                raise TypeError("Synthesizer output did not contain parsed claims")
            proposals = result.parsed
        except (TypedError, ValueError, TypeError, KeyError):
            proposals = self._fallback(facts, coverage, evidence)
            degraded = True
            degradation_code = "model_synthesizer_fallback"

        draft = ClaimSynthesizer(
            DeterministicIdFactory(f"{run_id}:answer-claims")
        ).synthesize(
            tenant_id=tenant_id,
            run_id=run_id,
            proposals=proposals,
            coverage_by_fact=coverage,
        )
        validated = CitationValidator(
            DeterministicIdFactory(f"{run_id}:citations")
        ).validate(
            draft,
            {item.id: item for item in evidence},
            unsupported_policy=UnsupportedClaimPolicy.MARK_UNCONFIRMED,
        )
        return ConstrainedSynthesisResult(
            validated,
            degraded,
            degradation_code,
        )

    @classmethod
    def deterministic(
        cls,
        *,
        tenant_id: UUID,
        run_id: UUID,
        facts: dict[UUID, FactRequirement],
        coverage: dict[UUID, CoverageAssessment],
        evidence: tuple[VerifiedEvidence, ...],
    ) -> ConstrainedSynthesisResult:
        proposals = cls._fallback(facts, coverage, evidence)
        draft = ClaimSynthesizer(
            DeterministicIdFactory(f"{run_id}:answer-claims")
        ).synthesize(
            tenant_id=tenant_id,
            run_id=run_id,
            proposals=proposals,
            coverage_by_fact=coverage,
        )
        validated = CitationValidator(
            DeterministicIdFactory(f"{run_id}:citations")
        ).validate(
            draft,
            {item.id: item for item in evidence},
            unsupported_policy=UnsupportedClaimPolicy.MARK_UNCONFIRMED,
        )
        return ConstrainedSynthesisResult(validated, False)


__all__ = [
    "ConstrainedModelSynthesizer",
    "ConstrainedSynthesisResult",
    "ProposedClaimParser",
]
