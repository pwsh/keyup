"""The live feed: LISTEN/NOTIFY into WebSocket and SSE frames (§4.4)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import requires_db

pytestmark = requires_db


def _wait_for_bus(timeout: float = 5.0) -> None:
    """The listener connects in a background thread; give it a moment."""
    from app.live import bus

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bus.connected:
            return
        time.sleep(0.05)
    pytest.fail("the live bus never connected to PostgreSQL")


def test_the_bus_starts_with_the_application(client) -> None:
    _wait_for_bus()
    body = client.get("/api/live/status").json()
    assert body["connected"] is True
    assert body["channel"] == "keyup_events"


def test_a_notify_reaches_a_websocket_client(client, archive, session) -> None:
    from app.db import notify

    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"

        notify(session, "call.new", {"id": archive.transcribed_id})
        session.commit()

        frame = json.loads(socket.receive_text())
        assert frame["type"] == "call.new"
        # The payload carries an id only; the API re-reads the row and sends the full call.
        assert frame["call"]["id"] == archive.transcribed_id
        assert frame["call"]["transcript_text"].startswith("Engine 1")
        assert frame["call"]["audio_url"].endswith("/audio")


def test_the_client_filter_decides_what_is_delivered(client, archive, session) -> None:
    from app.db import notify
    from app.models import Call

    _wait_for_bus()
    wanted = session.get(Call, archive.transcribed_id)
    other = next(
        session.get(Call, i)
        for i in archive.call_ids
        if session.get(Call, i).talkgroup_id != wanted.talkgroup_id
    )

    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({"talkgroup": [wanted.talkgroup_ids[0]]}))
        ack = json.loads(socket.receive_text())
        assert ack["filter"]["talkgroup"] == [wanted.talkgroup_ids[0]]

        notify(session, "call.new", {"id": other.id})
        notify(session, "call.transcribed", {"id": wanted.id})
        session.commit()

        frame = json.loads(socket.receive_text())
        # The filtered-out call is never delivered, so the first frame is the wanted one.
        assert frame["type"] == "call.transcribed"
        assert frame["call"]["id"] == wanted.id


def test_a_bad_filter_frame_is_reported_not_fatal(client, archive) -> None:
    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text("{not json")
        error = json.loads(socket.receive_text())
        assert error["type"] == "error"
        assert "bad filter" in error["detail"]

        socket.send_text(json.dumps({"talkgroup": [1701]}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"


class _GoneAway:
    """A socket whose client leaves with 1001 after one filter frame.

    ``TestClient`` cannot stage this: its exit stack cancels the application task before it
    reads the task's result, so an exception escaping the route is swallowed there and the
    bug would pass unnoticed.  The route is therefore driven directly, against a websocket
    that behaves the way a closed browser tab does — every further read *and* write raises
    ``WebSocketDisconnect(1001, '')``.
    """

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.state = SimpleNamespace()
        self.sent: list[str] = []
        self.gone = False
        self._frames = [json.dumps({})]

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        self.gone = True
        raise WebSocketDisconnect(code=1001, reason="")

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise WebSocketDisconnect(code=1001, reason="")
        self.sent.append(text)


def test_a_client_going_away_is_a_clean_close_not_an_asgi_error(session) -> None:  # noqa: ANN001, ARG001
    """1001 "going away" — a tab closed, a page navigated — must not raise out of the route.

    ``docker logs keyup-api-1`` carried two ``Exception in ASGI application`` a day whose
    root was ``starlette.websockets.WebSocketDisconnect: (1001, '')``.  The filter-reading
    task ended on the disconnect, and the route's ``finally`` re-raised it by awaiting the
    task it had just cancelled — cancelling a task that has already finished is a no-op, so
    ``await receiver`` handed the exception straight back out of the route.
    """
    from app.api.live import live_socket

    socket = _GoneAway()
    asyncio.run(live_socket(socket))  # the assertion: it returns instead of raising

    assert socket.gone
    assert json.loads(socket.sent[0])["type"] == "filter.ok"


def test_an_edit_announces_itself_on_the_feed(client, archive, write_token) -> None:
    """`PATCH /calls/{id}` notifies, so every open feed sees the change."""
    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({}))
        socket.receive_text()

        client.patch(
            f"/api/calls/{archive.call_ids[0]}",
            json={"starred": True},
            headers={"Authorization": f"Bearer {write_token}"},
        )
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "call.updated"
        assert frame["call"]["id"] == archive.call_ids[0]
        assert frame["call"]["starred"] is True


def test_sse_stream_is_event_stream_formatted(client, archive) -> None:
    """Pull the first two frames straight from the generator.

    An SSE response never ends, so it is driven directly rather than through the test
    client, which would block waiting for a body that is designed not to finish.
    """
    import asyncio

    from app.api.filters import CallFilters
    from app.api.live import live_sse

    class _NeverDisconnects:
        async def is_disconnected(self) -> bool:
            return False

    async def drive() -> tuple[bytes, bytes]:
        response = await live_sse(CallFilters(), _NeverDisconnects())
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        frames = response.body_iterator
        first = await frames.__anext__()
        # The heartbeat is the only thing that arrives on a quiet feed.
        second = await asyncio.wait_for(frames.__anext__(), timeout=30)
        await frames.aclose()
        return first, second

    _wait_for_bus()
    first, second = asyncio.run(drive())
    assert first == b": connected\n\n"
    assert second.startswith(b"event: ingest.status\ndata: ")
    assert json.loads(second.split(b"data: ", 1)[1])["status"]["calls"] == len(archive.call_ids)


def test_sse_is_documented_as_an_event_stream(client) -> None:
    schema = client.get("/api/openapi.json").json()
    responses = schema["paths"]["/api/live/sse"]["get"]["responses"]
    assert "text/event-stream" in responses["200"]["content"]


def test_unparseable_notifications_are_ignored(client, session) -> None:
    from sqlalchemy import text

    from app.live import bus

    _wait_for_bus()
    before = bus.events_seen
    session.execute(text("SELECT pg_notify('keyup_events', 'not json at all')"))
    session.commit()
    time.sleep(0.5)
    assert bus.events_seen == before
    assert bus.connected is True


def test_a_cross_origin_websocket_is_refused(client, archive) -> None:
    """M1: a browser applies no same-origin policy to a WebSocket, and sends the cookie."""
    from starlette.websockets import WebSocketDisconnect

    _wait_for_bus()
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/api/live", headers={"Origin": "https://evil.example"}) as s,
    ):
        s.receive_text()
    assert refused.value.code == 1008

    # The same connection from this site is fine.
    with client.websocket_connect("/api/live", headers={"Origin": "http://testserver"}) as socket:
        socket.send_text(json.dumps({}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"


def test_a_superseded_call_is_forwarded_as_a_bare_pair_of_ids(client, archive, session) -> None:
    """Reconciliation demoted a row a live list is showing (§3.8, 2026-09-10).

    No call object and no filtering: the demoted row is now ``duplicate_of`` the winner and
    so passes nobody's filter — which is exactly why the client cannot be left to re-read
    it, and why the frame is the two ids.
    """
    from app.db import notify

    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({"talkgroup": [999999]}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"

        notify(
            session,
            "call.superseded",
            {"id": archive.transcribed_id, "by": 4242, "talkgroup_id": 1, "started_at": None},
        )
        session.commit()

        frame = json.loads(socket.receive_text())
    assert frame == {"type": "call.superseded", "id": archive.transcribed_id, "by": 4242}


def test_a_superseded_event_without_an_id_is_dropped(client) -> None:
    from app.api import live as live_api
    from app.api.filters import CallFilters

    assert live_api._render({"event": "call.superseded", "by": 7}, CallFilters()) is None


def test_one_event_is_rendered_once_for_identical_subscribers(client, archive, session) -> None:
    """L10: fifty open tabs on the same filter must not be fifty identical queries."""
    from app.api import live as live_api
    from app.api.filters import CallFilters

    live_api._render_cache.clear()
    calls: list[int] = []
    original = live_api.call_service.call_if_matches

    def counting(session_, f, call_id):  # noqa: ANN001, ANN202
        calls.append(call_id)
        return original(session_, f, call_id)

    live_api.call_service.call_if_matches = counting
    try:
        event = {"event": "call.new", "id": archive.transcribed_id}
        frames = [live_api._render(event, CallFilters()) for _ in range(5)]
    finally:
        live_api.call_service.call_if_matches = original

    assert all(f == frames[0] for f in frames)
    assert len(calls) == 1


def test_nothing_is_delivered_before_the_client_has_sent_a_filter(client, archive, session) -> None:
    """The socket used to start as "everything": a call recorded between the accept and the
    first filter frame went out unfiltered, which the calls page — reconnecting on every
    filter change — showed as another talkgroup's call under a narrowed list."""
    from app.db import notify
    from app.models import Call

    _wait_for_bus()
    wanted = session.get(Call, archive.transcribed_id)
    other = next(
        session.get(Call, i)
        for i in archive.call_ids
        if session.get(Call, i).talkgroup_id != wanted.talkgroup_id
    )
    with client.websocket_connect("/api/live") as socket:
        # An event before any filter frame: held back, not broadcast.
        notify(session, "call.new", {"id": other.id})
        session.commit()
        time.sleep(0.3)
        socket.send_text(json.dumps({"talkgroup": [wanted.talkgroup_ids[0]]}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"
        notify(session, "call.transcribed", {"id": wanted.id})
        session.commit()
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "call.transcribed"
        assert frame["call"]["id"] == wanted.id


def test_a_rolling_window_with_no_end_passes_a_call_started_now(archive, session) -> None:  # noqa: ANN001
    """The SPA's rolling presets send ``date_from`` only, and "until now" stays open.

    The page used to freeze ``date_to`` at the moment it loaded and send that as the live
    filter frame, so every call started after page load failed ``started_at <= date_to``
    and the Live and Calls feeds went quiet.  A frame with no ``date_to`` is open-ended.
    """
    from datetime import UTC, datetime, timedelta

    from app.api.filters import filters_from_mapping
    from app.models import Call
    from app.services.calls import call_if_matches

    now = datetime.now(UTC)
    call = session.get(Call, archive.transcribed_id)
    call.started_at = now
    call.ended_at = now + timedelta(seconds=3)
    session.commit()

    since = (now - timedelta(hours=24)).replace(second=0, microsecond=0)
    frame = filters_from_mapping({"date_from": since.isoformat().replace("+00:00", "Z")})
    assert frame.date_to is None
    found = call_if_matches(session, frame, call.id)
    assert found is not None
    assert found.id == call.id

    # …and the frozen end the SPA used to send is exactly what hid it.
    loaded = now - timedelta(minutes=5)
    frozen = filters_from_mapping(
        {"date_from": since.isoformat(), "date_to": loaded.isoformat()}
    )
    assert call_if_matches(session, frozen, call.id) is None


# ------------------------------------------------------------------ Robustness ---


@pytest.fixture(autouse=True)
def _fresh_live_caches():  # noqa: ANN202
    """The render and status memos are process-wide; no test inherits another's frames."""
    from app.api import live as live_api

    live_api._render_cache.clear()
    live_api._status_cache = None
    yield
    live_api._render_cache.clear()
    live_api._status_cache = None


def test_an_event_that_fails_to_render_is_skipped_not_fatal(client, archive, session, monkeypatch) -> None:  # noqa: ANN001
    """L4: a database blip while re-reading one call used to close every subscriber's
    socket.  The event is skipped and the stream carries on."""
    from app.api import live as live_api
    from app.db import notify

    original = live_api.call_service.call_if_matches
    failures = {"left": 1}

    def flaky(session_, f, call_id):  # noqa: ANN001, ANN202
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("server closed the connection unexpectedly")
        return original(session_, f, call_id)

    monkeypatch.setattr(live_api.call_service, "call_if_matches", flaky)
    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"

        notify(session, "call.new", {"id": archive.call_ids[0]})
        session.commit()
        notify(session, "call.transcribed", {"id": archive.transcribed_id})
        session.commit()

        frame = json.loads(socket.receive_text())
    # The first event was the one that failed: skipped, and the socket is still open.
    assert failures["left"] == 0
    assert frame["type"] == "call.transcribed"
    assert frame["call"]["id"] == archive.transcribed_id


def test_the_bus_stamps_every_event_with_an_increasing_sequence() -> None:
    from app.live import LiveBus

    bus_ = LiveBus("postgresql://unused/never")

    async def drive() -> list[dict]:
        subscriber = bus_.subscribe()
        original = {"event": "call.updated", "id": 7}
        bus_.publish(original)
        bus_.publish(dict(original))
        got = [await subscriber.queue.get(), await subscriber.queue.get()]
        assert "seq" not in original  # a stamped copy, not the caller's dict
        return got

    first, second = asyncio.run(drive())
    assert first["id"] == second["id"] == 7
    assert second["seq"] == first["seq"] + 1


def test_two_edits_to_one_call_inside_the_cache_window_are_both_rendered(client, archive, session) -> None:  # noqa: ANN001
    """L5: keyed on (kind, call, filter) the second ``call.updated`` within 3 s was answered
    with the first one's frame — the star that was just set appeared unset."""
    from app.api import live as live_api
    from app.api.filters import CallFilters
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    call.notes = "first"
    session.commit()
    first = live_api._render({"event": "call.updated", "id": call.id, "seq": 1}, CallFilters())

    call.notes = "second"
    session.commit()
    second = live_api._render({"event": "call.updated", "id": call.id, "seq": 2}, CallFilters())
    # …while a repeat of one event (another subscriber, same filter) is still one query.
    again = live_api._render({"event": "call.updated", "id": call.id, "seq": 2}, CallFilters())

    assert first["call"]["notes"] == "first"
    assert second["call"]["notes"] == "second"
    assert again is second


def test_the_heartbeat_keeps_time_while_filtered_out_events_stream_past(client, archive, monkeypatch) -> None:  # noqa: ANN001
    """Every bus event used to restart the heartbeat timer, including the ones the
    subscriber's filter threw away — a narrow filter on a busy system never heard one."""
    from app.api import live as live_api
    from app.api.filters import CallFilters
    from app.live import bus

    narrow = CallFilters(talkgroup=(999999,))

    async def drive() -> tuple[list[dict], float]:
        frames: list[dict] = []
        stream = live_api._events(narrow, 0.6)
        started = time.monotonic()

        async def chatter() -> None:
            while True:
                bus.publish({"event": "call.new", "id": archive.call_ids[0]})
                await asyncio.sleep(0.1)

        noise = asyncio.create_task(chatter())
        try:
            frame = await asyncio.wait_for(stream.__anext__(), timeout=5)
            frames.append(frame)
        finally:
            noise.cancel()
            await stream.aclose()
        return frames, time.monotonic() - started

    _wait_for_bus()
    frames, took = asyncio.run(drive())
    assert frames[0]["type"] == "ingest.status"
    assert took < 3


def test_the_status_frame_is_computed_once_per_interval_not_per_subscriber(client, monkeypatch) -> None:  # noqa: ANN001
    from app.api import live as live_api

    calls: list[int] = []

    def counting(session_):  # noqa: ANN001, ANN202, ARG001
        calls.append(1)
        return {"calls": len(calls)}

    monkeypatch.setattr(live_api, "ingest_status", counting)
    frames = [live_api._status_frame(10.0) for _ in range(20)]
    assert len(calls) == 1
    assert all(frame is frames[0] for frame in frames)

    # Past the age it is recomputed; with no age (a direct call) it always is.
    live_api._status_cache = (time.monotonic() - 11, frames[0])
    live_api._status_frame(10.0)
    live_api._status_frame()
    assert len(calls) == 3


def test_a_listener_reconnect_tells_every_client_there_is_a_gap(client, archive, session) -> None:  # noqa: ANN001
    """L6: notifications sent while the LISTEN connection was down are gone for good.  The
    bus says so on the way back, so the client refetches instead of keeping a hole."""
    from sqlalchemy import text

    from app.live import bus

    _wait_for_bus()
    before = bus.reconnects
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({"talkgroup": [999999]}))
        assert json.loads(socket.receive_text())["type"] == "filter.ok"

        killed = session.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND query ILIKE 'LISTEN keyup_events%'"
            )
        ).scalars().all()
        session.commit()
        assert killed and all(killed)

        frame = json.loads(socket.receive_text())
        while frame["type"] == "ingest.status":
            frame = json.loads(socket.receive_text())
    assert frame == {"type": "gap", "reason": "reconnected"}
    assert bus.reconnects == before + 1
    _wait_for_bus()


