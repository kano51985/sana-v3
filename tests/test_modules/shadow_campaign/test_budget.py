from decimal import Decimal

import pytest

from sana.modules.shadow_campaign.budget import (
    CampaignBudgetSnapshot,
    ReservationRequest,
    SettlementUsage,
)
from sana.modules.shadow_campaign.domain import StopIntent


def _snapshot(**overrides: object) -> CampaignBudgetSnapshot:
    values: dict[str, object] = {
        "provider_call_admission_ceiling": 40,
        "provider_call_structural_ceiling": 48,
        "estimated_cost_stop_threshold": Decimal("1.00"),
        "observed_provider_calls": 8,
        "possibly_billed_call_charge": 4,
        "reserved_provider_calls": 8,
        "observed_estimated_cost": Decimal("0.10"),
        "possibly_billed_cost_charge": Decimal("0.05"),
        "reserved_estimated_cost": Decimal("0.10"),
    }
    values.update(overrides)
    return CampaignBudgetSnapshot(**values)  # type: ignore[arg-type]


def test_admission_accounts_for_observed_uncertain_and_reserved_ledgers() -> None:
    admission = _snapshot(
        observed_provider_calls=20,
        possibly_billed_call_charge=8,
        reserved_provider_calls=8,
    ).admit(ReservationRequest(8, Decimal("0.10")))

    assert admission.allowed is False
    assert admission.stop_intent is StopIntent.CALL_CEILING
    assert admission.reason == "provider_call_admission_ceiling"
    assert admission.projected_provider_calls == 44


def test_structural_ceiling_has_priority_over_cost_limit() -> None:
    admission = _snapshot(
        provider_call_admission_ceiling=48,
        observed_provider_calls=41,
        observed_estimated_cost=Decimal("1.00"),
    ).admit(ReservationRequest(8, Decimal("0.10")))

    assert admission.stop_intent is StopIntent.CALL_CEILING
    assert admission.reason == "provider_call_structural_ceiling"


def test_cost_limit_denies_but_exact_boundary_is_admitted() -> None:
    denied = _snapshot(
        observed_provider_calls=0,
        possibly_billed_call_charge=0,
        reserved_provider_calls=0,
        observed_estimated_cost=Decimal("0.91"),
        possibly_billed_cost_charge=Decimal("0"),
        reserved_estimated_cost=Decimal("0"),
    ).admit(ReservationRequest(8, Decimal("0.10")))
    boundary = _snapshot(
        observed_provider_calls=0,
        possibly_billed_call_charge=0,
        reserved_provider_calls=0,
        observed_estimated_cost=Decimal("0.90"),
        possibly_billed_cost_charge=Decimal("0"),
        reserved_estimated_cost=Decimal("0"),
    ).admit(ReservationRequest(8, Decimal("0.10")))

    assert denied.stop_intent is StopIntent.BUDGET
    assert denied.reason == "estimated_cost_stop_threshold"
    assert boundary.allowed is True
    assert boundary.projected_estimated_cost == Decimal("1.00")


def test_active_reservation_pressure_is_distinct_from_irreversible_spend() -> None:
    snapshot = _snapshot(
        observed_provider_calls=14,
        possibly_billed_call_charge=0,
        reserved_provider_calls=4,
        observed_estimated_cost=Decimal("0.0029"),
        possibly_billed_cost_charge=Decimal("0"),
        reserved_estimated_cost=Decimal("0.004"),
        estimated_cost_stop_threshold=Decimal("0.01"),
    )
    request = ReservationRequest(4, Decimal("0.004"))

    assert snapshot.admit(request).allowed is False
    assert snapshot.admit_after_active_reservations_settle(request).allowed is True


def test_settlement_usage_hash_is_stable_and_detects_reservation_overrun() -> None:
    usage = SettlementUsage(
        observed_provider_calls=7,
        prompt_tokens=123,
        completion_tokens=45,
        observed_estimated_cost=Decimal("0.08"),
        possibly_billed_call_charge=2,
        possibly_billed_cost_charge=Decimal("0.01"),
    )
    same = SettlementUsage(
        observed_provider_calls=7,
        prompt_tokens=123,
        completion_tokens=45,
        observed_estimated_cost=Decimal("0.080"),
        possibly_billed_call_charge=2,
        possibly_billed_cost_charge=Decimal("0.010"),
    )

    assert usage.sha256 == same.sha256
    assert usage.exceeds(ReservationRequest(8, Decimal("0.10"))) is True


def test_money_values_use_the_database_scale_and_rounding_rule() -> None:
    usage = SettlementUsage(
        observed_provider_calls=1,
        prompt_tokens=0,
        completion_tokens=0,
        observed_estimated_cost=Decimal("0.00000000005"),
    )

    assert usage.observed_estimated_cost == Decimal("0.0000000001")


@pytest.mark.parametrize(
    "factory",
    (
        lambda: ReservationRequest(0, Decimal("0.1")),
        lambda: ReservationRequest(1, Decimal("NaN")),
        lambda: _snapshot(observed_provider_calls=-1),
        lambda: _snapshot(estimated_cost_stop_threshold=Decimal("0")),
        lambda: SettlementUsage(-1, 0, 0, Decimal("0")),
        lambda: SettlementUsage(0, 0, 0, Decimal("Infinity")),
    ),
)
def test_invalid_budget_values_fail_closed(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]
