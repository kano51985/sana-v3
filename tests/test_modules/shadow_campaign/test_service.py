from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sana.modules.shadow_campaign.domain import (
    CampaignLifecycle,
    CampaignStatus,
    GateStatus,
    StopIntent,
    snapshot_hash,
)
from sana.modules.shadow_campaign.manifest import ShadowManifest
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CostRate,
    DOCKER_SMOKE_V1,
    ReviewRubric,
    SHADOW_FULL_V1,
)
from sana.modules.shadow_campaign.service import (
    CampaignParentEvidence,
    CampaignProvenance,
    CampaignService,
    CampaignLifecycleService,
    CreateCampaignCommand,
    ExistingCampaign,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import DeterministicIdFactory


NOW = datetime(2026, 8, 15, tzinfo=UTC)
RUBRIC = ReviewRubric("review-v1")
RATE = CostRate(
    "deepseek-test-rate-v1",
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("0.001"),
)


def _manifest() -> ShadowManifest:
    cases = tuple(SimpleNamespace(smoke=index < 6) for index in range(40))
    return ShadowManifest("shadow-cases-v1", cases, "a" * 64)


def _provenance(*, suffix: str = "a", clean: bool = True) -> CampaignProvenance:
    environment = {"compose_project": "sana-shadow", "network": "isolated"}
    digest = suffix * 64
    return CampaignProvenance(
        candidate_commit_sha=suffix * 40,
        candidate_source_clean=clean,
        candidate_image_id=f"sana-candidate@sha256:{digest}",
        candidate_oci_revision=suffix * 40,
        alembic_head="0009_shadow_campaign_gate",
        candidate_config_hash=digest,
        harness_commit_sha=suffix * 40,
        harness_source_clean=clean,
        harness_fileset_hash=digest,
        collector_schema_version="shadow-collector-v2",
        environment_identity_hash=snapshot_hash(environment),
        environment_snapshot=environment,
    )


class FakeCampaignRepository:
    def __init__(self) -> None:
        self.existing: ExistingCampaign | None = None
        self.parent: CampaignParentEvidence | None = None
        self.added = []
        self.inserted = True
        self.lifecycle: CampaignLifecycle | None = None
        self.saved = []

    async def find_creation(self, tenant_id, user_id, idempotency_key):
        return self.existing

    async def parent_evidence(self, tenant_id, campaign_id):
        if self.parent is not None and self.parent.id == campaign_id:
            return self.parent
        return None

    async def add(self, creation) -> bool:
        self.added.append(creation)
        return self.inserted

    async def get_for_update(self, tenant_id, campaign_id):
        if self.lifecycle is not None and self.lifecycle.id == campaign_id:
            return self.lifecycle
        return None

    async def save_lifecycle(self, campaign) -> None:
        self.saved.append(campaign)


class FakeUnitOfWork:
    def __init__(self, repository: FakeCampaignRepository) -> None:
        self.campaigns = repository
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self) -> None:
        self.committed = True


def _service(repository: FakeCampaignRepository):
    uow = FakeUnitOfWork(repository)
    service = CampaignService(
        lambda tenant_id: uow,
        DeterministicIdFactory("shadow-campaign"),
        FrozenClock(NOW),
        CampaignPolicyCatalog.standard(
            review_rubrics=(RUBRIC,),
            cost_rates=(RATE,),
        ),
    )
    return service, uow


def _command(
    *,
    profile_version: str = DOCKER_SMOKE_V1.version,
    parent_id: UUID | None = None,
    name: str = "candidate smoke",
    provenance: CampaignProvenance | None = None,
) -> CreateCampaignCommand:
    return CreateCampaignCommand(
        tenant_id=uuid4(),
        user_id=uuid4(),
        name=name,
        idempotency_key="campaign-request-1",
        profile_version=profile_version,
        manifest=_manifest(),
        review_rubric=RUBRIC,
        cost_rate=RATE,
        provenance=provenance or _provenance(),
        retention_until=NOW + timedelta(days=30),
        parent_smoke_campaign_id=parent_id,
    )


@pytest.mark.asyncio
async def test_create_locks_validated_snapshots_and_commits_once() -> None:
    repository = FakeCampaignRepository()
    service, uow = _service(repository)
    command = _command()

    receipt = await service.create(command)

    assert uow.committed
    assert receipt.status is CampaignStatus.CREATED
    assert receipt.duplicate is False
    creation = repository.added[0]
    assert creation.profile_snapshot == DOCKER_SMOKE_V1.snapshot()
    assert creation.profile_hash == DOCKER_SMOKE_V1.sha256
    assert creation.manifest_hash == command.manifest.sha256
    assert creation.gate_policy_version == DOCKER_SMOKE_V1.gate_policy_version
    assert creation.environment_snapshot == command.provenance.environment_snapshot
    assert creation.creation_request_hash == receipt.request_hash