def test_a_subscriber_that_overflowed_is_told_so_once() -> None:
    from app.api import live as live_api
    from app.live import QUEUE_MAXSIZE, Subscriber

    async def drive() -> tuple[bool, bool, bool]:
        subscriber = Subscriber(asyncio.get_running_loop())
        for n in range(QUEUE_MAXSIZE):
            subscriber.offer({"event": "call.new", "id": n})
        clean = live_api._take_overflow(subscriber)
        subscriber.offer({"event": "call.new", "id": -1})
        return clean, live_api._take_overflow(subscriber), live_api._take_overflow(subscriber)

    clean, first, second = asyncio.run(drive())
    assert (clean, first, second) == (False, True, False)
    assert live_api._render({"event": "live.gap", "reason": "overflow"}, None) == {
        "type": "gap",
        "reason": "overflow",
    }


def test_filter_ok_carries_the_heartbeat_interval(client, archive) -> None:  # noqa: ANN001
    from app.config import get_settings

    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({}))
        ack = json.loads(socket.receive_text())
    assert ack["heartbeat_s"] == float(get_settings().live_status_seconds)


# --------------------------------------------------------- Filter validation ---


@pytest.mark.parametrize(
    ("frame", "field"),
    [
        ({"talkgroup": [3_000_000_000]}, "talkgroup"),
        ({"patch": 2**31}, "patch"),
        ({"radio": [2**63]}, "radio"),
        ({"ids": ["seven"]}, "ids"),
        ({"emergency": "maybe"}, "emergency"),
        ({"starred": 2}, "starred"),
        ({"min_duration": "x"}, "min_duration"),
        ({"max_duration": -1}, "max_duration"),
        ({"time_from": "25:99"}, "time_from"),
        ({"q": {"nested": True}}, "q"),
    ],
)
def test_a_badly_typed_filter_is_a_422_not_an_exception_per_event(frame, field) -> None:  # noqa: ANN001
    from fastapi import HTTPException

    from app.api.filters import filters_from_mapping

    with pytest.raises(HTTPException) as refused:
        filters_from_mapping(frame)
    assert refused.value.status_code == 422
    assert str(refused.value.detail).startswith(field)


