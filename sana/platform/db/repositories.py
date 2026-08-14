"""Tenant-scoped SQLAlchemy repositories returning domain values and DTOs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sana.modules.conversation.domain import (
    MessageDraft,
    ResponseRunDraft,
    SubmissionReceipt,
)
from sana.modules.orchestration.domain import SearchRun
from sana.modules.orchestration.repository import (
    budget_to_dict,
    run_from_record,
    usage_to_dict,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.orchestration import SearchRunRecord


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
                status=run.status.value,
                answer_quality=run.answer_quality.value,
                stop_reason=run.stop_reason.value if run.stop_reason else None,
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
