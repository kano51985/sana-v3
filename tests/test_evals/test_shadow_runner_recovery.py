from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from types import SimpleNamespace

from sana.app.shadow_api_client import ShadowAPIError
from sana.app.shadow_runner import ShadowCampaignRunner
from sana.modules.identity.domain import Principal
from sana.modules.conversation.domain import normalized_content_sha256
from sana.modules.shadow_campaign.domain import ReservationState, SchedulingState
from sana.modules.shadow_campaign.execution import (
    CampaignExecutionService,
    CampaignSubmissionReceipt,
    CandidateSubmissionReceipt,
)
from sana.modules.shadow_campaign.scheduler import RunLease


NOW = datetime(2026, 8, 15, tzinfo=UTC)
PROMPT = "Recover this exact request"


class SimulatedProcessCrash(BaseException):
    pass


@dataclass
class DurableResult:
    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    state: SchedulingState = SchedulingState.CLAIMED
    conversation_id: UUID | None = None
    search_run_id: UUID | None = None
    version: int = 2

    def lease(self) -> RunLease | None:
        if self.state not in {
            SchedulingState.CLAIMED,
            SchedulingState.CONVERSATION_BOUND,
        }:
            return None
        return RunLease(
            self.id,
            self.tenant_id,
            self.campaign_id,
            "case-01",
            1,
            1,
            self.state,
            "recovery-worker",
            NOW + timedelta(minutes=1),
            self.conversation_id,
            self.search_run_id,
            "conversation-key",
            "message-key",
            normalized_content_sha256(PROMPT),
            ReservationState.ACTIVE,
            self.version,
            self.version,
        )


class DurableCandidateAPI:
    def __init__(self) -> None:
        self.conversations: dict[str, UUID] = {}
        self.submissions: dict[str, CandidateSubmissionReceipt] = {}
        self.crash_after_conversation_receipt = False
        self.crash_after_message_receipt = False

    async def create_conversation(self, *, title: str, idempotency_key: str) -> UUID:
        del title
        conversation_id = self.conversations.setdefault(idempotency_key, uuid4())
        if self.crash_after_conversation_receipt:
            self.crash_after_conversation_receipt = False
            raise SimulatedProcessCrash()
        return conversation_id

    async def submit_message(
        self,
        *,
        conversation_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt:
        del conversation_id, content
        receipt = self.submissions.setdefault(
            idempotency_key,
            CandidateSubmissionReceipt(uuid4(), uuid4(), uuid4()),
        )
        if self.crash_after_message_receipt:
            self.crash_after_message_receipt = False
            raise SimulatedProcessCrash()
        return receipt


class DurableExecutionRepository:
    def __init__(self, result: DurableResult) -> None:
        self.result = result
        self.crash_after_conversation_binding = False
        self.crash_after_run_binding = False

    async def prepare_conversation_attempt(self, lease: RunLease) -> None:
        self._advance(lease)

    async def bind_conversation(self, lease: RunLease, conversation_id: UUID) -> None:
        self.result.conversation_id = conversation_id
        self.result.state = SchedulingState.CONVERSATION_BOUND
        self._advance(lease, conversation_id=conversation_id)
        if self.crash_after_conversation_binding:
            self.crash_after_conversation_binding = False
            raise SimulatedProcessCrash()

    async def prepare_submission_attempt(self, lease: RunLease) -> None:
        self._advance(lease)

    async def bind_submission(
        self,
        lease: RunLease,
        receipt: CandidateSubmissionReceipt,
    ) -> CampaignSubmissionReceipt:
        self.result.search_run_id = receipt.search_run_id
        self.result.state = SchedulingState.SUBMITTED
        self.result.version += 1
        if self.crash_after_run_binding:
            self.crash_after_run_binding = False
            raise SimulatedProcessCrash()
        return CampaignSubmissionReceipt(self.result.id, receipt.search_run_id)

    def _advance(self, lease: RunLease, *, conversation_id: UUID | None = None) -> None:
        self.result.version += 1
        if conversation_id is None:
            lease.accept_attempt_fence(self.result.version)
        else:
            lease.accept_conversation_fence(self.result.version, conversation_id)


class FakeUnitOfWork:
    def __init__(self, repository: DurableExecutionRepository) -> None:
        self.campaign_execution = repository

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_point",
    (
        "conversation_receipt",
        "conversation_binding",
        "message_receipt",
        "run_binding",
    ),
)
async def test_four_receipt_crash_windows_never_create_a_second_search_run(
    crash_point: str,
) -> None:
    result = DurableResult(uuid4(), uuid4(), uuid4())
    repository = DurableExecutionRepository(result)
    candidate = DurableCandidateAPI()
    setattr(
        candidate if crash_point in {"conversation_receipt", "message_receipt"} else repository,
        f"crash_after_{crash_point}",
        True,
    )
    service = CampaignExecutionService(
        lambda tenant_id: FakeUnitOfWork(repository),
        candidate,
    )

    with pytest.raises(SimulatedProcessCrash):
        await service.execute(result.lease(), PROMPT)  # type: ignore[arg-type]

    recovered_lease = result.lease()
    if recovered_lease is not None:
        await service.execute(recovered_lease, PROMPT)

    assert len(candidate.conversations) == 1
    assert len(candidate.submissions) == 1
    assert result.search_run_id == candidate.submissions["message-key"].search_run_id
    assert result.state is SchedulingState.SUBMITTED


@pytest.mark.asyncio
async def test_runner_cancellation_persists_pause_before_propagating() -> None:
    runner = object.__new__(ShadowCampaignRunner)
    paused = asyncio.Event()

    async def blocked(principal, campaign_id, manifest):
        del principal, campaign_id, manifest
        await asyncio.Event().wait()

    async def pause(principal, campaign_id):
        del principal, campaign_id
        paused.set()

    runner._run_loop = blocked  # type: ignore[method-assign]
    runner._pause_on_interrupt = pause  # type: ignore[method-assign]
    principal = Principal(uuid4(), uuid4(), "test", "owner")
    task = asyncio.create_task(runner.run(principal, uuid4(), object()))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert paused.is_set()


@pytest.mark.asyncio
async def test_uncertain_post_is_replayed_once_before_possibly_billed_failure() -> None:
    runner = object.__new__(ShadowCampaignRunner)
    failures = []
    stops = []

    class Execution:
        async def execute(self, lease, prompt):
            del prompt
            lease.submission_attempt_count += 1
            raise ShadowAPIError(
                "transport_exhausted",
                status_code=None,
                retryable=True,
                request_may_have_committed=True,
            )

    async def stop(principal, campaign_id, reason):
        del principal, campaign_id
        stops.append(reason)

    async def mark(lease, failure):
        del lease
        failures.append(failure)

    runner._execution = Execution()
    runner._ensure_fatal_stop = stop  # type: ignore[method-assign]
    runner._mark_failure = mark  # type: ignore[method-assign]
    durable = DurableResult(
        uuid4(),
        uuid4(),
        uuid4(),
        SchedulingState.CONVERSATION_BOUND,
        uuid4(),
    )
    lease = durable.lease()
    assert lease is not None
    principal = Principal(lease.tenant_id, uuid4(), "test", "owner")
    manifest = SimpleNamespace(
        cases=(SimpleNamespace(id=lease.case_id, prompt=PROMPT),)
    )

    await runner._submit(principal, lease, manifest)
    assert failures == []

    await runner._submit(principal, lease, manifest)

    assert len(failures) == 1
    assert failures[0].possibly_billed
    assert stops == ["transport_exhausted", "transport_exhausted"]
