"""Is this host's clock good enough to upload calls with?

Step 1 of the Broadcastify Calls upload workflow, before recording, before encoding and
before registering anything:

    "Verify time synchronization on the host system. NTP accuracy is required; calls with
    bad timestamps are rejected or deduplicated incorrectly."

That is not decoration.  ``ts`` is the call's **start** in whole epoch seconds, and it is
part of the tuple Broadcastify deduplicates on (system, talkgroup, timestamp).  A host
whose clock has drifted publishes every call at the wrong moment: they interleave wrongly
with the other uploaders on the same system, the ``SKIPPED---ALREADY-RECEIVED-THIS-CALL``
answer stops recognizing genuine duplicates, and there is nothing to fix afterwards
because the archive keeps whatever timestamp it was told.  So the offset is *measured*,
shown next to the service, and — past :data:`PAUSE_OFFSET_S` — the Calls uploader stops
until it recovers.

Two ways of measuring it, in this order:

``ntp``
    An SNTP query to :attr:`~app.config.Settings.export_ntp_server` (the deployment's own
    NTP server by default).  One 48-byte UDP datagram, stdlib :mod:`socket`, no
    dependency, and it is the real measurement: NTP timestamps are 32.32 fixed point, so
    the answer is good to well under a millisecond, and the round trip is halved out.

``http``
    The ``Date`` header of a response we were already making — the credential test, or a
    sampled stage-1 registration.  A fallback for when the NTP server is blank or
    unreachable, and a coarse one: HTTP dates have one-second resolution, so this can only
    ever say "within a second or two", which is exactly the granularity the ±2 s / ±5 s
    thresholds below are stated at.

Neither is allowed to fail loudly.  A clock probe that raised would take an upload with it,
and "we could not measure the clock" is not the same claim as "the clock is wrong": both
paths return ``None`` and the caller reports ``clock_ok`` as ``None`` — unknown — rather
than as a failure.  Only a *measured* offset past the threshold pauses anything.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime

log = logging.getLogger("app.exporters.clock")

__all__ = [
    "OK_OFFSET_S",
    "PAUSE_OFFSET_S",
    "ClockCheck",
    "check_clock",
    "http_offset",
    "ntp_offset",
    "sample_response",
    "sampled",
]

#: Seconds between 1900-01-01 (the NTP epoch) and 1970-01-01 (the Unix one).
NTP_EPOCH_DELTA = 2_208_988_800

#: ``|offset|`` at or under this reads as **synchronized** (``clock_ok`` true).  Two
#: seconds is the coarsest the HTTP fallback can resolve, so a tighter bound would be a
#: number this module cannot honestly measure by both routes.
OK_OFFSET_S = 2.0

#: ``|offset|`` past this **pauses the Calls uploader**.  Deliberately wider than
#: :data:`OK_OFFSET_S`: between the two the status goes amber and an operator is told, but
#: uploads continue, because a second or three of drift mis-sorts a call and five seconds
#: starts breaking the dedupe tuple outright.
PAUSE_OFFSET_S = 5.0

#: How often the *worker* re-measures (the coordinator's ten minutes).  A test button
#: press always measures afresh.
WORKER_INTERVAL_S = 600.0

#: How often a stage-1 response may be sampled for its ``Date``.  This is the cheap path —
#: it costs nothing but parsing a header we already received — but there is no point
#: recomputing it on every call in a burst.
SAMPLE_INTERVAL_S = 60.0

#: The SNTP request: LI=0 (no warning), VN=4, Mode=3 (client) — 0b00_100_011.
_SNTP_FIRST_BYTE = 0x23
_SNTP_REQUEST = bytes([_SNTP_FIRST_BYTE]) + bytes(47)
_SNTP_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class ClockCheck:
    """One measurement of how far this host's clock is from a reference.

    ``offset_s`` is **host minus reference**: positive means this host is *ahead*, which is
    the direction that stamps a call in the future.  ``None`` everywhere means nothing
    could be measured, which is reported as unknown and never as a failure.
    """

    offset_s: float | None = None
    #: ``"ntp"``, ``"http"``, or ``None`` when neither route answered.
    source: str | None = None
    #: What was asked — the NTP server's address, or the URL whose ``Date`` was read.
    reference: str = ""
    #: Why there is no offset, when there is none.
    detail: str = ""

    @property
    def ok(self) -> bool | None:
        """``True`` / ``False`` when measured, ``None`` when it could not be measured."""
        if self.offset_s is None:
            return None
        return abs(self.offset_s) <= OK_OFFSET_S

    @property
    def should_pause(self) -> bool:
        """Is the drift bad enough to stop uploading?  An unknown offset never pauses."""
        return self.offset_s is not None and abs(self.offset_s) > PAUSE_OFFSET_S

    def as_dict(self) -> dict[str, object]:
        """The three fields that go in the status block and in the test result."""
        return {
            "clock_offset_s": self.offset_s,
            "clock_ok": self.ok,
            "clock_source": self.source,
        }


def _ntp_seconds(raw: bytes) -> float:
    """One 64-bit NTP timestamp (32.32 fixed point, seconds since 1900) as Unix seconds."""
    whole, fraction = struct.unpack("!II", raw)
    return (whole - NTP_EPOCH_DELTA) + (fraction / 2**32)


def ntp_offset(server: str, *, timeout_s: float = _SNTP_TIMEOUT_S) -> ClockCheck:
    """One SNTP round trip.  ``host - reference`` in seconds, or a ``ClockCheck`` saying why not.

    The four timestamps are the classic ones: ``t1`` when we sent, ``t2``/``t3`` the
    server's receive and transmit stamps out of the packet, ``t4`` when we got it back.
    NTP's offset is ``((t2-t1) + (t3-t4)) / 2`` — server minus client — and this module
    reports the other sign, so it is negated once, here, rather than everywhere it is read.

    Every failure is swallowed into a ``ClockCheck`` with no offset.  A missing NTP server
    must never be able to stop an upload; it only stops the *claim* that the clock is good.
    """
    address = (server or "").strip()
    if not address:
        return ClockCheck(detail="no NTP server configured")
    host, _, port_text = address.partition(":")
    try:
        port = int(port_text) if port_text else 123
    except ValueError:
        return ClockCheck(reference=address, detail=f"unreadable NTP port in {address!r}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(max(0.1, float(timeout_s)))
        t1 = time.time()
        sock.sendto(_SNTP_REQUEST, (host, port))
        packet, _ = sock.recvfrom(1024)
        t4 = time.time()
    except OSError as exc:  # socket.timeout is one of these
        return ClockCheck(reference=address, detail=f"NTP query to {address} failed: {exc}")
    finally:
        sock.close()
    if len(packet) < 48:
        return ClockCheck(reference=address, detail=f"short NTP reply ({len(packet)} bytes)")
    t2 = _ntp_seconds(packet[32:40])
    t3 = _ntp_seconds(packet[40:48])
    if t3 <= 0:
        # A zero transmit timestamp is a Kiss-o'-Death or an unsynchronized server; it is
        # not a measurement and must not be read as one.
        return ClockCheck(reference=address, detail=f"{address} answered with no transmit timestamp")
    server_minus_client = ((t2 - t1) + (t3 - t4)) / 2
    return ClockCheck(offset_s=-server_minus_client, source="ntp", reference=address)


def http_offset(date_header: str | None, *, sent_at: float, received_at: float, url: str = "") -> ClockCheck:
    """The ``Date`` of a response we already have — the fallback route.

    ``sent_at``/``received_at`` bracket the request, and the host's own time is taken as
    their midpoint so half the round trip is not charged to the offset.  HTTP dates carry
    whole seconds only, so the answer is good to about a second and no better, which is
    why :data:`OK_OFFSET_S` is stated at two.
    """
    if not date_header:
        return ClockCheck(reference=url, detail="the response carried no Date header")
    try:
        moment = parsedate_to_datetime(date_header)
    except (TypeError, ValueError) as exc:
        return ClockCheck(reference=url, detail=f"unreadable Date header {date_header!r}: {exc}")
    if moment is None:  # pragma: no cover - parsedate_to_datetime raises on modern Pythons
        return ClockCheck(reference=url, detail=f"unreadable Date header {date_header!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    midpoint = (float(sent_at) + float(received_at)) / 2
    return ClockCheck(offset_s=midpoint - moment.timestamp(), source="http", reference=url)


def check_clock(
    settings: object,
    *,
    date_header: str | None = None,
    sent_at: float | None = None,
    received_at: float | None = None,
    url: str = "",
) -> ClockCheck:
    """NTP first, the ``Date`` header second — the order the coordinator asked for.

    The HTTP arguments are optional because most callers have no response to hand: they
    get the NTP answer or an unmeasured one.  Whichever route answers is recorded in
    ``source`` so a reader can tell a sub-millisecond NTP measurement from a
    second-resolution header guess.
    """
    server = str(getattr(settings, "export_ntp_server", "") or "")
    check = ntp_offset(server)
    if check.offset_s is not None:
        return check
    if date_header is not None and sent_at is not None and received_at is not None:
        fallback = http_offset(date_header, sent_at=sent_at, received_at=received_at, url=url)
        if fallback.offset_s is not None:
            return fallback
        return ClockCheck(reference=fallback.reference, detail=f"{check.detail}; {fallback.detail}")
    return check


# --------------------------------------------------------------------------------------
# the sampled cache
# --------------------------------------------------------------------------------------
#
# The worker re-measures every WORKER_INTERVAL_S and stage-1 responses are sampled at most
# once every SAMPLE_INTERVAL_S; both write here, and the status endpoint — which runs in a
# different process from the worker and has no business opening a UDP socket on a page
# render — reads it.  Module state rather than a table: it is a measurement of *this*
# process's clock, and a stale value from another host would be worse than none.

_lock = threading.Lock()
_last: ClockCheck | None = None
_last_at: float = 0.0
_sampled_at: float = 0.0


def _store(check: ClockCheck) -> ClockCheck:
    global _last, _last_at
    with _lock:
        _last = check
        _last_at = time.monotonic()
    return check


def sampled(settings: object, *, interval_s: float = WORKER_INTERVAL_S, force: bool = False) -> ClockCheck:
    """The cached measurement, re-measuring when it is older than ``interval_s``.

    ``force`` is the test button: an operator pressing it wants the clock as it is now, not
    as it was nine minutes ago.
    """
    with _lock:
        cached, age = _last, time.monotonic() - _last_at
    if cached is not None and not force and age < max(1.0, float(interval_s)):
        return cached
    return _store(check_clock(settings))


def sample_response(
    settings: object, *, date_header: str | None, sent_at: float, received_at: float, url: str = ""
) -> None:
    """Cheaply fold a response we already made into the cache, at most once a minute.

    Called from stage 1, where the ``Date`` header costs nothing.  It never opens a socket
    and never blocks the upload: a header that will not parse simply leaves the previous
    measurement in place.
    """
    now = time.monotonic()
    global _sampled_at
    with _lock:
        if now - _sampled_at < SAMPLE_INTERVAL_S:
            return
        _sampled_at = now
    check = http_offset(date_header, sent_at=sent_at, received_at=received_at, url=url)
    if check.offset_s is not None:
        _store(check)


def reset() -> None:
    """Drop the cache — the tests' only way to make a measurement repeatable."""
    global _last, _last_at, _sampled_at
    with _lock:
        _last = None
        _last_at = 0.0
        _sampled_at = 0.0
