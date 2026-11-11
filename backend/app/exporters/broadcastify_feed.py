"""The Broadcastify **Feed** exporter — a live Icecast source, not a file uploader.

Broadcastify sells two different products and this deployment owns both kinds of
credential, which is exactly how they get confused:

``Broadcastify Calls``   (:mod:`app.exporters.broadcastify`)
    one HTTP upload *per call*, authenticated with an API key and a numeric system id.
    A call arrives in their archive a few seconds after it ends.
``Broadcastify Feed``    (this module)
    one **continuous audio stream** to an Icecast mount, authenticated with a mount
    password.  It is what a listener hears live on the feed's page, and the operator's
    feed page states its parameters under *Feed Technical Details (Advanced)*:

        If you are required to provide a username for your broadcast client, use:
        ``source``.  Server ``audioN.broadcastify.com``, Port ``80``, Mount ``/…``,
        Password ``…``

Those five fields are the five settings this module reads, spelled the way Broadcastify
spells them (``broadcastify_feed_server`` / ``_port`` / ``_mount`` / ``_password`` /
``_username``), so that an operator can copy them across without translation.

**What it does.**  It plays the archive's live calls, in order, as one MP3 bitstream at
real time.  Every accepted call is re-encoded to the mount's exact format (CBR mono MP3,
16 kbit/s at 22.05 kHz by default), and the gaps between calls — most of the day on a
quiet system — are filled with encoded silence, because an Icecast mount drops a source
that stops writing.  Frames are handed to the socket on a clock rather than as fast as
they encode: a feed that ran ahead of real time would empty its queue and stutter.

**What it never does.**  It never plays anything the Calls exporter would not upload.  The
same two safety guards apply, unchanged (:mod:`app.exporters.gate`): live admission band
only, and only calls that started after *this* service's own enable watermark
(``export_broadcastify_feed_enabled_at``).  On top of them it honours the operator's
Broadcastify *selection* — ``export_broadcastify_talkgroup_mode``, the per-talkgroup and
per-frequency ``export_broadcastify`` checkboxes, ``export_broadcastify_systems``, the
encrypted and minimum-duration rules — because the selection is a statement about what may
be published to Broadcastify, and the feed is Broadcastify.

**Queue policy.**  A feed's whole value is being live.  When calls arrive faster than real
time (a busy incident, or a burst after a reconnect) the queue is trimmed from the
*oldest* end to ``broadcastify_feed_max_backlog_s`` seconds: the newest audio always goes
out, and what is dropped is counted rather than played late.

**It ships off.**  ``export_broadcastify_feed_enabled`` defaults to ``false`` and must stay
that way while the operator's existing broadcast client is running: two sources cannot
hold one Icecast mount, and the second one is simply refused (docs/EXPORTERS.md).
"""

from __future__ import annotations

import base64
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.audio import AudioUnavailable, resolve_playback
from app.config import Settings
from app.models import Call, Setting
from app.runtime_settings import effective_settings
from app.stt.normalize import ffmpeg_audio_filter

from .gate import excluded_reason, live_band_clause, unit_calls_clause, watermark
from .gate import export_normalize as _export_normalize
from .ledger import skip_reason_for
from .loudness import export_filter
from .mp3 import Frame, iter_frames
from .transcode import TranscodeError, mp3_silence, transcode_to_mp3

log = logging.getLogger("app.exporters.broadcastify_feed")

__all__ = [
    "FEED_SERVICE",
    "SELECTION_SERVICE",
    "STATUS_KEY",
    "BroadcastifyFeed",
    "FeedConfig",
    "FeedError",
    "FeedStatus",
    "FeedSupervisor",
    "IcecastSource",
    "QueuedCall",
    "feed_config",
    "read_status",
    "probe_mount",
    "write_status",
]

#: This service's own name — the enable switch ``export_broadcastify_feed_enabled`` and the
#: watermark ``export_broadcastify_feed_enabled_at`` are both built from it.  It is
#: deliberately *not* in :data:`app.models.CALL_EXPORT_SERVICES`: the feed has no ledger,
#: because a live stream has no per-call outcome to record and nothing to retry.
FEED_SERVICE = "broadcastify_feed"

