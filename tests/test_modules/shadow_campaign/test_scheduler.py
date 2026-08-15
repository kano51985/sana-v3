from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ReservationState,
    SchedulingState,
)
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    CaseCategory,
    ShadowManifest,
)
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CostRate,
    DOCKER_SMOKE_V1,
    ReviewRubric,
    SHADOW_FULL_V1,
    SHADOW_SMOKE_GATE_V1,
)
from sana.modules.shadow_campaign.scheduler import (
    CampaignSchedulingEvidence,
    CampaignSchedulingService,
    RunLease,
    materialize_run_plans,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation


CAMPAIGN_ID = UUID("2cabcec0-04c6-49da-8754-0e28f23ff51d")
NOW = datetime(2026, 8, 15, tzinfo=UTC)
RUBRIC = ReviewRubric("review-v1")
RATE = CostRate("test-rate-v1", Decimal("0.1"), Decimal("0.2"), Decimal("0.001"))


class Case:
    def __init__(
        self,
        case_id: str,
        mode: SearchMode,
        locale: str,
        *,
        smoke: bool,
    ) -> None:
        self.id = case_id
        self.prompt = f"prompt for {case_id}"
        self.expected_mode = mode
        self.locale = locale
        self.category = CaseCategory.VERSION
        self.answerability = Answerability.ANSWERABLE
        self.smoke = smoke


def _manifest(*, reverse: bool = False) -> ShadowManifest:
    cases = []
    for mode in (SearchMode.FAST, SearchMode.RESEARCH):
        for locale in ("zh-CN", "en"):
            for index in range(10):
                smoke = (
                    (mode is SearchMode.FAST and locale == "zh-CN" and index < 2)
                    or (mode is SearchMode.FAST and locale == "en" and index == 0)
                    or (
                        mode is SearchMode.RESEARCH
                        and locale == "zh-CN"
                        and index == 0
                    )
                    or (
                        mode is SearchMode.RESEARCH
                        and locale == "en"
                        and index < 2
                    )
                )
                cases.append(
                    Case(
                        f"{mode.value.lower()}-{locale.lower()}-{index}",
                        mode,
                        locale,
                        smoke=smoke,
                    )
                )
    if reverse:
        cases.reverse()
    return ShadowManifest("shadow-cases-v1", tuple(cases), "a" * 64)


def test_full_plan_is_stable_balanced_and_review_stratified() -> None:
    forward = materialize_run_plans(
        CAMPAIGN_ID,
        _manifest(),
        SHADOW_FULL_V1,
        required_reviews=20,
    )
    reversed_input = materialize_run_plans(
        CAMPAIGN_ID,
        _manifest(reverse=True),
        SHADOW_FULL_V1,
        required_reviews=20,
    )

    assert forward == reversed_input
    assert len(forward) == 120
    assert [item.schedule_ordinal for item in forward] == list(range(1, 121))
    assert len({item.id for item in forward}) == 120
    assert len({item.submission_request_hash for item in forward}) == 120
    assert sum(item.manual_review_selected for item in forward) == 20
    assert [item.case_id for item in forward[:4]] == [
        "fast-zh-cn-0",
        "research-zh-cn-0",
        "fast-en-0",
        "research-en-0",
    ]


def test_smoke_plan_contains_only_six_locked_cases_and_no_reviews() -> None:
    plans = materialize_run_plans(
        CAMPAIGN_ID,
        _manifest(),
        DOCKER_SMOKE_V1,
        required_reviews=0,
    )

    assert len(plans) == 6
    assert all(item.repetition == 1 for item in plans)
    assert not any(item.manual_review_selected for item in plans)


def test_run_lease_uses_version_as_a_fencing_token() -> None:
    lease = RunLease(
        id=CAMPAIGN_ID,
        tenant_id=UUID(int=1),
        campaign_id=UUID(int=2),
        case_id="case-1",
        repetition=1,
        schedule_ordinal=1,
        state=SchedulingState.CLAIMED,
        lease_owner="worker-a",
        lease_expires_at=NOW + timedelta(seconds=30),
        conversation_id=None,
        search_run_id=None,
        reservation_state=ReservationState.NONE,
        version=1,
        _persisted_version=1,
    )

    lease.renew(NOW + timedelta(seconds=5), NOW + timedelta(seconds=60))

    assert lease.version == 2
    assert lease.persisted_version == 1
    with pytest.raises(InvariantViolation, match="Expired scheduling leases"):
        lease.renew(
            NOW + timedelta(seconds=61),
            NOW + timedelta(seconds=90),
        )


class SchedulingRepository:
    def __init__(self, evidence: CampaignSchedulingEvidence) -> None:
        self.evidence = evidence
        self.plans = ()

    async def scheduling_evidence_for_update(self, tenant_id, campaign_id):
        if tenant_id == self.evidence.tenant_id and campaign_id == self.evidence.id:
            return self.evidence
        return None

    async def materialize_results(self, evidence, plans, now):
        self.plans = plans
        return len(plans)


class SchedulingUnitOfWork:
    def __init__(self, repository: SchedulingRepository) -> None:
        self.campaigns = repository
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self):
        self.committed = True


