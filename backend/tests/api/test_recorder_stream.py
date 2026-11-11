"""``WS /api/recorder/stream`` — the recorder's push channel.

The wire contract these tests hold the server to is the one written out in
``docs/RECORDERS.md`` §"Push channel"; the trunkd client is written against the same text,
so a change here is a change to a contract with something that is not in this repository.
"""

from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

STREAM = "/api/recorder/stream"


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    """The recorder's own credential: a raw ``kup_…`` token with only the ``upload`` scope."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


def hello(instance: str, *, seen: int | None = None, version: str = "trunkd 1.2.0") -> str:
    return json.dumps(
        {"type": "hello", "instance": instance, "recorder_version": version, "desired_seen": seen}
    )


def register(client, upload_token: str, instance: str) -> None:  # noqa: ANN001
    """Create the recorders row the way a real recorder does — its first status POST."""
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": instance, "tuners": [{"tuner_key": "rtl-0", "backend": "soapy"}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200, resp.text


def recorder_id(client, admin_token: str, instance: str) -> int:  # noqa: ANN001
    body = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    return next(r["id"] for r in body if r["instance_id"] == instance)


def until(socket, kind: str, limit: int = 12) -> dict:  # noqa: ANN001
    """The next frame of ``kind``, skipping heartbeats and other noise."""
    for _ in range(limit):
        frame = json.loads(socket.receive_text())
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame in {limit} frames")


# ------------------------------------------------------------------------------ auth ---


def test_a_bearer_upload_token_opens_the_socket(client, upload_token) -> None:  # noqa: ANN001
    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-bearer"))
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "hello"
        assert frame["heartbeat_s"] == 15
        assert frame["desired_version"] == 0
        assert frame["recorder_block_version"] == 0
        assert frame["server_time"].endswith("Z")


def test_the_key_query_parameter_also_opens_it(client, upload_token) -> None:  # noqa: ANN001
    """For a tool that cannot set a header — the same shape ``/api/call-upload`` allows."""
    with client.websocket_connect(f"{STREAM}?key={upload_token}") as socket:
        socket.send_text(hello("rec-key"))
        assert json.loads(socket.receive_text())["type"] == "hello"


def test_no_token_is_refused_with_4401(client) -> None:  # noqa: ANN001
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(STREAM) as s:
        s.receive_text()
    assert refused.value.code == 4401


def test_a_read_token_is_refused_with_4401(client, read_token) -> None:  # noqa: ANN001
    """The scope is checked, not merely the credential: this is the recorder's channel."""
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(STREAM, headers=bearer(read_token)) as s,
    ):
        s.receive_text()
    assert refused.value.code == 4401


def test_a_first_frame_that_is_not_a_hello_is_4408(client, upload_token) -> None:  # noqa: ANN001
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket,
    ):
        socket.send_text(json.dumps({"type": "ping"}))
        socket.receive_text()
    assert refused.value.code == 4408


def test_the_hello_timeout_is_ten_seconds(client) -> None:  # noqa: ANN001
    """Documented rather than waited for: a 10 s sleep in the suite buys nothing."""
    from app.api import recorder_stream

    assert recorder_stream.HELLO_TIMEOUT_S == 10.0
    assert recorder_stream.CLOSE_NO_HELLO == 4408