@pytest.mark.asyncio
async def test_idempotent_retry_returns_existing_and_payload_change_conflicts() -> None:
    repository = FakeCampaignRepository()
    service, _uow = _service(repository)
    command = _command()
    first = await service.create(command)
    request_hash = repository.added[0].creation_request_hash
    repository.existing = ExistingCampaign(
        first.id,
        request_hash,
        CampaignStatus.CREATED,
    )
    repository.added.clear()

    duplicate = await service.create(
        replace(command, retention_until=command.retention_until + timedelta(seconds=1))
    )

    assert duplicate.id == first.id
    assert duplicate.duplicate is True
    assert not repository.added
    with pytest.raises(InvariantViolation, match="Idempotency-Key") as error:
        await service.create(replace(command, name="different payload"))
    assert error.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_request_cannot_override_registered_cost_rates() -> None:
    repository = FakeCampaignRepository()
    service, _uow = _service(repository)
    command = _command()
    forged_rate = replace(RATE, prompt_per_million_usd=Decimal("0"))

    with pytest.raises(InvariantViolation, match="locked policy catalog") as error:
        await service.create(replace(command, cost_rate=forged_rate))

    assert error.value.code == "unapproved_evaluation_asset"
    assert not repository.added


def _parent(command: CreateCampaignCommand, parent_id: UUID) -> CampaignParentEvidence:
    provenance = command.provenance
    return CampaignParentEvidence(
        id=parent_id,
        status=CampaignStatus.COMPLETED,
        gate_status=GateStatus.PASS,
        decision_hash="b" * 64,
        profile_snapshot=DOCKER_SMOKE_V1.snapshot(),
        manifest_hash=command.manifest.sha256,
        review_rubric_hash=command.review_rubric.sha256,
        cost_rate_hash=command.cost_rate.sha256,
        candidate_commit_sha=provenance.candidate_commit_sha,
        candidate_source_clean=provenance.candidate_source_clean,
        candidate_image_id=provenance.candidate_image_id,
        candidate_oci_revision=provenance.candidate_oci_revision,
        alembic_head=provenance.alembic_head,
        candidate_config_hash=provenance.candidate_config_hash,
        harness_commit_sha=provenance.harness_commit_sha,
        harness_source_clean=provenance.harness_source_clean,
        harness_fileset_hash=provenance.harness_fileset_hash,
        collector_schema_version=provenance.collector_schema_version,
        environment_identity_hash=provenance.environment_identity_hash,
    )


@pytest.mark.asyncio
async def test_full_campaign_requires_matching_passed_smoke_evidence() -> None:
    repository = FakeCampaignRepository()
    service, uow = _service(repository)
    parent_id = uuid4()
    command = _command(
        profile_version=SHADOW_FULL_V1.version,
        parent_id=parent_id,
        name="candidate full",
    )
    repository.parent = _parent(command, parent_id)

    await service.create(command)

    assert uow.committed
    assert repository.added[0].parent_smoke_campaign_id == parent_id
    assert repository.added[0].parent_smoke_decision_hash == "b" * 64


@pytest.mark.asyncio
async def test_full_campaign_rejects_dirty_or_mismatched_smoke_evidence() -> None:
    repository = FakeCampaignRepository()
    service, _uow = _service(repository)
    parent_id = uuid4()
    dirty = _command(
        profile_version=SHADOW_FULL_V1.version,
        parent_id=parent_id,
        provenance=_provenance(clean=False),
    )
    repository.parent = _parent(dirty, parent_id)
    with pytest.raises(InvariantViolation, match="clean source trees"):
        await service.create(dirty)

    clean = _command(
        profile_version=SHADOW_FULL_V1.version,
        parent_id=parent_id,
    )
    repository.parent = _parent(clean, parent_id)
    repository.parent = replace(
        repository.parent,
        candidate_config_hash="f" * 64,
    )
    with pytest.raises(InvariantViolation, match="exact candidate"):
        await service.create(clean)


@pytest.mark.asyncio
async def test_lifecycle_service_locks_owner_scoped_mutations() -> None:
    tenant_id, user_id, campaign_id = uuid4(), uuid4(), uuid4()
    repository = FakeCampaignRepository()
    repository.lifecycle = CampaignLifecycle(campaign_id, tenant_id, user_id, 6, 6)
    uow = FakeUnitOfWork(repository)
    service = CampaignLifecycleService(lambda requested_tenant: uow, FrozenClock(NOW))

    hidden = await service.start(tenant_id, uuid4(), campaign_id)
    started = await service.start(tenant_id, user_id, campaign_id)
    stopping = await service.request_stop(
        tenant_id,
        user_id,
        campaign_id,
        StopIntent.PAUSE,
        "maintenance",
    )

    assert hidden is None
    assert started is stopping is repository.lifecycle
    assert stopping.status is CampaignStatus.STOPPING
    assert len(repository.saved) == 2
    assert uow.committed
