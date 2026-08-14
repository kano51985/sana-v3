from __future__ import annotations

from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.modules.conversation.domain import ConversationService, SubmitMessageCommand
from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import FrozenClock
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