#: …but the operator's *mode and system list* are Broadcastify's, shared with the Calls
#: exporter: one Broadcastify account, one ``export_broadcastify_talkgroup_mode`` and one
#: ``export_broadcastify_systems``.  :data:`app.exporters.gate.SETTINGS_SERVICE` is what
#: maps this service onto them, so the selection functions are called with ``FEED_SERVICE``
#: and still read Broadcastify's settings.
#:
#: The per-talkgroup **checkbox** is no longer shared.  The feed reads
#: ``talkgroups.export_broadcastify`` — the column both products used to share, so an
#: existing selection keeps meaning what it meant — while Calls reads
#: ``talkgroups.export_bcfy_calls``, which an operator sets separately because a
#: permanent archive on somebody else's site and audio that scrolls past are not the same
#: decision (:data:`app.exporters.gate.FLAG_COLUMNS`).
SELECTION_SERVICE = FEED_SERVICE

#: Where the feed's live status is written for the API to read.  The worker process holds
#: the socket and the API process answers ``GET /admin/exports``, so the status has to go
#: through the database; this is the same trick the enable watermark uses, and like it the
#: key is not in :data:`app.runtime_settings.SETTINGS_SPEC`, so it is never editable and
#: never layered into :class:`~app.config.Settings`.
STATUS_KEY = "broadcastify_feed_status"

#: A status older than this is reported as disconnected however it was left: it means the
#: worker died mid-stream rather than shutting the mount down.
STATUS_STALE_S = 90.0

#: How often the status row is rewritten while nothing changes.  One small UPDATE a minute
#: is cheap; a write per frame would be forty a second.
STATUS_INTERVAL_S = 15.0

#: Reconnect backoff: 5 s, 10 s, 20 s, 40 s, capped.  A mount held by another source
#: (the operator's other broadcast client) fails instantly and forever, so the cap matters
#: more than the curve.
BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 60.0

#: How far behind the wall clock the sender may fall before it stops trying to catch up.
#: A stall longer than this — a slow transcode, a paused process — is resynchronized rather
#: than repaid, because dumping ten seconds of audio into the socket at once is a burst the
#: listener hears as a skip and Icecast may simply reject.
MAX_DRIFT_S = 5.0

#: How long the loader sleeps between looks for new calls.  Shorter than the Calls
#: exporter's poll because this one is trying to be live.
POLL_INTERVAL_S = 3.0

#: How many new calls one poll will pick up.  A cap rather than a policy: anything past it
#: is picked up on the next poll, and the backlog trim decides what actually plays.
POLL_LIMIT = 25

#: The ``calls`` columns the poll leaves behind.  It runs every three seconds and used to read
#: whole rows; the JSON blobs and the transcript here are most of a row's width on production
#: (``raw_meta`` ~0.9 KB, ``reconcile_reason`` ~0.5 KB of a ~1.5 KB row on recent calls) and
#: were parsed into Python for every call the poll saw, while nothing on the feed's path —
#: :func:`~app.exporters.ledger.skip_reason_for`, :func:`~app.exporters.gate.excluded_reason`,
#: :func:`~app.audio.resolve_playback`, the export filter — reads any of them.  A deny-list
#: rather than ``load_only``'s allow-list: the gate reads the call through ``getattr`` in a
#: dozen places, and a column an allow-list forgot would come back as one lazy SELECT per
#: call rather than as an error, which is the slow path this exists to remove.  A column
#: named here that something does read still works — deferred, it loads on first touch.
FEED_POLL_SKIPPED = (
    "raw_meta",
    "reconcile_reason",
    "source_quality",
    "transcript_text",
    "search_tsv",
    "notes",
)
FEED_POLL_DEFERRED = tuple(defer(getattr(Call, name)) for name in FEED_POLL_SKIPPED)

USER_AGENT = "keyup-feed/1.0"