def _evidence() -> CampaignSchedulingEvidence:
    return CampaignSchedulingEvidence(
        id=CAMPAIGN_ID,
        tenant_id=UUID(int=1),
        created_by_user_id=UUID(int=2),
        status=CampaignStatus.CREATED,
        profile_version=DOCKER_SMOKE_V1.version,
        profile_hash=DOCKER_SMOKE_V1.sha256,
        profile_snapshot=DOCKER_SMOKE_V1.snapshot(),
        gate_policy_version=SHADOW_SMOKE_GATE_V1.version,
        gate_policy_hash=SHADOW_SMOKE_GATE_V1.sha256,
        gate_policy_snapshot=SHADOW_SMOKE_GATE_V1.snapshot(),
        manifest_version="shadow-cases-v1",
        manifest_hash="a" * 64,
        repetitions=1,
        max_runs=6,
        max_concurrency=2,
        planned_count=0,
        result_count=0,
        retention_until=NOW + timedelta(days=30),
        version=0,
    )


@pytest.mark.asyncio
async def test_materialization_service_verifies_snapshots_and_commits_atomically() -> None:
    evidence = _evidence()
    repository = SchedulingRepository(evidence)
    uow = SchedulingUnitOfWork(repository)
    service = CampaignSchedulingService(
        lambda tenant_id: uow,
        FrozenClock(NOW),
        CampaignPolicyCatalog.standard(
            review_rubrics=(RUBRIC,),
            cost_rates=(RATE,),
        ),
    )

    receipt = await service.materialize(
        evidence.tenant_id,
        evidence.created_by_user_id,
        evidence.id,
        _manifest(),
    )

    assert receipt is not None and receipt.planned_count == 6
    assert len(repository.plans) == 6
    assert uow.committed


@pytest.mark.asyncio
async def test_materialization_fails_closed_on_manifest_snapshot_mismatch() -> None:
    evidence = _evidence()
    repository = SchedulingRepository(evidence)
    uow = SchedulingUnitOfWork(repository)
    service = CampaignSchedulingService(
        lambda tenant_id: uow,
        FrozenClock(NOW),
        CampaignPolicyCatalog.standard(
            review_rubrics=(RUBRIC,),
            cost_rates=(RATE,),
        ),
    )
    manifest = ShadowManifest("shadow-cases-v1", _manifest().cases, "f" * 64)

    with pytest.raises(InvariantViolation, match="frozen inputs") as error:
        await service.materialize(
            evidence.tenant_id,
            evidence.created_by_user_id,
            evidence.id,
            manifest,
        )

    assert error.value.code == "campaign_snapshot_mismatch"
    assert not repository.plans
