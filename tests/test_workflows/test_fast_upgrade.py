from datetime import datetime, timezone
from uuid import UUID, uuid4

from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.evidence.domain import EvidenceLevel
from sana.modules.orchestration.domain import (
    RoutingDecision,
    SearchMode,
    SearchRun,
)
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.orchestration.research_workflow import (
    FastUpgradePolicy,
    ResearchWorkflow,
)
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def assessment(
    tenant_id: UUID,
    run_id: UUID,
    fact_id: UUID,
    fact_key: str,
    status: FactCoverage,
) -> CoverageAssessment:
    return CoverageAssessment(
        tenant_id,
        run_id,
        fact_id,
        fact_key,
        status,
        EvidenceLevel.L1_GROUNDED if status is not FactCoverage.OPEN else None,
        (),
        (),
        (),
        (),
        0,
        status is FactCoverage.PARTIAL,
    )


def fast_run(tenant_id: UUID, run_id: UUID) -> SearchRun:
    policy = SearchPolicy.default()
    run = SearchRun(
        id=run_id,
        tenant_id=tenant_id,
        conversation_id=uuid4(),
        message_id=uuid4(),
        response_run_id=uuid4(),
        routing=RoutingDecision(
            SearchMode.FAST,
            ("single_or_low_complexity_fact",),
            policy.version,
            0.8,
        ),
        budget=policy.snapshot(SearchMode.FAST, NOW),
    )
    run.start(NOW)
    return run


def test_strong_freshness_gap_upgrades_same_run_to_research_budget() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    fact = FactRequirement(
        key="current-version",
        fact_type=FactType.VERSION,
        description="Current version",
        subject="current version",
        freshness=Freshness.CURRENT,
    )
    intent = NormalizedIntent("Apex Legends", (), "en", (fact,))
    decision = FastUpgradePolicy().evaluate(
        tenant_id=tenant_id,
        run_id=run_id,
        intent=intent,
        fact_ids={fact.key: fact_id},
        coverage={},
    )
    run = fast_run(tenant_id, run_id)

    ResearchWorkflow().upgrade_fast_run(run, decision, SearchPolicy.default())

    assert decision.should_upgrade is True
    assert decision.reason_codes == ("strong_freshness_gap",)
    assert run.mode is SearchMode.RESEARCH
    assert run.budget.created_at == NOW
    assert (run.budget.hard_deadline_at - NOW).total_seconds() == 120
    assert "fast_value_upgrade" in run.routing.reason_codes


def test_ordinary_stable_low_risk_gap_does_not_upgrade() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    fact = FactRequirement(
        key="background",
        fact_type=FactType.BACKGROUND,
        description="Background",
        subject="background",
    )
    decision = FastUpgradePolicy().evaluate(
        tenant_id=tenant_id,
        run_id=run_id,
        intent=NormalizedIntent("Apex Legends", (), "en", (fact,)),
        fact_ids={fact.key: fact_id},
        coverage={},
    )

    assert decision.should_upgrade is False
    assert decision.gap_fact_ids == (fact_id,)
    assert decision.reason_codes == ()


def test_conflict_and_high_consequence_l1_are_upgrade_reasons() -> None:
    tenant_id, run_id = uuid4(), uuid4()
    conflict_id, safety_id = uuid4(), uuid4()
    conflict = FactRequirement(
        key="release",
        fact_type=FactType.VERSION,
        description="Release version",
        subject="release version",
    )
    safety = FactRequirement(
        key="safety",
        fact_type=FactType.CURRENT_VALUE,
        description="Medical safety",
        subject="medical safety",
        consequence=Consequence.HIGH,
    )
    intent = NormalizedIntent("Example medicine", (), "en", (conflict, safety))
    decision = FastUpgradePolicy().evaluate(
        tenant_id=tenant_id,
        run_id=run_id,
        intent=intent,
        fact_ids={conflict.key: conflict_id, safety.key: safety_id},
        coverage={
            conflict_id: assessment(
                tenant_id, run_id, conflict_id, conflict.key, FactCoverage.PARTIAL
            ),
            safety_id: assessment(
                tenant_id, run_id, safety_id, safety.key, FactCoverage.COVERED
            ),
        },
    )

    assert decision.should_upgrade is True
    assert set(decision.reason_codes) == {
        "evidence_conflict",
        "high_consequence_gap",
    }


def test_explicit_complete_sources_requires_l2_or_upgrades() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    fact = FactRequirement(
        key="sources",
        fact_type=FactType.BACKGROUND,
        description="Complete source record",
        subject="source record",
    )
    intent = NormalizedIntent(
        "Apex Legends",
        (),
        "en",
        (fact,),
        requires_complete_sources=True,
    )

    decision = FastUpgradePolicy().evaluate(
        tenant_id=tenant_id,
        run_id=run_id,
        intent=intent,
        fact_ids={fact.key: fact_id},
        coverage={
            fact_id: assessment(
                tenant_id,
                run_id,
                fact_id,
                fact.key,
                FactCoverage.COVERED,
            )
        },
    )

    assert decision.should_upgrade is True
    assert decision.reason_codes == ("complete_coverage_gap",)
