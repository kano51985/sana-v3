"""Tenant-scoped SQLAlchemy repositories returning domain values and DTOs."""

from __future__ import annotations

from uuid import UUID

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.conversation.domain import (
    MessageDraft,
    ResponseRunDraft,
    SubmissionReceipt,
)
from sana.modules.orchestration.domain import SearchRun, SearchStep, StepAttempt
from sana.modules.orchestration.outbox import (
    OutboxMessage,
    PendingOutboxMessage,
    trace_context_from_dict,
    trace_context_to_dict,
)
from sana.modules.orchestration.events import RunEventData
from sana.modules.orchestration.repository import (
    artifact_to_dict,
    budget_to_dict,
    run_from_record,
    step_from_record,
    usage_to_dict,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.orchestration import (
    OutboxEvent,
    RunEvent,
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)


class SqlConversationRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def is_owned_by(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        user_id: UUID,
    ) -> bool:
        self._assert_tenant(tenant_id)
        statement = select(Conversation.id).where(
            Conversation.tenant_id == tenant_id,
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        return (await self._session.scalar(statement)) is not None

    async def find_submission(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> SubmissionReceipt | None:
        self._assert_tenant(tenant_id)
        statement = (
            select(
                Message.id,
                ResponseRun.id,
                SearchRunRecord.id,
                SearchRunRecord.status,
            )
            .join(
                ResponseRun,
                (ResponseRun.tenant_id == Message.tenant_id)
                & (ResponseRun.message_id == Message.id),
            )
            .join(
                SearchRunRecord,
                (SearchRunRecord.tenant_id == ResponseRun.tenant_id)
                & (SearchRunRecord.response_run_id == ResponseRun.id),
            )
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation_id,
                Message.idempotency_key == idempotency_key,
            )
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return SubmissionReceipt(
            message_id=row[0],
            response_run_id=row[1],
            search_run_id=row[2],
            status=row[3],
        )

    async def add_message(self, message: MessageDraft) -> None:
        self._assert_tenant(message.tenant_id)
        self._session.add(
            Message(
                id=message.id,
                tenant_id=message.tenant_id,
                conversation_id=message.conversation_id,
                author_user_id=message.author_user_id,
                role=message.role.value,
                content=message.content,
                message_metadata={},
                idempotency_key=message.idempotency_key,
                created_at=message.created_at,
            )
        )
        # These records are intentionally mapped without ORM relationships.
        # Flush each aggregate parent before repositories stage its dependants,
        # otherwise SQLAlchemy has no unit-of-work relationship edge from which
        # to derive a safe INSERT order.
        await self._session.flush()


class SqlResponseRunRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(self, response_run: ResponseRunDraft) -> None:
        if response_run.tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )
        self._session.add(
            ResponseRun(
                id=response_run.id,
                tenant_id=response_run.tenant_id,
                conversation_id=response_run.conversation_id,
                message_id=response_run.message_id,
                status=response_run.status,
                created_at=response_run.created_at,
                updated_at=response_run.created_at,
            )
        )
        await self._session.flush()


class SqlRunRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def get(self, tenant_id: UUID, run_id: UUID) -> SearchRun | None:
        self._assert_tenant(tenant_id)
        statement = select(SearchRunRecord).where(
            SearchRunRecord.tenant_id == tenant_id,
            SearchRunRecord.id == run_id,
        )
        record = await self._session.scalar(statement)
        return run_from_record(record) if record is not None else None

    async def get_for_update(
        self,
        tenant_id: UUID,
        run_id: UUID,
    ) -> SearchRun | None:
        self._assert_tenant(tenant_id)
        statement = (
            select(SearchRunRecord)
            .where(
                SearchRunRecord.tenant_id == tenant_id,
                SearchRunRecord.id == run_id,
            )
            .with_for_update()
        )
        record = await self._session.scalar(statement)
        return run_from_record(record) if record is not None else None

    async def add(self, run: SearchRun) -> None:
        self._assert_tenant(run.tenant_id)
        self._session.add(
            SearchRunRecord(
                id=run.id,
                tenant_id=run.tenant_id,
                response_run_id=run.response_run_id,
                conversation_id=run.conversation_id,
                message_id=run.message_id,
                mode=run.mode.value,
                route_reason_codes=list(run.routing.reason_codes),
                policy_version=run.routing.policy_version,
                route_confidence=run.routing.confidence,
                status=run.status.value,
                answer_quality=run.answer_quality.value,
                stop_reason=run.stop_reason.value if run.stop_reason else None,
                soft_deadline_at=run.budget.soft_deadline_at,
                hard_deadline_at=run.budget.hard_deadline_at,
                budget_snapshot=budget_to_dict(run.budget),
                usage_snapshot=usage_to_dict(run.usage),
                created_at=run.budget.created_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                version=run.version,
            )
        )
        await self._session.flush()

    async def save(self, run: SearchRun) -> None:
        self._assert_tenant(run.tenant_id)
        expected_version = run.persisted_version
        statement = (
            update(SearchRunRecord)
            .where(
                SearchRunRecord.tenant_id == run.tenant_id,
                SearchRunRecord.id == run.id,
                SearchRunRecord.version == expected_version,
            )
            .values(
                mode=run.mode.value,
                route_reason_codes=list(run.routing.reason_codes),
                policy_version=run.routing.policy_version,
                route_confidence=run.routing.confidence,
                status=run.status.value,
                answer_quality=run.answer_quality.value,
                stop_reason=run.stop_reason.value if run.stop_reason else None,
                soft_deadline_at=run.budget.soft_deadline_at,
                hard_deadline_at=run.budget.hard_deadline_at,
                budget_snapshot=budget_to_dict(run.budget),
                usage_snapshot=usage_to_dict(run.usage),
                started_at=run.started_at,
                completed_at=run.completed_at,
                version=run.version,
            )
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise InvariantViolation(
                "Search run was modified concurrently",
                code="optimistic_lock_failed",
            )
        run.mark_persisted()


class SqlStepRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def get(self, tenant_id: UUID, step_id: UUID) -> SearchStep | None:
        self._assert_tenant(tenant_id)
        statement = select(SearchStepRecord).where(
            SearchStepRecord.tenant_id == tenant_id,
            SearchStepRecord.id == step_id,
        )
        record = await self._session.scalar(statement)
        return step_from_record(record) if record is not None else None

    async def get_for_update(
        self,
        tenant_id: UUID,
        step_id: UUID,
    ) -> SearchStep | None:
        self._assert_tenant(tenant_id)
        statement = (
            select(SearchStepRecord)
            .where(
                SearchStepRecord.tenant_id == tenant_id,
                SearchStepRecord.id == step_id,
            )
            .with_for_update()
        )
        record = await self._session.scalar(statement)
        return step_from_record(record) if record is not None else None

    async def add(self, step: SearchStep) -> None:
        self._assert_tenant(step.tenant_id)
        self._session.add(
            SearchStepRecord(
                id=step.id,
                tenant_id=step.tenant_id,
                run_id=step.run_id,
                step_key=step.step_key,
                step_type=step.step_type.value,
                plan_revision=step.plan_revision,
                status=step.status.value,
                input_ref=artifact_to_dict(step.input_ref),
                output_ref=(
                    artifact_to_dict(step.output_ref)
                    if step.output_ref is not None
                    else None
                ),
                retry_at=step.retry_at,
                version=step.version,
            )
        )

    async def save(self, step: SearchStep) -> None:
        self._assert_tenant(step.tenant_id)
        statement = (
            update(SearchStepRecord)
            .where(
                SearchStepRecord.tenant_id == step.tenant_id,
                SearchStepRecord.id == step.id,
                SearchStepRecord.version == step.persisted_version,
            )
            .values(
                status=step.status.value,
                output_ref=(
                    artifact_to_dict(step.output_ref)
                    if step.output_ref is not None
                    else None
                ),
                retry_at=step.retry_at,
                version=step.version,
            )
        )
        result = await self._session.execute(statement)
        if result.rowcount != 1:
            raise InvariantViolation(
                "Search step was modified concurrently",
                code="optimistic_lock_failed",
            )
        step.mark_persisted()


class SqlOutboxRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def add(self, message: OutboxMessage) -> None:
        self._assert_tenant(message.tenant_id)
        self._session.add(
            OutboxEvent(
                id=message.id,
                tenant_id=message.tenant_id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                event_type=message.event_type,
                payload=dict(message.payload),
                trace_context=trace_context_to_dict(message.trace_context),
                dedupe_key=message.dedupe_key,
                available_at=message.available_at,
                created_at=message.created_at,
                publish_attempts=0,
            )
        )

    async def claim_unpublished(
        self,
        now: datetime,
        limit: int,
    ) -> list[PendingOutboxMessage]:
        statement = (
            select(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == self._tenant_id,
                OutboxEvent.published_at.is_(None),
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        records = (await self._session.scalars(statement)).all()
        return [
            PendingOutboxMessage(
                OutboxMessage(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    aggregate_type=record.aggregate_type,
                    aggregate_id=record.aggregate_id,
                    event_type=record.event_type,
                    payload=dict(record.payload),
                    trace_context=trace_context_from_dict(record.trace_context),
                    dedupe_key=record.dedupe_key,
                    available_at=record.available_at,
                    created_at=record.created_at,
                ),
                publish_attempts=record.publish_attempts,
            )
            for record in records
        ]

    async def mark_published(self, message_id: UUID, published_at: datetime) -> None:
        await self._session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == self._tenant_id,
                OutboxEvent.id == message_id,
                OutboxEvent.published_at.is_(None),
            )
            .values(
                published_at=published_at,
                publish_attempts=OutboxEvent.publish_attempts + 1,
                last_error=None,
            )
        )

    async def mark_failed(self, message_id: UUID, error: str) -> None:
        await self._session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.tenant_id == self._tenant_id,
                OutboxEvent.id == message_id,
                OutboxEvent.published_at.is_(None),
            )
            .values(
                publish_attempts=OutboxEvent.publish_attempts + 1,
                last_error=error[:2000],
            )
        )


class SqlAttemptRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def add(self, attempt: StepAttempt) -> None:
        self._assert_tenant(attempt.tenant_id)
        self._session.add(
            StepAttemptRecord(
                id=attempt.id,
                tenant_id=attempt.tenant_id,
                run_id=attempt.run_id,
                step_id=attempt.step_id,
                attempt_no=attempt.attempt_no,
                idempotency_key=attempt.idempotency_key,
                lease_owner=attempt.lease_owner,
                leased_until=attempt.leased_until,
                deadline_at=attempt.deadline_at,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
                error_type=attempt.error.category.value if attempt.error else None,
                error_code=attempt.error.code if attempt.error else None,
                error_details=attempt.error.to_dict() if attempt.error else None,
                input_ref=artifact_to_dict(attempt.input_ref),
                output_ref=(
                    artifact_to_dict(attempt.output_ref)
                    if attempt.output_ref is not None
                    else None
                ),
            )
        )

    async def next_attempt_no(self, tenant_id: UUID, step_id: UUID) -> int:
        self._assert_tenant(tenant_id)
        statement = select(
            func.coalesce(func.max(StepAttemptRecord.attempt_no), 0) + 1
        ).where(
            StepAttemptRecord.tenant_id == tenant_id,
            StepAttemptRecord.step_id == step_id,
        )
        return int(await self._session.scalar(statement))

    async def complete(self, attempt: StepAttempt) -> None:
        self._assert_tenant(attempt.tenant_id)
        if not attempt.is_complete:
            raise InvariantViolation(
                "Cannot persist an incomplete attempt as completed",
                code="attempt_not_complete",
            )
        result = await self._session.execute(
            update(StepAttemptRecord)
            .where(
                StepAttemptRecord.tenant_id == attempt.tenant_id,
                StepAttemptRecord.id == attempt.id,
                StepAttemptRecord.completed_at.is_(None),
            )
            .values(
                leased_until=attempt.leased_until,
                completed_at=attempt.completed_at,
                error_type=(
                    attempt.error.category.value if attempt.error else None
                ),
                error_code=attempt.error.code if attempt.error else None,
                error_details=(
                    attempt.error.to_dict() if attempt.error else None
                ),
                output_ref=(
                    artifact_to_dict(attempt.output_ref)
                    if attempt.output_ref is not None
                    else None
                ),
            )
        )
        if result.rowcount != 1:
            raise InvariantViolation(
                "Attempt was already finalized or does not exist",
                code="attempt_finalize_conflict",
            )

    async def renew(self, attempt: StepAttempt) -> None:
        self._assert_tenant(attempt.tenant_id)
        if attempt.is_complete:
            raise InvariantViolation(
                "Cannot renew a completed attempt",
                code="attempt_already_complete",
            )
        result = await self._session.execute(
            update(StepAttemptRecord)
            .where(
                StepAttemptRecord.tenant_id == attempt.tenant_id,
                StepAttemptRecord.id == attempt.id,
                StepAttemptRecord.lease_owner == attempt.lease_owner,
                StepAttemptRecord.completed_at.is_(None),
                StepAttemptRecord.leased_until < attempt.leased_until,
            )
            .values(leased_until=attempt.leased_until)
        )
        if result.rowcount != 1:
            raise InvariantViolation(
                "Attempt lease could not be renewed",
                code="attempt_lease_renewal_conflict",
            )


class SqlRunEventRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def add(self, event: RunEventData) -> None:
        self._assert_tenant(event.tenant_id)
        self._session.add(
            RunEvent(
                id=event.id,
                tenant_id=event.tenant_id,
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=dict(event.payload),
                created_at=event.created_at,
            )
        )

    async def next_sequence(self, tenant_id: UUID, run_id: UUID) -> int:
        self._assert_tenant(tenant_id)
        statement = select(func.coalesce(func.max(RunEvent.sequence), 0) + 1).where(
            RunEvent.tenant_id == tenant_id,
            RunEvent.run_id == run_id,
        )
        return int(await self._session.scalar(statement))
