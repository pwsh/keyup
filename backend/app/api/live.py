"""The live feed: ``WS /api/live`` and ``GET /api/live/sse`` (§4.4).

Both carry the same event types:

``call.new``          a recording has been ingested
``call.transcribed``  a transcript landed on a call
``call.updated``      somebody edited a call (notes, tags, star, correction)
``call.superseded``   reconciliation demoted a call that was primary; the row a live list
                      is showing must go (2026-09-10)
``ingest.status``     a heartbeat every ``LIVE_STATUS_SECONDS`` of wall clock, so a quiet
                      (or narrowly filtered) feed still proves it is alive and the UI can
                      show "no traffic for 4 minutes"
``gap``               events were lost on the way to this client — the server's LISTEN
                      connection dropped and came back, or the client fell so far behind
                      that its queue overflowed.  ``{"type": "gap", "reason": …}``; the
                      client should refetch the window it is showing

A client sends its filter as the first WebSocket frame — the same object ``GET /calls``
takes — and may send a new one at any time to re-filter without reconnecting.  The SSE
variant takes the filter as query parameters instead, because an ``EventSource`` cannot
send anything.

Events from the bus carry identifiers only, so each one is re-read from the database and
run through the subscriber's filter before it is sent.  That work happens in a worker
thread: a synchronous SQLAlchemy session must never block the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.filters import CallFilters, Filters, filters_from_mapping
from app.auth import allowed_hosts, authenticate, host_of
from app.config import get_settings
from app.db import ReleasedSession, SessionLocal, get_session
from app.ingest.service import ingest_status
from app.live import GAP_EVENT, Subscriber, bus
from app.services import calls as call_service

log = logging.getLogger("app.api.live")
router = APIRouter(tags=["live"])
DbSession = Annotated[Session, Depends(get_session)]

#: Events whose payload names a call and which therefore get a `call` object attached.
CALL_EVENTS = {"call.new", "call.transcribed", "call.updated"}

#: The one call event that carries *no* call object.  A superseded row has just been marked
#: ``duplicate_of`` the winner, so it no longer satisfies any subscriber's filter (the call
#: filters exclude duplicates) and re-reading it would render nothing — which is exactly why
#: the demoted copy would otherwise sit on the live list for ever.  The frame is the two ids.
SUPERSEDED_EVENT = "call.superseded"


#: How long a rendered frame is reused for other subscribers watching the same thing, and
#: how many are kept.  One ``call.new`` used to cost one query *per connected client*
#: (L10); the archive's whole point is that everybody watches the same feed, usually with
#: the same filter, so the query is done once and the answer shared.
RENDER_TTL_S = 3.0
RENDER_CACHE_MAX = 512

#: (event kind, call id, the bus's per-event ``seq``, filter).  The ``seq`` is what keeps
#: two edits to one call inside :data:`RENDER_TTL_S` apart: keyed on the call alone, the
#: second ``call.updated`` was answered with the first one's frame (L5).
RenderKey = tuple[str, int, int | None, str]

_render_lock = threading.Lock()
_render_cache: OrderedDict[RenderKey, tuple[float, dict[str, Any] | None]] = OrderedDict()


def _guest_filters(f: CallFilters, *, is_guest: bool) -> CallFilters:
    return f.replace(guest_view=True) if is_guest else f


def _socket_is_guest(websocket: WebSocket) -> bool:
    """No credential, or one that does not resolve: the socket gets the guest's view."""
    session = SessionLocal()
    try:
        return authenticate(websocket, session) is None
    except HTTPException:
        return True
    finally:
        session.close()


def _filter_key(f: CallFilters) -> str:
    return json.dumps(f.echo(), sort_keys=True, default=str)


def _cached_frame(key: RenderKey) -> tuple[bool, dict[str, Any] | None]:
    now = time.monotonic()
    with _render_lock:
        entry = _render_cache.get(key)
        if entry is None:
            return False, None
        stamped, frame = entry
        if now - stamped >= RENDER_TTL_S:
            del _render_cache[key]
            return False, None
        _render_cache.move_to_end(key)
        return True, frame


def _store_frame(key: RenderKey, frame: dict[str, Any] | None) -> None:
    with _render_lock:
        _render_cache[key] = (time.monotonic(), frame)
        _render_cache.move_to_end(key)
        while len(_render_cache) > RENDER_CACHE_MAX:
            _render_cache.popitem(last=False)


