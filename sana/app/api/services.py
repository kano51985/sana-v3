"""Database-backed API application services and SSE resume logic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from uuid import UUID

from sqlalchemy import select
from redis.exceptions import RedisError

from sana.app.api.dependencies import EvidenceView, EventView, RunView
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import SearchRun
from sana.modules.orchestration.events import RunEventData
from sana.modules.shared.clock import Clock
from sana.modules.shared.ids import IdFactory
from sana.platform.db.models.orchestration import RunEvent
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
        status=run.status.value,
        answer_quality=run.answer_quality.value,
        stop_reason=run.stop_reason.value if run.stop_reason else None,
        soft_deadline_at=run.budget.soft_deadline_at,
        hard_deadline_at=run.budget.hard_deadline_at,
        created_at=run.budget.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
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
    ) -> list[EvidenceView] | None:
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
            return [
                EvidenceView(
                    fact_key=row[0],
                    verdict=row[1],
                    confidence=row[2],
                    quote=row[3],
                    source_url=row[4],
                )
                for row in rows
            ]


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
