"""The Broadcastify **Feed** exporter: the mount protocol, the pacing, and the guards.

Nothing here talks to Broadcastify.  The Icecast half runs against a tiny threaded server
in this file that speaks the source protocol and counts bytes; the pacing half runs on an
injected clock, so "one second of silence is thirty-eight frames" is asserted rather than
waited for.

The tests that matter most are the same ones that matter for the Calls exporter, because
the feed publishes to the same company under the same operator selection: a backlog call
must never reach the stream, a call from before the switch was flipped must never reach the
stream, and a talkgroup the operator un-ticked for Broadcastify must never reach the stream.
"""

from __future__ import annotations

import base64
import shutil
import socketserver
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.exporters import gate
from app.exporters.broadcastify_feed import (
    FEED_SERVICE,
    BroadcastifyFeed,
    FeedConfig,
    FeedError,
    FeedStatus,
    FeedSupervisor,
    IcecastSource,
    QueuedCall,
    feed_config,
    probe_mount,
    read_status,
    write_status,
)
from app.exporters.mp3 import iter_frames, parse_header
from app.exporters.transcode import mp3_silence
from tests.conftest import requires_db

# --------------------------------------------------------------------------------------
# a synthetic MPEG-2 layer III frame: 16 kbit/s, 22.05 kHz, mono — the feed's own format
# --------------------------------------------------------------------------------------

#: 0xFF F3 20 C0 — sync, MPEG-2 layer III no-CRC, bitrate index 2 (16 kbit/s), rate index 0
#: (22 050 Hz), no padding, mono.  ``72 * 16000 // 22050`` = 52 bytes long.
FRAME_HEADER = bytes((0xFF, 0xF3, 0x20, 0xC0))
FRAME_LEN = 52
FRAME_S = 576 / 22050


def frame(fill: int = 0) -> bytes:
    return FRAME_HEADER + bytes([fill]) * (FRAME_LEN - 4)


def stream(count: int) -> bytes:
    return b"".join(frame(index % 251) for index in range(count))