def _render(event: dict[str, Any], f: CallFilters) -> dict[str, Any] | None:
    """Turn a bus event into a client frame, or ``None`` when this client filtered it out.

    Memoized for :data:`RENDER_TTL_S` on (event kind, call id, event ``seq``, filter): the
    frame one event produces is the same for every subscriber whose filter is the same, and
    fanning one event out to fifty open tabs must not be fifty identical queries.
    """
    kind = event.get("event")
    if kind == "ingest.status":
        return {"type": "ingest.status", "status": event.get("status", {})}
    if kind == GAP_EVENT:
        return _gap_frame(str(event.get("reason") or "reconnected"))
    if kind == SUPERSEDED_EVENT:
        loser = event.get("id")
        winner = event.get("by")
        if loser is None:
            return None
        # Unfiltered on purpose: a client that never held the row drops nothing, and a
        # client whose filter no longer matches the demoted copy is precisely the one that
        # has to be told about it.
        return {"type": SUPERSEDED_EVENT, "id": int(loser), "by": None if winner is None else int(winner)}
    if kind not in CALL_EVENTS:
        return None
    call_id = event.get("id") or event.get("call_id")
    if call_id is None:
        return None
    seq = event.get("seq")
    key: RenderKey = (str(kind), int(call_id), None if seq is None else int(seq), _filter_key(f))
    hit, cached = _cached_frame(key)
    if hit:
        return cached
    session = SessionLocal()
    try:
        call = call_service.call_if_matches(session, f, int(call_id))
    finally:
        session.close()
    frame = None if call is None else {"type": kind, "call": call.model_dump(mode="json")}
    _store_frame(key, frame)
    return frame


def _gap_frame(reason: str) -> dict[str, Any]:
    return {"type": "gap", "reason": reason}


def _safe_render(event: dict[str, Any], f: CallFilters) -> dict[str, Any] | None:
    """:func:`_render`, but an event that cannot be rendered is skipped, not fatal.

    A database blip while re-reading one call used to escape the socket loop and close
    every subscriber's connection at once (L4), and each client's reconnect then came back
    to the same failure.  One event is not worth a connection: log it and move on.  The
    row reaches the client with the next event about it or the page's next refetch; no
    ``gap`` is sent for it, because a render that fails on every event (the database is
    down) would turn into a refetch storm against the same database.
    """
    try:
        return _render(event, f)
    except Exception as exc:  # noqa: BLE001 - one bad event must not end the stream
        log.warning(
            "live: could not render %s for call %s, skipped (%s: %s)",
            event.get("event"),
            event.get("id") or event.get("call_id"),
            type(exc).__name__,
            exc,
        )
        return None


#: The last heartbeat's status frame and when it was built (``time.monotonic``).  Every
#: subscriber's heartbeat used to run ``ingest_status`` on its own; one computation per
#: heartbeat interval is shared by all of them instead.
_status_lock = threading.Lock()
_status_cache: tuple[float, dict[str, Any]] | None = None


def _status_frame(max_age_s: float = 0.0) -> dict[str, Any]:
    """The ``ingest.status`` heartbeat frame, reused while younger than ``max_age_s``.

    The lock is held across the computation on purpose: fifty subscribers whose timers
    fire together wait for the one query rather than each starting their own.
    """
    global _status_cache  # noqa: PLW0603 - a process-wide memo, like the render cache
    with _status_lock:
        cached = _status_cache
        started = time.monotonic()
        if cached is not None and max_age_s > 0 and started - cached[0] < max_age_s:
            return cached[1]
        session = SessionLocal()
        try:
            status = ingest_status(session)
        except Exception as exc:  # noqa: BLE001 - a heartbeat must never kill the socket
            log.warning("live: status heartbeat failed: %s", exc)
            status = {"error": str(exc)}
        finally:
            session.close()
        frame = {"type": "ingest.status", "status": json.loads(json.dumps(status, default=str))}
        _status_cache = (started, frame)
        return frame


