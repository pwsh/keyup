"""A long-lived connection must not sit on a database session.

Every request — a WebSocket handshake and an SSE ``GET`` included — goes through
:func:`app.auth.mutation_guard`, which reads the settings table and, for anything
authenticated, the ``sessions``/``users`` rows.  It does that on the request-scoped
session from ``Depends(get_session)``, and for a **WebSocket** that dependency's cleanup
runs when the socket closes.  So ``WS /api/live`` and ``WS /api/recorder/stream`` each held
one pooled connection with an **open read transaction** for as long as the client stayed
connected — days, for a recorder.

Production is what these tests are written from: two sessions ``idle in transaction`` for
53 minutes and 4 minutes, whose last statement was authentication's ``users`` SELECT, at a
moment when ``GET /sites`` was scanning ``calls`` ten times over concurrently.  Idle in
transaction is the expensive half — the pool slot is bad, but the snapshot the transaction
pins keeps autovacuum off the archive's busiest table for the life of the socket.

The SSE routes turn out **not** to have the bug on this FastAPI version — since 0.106 the
dependency exit stack for an HTTP request is unwound before the response body is streamed,
so a ``StreamingResponse`` no longer inherits its request's session.  They are covered here
anyway, and they declare :data:`~app.db.ReleasedSession` anyway, because that is a detail of
the framework's internals rather than a promise of the route's: the day one of them grows a
``DbSession`` parameter, or the exit-stack behaviour changes back, these tests say so.

The assertion below is therefore about the *transaction*, not the object: every session
this process has made must be out of a transaction while the connection is merely open and
nothing is being asked of it.  :data:`app.db.ReleasedSession` is what each route declares
to get there, and the per-poll work each of them does opens its own short
``SessionLocal()`` in a worker thread and closes it again.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def sessions(monkeypatch) -> list[Session]:  # noqa: ANN001
    """Every session the process opens from here on, in the order it opened them."""
    from app import db as db_module

    made: list[Session] = []
    real = db_module.SessionLocal

    def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        session = real(*args, **kwargs)
        made.append(session)
        return session

    monkeypatch.setattr(db_module, "SessionLocal", factory)
    return made


def assert_idle(sessions: list[Session], route: str) -> None:
    """No session anywhere in the process is holding a transaction open."""
    assert sessions, f"{route}: nothing opened a session, so this proves nothing"
    holding = [s for s in sessions if s.in_transaction()]
    assert not holding, (
        f"{route}: {len(holding)} of {len(sessions)} sessions are still in a transaction "
        "while the connection is idle — that is the 'idle in transaction' backend "
        "production saw for 53 minutes"
    )


def test_the_live_websocket_releases_its_session(client, sessions) -> None:  # noqa: ANN001
    """``WS /api/live``: authenticated, accepted, and holding nothing."""
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"
        assert_idle(sessions, "WS /api/live")


def open_sse(path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Open an SSE route for real, read its first frame, and leave it connected.

    Not ``TestClient.stream``: these generators end only on a client disconnect, which the
    test client cannot deliver while the body is being consumed, so a ``with`` block round
    one of them never returns.  Driving the ASGI app directly is also the more honest
    test — the whole point is what FastAPI's *dependency solving* does, and this is the
    same path a real request takes: application-level ``mutation_guard``, the route's
    ``require_admin``, then :data:`~app.db.ReleasedSession`.

    Returns the status and the first body chunk, with the connection still open at the
    moment the caller inspects the sessions; the server task is then cancelled, which is
    what a client hanging up looks like from inside the generator.
    """
    import asyncio

    from app.main import app

    async def drive() -> tuple[int, bytes]:
        target, _, query = path.partition("?")
        raw = [(b"host", b"testserver")]
        raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": target,
            "raw_path": target.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": raw,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        started: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        chunk: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        async def receive() -> dict[str, object]:
            await asyncio.Event().wait()  # the client never hangs up on its own
            raise AssertionError("unreachable")

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start" and not started.done():
                started.set_result(int(message["status"]))
            elif message["type"] == "http.response.body" and not chunk.done():
                chunk.set_result(bytes(message.get("body") or b""))

        served = asyncio.create_task(app(scope, receive, send))
        try:
            status = await asyncio.wait_for(started, timeout=10)
            first = await asyncio.wait_for(chunk, timeout=10) if status == 200 else b""
            # Still connected here — this is the moment the assertion is about.
            return status, first
        finally:
            served.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                await served

    return asyncio.run(drive())


