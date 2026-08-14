"""Fact coverage policy for L0/L1/L2 evidence and visible conflicts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sana.modules.evidence.domain import (
    DiscoveryEvidence,
    EvidenceLevel,
    EvidenceVerdict,
    SourceAuthority,
    SupportType,
    VerifiedEvidence,
)
from sana.modules.search_planning.domain import Consequence, FactRequirement, Freshness


class FactCoverage(StrEnum):
    OPEN = "OPEN"
    COVERED = "COVERED"
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True, slots=True)
class CoverageAssessment:
    tenant_id: UUID
    run_id: UUID
    fact_requirement_id: UUID
    fact_key: str
    status: FactCoverage
    level: EvidenceLevel | None
    evidence_ids: tuple[UUID, ...]
    supporting_ids: tuple[UUID, ...]
    contradicting_ids: tuple[UUID, ...]
    reason_codes: tuple[str, ...]
    discovery_count: int
    requires_research_upgrade: bool


class CoverageEvaluator:
    def evaluate(
        self,
        tenant_id: UUID,
        run_id: UUID,
        fact_requirement_id: UUID,
        fact: FactRequirement,
        evidence: tuple[VerifiedEvidence, ...],
        *,
        discovery: tuple[DiscoveryEvidence, ...] = (),
    ) -> CoverageAssessment:
        accepted_by_id: dict[UUID, VerifiedEvidence] = {}
        for item in evidence:
            if (
                isinstance(item, VerifiedEvidence)
                and item.tenant_id == tenant_id
                and item.run_id == run_id
                and item.fact_requirement_id == fact_requirement_id
                and item.verdict is EvidenceVerdict.ACCEPTED
            ):
                accepted_by_id.setdefault(item.id, item)
        accepted = tuple(accepted_by_id.values())
        supports = tuple(
            item for item in accepted if item.support_type is SupportType.SUPPORTS
        )
        contradicts = tuple(
            item
            for item in accepted
            if item.support_type is SupportType.CONTRADICTS
        )
        discovery_count = sum(
            1
            for item in discovery
            if item.fact_requirement_id == fact_requirement_id
            and item.tenant_id == tenant_id
            and item.run_id == run_id
        )
        if supports and contradicts:
            high_value = (
                fact.freshness is not Freshness.STABLE
                or fact.consequence is Consequence.HIGH
            )
            return CoverageAssessment(
                tenant_id,
                run_id,
                fact_requirement_id,
                fact.key,
                FactCoverage.PARTIAL,
                EvidenceLevel.L1_GROUNDED,
                tuple(item.id for item in accepted),
                tuple(item.id for item in supports),
                tuple(item.id for item in contradicts),
                ("support_contradiction",),
                discovery_count,
                high_value,
            )
        if not accepted:
            return CoverageAssessment(
                tenant_id,
                run_id,
                fact_requirement_id,
                fact.key,
                FactCoverage.OPEN,
                None,
                (),
                (),
                (),
                ("no_grounded_evidence",),
                discovery_count,
                False,
            )

        official = any(
            item.source.authority is SourceAuthority.OFFICIAL for item in accepted
        )
        independent_sources = {
            item.source.source_identity
            for item in accepted
            if item.source.authority is SourceAuthority.INDEPENDENT
        }
        is_l2 = official or len(independent_sources) >= 2
        return CoverageAssessment(
            tenant_id,
            run_id,
            fact_requirement_id,
            fact.key,
            FactCoverage.VERIFIED if is_l2 else FactCoverage.COVERED,
            EvidenceLevel.L2_VERIFIED if is_l2 else EvidenceLevel.L1_GROUNDED,
            tuple(item.id for item in accepted),
            tuple(item.id for item in supports),
            tuple(item.id for item in contradicts),
            (
                ("official_source",)
                if official
                else ("two_independent_sources",)
                if is_l2
                else ("single_grounded_source",)
            ),
            discovery_count,
            False,
        )