class Clock:
    """A virtual monotonic clock: ``sleep`` is the only thing that advances it."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


class FakeSource:
    """An :class:`IcecastSource` with the socket taken out."""

    def __init__(self, config: FeedConfig, *, fail_after: int | None = None) -> None:
        self.config = config
        self.sent: list[bytes] = []
        self.bytes_sent = 0
        self.connects = 0
        self._open = False
        self.fail_after = fail_after

    def connect(self) -> None:
        self.connects += 1
        self._open = True
        self.bytes_sent = 0

    def send(self, data: bytes) -> None:
        if not self._open:
            raise FeedError("not connected")
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            self._open = False
            raise FeedError("the mount dropped the connection")
        self.sent.append(data)
        self.bytes_sent += len(data)

    def close(self) -> None:
        self._open = False

    @property
    def connected(self) -> bool:
        return self._open


def build_feed(
    session_factory=None,  # noqa: ANN001
    *,
    config: FeedConfig | None = None,
    source: FakeSource | None = None,
    clock: Clock | None = None,
    settings: Settings | None = None,
    transcode=None,  # noqa: ANN001
) -> tuple[BroadcastifyFeed, FakeSource, Clock]:
    config = config or FeedConfig(server="audio.test", mount="/m", password="pw")
    source = source or FakeSource(config)
    clock = clock or Clock()
    feed = BroadcastifyFeed(
        config,
        settings=settings or Settings(_env_file=None),
        session_factory=session_factory or (lambda: None),
        clock=clock,
        sleep=clock.sleep,
        source_factory=lambda _config: source,
        transcode=transcode or (lambda *a, **k: stream(4)),
        silence=lambda **_kw: stream(40),
    )
    feed._flush_status = lambda **_kw: None  # type: ignore[method-assign]  # no database here
    return feed, source, clock


# --------------------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------------------


def test_a_feed_frame_header_is_read_as_26_ms_of_22_khz() -> None:
    parsed = parse_header(FRAME_HEADER)
    assert parsed is not None
    length, duration, rate, bitrate = parsed
    assert (length, rate, bitrate) == (FRAME_LEN, 22_050, 16)
    assert duration == pytest.approx(FRAME_S)


@pytest.mark.parametrize(
    "head",
    [
        b"\x00\x00\x00\x00",  # no sync
        b"\xff\xfb\x00\xc0",  # free-format bitrate index
        b"\xff\xf5\x20\xc0",  # layer II, not III
        b"\xff\xf3\x2c\xc0",  # reserved sample-rate index
    ],
)
def test_a_header_that_is_not_a_layer_iii_frame_is_refused(head: bytes) -> None:
    assert parse_header(head) is None


def test_iter_frames_steps_over_an_id3_tag_and_resyncs_after_junk() -> None:
    tag = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
    data = tag + frame(1) + b"garbage" + frame(2)
    frames = list(iter_frames(data))
    assert [f.data[4] for f in frames] == [1, 2]
    assert all(len(f.data) == FRAME_LEN for f in frames)


def test_a_truncated_final_frame_is_not_sent() -> None:
    assert len(list(iter_frames(frame(1) + FRAME_HEADER + b"\x00" * 10))) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_real_encoded_silence_is_one_second_of_feed_format_frames() -> None:
    frames = list(iter_frames(mp3_silence(seconds=1.0, bitrate_kbps=16, sample_rate=22_050)))
    assert frames, "ffmpeg produced no MP3 frames"
    assert {f.sample_rate for f in frames} == {22_050}
    assert {f.bitrate_kbps for f in frames} == {16}
    # LAME's encoder delay adds a frame or two either side of the second we asked for.
    assert sum(f.duration_s for f in frames) == pytest.approx(1.0, abs=0.15)


# --------------------------------------------------------------------------------------
# the mount: a fake Icecast that speaks the source protocol
# --------------------------------------------------------------------------------------


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: D102
        server = self.server
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = self.request.recv(1)
            if not chunk:
                return
            head += chunk
        server.requests.append(head.decode("latin-1"))  # type: ignore[attr-defined]
        if server.refuse:  # type: ignore[attr-defined]
            self.request.sendall(b"HTTP/1.0 401 Unauthorized\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            server.received += len(data)  # type: ignore[attr-defined]
            drop = server.drop_after  # type: ignore[attr-defined]
            if drop is not None and server.received >= drop:  # type: ignore[attr-defined]
                server.dropped.set()  # type: ignore[attr-defined]
                self.request.close()
                return


class FakeIcecast(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *, refuse: bool = False, drop_after: int | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[str] = []
        self.received = 0
        self.refuse = refuse
        self.drop_after = drop_after
        self.dropped = threading.Event()

    def __enter__(self) -> FakeIcecast:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)

    def config(self, **overrides: object) -> FeedConfig:
        host, port = self.server_address[0], self.server_address[1]
        return FeedConfig(server=str(host), port=int(port), mount="/testmount", password="pw", **overrides)  # type: ignore[arg-type]


def headers_of(request: str) -> dict[str, str]:
    lines = request.strip().splitlines()
    return {
        key.strip().lower(): value.strip()
        for key, _, value in (line.partition(":") for line in lines[1:])
        if key
    }


def test_the_source_request_is_the_icecast_source_verb_with_basic_auth() -> None:
    with FakeIcecast() as server:
        config = server.config()
        source = IcecastSource(config)
        source.connect()
        source.send(frame())
        source.close()

    request = server.requests[0]
    assert request.splitlines()[0] == "SOURCE /testmount HTTP/1.0"
    headers = headers_of(request)
    assert headers["content-type"] == "audio/mpeg"
    assert headers["ice-public"] == "0"
    assert headers["ice-name"]
    token = headers["authorization"].split(" ", 1)[1]
    # The username Broadcastify's feed page tells you to use, and the mount password.
    assert base64.b64decode(token).decode() == "source:pw"
    assert server.received == FRAME_LEN


def test_a_mount_that_refuses_the_source_is_an_error_not_a_silent_success() -> None:
    with FakeIcecast(refuse=True) as server:
        source = IcecastSource(server.config())
        with pytest.raises(FeedError) as caught:
            source.connect()
    assert "401" in str(caught.value)
    assert not source.connected


def test_a_mount_that_is_not_listening_is_an_error() -> None:
    config = FeedConfig(server="127.0.0.1", port=9, mount="/m", password="pw", timeout_s=0.5)
    with pytest.raises(FeedError):
        IcecastSource(config).connect()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_test_connection_opens_the_mount_writes_silence_and_closes() -> None:
    with FakeIcecast() as server:
        result = probe_mount(server.config(), seconds=0.5)
    assert result["ok"] is True
    assert server.received > 0
    assert "accepted a source" in result["checked"]
    assert len(server.requests) == 1


def test_the_test_connection_refuses_to_dial_an_unconfigured_feed() -> None:
    result = probe_mount(FeedConfig())
    assert result["ok"] is False
    assert "blank" in result["checked"]


# --------------------------------------------------------------------------------------
# pacing and silence
# --------------------------------------------------------------------------------------


def run_for(feed: BroadcastifyFeed, clock: Clock, seconds: float, *, limit: int = 20_000) -> None:
    stop = threading.Event()
    for _ in range(limit):
        if clock.t >= seconds:
            return
        feed.send_pass(stop)
    raise AssertionError("the sender made no progress")


def test_silence_fills_the_gap_at_exactly_real_time() -> None:
    """A quiet minute is still a minute of bytes: Icecast drops a source that stops."""
    feed, source, clock = build_feed()
    run_for(feed, clock, 1.0)

    # One second at 26.12 ms a frame — never a burst, never a stall.
    assert len(source.sent) == pytest.approx(1.0 / FRAME_S, abs=1)
    assert {len(chunk) for chunk in source.sent} == {FRAME_LEN}
    assert feed.status().connected is True


def test_silence_can_be_turned_off_and_then_nothing_is_written() -> None:
    feed, source, clock = build_feed(config=FeedConfig(server="a", mount="/m", password="p", silence=False))
    run_for(feed, clock, 1.0)
    assert source.sent == []


def test_a_queued_call_plays_before_the_silence_resumes() -> None:
    feed, source, clock = build_feed()
    feed.enqueue(QueuedCall(call_id=7, frames=list(iter_frames(stream(10)))))
    run_for(feed, clock, 10 * FRAME_S)

    assert len(source.sent) == 10
    assert feed.status().calls_played == 0  # still mid-call until the next frame is asked for
    run_for(feed, clock, 1.0)
    assert feed.status().calls_played == 1
    assert len(source.sent) > 10


def test_a_backlog_longer_than_the_bound_drops_the_oldest_calls() -> None:
    """A feed is worth listening to because it is live; late audio is dropped, not queued."""
    config = FeedConfig(server="a", mount="/m", password="p", max_backlog_s=1.0)
    feed, _source, _clock = build_feed(config=config)
    # Four calls of one second each against a one-second bound.
    for call_id in range(1, 5):
        feed.enqueue(QueuedCall(call_id=call_id, frames=list(iter_frames(stream(38)))))

    queued = [item.call_id for item in feed._queue]
    assert queued == [4], "the newest call must survive and the older ones must be dropped"
    assert feed.status().dropped == 3


def test_the_backlog_bound_never_drops_the_only_call_it_has() -> None:
    config = FeedConfig(server="a", mount="/m", password="p", max_backlog_s=0.1)
    feed, _source, _clock = build_feed(config=config)
    feed.enqueue(QueuedCall(call_id=1, frames=list(iter_frames(stream(38)))))
    assert [item.call_id for item in feed._queue] == [1]
    assert feed.status().dropped == 0


def test_the_sender_reconnects_after_the_mount_drops_it() -> None:
    config = FeedConfig(server="a", mount="/m", password="p")
    source = FakeSource(config, fail_after=5)
    feed, source, clock = build_feed(config=config, source=source)
    stop = threading.Event()

    for _ in range(200):
        feed.send_pass(stop)
        if source.connects > 1:
            break

    assert source.connects > 1, "a dropped connection must be reopened"
    status = feed.status()
    assert status.last_error and "dropped" in status.last_error


def test_a_refused_mount_backs_off_rather_than_spinning() -> None:
    class Refusing(FakeSource):
        def connect(self) -> None:
            self.connects += 1
            raise FeedError("the mount refused the source: 401 Unauthorized")

    config = FeedConfig(server="a", mount="/m", password="p")
    feed, source, clock = build_feed(config=config, source=Refusing(config))
    stop = threading.Event()
    feed.send_pass(stop)
    assert feed.backoff_s() == 5.0
    feed.send_pass(stop)
    assert feed.backoff_s() == 10.0
    assert feed.status().connected is False
    assert "401" in (feed.status().last_error or "")


def test_a_long_stall_resynchronizes_instead_of_bursting() -> None:
    """Ten seconds behind is not repaid frame by frame; the stream restarts from now."""
    feed, source, clock = build_feed()
    stop = threading.Event()
    feed.send_pass(stop)  # connects and sends the first frame
    clock.t += 60.0
    feed.send_pass(stop)
    assert feed._due == pytest.approx(clock.t)


# --------------------------------------------------------------------------------------
# status, shared with the API process through the settings table
# --------------------------------------------------------------------------------------


@requires_db
def test_the_status_row_survives_the_trip_through_the_database(session) -> None:  # noqa: ANN001
    write_status(session, FeedStatus(connected=True, bytes_sent=99, calls_played=3, dropped=1))
    read = read_status(session)
    assert read["connected"] is True
    assert (read["bytes_sent"], read["calls_played"], read["dropped"]) == (99, 3, 1)


@requires_db
def test_a_stale_status_reads_as_disconnected(session) -> None:  # noqa: ANN001
    from app.models import Setting

    write_status(session, FeedStatus(connected=True))
    row = session.get(Setting, "broadcastify_feed_status")
    row.value = {**row.value, "updated_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
    session.commit()
    assert read_status(session)["connected"] is False


@requires_db
def test_the_status_key_is_not_an_editable_setting() -> None:
    from app.runtime_settings import SETTABLE_KEYS

    assert "broadcastify_feed_status" not in SETTABLE_KEYS


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


def test_the_feed_ships_disabled_and_unconfigured() -> None:
    settings = Settings(_env_file=None)
    assert settings.export_broadcastify_feed_enabled is False
    config = feed_config(settings)
    assert config.configured() is False
    assert (config.username, config.port) == ("source", 80)
    assert (config.bitrate_kbps, config.sample_rate) == (16, 22_050)
    assert config.max_backlog_s == 120.0


def test_the_mount_gets_exactly_one_leading_slash() -> None:
    assert (
        feed_config(Settings(_env_file=None, broadcastify_feed_mount="testmount")).mount_path
        == "/testmount"
    )
    assert (
        feed_config(Settings(_env_file=None, broadcastify_feed_mount="/testmount")).mount_path
        == "/testmount"
    )


def test_the_fingerprint_changes_with_the_credentials_but_not_with_the_backlog() -> None:
    from dataclasses import replace

    base = FeedConfig(server="a", mount="/m", password="p")
    # The backlog bound and the silence switch are read by the running loop…
    assert base.fingerprint() == replace(base, max_backlog_s=30.0, silence=False).fingerprint()
    # …but a new password or a new format means a new connection.
    assert base.fingerprint() != replace(base, password="q").fingerprint()
    assert base.fingerprint() != replace(base, bitrate_kbps=32).fingerprint()


# --------------------------------------------------------------------------------------
# the supervisor
# --------------------------------------------------------------------------------------


class DummyFeed:
    instances: list[DummyFeed] = []

    def __init__(self, config, *, settings, session_factory) -> None:  # noqa: ANN001
        self.config = config
        self.ran = threading.Event()
        DummyFeed.instances.append(self)

    def run(self, stop: threading.Event) -> None:
        self.ran.set()
        stop.wait(30)


@pytest.fixture
def dummy_feed(monkeypatch):  # noqa: ANN001, ANN201
    from app.exporters import broadcastify_feed as module

    DummyFeed.instances = []
    monkeypatch.setattr(module, "BroadcastifyFeed", DummyFeed)
    return DummyFeed


def test_the_supervisor_starts_nothing_while_the_switch_is_off(dummy_feed) -> None:  # noqa: ANN001
    supervisor = FeedSupervisor(session_factory=lambda: None)
    settings = Settings(
        _env_file=None,
        broadcastify_feed_server="audio.test",
        broadcastify_feed_mount="/m",
        broadcastify_feed_password="pw",
    )
    supervisor.reconcile(settings, enabled=False)
    assert supervisor.running is False
    assert dummy_feed.instances == []


def test_the_supervisor_starts_nothing_without_credentials(dummy_feed) -> None:  # noqa: ANN001
    supervisor = FeedSupervisor(session_factory=lambda: None)
    supervisor.reconcile(Settings(_env_file=None), enabled=True)
    assert supervisor.running is False
    assert dummy_feed.instances == []


def test_the_supervisor_starts_stops_and_restarts_on_a_credential_change(dummy_feed) -> None:  # noqa: ANN001
    supervisor = FeedSupervisor(session_factory=lambda: None)
    settings = Settings(
        _env_file=None,
        broadcastify_feed_server="audio.test",
        broadcastify_feed_mount="/m",
        broadcastify_feed_password="pw",
        export_broadcastify_feed_enabled=True,
    )
    supervisor.reconcile(settings, enabled=True)
    assert supervisor.running is True
    assert dummy_feed.instances[0].ran.wait(5)

    # The same configuration again is a no-op: the stream is not restarted every cycle.
    supervisor.reconcile(settings, enabled=True)
    assert len(dummy_feed.instances) == 1

    moved = settings.model_copy(update={"broadcastify_feed_mount": "/other"})
    supervisor.reconcile(moved, enabled=True)
    assert len(dummy_feed.instances) == 2

    supervisor.reconcile(moved, enabled=False)
    assert supervisor.running is False


# --------------------------------------------------------------------------------------
# the guards and the operator's selection, against the real database
# --------------------------------------------------------------------------------------


@pytest.fixture
def feed_settings(tmp_path) -> Settings:  # noqa: ANN001
    """Feed settings that are credentialled but whose **switch is still off**."""
    return Settings(
        _env_file=None,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        broadcastify_feed_server="audio9.broadcastify.test",
        broadcastify_feed_mount="/testmount",
        broadcastify_feed_password="pw",
    )


@pytest.fixture
def db_feed(session, feed_settings, tmp_path):  # noqa: ANN001, ARG001
    """A feed reading the test database, with the encoder stubbed out."""
    from app.db import SessionLocal

    config = feed_config(feed_settings)
    source = FakeSource(config)
    clock = Clock()
    feed = BroadcastifyFeed(
        config,
        settings=feed_settings,
        session_factory=SessionLocal,
        clock=clock,
        sleep=clock.sleep,
        source_factory=lambda _config: source,
        transcode=lambda *a, **k: stream(6),
        silence=lambda **_kw: stream(40),
    )
    return feed


def queued_ids(feed: BroadcastifyFeed) -> list[int]:
    return [item.call_id for item in feed._queue]


def enable_feed(session, when: datetime | None = None) -> datetime:  # noqa: ANN001
    """Stamp the feed's own enable watermark, the way the worker's cycle does."""
    mark = gate.set_watermark(session, FEED_SERVICE, when=when)
    session.commit()
    return mark


@requires_db
def test_a_live_call_after_the_watermark_reaches_the_stream(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    call = seed_call(session, root=tmp_path)

    assert db_feed.poll_once() == 1
    assert queued_ids(db_feed) == [call.id]


@requires_db
def test_the_poll_leaves_the_heavy_columns_behind(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """The three-second poll reads no transcript, search vector or JSON blob.

    Not in the SELECT, and not fetched afterwards either: a deferred column something on
    the feed's path did read would show up here as a second, per-call SELECT.
    """
    from sqlalchemy import event

    from app.db import engine
    from app.exporters.broadcastify_feed import FEED_POLL_SKIPPED
    from tests.test_exporters import seed_call

    heavy = set(FEED_POLL_SKIPPED)
    assert {"raw_meta", "search_tsv", "transcript_text"} <= heavy
    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    call = seed_call(session, root=tmp_path)
    statements: list[str] = []

    def spy(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        if "FROM calls" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        assert db_feed.poll_once() == 1
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    assert queued_ids(db_feed) == [call.id]
    assert statements, "the poll read no calls at all"
    for statement in statements:
        for column in heavy:
            assert f"calls.{column}" not in statement, (column, statement)


@requires_db
def test_the_feed_encodes_each_call_at_the_export_loudness(session, db_feed, feed_settings, tmp_path) -> None:  # noqa: ANN001
    """The feed is a bitstream of calls, so the export gain lands per call, before the encode.

    And it *composes with* the §5.2 normalize filter rather than replacing it — the feed's
    own ``af`` handling is what this stage is stacked on top of.
    """
    from app.exporters.loudness import export_filter
    from app.stt.normalize import ffmpeg_audio_filter
    from tests.test_exporters import seed_call

    seen: list[str | None] = []

    def recording(path, *, af=None, **_kw):  # noqa: ANN001, ANN202, ARG001
        seen.append(af)
        return stream(6)

    db_feed._transcode = recording
    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    seed_call(session, root=tmp_path)

    assert db_feed.poll_once() == 1
    normalize = ffmpeg_audio_filter(feed_settings)
    assert seen == [export_filter(normalize, feed_settings)]
    assert seen[0].endswith("loudnorm=I=-11:TP=-3.0:LRA=11,alimiter=limit=0.707946:level=disabled:attack=5:release=50")
    if normalize:
        assert seen[0].startswith(normalize)


@requires_db
def test_a_short_call_on_the_feed_takes_the_measured_gain_branch(
    session, db_feed, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """Under three seconds the feed gets the same ``volume=`` branch every export does."""
    from app.exporters import loudness
    from tests.test_exporters import seed_call

    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-25.0, -50.0)
    )
    seen: list[str | None] = []

    def recording(path, *, af=None, **_kw):  # noqa: ANN001, ANN202, ARG001
        seen.append(af)
        return stream(6)

    db_feed._transcode = recording
    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    seed_call(session, root=tmp_path, duration=1.4)

    assert db_feed.poll_once() == 1
    assert seen[0].endswith("volume=14dB,alimiter=limit=0.707946:level=disabled:attack=5:release=50")


@requires_db
def test_the_feed_is_unchanged_when_the_export_loudness_is_off(session, tmp_path) -> None:  # noqa: ANN001
    """0 means the feed streams exactly what it streamed before the setting existed."""
    from app.db import SessionLocal
    from app.exporters.broadcastify_feed import feed_config
    from app.stt.normalize import ffmpeg_audio_filter
    from tests.test_exporters import seed_call

    settings = Settings(
        _env_file=None,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        broadcastify_feed_server="audio9.broadcastify.test",
        broadcastify_feed_mount="/testmount",
        broadcastify_feed_password="pw",
        export_target_lufs=0.0,
    )
    seen: list[str | None] = []
    config = feed_config(settings)
    feed = BroadcastifyFeed(
        config,
        settings=settings,
        session_factory=SessionLocal,
        clock=Clock(),
        sleep=lambda _s: None,
        source_factory=lambda _config: FakeSource(config),
        transcode=lambda _p, *, af=None, **_kw: (seen.append(af), stream(6))[1],
        silence=lambda **_kw: stream(40),
    )
    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    seed_call(session, root=tmp_path)

    assert feed.poll_once() == 1
    assert seen == [ffmpeg_audio_filter(settings)]


@requires_db
def test_a_backlog_call_is_never_streamed(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """The same guard as the Calls exporter: a migration must not become a live feed."""
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    seed_call(session, root=tmp_path, priority="backlog")
    seed_call(session, root=tmp_path, priority=None, tgid=1102)

    assert db_feed.poll_once() == 0
    assert queued_ids(db_feed) == []


@requires_db
def test_a_call_from_before_the_switch_was_flipped_is_never_streamed(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    from tests.test_exporters import seed_call

    seed_call(session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(minutes=1))
    enable_feed(session)

    assert db_feed.poll_once() == 0
    assert queued_ids(db_feed) == []


@requires_db
def test_nothing_is_streamed_while_there_is_no_watermark(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    from tests.test_exporters import seed_call

    seed_call(session, root=tmp_path)
    assert db_feed.poll_once() == 0


@requires_db
def test_an_encrypted_call_is_not_streamed(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    seed_call(session, root=tmp_path, encrypted=True)
    assert db_feed.poll_once() == 0


@requires_db
def test_a_talkgroup_the_operator_untick_for_broadcastify_is_not_streamed(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """One selection, both Broadcastify products: the Calls checkbox governs the feed too."""
    from app.models import Setting, Talkgroup
    from app.runtime_settings import invalidate
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(seconds=30))
    call = seed_call(session, root=tmp_path)
    session.add(Setting(key="export_broadcastify_talkgroup_mode", value="allow"))
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.export_broadcastify = False
    session.commit()
    invalidate()

    assert db_feed.poll_once() == 0
    assert queued_ids(db_feed) == []

    talkgroup.export_broadcastify = True
    session.commit()
    db_feed._handled.clear()
    assert db_feed.poll_once() == 1


@requires_db
def test_a_call_older_than_the_backlog_bound_is_not_even_encoded(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(hours=2))
    seed_call(session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(hours=1))
    db_feed._floor = datetime.now(UTC) - timedelta(hours=2)

    assert db_feed.poll_once() == 0


@requires_db
def test_a_long_call_ingested_after_a_shorter_later_one_still_streams(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """Rows are inserted when a call *ends*, not when it starts.

    A 30 s dispatch call keyed up at t0 lands after a 2 s call that keyed up at t0+5.  The
    old ``started_at`` cursor had already passed t0 by then, so the long call — the traffic
    a listener most wants — never went out.
    """
    from tests.test_exporters import seed_call

    t0 = datetime.now(UTC) - timedelta(seconds=40)
    enable_feed(session, t0 - timedelta(seconds=30))
    short = seed_call(session, root=tmp_path, started_at=t0 + timedelta(seconds=5), duration=2.0)
    assert db_feed.poll_once() == 1

    long = seed_call(session, root=tmp_path, started_at=t0, duration=30.0)
    assert db_feed.poll_once() == 1
    assert queued_ids(db_feed) == [short.id, long.id]

    # …and neither is picked up a second time.
    assert db_feed.poll_once() == 0


@requires_db
def test_calls_sharing_a_start_time_are_not_lost_to_the_page_limit(session, db_feed, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Ties split by ``LIMIT`` used to fall on the far side of a ``started_at >`` cursor."""
    from app.exporters import broadcastify_feed
    from tests.test_exporters import seed_call

    monkeypatch.setattr(broadcastify_feed, "POLL_LIMIT", 2)
    when = datetime.now(UTC) - timedelta(seconds=10)
    enable_feed(session, when - timedelta(seconds=30))
    ids = [
        seed_call(session, root=tmp_path, started_at=when, file_path=f"20260901/tie-{n}.mp3").id
        for n in range(3)
    ]

    assert db_feed.poll_once() == 2
    assert db_feed.poll_once() == 1
    assert sorted(queued_ids(db_feed)) == sorted(ids)


