"""Redis Streams cache keyed by authoritative PostgreSQL event sequence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import ResponseError


@dataclass(frozen=True, slots=True)
class StreamEvent:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class RedisEventStream:
    def __init__(self, redis: Redis, *, max_length: int = 2_000) -> None:
        self._redis = redis
        self._max_length = max_length

    @staticmethod
    def key(tenant_id: UUID, run_id: UUID) -> str:
        return f"sana:run-events:{tenant_id}:{run_id}"

    async def publish(
        self,
        tenant_id: UUID,
        run_id: UUID,
        event: StreamEvent,
    ) -> None:
        stream_key = self.key(tenant_id, run_id)
        stream_id = f"{event.sequence}-0"
        fields = {
            "event_type": event.event_type,
            "payload": json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
            "created_at": event.created_at.isoformat(),
        }
        try:
            await self._redis.xadd(
                stream_key,
                fields,
                id=stream_id,
                maxlen=self._max_length,
                approximate=True,
            )
        except ResponseError as exc:
            existing = await self._redis.xrange(stream_key, min=stream_id, max=stream_id)
            if existing:
                return
            newest = await self._redis.xrevrange(stream_key, count=1)
            if newest and int(_decode(newest[0][0]).split("-", 1)[0]) >= event.sequence:
                return
            raise exc

    async def read_after(
        self,
        tenant_id: UUID,
        run_id: UUID,
        after_sequence: int,
        *,
        block_ms: int,
        count: int = 100,
    ) -> list[StreamEvent]:
        response = await self._redis.xread(
            {self.key(tenant_id, run_id): f"{after_sequence}-0"},
            count=count,
            block=block_ms,
        )
        events: list[StreamEvent] = []
        for _, entries in response:
            for stream_id, fields in entries:
                decoded_id = _decode(stream_id)
                decoded = {_decode(key): _decode(value) for key, value in fields.items()}
                events.append(
                    StreamEvent(
                        sequence=int(decoded_id.split("-", 1)[0]),
                        event_type=decoded["event_type"],
                        payload=json.loads(decoded["payload"]),
                        created_at=datetime.fromisoformat(decoded["created_at"]),
                    )
                )
        return events


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
