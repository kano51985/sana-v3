"""PostgreSQL fencing adapter for candidate Conversation/Message binding."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.conversation.domain import normalized_content_sha256
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ReservationState,
    SchedulingState,
)
from sana.modules.shadow_campaign.execution import (
    CampaignSubmissionReceipt,
    CandidateSubmissionReceipt,
    campaign_conversation_title,
)
from sana.modules.shadow_campaign.scheduler import RunLease
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.conversation import Conversation, Message
from sana.platform.db.models.orchestration import SearchRunRecord
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowRunResultRecord,
)


class SqlShadowExecutionRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def prepare_conversation_attempt(self, lease: RunLease) -> None:
        now, campaign, result = await self._locked_records(lease)
        self._assert_active_lease(result, lease, now)
        self._assert_active_reservation(result)
        self._assert_outbound_allowed(campaign, result)
        if (
            result.scheduling_state != SchedulingState.CLAIMED.value
            or result.conversation_id is not None
            or result.search_run_id is not None
        ):
            raise InvariantViolation(
                "Conversation creation is not valid in the current Result state",
                code="conversation_attempt_state_invalid",
            )
        next_version = result.version + 1
        result.conversation_attempt_count += 1
        result.version = next_version
        result.updated_at = now
        await self._session.flush()
        lease.accept_attempt_fence(next_version)

    async def bind_conversation(
        self,
        lease: RunLease,
        conversation_id: UUID,
    ) -> None:
        now, campaign, result = await self._locked_records(lease)
        self._assert_active_lease(result, lease, now)
        self._assert_active_reservation(result)
        if result.conversation_id is not None:
            if (
                result.conversation_id == conversation_id
                and result.scheduling_state
                == SchedulingState.CONVERSATION_BOUND.value
            ):
                return
            raise InvariantViolation(
                "A Result cannot replace its bound Conversation",
                code="conversation_binding_conflict",
            )
        if result.scheduling_state != SchedulingState.CLAIMED.value:
            raise InvariantViolation(
                "Conversation binding is not valid in the current Result state",
                code="conversation_binding_state_invalid",
            )
        authoritative = (
            await self._session.execute(
                select(
                    Conversation.creation_idempotency_key,
                    Conversation.creation_request_hash,
                    Conversation.title,
                ).where(
                    Conversation.tenant_id == lease.tenant_id,
                    Conversation.id == conversation_id,
                    Conversation.user_id == campaign.created_by_user_id,
                )
            )
        ).one_or_none()
        expected_title = campaign_conversation_title(
            result.case_id,
            result.repetition,
        )
        expected_title_hash = normalized_content_sha256(expected_title)
        if (
            authoritative is None
            or authoritative[0] != result.conversation_idempotency_key
            or authoritative[2] != expected_title
            or authoritative[1] != expected_title_hash
        ):
            raise InvariantViolation(
                "Candidate Conversation receipt is not authoritative",
                code="candidate_conversation_mismatch",
            )
        collision = await self._session.scalar(
            select(ShadowRunResultRecord.id).where(
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.conversation_id == conversation_id,
                ShadowRunResultRecord.id != lease.id,
            )
        )
        if collision is not None:
            raise InvariantViolation(
                "Candidate Conversation is already bound to another Result",
                code="conversation_already_bound",
            )
        next_version = result.version + 1
        result.conversation_id = conversation_id
        result.scheduling_state = SchedulingState.CONVERSATION_BOUND.value
        result.version = next_version
        result.updated_at = now
        await self._session.flush()
        lease.accept_conversation_fence(next_version, conversation_id)

    async def prepare_submission_attempt(self, lease: RunLease) -> None:
        now, campaign, result = await self._locked_records(lease)
        self._assert_active_lease(result, lease, now)
        self._assert_outbound_allowed(campaign, result)
        if (
            result.scheduling_state
            != SchedulingState.CONVERSATION_BOUND.value
            or result.conversation_id is None
            or result.search_run_id is not None
        ):
            raise InvariantViolation(
                "Message submission is not valid in the current Result state",
                code="submission_attempt_state_invalid",
            )
        next_version = result.version + 1
        result.submission_attempt_count += 1
        result.version = next_version
        result.updated_at = now
        await self._session.flush()
        lease.accept_attempt_fence(next_version)

    async def bind_submission(
        self,
        lease: RunLease,
        receipt: CandidateSubmissionReceipt,
    ) -> CampaignSubmissionReceipt:
        now, campaign, result = await self._locked_records(lease)
        if result.search_run_id is not None:
            if (
                result.search_run_id != receipt.search_run_id
                or result.scheduling_state != SchedulingState.SUBMITTED.value
            ):
                raise InvariantViolation(
                    "A Result cannot replace its bound SearchRun",
                    code="search_run_binding_conflict",
                )
            await self._assert_authoritative_receipt(campaign, result, receipt)
            return CampaignSubmissionReceipt(
                result.id,
                receipt.search_run_id,
                duplicate=True,
            )

        self._assert_active_lease(result, lease, now)
        self._assert_active_reservation(result)
        if (
            result.scheduling_state
            != SchedulingState.CONVERSATION_BOUND.value
            or result.conversation_id is None
        ):
            raise InvariantViolation(
                "SearchRun binding is not valid in the current Result state",
                code="search_run_binding_state_invalid",
            )
        await self._assert_authoritative_receipt(campaign, result, receipt)
        collision = await self._session.scalar(
            select(ShadowRunResultRecord.id).where(
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.search_run_id == receipt.search_run_id,
                ShadowRunResultRecord.id != lease.id,
            )
        )
        if collision is not None:
            raise InvariantViolation(
                "Candidate SearchRun is already bound to another Result",
                code="search_run_already_bound",
            )
        if campaign.submitted_count >= campaign.max_runs:
            raise InvariantViolation(
                "Campaign submitted counter cannot exceed max_runs",
                code="campaign_ledger_mismatch",
            )
        result.search_run_id = receipt.search_run_id
        result.scheduling_state = SchedulingState.SUBMITTED.value
        result.lease_owner = None
        result.lease_expires_at = None
        result.version += 1
        result.updated_at = now
        campaign.submitted_count += 1
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        return CampaignSubmissionReceipt(result.id, receipt.search_run_id)

    async def _locked_records(
        self,
        lease: RunLease,
    ) -> tuple[datetime, ShadowCampaignRecord, ShadowRunResultRecord]:
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
    def _assert_outbound_allowed(
        campaign: ShadowCampaignRecord,
        result: ShadowRunResultRecord,
    ) -> None:
        if campaign.status != CampaignStatus.RUNNING.value:
            raise InvariantViolation(
                "Campaign is not accepting new candidate API calls",
                code="campaign_not_running",
            )
        SqlShadowExecutionRepository._assert_active_reservation(result)

    @staticmethod
    def _assert_active_reservation(result: ShadowRunResultRecord) -> None:
        if result.reservation_state != ReservationState.ACTIVE.value:
            raise InvariantViolation(
                "Candidate API calls require an active budget reservation",
                code="reservation_not_active",
            )

    async def _assert_authoritative_receipt(
        self,
        campaign: ShadowCampaignRecord,
        result: ShadowRunResultRecord,
        receipt: CandidateSubmissionReceipt,
    ) -> None:
        row = (
            await self._session.execute(
                select(
                    SearchRunRecord.message_id,
                    SearchRunRecord.response_run_id,
                    SearchRunRecord.conversation_id,
                    Message.conversation_id,
                    Message.author_user_id,
                    Message.idempotency_key,
                    Message.content,
                )
                .join(
                    Message,
                    (Message.tenant_id == SearchRunRecord.tenant_id)
                    & (Message.id == SearchRunRecord.message_id),
                )
                .where(
                    SearchRunRecord.tenant_id == result.tenant_id,
                    SearchRunRecord.id == receipt.search_run_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise InvariantViolation(
                "Candidate SearchRun receipt is not authoritative",
                code="candidate_search_run_not_found",
            )
        if (
            row[0] != receipt.message_id
            or row[1] != receipt.response_run_id
            or row[2] != result.conversation_id
            or row[3] != result.conversation_id
            or row[4] != campaign.created_by_user_id
            or row[5] != result.message_idempotency_key
        ):
            raise InvariantViolation(
                "Candidate SearchRun receipt does not match the Result binding",
                code="candidate_receipt_mismatch",
            )
        if normalized_content_sha256(row[6]) != result.submission_request_hash:
            raise InvariantViolation(
                "Candidate Message content does not match the materialized request",
                code="submission_payload_mismatch",
            )
