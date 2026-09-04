"""The AI activity feed (design §20).

Every stage of the agent writes one line here. It doubles as the demo's
narrative surface and as a human-readable trace for debugging a decision.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ActivityEvent

log = logging.getLogger(__name__)

_MAX_SUBSCRIBER_BACKLOG = 200
_subscribers: set[asyncio.Queue] = set()
_recent: deque[dict] = deque(maxlen=100)


def _serialise(event: ActivityEvent) -> dict:
    # Stamp UTC: a naive ISO string is read as local time by the browser.
    ts = event.created_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "id": event.id,
        "ts": (ts.isoformat() if ts else None),
        "transaction_id": event.transaction_id,
        "stage": event.stage,
        "level": event.level,
        "message": event.message,
        "detail": event.detail,
    }


def emit(
    db: Session,
    *,
    stage: str,
    message: str,
    transaction_id: str | None = None,
    level: str = "info",
    detail: dict[str, Any] | None = None,
    flush: bool = True,
) -> ActivityEvent:
    event = ActivityEvent(
        transaction_id=transaction_id, stage=stage, level=level, message=message, detail=detail
    )
    db.add(event)
    if flush:
        db.flush()  # assign an id so the streamed payload matches what is stored
    payload = _serialise(event)
    _recent.append(payload)
    _publish(payload)
    return event


def _publish(payload: dict) -> None:
    """Fan out to SSE subscribers, dropping messages for stalled clients."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.debug("Dropping activity event for a slow subscriber")


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_SUBSCRIBER_BACKLOG)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def recent(db: Session, limit: int = 60, transaction_id: str | None = None) -> list[dict]:
    stmt = select(ActivityEvent).order_by(ActivityEvent.id.desc()).limit(limit)
    if transaction_id:
        stmt = (
            select(ActivityEvent)
            .where(ActivityEvent.transaction_id == transaction_id)
            .order_by(ActivityEvent.id.desc())
            .limit(limit)
        )
    return [_serialise(e) for e in db.execute(stmt).scalars().all()]
