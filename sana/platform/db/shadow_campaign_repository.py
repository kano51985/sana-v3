"""PostgreSQL adapter for the shadow campaign application service."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.shadow_campaign.domain import (
    CampaignLifecycle,
    CampaignStatus,
    GateStatus,
    ReservationState,
    SchedulingState,
    StopIntent,
    canonical_snapshot,
)
from sana.modules.shadow_campaign.budget import (
    BudgetReleaseReceipt,
    BudgetReservationReceipt,
    BudgetSettlementReceipt,
    CampaignBudgetSnapshot,
    ReservationRequest,
    SettlementUsage,
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
        if campaign is None or campaign.status not in {
            CampaignStatus.RUNNING.value,
            CampaignStatus.STOPPING.value,
        }:
            return None
        active_units = await self._session.scalar(
            select(func.count(ShadowRunResultRecord.id)).where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                or_(
                    and_(
                        ShadowRunResultRecord.scheduling_state
                        == SchedulingState.SUBMITTED.value,
                        ShadowRunResultRecord.reservation_state
                        == ReservationState.ACTIVE.value,
                    ),
                    and_(
                        ShadowRunResultRecord.scheduling_state.in_(
                            (
                                SchedulingState.CLAIMED.value,
                                SchedulingState.CONVERSATION_BOUND.value,
                            )
                        ),
                        ShadowRunResultRecord.lease_expires_at > now,
                    ),
                ),
            )
        )
        if int(active_units or 0) >= campaign.max_concurrency:
            return None
        scheduling_predicate = or_(
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
        )
        if campaign.status == CampaignStatus.STOPPING.value:
            # STOPPING drains only attempts that crossed a durable outbound fence.
            scheduling_predicate = and_(
                ShadowRunResultRecord.scheduling_state.in_(
                    (
                        SchedulingState.CLAIMED.value,
                        SchedulingState.CONVERSATION_BOUND.value,
                    )
                ),
                ShadowRunResultRecord.lease_expires_at <= now,
                or_(
                    ShadowRunResultRecord.conversation_attempt_count > 0,
                    ShadowRunResultRecord.submission_attempt_count > 0,
                ),
            )
        candidate = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                scheduling_predicate,
            )
            .order_by(ShadowRunResultRecord.schedule_ordinal)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if candidate is None:
            return None
        claimed_state = (
            SchedulingState.CONVERSATION_BOUND
            if candidate.conversation_id is not None
            else SchedulingState.CLAIMED
        )
        next_version = candidate.version + 1
        claimed = await self._session.execute(
            update(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.id == candidate.id,
                ShadowRunResultRecord.version == candidate.version,
            )
            .values(
                scheduling_state=claimed_state.value,
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
            state=claimed_state,
            lease_owner=worker_id,
            lease_expires_at=lease_expires_at,
            conversation_id=candidate.conversation_id,
            search_run_id=candidate.search_run_id,
            conversation_idempotency_key=candidate.conversation_idempotency_key,
            message_idempotency_key=candidate.message_idempotency_key,
            submission_request_hash=candidate.submission_request_hash,
            reservation_state=ReservationState(candidate.reservation_state),
            version=next_version,
            _persisted_version=next_version,
            conversation_attempt_count=candidate.conversation_attempt_count,
            submission_attempt_count=candidate.submission_attempt_count,
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

    async def reserve_run_budget(
        self,
        lease: RunLease,
    ) -> BudgetReservationReceipt:
        self._assert_tenant(lease.tenant_id)
        now, campaign, result = await self._locked_budget_records(
            lease.tenant_id,
            lease.campaign_id,
            lease.id,
        )
        self._assert_active_lease(result, lease, now)
        if campaign.status != CampaignStatus.RUNNING.value:
            intent = StopIntent(campaign.stop_intent)
            return BudgetReservationReceipt(
                False,
                intent if intent is not StopIntent.NONE else StopIntent.FATAL,
                campaign.stop_reason or "campaign_not_running",
            )

        reservation_state = ReservationState(result.reservation_state)
        if reservation_state is ReservationState.ACTIVE:
            raise InvariantViolation(
                "An active reservation must be reconciled before retrying the run",
                code="active_reservation_recovery_required",
            )
        if reservation_state is not ReservationState.NONE:
            raise InvariantViolation(
                "A terminal reservation cannot be reserved again",
                code="reservation_already_terminal",
            )

        request = self._reservation_request(campaign)
        admission = CampaignBudgetSnapshot(
            provider_call_admission_ceiling=(
                campaign.provider_call_admission_ceiling
            ),
            provider_call_structural_ceiling=(
                campaign.provider_call_structural_ceiling
            ),
            estimated_cost_stop_threshold=campaign.estimated_cost_stop_threshold,
            observed_provider_calls=campaign.observed_provider_calls,
            possibly_billed_call_charge=campaign.possibly_billed_call_charge,
            reserved_provider_calls=campaign.reserved_provider_calls,
            observed_estimated_cost=campaign.observed_estimated_cost,
            possibly_billed_cost_charge=campaign.possibly_billed_cost_charge,
            reserved_estimated_cost=campaign.reserved_estimated_cost,
        ).admit(request)
        next_result_version = result.version + 1
        if not admission.allowed:
            campaign.status = CampaignStatus.STOPPING.value
            campaign.stop_intent = admission.stop_intent.value
            campaign.stop_reason = f"Budget admission denied: {admission.reason}"
            campaign.version += 1
            campaign.updated_at = now
            result.budget_violation = True
            result.version = next_result_version
            result.updated_at = now
            await self._session.flush()
            lease.accept_budget_fence(next_result_version, ReservationState.NONE)
            return BudgetReservationReceipt(
                False,
                admission.stop_intent,
                admission.reason,
            )

        campaign.reserved_provider_calls += request.provider_calls
        campaign.reserved_estimated_cost += request.estimated_cost
        campaign.version += 1
        campaign.updated_at = now
        result.reserved_provider_calls = request.provider_calls
        result.reserved_estimated_cost = request.estimated_cost
        result.reservation_state = ReservationState.ACTIVE.value
        result.reservation_reserved_at = now
        result.version = next_result_version
        result.updated_at = now
        await self._session.flush()
        lease.accept_budget_fence(next_result_version, ReservationState.ACTIVE)
        return BudgetReservationReceipt(
            True,
            StopIntent.NONE,
            None,
            request.provider_calls,
            request.estimated_cost,
        )

    async def settle_run_budget(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
        source_snapshot_digest: str,
        usage: SettlementUsage,
    ) -> BudgetSettlementReceipt:
        self._assert_tenant(tenant_id)
        digest = source_snapshot_digest.strip()
        if (
            len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("source_snapshot_digest must be a lowercase SHA-256")
        now, campaign, result = await self._locked_budget_records(
            tenant_id,
            campaign_id,
            result_id,
        )
        reservation_state = ReservationState(result.reservation_state)
        if reservation_state is ReservationState.SETTLED:
            if self._settlement_matches(result, digest, usage):
                return BudgetSettlementReceipt(result.id, True, result.budget_violation)
            raise InvariantViolation(
                "A result budget was already settled from different source data",
                code="settlement_conflict",
            )
        if reservation_state is not ReservationState.ACTIVE:
            raise InvariantViolation(
                "Only active reservations can be settled",
                code="reservation_not_active",
            )
        if result.scheduling_state not in {
            SchedulingState.SUBMITTED.value,
            SchedulingState.COLLECTED.value,
            SchedulingState.FAILED.value,
        } or result.source_terminal_at is None:
            raise InvariantViolation(
                "Budget settlement requires a terminal source snapshot",
                code="source_not_terminal",
            )
        if result.source_snapshot_digest not in {None, digest}:
            raise InvariantViolation(
                "The terminal source snapshot changed before settlement",
                code="settlement_conflict",
            )

        reservation = ReservationRequest(
            result.reserved_provider_calls,
            Decimal(result.reserved_estimated_cost),
        )
        if (
            campaign.reserved_provider_calls < reservation.provider_calls
            or campaign.reserved_estimated_cost < reservation.estimated_cost
        ):
            raise InvariantViolation(
                "Campaign reservation ledgers are inconsistent",
                code="reservation_ledger_underflow",
            )
        campaign.reserved_provider_calls -= reservation.provider_calls
        campaign.reserved_estimated_cost -= reservation.estimated_cost
        campaign.observed_provider_calls += usage.observed_provider_calls
        campaign.observed_prompt_tokens += usage.prompt_tokens
        campaign.observed_completion_tokens += usage.completion_tokens
        campaign.observed_estimated_cost += usage.observed_estimated_cost
        campaign.possibly_billed_call_charge += usage.possibly_billed_call_charge
        campaign.possibly_billed_cost_charge += usage.possibly_billed_cost_charge
        if usage.possibly_billed_call_charge or usage.possibly_billed_cost_charge:
            campaign.possibly_billed_count += 1

        result.reservation_state = ReservationState.SETTLED.value
        result.settled_observed_provider_calls = usage.observed_provider_calls
        result.prompt_tokens = usage.prompt_tokens
        result.completion_tokens = usage.completion_tokens
        result.settled_observed_cost = usage.observed_estimated_cost
        result.estimated_cost = usage.observed_estimated_cost
        result.possibly_billed_call_charge = usage.possibly_billed_call_charge
        result.possibly_billed_cost_charge = usage.possibly_billed_cost_charge
        result.budget_settled_at = now
        result.source_snapshot_digest = digest
        result.budget_violation = usage.exceeds(reservation)
        result.version += 1
        result.updated_at = now

        stop_intent, stop_reason = self._budget_breach(campaign, reservation, usage)
        if (
            stop_intent is not StopIntent.NONE
            and campaign.status == CampaignStatus.RUNNING.value
        ):
            campaign.status = CampaignStatus.STOPPING.value
            campaign.stop_intent = stop_intent.value
            campaign.stop_reason = stop_reason
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        return BudgetSettlementReceipt(
            result.id,
            False,
            result.budget_violation,
        )

    async def release_run_budget(
        self,
        lease: RunLease,
    ) -> BudgetReleaseReceipt:
        self._assert_tenant(lease.tenant_id)
        now, campaign, result = await self._locked_budget_records(
            lease.tenant_id,
            lease.campaign_id,
            lease.id,
        )
        self._assert_active_lease(result, lease, now)
        reservation_state = ReservationState(result.reservation_state)
        if reservation_state is ReservationState.RELEASED:
            return BudgetReleaseReceipt(result.id, True)
        if reservation_state is not ReservationState.ACTIVE:
            raise InvariantViolation(
                "Only active reservations can be released",
                code="reservation_not_active",
            )
        if (
            result.submission_attempt_count
            or result.source_terminal_at is not None
            or result.source_snapshot_digest is not None
        ):
            raise InvariantViolation(
                "A reservation cannot be released after submission begins",
                code="reservation_release_too_late",
            )
        if (
            campaign.reserved_provider_calls < result.reserved_provider_calls
            or campaign.reserved_estimated_cost < result.reserved_estimated_cost
        ):
            raise InvariantViolation(
                "Campaign reservation ledgers are inconsistent",
                code="reservation_ledger_underflow",
            )
        campaign.reserved_provider_calls -= result.reserved_provider_calls
        campaign.reserved_estimated_cost -= result.reserved_estimated_cost
        campaign.version += 1
        campaign.updated_at = now
        next_result_version = result.version + 1
        result.reservation_state = ReservationState.RELEASED.value
        result.reservation_released_at = now
        result.version = next_result_version
        result.updated_at = now
        await self._session.flush()
        lease.accept_budget_fence(next_result_version, ReservationState.RELEASED)
        return BudgetReleaseReceipt(result.id, False)

    async def _locked_budget_records(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
    ) -> tuple[datetime, ShadowCampaignRecord, ShadowRunResultRecord]:
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if campaign is None:
            raise InvariantViolation(
                "Shadow campaign does not exist",
                code="campaign_not_found",
            )
        result = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                ShadowRunResultRecord.id == result_id,
            )
            .with_for_update()
        )
        if result is None:
            raise InvariantViolation(
                "Shadow campaign result does not exist",
                code="campaign_result_not_found",
            )
        return now, campaign, result

    @staticmethod
    def _assert_active_lease(
        result: ShadowRunResultRecord,
        lease: RunLease,
        now: datetime,
    ) -> None:
        if (
            result.scheduling_state
            not in {
                SchedulingState.CLAIMED.value,
                SchedulingState.CONVERSATION_BOUND.value,
            }
            or result.scheduling_state != lease.state.value
            or result.lease_owner != lease.lease_owner
            or result.lease_expires_at is None
            or result.lease_expires_at <= now
            or result.version != lease.persisted_version
        ):
            raise InvariantViolation(
                "Scheduling lease fencing token is stale",
                code="scheduling_lease_fence_lost",
            )

    @staticmethod
    def _reservation_request(campaign: ShadowCampaignRecord) -> ReservationRequest:
        calls, remainder = divmod(
            campaign.provider_call_structural_ceiling,
            campaign.max_runs,
        )
        if remainder or calls < 1:
            raise InvariantViolation(
                "Campaign structural call envelope is corrupt",
                code="campaign_budget_snapshot_invalid",
            )
        raw_cost = campaign.cost_rate_snapshot.get(
            "possibly_billed_run_reserve_usd"
        )
        if raw_cost is None:
            raise InvariantViolation(
                "Campaign cost-rate snapshot has no run reserve",
                code="campaign_budget_snapshot_invalid",
            )
        try:
            return ReservationRequest(calls, Decimal(str(raw_cost)))
        except (ValueError, ArithmeticError) as error:
            raise InvariantViolation(
                "Campaign cost-rate snapshot has an invalid run reserve",
                code="campaign_budget_snapshot_invalid",
            ) from error

    @staticmethod
    def _settlement_matches(
        result: ShadowRunResultRecord,
        digest: str,
        usage: SettlementUsage,
    ) -> bool:
        return (
            result.source_snapshot_digest == digest
            and result.settled_observed_provider_calls
            == usage.observed_provider_calls
            and result.prompt_tokens == usage.prompt_tokens
            and result.completion_tokens == usage.completion_tokens
            and result.settled_observed_cost == usage.observed_estimated_cost
            and result.possibly_billed_call_charge
            == usage.possibly_billed_call_charge
            and result.possibly_billed_cost_charge
            == usage.possibly_billed_cost_charge
        )

    @staticmethod
    def _budget_breach(
        campaign: ShadowCampaignRecord,
        reservation: ReservationRequest,
        usage: SettlementUsage,
    ) -> tuple[StopIntent, str | None]:
        total_calls = (
            campaign.observed_provider_calls
            + campaign.possibly_billed_call_charge
            + campaign.reserved_provider_calls
        )
        total_cost = (
            campaign.observed_estimated_cost
            + campaign.possibly_billed_cost_charge
            + campaign.reserved_estimated_cost
        )
        if total_calls > campaign.provider_call_structural_ceiling:
            return StopIntent.CALL_CEILING, "provider_call_structural_ceiling"
        if total_calls > campaign.provider_call_admission_ceiling:
            return StopIntent.CALL_CEILING, "provider_call_admission_ceiling"
        if (
            usage.observed_provider_calls + usage.possibly_billed_call_charge
            > reservation.provider_calls
        ):
            return StopIntent.CALL_CEILING, "run_provider_call_reservation"
        if total_cost > campaign.estimated_cost_stop_threshold:
            return StopIntent.BUDGET, "estimated_cost_stop_threshold"
        if (
            usage.observed_estimated_cost + usage.possibly_billed_cost_charge
            > reservation.estimated_cost
        ):
            return StopIntent.BUDGET, "run_estimated_cost_reservation"
        return StopIntent.NONE, None

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