class FeedError(RuntimeError):
    """The mount could not be opened, or the connection to it failed."""


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """The five fields from *Feed Technical Details (Advanced)*, plus the encode knobs."""

    server: str = ""
    port: int = 80
    mount: str = ""
    username: str = "source"
    password: str = ""
    bitrate_kbps: int = 16
    sample_rate: int = 22_050
    max_backlog_s: float = 120.0
    silence: bool = True
    name: str = "keyup"
    timeout_s: float = 15.0

    @property
    def mount_path(self) -> str:
        """The mount with exactly one leading slash, however the operator pasted it."""
        return "/" + self.mount.strip().lstrip("/")

    def configured(self) -> bool:
        """Is there enough here to open a mount at all?"""
        return bool(self.server.strip() and self.mount.strip() and self.password)

    def fingerprint(self) -> tuple:
        """Everything a running stream would have to be restarted to pick up.

        The password and the encode format are in it; the backlog bound and the silence
        switch are *not*, because both are read on every pass of the running loop.
        """
        return (
            self.server.strip(),
            int(self.port),
            self.mount_path,
            self.username,
            self.password,
            int(self.bitrate_kbps),
            int(self.sample_rate),
        )


def feed_config(settings: Any) -> FeedConfig:
    """Read the feed's settings off a :class:`~app.config.Settings`."""

    def number(key: str, fallback: float) -> float:
        try:
            return float(getattr(settings, key, fallback) or fallback)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return fallback

    return FeedConfig(
        server=str(getattr(settings, "broadcastify_feed_server", "") or "").strip(),
        port=int(number("broadcastify_feed_port", 80)),
        mount=str(getattr(settings, "broadcastify_feed_mount", "") or "").strip(),
        username=str(getattr(settings, "broadcastify_feed_username", "source") or "source").strip(),
        password=str(getattr(settings, "broadcastify_feed_password", "") or ""),
        bitrate_kbps=int(number("broadcastify_feed_bitrate_kbps", 16)),
        sample_rate=int(number("broadcastify_feed_sample_rate", 22_050)),
        max_backlog_s=number("broadcastify_feed_max_backlog_s", 120.0),
        silence=bool(getattr(settings, "broadcastify_feed_silence", True)),
        timeout_s=number("export_timeout_s", 15.0),
    )


# --------------------------------------------------------------------------------------
# status, shared with the API process through the settings table
# --------------------------------------------------------------------------------------


@dataclass
class FeedStatus:
    """What ``GET /admin/exports`` shows for the feed."""

    connected: bool = False
    since: str | None = None
    bytes_sent: int = 0
    calls_played: int = 0
    dropped: int = 0
    queued_s: float = 0.0
    last_error: str | None = None
    last_error_at: str | None = None
    updated_at: str | None = None

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["updated_at"] = datetime.now(UTC).isoformat()
        return data


def write_status(session: Session, status: FeedStatus) -> None:
    """Persist the feed's status.  Never raises — a status write is not worth a stream."""
    try:
        row = session.get(Setting, STATUS_KEY)
        if row is None:
            row = Setting(
                key=STATUS_KEY,
                description="Live status of the Broadcastify Feed source (written by the worker).",
            )
            session.add(row)
        row.value = status.payload()
        session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not break the feed
        log.debug("broadcastify feed: could not write the status row", exc_info=True)
        session.rollback()


def read_status(session: Session) -> dict[str, Any]:
    """The last status the worker wrote, with a stale one reported as disconnected."""
    blank = asdict(FeedStatus())
    row = session.get(Setting, STATUS_KEY)
    value = row.value if row is not None else None
    if not isinstance(value, dict):
        return blank
    data = {**blank, **{k: v for k, v in value.items() if k in blank}}
    stamped = str(data.get("updated_at") or "")
    if stamped:
        try:
            when = datetime.fromisoformat(stamped)
        except ValueError:  # pragma: no cover - defensive
            when = None
        if when is not None:
            when = when if when.tzinfo else when.replace(tzinfo=UTC)
            if (datetime.now(UTC) - when).total_seconds() > STATUS_STALE_S:
                data["connected"] = False
    return data


# --------------------------------------------------------------------------------------
# the Icecast source connection
# --------------------------------------------------------------------------------------


