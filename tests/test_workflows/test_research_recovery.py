from sana.modules.evidence.coverage import FactCoverage
from sana.modules.evidence.evidence_gain import EvidenceGainEstimator
from sana.modules.orchestration.domain import SearchMode, StepStatus
from sana.modules.orchestration.research_workflow import ResearchWorkflow
from sana.modules.search_planning.domain import FactRequirement, FactType, NormalizedIntent
from sana.modules.search_planning.expansion import ExpansionPlanner
from sana.modules.search_planning.query_compiler import QueryCompiler

from tests.test_workflows.test_research_search import assessment


def test_recovery_adds_only_missing_new_revision_steps() -> None:
    facts = tuple(
        FactRequirement(key, fact_type, key, subject)
        for key, fact_type, subject in (
            ("version", FactType.VERSION, "current version"),
            ("patch", FactType.PATCH_NOTES, "patch notes"),
            ("meta", FactType.TEAM_META, "team meta"),
        )
    )
    intent = NormalizedIntent("Apex Legends", (), "en", facts)
    initial = QueryCompiler().compile(intent, SearchMode.RESEARCH)
    gap = facts[-1]
    gain = EvidenceGainEstimator().estimate(
        gap,
        assessment(gap, FactCoverage.OPEN),
        source_novelty=1,
        query_novelty=1,
        official_source_available=True,
    )
    decision = ExpansionPlanner().plan(
        intent,
        current_revision=1,
        completed_expansion_rounds=0,
        gap_fact_keys=frozenset({gap.key}),
        gains=(gain,),
        existing_queries=initial,
    )
    assert decision.steps
    completed_new = decision.steps[0]
    persisted = {
        (1, "discover:q:1:version:1"): StepStatus.SUCCEEDED,
        (completed_new.plan_revision, completed_new.step_key): StepStatus.SUCCEEDED,
    }

    missing = ResearchWorkflow.recover_new_steps(decision, persisted)

    assert completed_new not in missing
    assert all(step.plan_revision == 2 for step in missing)
    assert all(not step.step_key.startswith("discover:q:1:") for step in missing)
    assert ResearchWorkflow.recover_new_steps(decision, persisted) == missing
