from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from sana.app.api.dependencies import get_container, require_principal
from sana.modules.identity.domain import Principal


router = APIRouter(prefix="/api/v1/runs", tags=["events"])


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    if await get_container(request).run_service.get(principal, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    try:
        after_sequence = int(last_event_id or 0)
        if after_sequence < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID must be a non-negative event sequence",
        ) from exc

    async def event_source():
        async for event in get_container(request).event_service.subscribe(
            principal,
            run_id,
            after_sequence,
        ):
            payload = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
            yield (
                f"id: {event.sequence}\n"
                f"event: {event.event_type}\n"
                f"data: {payload}\n\n"
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
