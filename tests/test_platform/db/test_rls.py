from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.platform.db.session import create_database_engine


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_pool_connection_cannot_retain_or_cross_tenant_context() -> None:
    engine = create_database_engine(DATABASE_URL)
    tenant_a, tenant_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, status) "
                        "VALUES (:a, :a_slug, 'A', 'ACTIVE'), (:b, :b_slug, 'B', 'ACTIVE')"
                    ),
                    {
                        "a": tenant_a,
                        "b": tenant_b,
                        "a_slug": f"test-{tenant_a}",
                        "b_slug": f"test-{tenant_b}",
                    },
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant_a)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, display_name, status) "
                        "VALUES (:id, :tenant, 'a@example.test', 'A', 'ACTIVE')"
                    ),
                    {"id": user_a, "tenant": tenant_a},
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant_b)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, display_name, status) "
                        "VALUES (:id, :tenant, 'b@example.test', 'B', 'ACTIVE')"
                    ),
                    {"id": user_b, "tenant": tenant_b},
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant_a)},
                )
                visible = await connection.scalar(text("SELECT count(*) FROM users"))
                assert visible == 1
            finally:
                await transaction.rollback()

            transaction = await connection.begin()
            try:
                context = await connection.scalar(
                    text("SELECT current_setting('app.tenant_id', true)")
                )
                assert context in (None, "")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
