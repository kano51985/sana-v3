from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.orchestration.domain import BudgetUsage, SearchMode
from sana.modules.orchestration.policy import (
    BudgetExceeded,
    BudgetGuard,
    BudgetPhase,
    SearchPolicy,
)


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def test_fast_and_research_deadlines_match_product_targets() -> None:
    policy = SearchPolicy.default()
    fast = policy.snapshot(SearchMode.FAST, NOW)
    research = policy.snapshot(SearchMode.RESEARCH, NOW)

    assert fast.soft_deadline_at == NOW + timedelta(seconds=14)
    assert fast.hard_deadline_at == NOW + timedelta(seconds=15)
    assert research.hard_deadline_at == NOW + timedelta(seconds=120)


def test_discovery_cannot_consume_synthesis_reserve() -> None:
    snapshot = SearchPolicy.default().snapshot(SearchMode.FAST, NOW)
    guard = BudgetGuard(snapshot)

    assert guard.non_synthesis_deadline == NOW + timedelta(seconds=13)
    assert guard.can_start(BudgetPhase.DISCOVERY, NOW + timedelta(seconds=12.5))
    assert not guard.can_start(BudgetPhase.DISCOVERY, NOW + timedelta(seconds=13))
    assert guard.can_start(BudgetPhase.SYNTHESIZE, NOW + timedelta(seconds=14))


def test_counter_and_phase_limits_raise_typed_budget_error() -> None:
    snapshot = SearchPolicy.default().snapshot(SearchMode.FAST, NOW)
    guard = BudgetGuard(snapshot)

    with pytest.raises(BudgetExceeded):
        guard.validate(BudgetUsage(query_count=5))
    with pytest.raises(BudgetExceeded):
        guard.validate(BudgetUsage(phase_seconds={"discovery": 4.3}))


def test_usage_addition_is_immutable() -> None:
    initial = BudgetUsage()
    updated = initial.add(queries=2, phase="discovery", elapsed_seconds=1.5)

    assert initial.query_count == 0
    assert updated.query_count == 2
    assert updated.phase_seconds["discovery"] == 1.5
