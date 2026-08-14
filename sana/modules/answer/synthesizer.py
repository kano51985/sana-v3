"""Convert structured model proposals into claims constrained by coverage state."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sana.modules.answer.domain import (
    AnswerClaim,
    ClaimKind,
    ClaimSupport,
    DraftAnswer,
    ProposedClaim,
)
from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.shared.ids import IdFactory


class ClaimSynthesizer:
    def __init__(self, id_factory: IdFactory) -> None:
        self._ids = id_factory

    def synthesize(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        proposals: tuple[ProposedClaim, ...],
        coverage_by_fact: Mapping[UUID, CoverageAssessment],
    ) -> DraftAnswer:
        claims: list[AnswerClaim] = []
        seen_keys: set[str] = set()
        for proposal in proposals:
            if proposal.claim_key in seen_keys:
                raise ValueError("Answer claim keys must be unique")
            seen_keys.add(proposal.claim_key)
            assessment = (
                coverage_by_fact.get(proposal.fact_requirement_id)
                if proposal.fact_requirement_id is not None
                else None
            )
            if assessment is not None and (
                assessment.tenant_id != tenant_id or assessment.run_id != run_id
            ):
                raise ValueError("Coverage tenant/run does not match draft answer")
            allowed_ids = set(assessment.evidence_ids) if assessment else set()
            evidence_ids = tuple(
                evidence_id
                for evidence_id in dict.fromkeys(proposal.evidence_ids)
                if evidence_id in allowed_ids
            )
            support = self._support(assessment, evidence_ids, proposal.kind)
            claims.append(
                AnswerClaim(
                    id=self._ids.new_uuid(),
                    tenant_id=tenant_id,
                    run_id=run_id,
                    claim_key=proposal.claim_key,
                    text=proposal.text,
                    fact_requirement_id=proposal.fact_requirement_id,
                    kind=proposal.kind,
                    support=support,
                    evidence_ids=evidence_ids,
                )
            )
        return DraftAnswer(tenant_id, run_id, tuple(claims))

    @staticmethod
    def _support(
        assessment: CoverageAssessment | None,
        evidence_ids: tuple[UUID, ...],
        kind: ClaimKind,
    ) -> ClaimSupport:
        if kind is not ClaimKind.FACTUAL:
            return ClaimSupport.UNSUPPORTED
        if assessment is None or not evidence_ids:
            return ClaimSupport.UNSUPPORTED
        if assessment.status is FactCoverage.VERIFIED:
            return ClaimSupport.VERIFIED
        if assessment.status is FactCoverage.PARTIAL:
            return ClaimSupport.CONFLICTED
        if assessment.status is FactCoverage.COVERED:
            return ClaimSupport.GROUNDED
        return ClaimSupport.UNSUPPORTED
