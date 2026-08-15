"""Deterministic run materialization and fenced scheduling leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID, uuid5

from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ReservationState,
    SchedulingState,
    canonical_snapshot,
    require_aware,
    snapshot_hash,
)
from sana.modules.shadow_campaign.evaluator import select_review_units
from sana.modules.shadow_campaign.manifest import ShadowCase, ShadowManifest
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CampaignProfile,
    GatePolicy,
)
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import CampaignUnitOfWorkFactory


_STRATA = (
    (SearchMode.FAST, "zh-CN"),
    (SearchMode.RESEARCH, "zh-CN"),
    (SearchMode.FAST, "en"),
    (SearchMode.RESEARCH, "en"),
)


@dataclass(frozen=True, slots=True)
class RunPlan:
    id: UUID
    case_id: str
    repetition: int
    schedule_ordinal: int
    manual_review_selected: bool
    prompt: str
    locale: str
    category: str
    answerability: str
    expected_mode: str
    conversation_idempotency_key: str
    message_idempotency_key: str
    submission_request_hash: str


def _ordered_cases(cases: tuple[ShadowCase, ...]) -> tuple[ShadowCase, ...]:
    grouped = {
        stratum: sorted(
            (
                case
                for case in cases
                if (case.expected_mode, case.locale) == stratum
            ),
            key=lambda case: case.id,
        )
        for stratum in _STRATA
    }
    if sum(map(len, grouped.values())) != len(cases):
        raise ValueError("Campaign cases contain an unsupported mode/locale stratum")
    ordered: list[ShadowCase] = []
    for index in range(max(map(len, grouped.values()), default=0)):
        for stratum in _STRATA:
            if index < len(grouped[stratum]):
                ordered.append(grouped[stratum][index])
    return tuple(ordered)


def materialize_run_plans(
    campaign_id: UUID,
    manifest: ShadowManifest,
    profile: CampaignProfile,
    *,
    required_reviews: int,
) -> tuple[RunPlan, ...]:
    selected_cases = manifest.smoke_cases if profile.smoke_only else manifest.cases
    ordered_cases = _ordered_cases(selected_cases)
    if len(ordered_cases) * profile.repetitions != profile.max_runs:
        raise InvariantViolation(
            "Manifest selection does not match campaign max_runs",
            code="manifest_profile_mismatch",
        )
    if required_reviews < 0 or required_reviews % 4:
        raise InvariantViolation(
            "Required manual reviews must divide evenly across four strata",
            code="invalid_review_sample_size",
        )
    selected_reviews = (
        {
            (item.case_id, item.repetition)
            for item in select_review_units(
                campaign_id,
                manifest,
                repetitions=profile.repetitions,
                per_stratum=required_reviews // 4,
            )
        }
        if required_reviews
        else set()
    )
    plans: list[RunPlan] = []
    for repetition in range(1, profile.repetitions + 1):
        for case in ordered_cases:
            ordinal = len(plans) + 1
            conversation_key = (
                f"shadow-conversation:{campaign_id}:{case.id}:{repetition}"
            )
            message_key = f"shadow-message:{campaign_id}:{case.id}:{repetition}"
            request_hash = snapshot_hash(
                {
                    "campaign_id": campaign_id,
                    "case_id": case.id,
                    "repetition": repetition,
                    "prompt": case.prompt,
                    "locale": case.locale,
                    "category": case.category.value,
                    "answerability": case.answerability.value,
                    "expected_mode": case.expected_mode.value,
                    "conversation_idempotency_key": conversation_key,
                    "message_idempotency_key": message_key,
                }
            )
            plans.append(
                RunPlan(
                    id=uuid5(
                        campaign_id,
                        f"shadow-run-result:{case.id}:{repetition}",
                    ),
                    case_id=case.id,
                    repetition=repetition,
                    schedule_ordinal=ordinal,
                    manual_review_selected=(case.id, repetition) in selected_reviews,
                    prompt=case.prompt,
                    locale=case.locale,
                    category=case.category.value,
                    answerability=case.answerability.value,
                    expected_mode=case.expected_mode.value,
                    conversation_idempotency_key=conversation_key,
                    message_idempotency_key=message_key,
                    submission_request_hash=request_hash,
                )
            )
    return tuple(plans)


@dataclass(frozen=True, slots=True)
class CampaignSchedulingEvidence:
    id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    status: CampaignStatus
    profile_version: str
    profile_hash: str
    profile_snapshot: Mapping[str, Any]
    gate_policy_version: str
    gate_policy_hash: str
    gate_policy_snapshot: Mapping[str, Any]
    manifest_version: str
    manifest_hash: str
    repetitions: int
    max_runs: int
    max_concurrency: int
    planned_count: int
    result_count: int
    retention_until: datetime
    version: int


@dataclass(frozen=True, slots=True)
class CampaignMaterializationReceipt:
    campaign_id: UUID
    planned_count: int
    duplicate: bool = False


@dataclass(slots=True)
class RunLease:
    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    case_id: str
    repetition: int
    schedule_ordinal: int
    state: SchedulingState
    lease_owner: str
    lease_expires_at: datetime
    conversation_id: UUID | None
    search_run_id: UUID | None
    reservation_state: ReservationState
    version: int
    _persisted_version: int

    def __post_init__(self) -> None:
        require_aware(self.lease_expires_at, "lease_expires_at")
        if self.state not in {
            SchedulingState.CLAIMED,
            SchedulingState.CONVERSATION_BOUND,
        }:
            raise ValueError(
                "A scheduling lease must be CLAIMED or CONVERSATION_BOUND"
            )
        if not self.lease_owner.strip() or self.version < 1:
            raise ValueError("A scheduling lease requires an owner and fencing version")

    def accept_budget_fence(
        self,
        version: int,
        reservation_state: ReservationState,
    ) -> None:
        if version <= self.version:
            raise InvariantViolation("Budget fencing token must advance")
        self.version = version
        self._persisted_version = version
        self.reservation_state = ReservationState(reservation_state)

    @property
    def persisted_version(self) -> int:
        return self._persisted_version

    def renew(self, now: datetime, lease_expires_at: datetime) -> None:
        require_aware(now, "lease renewal time")
        require_aware(lease_expires_at, "lease_expires_at")
        if now >= self.lease_expires_at:
            raise InvariantViolation(
                "Expired scheduling leases cannot be renewed",
                code="scheduling_lease_expired",
            )
        if lease_expires_at <= self.lease_expires_at:
            raise InvariantViolation("Scheduling lease renewal must extend the lease")
        self.lease_expires_at = lease_expires_at
        self.version += 1

    def mark_persisted(self) -> None:
        self._persisted_version = self.version


class CampaignSchedulingService:
    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        clock: Clock,
        policy_catalog: CampaignPolicyCatalog,
        *,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Scheduling lease duration must be positive")
        self._uow_factory = uow_factory
        self._clock = clock
        self._catalog = policy_catalog
        self._lease_duration = lease_duration

    async def materialize(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        manifest: ShadowManifest,
    ) -> CampaignMaterializationReceipt | None:
        now = self._clock.now()
        require_aware(now, "materialization time")
        async with self._uow_factory(tenant_id) as uow:
            evidence = await uow.campaigns.scheduling_evidence_for_update(
                tenant_id,
                campaign_id,
            )
            if evidence is None or evidence.created_by_user_id != user_id:
                return None
            profile, gate_policy = self._catalog.resolve(evidence.profile_version)
            self._verify_frozen_inputs(evidence, manifest, profile, gate_policy)
            if (
                evidence.planned_count == evidence.max_runs
                and evidence.result_count == evidence.max_runs
            ):
                return CampaignMaterializationReceipt(
                    campaign_id,
                    evidence.max_runs,
                    duplicate=True,
                )
            if evidence.status is not CampaignStatus.CREATED:
                raise InvariantViolation(
                    "Campaign run plan can only be created before start",
                    code="campaign_already_started",
                )
            if evidence.planned_count or evidence.result_count:
                raise InvariantViolation(
                    "Campaign has a partial or corrupt run plan",
                    code="materialization_state_corrupt",
                )
            plans = materialize_run_plans(
                campaign_id,
                manifest,
                profile,
                required_reviews=gate_policy.required_reviews,
            )
            inserted = await uow.campaigns.materialize_results(
                evidence,
                plans,
                now,
            )
            if inserted != len(plans):
                raise InvariantViolation(
                    "Campaign run materialization was not atomic",
                    code="materialization_count_mismatch",
                )
            await uow.commit()
            return CampaignMaterializationReceipt(campaign_id, inserted)

    async def claim_next(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
    ) -> RunLease | None:
        normalized_worker = worker_id.strip()
        if not normalized_worker or len(normalized_worker) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        async with self._uow_factory(tenant_id) as uow:
            lease = await uow.campaigns.claim_next_result(
                tenant_id,
                campaign_id,
                normalized_worker,
                self._lease_duration,
            )
            if lease is None:
                return None
            await uow.commit()
            return lease

    async def renew(self, lease: RunLease) -> RunLease:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaigns.renew_result_lease(
                lease,
                self._lease_duration,
            )
            await uow.commit()
        return lease

    @staticmethod
    def _verify_frozen_inputs(
        evidence: CampaignSchedulingEvidence,
        manifest: ShadowManifest,
        profile: CampaignProfile,
        gate_policy: GatePolicy,
    ) -> None:
        mismatches: list[str] = []
        comparisons = (
            ("profile_hash", evidence.profile_hash, profile.sha256),
            (
                "profile_snapshot",
                canonical_snapshot(evidence.profile_snapshot),
                profile.snapshot(),
            ),
            (
                "gate_policy_version",
                evidence.gate_policy_version,
                gate_policy.version,
            ),
            ("gate_policy_hash", evidence.gate_policy_hash, gate_policy.sha256),
            (
                "gate_policy_snapshot",
                canonical_snapshot(evidence.gate_policy_snapshot),
                gate_policy.snapshot(),
            ),
            ("manifest_version", evidence.manifest_version, manifest.version),
            ("manifest_hash", evidence.manifest_hash, manifest.sha256),
            ("repetitions", evidence.repetitions, profile.repetitions),
            ("max_runs", evidence.max_runs, profile.max_runs),
            ("max_concurrency", evidence.max_concurrency, profile.max_concurrency),
        )
        mismatches.extend(name for name, actual, expected in comparisons if actual != expected)
        if mismatches:
            raise InvariantViolation(
                "Campaign frozen inputs do not match the scheduling harness",
                code="campaign_snapshot_mismatch",
                details={"mismatched_fields": mismatches},
            )