def test_an_upload_token_is_enough_when_reads_are_closed(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """AUTH_REQUIRE_READ closes `WS /api/live` to an upload token; this route is not that.

    The guard's WebSocket branch asks a socket for the ``read`` scope when the archive is
    closed, and a recorder's token does not hold it — so the push channel has to be on the
    self-authenticating list or the one client it exists for could never open it.
    """
    client.put(
        "/api/admin/settings", json={"auth_require_read": "true"}, headers=bearer(admin_token)
    ).raise_for_status()
    try:
        assert client.get("/api/calls").status_code == 401  # the archive really is closed
        with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
            socket.send_text(hello("rec-closed"))
            assert json.loads(socket.receive_text())["type"] == "hello"
    finally:
        client.put(
            "/api/admin/settings", json={"auth_require_read": ""}, headers=bearer(admin_token)
        )


# ----------------------------------------------------------------------------- hello ---


def test_a_stale_desired_seen_gets_a_desired_state_at_once(
    client, upload_token, admin_token
) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-stale")
    rid = recorder_id(client, admin_token, "rec-stale")
    client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"role": "control"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-stale", seen=0))
        greeting = json.loads(socket.receive_text())
        assert greeting["type"] == "hello"
        assert greeting["desired_version"] == 1
        frame = json.loads(socket.receive_text())
        assert frame == {"type": "desired_state", "version": 1, "recorder_block_version": 0}


def test_a_current_desired_seen_gets_no_desired_state(
    client, upload_token, admin_token, monkeypatch
) -> None:  # noqa: ANN001
    from app.api import recorder_stream

    monkeypatch.setattr(recorder_stream, "HEARTBEAT_S", 0.5)
    register(client, upload_token, "rec-current")
    rid = recorder_id(client, admin_token, "rec-current")
    client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"role": "control"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-current", seen=1))
        greeting = json.loads(socket.receive_text())
        assert greeting["desired_version"] == 1
        # Nothing has changed since version 1, so the *next* frame is the heartbeat —
        # never a "go fetch" for something the recorder already has.
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "heartbeat"
        assert frame["ts"].endswith("Z")


def test_an_unknown_recorder_keeps_the_socket_open(client, upload_token) -> None:  # noqa: ANN001
    """No row yet: the recorder's first status POST creates it, and the socket waits."""
    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-not-yet"))
        greeting = json.loads(socket.receive_text())
        assert greeting["desired_version"] == 0
        socket.send_text(json.dumps({"type": "ping"}))
        assert until(socket, "pong") == {"type": "pong"}

        # And once the row appears, the socket that was waiting for it is told.
        register(client, upload_token, "rec-not-yet")
        frame = until(socket, "desired_state")
        assert frame["version"] == 0


# ------------------------------------------------------------------------------ push ---


def test_a_tuner_write_pushes_a_desired_state(client, upload_token, admin_token) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-push")
    rid = recorder_id(client, admin_token, "rec-push")

    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-push", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"

        resp = client.put(
            f"/api/admin/recorders/{rid}/tuners/rtl-0",
            json={"role": "voice"},
            headers={**bearer(admin_token), **CSRF_HEADERS},
        )
        assert resp.status_code == 200, resp.text

        frame = until(socket, "desired_state")
        assert frame["version"] == 1
        assert frame["recorder_block_version"] == 0


def test_a_recorder_block_write_pushes_a_desired_state(client, upload_token, admin_token) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-block")
    rid = recorder_id(client, admin_token, "rec-block")

    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-block", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"

        resp = client.put(
            f"/api/admin/recorders/{rid}",
            json={"spool_only": True},
            headers={**bearer(admin_token), **CSRF_HEADERS},
        )
        assert resp.status_code == 200, resp.text

        frame = until(socket, "desired_state")
        # The tuner counter did not move; the recorder block's did.
        assert frame["version"] == 0
        assert frame["recorder_block_version"] == 1


# ------------------------------------------------------------------- keepalive frames ---


def test_a_quiet_socket_sends_a_heartbeat(client, upload_token, monkeypatch) -> None:  # noqa: ANN001
    """15 s in production; shortened here so the suite does not sit still for it."""
    from app.api import recorder_stream

    monkeypatch.setattr(recorder_stream, "HEARTBEAT_S", 0.5)
    register(client, upload_token, "rec-quiet")
    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-quiet", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"
        frame = until(socket, "heartbeat")
        assert frame["ts"].endswith("Z")


def test_ping_is_answered_with_pong(client, upload_token) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-ping")
    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-ping", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"
        socket.send_text(json.dumps({"type": "ping"}))
        assert until(socket, "pong") == {"type": "pong"}


def test_an_unknown_client_frame_is_ignored(client, upload_token) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-unknown")
    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-unknown", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"
        socket.send_text(json.dumps({"type": "who-knows", "payload": 1}))
        socket.send_text("not json at all")
        # The socket survives both, and still answers.
        socket.send_text(json.dumps({"type": "ping"}))
        assert until(socket, "pong") == {"type": "pong"}


# -------------------------------------------------------------------------- registry ---


def test_admin_recorders_reports_the_open_socket(client, upload_token, admin_token) -> None:  # noqa: ANN001
    register(client, upload_token, "rec-registry")
    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    before = next(r for r in rows if r["instance_id"] == "rec-registry")["stream"]
    assert before == {"connected": False, "since": None, "client_count": 0}

    with client.websocket_connect(STREAM, headers=bearer(upload_token)) as socket:
        socket.send_text(hello("rec-registry", seen=0))
        assert json.loads(socket.receive_text())["type"] == "hello"

        rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        during = next(r for r in rows if r["instance_id"] == "rec-registry")["stream"]
        assert during["connected"] is True
        assert during["client_count"] == 1
        assert during["since"] is not None

    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    after = next(r for r in rows if r["instance_id"] == "rec-registry")["stream"]
    assert after["connected"] is False
    assert after["client_count"] == 0