def test_booleans_and_numbers_in_a_mapping_are_coerced() -> None:
    """``bool("false")`` is True — ``include_duplicates: "false"`` used to include them."""
    from app.api.filters import filters_from_mapping

    f = filters_from_mapping(
        {
            "emergency": "false",
            "encrypted": "true",
            "starred": 1,
            "include_duplicates": "false",
            "include_shared": "0",
            "min_duration": "2.5",
            "talkgroup": ["1701", 1102, 1103.0],
            "has_audio": "",
        }
    )
    assert f.emergency is False
    assert f.encrypted is True
    assert f.starred is True
    assert f.include_duplicates is False
    assert f.include_shared is False
    assert f.min_duration == 2.5
    assert f.talkgroup == (1701, 1102, 1103)
    assert f.has_audio is None


def test_an_out_of_range_talkgroup_is_a_422_on_rest(client, archive) -> None:  # noqa: ANN001
    """``talkgroup=3000000000`` overflowed ``integer[]`` and answered 500."""
    response = client.get("/api/calls", params={"talkgroup": 3_000_000_000})
    assert response.status_code == 422
    assert "talkgroup" in response.json()["detail"]
    # A system "id" too long for the column is simply no such system, not an error.
    assert client.get("/api/calls", params={"system": "99999999999"}).json()["items"] == []


def test_an_out_of_range_filter_frame_is_an_error_frame_and_the_socket_lives_on(client, archive, session) -> None:  # noqa: ANN001
    from app.db import notify

    _wait_for_bus()
    with client.websocket_connect("/api/live") as socket:
        socket.send_text(json.dumps({"talkgroup": [3_000_000_000]}))
        error = json.loads(socket.receive_text())
        assert error["type"] == "error"
        assert error["detail"].startswith("bad filter: talkgroup")

        socket.send_text(json.dumps([1, 2]))
        assert json.loads(socket.receive_text())["type"] == "error"

        socket.send_text(json.dumps({"starred": "false", "include_duplicates": "false"}))
        ack = json.loads(socket.receive_text())
        assert ack["type"] == "filter.ok"
        assert ack["filter"]["starred"] is False

        notify(session, "call.transcribed", {"id": archive.transcribed_id})
        session.commit()
        frame = json.loads(socket.receive_text())
        assert frame["type"] == "call.transcribed"
