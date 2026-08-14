"""Final gate: only accepted grounded evidence may produce citations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sana.modules.answer.domain import (
    AnswerClaim,
    Citation,
    ClaimKind,
    ClaimSupport,
    DraftAnswer,
    UnsupportedClaimPolicy,
    ValidatedAnswer,
    ValidationIssue,
)
from sana.modules.evidence.domain import (
    EvidenceVerdict,
    SourceAuthority,
    SupportType,
    VerifiedEvidence,
)
from sana.modules.shared.ids import IdFactory


class CitationValidator:
    def __init__(self, id_factory: IdFactory) -> None:
        self._ids = id_factory

    def validate(
        self,
        draft: DraftAnswer,
        evidence_by_id: Mapping[UUID, object],
        *,
        unsupported_policy: UnsupportedClaimPolicy = UnsupportedClaimPolicy.MARK_UNCONFIRMED,
    ) -> ValidatedAnswer:
        claims: list[AnswerClaim] = []
        citations: list[Citation] = []
        issues: list[ValidationIssue] = []
        citation_label = 0
        for claim in draft.claims:
            if claim.tenant_id != draft.tenant_id or claim.run_id != draft.run_id:
                raise ValueError("Claim tenant/run does not match draft answer")
            if claim.kind is not ClaimKind.FACTUAL:
                claims.append(claim)
                continue
            valid = self._valid_evidence(claim, evidence_by_id)
            invalid_count = len(claim.evidence_ids) - len(valid)
            if invalid_count:
                issues.append(
                    ValidationIssue(
                        claim.claim_key,
                        "invalid_evidence_mapping",
                        f"Removed {invalid_count} invalid evidence mapping(s)",
                    )
                )
            if not valid:
                replacement = self._handle_unsupported(
                    claim,
                    unsupported_policy,
                    issues,
                )
                if replacement is not None:
                    claims.append(replacement)
                continue

            validated_claim = replace(
                claim,
                evidence_ids=tuple(item.id for item in valid),
                support=self._validated_support(valid),
            )
            if validated_claim.support is not claim.support:
                issues.append(
                    ValidationIssue(
                        claim.claim_key,
                        "support_recalculated",
                        f"Support changed from {claim.support.value} "
                        f"to {validated_claim.support.value}",
                    )
                )
            claims.append(validated_claim)
            for ordinal, evidence in enumerate(valid, start=1):
                citation_label += 1
                candidate = evidence.candidate
                citations.append(
                    Citation(
                        id=self._ids.new_uuid(),
                        tenant_id=draft.tenant_id,
                        run_id=draft.run_id,
                        answer_claim_id=claim.id,
                        verified_evidence_id=evidence.id,
                        document_version_id=candidate.source.document_version_id,
                        document_chunk_id=candidate.source.document_chunk_id,
                        ordinal=ordinal,
                        label=f"[{citation_label}]",
                        rendered_url=candidate.source.url,
                        quote=candidate.quote,
                        start_offset=candidate.start_offset,
                        end_offset=candidate.end_offset,
                    )
                )
        return ValidatedAnswer(
            draft.tenant_id,
            draft.run_id,
            tuple(claims),
            tuple(citations),
            tuple(issues),
        )

    @staticmethod
    def _valid_evidence(
        claim: AnswerClaim,
        evidence_by_id: Mapping[UUID, object],
    ) -> tuple[VerifiedEvidence, ...]:
        valid: list[VerifiedEvidence] = []
        for evidence_id in dict.fromkeys(claim.evidence_ids):
            evidence = evidence_by_id.get(evidence_id)
            if not isinstance(evidence, VerifiedEvidence):
                continue
            if evidence.id != evidence_id:
                continue
            if evidence.verdict is not EvidenceVerdict.ACCEPTED:
                continue
            if evidence.tenant_id != claim.tenant_id or evidence.run_id != claim.run_id:
                continue
            if evidence.fact_requirement_id != claim.fact_requirement_id:
                continue
            valid.append(evidence)
        return tuple(valid)

    @staticmethod
    def _validated_support(
        evidence: tuple[VerifiedEvidence, ...],
    ) -> ClaimSupport:
        directions = {item.support_type for item in evidence}
        if directions == {SupportType.SUPPORTS, SupportType.CONTRADICTS}:
            return ClaimSupport.CONFLICTED
        if any(item.source.authority is SourceAuthority.OFFICIAL for item in evidence):
            return ClaimSupport.VERIFIED
        independent_sources = {
            item.source.source_identity
            for item in evidence
            if item.source.authority is SourceAuthority.INDEPENDENT
        }
        if len(independent_sources) >= 2:
            return ClaimSupport.VERIFIED
        return ClaimSupport.GROUNDED

    @staticmethod
    def _handle_unsupported(
        claim: AnswerClaim,
        policy: UnsupportedClaimPolicy,
        issues: list[ValidationIssue],
    ) -> AnswerClaim | None:
        issues.append(
            ValidationIssue(
                claim.claim_key,
                "unsupported_factual_claim",
                f"Applied unsupported claim policy: {policy.value}",
            )
        )
        if policy is UnsupportedClaimPolicy.DROP:
            return None
        prefix = (
            "尚未确认："
            if policy is UnsupportedClaimPolicy.MARK_UNCONFIRMED
            else "现有来源仅提示、尚不能确认："
        )
        return replace(
            claim,
            text=prefix + claim.text,
            kind=ClaimKind.UNCERTAINTY,
            support=ClaimSupport.UNCONFIRMED,
            evidence_ids=(),
        )
