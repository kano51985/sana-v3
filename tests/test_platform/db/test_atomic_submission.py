from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.app.api.services import DatabaseConversationCatalogService
from sana.modules.conversation.domain import ConversationService, SubmitMessageCommand
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import RandomIdFactory, TraceContext
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_atomic_submission_flushes_foreign_key_parents_in_order() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    tenant_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    policy = SearchPolicy.default()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Submission Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"submission-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, display_name, status) "
                    "VALUES (:id, :tenant, :email, 'Submission User', 'ACTIVE')"
                ),
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "email": f"{user_id}@example.test",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, tenant_id, user_id, title, status, created_at, updated_at) "
                    "VALUES (:id, :tenant, :user, 'Submission', 'ACTIVE', :now, :now)"
                ),
                {
                    "id": conversation_id,
                    "tenant": tenant_id,
                    "user": user_id,
                    "now": NOW,
                },
            )

        service = ConversationService(
            TenantUnitOfWorkFactory(sessions),
            RandomIdFactory(),
            FrozenClock(NOW),
            policy,
        )
        receipt = await service.submit_message(
            SubmitMessageCommand(
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                content="Persist every parent before its children",
                idempotency_key=f"submission-{uuid4()}",
                routing=RoutingDecision(
                    SearchMode.FAST,
                    ("single_fact",),
                    policy.version,
                    0.95,
                ),
                trace_context=TraceContext.create(),
            )
        )

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM messages) AS messages, "
                        "(SELECT count(*) FROM response_runs) AS response_runs, "
                        "(SELECT count(*) FROM search_runs) AS search_runs, "
                        "(SELECT count(*) FROM search_steps) AS search_steps, "
                        "(SELECT count(*) FROM run_events) AS run_events, "
                        "(SELECT count(*) FROM outbox_events) AS outbox_events"
                    )
                )
            ).one()
        assert counts == (1, 1, 1, 1, 1, 1)
        assert receipt.status == "QUEUED"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_conversation_and_submission_idempotency_are_concurrency_safe() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    tenant_id, user_id = uuid4(), uuid4()
    principal = Principal(tenant_id, user_id, "integration", str(user_id))
    policy = SearchPolicy.default()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Idempotency Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"idempotency-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, display_name, status) "
                    "VALUES (:id, :tenant, :email, 'Idempotency User', 'ACTIVE')"
                ),
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "email": f"{user_id}@example.test",
                },
            )

        uow_factory = TenantUnitOfWorkFactory(sessions)
        catalog = DatabaseConversationCatalogService(
            uow_factory,
            FrozenClock(NOW),
            RandomIdFactory(),
        )
        conversations = await asyncio.gather(
            catalog.create(principal, " Shadow case ", "conversation-key"),
            catalog.create(principal, "Shadow case", "conversation-key"),
        )
        assert conversations[0].id == conversations[1].id
        assert conversations[0].title == "Shadow case"
        with pytest.raises(InvariantViolation) as conversation_error:
            await catalog.create(
                principal,
                "Different title",
                "conversation-key",
            )
        assert conversation_error.value.code == "idempotency_conflict"

        service = ConversationService(
            uow_factory,
            RandomIdFactory(),
            FrozenClock(NOW),
            policy,
        )
        command = SubmitMessageCommand(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversations[0].id,
            content=" Persist one workflow ",
            idempotency_key="message-key",
            routing=RoutingDecision(
                SearchMode.FAST,
                ("single_fact",),
                policy.version,
                0.95,
            ),
            trace_context=TraceContext.create(),
        )
        receipts = await asyncio.gather(
            service.submit_message(command),
            service.submit_message(command),
        )
        assert receipts[0].search_run_id == receipts[1].search_run_id
        assert sorted(item.duplicate for item in receipts) == [False, True]
        with pytest.raises(InvariantViolation) as message_error:
            await service.submit_message(
                SubmitMessageCommand(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=conversations[0].id,
                    content="Different content",
                    idempotency_key="message-key",
                    routing=command.routing,
                    trace_context=TraceContext.create(),
                )
            )
        assert message_error.value.code == "idempotency_conflict"

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM conversations) AS conversations, "
                        "(SELECT count(*) FROM messages) AS messages, "
                        "(SELECT count(*) FROM response_runs) AS response_runs, "
                        "(SELECT count(*) FROM search_runs) AS search_runs, "
                        "(SELECT count(*) FROM search_steps) AS search_steps, "
                        "(SELECT count(*) FROM run_events) AS run_events, "
                        "(SELECT count(*) FROM outbox_events) AS outbox_events"
                    )
                )
            ).one()
        assert counts == (1, 1, 1, 1, 1, 1, 1)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