class _Heartbeat:
    """A wall-clock heartbeat: due every ``interval`` seconds whatever else arrives.

    The old loop waited ``interval`` for the *next bus event* and sent a heartbeat only on
    a timeout, so every event restarted the clock — including the ones this subscriber's
    filter threw away.  A client narrowed to one quiet talkgroup on a busy system never
    heard a heartbeat at all, and could not tell a live feed from a dead one.
    """

    def __init__(self, interval: float) -> None:
        self.interval = max(0.5, float(interval))
        self.due = time.monotonic() + self.interval

    def remaining(self) -> float:
        return max(0.0, self.due - time.monotonic())

    def fire(self) -> bool:
        """True (and the next beat scheduled) when a heartbeat is owed now."""
        now = time.monotonic()
        if now < self.due:
            return False
        # From now, not from ``due``: a loop that was held up does not owe a burst.
        self.due = now + self.interval
        return True


async def _next_event(subscriber: Subscriber, beat: _Heartbeat) -> dict[str, Any] | None:
    """The next bus event, or ``None`` when the heartbeat falls due first."""
    try:
        return await asyncio.wait_for(subscriber.queue.get(), timeout=beat.remaining())
    except TimeoutError:
        return None


def _status_max_age(beat: _Heartbeat) -> float:
    """How old a shared status frame may be for this heartbeat: under one interval, so a
    subscriber whose beat falls just after the last computation still gets a fresh one on
    its next beat rather than every other one."""
    return beat.interval * 0.8


def _take_overflow(subscriber: Subscriber) -> bool:
    """Whether this subscriber has lost events to a full queue since the last look."""
    if subscriber.overflowed:
        subscriber.overflowed = False
        return True
    return False


async def _events(f: CallFilters, heartbeat_s: float) -> AsyncIterator[dict[str, Any]]:
    """Yield client frames for one subscriber until it is canceled."""
    subscriber = bus.subscribe()
    beat = _Heartbeat(heartbeat_s)
    try:
        while True:
            event = await _next_event(subscriber, beat)
            if _take_overflow(subscriber):
                yield _gap_frame("overflow")
            if event is not None:
                frame = await run_in_threadpool(_safe_render, event, f)
                if frame is not None:
                    yield frame
            if beat.fire():
                yield await run_in_threadpool(_status_frame, _status_max_age(beat))
    finally:
        bus.unsubscribe(subscriber)


