from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import fakeredis.aioredis
import pytest

from sana.platform.events.redis_stream import RedisEventStream, StreamEvent


@pytest.mark.asyncio
async def test_sse_resumes_after_last_postgres_sequence(api_context) -> None:
    response = await api_context.client.get(
        f"/api/v1/runs/{api_context.run_id}/events",
        headers={**api_context.auth, "Last-Event-ID": "6"},
    )

    assert response.status_code == 200
    assert api_context.events.after_sequences == [6]
    assert "id: 7\n" in response.text
    assert "id: 6\n" not in response.text
    assert "event: RUN_COMPLETED\n" in response.text


@pytest.mark.asyncio
async def test_sse_rejects_malformed_resume_cursor(api_context) -> None:
    response = await api_context.client.get(
        f"/api/v1/runs/{api_context.run_id}/events",
        headers={**api_context.auth, "Last-Event-ID": "not-an-integer"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_redis_stream_is_replayable_but_safe_to_clear() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    stream = RedisEventStream(redis)
    tenant_id, run_id = uuid4(), uuid4()
    event = StreamEvent(
        sequence=3,
        event_type="STEP_COMPLETED",
        payload={"step": "plan"},
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    try:
        await stream.publish(tenant_id, run_id, event)
        replayed = await stream.read_after(
            tenant_id,
            run_id,
            2,
            block_ms=1,
        )
        assert replayed == [event]

        await redis.flushdb()
        assert await stream.read_after(
            tenant_id,
            run_id,
            3,
            block_ms=1,
        ) == []
    finally:
        await redis.aclose()
