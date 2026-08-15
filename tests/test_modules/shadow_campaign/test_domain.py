from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ErrorClass,
    GateStatus,
    ReservationState,
    ReviewActor,
    ReviewVerdict,
    SchedulingState,
    StopIntent,
    canonical_json_bytes,
    snapshot_hash,
)
from sana.modules.shadow_campaign.policy import (
    DOCKER_SMOKE_V1,
    SHADOW_FULL_V1,
    CampaignProfile,
)


def test_stable_enums_cover_persisted_vocabulary() -> None:
    assert {item.value for item in CampaignStatus} == {
        "CREATED",
        "RUNNING",
        "STOPPING",
        "PAUSED",
        "AWAITING_REVIEW",
        "COMPLETED",
        "ABORTED",
    }
    assert {item.value for item in GateStatus} == {
        "PENDING",
        "PASS",
        "FAIL",
        "INSUFFICIENT_SAMPLE",
    }
    assert StopIntent.PAUSE.value == "PAUSE"
    assert SchedulingState.SKIPPED.value == "SKIPPED"
    assert ReservationState.SETTLED.value == "SETTLED"
    assert ReviewVerdict.MAJOR_ERROR.value == "MAJOR_ERROR"
    assert ReviewActor.SYSTEM.value == "SYSTEM"
    assert ErrorClass.CANDIDATE_DEFECT.value == "CANDIDATE_DEFECT"


def test_canonical_json_is_order_independent_and_normalizes_domain_scalars() -> None:
    at = datetime(2026, 8, 15, 12, 30, tzinfo=UTC)
    left = {
        "money": Decimal("0.1000"),
        "at": at,
        "status": GateStatus.PASS,
        "nested": {"b": 2, "a": 1},
    }
    right = {
        "nested": {"a": 1, "b": 2},
        "status": GateStatus.PASS,
        "at": at,
        "money": Decimal("0.1000"),
    }

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert snapshot_hash(left) == snapshot_hash(right)
    assert b'"money":"0.1000"' in canonical_json_bytes(left)
    assert b'"at":"2026-08-15T12:30:00Z"' in canonical_json_bytes(left)


def test_canonical_json_rejects_naive_time_and_non_finite_float() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json_bytes({"at": datetime(2026, 8, 15, 12, 30)})
    with pytest.raises(ValueError, match="finite"):
        canonical_json_bytes({"value": float("nan")})


def test_live_profiles_have_locked_safety_boundaries() -> None:
    assert DOCKER_SMOKE_V1.max_runs == 6
    assert DOCKER_SMOKE_V1.provider_call_admission_ceiling == 32
    assert DOCKER_SMOKE_V1.provider_call_structural_ceiling == 48
    assert DOCKER_SMOKE_V1.estimated_cost_stop_threshold == Decimal("0.01")
    assert DOCKER_SMOKE_V1.snapshot()["estimated_cost_stop_threshold"] == "0.01"
    assert SHADOW_FULL_V1.max_runs == 120
    assert SHADOW_FULL_V1.provider_call_admission_ceiling == 480
    assert SHADOW_FULL_V1.provider_call_structural_ceiling == 960
    assert SHADOW_FULL_V1.estimated_cost_stop_threshold == Decimal("0.10")


def test_profile_rejects_inconsistent_structural_ceiling() -> None:
    with pytest.raises(ValueError, match="structural ceiling"):
        CampaignProfile(
            version="bad-v1",
            max_runs=6,
            repetitions=1,
            max_concurrency=2,
            provider_call_admission_ceiling=32,
            provider_call_structural_ceiling=47,
            estimated_cost_stop_threshold=Decimal("0.01"),
            gate_policy_version="shadow-smoke-gate-v1",
            smoke_only=True,
        )