@router.websocket("/live")
async def live_socket(websocket: WebSocket, _released: ReleasedSession = None) -> None:
    """Rolling feed of calls as they are recorded and transcribed.

    Protocol: connect, then send a JSON filter object (``{"talkgroup": [1101], "q": "fire"}``
    — every ``GET /calls`` parameter works, arrays as arrays).  Send ``{}`` for everything.
    A further frame replaces the filter.  The server sends
    ``{"type": "call.new", "call": {…}}`` and friends, plus an ``ingest.status`` heartbeat
    and ``{"type": "call.superseded", "id": …, "by": …}`` when reconciliation demotes a row
    the list is showing (no call object: the demoted copy no longer passes any filter).
    ``filter.ok`` carries ``heartbeat_s`` so a client can tell a dead connection from a
    quiet one, and ``{"type": "gap", "reason": …}`` means events were lost on the way
    (``reconnected``: the server's LISTEN dropped; ``overflow``: this client fell behind) and
    the client should refetch its window.  A bad filter frame is answered with an ``error``
    frame and leaves the previous filter in place; an event that fails to render is skipped.

    A browser does not apply the same-origin policy to a WebSocket and sends the session
    cookie with it regardless, so the handshake's ``Origin`` is checked against the same
    host list a state-changing request is held to before the socket is accepted (M1).  A
    request with no ``Origin`` is not from a browser — the CLI, a script — and is allowed.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and host_of(origin) not in allowed_hosts(websocket):
        log.warning("live: refusing a websocket from %r", origin)
        await websocket.close(code=1008, reason="cross-origin websocket rejected")
        return
    await websocket.accept()
    settings = get_settings()
    # No filter until the client has sent one.  It used to start as "everything", and a
    # call recorded in the gap between the accept and the first frame went out unfiltered
    # — the calls page reconnects on every filter change, so a reader narrowing to one
    # talkgroup saw other talkgroups' calls arrive for a moment after each change.
    state: dict[str, CallFilters | None] = {"filter": None}
    is_guest = await run_in_threadpool(_socket_is_guest, websocket)

    async def receive_filters() -> None:
        """Read filter frames until the client stops sending them.

        A client going away is an ordinary end to this task, not an error: a browser tab
        closing sends 1001 and a reload closes mid-frame, and either way there is nothing
        left to answer.  It has to be caught *here* — an exception left on this task is
        re-raised by the ``await receiver`` in the route's ``finally`` below, where it
        escapes the route as an unhandled ASGI exception rather than a disconnect.

        The socket writes sit outside the inner ``try`` on purpose, so that a disconnect
        during a send is never mistaken for a bad filter and answered with another send.
        """
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw) if raw.strip() else {}
                    if not isinstance(payload, dict):
                        raise TypeError("a filter frame is a JSON object")
                    parsed = await run_in_threadpool(filters_from_mapping, payload)
                    state["filter"] = _guest_filters(parsed, is_guest=is_guest)
                    frame = {
                        "type": "filter.ok",
                        "filter": state["filter"].echo(),
                        # The client's watchdog is set from this: no frame for three of
                        # these and it assumes the connection is dead and reconnects.
                        "heartbeat_s": beat.interval,
                    }
                except Exception as exc:  # noqa: BLE001 - report, do not disconnect
                    detail = exc.detail if isinstance(exc, HTTPException) else exc
                    frame = {"type": "error", "detail": f"bad filter: {detail}"}
                await websocket.send_text(orjson.dumps(frame).decode())
        except WebSocketDisconnect:
            return
        except RuntimeError:  # the socket closed while we were writing to it
            return

    beat = _Heartbeat(settings.live_status_seconds)
    receiver = asyncio.create_task(receive_filters())
    subscriber = bus.subscribe()

    async def send(frame: dict[str, Any]) -> None:
        await websocket.send_text(orjson.dumps(frame).decode())

    try:
        while True:
            event = await _next_event(subscriber, beat)
            if _take_overflow(subscriber):
                await send(_gap_frame("overflow"))
            current = state["filter"]
            if event is not None:
                if current is None:
                    # Nothing has been asked for yet: only the heartbeat — and word that
                    # something was lost, which is filter-independent — may pass.
                    if event.get("event") in ("ingest.status", GAP_EVENT):
                        frame = await run_in_threadpool(_safe_render, event, CallFilters())
                        if frame is not None:
                            await send(frame)
                else:
                    frame = await run_in_threadpool(_safe_render, event, current)
                    if frame is not None:
                        await send(frame)
            if beat.fire():
                await send(await run_in_threadpool(_status_frame, _status_max_age(beat)))
    except WebSocketDisconnect:
        pass
    except RuntimeError:  # the socket closed while we were writing to it
        pass
    finally:
        bus.unsubscribe(subscriber)
        receiver.cancel()
        # Anything the receiver died of is already a closed socket by the time we are
        # here; re-raising it out of a ``finally`` would surface as "Exception in ASGI
        # application" for what is only a client that went away.
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            await receiver


@router.get(
    "/live/sse",
    summary="The live feed as Server-Sent Events",
    description=(
        "The same events as `WS /live` for clients that cannot open a WebSocket. The filter is "
        "taken from the query string instead of a frame, so it is fixed for the life of the "
        "connection. Each message is `event: <type>` plus a JSON `data:` line."
    ),
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def live_sse(
    f: Filters, request: Request, _released: ReleasedSession = None
) -> StreamingResponse:
    heartbeat = float(get_settings().live_status_seconds)

    async def stream() -> AsyncIterator[bytes]:
        yield b": connected\n\n"
        async for frame in _events(f, heartbeat):
            if await request.is_disconnected():
                break
            yield f"event: {frame['type']}\n".encode() + b"data: " + orjson.dumps(frame) + b"\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get(
    "/live/status",
    summary="Is the live bus connected?",
    description="Diagnostics for the LISTEN/NOTIFY bridge: connection state, subscribers, events seen.",
)
def live_status(
    db: DbSession, verbose: Annotated[bool, Query(description="Include the ingest counters")] = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connected": bus.connected,
        "channel": "keyup_events",
        "subscribers": bus.subscriber_count,
        "events_seen": bus.events_seen,
        "last_event_at": bus.last_event_at,
    }
    if verbose:
        payload["ingest"] = json.loads(json.dumps(ingest_status(db), default=str))
    return payload


__all__ = ["router"]
