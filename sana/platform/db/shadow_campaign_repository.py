"""PostgreSQL adapter for the shadow campaign application service."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.shadow_campaign.domain import (
    CampaignLifecycle,
    CampaignStatus,
    GateStatus,
    SchedulingState,
    StopIntent,
    canonical_snapshot,
)
from sana.modules.shadow_campaign.scheduler import (
    CampaignSchedulingEvidence,
    RunLease,
    RunPlan,
)
from sana.modules.shadow_campaign.service import (
    CampaignCreation,
    CampaignParentEvidence,
    ExistingCampaign,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowRunResultRecord,
)


class SqlShadowCampaignRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def find_creation(
        self,
        tenant_id: UUID,
        user_id: UUID,
        idempotency_key: str,
    ) -> ExistingCampaign | None:
        self._assert_tenant(tenant_id)
        row = (
            await self._session.execute(
                select(
                    ShadowCampaignRecord.id,
                    ShadowCampaignRecord.creation_request_hash,
                    ShadowCampaignRecord.status,
                ).where(
                    ShadowCampaignRecord.tenant_id == tenant_id,
                    ShadowCampaignRecord.created_by_user_id == user_id,
                    ShadowCampaignRecord.creation_idempotency_key == idempotency_key,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return ExistingCampaign(
            id=row[0],
            creation_request_hash=row[1],
            status=CampaignStatus(row[2]),
        )

    async def parent_evidence(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignParentEvidence | None:
        self._assert_tenant(tenant_id)
        record = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if record is None:
            return None
        return CampaignParentEvidence(
            id=record.id,
            status=CampaignStatus(record.status),
            gate_status=GateStatus(record.gate_status),
            decision_hash=record.decision_hash,
            profile_snapshot=dict(record.profile_snapshot),
            manifest_hash=record.manifest_hash,
            review_rubric_hash=record.review_rubric_hash,
            cost_rate_hash=record.cost_rate_hash,
            candidate_commit_sha=record.candidate_commit_sha,
            candidate_source_clean=record.candidate_source_clean,
            candidate_image_id=record.candidate_image_id,
            candidate_oci_revision=record.candidate_oci_revision,
            alembic_head=record.alembic_head,
            candidate_config_hash=record.candidate_config_hash,
            harness_commit_sha=record.harness_commit_sha,
            harness_source_clean=record.harness_source_clean,
            harness_fileset_hash=record.harness_fileset_hash,
            collector_schema_version=record.collector_schema_version,
            environment_identity_hash=record.environment_identity_hash,
        )

    async def add(self, creation: CampaignCreation) -> bool:
        self._assert_tenant(creation.tenant_id)
        provenance = creation.provenance
        statement = (
            insert(ShadowCampaignRecord)
            .values(
                id=creation.id,
                tenant_id=creation.tenant_id,
                created_by_user_id=creation.created_by_user_id,
                name=creation.name,
                creation_idempotency_key=creation.creation_idempotency_key,
                creation_request_hash=creation.creation_request_hash,
                parent_smoke_campaign_id=creation.parent_smoke_campaign_id,
                parent_smoke_decision_hash=creation.parent_smoke_decision_hash,
                status=CampaignStatus.CREATED.value,
                gate_status=GateStatus.PENDING.value,
                profile_version=creation.profile.version,
                profile_hash=creation.profile_hash,
                profile_snapshot=creation.profile_snapshot,
                gate_policy_version=creation.gate_policy.version,
                gate_policy_hash=creation.gate_policy_hash,
                gate_policy_snapshot=creation.gate_policy_snapshot,
                manifest_version=creation.manifest.version,
                manifest_hash=creation.manifest_hash,
                manifest_case_count=len(creation.manifest.cases),
                repetitions=creation.profile.repetitions,
                review_rubric_version=creation.review_rubric.version,
                review_rubric_hash=creation.review_rubric_hash,
                review_rubric_snapshot=creation.review_rubric.snapshot(),
                cost_rate_version=creation.cost_rate.version,
                cost_rate_hash=creation.cost_rate_hash,
                cost_rate_snapshot=creation.cost_rate.snapshot(),
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
                environment_snapshot=canonical_snapshot(
                    provenance.environment_snapshot
                ),
                max_runs=creation.profile.max_runs,
                max_concurrency=creation.profile.max_concurrency,
                estimated_cost_stop_threshold=(
                    creation.profile.estimated_cost_stop_threshold
                ),
                provider_call_admission_ceiling=(
                    creation.profile.provider_call_admission_ceiling
                ),
                provider_call_structural_ceiling=(
                    creation.profile.provider_call_structural_ceiling
                ),
                retention_until=creation.retention_until,
                created_at=creation.created_at,
                updated_at=creation.created_at,
            )
            .on_conflict_do_nothing(
                constraint="uq_shadow_campaigns_owner_creation_key"
            )
            .returning(ShadowCampaignRecord.id)
        )
        return (await self._session.scalar(statement)) is not None

    async def get_for_update(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignLifecycle | None:
        self._assert_tenant(tenant_id)
        record = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        return self._lifecycle(record) if record is not None else None

    async def save_lifecycle(self, campaign: CampaignLifecycle) -> None:
        self._assert_tenant(campaign.tenant_id)
        statement = (
            update(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == campaign.tenant_id,
                ShadowCampaignRecord.id == campaign.id,
                ShadowCampaignRecord.version == campaign.persisted_version,
            )
            .values(
                status=campaign.status.value,
                gate_status=campaign.gate_status.value,
                stop_intent=campaign.stop_intent.value,
                stop_reason=campaign.stop_reason,
                started_at=campaign.started_at,
                review_deadline_at=campaign.review_deadline_at,
                completed_at=campaign.completed_at,
                version=campaign.version,
                updated_at=func.now(),
            )
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise InvariantViolation(
                "Shadow campaign was modified concurrently",
                code="optimistic_lock_failed",
            )
        campaign.mark_persisted()

    async def scheduling_evidence_for_update(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignSchedulingEvidence | None:
        self._assert_tenant(tenant_id)
        record = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if record is None:
            return None
        result_count = await self._session.scalar(
            select(func.count(ShadowRunResultRecord.id)).where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
            )
        )
        return CampaignSchedulingEvidence(
            id=record.id,
            tenant_id=record.tenant_id,
            created_by_user_id=record.created_by_user_id,
            status=CampaignStatus(record.status),
            profile_version=record.profile_version,
            profile_hash=record.profile_hash,
            profile_snapshot=dict(record.profile_snapshot),
            gate_policy_version=record.gate_policy_version,
            gate_policy_hash=record.gate_policy_hash,
            gate_policy_snapshot=dict(record.gate_policy_snapshot),
            manifest_version=record.manifest_version,
            manifest_hash=record.manifest_hash,
            repetitions=record.repetitions,
            max_runs=record.max_runs,
            max_concurrency=record.max_concurrency,
            planned_count=record.planned_count,
            result_count=int(result_count or 0),
            retention_until=record.retention_until,
            version=record.version,
        )

    async def materialize_results(
        self,
        evidence: CampaignSchedulingEvidence,
        plans: tuple[RunPlan, ...],
        now: datetime,
    ) -> int:
        self._assert_tenant(evidence.tenant_id)
        rows = [
            {
                "id": plan.id,
                "tenant_id": evidence.tenant_id,
                "campaign_id": evidence.id,
                "case_id": plan.case_id,
                "repetition": plan.repetition,
                "schedule_ordinal": plan.schedule_ordinal,
                "manual_review_selected": plan.manual_review_selected,
                "locale": plan.locale,
                "category": plan.category,
                "answerability": plan.answerability,
                "expected_mode": plan.expected_mode,
                "scheduling_state": SchedulingState.PENDING.value,
                "conversation_idempotency_key": (
                    plan.conversation_idempotency_key
                ),
                "message_idempotency_key": plan.message_idempotency_key,
                "submission_request_hash": plan.submission_request_hash,
                "retention_until": evidence.retention_until,
                "created_at": now,
                "updated_at": now,
            }
            for plan in plans
        ]
        result = await self._session.execute(insert(ShadowRunResultRecord).values(rows))
        campaign_update = await self._session.execute(
            update(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == evidence.tenant_id,
                ShadowCampaignRecord.id == evidence.id,
                ShadowCampaignRecord.status == CampaignStatus.CREATED.value,
                ShadowCampaignRecord.version == evidence.version,
                ShadowCampaignRecord.planned_count == 0,
            )
            .values(
                planned_count=len(plans),
                version=evidence.version + 1,
                updated_at=now,
            )
        )
        if campaign_update.rowcount != 1:
            raise InvariantViolation(
                "Campaign changed while its run plan was materialized",
                code="materialization_fence_lost",
            )
        return int(result.rowcount or 0)

    async def claim_next_result(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RunLease | None:
        self._assert_tenant(tenant_id)
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        lease_expires_at = now + lease_duration
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if campaign is None or campaign.status != CampaignStatus.RUNNING.value:
            return None
        active_leases = await self._session.scalar(
            select(func.count(ShadowRunResultRecord.id)).where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                ShadowRunResultRecord.scheduling_state.in_(
                    (
                        SchedulingState.CLAIMED.value,
                        SchedulingState.CONVERSATION_BOUND.value,
                    )
                ),
                ShadowRunResultRecord.lease_expires_at > now,
            )
        )
        if int(active_leases or 0) >= campaign.max_concurrency:
            return None
        candidate = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                or_(
                    ShadowRunResultRecord.scheduling_state
                    == SchedulingState.PENDING.value,
                    and_(
                        ShadowRunResultRecord.scheduling_state.in_(
                            (
                                SchedulingState.CLAIMED.value,
                                SchedulingState.CONVERSATION_BOUND.value,
                            )
                        ),
                        ShadowRunResultRecord.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(ShadowRunResultRecord.schedule_ordinal)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None
        next_version = candidate.version + 1
        claimed = await self._session.execute(
            update(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.id == candidate.id,
                ShadowRunResultRecord.version == candidate.version,
            )
            .values(
                scheduling_state=SchedulingState.CLAIMED.value,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                version=next_version,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            raise InvariantViolation(
                "Scheduling claim fencing token was lost",
                code="scheduling_claim_fence_lost",
            )
        return RunLease(
            id=candidate.id,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            case_id=candidate.case_id,
            repetition=candidate.repetition,
            schedule_ordinal=candidate.schedule_ordinal,
            state=SchedulingState.CLAIMED,
            lease_owner=worker_id,
            lease_expires_at=lease_expires_at,
            conversation_id=candidate.conversation_id,
            search_run_id=candidate.search_run_id,
            version=next_version,
            _persisted_version=next_version,
        )

    async def renew_result_lease(
        self,
        lease: RunLease,
        lease_duration: timedelta,
    ) -> None:
        self._assert_tenant(lease.tenant_id)
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        lease.renew(
            now,
            max(now, lease.lease_expires_at) + lease_duration,
        )
        result = await self._session.execute(
            update(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.campaign_id == lease.campaign_id,
                ShadowRunResultRecord.id == lease.id,
                ShadowRunResultRecord.scheduling_state.in_(
                    (
                        SchedulingState.CLAIMED.value,
                        SchedulingState.CONVERSATION_BOUND.value,
                    )
                ),
                ShadowRunResultRecord.lease_owner == lease.lease_owner,
                ShadowRunResultRecord.version == lease.persisted_version,
                ShadowRunResultRecord.lease_expires_at > now,
            )
            .values(
                lease_expires_at=lease.lease_expires_at,
                version=lease.version,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise InvariantViolation(
                "Scheduling lease fencing token is stale",
                code="scheduling_lease_fence_lost",
            )
        lease.mark_persisted()

    @staticmethod
    def _lifecycle(record: ShadowCampaignRecord) -> CampaignLifecycle:
        return CampaignLifecycle.rehydrate(
            id=record.id,
            tenant_id=record.tenant_id,
            created_by_user_id=record.created_by_user_id,
            max_runs=record.max_runs,
            planned_count=record.planned_count,
            status=CampaignStatus(record.status),
            gate_status=GateStatus(record.gate_status),
            stop_intent=StopIntent(record.stop_intent),
            stop_reason=record.stop_reason,
            started_at=record.started_at,
            review_deadline_at=record.review_deadline_at,
            completed_at=record.completed_at,
            version=record.version,
        )
