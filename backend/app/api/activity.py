"""The activity feed: ``GET /api/activity`` and ``GET /api/activity/stream`` (Admin → Activity).

The operational log of the ingest/transcription pipeline — uploads accepted, duplicates
rejected, no-audio/encrypted calls, transcriptions queued/done/failed, errors.  Two shapes:

* ``GET /activity`` backfills a page of recent rows, newest first, keyset-paginated by id;
* ``GET /activity/stream`` is Server-Sent Events: it subscribes to the live bus and, for
  each ``activity`` notification, re-reads that row and emits it as a ``data:`` JSON frame,
  with a periodic heartbeat comment so a quiet feed still proves it is alive.

Both are gated behind :data:`~app.auth.AdminPrincipal`: the archive's reads are open, but
this is operational plumbing.  ``EventSource`` cannot set an ``Authorization`` header, so
the stream authenticates with the same-origin session cookie the rest of the SPA uses —
exactly as the live feed does — which the admin gate resolves like any other request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import activity
from app.auth import require_admin
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.live import bus

log = logging.getLogger("app.api.activity")
router = APIRouter(tags=["activity"], dependencies=[Depends(require_admin)])
DbSession = Annotated[Session, Depends(get_session)]

#: Cap on one page, so a client cannot ask for the whole table at once.
MAX_LIMIT = 500


@router.get(
    "/activity",
    summary="Recent pipeline activity",
    description=(
        "The operational log of ingest and transcription, newest first. Keyset-paginate by "
        "passing the smallest `id` you have as `before_id`. Filter by `source`, `level` and "
        "`since` (an ISO timestamp). Admin only."
    ),
)
def list_activity(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="rows per page")] = 100,
    before_id: Annotated[int | None, Query(description="only rows with a smaller id")] = None,
    source: Annotated[
        str | None, Query(description="ingest|upload|transcribe|export|rr|import|system|arbiter")
    ] = None,
    level: Annotated[str | None, Query(description="info|warn|error")] = None,
    since: Annotated[datetime | None, Query(description="only rows at or after this ISO time")] = None,
) -> list[dict[str, Any]]:
    rows = activity.recent(
        db, limit=limit, before_id=before_id, source=source, level=level, since=since
    )
    return [activity.to_dict(row) for row in rows]


def _render(event_id: int) -> dict[str, Any] | None:
    """Re-read one activity row for the stream, in a worker thread (sync session)."""
    session = SessionLocal()
    try:
        row = activity.get(session, event_id)
        return None if row is None else activity.to_dict(row)
    finally:
        session.close()


@router.get(
    "/activity/stream",
    summary="The activity feed as Server-Sent Events",
    description=(
        "Tails the activity log live. Each new event is re-read from the database and sent as "
        "an `event: activity` frame with a JSON `data:` line; a comment heartbeat is sent when "
        "the feed is quiet. Admin only; authenticates with the session cookie."
    ),
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_activity(request: Request) -> StreamingResponse:
    heartbeat = float(get_settings().live_status_seconds)

    async def events() -> AsyncIterator[bytes]:
        yield b": connected\n\n"
        subscriber = bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if event.get("event") != activity.BUS_EVENT:
                    continue
                event_id = event.get("id")
                if event_id is None:
                    continue
                frame = await run_in_threadpool(_render, int(event_id))
                if frame is None:
                    continue
                yield b"event: activity\ndata: " + orjson.dumps(frame) + b"\n\n"
        finally:
            bus.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


__all__ = ["router"]
