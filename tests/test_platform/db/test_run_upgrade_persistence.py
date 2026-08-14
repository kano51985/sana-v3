from datetime import datetime, timezone
from uuid import uuid4

from sana.modules.orchestration.domain import RoutingDecision, SearchMode, SearchRun
from sana.modules.orchestration.policy import SearchPolicy
from sana.platform.db.repositories import SqlRunRepository


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class Result:
    rowcount = 1


class CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return Result()


async def test_save_persists_fast_to_research_route_and_budget_snapshot() -> None:
    tenant_id = uuid4()
    policy = SearchPolicy.default()
    run = SearchRun(
        id=uuid4(),
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
    run.mark_persisted()
    run.upgrade_to_research(
        RoutingDecision(
            SearchMode.RESEARCH,
            ("single_or_low_complexity_fact", "fast_value_upgrade"),
            policy.version,
            1.0,
        ),
        policy.snapshot(SearchMode.RESEARCH, NOW),
    )
    session = CapturingSession()

    await SqlRunRepository(session, tenant_id).save(run)  # type: ignore[arg-type]

    params = session.statement.compile().params
    assert params["mode"] == "RESEARCH"
    assert params["route_reason_codes"] == [
        "single_or_low_complexity_fact",
        "fast_value_upgrade",
    ]
    assert params["soft_deadline_at"] == run.budget.soft_deadline_at
    assert params["hard_deadline_at"] == run.budget.hard_deadline_at
    assert params["budget_snapshot"]["hard_deadline_at"] == (
        run.budget.hard_deadline_at.isoformat()
    )
