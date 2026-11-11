"""``WS /api/recorder/stream`` — the recorder's push channel (docs/RECORDERS.md).

A recorder polls ``GET /api/recorder/desired-state`` every ~10 s.  That is the contract and
it stays: this socket does not carry the desired-state document and never replaces the
poll.  All it does is say *"go fetch"* the moment something an operator wrote lands, so a
retune reaches the box in milliseconds instead of at the next poll.  A dropped frame, a
dropped socket, a keyup restart — every one of them costs at most one poll interval, which
is exactly today's behaviour.

The frame contract (both directions, JSON text) is written out verbatim in
``docs/RECORDERS.md`` §"Push channel"; the trunkd client is written against that text.

Two things decide when a ``desired_state`` frame goes out:

* a **per-connection poll** of one indexed row every :data:`POLL_INTERVAL_S`, run in a
  worker thread — a synchronous SQLAlchemy session must never block the event loop — which
  is what makes this work at all under several uvicorn workers: the write that bumped the
  version may have happened in a *different process*, and no in-process signal would cross;
* an **in-process broadcaster**, :func:`announce`, that the admin write paths call after
  they commit, so a change made in this worker is pushed without waiting for the poll.

The registry behind :func:`stream_info` is per-process for the same reason in reverse: with
several workers, ``GET /api/admin/recorders`` reports the connections *this* worker holds
and cannot see the others'.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.auth import bearer_secret, verify_token
from app.db import ReleasedSession, SessionLocal
from app.models import Recorder

log = logging.getLogger("app.api.recorder_stream")
router = APIRouter(tags=["recorder"])

#: Seconds of silence after which a ``heartbeat`` frame is sent, and the value advertised
#: to the client in the server's ``hello`` so it can size its own read timeout.
HEARTBEAT_S = 15.0

#: How long the client has to send its ``hello`` after the socket is accepted.
HELLO_TIMEOUT_S = 10.0

#: How often one connection re-reads its recorder's version columns.  One indexed SELECT
#: per connection per second: a recorder fleet is single digits, not thousands.
POLL_INTERVAL_S = 1.0

#: At most one ``desired_state`` frame per this many seconds, so a burst of writes (an
#: operator saving four tuners, the arbiter promoting a recorder) is one "go fetch".
COALESCE_S = 0.25

#: Close codes.  4401: no usable credential.  4408: no ``hello`` in time.
CLOSE_UNAUTHORIZED = 4401
CLOSE_NO_HELLO = 4408

#: The scopes that may open this socket.  The recorder's own token carries ``upload``;
#: ``admin`` implies it, which is what lets a person open the socket with a tool.
STREAM_SCOPES = frozenset({"upload", "admin"})


def _now() -> str:
    """The current instant as RFC 3339 with a ``Z`` offset."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------- registry ---


@dataclass
class _Connection:
    """One open socket, and the way a synchronous writer wakes it."""

    instance: str
    since: datetime
    loop: asyncio.AbstractEventLoop
    wake: asyncio.Event = field(default_factory=asyncio.Event)


_registry_lock = threading.Lock()
_connections: dict[str, list[_Connection]] = {}


def _register(conn: _Connection) -> None:
    with _registry_lock:
        _connections.setdefault(conn.instance, []).append(conn)


def _unregister(conn: _Connection) -> None:
    with _registry_lock:
        peers = _connections.get(conn.instance)
        if not peers:
            return
        with contextlib.suppress(ValueError):
            peers.remove(conn)
        if not peers:
            _connections.pop(conn.instance, None)


def announce(instance: str | None) -> None:
    """Nudge every socket this process holds for ``instance`` to re-read its versions.

    Called from the admin write paths **after the commit**, so the version the woken
    connection reads back is the one that was just written.  Safe from a worker thread —
    the wake goes through the connection's own event loop — and safe to call for an
    instance nobody is connected for, which is the common case.

    This is an optimization, never the mechanism: a change made in another uvicorn worker
    is picked up by the per-connection poll within :data:`POLL_INTERVAL_S` regardless.
    """
    if not instance:
        return
    with _registry_lock:
        peers = list(_connections.get(instance, ()))
    for conn in peers:
        with contextlib.suppress(RuntimeError):  # the loop closed under us
            conn.loop.call_soon_threadsafe(conn.wake.set)


