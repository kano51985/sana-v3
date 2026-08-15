from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sana.modules.shadow_campaign.domain import (
    CampaignLifecycle,
    CampaignStatus,
    GateStatus,
    StopIntent,
)
from sana.modules.shared.errors import InvariantViolation


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def test_campaign_lifecycle_supports_pause_resume_review_and_completion() -> None:
    campaign = CampaignLifecycle(uuid4(), uuid4(), uuid4(), 6, 6)

    campaign.start(NOW)
    campaign.request_stop(StopIntent.PAUSE, "operator pause")
    campaign.settle_stop(NOW + timedelta(minutes=1))
    campaign.resume(NOW + timedelta(minutes=2))
    campaign.await_review(
        NOW + timedelta(minutes=3),
        deadline=NOW + timedelta(days=7),
    )
    campaign.complete(GateStatus.PASS, NOW + timedelta(minutes=4))

    assert campaign.status is CampaignStatus.COMPLETED
    assert campaign.gate_status is GateStatus.PASS
    assert campaign.started_at == NOW
    assert campaign.completed_at == NOW + timedelta(minutes=4)
    assert campaign.version == 6
    with pytest.raises(InvariantViolation, match="Illegal campaign transition"):
        campaign.start(NOW + timedelta(minutes=5))


def test_non_pause_stop_settles_to_aborted_and_terminal_state_is_immutable() -> None:
    campaign = CampaignLifecycle(uuid4(), uuid4(), uuid4(), 6, 6)
    campaign.start(NOW)
    campaign.request_stop(StopIntent.BUDGET, "cost ceiling reached")
    campaign.settle_stop(NOW + timedelta(seconds=1))

    assert campaign.status is CampaignStatus.ABORTED
    assert campaign.stop_intent is StopIntent.BUDGET
    assert campaign.completed_at == NOW + timedelta(seconds=1)
    with pytest.raises(InvariantViolation, match="Illegal campaign transition"):
        campaign.await_review(
            NOW + timedelta(seconds=2),
            deadline=NOW + timedelta(days=1),
        )


def test_operator_abort_can_escalate_an_in_progress_pause_drain() -> None:
    campaign = CampaignLifecycle(uuid4(), uuid4(), uuid4(), 6, 6)
    campaign.start(NOW)
    campaign.request_stop(StopIntent.PAUSE, "operator pause")

    campaign.escalate_stop(StopIntent.ABORT, "operator abort")
    campaign.settle_stop(NOW + timedelta(seconds=1))

    assert campaign.status is CampaignStatus.ABORTED
    assert campaign.stop_intent is StopIntent.ABORT


def test_campaign_cannot_complete_with_a_pending_gate() -> None:
    campaign = CampaignLifecycle(uuid4(), uuid4(), uuid4(), 6, 6)
    campaign.start(NOW)

    with pytest.raises(InvariantViolation, match="final gate decision"):
        campaign.complete(GateStatus.PENDING, NOW + timedelta(seconds=1))


def test_campaign_cannot_start_before_run_plan_is_fully_materialized() -> None:
    campaign = CampaignLifecycle(uuid4(), uuid4(), uuid4(), 6, 0)

    with pytest.raises(InvariantViolation, match="every planned run") as error:
        campaign.start(NOW)

    assert error.value.code == "campaign_not_materialized"
