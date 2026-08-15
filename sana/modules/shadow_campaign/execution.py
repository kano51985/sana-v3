"""Crash-recoverable binding of campaign units to the candidate API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sana.modules.conversation.domain import normalized_content_sha256
from sana.modules.shadow_campaign.domain import ReservationState
from sana.modules.shadow_campaign.scheduler import RunLease
from sana.modules.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import CampaignUnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class CandidateSubmissionReceipt:
    message_id: UUID
    response_run_id: UUID
    search_run_id: UUID


@dataclass(frozen=True, slots=True)
class CampaignSubmissionReceipt:
    result_id: UUID
    search_run_id: UUID
    duplicate: bool = False


class CandidateGateway(Protocol):
    async def create_conversation(
        self,
        *,
        title: str,
        idempotency_key: str,
    ) -> UUID: ...

    async def submit_message(
        self,
        *,
        conversation_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt: ...


def campaign_conversation_title(case_id: str, repetition: int) -> str:
    return f"Shadow evaluation {case_id} repetition {repetition}"


class CampaignExecutionService:
    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        gateway: CandidateGateway,
    ) -> None:
        self._uow_factory = uow_factory
        self._gateway = gateway

    async def execute(
        self,
        lease: RunLease,
        prompt: str,
    ) -> CampaignSubmissionReceipt:
        if lease.reservation_state is not ReservationState.ACTIVE:
            raise InvariantViolation(
                "Candidate submission requires an active budget reservation",
                code="reservation_not_active",
            )
        if normalized_content_sha256(prompt) != lease.submission_request_hash:
            raise InvariantViolation(
                "Manifest prompt does not match the materialized request hash",
                code="submission_payload_mismatch",
            )

        conversation_id = lease.conversation_id
        if conversation_id is None:
            await self._prepare_conversation(lease)
            conversation_id = await self._gateway.create_conversation(
                title=campaign_conversation_title(lease.case_id, lease.repetition),
                idempotency_key=lease.conversation_idempotency_key,
            )
            await self._bind_conversation(lease, conversation_id)

        await self._prepare_submission(lease)
        receipt = await self._gateway.submit_message(
            conversation_id=conversation_id,
            content=prompt.strip(),
            idempotency_key=lease.message_idempotency_key,
        )
        return await self._bind_submission(lease, receipt)

    async def _prepare_conversation(self, lease: RunLease) -> None:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_execution.prepare_conversation_attempt(lease)
            await uow.commit()
        lease.conversation_attempt_count += 1

    async def _bind_conversation(
        self,
        lease: RunLease,
        conversation_id: UUID,
    ) -> None:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_execution.bind_conversation(
                lease,
                conversation_id,
            )
            await uow.commit()

    async def _prepare_submission(self, lease: RunLease) -> None:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_execution.prepare_submission_attempt(lease)
            await uow.commit()
        lease.submission_attempt_count += 1

    async def _bind_submission(
        self,
        lease: RunLease,
        receipt: CandidateSubmissionReceipt,
    ) -> CampaignSubmissionReceipt:
        async with self._uow_factory(lease.tenant_id) as uow:
            bound = await uow.campaign_execution.bind_submission(lease, receipt)
            await uow.commit()
            return bound
