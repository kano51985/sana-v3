"""Tenant/owner-scoped Runner control and crash reconciliation adapter."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    GateStatus,
    ReservationState,
    SchedulingState,
    StopIntent,
    snapshot_hash,
)
from sana.modules.shadow_campaign.budget import SettlementUsage
from sana.modules.shadow_campaign.collector import CollectorLease
from sana.modules.shadow_campaign.runner import (
    CampaignReviewCandidate,
    CampaignRunState,
    CampaignRunSummary,
    RunnerFailure,
    RunnerFailureReceipt,
)
from sana.modules.shadow_campaign.scheduler import RunLease
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowManualReviewRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import SearchRunRecord
from sana.platform.db.shadow_campaign_repository import SqlShadowCampaignRepository


class SqlShadowRunnerRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def list_owned(
        self,
        tenant_id: UUID,
        user_id: UUID,
    ) -> tuple[CampaignRunSummary, ...]:
        self._assert_tenant(tenant_id)
        rows = (
            await self._session.execute(
                select(ShadowCampaignRecord)
                .where(
                    ShadowCampaignRecord.tenant_id == tenant_id,
                    ShadowCampaignRecord.created_by_user_id == user_id,
                )
                .order_by(
                    ShadowCampaignRecord.created_at.desc(),
                    ShadowCampaignRecord.id,
                )
            )
        ).scalars()
        return tuple(
            CampaignRunSummary(
                row.id,
                CampaignStatus(row.status),
                GateStatus(row.gate_status),
                row.profile_version,
                row.planned_count,
                row.submitted_count,
                row.collected_count + row.failed_count,
                row.skipped_count,
                row.created_at,
            )
            for row in rows
        )

    async def read_owned_state(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
    ) -> CampaignRunState | None:
        self._assert_tenant(tenant_id)
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord).where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
                ShadowCampaignRecord.created_by_user_id == user_id,
            )
        )
        if campaign is None:
            return None
        counts = dict(
            (
                await self._session.execute(
                    select(
                        ShadowRunResultRecord.scheduling_state,
                        func.count(ShadowRunResultRecord.id),
                    )
                    .where(
                        ShadowRunResultRecord.tenant_id == tenant_id,
                        ShadowRunResultRecord.campaign_id == campaign_id,
                    )
                    .group_by(ShadowRunResultRecord.scheduling_state)
                )
            ).all()
        )
        active_reservations = int(
            await self._session.scalar(
                select(func.count(ShadowRunResultRecord.id)).where(
                    ShadowRunResultRecord.tenant_id == tenant_id,
                    ShadowRunResultRecord.campaign_id == campaign_id,
                    ShadowRunResultRecord.reservation_state
                    == ReservationState.ACTIVE.value,
                )
            )
            or 0
        )
        selected_reviews, completed_reviews = (
            await self._session.execute(
                select(
                    func.count(ShadowRunResultRecord.id).filter(
                        ShadowRunResultRecord.manual_review_selected.is_(True)
                    ),
                    func.count(ShadowManualReviewRecord.id),
                )
                .select_from(ShadowRunResultRecord)
                .outerjoin(
                    ShadowManualReviewRecord,
                    (
                        ShadowManualReviewRecord.tenant_id
                        == ShadowRunResultRecord.tenant_id
                    )
                    & (
                        ShadowManualReviewRecord.campaign_id
                        == ShadowRunResultRecord.campaign_id
                    )
                    & (
                        ShadowManualReviewRecord.result_id
                        == ShadowRunResultRecord.id
                    ),
                )
                .where(
                    ShadowRunResultRecord.tenant_id == tenant_id,
                    ShadowRunResultRecord.campaign_id == campaign_id,
                )
            )
        ).one()
        return CampaignRunState(
            campaign.id,
            campaign.tenant_id,
            campaign.created_by_user_id,
            CampaignStatus(campaign.status),
            GateStatus(campaign.gate_status),
            StopIntent(campaign.stop_intent),
            campaign.profile_version,
            campaign.max_runs,
            int(counts.get(SchedulingState.PENDING.value, 0)),
            int(counts.get(SchedulingState.CLAIMED.value, 0))
            + int(counts.get(SchedulingState.CONVERSATION_BOUND.value, 0)),
            int(counts.get(SchedulingState.SUBMITTED.value, 0)),
            int(counts.get(SchedulingState.COLLECTED.value, 0)),
            int(counts.get(SchedulingState.FAILED.value, 0)),
            int(counts.get(SchedulingState.SKIPPED.value, 0)),
            active_reservations,
            int(selected_reviews or 0),
            int(completed_reviews or 0),
            campaign.review_deadline_at,
        )

    async def mark_failure(
        self,
        lease: RunLease,
        failure: RunnerFailure,
    ) -> RunnerFailureReceipt:
        self._assert_tenant(lease.tenant_id)
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == lease.tenant_id,
                ShadowCampaignRecord.id == lease.campaign_id,
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
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.campaign_id == lease.campaign_id,
                ShadowRunResultRecord.id == lease.id,
            )
            .with_for_update()
        )
        if result is None:
            raise InvariantViolation(
                "Shadow campaign result does not exist",
                code="campaign_result_not_found",
            )
        if result.scheduling_state == SchedulingState.FAILED.value:
            return RunnerFailureReceipt(
                result.id,
                bool(result.possibly_billed_call_charge),
                True,
            )
        if (
            result.scheduling_state
            not in {
                SchedulingState.CLAIMED.value,
                SchedulingState.CONVERSATION_BOUND.value,
            }
            or result.lease_owner != lease.lease_owner
            or result.lease_expires_at is None
            or result.lease_expires_at <= now
            or result.version != lease.persisted_version
        ):
            raise InvariantViolation(
                "Scheduling lease fencing token is stale",
                code="scheduling_lease_fence_lost",
            )
        reservation = ReservationState(result.reservation_state)
        if failure.possibly_billed and reservation is not ReservationState.ACTIVE:
            raise InvariantViolation(
                "A possibly billed failure requires an active reservation",
                code="possibly_billed_without_reservation",
            )
        if reservation is ReservationState.ACTIVE:
            campaign.reserved_provider_calls -= result.reserved_provider_calls
            campaign.reserved_estimated_cost -= result.reserved_estimated_cost
            if failure.possibly_billed:
                digest = snapshot_hash(
                    {
                        "schema": "shadow-runner-failure-v1",
                        "tenant_id": lease.tenant_id,
                        "campaign_id": lease.campaign_id,
                        "result_id": lease.id,
                        "error_class": failure.error_class,
                        "error_code": failure.error_code,
                        "failed_phase": failure.failed_phase,
                        "reserved_provider_calls": result.reserved_provider_calls,
                        "reserved_estimated_cost": result.reserved_estimated_cost,
                    }
                )
                campaign.possibly_billed_call_charge += result.reserved_provider_calls
                campaign.possibly_billed_cost_charge += result.reserved_estimated_cost
                campaign.possibly_billed_count += 1
                result.possibly_billed_call_charge = result.reserved_provider_calls
                result.possibly_billed_cost_charge = result.reserved_estimated_cost
                result.reservation_state = ReservationState.SETTLED.value
                result.budget_settled_at = now
                result.source_terminal_at = now
                result.source_snapshot_digest = digest
            else:
                result.reservation_state = ReservationState.RELEASED.value
                result.reservation_released_at = now
        elif reservation is not ReservationState.NONE:
            raise InvariantViolation(
                "A terminal reservation cannot be failed again",
                code="reservation_already_terminal",
            )
        result.scheduling_state = SchedulingState.FAILED.value
        result.lease_owner = None
        result.lease_expires_at = None
        result.error_class = failure.error_class.value
        result.error_code = failure.error_code
        result.failed_phase = failure.failed_phase
        result.error_signal_flags = [failure.error_code]
        result.version += 1
        result.updated_at = now
        campaign.failed_count += 1
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        return RunnerFailureReceipt(result.id, failure.possibly_billed, False)

    async def mark_collector_failure(
        self,
        lease: CollectorLease,
        failure: RunnerFailure,
    ) -> RunnerFailureReceipt:
        """Fail a submitted unit while settling from its durable invocation audit."""

        self._assert_tenant(lease.tenant_id)
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == lease.tenant_id,
                ShadowCampaignRecord.id == lease.campaign_id,
            )
            .with_for_update()
        )
        result = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.campaign_id == lease.campaign_id,
                ShadowRunResultRecord.id == lease.id,
            )
            .with_for_update()
        )
        if campaign is None or result is None:
            raise InvariantViolation(
                "Collector failure binding was not found",
                code="campaign_result_not_found",
            )
        if result.scheduling_state == SchedulingState.FAILED.value:
            return RunnerFailureReceipt(
                result.id,
                bool(result.possibly_billed_call_charge),
                True,
            )
        if (
            result.scheduling_state != SchedulingState.SUBMITTED.value
            or result.search_run_id != lease.search_run_id
            or result.collector_lease_owner != lease.lease_owner
            or result.collector_lease_expires_at is None
            or result.collector_lease_expires_at <= now
            or result.version != lease.persisted_version
            or result.reservation_state != ReservationState.ACTIVE.value
        ):
            raise InvariantViolation(
                "Collector lease fencing token is stale",
                code="collector_lease_fence_lost",
            )
        run = await self._session.scalar(
            select(SearchRunRecord).where(
                SearchRunRecord.tenant_id == lease.tenant_id,
                SearchRunRecord.id == lease.search_run_id,
            )
        )
        if run is None or run.completed_at is None:
            raise InvariantViolation(
                "Collector failure cannot seal a non-terminal source",
                code="source_not_terminal",
            )
        invocations = tuple(
            (
                await self._session.scalars(
                    select(ModelInvocationRecord)
                    .where(
                        ModelInvocationRecord.tenant_id == lease.tenant_id,
                        ModelInvocationRecord.run_id == lease.search_run_id,
                    )
                    .order_by(ModelInvocationRecord.id)
                )
            ).all()
        )
        billed = tuple(
            item
            for item in invocations
            if item.provider_called and item.billing_disposition == "BILLED"
        )
        possibly = tuple(
            item
            for item in invocations
            if item.provider_called
            and item.billing_disposition == "POSSIBLY_BILLED"
        )
        prompt_tokens = sum(item.prompt_tokens for item in billed)
        completion_tokens = sum(item.completion_tokens for item in billed)
        rate = campaign.cost_rate_snapshot
        observed_cost = (
            Decimal(prompt_tokens) * Decimal(str(rate["prompt_per_million_usd"]))
            + Decimal(completion_tokens)
            * Decimal(str(rate["completion_per_million_usd"]))
        ) / Decimal(1_000_000)
        usage = SettlementUsage(
            len(billed),
            prompt_tokens,
            completion_tokens,
            observed_cost,
            len(possibly),
            Decimal(str(rate["possibly_billed_run_reserve_usd"]))
            if possibly
            else Decimal(0),
        )
        digest = snapshot_hash(
            {
                "schema": "shadow-collector-failure-v1",
                "tenant_id": lease.tenant_id,
                "campaign_id": lease.campaign_id,
                "result_id": lease.id,
                "search_run_id": lease.search_run_id,
                "source_terminal_at": run.completed_at,
                "failure": failure,
                "invocations": tuple(
                    {
                        "id": item.id,
                        "status": item.status,
                        "billing_disposition": item.billing_disposition,
                        "provider_called": item.provider_called,
                        "prompt_tokens": item.prompt_tokens,
                        "completion_tokens": item.completion_tokens,
                        "completed_at": item.completed_at,
                    }
                    for item in invocations
                ),
            }
        )
        result.source_terminal_at = run.completed_at
        result.scheduling_state = SchedulingState.FAILED.value
        result.collector_lease_owner = None
        result.collector_lease_expires_at = None
        result.model_call_count = len(
            tuple(item for item in invocations if item.provider_called)
        )
        result.error_class = failure.error_class.value
        result.error_code = failure.error_code
        result.failed_phase = failure.failed_phase
        result.error_signal_flags = [failure.error_code]
        result.version += 1
        result.updated_at = now
        campaign.failed_count += 1
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        await SqlShadowCampaignRepository(
            self._session,
            lease.tenant_id,
        ).settle_run_budget(
            lease.tenant_id,
            lease.campaign_id,
            lease.id,
            digest,
            usage,
        )
        return RunnerFailureReceipt(result.id, bool(possibly), False)

    async def skip_pending(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        reason: str,
    ) -> int:
        self._assert_tenant(tenant_id)
        normalized_reason = reason.strip()
        if (
            not normalized_reason
            or len(normalized_reason) > 100
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
                for character in normalized_reason
            )
        ):
            raise ValueError("Skip reason must be a stable lowercase identifier")
        now = await self._session.scalar(select(func.clock_timestamp()))
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if now is None or campaign is None:
            raise InvariantViolation(
                "Campaign was unavailable while skipping pending units",
                code="campaign_not_found",
            )
        if campaign.status != CampaignStatus.STOPPING.value:
            raise InvariantViolation(
                "Only a STOPPING campaign can seal pending units",
                code="campaign_not_stopping",
            )
        result = await self._session.execute(
            update(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                ShadowRunResultRecord.scheduling_state
                == SchedulingState.PENDING.value,
            )
            .values(
                scheduling_state=SchedulingState.SKIPPED.value,
                stable_skip_reason=normalized_reason,
                version=ShadowRunResultRecord.version + 1,
                updated_at=now,
            )
        )
        skipped = int(result.rowcount or 0)
        if skipped:
            campaign.skipped_count += skipped
            campaign.version += 1
            campaign.updated_at = now
            await self._session.flush()
        return skipped

    async def review_candidates(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
    ) -> tuple[CampaignReviewCandidate, ...]:
        self._assert_tenant(tenant_id)
        reviewed = exists(
            select(ShadowManualReviewRecord.id).where(
                ShadowManualReviewRecord.tenant_id == tenant_id,
                ShadowManualReviewRecord.campaign_id == campaign_id,
                ShadowManualReviewRecord.result_id == ShadowRunResultRecord.id,
            )
        )
        rows = (
            await self._session.execute(
                select(
                    ShadowRunResultRecord,
                    ShadowCampaignRecord.review_rubric_version,
                    reviewed.label("reviewed"),
                )
                .join(
                    ShadowCampaignRecord,
                    (
                        ShadowCampaignRecord.tenant_id
                        == ShadowRunResultRecord.tenant_id
                    )
                    & (
                        ShadowCampaignRecord.id
                        == ShadowRunResultRecord.campaign_id
                    ),
                )
                .where(
                    ShadowRunResultRecord.tenant_id == tenant_id,
                    ShadowRunResultRecord.campaign_id == campaign_id,
                    ShadowRunResultRecord.manual_review_selected.is_(True),
                    ShadowCampaignRecord.created_by_user_id == user_id,
                )
                .order_by(ShadowRunResultRecord.schedule_ordinal)
            )
        ).all()
        return tuple(
            CampaignReviewCandidate(
                result.id,
                result.conversation_id,
                result.search_run_id,
                result.case_id,
                result.repetition,
                result.answerability,
                result.answer_quality or "UNKNOWN",
                rubric_version,
                bool(is_reviewed),
            )
            for result, rubric_version, is_reviewed in rows
            if result.conversation_id is not None
            and result.search_run_id is not None
            and result.scheduling_state == SchedulingState.COLLECTED.value
        )


__all__ = ["SqlShadowRunnerRepository"]
