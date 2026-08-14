"""Estimate whether another research round is likely to close a fact gap."""

from __future__ import annotations

from dataclasses import dataclass

from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.search_planning.domain import Consequence, FactRequirement, Freshness


@dataclass(frozen=True, slots=True)
class ExpectedEvidenceGain:
    fact_key: str
    score: float
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1:
            raise ValueError("Expected evidence gain must be between zero and one")


class EvidenceGainEstimator:
    def estimate(
        self,
        fact: FactRequirement,
        assessment: CoverageAssessment,
        *,
        source_novelty: float,
        query_novelty: float,
        official_source_available: bool,
    ) -> ExpectedEvidenceGain:
        if not 0 <= source_novelty <= 1 or not 0 <= query_novelty <= 1:
            raise ValueError("Evidence novelty signals must be between zero and one")
        if assessment.fact_key != fact.key:
            raise ValueError("Coverage assessment does not match fact")
        reasons: list[str] = []
        if assessment.status is FactCoverage.OPEN:
            base = 0.35
            reasons.append("open_required_fact")
        elif assessment.status is FactCoverage.PARTIAL:
            base = 0.45
            reasons.append("conflict_resolution")
        elif assessment.status is FactCoverage.COVERED:
            base = 0.15
            reasons.append("l2_promotion")
        else:
            return ExpectedEvidenceGain(fact.key, 0.0, ("already_verified",))

        score = base + 0.2 * source_novelty + 0.15 * query_novelty
        if official_source_available:
            score += 0.15
            reasons.append("official_source_available")
        if fact.freshness is not Freshness.STABLE:
            score += 0.05
            reasons.append("freshness_value")
        if fact.consequence is Consequence.HIGH:
            score += 0.05
            reasons.append("high_consequence_value")
        if source_novelty >= 0.5:
            reasons.append("novel_source_class")
        if query_novelty >= 0.5:
            reasons.append("novel_query")
        return ExpectedEvidenceGain(fact.key, min(score, 1.0), tuple(reasons))
