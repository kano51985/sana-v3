"""Application service for immutable, idempotent shadow campaign creation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping
from uuid import UUID

from sana.modules.shadow_campaign.domain import (
    CampaignLifecycle,
    CampaignStatus,
    GateStatus,
    StopIntent,
    canonical_snapshot,
    freeze_json,
    require_aware,
    snapshot_hash,
)
from sana.modules.shadow_campaign.manifest import ShadowManifest
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CampaignProfile,
    CostRate,
    GateKind,
    GatePolicy,
    ReviewRubric,
)
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import IdFactory

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import (
        CampaignRepository,
        CampaignUnitOfWorkFactory,
    )


_HEX = frozenset("0123456789abcdefABCDEF")


def _require_hex(value: str, field_name: str, lengths: tuple[int, ...]) -> str:
    normalized = value.strip().lower()
    if len(normalized) not in lengths or any(character not in _HEX for character in normalized):
        expected = " or ".join(map(str, lengths))
        raise ValueError(f"{field_name} must be a {expected}-character hex digest")
    return normalized


def _require_text(value: str, field_name: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain between 1 and {maximum} characters")
    return normalized


@dataclass(frozen=True, slots=True)
class CampaignProvenance:
    candidate_commit_sha: str
    candidate_source_clean: bool
    candidate_image_id: str
    candidate_oci_revision: str
    alembic_head: str
    candidate_config_hash: str
    harness_commit_sha: str
    harness_source_clean: bool
    harness_fileset_hash: str
    collector_schema_version: str
    environment_identity_hash: str
    environment_snapshot: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_commit_sha",
            _require_hex(self.candidate_commit_sha, "candidate_commit_sha", (40, 64)),
        )
        object.__setattr__(
            self,
            "harness_commit_sha",
            _require_hex(self.harness_commit_sha, "harness_commit_sha", (40, 64)),
        )
        for field_name in (
            "candidate_oci_revision",
            "candidate_config_hash",
            "harness_fileset_hash",
            "environment_identity_hash",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_hex(getattr(self, field_name), field_name, (64,)),
            )
        if not isinstance(self.candidate_source_clean, bool) or not isinstance(
            self.harness_source_clean,
            bool,
        ):
            raise ValueError("Source-clean provenance flags must be boolean")
        object.__setattr__(
            self,
            "candidate_image_id",
            _require_text(self.candidate_image_id, "candidate_image_id", 200),
        )
        if not self.candidate_image_id.endswith(
            f"sha256:{self.candidate_oci_revision}"
        ):
            raise ValueError(
                "candidate_image_id must bind the immutable OCI revision digest"
            )
        object.__setattr__(
            self,
            "alembic_head",
            _require_text(self.alembic_head, "alembic_head", 100),
        )
        object.__setattr__(
            self,
            "collector_schema_version",
            _require_text(
                self.collector_schema_version,
                "collector_schema_version",
                100,
            ),
        )
        environment = freeze_json(self.environment_snapshot)
        if not isinstance(environment, Mapping) or not environment:
            raise ValueError("environment_snapshot must be a non-empty mapping")
        object.__setattr__(self, "environment_snapshot", environment)
        if snapshot_hash(environment) != self.environment_identity_hash:
            raise ValueError(
                "environment_identity_hash must bind the canonical environment snapshot"
            )

    def snapshot(self) -> dict[str, Any]:
        return canonical_snapshot(self)


@dataclass(frozen=True, slots=True)
class CreateCampaignCommand:
    tenant_id: UUID
    user_id: UUID
    name: str
    idempotency_key: str
    profile_version: str
    manifest: ShadowManifest
    review_rubric: ReviewRubric
    cost_rate: CostRate
    provenance: CampaignProvenance
    retention_until: datetime
    parent_smoke_campaign_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_text(self.name, "name", 200))
        object.__setattr__(
            self,
            "idempotency_key",
            _require_text(self.idempotency_key, "idempotency_key", 100),
        )
        object.__setattr__(
            self,
            "profile_version",
            _require_text(self.profile_version, "profile_version", 100),
        )
        require_aware(self.retention_until, "retention_until")
        if not self.manifest.version.strip():
            raise ValueError("Manifest version cannot be empty")
        _require_hex(self.manifest.sha256, "manifest sha256", (64,))


@dataclass(frozen=True, slots=True)
class ExistingCampaign:
    id: UUID
    creation_request_hash: str
    status: CampaignStatus


@dataclass(frozen=True, slots=True)
class CampaignParentEvidence:
    id: UUID
    status: CampaignStatus
    gate_status: GateStatus
    decision_hash: str | None
    profile_snapshot: Mapping[str, Any]
    manifest_hash: str
    review_rubric_hash: str
    cost_rate_hash: str
    candidate_commit_sha: str
    candidate_source_clean: bool
    candidate_image_id: str
    candidate_oci_revision: str
    alembic_head: str
    candidate_config_hash: str
    harness_commit_sha: str
    harness_source_clean: bool
    harness_fileset_hash: str
    collector_schema_version: str
    environment_identity_hash: str


@dataclass(frozen=True, slots=True)
class CampaignCreation:
    id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    name: str
    creation_idempotency_key: str
    creation_request_hash: str
    profile: CampaignProfile
    gate_policy: GatePolicy
    manifest: ShadowManifest
    review_rubric: ReviewRubric
    cost_rate: CostRate
    provenance: CampaignProvenance
    parent_smoke_campaign_id: UUID | None
    parent_smoke_decision_hash: str | None
    created_at: datetime
    retention_until: datetime

    @property
    def profile_snapshot(self) -> dict[str, object]:
        return self.profile.snapshot()

    @property
    def profile_hash(self) -> str:
        return self.profile.sha256

    @property
    def gate_policy_version(self) -> str:
        return self.gate_policy.version

    @property
    def gate_policy_snapshot(self) -> dict[str, object]:
        return self.gate_policy.snapshot()

    @property
    def gate_policy_hash(self) -> str:
        return self.gate_policy.sha256

    @property
    def manifest_hash(self) -> str:
        return self.manifest.sha256

    @property
    def review_rubric_hash(self) -> str:
        return self.review_rubric.sha256

    @property
    def cost_rate_hash(self) -> str:
        return self.cost_rate.sha256

    @property
    def environment_snapshot(self) -> Mapping[str, Any]:
        return self.provenance.environment_snapshot


@dataclass(frozen=True, slots=True)
class CampaignCreationReceipt:
    id: UUID
    status: CampaignStatus
    request_hash: str
    duplicate: bool = False


class CampaignService:
    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        id_factory: IdFactory,
        clock: Clock,
        policy_catalog: CampaignPolicyCatalog,
    ) -> None:
        self._uow_factory = uow_factory
        self._ids = id_factory
        self._clock = clock
        self._catalog = policy_catalog

    async def create(self, command: CreateCampaignCommand) -> CampaignCreationReceipt:
        now = self._clock.now()
        require_aware(now, "campaign created_at")
        if command.retention_until <= now:
            raise InvariantViolation("Campaign retention must end after creation")
        profile, policy = self._catalog.resolve(command.profile_version)
        review_rubric, cost_rate = self._catalog.resolve_evaluation_assets(
            command.review_rubric,
            command.cost_rate,
        )
        self._validate_manifest_profile(command.manifest, profile, policy)

        async with self._uow_factory(command.tenant_id) as uow:
            parent = await self._resolve_parent(
                uow.campaigns,
                command,
                profile,
                review_rubric,
                cost_rate,
            )
            parent_hash = parent.decision_hash if parent is not None else None
            request_hash = self._request_hash(
                command,
                profile,
                policy,
                review_rubric,
                cost_rate,
                parent_hash,
            )
            existing = await uow.campaigns.find_creation(
                command.tenant_id,
                command.user_id,
                command.idempotency_key,
            )
            if existing is not None:
                return self._duplicate_or_conflict(existing, request_hash)

            creation = CampaignCreation(
                id=self._ids.new_uuid(),
                tenant_id=command.tenant_id,
                created_by_user_id=command.user_id,
                name=command.name,
                creation_idempotency_key=command.idempotency_key,
                creation_request_hash=request_hash,
                profile=profile,
                gate_policy=policy,
                manifest=command.manifest,
                review_rubric=review_rubric,
                cost_rate=cost_rate,
                provenance=command.provenance,
                parent_smoke_campaign_id=command.parent_smoke_campaign_id,
                parent_smoke_decision_hash=parent_hash,
                created_at=now,
                retention_until=command.retention_until,
            )
            inserted = await uow.campaigns.add(creation)
            if not inserted:
                winner = await uow.campaigns.find_creation(
                    command.tenant_id,
                    command.user_id,
                    command.idempotency_key,
                )
                if winner is None:
                    raise InvariantViolation(
                        "Campaign idempotency winner was not visible",
                        code="idempotency_race_unresolved",
                    )
                return self._duplicate_or_conflict(winner, request_hash)
            await uow.commit()
            return CampaignCreationReceipt(
                creation.id,
                CampaignStatus.CREATED,
                request_hash,
            )

    @staticmethod
    def _validate_manifest_profile(
        manifest: ShadowManifest,
        profile: CampaignProfile,
        policy: GatePolicy,
    ) -> None:
        expected_kind = GateKind.SMOKE if profile.smoke_only else GateKind.FULL
        if policy.kind is not expected_kind:
            raise InvariantViolation(
                "Campaign profile and gate policy kind differ",
                code="profile_policy_mismatch",
            )
        selected_cases = len(manifest.smoke_cases) if profile.smoke_only else len(manifest.cases)
        if selected_cases * profile.repetitions != profile.max_runs:
            raise InvariantViolation(
                "Manifest selection does not match the locked campaign run count",
                code="manifest_profile_mismatch",
                details={
                    "selected_cases": selected_cases,
                    "repetitions": profile.repetitions,
                    "max_runs": profile.max_runs,
                },
            )

    async def _resolve_parent(
        self,
        repository: "CampaignRepository",
        command: CreateCampaignCommand,
        profile: CampaignProfile,
        review_rubric: ReviewRubric,
        cost_rate: CostRate,
    ) -> CampaignParentEvidence | None:
        parent_id = command.parent_smoke_campaign_id
        if profile.smoke_only:
            if parent_id is not None:
                raise InvariantViolation(
                    "Smoke campaigns cannot have a parent campaign",
                    code="unexpected_parent_smoke",
                )
            return None
        if parent_id is None:
            raise InvariantViolation(
                "Full campaigns require a passed parent smoke campaign",
                code="parent_smoke_required",
            )
        if not (
            command.provenance.candidate_source_clean
            and command.provenance.harness_source_clean
        ):
            raise InvariantViolation(
                "Full campaigns require clean source trees",
                code="dirty_full_campaign_source",
            )
        parent = await repository.parent_evidence(command.tenant_id, parent_id)
        if parent is None:
            raise InvariantViolation(
                "Parent smoke campaign was not found",
                code="parent_smoke_not_found",
            )
        if (
            parent.status is not CampaignStatus.COMPLETED
            or parent.gate_status is not GateStatus.PASS
            or parent.decision_hash is None
            or parent.profile_snapshot.get("smoke_only") is not True
        ):
            raise InvariantViolation(
                "Parent smoke campaign is not a completed PASS decision",
                code="parent_smoke_not_passed",
            )
        expected = {
            "manifest_hash": command.manifest.sha256,
            "review_rubric_hash": review_rubric.sha256,
            "cost_rate_hash": cost_rate.sha256,
            **command.provenance.snapshot(),
        }
        comparable_fields = (
            "manifest_hash",
            "review_rubric_hash",
            "cost_rate_hash",
            "candidate_commit_sha",
            "candidate_source_clean",
            "candidate_image_id",
            "candidate_oci_revision",
            "alembic_head",
            "candidate_config_hash",
            "harness_commit_sha",
            "harness_source_clean",
            "harness_fileset_hash",
            "collector_schema_version",
            "environment_identity_hash",
        )
        mismatches = [
            field_name
            for field_name in comparable_fields
            if getattr(parent, field_name) != expected[field_name]
        ]
        if mismatches:
            raise InvariantViolation(
                "Parent smoke did not certify the exact candidate and harness",
                code="parent_smoke_provenance_mismatch",
                details={"mismatched_fields": mismatches},
            )
        return parent

    @staticmethod
    def _request_hash(
        command: CreateCampaignCommand,
        profile: CampaignProfile,
        policy: GatePolicy,
        review_rubric: ReviewRubric,
        cost_rate: CostRate,
        parent_decision_hash: str | None,
    ) -> str:
        return snapshot_hash(
            {
                "tenant_id": command.tenant_id,
                "user_id": command.user_id,
                "name": command.name,
                "profile": profile.snapshot(),
                "profile_hash": profile.sha256,
                "gate_policy": policy.snapshot(),
                "gate_policy_hash": policy.sha256,
                "manifest": {
                    "version": command.manifest.version,
                    "sha256": command.manifest.sha256,
                    "case_count": len(command.manifest.cases),
                },
                "review_rubric": review_rubric.snapshot(),
                "review_rubric_hash": review_rubric.sha256,
                "cost_rate": cost_rate.snapshot(),
                "cost_rate_hash": cost_rate.sha256,
                "provenance": command.provenance.snapshot(),
                "retention_until": command.retention_until,
                "parent_smoke_campaign_id": command.parent_smoke_campaign_id,
                "parent_smoke_decision_hash": parent_decision_hash,
            }
        )

    @staticmethod
    def _duplicate_or_conflict(
        existing: ExistingCampaign,
        request_hash: str,
    ) -> CampaignCreationReceipt:
        if existing.creation_request_hash != request_hash:
            raise InvariantViolation(
                "Idempotency-Key was already used with a different campaign payload",
                code="idempotency_conflict",
            )
        return CampaignCreationReceipt(
            existing.id,
            CampaignStatus(existing.status),
            request_hash,
            duplicate=True,
        )


class CampaignLifecycleService:
    """Tenant/owner authorization boundary around lifecycle mutations."""

    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def start(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
    ) -> CampaignLifecycle | None:
        return await self._owned_mutation(
            tenant_id,
            user_id,
            campaign_id,
            lambda campaign: campaign.start(self._clock.now()),
        )

    async def request_stop(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        intent: StopIntent,
        reason: str,
    ) -> CampaignLifecycle | None:
        return await self._owned_mutation(
            tenant_id,
            user_id,
            campaign_id,
            lambda campaign: campaign.request_stop(intent, reason),
        )

    async def abort(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        reason: str,
    ) -> CampaignLifecycle | None:
        return await self._owned_mutation(
            tenant_id,
            user_id,
            campaign_id,
            lambda campaign: campaign.abort(reason, self._clock.now()),
        )

    async def settle_stop(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignLifecycle | None:
        return await self._system_mutation(
            tenant_id,
            campaign_id,
            lambda campaign: campaign.settle_stop(self._clock.now()),
        )

    async def await_review(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        deadline: datetime,
    ) -> CampaignLifecycle | None:
        return await self._system_mutation(
            tenant_id,
            campaign_id,
            lambda campaign: campaign.await_review(
                self._clock.now(),
                deadline=deadline,
            ),
        )

    async def _owned_mutation(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        mutate: Callable[[CampaignLifecycle], None],
    ) -> CampaignLifecycle | None:
        async with self._uow_factory(tenant_id) as uow:
            campaign = await uow.campaigns.get_for_update(tenant_id, campaign_id)
            if campaign is None or campaign.created_by_user_id != user_id:
                return None
            mutate(campaign)
            await uow.campaigns.save_lifecycle(campaign)
            await uow.commit()
            return campaign

    async def _system_mutation(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        mutate: Callable[[CampaignLifecycle], None],
    ) -> CampaignLifecycle | None:
        async with self._uow_factory(tenant_id) as uow:
            campaign = await uow.campaigns.get_for_update(tenant_id, campaign_id)
            if campaign is None:
                return None
            mutate(campaign)
            await uow.campaigns.save_lifecycle(campaign)
            await uow.commit()
            return campaign