@requires_db
def test_a_call_committed_after_a_higher_id_is_still_picked_up(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """Ids are handed out at INSERT and seen at COMMIT; two uploads can commit out of order.

    Staged by handling the higher id first: a high-water ``id`` cursor would now skip the
    lower one, which is exactly what an out-of-order commit looks like to the loader.
    """
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(seconds=60))
    early = seed_call(session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(seconds=20))
    late = seed_call(session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(seconds=10))
    db_feed._handled[int(late.id)] = late.started_at

    assert db_feed.poll_once() == 1
    assert queued_ids(db_feed) == [early.id]


@requires_db
def test_the_handled_set_forgets_calls_the_age_bound_has_passed(session, db_feed, tmp_path) -> None:  # noqa: ANN001
    """The cursor is bounded: nothing older than one backlog window is remembered."""
    from tests.test_exporters import seed_call

    enable_feed(session, datetime.now(UTC) - timedelta(hours=1))
    db_feed._handled[10**12] = datetime.now(UTC) - timedelta(minutes=30)
    call = seed_call(session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(seconds=5))

    assert db_feed.poll_once() == 1
    assert list(db_feed._handled) == [call.id]


@requires_db
def test_the_worker_cycle_stamps_the_feed_watermark_and_reconciles(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The switch is read on the worker's cycle, like the two upload switches."""
    from app.db import SessionLocal
    from app.exporters.worker import ExportWorker
    from app.models import Setting
    from app.runtime_settings import invalidate

    seen: list[bool] = []
    monkeypatch.setattr(
        FeedSupervisor,
        "reconcile",
        lambda self, settings, *, enabled: seen.append(enabled),  # noqa: ARG005
    )
    worker = ExportWorker(
        settings=Settings(_env_file=None, audio_root=tmp_path, tz="America/Winnipeg"),
        session_factory=SessionLocal,
        clients=[],
    )

    worker.cycle()
    assert seen == [False]
    assert gate.watermark(session, FEED_SERVICE) is None

    session.add(Setting(key="export_broadcastify_feed_enabled", value=True))
    session.commit()
    invalidate()
    worker.cycle()
    assert seen[-1] is True
    assert gate.watermark(session, FEED_SERVICE) is not None

    session.get(Setting, "export_broadcastify_feed_enabled").value = False
    session.commit()
    invalidate()
    worker.cycle()
    assert seen[-1] is False
    assert gate.watermark(session, FEED_SERVICE) is None
