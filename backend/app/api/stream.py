"""Server-sent events for the live AI activity feed (design §20)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..services import activity

router = APIRouter(tags=["stream"])

_HEARTBEAT_SECONDS = 15.0


@router.get("/activity/stream")
async def stream_activity(request: Request):
    queue = activity.subscribe()

    async def events():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"  # keeps proxies from closing the stream
                    continue
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        finally:
            activity.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