def test_the_live_sse_stream_releases_its_session(client, sessions) -> None:  # noqa: ANN001, ARG001
    """``GET /api/live/sse``: the connection is open, the transaction is not."""
    status, first = open_sse("/api/live/sse")
    assert (status, first) == (200, b": connected\n\n")
    assert_idle(sessions, "GET /api/live/sse")


def test_the_spectrum_stream_releases_its_session(client, admin_token, sessions) -> None:  # noqa: ANN001, ARG001
    """``GET /api/admin/recorders/spectrum/stream``: admin auth, then hands the session back.

    This is the stream the operator watches, and it is the one that dropped below one
    frame a second while ``/sites`` was scanning: it must cost the database nothing at all
    between frames, which it cannot do while holding a transaction open.
    """
    status, first = open_sse(
        "/api/admin/recorders/spectrum/stream?instance=rec-01&tuner=rtl-0",
        {"authorization": f"Bearer {admin_token}"},
    )
    assert (status, first) == (200, b": connected\n\n")
    assert_idle(sessions, "GET /api/admin/recorders/spectrum/stream")


def test_the_recorders_stream_releases_its_session(client, admin_token, sessions) -> None:  # noqa: ANN001, ARG001
    """``GET /api/admin/recorders/stream``: the tuner-status feed, same rule."""
    status, first = open_sse(
        "/api/admin/recorders/stream", {"authorization": f"Bearer {admin_token}"}
    )
    assert (status, first) == (200, b": connected\n\n")
    assert_idle(sessions, "GET /api/admin/recorders/stream")


def test_the_recorder_push_channel_releases_its_session(  # noqa: ANN001
    client, upload_token, sessions
) -> None:
    """``WS /api/recorder/stream``: the socket a recorder holds open for days.

    It is the worst case of the four — a recorder does not close its channel — and it is
    exempt from ``mutation_guard``'s authentication (it presents its own token in the
    handshake), but not from the settings read at the top of the guard, which is enough on
    its own to leave a transaction open for the life of the connection.
    """
    with client.websocket_connect(
        "/api/recorder/stream", headers=bearer(upload_token)
    ) as socket:
        socket.send_text(
            json.dumps({"type": "hello", "instance": "rec-01", "recorder_version": "trunkd 1.2.0"})
        )
        assert json.loads(socket.receive_text())["type"] == "hello"
        assert_idle(sessions, "WS /api/recorder/stream")


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    """The recorder's own credential — the ``upload`` scope and nothing else."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-stream", ["upload"], created_by="tests")
    return raw


@pytest.fixture
def admin_cookie(client, session, user_password) -> str:  # noqa: ANN001
    """A signed-in admin's session cookie — what an operator's browser actually presents."""
    from app.auth import COOKIE_NAME, hash_password
    from app.models import User

    session.add(
        User(
            username="stream-admin",
            display_name="Stream Admin",
            password_hash=hash_password(user_password),
            scopes=["admin"],
        )
    )
    session.commit()
    resp = client.post(
        "/api/auth/login",
        json={"username": "stream-admin", "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return f"{COOKIE_NAME}={client.cookies[COOKIE_NAME]}"


def test_a_cookie_session_does_not_pin_a_transaction_on_the_spectrum_stream(  # noqa: ANN001
    client, admin_cookie, sessions
) -> None:
    """The production shape, exactly: a browser's cookie on the stream the operator watches.

    A bearer token is not enough to reproduce it — verifying one stamps ``last_used_at``
    and commits, which ends the transaction as a side effect. A cookie session only
    *reads* (``sessions``, then ``users``), so the read transaction stays open with the
    ``users`` SELECT as its last statement, which is precisely what production showed
    sitting idle for 53 minutes.
    """
    sessions.clear()  # the login above is a normal request and closes its own session
    status, first = open_sse(
        "/api/admin/recorders/spectrum/stream?instance=rec-01&tuner=rtl-0",
        {"cookie": admin_cookie},
    )
    assert (status, first) == (200, b": connected\n\n")
    assert_idle(sessions, "GET /api/admin/recorders/spectrum/stream (cookie)")