class IcecastSource:
    """One source connection to an Icecast mount.

    Icecast accepts a source on the classic ``SOURCE /mount HTTP/1.0`` verb (what every
    broadcast client from butt to sdrtrunk sends) and, since 2.4, on ``PUT``.  ``SOURCE``
    is what this sends: it is the one both Icecast and the Shoutcast-compatible front ends
    Broadcastify runs will accept, and the response is an ordinary HTTP status line either
    way.

    Authentication is HTTP Basic with the username from the feed page — ``source`` — and
    the mount password.  Neither is ever logged; :meth:`connect` logs the status line and
    the mount, and nothing else.
    """

    def __init__(
        self,
        config: FeedConfig,
        *,
        connector: Callable[[tuple[str, int], float], socket.socket] | None = None,
    ) -> None:
        self.config = config
        self._connector = connector or (lambda address, timeout: socket.create_connection(address, timeout))
        self._sock: socket.socket | None = None
        self.bytes_sent = 0

    # -- lifecycle ---------------------------------------------------------------------

    def connect(self) -> None:
        config = self.config
        if not config.configured():  # pragma: no cover - the caller checks first
            raise FeedError("the feed has no server, mount or password")
        try:
            sock = self._connector((config.server, int(config.port)), config.timeout_s)
        except OSError as exc:
            raise FeedError(f"could not reach {config.server}:{config.port}: {exc}") from exc
        try:
            sock.settimeout(config.timeout_s)
            sock.sendall(self.request_head())
            reply = self._read_reply(sock)
        except OSError as exc:
            self._shut(sock)
            raise FeedError(f"the mount refused the connection: {exc}") from exc
        if not self._accepted(reply):
            self._shut(sock)
            raise FeedError(f"the mount refused the source: {reply.strip()[:200] or 'no reply'}")
        self._sock = sock
        self.bytes_sent = 0
        log.info("broadcastify feed: source connected to %s%s", config.server, config.mount_path)

    def request_head(self) -> bytes:
        """The source request, byte for byte.  Separate so a test can read it."""
        config = self.config
        token = base64.b64encode(f"{config.username}:{config.password}".encode()).decode("ascii")
        headers = [
            f"SOURCE {config.mount_path} HTTP/1.0",
            f"Host: {config.server}:{int(config.port)}",
            f"Authorization: Basic {token}",
            f"User-Agent: {USER_AGENT}",
            "Content-Type: audio/mpeg",
            f"ice-name: {config.name}",
            # A keyup feed is never listed: the mount's own Broadcastify page is the
            # listing, and a directory entry from a source client would be a second one.
            "ice-public: 0",
            "ice-description: Keyup live scanner audio",
            (
                "ice-audio-info: "
                f"ice-samplerate={int(config.sample_rate)};ice-bitrate={int(config.bitrate_kbps)};ice-channels=1"
            ),
        ]
        return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")

    @staticmethod
    def _accepted(reply: str) -> bool:
        """Icecast answers ``HTTP/1.0 200 OK``; a Shoutcast front end answers ``OK2``."""
        head = reply.strip().splitlines()[0].strip() if reply.strip() else ""
        return " 200" in head or head.upper().startswith("OK")

    @staticmethod
    def _read_reply(sock: socket.socket, limit: int = 4096) -> str:
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) < limit:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buffer += chunk
        return buffer.decode("latin-1", "replace")

    @staticmethod
    def _shut(sock: socket.socket) -> None:
        with suppress(OSError):  # closing a socket the other end already broke
            sock.close()

    def send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise FeedError("the feed is not connected")
        try:
            sock.sendall(data)
        except OSError as exc:
            self.close()
            raise FeedError(f"the mount dropped the connection: {exc}") from exc
        self.bytes_sent += len(data)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            self._shut(sock)

    @property
    def connected(self) -> bool:
        return self._sock is not None


def probe_mount(
    config: FeedConfig,
    *,
    seconds: float = 2.0,
    connector: Callable[[tuple[str, int], float], socket.socket] | None = None,
) -> dict[str, Any]:
    """Open the mount, write ``seconds`` of silence, close.  For the admin test button.

    This is the only honest test there is: Icecast has no "check my password" endpoint, and
    a mount either accepts a source or it does not.  It publishes **no call audio** — what
    a listener hears for those two seconds is silence — but it *does* briefly occupy the
    mount, so it fails while another broadcast client holds it, which is itself the answer
    an operator most often needs.
    """
    if not config.configured():
        return {
            "ok": False,
            "checked": "nothing: the server, mount or password is blank",
            "detail": "set the Server, Mount and Password from the feed's Feed Technical Details panel",
        }
    source = IcecastSource(config, connector=connector)
    try:
        silence = mp3_silence(
            seconds=max(0.5, seconds), bitrate_kbps=config.bitrate_kbps, sample_rate=config.sample_rate
        )
    except TranscodeError as exc:
        return {"ok": False, "checked": "the encoder, before any connection was made", "detail": str(exc)}
    try:
        source.connect()
        source.send(silence)
    except FeedError as exc:
        return {
            "ok": False,
            "checked": f"opening the mount {config.mount_path} on {config.server}:{config.port}",
            "detail": str(exc),
        }
    finally:
        source.close()
    return {
        "ok": True,
        "checked": (
            f"the mount {config.mount_path} accepted a source and took "
            f"{source.bytes_sent} bytes of silence; nothing else was published"
        ),
        "detail": f"connected to {config.server}:{config.port} as {config.username}",
    }


