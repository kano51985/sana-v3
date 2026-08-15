"""Fail-closed campaign budget admission and exactly-once settlement values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.shadow_campaign.domain import StopIntent, snapshot_hash
from sana.modules.shadow_campaign.scheduler import RunLease

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import CampaignUnitOfWorkFactory


_MONEY_QUANTUM = Decimal("0.0000000001")


def _nonnegative_decimal(value: Decimal, field_name: str) -> Decimal:
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field_name} must be a finite non-negative Decimal")
    try:
        return parsed.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP).normalize()
    except InvalidOperation as error:
        raise ValueError(
            f"{field_name} exceeds the campaign money precision"
        ) from error


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    provider_calls: int
    estimated_cost: Decimal

    def __post_init__(self) -> None:
        if self.provider_calls < 1:
            raise ValueError("A run reservation must include provider calls")
        object.__setattr__(
            self,
            "estimated_cost",
            _nonnegative_decimal(self.estimated_cost, "estimated_cost"),
        )


@dataclass(frozen=True, slots=True)
class CampaignBudgetSnapshot:
    provider_call_admission_ceiling: int
    provider_call_structural_ceiling: int
    estimated_cost_stop_threshold: Decimal
    observed_provider_calls: int = 0
    possibly_billed_call_charge: int = 0
    reserved_provider_calls: int = 0
    observed_estimated_cost: Decimal = Decimal(0)
    possibly_billed_cost_charge: Decimal = Decimal(0)
    reserved_estimated_cost: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        counters = (
            self.provider_call_admission_ceiling,
            self.provider_call_structural_ceiling,
            self.observed_provider_calls,
            self.possibly_billed_call_charge,
            self.reserved_provider_calls,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Campaign budget call counters cannot be negative")
        if not 0 < self.provider_call_admission_ceiling <= self.provider_call_structural_ceiling:
            raise ValueError("Campaign call ceilings are invalid")
        for field_name in (
            "estimated_cost_stop_threshold",
            "observed_estimated_cost",
            "possibly_billed_cost_charge",
            "reserved_estimated_cost",
        ):
            value = _nonnegative_decimal(getattr(self, field_name), field_name)
            object.__setattr__(self, field_name, value)
        if self.estimated_cost_stop_threshold == 0:
            raise ValueError("Campaign cost stop threshold must be positive")

    def admit(self, request: ReservationRequest) -> "BudgetAdmission":
        projected_calls = (
            self.observed_provider_calls
            + self.possibly_billed_call_charge
            + self.reserved_provider_calls
            + request.provider_calls
        )
        projected_cost = (
            self.observed_estimated_cost
            + self.possibly_billed_cost_charge
            + self.reserved_estimated_cost
            + request.estimated_cost
        )
        if projected_calls > self.provider_call_structural_ceiling:
            return BudgetAdmission.denied(
                StopIntent.CALL_CEILING,
                "provider_call_structural_ceiling",
                projected_calls,
                projected_cost,
            )
        if projected_calls > self.provider_call_admission_ceiling:
            return BudgetAdmission.denied(
                StopIntent.CALL_CEILING,
                "provider_call_admission_ceiling",
                projected_calls,
                projected_cost,
            )
        if projected_cost > self.estimated_cost_stop_threshold:
            return BudgetAdmission.denied(
                StopIntent.BUDGET,
                "estimated_cost_stop_threshold",
                projected_calls,
                projected_cost,
            )
        return BudgetAdmission(
            True,
            StopIntent.NONE,
            None,
            projected_calls,
            projected_cost,
        )

    def admit_after_active_reservations_settle(
        self,
        request: ReservationRequest,
    ) -> "BudgetAdmission":
        """Project durable spend only, excluding transient in-flight holds."""

        return replace(
            self,
            reserved_provider_calls=0,
            reserved_estimated_cost=Decimal(0),
        ).admit(request)


@dataclass(frozen=True, slots=True)
class BudgetAdmission:
    allowed: bool
    stop_intent: StopIntent
    reason: str | None
    projected_provider_calls: int
    projected_estimated_cost: Decimal

    @classmethod
    def denied(
        cls,
        intent: StopIntent,
        reason: str,
        calls: int,
        cost: Decimal,
    ) -> "BudgetAdmission":
        return cls(False, intent, reason, calls, cost)


@dataclass(frozen=True, slots=True)
class SettlementUsage:
    observed_provider_calls: int
    prompt_tokens: int
    completion_tokens: int
    observed_estimated_cost: Decimal
    possibly_billed_call_charge: int = 0
    possibly_billed_cost_charge: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        counters = (
            self.observed_provider_calls,
            self.prompt_tokens,
            self.completion_tokens,
            self.possibly_billed_call_charge,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Settlement counters cannot be negative")
        for field_name in (
            "observed_estimated_cost",
            "possibly_billed_cost_charge",
        ):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_decimal(getattr(self, field_name), field_name),
            )

    @property
    def sha256(self) -> str:
        return snapshot_hash(self)

    def exceeds(self, request: ReservationRequest) -> bool:
        return (
            self.observed_provider_calls + self.possibly_billed_call_charge
            > request.provider_calls
            or self.observed_estimated_cost + self.possibly_billed_cost_charge
            > request.estimated_cost
        )


@dataclass(frozen=True, slots=True)
class BudgetReservationReceipt:
    allowed: bool
    stop_intent: StopIntent
    reason: str | None
    reserved_provider_calls: int = 0
    reserved_estimated_cost: Decimal = Decimal(0)
    deferred: bool = False

    def __post_init__(self) -> None:
        if self.allowed and self.deferred:
            raise ValueError("An allowed budget reservation cannot be deferred")
        if self.deferred and self.stop_intent is not StopIntent.NONE:
            raise ValueError("Deferred capacity cannot carry a terminal stop intent")


@dataclass(frozen=True, slots=True)
class BudgetSettlementReceipt:
    result_id: UUID
    duplicate: bool
    budget_violation: bool


@dataclass(frozen=True, slots=True)
class BudgetReleaseReceipt:
    result_id: UUID
    duplicate: bool


class CampaignBudgetService:
    def __init__(self, uow_factory: "CampaignUnitOfWorkFactory") -> None:
        self._uow_factory = uow_factory

    async def reserve_run(self, lease: RunLease) -> BudgetReservationReceipt:
        async with self._uow_factory(lease.tenant_id) as uow:
            receipt = await uow.campaigns.reserve_run_budget(lease)
            await uow.commit()
            return receipt

    async def settle_result(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
        *,
        source_snapshot_digest: str,
        usage: SettlementUsage,
    ) -> BudgetSettlementReceipt:
        async with self._uow_factory(tenant_id) as uow:
            receipt = await uow.campaigns.settle_run_budget(
                tenant_id,
                campaign_id,
                result_id,
                source_snapshot_digest,
                usage,
            )
            await uow.commit()
            return receipt

    async def release_run(self, lease: RunLease) -> BudgetReleaseReceipt:
        async with self._uow_factory(lease.tenant_id) as uow:
            receipt = await uow.campaigns.release_run_budget(lease)
            await uow.commit()
            return receipt