def stream_info(instance: str) -> dict[str, Any]:
    """``{connected, since, client_count}`` for one recorder — **per worker process**."""
    with _registry_lock:
        peers = list(_connections.get(instance, ()))
    return {
        "connected": bool(peers),
        "since": min((c.since for c in peers), default=None),
        "client_count": len(peers),
    }


# ------------------------------------------------------------------- database reads ---


def _authenticate(secret: str | None) -> str | None:
    """The token's label when ``secret`` is a live token holding a stream scope, else None."""
    if not secret:
        return None
    session = SessionLocal()
    try:
        token = verify_token(session, secret)
        if token is None:
            return None
        if not (set(token.scopes or ()) & STREAM_SCOPES):
            return None
        return token.label or "token"
    finally:
        session.close()


def _versions(instance: str, recorder_id: int | None) -> tuple[int, str, int, int] | None:
    """``(id, instance_id, version, desired_recorder_version)`` for one recorder, or None.

    One indexed SELECT: by primary key once the row has been found, by the unique
    ``instance_id`` until then.  ``instance_id`` is read back too, so a rename — which
    changes what the recorder should be calling itself — counts as a change like any other.
    """
    session = SessionLocal()
    try:
        stmt = select(
            Recorder.id,
            Recorder.instance_id,
            Recorder.version,
            Recorder.desired_recorder_version,
        )
        stmt = (
            stmt.where(Recorder.id == recorder_id)
            if recorder_id is not None
            else stmt.where(Recorder.instance_id == instance)
        )
        row = session.execute(stmt).first()
    finally:
        session.close()
    if row is None:
        return None
    return (int(row[0]), str(row[1]), int(row[2] or 0), int(row[3] or 0))


# ------------------------------------------------------------------------- the route ---


@router.websocket("/recorder/stream")
async def recorder_stream(websocket: WebSocket, _released: ReleasedSession = None) -> None:
    """Tell one recorder, immediately, that its desired state moved.

    ``Authorization: Bearer kup_…`` on a token holding the ``upload`` scope — the recorder's
    own credential, the same one the poll and the status push use — or ``?key=…`` for a
    command-line tool that cannot set a header.  Anything else is closed with **4401**
    before a frame is exchanged.  There is no cookie path and no ``Origin`` check because
    there is no cookie to ride on: a browser cannot open this socket by accident.

    Then: ``{"type":"hello","instance":…,"recorder_version":…,"desired_seen":…}`` within
    :data:`HELLO_TIMEOUT_S` seconds or the socket is closed with **4408**.
    """
    secret = bearer_secret(websocket) or websocket.query_params.get("key")
    label = await run_in_threadpool(_authenticate, secret)
    if label is None:
        log.info("recorder stream: refused, no token with the 'upload' scope")
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="an 'upload' token is required")
        return

    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HELLO_TIMEOUT_S)
    except TimeoutError:
        log.info("recorder stream: closing, no hello within %.0fs (%s)", HELLO_TIMEOUT_S, label)
        # A close on a socket the client has already dropped raises rather than closing;
        # there is nothing left to tell it, so the attempt is best-effort.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close(code=CLOSE_NO_HELLO, reason="no hello frame")
        return
    except (WebSocketDisconnect, RuntimeError):
        return

    hello = _parse_hello(raw)
    if hello is None:
        log.info("recorder stream: closing, first frame was not a hello (%s)", label)
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close(code=CLOSE_NO_HELLO, reason="first frame must be a hello")
        return
    instance, recorder_version, desired_seen = hello

    await _serve(websocket, instance, recorder_version, desired_seen, label)


