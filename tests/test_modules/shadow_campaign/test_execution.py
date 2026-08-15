from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sana.modules.conversation.domain import normalized_content_sha256
from sana.modules.shadow_campaign.domain import ReservationState, SchedulingState
from sana.modules.shadow_campaign.execution import (
    CampaignExecutionService,
    CampaignSubmissionReceipt,
    CandidateSubmissionReceipt,
)
from sana.modules.shadow_campaign.scheduler import RunLease
from sana.modules.shared.errors import InvariantViolation


NOW = datetime(2026, 8, 15, tzinfo=UTC)
PROMPT = "What changed in the current release?"


def _lease(*, conversation_id: UUID | None = None) -> RunLease:
    state = (
        SchedulingState.CONVERSATION_BOUND
        if conversation_id is not None
        else SchedulingState.CLAIMED
    )
    return RunLease(
        id=uuid4(),
        tenant_id=uuid4(),
        campaign_id=uuid4(),
        case_id="case-01",
        repetition=1,
        schedule_ordinal=1,
        state=state,
        lease_owner="worker-a",
        lease_expires_at=NOW + timedelta(seconds=30),
        conversation_id=conversation_id,
        search_run_id=None,
        conversation_idempotency_key="conversation-key",
        message_idempotency_key="message-key",
        submission_request_hash=normalized_content_sha256(PROMPT),
        reservation_state=ReservationState.ACTIVE,
        version=2,
        _persisted_version=2,
    )


class FakeExecutionRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.submission = CandidateSubmissionReceipt(uuid4(), uuid4(), uuid4())

    async def prepare_conversation_attempt(self, lease: RunLease) -> None:
        self.calls.append("prepare_conversation")
        lease.accept_attempt_fence(lease.version + 1)

    async def bind_conversation(
        self,
        lease: RunLease,
        conversation_id: UUID,
    ) -> None:
        self.calls.append("bind_conversation")
        lease.accept_conversation_fence(lease.version + 1, conversation_id)

    async def prepare_submission_attempt(self, lease: RunLease) -> None:
        self.calls.append("prepare_submission")
        lease.accept_attempt_fence(lease.version + 1)

    async def bind_submission(
        self,
        lease: RunLease,
        receipt: CandidateSubmissionReceipt,
    ) -> CampaignSubmissionReceipt:
        self.calls.append("bind_submission")
        assert receipt == self.submission
        return CampaignSubmissionReceipt(lease.id, receipt.search_run_id)


class FakeUnitOfWork:
    def __init__(self, repository: FakeExecutionRepository) -> None:
        self.campaign_execution = repository
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class FakeGateway:
    def __init__(self, repository: FakeExecutionRepository) -> None:
        self.repository = repository
        self.conversation_id = uuid4()
        self.calls: list[str] = []

    async def create_conversation(self, *, title: str, idempotency_key: str) -> UUID:
        self.calls.append("create_conversation")
        assert title == "Shadow evaluation case-01 repetition 1"
        assert idempotency_key == "conversation-key"
        return self.conversation_id

    async def submit_message(
        self,
        *,
        conversation_id: UUID,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt:
        self.calls.append("submit_message")
        assert conversation_id == self.conversation_id
        assert content == PROMPT
        assert idempotency_key == "message-key"
        return self.repository.submission


@pytest.mark.asyncio
async def test_execution_commits_attempts_before_each_candidate_api_call() -> None:
    lease = _lease()
    repository = FakeExecutionRepository()
    gateway = FakeGateway(repository)
    service = CampaignExecutionService(
        lambda tenant_id: FakeUnitOfWork(repository),
        gateway,
    )

    receipt = await service.execute(lease, PROMPT)

    assert receipt.search_run_id == repository.submission.search_run_id
    assert repository.calls == [
        "prepare_conversation",
        "bind_conversation",
        "prepare_submission",
        "bind_submission",
    ]
    assert gateway.calls == ["create_conversation", "submit_message"]
    assert lease.conversation_id == gateway.conversation_id
    assert lease.state is SchedulingState.CONVERSATION_BOUND


@pytest.mark.asyncio
async def test_recovery_with_bound_conversation_never_creates_another_one() -> None:
    repository = FakeExecutionRepository()
    gateway = FakeGateway(repository)
    lease = _lease(conversation_id=gateway.conversation_id)
    service = CampaignExecutionService(
        lambda tenant_id: FakeUnitOfWork(repository),
        gateway,
    )

    await service.execute(lease, PROMPT)

    assert repository.calls == ["prepare_submission", "bind_submission"]
    assert gateway.calls == ["submit_message"]


@pytest.mark.asyncio
async def test_payload_mismatch_fails_before_any_candidate_or_database_call() -> None:
    repository = FakeExecutionRepository()
    gateway = FakeGateway(repository)
    service = CampaignExecutionService(
        lambda tenant_id: FakeUnitOfWork(repository),
        gateway,
    )

    with pytest.raises(InvariantViolation) as error:
        await service.execute(_lease(), "different prompt")

    assert error.value.code == "submission_payload_mismatch"
    assert repository.calls == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_submission_without_active_reservation_fails_before_outbound() -> None:
    repository = FakeExecutionRepository()
    gateway = FakeGateway(repository)
    lease = _lease()
    lease.reservation_state = ReservationState.NONE
    service = CampaignExecutionService(
        lambda tenant_id: FakeUnitOfWork(repository),
        gateway,
    )

    with pytest.raises(InvariantViolation) as error:
        await service.execute(lease, PROMPT)

    assert error.value.code == "reservation_not_active"
    assert gateway.calls == []
