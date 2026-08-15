"""Database-backed API application services and SSE resume logic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
import logging
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from redis.exceptions import RedisError

from sana.app.api.dependencies import (
    ConversationMessageView,
    ConversationView,
    EvidenceReportView,
    EvidenceView,
    EventView,
    MissingFactView,
    RunView,
)
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import SearchRun
from sana.modules.orchestration.events import RunEventData
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import IdFactory
from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.orchestration import RunEvent, SearchRunRecord
from sana.platform.db.models.search import (
    Document,
    DocumentVersion,
    EvidenceCandidate,
    FactRequirement,
    VerifiedEvidence,
)
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.events.redis_stream import RedisEventStream, StreamEvent


logger = logging.getLogger(__name__)


def _run_view(run: SearchRun) -> RunView:
    return RunView(
        id=run.id,
        conversation_id=run.conversation_id,
        message_id=run.message_id,
        mode=run.mode.value,
        routing_reason_codes=tuple(run.routing.reason_codes),
        route_confidence=run.routing.confidence,
        policy_version=run.routing.policy_version,
        status=run.status.value,
        answer_quality=run.answer_quality.value,
        stop_reason=run.stop_reason.value if run.stop_reason else None,
        soft_deadline_at=run.budget.soft_deadline_at,
        hard_deadline_at=run.budget.hard_deadline_at,
        created_at=run.budget.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


class DatabaseConversationCatalogService:
    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = id_factory

    async def create(
        self,
        principal: Principal,
        title: str,
        idempotency_key: str | None = None,
    ) -> ConversationView:
        now = self._clock.now()
        normalized_title = title.strip() or "新会话"
        normalized_key = idempotency_key.strip() if idempotency_key else None
        if normalized_key is not None and not 1 <= len(normalized_key) <= 200:
            raise ValueError(
                "Idempotency-Key must contain between 1 and 200 characters"
            )
        request_hash = (
            hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()
            if normalized_key is not None
            else None
        )
        conversation_id = self._ids.new_uuid()
        async with self._uow_factory(principal.tenant_id) as uow:
            statement = (
                insert(Conversation)
                .values(
                    id=conversation_id,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    title=normalized_title,
                    status="ACTIVE",
                    creation_idempotency_key=normalized_key,
                    creation_request_hash=request_hash,
                    created_at=now,
                    updated_at=now,
                )
                .returning(Conversation.id)
            )
            if normalized_key is not None:
                statement = statement.on_conflict_do_nothing(
                    constraint="uq_conversations_tenant_user_creation_key"
                )
            inserted_id = await uow.session.scalar(statement)
            if inserted_id is None:
                conversation = await uow.session.scalar(
                    select(Conversation).where(
                        Conversation.tenant_id == principal.tenant_id,
                        Conversation.user_id == principal.user_id,
                        Conversation.creation_idempotency_key == normalized_key,
                    )
                )
                if conversation is None:
                    raise InvariantViolation(
                        "Conversation idempotency lookup did not converge",
                        code="idempotency_state_corrupt",
                    )
                if conversation.creation_request_hash != request_hash:
                    raise InvariantViolation(
                        "Idempotency-Key was already used for a different title",
                        code="idempotency_conflict",
                    )
                view = self._view(conversation)
            else:
                view = ConversationView(
                    id=conversation_id,
                    title=normalized_title,
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            await uow.commit()
        return view

    async def list(self, principal: Principal) -> list[ConversationView]:
        async with self._uow_factory(principal.tenant_id) as uow:
            statement = (
                select(Conversation)
                .where(
                    Conversation.tenant_id == principal.tenant_id,
                    Conversation.user_id == principal.user_id,
                    Conversation.status == "ACTIVE",
                )
                .order_by(Conversation.updated_at.desc(), Conversation.id)
                .limit(100)
            )
            records = (await uow.session.scalars(statement)).all()
            return [self._view(item) for item in records]

    async def messages(
        self,
        principal: Principal,
        conversation_id: UUID,
    ) -> list[ConversationMessageView] | None:
        async with self._uow_factory(principal.tenant_id) as uow:
            owned = await uow.session.scalar(
                select(Conversation.id).where(
                    Conversation.tenant_id == principal.tenant_id,
                    Conversation.id == conversation_id,
                    Conversation.user_id == principal.user_id,
                )
            )
            if owned is None:
                return None
            statement = (
                select(
                    Message,
                    SearchRunRecord.id,
                    SearchRunRecord.status,
                    SearchRunRecord.answer_quality,
                )
                .outerjoin(
                    ResponseRun,
                    or_(
                        ResponseRun.message_id == Message.id,
                        ResponseRun.output_message_id == Message.id,
                    ),
                )
                .outerjoin(
                    SearchRunRecord,
                    SearchRunRecord.response_run_id == ResponseRun.id,
                )
                .where(
                    Message.tenant_id == principal.tenant_id,
                    Message.conversation_id == conversation_id,
                )
                .order_by(Message.created_at, Message.id)
                .limit(1_000)
            )
            rows = (await uow.session.execute(statement)).all()
            return [
                ConversationMessageView(
                    id=row[0].id,
                    role=row[0].role,
                    content=row[0].content,
                    created_at=row[0].created_at,
                    run_id=row[1],
                    run_status=row[2],
                    answer_quality=row[3],
                )
                for row in rows
            ]

    @staticmethod
    def _view(record: Conversation) -> ConversationView:
        return ConversationView(
            id=record.id,
            title=record.title,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class DatabaseRunApplicationService:
    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        clock: Clock,
        id_factory: IdFactory,
        redis_stream: RedisEventStream,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = id_factory
        self._redis = redis_stream

    async def get(self, principal: Principal, run_id: UUID) -> RunView | None:
        async with self._uow_factory(principal.tenant_id) as uow:
            run = await uow.runs.get(principal.tenant_id, run_id)
            return _run_view(run) if run is not None else None

    async def cancel(self, principal: Principal, run_id: UUID) -> RunView | None:
        cancelled_event: RunEventData | None = None
        async with self._uow_factory(principal.tenant_id) as uow:
            run = await uow.runs.get_for_update(principal.tenant_id, run_id)
            if run is None:
                return None
            if not run.is_terminal:
                cancelled_at = self._clock.now()
                run.cancel(cancelled_at)
                await uow.runs.save(run)
                cancelled_event = RunEventData(
                    id=self._ids.new_uuid(),
                    tenant_id=principal.tenant_id,
                    run_id=run_id,
                    sequence=await uow.events.next_sequence(
                        principal.tenant_id,
                        run_id,
                    ),
                    event_type="RUN_CANCELLED",
                    payload={"stop_reason": "USER_CANCELLED"},
                    created_at=cancelled_at,
                )
                await uow.events.add(cancelled_event)
                await uow.commit()
            view = _run_view(run)
        if cancelled_event is not None:
            try:
                await self._redis.publish(
                    principal.tenant_id,
                    run_id,
                    StreamEvent(
                        sequence=cancelled_event.sequence,
                        event_type=cancelled_event.event_type,
                        payload=dict(cancelled_event.payload),
                        created_at=cancelled_event.created_at,
                    ),
                )
            except RedisError:
                logger.warning(
                    "Redis event acceleration unavailable after run cancellation",
                    extra={"run_id": str(run_id)},
                )
        return view

    async def evidence(
        self,
        principal: Principal,
        run_id: UUID,
    ) -> EvidenceReportView | None:
        async with self._uow_factory(principal.tenant_id) as uow:
            run = await uow.runs.get(principal.tenant_id, run_id)
            if run is None:
                return None
            statement = (
                select(
                    FactRequirement.fact_key,
                    VerifiedEvidence.verdict,
                    VerifiedEvidence.confidence,
                    EvidenceCandidate.quote,
                    Document.canonical_url,
                )
                .join(
                    EvidenceCandidate,
                    EvidenceCandidate.id == VerifiedEvidence.candidate_id,
                )
                .join(
                    FactRequirement,
                    FactRequirement.id == EvidenceCandidate.fact_requirement_id,
                )
                .join(
                    DocumentVersion,
                    DocumentVersion.id == EvidenceCandidate.document_version_id,
                )
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    VerifiedEvidence.tenant_id == principal.tenant_id,
                    VerifiedEvidence.run_id == run_id,
                )
                .order_by(FactRequirement.fact_key, VerifiedEvidence.confidence.desc())
            )
            rows = (await uow.session.execute(statement)).all()
            evidence = tuple(
                EvidenceView(
                    fact_key=row[0],
                    verdict=row[1],
                    confidence=row[2],
                    quote=row[3],
                    source_url=row[4],
                )
                for row in rows
            )
            missing_rows = (
                await uow.session.execute(
                    select(
                        FactRequirement.fact_key,
                        FactRequirement.description,
                        FactRequirement.status,
                    )
                    .where(
                        FactRequirement.tenant_id == principal.tenant_id,
                        FactRequirement.run_id == run_id,
                        FactRequirement.required.is_(True),
                        FactRequirement.status.not_in(("COVERED", "VERIFIED")),
                    )
                    .order_by(FactRequirement.fact_key)
                )
            ).all()
            return EvidenceReportView(
                evidence=evidence,
                missing_facts=tuple(
                    MissingFactView(row[0], row[1], row[2]) for row in missing_rows
                ),
            )


class DatabaseRunEventService:
    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        redis_stream: RedisEventStream,
        *,
        block_ms: int = 15_000,
    ) -> None:
        self._uow_factory = uow_factory
        self._redis = redis_stream
        self._block_ms = block_ms

    async def _database_events(
        self,
        principal: Principal,
        run_id: UUID,
        after_sequence: int,
    ) -> tuple[list[EventView], bool]:
        async with self._uow_factory(principal.tenant_id) as uow:
            run = await uow.runs.get(principal.tenant_id, run_id)
            if run is None:
                return [], True
            statement = (
                select(RunEvent)
                .where(
                    RunEvent.tenant_id == principal.tenant_id,
                    RunEvent.run_id == run_id,
                    RunEvent.sequence > after_sequence,
                )
                .order_by(RunEvent.sequence)
                .limit(200)
            )
            records = (await uow.session.scalars(statement)).all()
            return (
                [
                    EventView(
                        sequence=record.sequence,
                        event_type=record.event_type,
                        payload=dict(record.payload),
                        created_at=record.created_at,
                    )
                    for record in records
                ],
                run.is_terminal,
            )

    async def subscribe(
        self,
        principal: Principal,
        run_id: UUID,
        after_sequence: int,
    ) -> AsyncIterator[EventView]:
        cursor = after_sequence
        while True:
            database_events, terminal = await self._database_events(
                principal,
                run_id,
                cursor,
            )
            for event in database_events:
                cursor = max(cursor, event.sequence)
                yield event
            if terminal:
                return
            try:
                cached = await self._redis.read_after(
                    principal.tenant_id,
                    run_id,
                    cursor,
                    block_ms=self._block_ms,
                )
            except RedisError:
                logger.warning(
                    "Redis SSE acceleration unavailable; polling PostgreSQL",
                    extra={"run_id": str(run_id)},
                )
                await asyncio.sleep(0.5)
                continue
            for event in cached:
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                yield EventView(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=event.payload,
                    created_at=event.created_at,
                )
