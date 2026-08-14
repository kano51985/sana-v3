from dataclasses import replace
from uuid import uuid4

from sana.modules.evidence.coverage import CoverageEvaluator, FactCoverage
from sana.modules.evidence.domain import SourceAuthority, SupportType
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
)

from tests.test_modules.evidence.test_evidence_levels import grounded_evidence


def test_support_and_contradiction_make_fact_partial_and_visible() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    supporting = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="first.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="support",
    )
    contradicting = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="second.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="contradict",
    )
    contradicting = replace(
        contradicting,
        candidate=replace(
            contradicting.candidate,
            support_type=SupportType.CONTRADICTS,
        ),
    )
    fact = FactRequirement(
        key="current-version",
        fact_type=FactType.VERSION,
        description="Current released version",
        subject="current version",
        freshness=Freshness.CURRENT,
        consequence=Consequence.HIGH,
    )

    result = CoverageEvaluator().evaluate(
        tenant_id,
        run_id,
        fact_id,
        fact,
        (supporting, contradicting),
    )

    assert result.status is FactCoverage.PARTIAL
    assert result.supporting_ids == (supporting.id,)
    assert result.contradicting_ids == (contradicting.id,)
    assert result.reason_codes == ("support_contradiction",)
    assert result.requires_research_upgrade is True


def test_stable_low_consequence_conflict_does_not_force_upgrade() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    supporting = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="first.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="stable-support",
    )
    contradicting = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="second.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="stable-contradict",
    )
    contradicting = replace(
        contradicting,
        candidate=replace(
            contradicting.candidate,
            support_type=SupportType.CONTRADICTS,
        ),
    )
    fact = FactRequirement(
        key="history",
        fact_type=FactType.BACKGROUND,
        description="Historical background",
        subject="history",
    )

    result = CoverageEvaluator().evaluate(
        tenant_id,
        run_id,
        fact_id,
        fact,
        (supporting, contradicting),
    )

    assert result.status is FactCoverage.PARTIAL
    assert result.requires_research_upgrade is False


def test_cross_tenant_evidence_cannot_change_coverage() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    foreign = grounded_evidence(
        tenant_id=uuid4(),
        run_id=run_id,
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="foreign",
    )
    fact = FactRequirement(
        key="history",
        fact_type=FactType.BACKGROUND,
        description="Historical background",
        subject="history",
    )

    result = CoverageEvaluator().evaluate(
        tenant_id,
        run_id,
        fact_id,
        fact,
        (foreign,),
    )

    assert result.status is FactCoverage.OPEN
    assert result.evidence_ids == ()