# --------------------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------------------


def _duration_of(call: Call) -> float | None:
    """``call.duration_s`` as a float — it is a ``Numeric`` column, so a ``Decimal``.

    Only the export gain reads it, and only to decide whether the clip is short enough to
    need the measured branch; ``None`` (a row with no duration) simply takes the
    ``loudnorm`` one.
    """
    raw = getattr(call, "duration_s", None)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):  # pragma: no cover - the column is numeric
        return None


@dataclass(slots=True)
class QueuedCall:
    """One encoded call waiting its turn on the stream."""

    call_id: int
    frames: list[Frame]
    started_at: datetime | None = None
    index: int = 0

    @property
    def remaining_s(self) -> float:
        return sum(frame.duration_s for frame in self.frames[self.index :])

    @property
    def done(self) -> bool:
        return self.index >= len(self.frames)

    def next_frame(self) -> Frame | None:
        if self.done:
            return None
        frame = self.frames[self.index]
        self.index += 1
        return frame


# --------------------------------------------------------------------------------------
# the feed itself
# --------------------------------------------------------------------------------------


class BroadcastifyFeed:
    """The source client: a loader thread that fills a queue, a sender that paces it.

    They are separate threads for the same reason the alert evaluator is separate from the
    LISTEN connection: encoding a call takes an unbounded fraction of a second, and doing
    it on the thread that owes Icecast a frame every 26 ms would show up on the stream as a
    gap.  The loader may block on ffmpeg for as long as it likes; the sender only ever
    takes a frame off a deque.
    """

    def __init__(
        self,
        config: FeedConfig,
        *,
        settings: Settings,
        session_factory: Callable[[], Session],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        source_factory: Callable[[FeedConfig], IcecastSource] | None = None,
        transcode: Callable[..., bytes] = transcode_to_mp3,
        silence: Callable[..., bytes] = mp3_silence,
    ) -> None:
        self.config = config
        self.settings = settings
        self.session_factory = session_factory
        self._clock = clock
        self._sleep = sleep
        self._source_factory = source_factory or (lambda config: IcecastSource(config))
        self._transcode = transcode
        self._silence = silence

        self._lock = threading.Lock()
        self._queue: deque[QueuedCall] = deque()
        self._playing: QueuedCall | None = None
        self._silence_frames: list[Frame] = []
        self._silence_index = 0
        #: The earliest ``started_at`` this run of the feed will ever look at: the enable
        #: watermark, or the backlog bound when the feed connected later than that.  Fixed
        #: on the first pass; the per-pass age bound only ever moves the window later.
        self._floor: datetime | None = None
        #: Every call this run has already considered (queued *or* refused), by id, with its
        #: ``started_at`` so an entry can be forgotten once the age bound has passed it.
        #: This is the loader's cursor — see :meth:`poll_once` for why it is a set.
        self._handled: dict[int, datetime] = {}
        self._source: IcecastSource | None = None
        self._status = FeedStatus()
        self._status_at = 0.0
        self._failures = 0
        #: when the next frame is owed to the socket, on ``clock()``'s scale
        self._due = 0.0

    # -- status ------------------------------------------------------------------------

    def status(self) -> FeedStatus:
        with self._lock:
            return replace(self._status)

    def _note(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self._status, key, value)

    def _flush_status(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._status_at < STATUS_INTERVAL_S:
            return
        self._status_at = now
        with self._lock:
            self._status.queued_s = round(
                sum(item.remaining_s for item in self._queue)
                + (self._playing.remaining_s if self._playing else 0.0),
                1,
            )
            snapshot = replace(self._status)
        session = self.session_factory()
        try:
            write_status(session, snapshot)
        finally:
            session.close()

    def _fail(self, message: str) -> None:
        log.warning("broadcastify feed: %s", message)
        self._note(
            connected=False,
            since=None,
            last_error=message[:500],
            last_error_at=datetime.now(UTC).isoformat(),
        )
        self._flush_status(force=True)

    # -- the queue ---------------------------------------------------------------------

    def enqueue(self, item: QueuedCall) -> None:
        """Add one encoded call and trim the queue back to the backlog bound.

        Trimming is from the **oldest** end, and it is the whole point: a feed that plays a
        five-minute backlog is not a live feed, it is a delayed one, and a listener who
        wanted delayed audio would be using the Calls archive.
        """
        dropped = 0
        with self._lock:
            self._queue.append(item)
            bound = max(0.0, float(self.config.max_backlog_s))
            if bound > 0:
                total = sum(entry.remaining_s for entry in self._queue)
                # Never drop the item just added: at a bound shorter than one call, the
                # newest call is still what a listener should be hearing.
                while total > bound and len(self._queue) > 1:
                    oldest = self._queue.popleft()
                    total -= oldest.remaining_s
                    dropped += 1
            self._status.dropped += dropped
        if dropped:
            log.info("broadcastify feed: dropped %d queued call(s) to stay near live", dropped)

    def _next_frame(self) -> Frame | None:
        """The next frame to send: the playing call's, the next call's, or silence."""
        with self._lock:
            playing = self._playing
            if playing is None or playing.done:
                if playing is not None and playing.done:
                    self._status.calls_played += 1
                playing = self._queue.popleft() if self._queue else None
                self._playing = playing
            if playing is not None:
                frame = playing.next_frame()
                if frame is not None:
                    return frame
                self._playing = None
        return self._silence_frame()

    def _silence_frame(self) -> Frame | None:
        if not self.config.silence:
            return None
        if not self._silence_frames:
            try:
                raw = self._silence(
                    seconds=1.0,
                    bitrate_kbps=self.config.bitrate_kbps,
                    sample_rate=self.config.sample_rate,
                )
            except TranscodeError as exc:
                log.error("broadcastify feed: no silence to send (%s)", exc)  # noqa: TRY400
                return None
            self._silence_frames = list(iter_frames(raw))
            self._silence_index = 0
            if not self._silence_frames:  # pragma: no cover - ffmpeg produced no frames
                log.error("broadcastify feed: the generated silence held no MP3 frames")
                return None
        frame = self._silence_frames[self._silence_index % len(self._silence_frames)]
        self._silence_index += 1
        return frame

    # -- the loader --------------------------------------------------------------------

    def _load_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - the loader outlives any one pass
                log.exception("broadcastify feed: a discovery pass failed")
            stop.wait(POLL_INTERVAL_S)

    def poll_once(self) -> int:
        """Pick up the calls recorded since the last pass and queue the ones that qualify.

        **The cursor is the set of calls already handled, not a timestamp.**  It used to be
        the highest ``started_at`` seen, but a row is inserted when the call *ends*: a 30 s
        dispatch call that keyed up at t0 lands after a 2 s call that keyed up at t0+5, by
        which time the cursor had passed t0 and the long call — exactly the traffic a
        listener most wants — was skipped for ever.  Ties split by ``LIMIT`` went the same
        way.  So each pass reads, in insert order (``calls.id``), every call that started
        inside the age bound and has not been handled yet; the handled ids are forgotten as
        the bound passes them, so the set never holds more than one backlog window of calls.
        A set rather than a high-water ``id`` because ids are handed out at INSERT and
        become visible at COMMIT, and two recorders uploading at once commit out of order.
        """
        session = self.session_factory()
        try:
            settings = effective_settings(session, base=self.settings)
            mark = watermark(session, FEED_SERVICE)
            if mark is None:
                # Not enabled (or the switch was just turned off): the guard says publish
                # nothing, and the supervisor is about to stop this feed anyway.
                return 0
            now = datetime.now(UTC)
            bound = max(5.0, float(feed_config(settings).max_backlog_s))
            if self._floor is None:
                # Start from the watermark, but never further back than the backlog bound:
                # a feed that connects an hour after being enabled starts with what is live
                # now, not with the hour it missed.
                self._floor = max(mark, now - timedelta(seconds=bound))
            # The age bound, in SQL: anything that started before it would be refused by
            # ``_accept`` anyway, and it only gets older.
            lower = max(self._floor, mark, now - timedelta(seconds=bound))
            self._handled = {cid: at for cid, at in self._handled.items() if at > lower}
            stmt = (
                select(Call)
                .options(*FEED_POLL_DEFERRED)
                .where(
                    Call.started_at > lower,
                    Call.duplicate_of.is_(None),
                    live_band_clause(),
                )
                .order_by(Call.id)
                .limit(POLL_LIMIT)
            )
            if self._handled:
                stmt = stmt.where(Call.id.not_in(list(self._handled)))
            # Unit-to-unit calls, when the operator has turned them off for the feed.
            # ``_accept`` would refuse them anyway (``excluded_reason`` answers
            # ``unit_call``); doing it in SQL spends no ffmpeg finding that out.
            private = unit_calls_clause(settings, FEED_SERVICE)
            if private is not None:
                stmt = stmt.where(private)
            rows = session.scalars(stmt).all()
            queued = 0
            for call in rows:
                started = call.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                self._handled[int(call.id)] = started if started is not None else now
                if self._accept(session, call, settings, now=now, bound=bound):
                    queued += 1
            return queued
        finally:
            session.close()

    def _accept(
        self, session: Session, call: Call, settings: Settings, *, now: datetime, bound: float
    ) -> bool:
        """Encode and queue one call, unless something says not to."""
        started = call.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if started is not None and (now - started).total_seconds() > bound:
            # Older than the backlog bound before it was even encoded: it would be dropped
            # by the trim the moment it was queued, so spend no ffmpeg on it.
            return False
        # ``no_speech=False``: the Feed is the channel as it sounded, and a mic key-up with
        # nothing behind it is part of that.  The *Calls* exporters refuse one
        # (:data:`~app.exporters.ledger.NO_SPEECH_REASON`) because a call archive is a list
        # of things somebody said; a live feed is not.
        reason = skip_reason_for(call, settings, no_speech=False)
        if reason is not None:
            return False
        deselected = excluded_reason(session, call, settings, SELECTION_SERVICE)
        if deselected is not None:
            log.debug("broadcastify feed: call %s not sent (%s)", call.id, deselected)
            return False
        try:
            path, processed = resolve_playback(call, settings)
        except AudioUnavailable:
            return False
        try:
            # The processed copy when there is one — a listener on the feed gets the same
            # trimmed, tone-tamed audio a listener here does, and it already carries its own
            # loudness normalization, so the §5.2 filter is applied only to an original.
            #
            # The export level goes on top of whichever source this is, per call and
            # before the encode: the feed is a bitstream of calls concatenated frame by
            # frame, so there is no later point at which a gain could be applied to the
            # stream as a whole (and a stream-wide one would ride over the silence between
            # calls).  ``export_filter`` composes with the §5.2 filter above rather than
            # replacing it, and is a no-op when ``export_target_lufs`` is 0.
            raw = self._transcode(
                path,
                af=export_filter(
                    None if processed or not _export_normalize(settings) else ffmpeg_audio_filter(settings),
                    settings,
                    path=path,
                    duration_s=_duration_of(call),
                    # …and the tone envelope back on after the lift, for the same reason the
                    # Calls exporters need it: the loudness stage measures an all-tone call
                    # as a quiet one and lifts the tone we just turned down.
                    audio_meta=call.audio_meta,
                    processed=processed,
                ),
                bitrate_kbps=self.config.bitrate_kbps,
                sample_rate=self.config.sample_rate,
                timeout_s=float(getattr(settings, "export_timeout_s", 30.0) or 30.0) * 4,
            )
        except (TranscodeError, OSError) as exc:
            log.warning("broadcastify feed: could not encode call %s (%s)", call.id, exc)
            return False
        frames = list(iter_frames(raw))
        if not frames:
            log.warning("broadcastify feed: call %s encoded to no MP3 frames", call.id)
            return False
        self.enqueue(QueuedCall(call_id=int(call.id), frames=frames, started_at=started))
        return True

    # -- the sender --------------------------------------------------------------------

    def _connect(self) -> bool:
        source = self._source_factory(self.config)
        try:
            source.connect()
        except FeedError as exc:
            self._source = None
            self._failures += 1
            self._fail(str(exc))
            return False
        self._source = source
        self._failures = 0
        self._note(connected=True, since=datetime.now(UTC).isoformat(), last_error=None)
        self._flush_status(force=True)
        return True

    def backoff_s(self) -> float:
        return min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, self._failures - 1)))

    def send_pass(self, stop: threading.Event) -> None:
        """One turn of the sender: connect if need be, then send whatever is due."""
        if self._source is None or not self._source.connected:
            if not self._connect():
                stop.wait(self.backoff_s())
                return
            # A fresh connection owes its first frame now, not whenever the last one did.
            self._due = self._clock()
        now = self._clock()
        due = self._due
        if now < due:
            self._sleep(min(due - now, 0.25))
            return
        frame = self._next_frame()
        if frame is None:
            # Nothing to play and silence turned off.  Stay in step with the wall clock so
            # that the next call starts now rather than replaying the quiet time.
            self._due = now
            self._sleep(0.05)
            return
        try:
            self._source.send(frame.data)
        except FeedError as exc:
            self._source = None
            self._failures += 1
            self._fail(str(exc))
            return
        self._note(bytes_sent=self._source.bytes_sent)
        self._due = due + frame.duration_s
        if self._due < now - MAX_DRIFT_S:
            self._due = now
        self._flush_status()

    def run(self, stop: threading.Event) -> None:
        """Stream until ``stop`` is set.  Owns the loader thread for its lifetime."""
        loader = threading.Thread(target=self._load_loop, args=(stop,), name="bcfy-feed-load", daemon=True)
        loader.start()
        try:
            while not stop.is_set():
                try:
                    self.send_pass(stop)
                except Exception:  # noqa: BLE001 - the stream outlives any one frame
                    log.exception("broadcastify feed: the sender failed")
                    stop.wait(1.0)
        finally:
            if self._source is not None:
                self._source.close()
            self._source = None
            self._note(connected=False, since=None)
            self._flush_status(force=True)
            loader.join(timeout=5.0)