def _parse_hello(raw: str) -> tuple[str, str | None, int | None] | None:
    """``(instance, recorder_version, desired_seen)``, or None when this is not a hello."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "hello":
        return None
    instance = payload.get("instance")
    if not isinstance(instance, str) or not instance.strip():
        return None
    version = payload.get("recorder_version")
    seen = payload.get("desired_seen")
    if isinstance(seen, bool) or not isinstance(seen, int):
        seen = None
    return instance.strip()[:128], version if isinstance(version, str) else None, seen


async def _serve(
    websocket: WebSocket,
    instance: str,
    recorder_version: str | None,
    desired_seen: int | None,
    label: str,
) -> None:
    """The connection proper: hello answer, then push/heartbeat until it goes away."""
    send_lock = asyncio.Lock()
    clock = {"last": time.monotonic(), "pushed": 0.0}

    async def send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(orjson.dumps(frame).decode())
        clock["last"] = time.monotonic()

    state = await run_in_threadpool(_versions, instance, None)
    recorder_id = state[0] if state is not None else None
    version = state[2] if state is not None else 0
    block = state[3] if state is not None else 0

    log.info(
        "recorder stream: %s connected (recorder_version=%s desired_seen=%s token=%s)",
        instance,
        recorder_version,
        desired_seen,
        label,
    )
    # These two sends are outside the loop's own guard below, so they carry their own: a
    # recorder that goes away in the instant between the handshake and the hello answer is
    # an ordinary disconnect, and letting it out of the route would make it an unhandled
    # ASGI exception instead.
    try:
        await send(
            {
                "type": "hello",
                "server_time": _now(),
                "heartbeat_s": int(HEARTBEAT_S),
                "desired_version": version,
                "recorder_block_version": block,
            }
        )
        # A client that has never seen a version (null) is told to fetch, but only once
        # there is a row to fetch for: a recorder keyup has not met yet has no desired
        # state at all, and its first status POST is what creates it.
        if state is not None and (desired_seen is None or desired_seen < version):
            await send({"type": "desired_state", "version": version, "recorder_block_version": block})
            clock["pushed"] = time.monotonic()
    except (WebSocketDisconnect, RuntimeError):
        log.info("recorder stream: %s disconnected (before the hello answer)", instance)
        return

    conn = _Connection(instance=instance, since=datetime.now(UTC), loop=asyncio.get_running_loop())
    seen = (state[1], version, block) if state is not None else None

    async def receive_frames() -> None:
        """Answer ``ping``; ignore everything else, which is how the contract grows."""
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                log.debug("recorder stream %s: ignoring unparseable frame", instance)
                continue
            kind = payload.get("type") if isinstance(payload, dict) else None
            if kind == "ping":
                await send({"type": "pong"})
            else:
                log.debug("recorder stream %s: ignoring frame of type %r", instance, kind)

    _register(conn)
    receiver = asyncio.create_task(receive_frames())
    reason = "client closed"
    try:
        while True:
            if receiver.done():
                break
            # A wake from `announce` cuts the wait short; the timeout is the poll, which
            # is what carries a change made in another worker process.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(conn.wake.wait(), timeout=POLL_INTERVAL_S)
            conn.wake.clear()

            current = await run_in_threadpool(_versions, instance, recorder_id)
            if current is not None:
                recorder_id = current[0]
                latest = (current[1], current[2], current[3])
                if latest != seen:
                    # Coalesce a burst: one "go fetch" for four tuners saved together.
                    delay = COALESCE_S - (time.monotonic() - clock["pushed"])
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # Re-read after the wait so the frame carries the *settled* versions.
                    current = await run_in_threadpool(_versions, instance, recorder_id) or current
                    seen = (current[1], current[2], current[3])
                    await send(
                        {
                            "type": "desired_state",
                            "version": current[2],
                            "recorder_block_version": current[3],
                        }
                    )
                    clock["pushed"] = time.monotonic()
                    continue
            if time.monotonic() - clock["last"] >= HEARTBEAT_S:
                await send({"type": "heartbeat", "ts": _now()})
    except WebSocketDisconnect:
        pass
    except RuntimeError:  # the socket closed while we were writing to it
        reason = "socket closed"
    except Exception as exc:  # noqa: BLE001 - one bad connection must not take the worker
        reason = f"error: {exc}"
        log.warning("recorder stream %s: %s", instance, exc)
    finally:
        _unregister(conn)
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receiver
        log.info("recorder stream: %s disconnected (%s)", instance, reason)


__all__ = ["announce", "router", "stream_info"]