# --------------------------------------------------------------------------------------
# the supervisor the exporter worker drives
# --------------------------------------------------------------------------------------


class FeedSupervisor:
    """Starts and stops one :class:`BroadcastifyFeed` as the runtime setting changes.

    The exporter worker calls :meth:`reconcile` once a cycle with the settings it has just
    read, so turning the feed on, off, or changing the mount takes effect within one poll
    interval rather than at the next deploy — the same contract the two upload switches
    have.
    """

    def __init__(self, *, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._feed: BroadcastifyFeed | None = None
        self._fingerprint: tuple | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def feed(self) -> BroadcastifyFeed | None:
        return self._feed

    def reconcile(self, settings: Settings, *, enabled: bool) -> None:
        config = feed_config(settings)
        want = bool(enabled) and config.configured()
        if not want:
            self.shutdown()
            return
        if self.running and self._fingerprint == config.fingerprint():
            if self._feed is not None:
                # The knobs that do not need a reconnect are picked up in place.
                self._feed.config = config
            return
        self.shutdown()
        self._start(config, settings)

    def _start(self, config: FeedConfig, settings: Settings) -> None:
        feed = BroadcastifyFeed(config, settings=settings, session_factory=self.session_factory)
        stop = threading.Event()
        thread = threading.Thread(target=feed.run, args=(stop,), name="bcfy-feed", daemon=True)
        thread.start()
        self._feed, self._stop, self._thread = feed, stop, thread
        self._fingerprint = config.fingerprint()
        log.info(
            "broadcastify feed: streaming to %s%s at %d kbit/s",
            config.server,
            config.mount_path,
            config.bitrate_kbps,
        )

    def shutdown(self) -> None:
        """Stop the stream if one is running.  Safe to call when none is."""
        stop, thread = self._stop, self._thread
        self._stop = self._thread = self._feed = None
        self._fingerprint = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)


def clear_status(session: Session) -> None:
    """Forget the last status — the feed is off and nothing is streaming."""
    row = session.get(Setting, STATUS_KEY)
    if row is not None and isinstance(row.value, dict) and row.value.get("connected"):
        row.value = FeedStatus().payload()
        session.commit()
