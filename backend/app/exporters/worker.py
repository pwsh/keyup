"""The exporter worker: one loop per service, driven by the runtime switches.

:meth:`ExportWorker.run` gives each call-export service a thread of its own (and keeps the
feed's reconcile in the caller's); :meth:`ExportWorker.cycle` is the same work in one
sequential pass, which is what the CLI and the tests drive.

Structurally this is the transcription worker's smaller sibling — a claim loop over a
ledger table (``app/stt/worker.py``) rather than a scheduled job — but with two rules the
transcription worker does not have, both of which exist because this one talks to somebody
else's public service:

There are three services here, not two: Broadcastify **Calls** and OpenMHz are per-call
uploads driven by the ledger below, and Broadcastify **Feed** is a live Icecast stream that
runs as its own thread beside this loop (:mod:`app.exporters.broadcastify_feed`).  All
three obey the same two rules.

**Nothing runs unless it is switched on.**  Each cycle re-reads
:func:`app.runtime_settings.effective_settings`, so ``export_broadcastify_enabled`` and
``export_openmhz_enabled`` take effect on the next cycle, not the next deploy.  Both ship
``false``.  A service that is off is not merely skipped: its watermark is cleared, so it
cannot accumulate a queue while disabled and cannot flush one on being enabled.

**Nothing that is not live is ever uploaded.**  :func:`app.exporters.gate.exportable` is
checked twice — once as SQL in :func:`~app.exporters.ledger.discover`, and again in Python
on the row the worker just claimed, immediately before the bytes go out.  The second check
is not redundant: a ledger row can outlive a watermark reset by the length of one cycle,
and the answer has to be "do not upload it".

**Broadcastify Calls will not upload with a bad clock.**  Step 1 of their workflow is
"verify time synchronization on the host system", because ``ts`` is part of the tuple they
deduplicate on.  :meth:`ExportWorker._clock_pauses` measures the offset every ten minutes
(:mod:`app.exporters.clock`) and stops that one service — not the feed, not OpenMHz — while
it is more than five seconds out.  An offset that could not be *measured* never pauses
anything.

Everything here is defensive.  A failing exporter must never touch ingest, transcription or
playback: every upload is wrapped, every failure lands in the ledger and the activity log,
and the loop keeps going.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import activity
from app.audio import AudioUnavailable, resolve_playback
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import CALL_EXPORT_SERVICES
from app.runtime_settings import effective_settings
from app.stt.normalize import ffmpeg_audio_filter

from . import claims, clock, ledger
from .base import (
    AmbiguousExportError,
    ExportClient,
    ExportError,
    PermanentExportError,
    SkipCall,
    UploadResult,
    facts_for,
)
from .broadcastify import (
    DUPLICATE_REASON,
    LATE_REASON,
    BroadcastifyClient,
    encoding_for,
    mp3_bitrate_kbps,
    remote_id_for,
)
from .broadcastify import SERVICE as BROADCASTIFY_SERVICE
from .broadcastify_feed import FEED_SERVICE, FeedSupervisor, clear_status
from .gate import excluded_reason, exportable, sync_watermark
from .gate import export_normalize as _export_normalize
from .loudness import export_filter
from .openmhz import OpenMhzClient
from .transcode import (
    CALLS_MP3_SAMPLE_RATE,
    TranscodeError,
    transcode_to_m4a,
    transcode_to_mp3,
)

log = logging.getLogger("app.exporters.worker")

__all__ = ["ExportWorker", "build_clients", "late_reason", "run_export_worker"]

SessionFactory = Callable[[], Session]


def build_clients(settings: Settings) -> list[ExportClient]:
    """One client per known service, in ledger order.  Configuration is checked later."""
    return [BroadcastifyClient(settings), OpenMhzClient(settings)]


def enabled_for(settings: Settings, service: str) -> bool:
    """The runtime switch for one service — ``False`` for anything unrecognized."""
    return bool(getattr(settings, f"export_{service}_enabled", False))


def _splits_stages(client: ExportClient) -> bool:
    """Can this client register and PUT separately, or only :meth:`upload` as one step?

    :class:`~app.exporters.broadcastify.BroadcastifyClient` can; the protocol contract in
    :class:`~app.exporters.base.ExportClient` asks only for ``upload``, so a stand-in that
    implements the contract and nothing more still works — it simply pays for the encode
    up front, as everything did before.
    """
    return callable(getattr(client, "register", None)) and callable(
        getattr(client, "put_audio", None)
    )


def call_end(call) -> datetime | None:  # noqa: ANN001 - app.models.Call
    """When the transmission *ended*, in UTC — ``ended_at``, else start plus duration.

    Broadcastify measures the skew from the end of the call rather than its start
    (:data:`~app.exporters.broadcastify.LATE_REASON`), so this is the instant the budget
    below is counted from.  ``None`` when the row carries neither.
    """
    end = getattr(call, "ended_at", None)
    if end is None:
        started = getattr(call, "started_at", None)
        if started is None:  # pragma: no cover - started_at is NOT NULL
            return None
        end = started + timedelta(seconds=float(getattr(call, "duration_s", 0) or 0))
    return end if end.tzinfo is not None else end.replace(tzinfo=UTC)


#: What is kept under ``broadcastify_skew_limit_s`` for the registration's own round trip:
#: a call measured at 14.6 s here arrives at 15.1 s there, and the whole point of the
#: pre-check is to be right about the answer without asking.
SKEW_MARGIN_S = 1.0


def late_reason(call, settings: Settings) -> str | None:  # noqa: ANN001 - app.models.Call
    """The skip reason for a call already past Broadcastify's skew window, or ``None``.

    A limit of 0 (or less) turns the check off entirely and lets Broadcastify answer for
    itself, which is the honest behaviour when an operator has decided not to guess.
    """
    limit = float(getattr(settings, "broadcastify_skew_limit_s", 0.0) or 0.0)
    if limit <= 0:
        return None
    end = call_end(call)
    if end is None:  # pragma: no cover - defensive
        return None
    age = (datetime.now(UTC) - end).total_seconds()
    if age <= limit - SKEW_MARGIN_S:
        return None
    return f"{LATE_REASON} ({age:.1f} s after call end)"


def _skew_window_on(settings: Settings) -> bool:
    """Is Broadcastify's skew pre-check on?  (``broadcastify_skew_limit_s`` > 0.)"""
    return float(getattr(settings, "broadcastify_skew_limit_s", 0.0) or 0.0) > 0


#: The pauses before each re-try of a success write that failed (:meth:`ExportWorker._record_success`).
OUTCOME_RETRY_DELAYS_S = (0.25, 1.0, 3.0)


@dataclass(frozen=True)
class _Success:
    """One upload that went out, as it is written to the ledger and the activity log."""

    service: str
    result: UploadResult
    #: keyup's Broadcastify claim for the call, closed with ``uploaded`` alongside the row
    claim_id: int | None
    meta: dict = field(default_factory=dict)


class ExportWorker:
    """Owns the cycle.  Construct it with a session factory; call :meth:`cycle` or :meth:`run`."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory = SessionLocal,
        clients: Sequence[ExportClient] | None = None,
        audio_root: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.clients = list(clients) if clients is not None else build_clients(self.settings)
        # Injected clients (the tests') are kept as they are; the real ones are rebuilt
        # from the runtime settings every cycle, because a client carries the settings it
        # was built with and an API key typed in after start-up must not wait for a restart.
        self._rebuild_clients = clients is None
        self._unconfigured_noted: set[str] = set()
        #: :meth:`run` gives each call-export service a thread of its own, so the two small
        #: pieces of per-service state below are touched from more than one of them.  The
        #: keys never collide — a service only ever reads and writes its own — but a
        #: ``set`` and a ``dict`` being mutated concurrently is not something to leave to
        #: the GIL's current implementation details.
        self._state_lock = threading.Lock()
        self.audio_root = Path(audio_root) if audio_root is not None else None
        self._sleep = sleep
        self._last_upload: dict[str, float] = {}
        #: Uploads that went out but whose ``uploaded`` could not be written, by ledger row
        #: id.  The row is still ``pending``; when it next comes up it is recorded from here
        #: instead of being sent a second time (:meth:`_record_success`).
        self._unrecorded: dict[int, _Success] = {}
        #: Whether the Calls uploader is currently paused on a bad clock.  A transition
        #: flag, not a state machine: it exists so the WARNING is logged once per outage
        #: rather than once per cycle (:meth:`_clock_pauses`).
        self._clock_paused = False
        #: The Broadcastify **Feed** — a live Icecast source rather than a queue of
        #: uploads, so it runs as its own thread beside this loop rather than inside it.
        #: The supervisor is created either way and starts nothing until the switch is on.
        self.feed = FeedSupervisor(session_factory=self.session_factory)

    # ------------------------------------------------------------------ settings

    def runtime(self, session: Session) -> Settings:
        """This worker's settings with the ``settings`` table layered on top.

        Deliberately **raises** rather than falling back to the process settings.  Falling
        back looks harmless — the process defaults have both switches off, so the failure
        direction is "export nothing" — but a cycle that reads both services as disabled
        goes on to *clear their enable watermarks*.  The next healthy cycle then re-stamps
        the watermark at "now", and every call recorded during the hiccup is silently
        skipped for ever.  :meth:`cycle` therefore abandons the whole cycle instead.
        """
        return effective_settings(session, base=self.settings)

    def _where(self, settings: Settings) -> Settings:
        """Settings with this worker's audio-root override applied (the tests use it)."""
        if self.audio_root is None:
            return settings
        return settings.model_copy(update={"audio_root": self.audio_root})

    def _throttle(self, service: str, settings: Settings) -> None:
        """Keep at least ``EXPORT_MIN_INTERVAL_S`` between two uploads to one service."""
        gap = float(getattr(settings, "export_min_interval_s", 0.0) or 0.0)
        if gap <= 0:
            return
        with self._state_lock:
            last = self._last_upload.get(service)
        if last is not None:
            wait = gap - (time.monotonic() - last)
            if wait > 0:
                # Outside the lock: this sleeps for up to ``gap`` seconds and the other
                # service's thread must not wait on it.
                self._sleep(wait)
        with self._state_lock:
            self._last_upload[service] = time.monotonic()

    # ------------------------------------------------------------------ one cycle

    def cycle(self) -> dict[str, int]:
        """Discover and upload for every enabled, configured service.  Returns per-service counts.

        Never raises.  A service that blows up is logged and the next one still runs.
        """
        counts: dict[str, int] = {}
        session = self.session_factory()
        try:
            settings = self.runtime(session)
        except Exception:  # noqa: BLE001 - the database is somebody else's problem
            # Skip everything: no watermark sync, no discovery, no uploads.  Doing nothing
            # for one cycle costs at most EXPORT_POLL_SECONDS of latency; guessing that the
            # switches are off would reset the enable watermarks and drop the gap's calls.
            log.exception(
                "export: could not read the settings table; skipping this cycle entirely "
                "(watermarks left untouched)"
            )
            session.rollback()
            session.close()
            return counts
        try:
            try:
                self._run_feed(session, settings)
            except Exception:  # noqa: BLE001 - the feed must not stop the uploaders
                log.exception("export: the broadcastify feed cycle failed")
                session.rollback()
            if self._rebuild_clients:
                self.clients = build_clients(settings)
            for client in self.clients:
                service = client.service
                if service not in CALL_EXPORT_SERVICES:  # pragma: no cover - defensive
                    continue
                try:
                    counts[service] = self._run_service(session, client, settings)
                except Exception:  # noqa: BLE001 - one service must not stop the others
                    log.exception("export: the %s cycle failed", service)
                    session.rollback()
        finally:
            session.close()
        return counts

    def _run_feed(self, session: Session, settings: Settings) -> None:
        """Reconcile the Broadcastify Feed with its switch — watermark first, then the thread.

        The order is the same as :meth:`_run_service`'s and for the same reason: the
        watermark is the guard, so turning the feed off has to clear the clock before
        anything could read it, and turning it on has to stamp the clock before the loader
        thread's first pass can see a call.
        """
        enabled = bool(getattr(settings, f"export_{FEED_SERVICE}_enabled", False))
        sync_watermark(session, FEED_SERVICE, enabled=enabled)
        session.commit()
        if not enabled:
            clear_status(session)
        self.feed.reconcile(settings, enabled=enabled)

    def _run_service(self, session: Session, client: ExportClient, settings: Settings) -> int:
        service = client.service
        enabled = enabled_for(settings, service)
        # Reconciling the watermark is the *only* thing that happens for a disabled
        # service, and it happens first: turning one off has to clear the clock before
        # anything else in this cycle could read it.
        mark = sync_watermark(session, service, enabled=enabled)
        session.commit()
        if not enabled:
            return 0
        if not client.configured():
            with self._state_lock:
                first = service not in self._unconfigured_noted
                self._unconfigured_noted.add(service)
            if first:
                log.warning(
                    "export: %s is switched on but has no credentials; nothing is uploaded "
                    "until they are entered on the Exports page",
                    service,
                )
            return 0
        with self._state_lock:
            self._unconfigured_noted.discard(service)
        if service == BROADCASTIFY_SERVICE and self._clock_pauses(settings):
            return 0

        ledger.discover(session, service=service, watermark=mark, settings=settings, limit=500)

        uploaded = 0
        batch = max(1, int(getattr(settings, "export_batch_size", 20) or 20))
        # Every row this pass has already handled, whatever became of it.  A row whose
        # outcome could not be written is rolled back to `pending` and still due, and
        # without this the loop would take it straight back — up to a batch of sends of
        # one call in one pass.
        handled: set[int] = set()
        for _ in range(batch):
            # Broadcastify Calls newest-first: see :func:`app.exporters.ledger.claim`.  A
            # backlog worked oldest-first spends every attempt on calls already past the
            # skew window while the fresh ones behind them go stale in turn.
            #
            # The claimed ledger row's FOR UPDATE lock is held from here until _handle
            # commits — across the encode and the whole HTTP upload, seconds — and _handle
            # then inserts an ``activity_events`` row whose ``call_id`` FK takes
            # ``FOR KEY SHARE`` on the call.  So no transaction anywhere may hold a lock
            # stronger than ``FOR NO KEY UPDATE`` on ``calls`` while it waits on
            # ``call_exports``: that is the cycle, and it is why :func:`app.db.lock_calls`
            # is ``FOR NO KEY UPDATE`` rather than ``FOR UPDATE``.
            claimed = ledger.claim(
                session,
                service=service,
                newest_first=service == BROADCASTIFY_SERVICE,
                exclude=handled,
            )
            if claimed is None:
                break
            handled.add(claimed.export_id)
            if self._handle(session, client, claimed, settings=settings, mark=mark):
                uploaded += 1
        return uploaded

    def _clock_pauses(self, settings: Settings) -> bool:
        """Step 1 of the Calls workflow: is this host's clock fit to stamp a ``ts``?

        "Verify time synchronization on the host system.  NTP accuracy is required; calls
        with bad timestamps are rejected or deduplicated incorrectly."  ``ts`` is part of
        the tuple Broadcastify deduplicates on, and a drifted clock therefore does damage
        that cannot be repaired from this end: the archive keeps whatever timestamp it was
        told, and the ``SKIPPED---ALREADY-RECEIVED-THIS-CALL`` answer stops recognizing the
        duplicates it exists to catch.

        So past :data:`app.exporters.clock.PAUSE_OFFSET_S` this returns ``True`` and the
        Calls cycle does nothing at all — no discovery, no claim, no upload.  Three things
        it deliberately does **not** do:

        * it does not touch the **feed** or **OpenMHz**.  A live Icecast stream carries no
          timestamps and OpenMHz's are its own problem; pausing them for Broadcastify's
          dedupe rule would be punishing two services for a third's requirement.
        * it does not clear the watermark, so recovery resumes from where it paused rather
          than skipping the outage.
        * it does not pause on an *unmeasured* clock.  "We could not reach the NTP server"
          is not "the clock is wrong", and a missing NTP server must never be able to stop
          uploading.

        Re-measured at most every :data:`app.exporters.clock.WORKER_INTERVAL_S`, so this
        costs one UDP round trip per ten minutes rather than one per cycle.
        """
        check = clock.sampled(settings)
        if not check.should_pause:
            if self._clock_paused:
                log.info(
                    "export: the host clock is back within tolerance (%+.3f s via %s); "
                    "resuming broadcastify calls uploads",
                    check.offset_s or 0.0,
                    check.source or "unknown",
                )
                self._clock_paused = False
            return False
        # Logged every cycle would be a wall of identical warnings; logged once is missed
        # by anyone who starts reading after it happened.  Once per transition, plus the
        # status block and the test button, which is where an operator looks.
        if not self._clock_paused:
            log.warning(
                "export: the host clock is %+.3f s out (measured via %s, %s); "
                "broadcastify calls uploads are paused until it is within %.0f s. "
                "Step 1 of the Calls workflow requires NTP accuracy: a drifted ts is "
                "deduplicated against the wrong (system, talkgroup, timestamp) tuple.",
                check.offset_s or 0.0,
                check.source or "unknown",
                check.reference or "no reference",
                clock.PAUSE_OFFSET_S,
            )
            self._clock_paused = True
        return True

    def _audio_for(self, service: str, path: Path, *, af: str | None, settings: Settings) -> bytes:
        """Step 3: encode one call the way *this* service wants it.

        OpenMHz takes the m4a it has always taken.  Broadcastify Calls takes whichever of
        the two formats its spec allows and ``broadcastify_encoding`` selects — AAC at
        32 kbit/s with an 18 kHz cutoff, or CBR mono MP3 at 22 050 Hz and 32 or 16 kbit/s —
        and the same choice drives the ``enc`` field and the stage-2 ``Content-Type``, so
        the three can never disagree.
        """
        timeout_s = float(getattr(settings, "export_timeout_s", 30.0) or 30.0) * 4
        if service == BROADCASTIFY_SERVICE and encoding_for(settings) == "mp3":
            return transcode_to_mp3(
                path,
                af=af,
                bitrate_kbps=mp3_bitrate_kbps(settings),
                sample_rate=CALLS_MP3_SAMPLE_RATE,
                timeout_s=timeout_s,
                # A standalone file, not a slice of the feed's bitstream: the Xing frame is
                # wanted here and is exactly what must not be written there.
                xing=True,
            )
        return transcode_to_m4a(path, af=af, timeout_s=timeout_s)

    # ------------------------------------------------------------------ one call

    def _handle(
        self,
        session: Session,
        client: ExportClient,
        claimed: ledger.Claimed,
        *,
        settings: Settings,
        mark,  # noqa: ANN001 - datetime | None
    ) -> bool:
        """Upload one claimed row.  Commits its own work; returns ``True`` on an upload.

        The caller's ``ledger.claim`` lock on the ``call_exports`` row is still held, and
        stays held through the encode and the upload below until this method commits.  The
        outcome it then writes includes an ``activity_events`` row carrying ``call_id``,
        whose foreign key makes Postgres take ``FOR KEY SHARE`` on the ``calls`` row — so
        the rule stated at :func:`app.db.lock_calls` applies: while any transaction waits on
        ``call_exports`` it must not hold a lock on ``calls`` stronger than
        ``FOR NO KEY UPDATE``, or ingest reconcile's ``repoint_exports`` and this method
        deadlock against each other.

        The Broadcastify **claim** is not part of that transaction: it is taken and
        committed in a session of its own (:meth:`_claim_call`) before anything is sent, so
        the per-talkgroup lock it is granted under is held for milliseconds rather than
        across the throttle, the registration, the encode and the PUT.
        """
        service = client.service
        call = claimed.call
        remembered = self._take_unrecorded(claimed.export_id)
        if remembered is not None:
            # Sent on an earlier pass whose outcome could not be written.  Record it; never
            # send it again.
            return self._record_success(session, claimed, remembered)
        #: how many sends this call of _handle made — more than one only for the
        #: Broadcastify retry inside the skew window, and read by the failure paths below
        tried = 1
        try:
            # Guard two: the SQL in discover() said yes, but the watermark may have moved
            # (or been cleared) since the row was written.  Re-ask before sending bytes.
            if not exportable(call, mark):
                ledger.mark_skipped(session, claimed.export_id, "outside the live export window")
                session.commit()
                return False

            # …and the operator's selection, re-read for the same reason: a talkgroup can
            # be un-ticked, or a system dropped, between discovery and the upload.
            deselected = excluded_reason(session, call, settings, service)
            if deselected is not None:
                ledger.mark_skipped(session, claimed.export_id, deselected)
                session.commit()
                return False

            reason = ledger.skip_reason_for(call, settings)
            if reason is not None:
                ledger.mark_skipped(session, claimed.export_id, reason)
                session.commit()
                return False

            facts = facts_for(session, call)
            if facts is None:
                ledger.mark_skipped(session, claimed.export_id, "no talkgroup")
                session.commit()
                return False

            # An earlier attempt sent the whole request and heard nothing back.  The service
            # may have the call, and OpenMHz does not deduplicate: ask before sending again.
            # A lookup that cannot answer raises ExportError and the row keeps waiting —
            # "could not ask" is never read as "not there".
            if claimed.verify_first and callable(getattr(client, "find_uploaded", None)):
                found = client.find_uploaded(facts)
                if found is not None:
                    return self._record_success(
                        session,
                        claimed,
                        _Success(
                            service=service,
                            result=found,
                            claim_id=None,
                            meta={
                                "service": service,
                                "talkgroup": facts.talkgroup,
                                "remote_id": found.remote_id,
                                "response": found.response,
                                "attempts": claimed.attempts,
                                "verified": True,
                            },
                        ),
                    )

            try:
                path, processed = resolve_playback(call, self._where(settings))
            except AudioUnavailable as exc:
                ledger.mark_skipped(session, claimed.export_id, f"no audio: {exc.detail}")
                session.commit()
                return False

            # The processed copy when there is one (§5.5) — a listener on OpenMHz gets the
            # same even, trimmed, tone-tamed audio a listener here does.  It carries its own
            # loudness normalization already, so the §5.2 filter is applied only to the
            # unprocessed original; running both would expand what is already even.
            # …and on top of either, the export level (:mod:`app.exporters.loudness`):
            # the stored file sits where the in-app player's compressor wants it, which is
            # too quiet for a service that has no compressor behind it.  Composed with the
            # §5.2 filter, never in place of it.
            base_af = None if processed or not _export_normalize(settings) else ffmpeg_audio_filter(settings)
            # …and the tone envelope back on after that lift: a call that is nothing but an
            # attenuated tone measures as a quiet call, and the export target would hand the
            # service a page louder than the file we stored (the tone-only call).
            af = export_filter(
                base_af,
                settings,
                path=path,
                duration_s=facts.duration_s,
                audio_meta=call.audio_meta,
                processed=processed,
            )
            claim_id: int | None = None
            if service == BROADCASTIFY_SERVICE and _splits_stages(client):
                late = late_reason(call, settings)
                if late is not None:
                    # Nothing is sent: the answer is already known, and a registration for
                    # a call this old costs a round trip to be told so.
                    ledger.mark_skipped(session, claimed.export_id, late)
                    session.commit()
                    return False
                # One immediate retry, inside the skew window, for a row nothing has tried
                # yet.  A backed-off retry is no retry at all here: the first step of the
                # curve (30 s) is twice the window, so it was always skipped as late.
                tries = 1 + self._immediate_retries(claimed, settings)
                audio: bytes | None = None
                for attempt in range(tries):
                    tried = attempt + 1
                    try:
                        # The claim, before anything is sent.  A recorder that already
                        # uploaded this call holds it, and registering it again would not
                        # read as a duplicate at the far end: Broadcastify keys one on a
                        # *whole-second* ``ts``, and the recorder's second and ours need
                        # not be the same one (docs/EXPORTERS.md, "Recorder-direct
                        # upload").  So the registry is the tie-break, and losing it costs
                        # no request at all.
                        held = self._claim_call(facts)
                        if not held.granted:
                            ledger.mark_skipped(
                                session, claimed.export_id, claims.reason_for(held.holder)
                            )
                            claims.finish(session, held.claim_id, call_id=claimed.call_id)
                            session.commit()
                            return False
                        claim_id = held.claim_id
                        self._throttle(service, settings)
                        # Register *before* encoding.  Nine stage-1 answers in ten on a
                        # system another feeder also uploads are "already received this
                        # call", and the encode that used to come first was a second or
                        # more of ffmpeg spent on a call Broadcastify was about to decline
                        # — a second that also counted against the skew window, because
                        # they measure the age of the call when the registration lands.
                        registration = client.register(facts)
                        if audio is None:
                            audio = self._audio_for(service, path, af=af, settings=settings)
                        try:
                            client.put_audio(registration.url, audio)
                        except ExportError as exc:
                            # The registration succeeded, so its line is the only thing the
                            # service said about this call and the ledger keeps it; the
                            # failure itself is retryable, and a retry starts again from a
                            # fresh registration because the URL is single-use.
                            raise ExportError(
                                str(exc),
                                response=registration.response,
                                response_code=registration.response_code,
                            ) from exc
                        result = UploadResult(
                            remote_id=remote_id_for(registration.url),
                            response=registration.response,
                            response_code=registration.response_code,
                        )
                        break
                    except PermanentExportError:
                        raise
                    except ExportError as exc:
                        if tried >= tries or late_reason(call, settings) is not None:
                            raise
                        log.warning(
                            "export: broadcastify attempt on call %s failed (%s); retrying "
                            "at once, inside the skew window",
                            claimed.call_id,
                            exc,
                        )
            else:
                audio = self._audio_for(service, path, af=af, settings=settings)
                self._throttle(service, settings)
                result = client.upload(facts, audio)
        except SkipCall as exc:
            # The service answered "not this one" — a duplicate it already has, or a
            # talkgroup it does not know.  Neither is a failure and neither is worth a
            # retry: record why and move on.
            #
            # No rollback first.  The claim holds a FOR UPDATE lock on the ledger row, and
            # rolling back would drop it while the row is still `pending` and still due —
            # a second worker process could then claim the same call and attempt it again.
            # The session has done nothing but reads since the claim, so the outcome is
            # written inside that same transaction, exactly as the success path does.
            try:
                ledger.mark_skipped(
                    session,
                    claimed.export_id,
                    exc.reason,
                    response=exc.response,
                    response_code=exc.response_code,
                )
                # A duplicate is the one skip worth counting on its own: it is the answer
                # the queue gives when somebody else's uploader — or an earlier run of this
                # one — already published the call, and on this deployment it is most of
                # the ledger.  Its own activity kind makes that a count rather than a
                # string search over ``skip_reason``.  ``info``: nothing is wrong, it is
                # just already there.
                if exc.reason.startswith(DUPLICATE_REASON):
                    activity.record(
                        session,
                        source="export",
                        level="info",
                        kind="export.duplicate",
                        message=DUPLICATE_REASON,
                        call_id=claimed.call_id,
                        meta={
                            "service": service,
                            "response": exc.response,
                            "response_code": exc.response_code,
                        },
                    )
                session.commit()
            except Exception:  # noqa: BLE001 - see _fail: the row must not come straight back
                log.exception("export: could not record the %s skip for call %s", service, claimed.call_id)
                self._rollback_quietly(session)
                self._bump_fresh(claimed, attempts=claimed.attempts, error=f"skip not recorded: {exc.reason}")
            return False
        except (ExportError, TranscodeError, OSError) as exc:
            self._fail(session, client, claimed, error=exc, settings=settings, tried=tried)
            return False
        except Exception as exc:  # noqa: BLE001 - an unexpected bug is still just a failure
            log.exception("export: %s raised on call %s", service, claimed.call_id)
            self._fail(session, client, claimed, error=exc, settings=settings, tried=tried)
            return False

        return self._record_success(
            session,
            claimed,
            _Success(
                service=service,
                result=result,
                claim_id=claim_id,
                meta={
                    "service": service,
                    "talkgroup": facts.talkgroup,
                    "bytes": len(audio or b""),
                    "remote_id": result.remote_id,
                    "response": result.response,
                    "attempts": claimed.attempts + tried,
                    # What was actually done to the audio on the way out, so an operator
                    # reading a row can see the export gain that was applied (or that there
                    # was none) without reconstructing it from the settings of the day.
                    "filter": af,
                },
            ),
        )

    # ------------------------------------------------------------------ claim and outcome

    def _immediate_retries(self, claimed: ledger.Claimed, settings: Settings) -> int:
        """How many times a Broadcastify send may be retried at once, inside this pass.

        One, for a row nothing has attempted yet, when the skew window is on.  A row a
        recorder already tried (``attempts = 1``, :func:`app.api.call_upload.record_broadcastify_outcome`)
        has had its first attempt, and this pass is its retry.  With the window off there
        is no reason to hurry and the ordinary backoff applies.
        """
        return 1 if _skew_window_on(settings) and claimed.attempts == 0 else 0

    def _claim_call(self, facts) -> claims.Claim:  # noqa: ANN001 - CallFacts
        """Take (or re-confirm) keyup's Broadcastify claim in a short transaction of its own.

        Committed before this returns, so the per-talkgroup advisory lock it was granted
        under is released at once — a recorder's ``POST /recorder/broadcastify/claim`` for
        the same talkgroup waits milliseconds, not the length of our upload.  A busy lock
        (:class:`~app.exporters.claims.ClaimBusy`) is a retryable failure like a timeout.
        """
        claim_session = self.session_factory()
        try:
            held = claims.acquire(
                claim_session,
                instance=claims.KEYUP,
                tgid=int(facts.talkgroup or 0),
                ts=facts.start_epoch,
                duration_s=facts.duration_s,
            )
            claim_session.commit()
            return held
        except claims.ClaimBusy as exc:
            self._rollback_quietly(claim_session)
            raise ExportError(f"broadcastify claim not taken: {exc}") from exc
        except Exception:
            self._rollback_quietly(claim_session)
            raise
        finally:
            claim_session.close()

    def _take_unrecorded(self, export_id: int) -> _Success | None:
        with self._state_lock:
            return self._unrecorded.pop(int(export_id), None)

    def _record_success(self, session: Session, claimed: ledger.Claimed, done: _Success) -> bool:
        """Write ``uploaded`` for a call that went out.  Always returns ``True``.

        Inside the claim's transaction first, as every outcome is.  If that write fails the
        call has still been published, and leaving the row ``pending`` would send it again
        on the next pass — so the write is retried in fresh transactions, the ledger row
        before the activity line, and if the database refuses all of them the success is
        remembered in memory and written, not re-sent, the next time the row comes up.
        """
        try:
            self._write_success(session, claimed, done, with_activity=True)
            return True
        except Exception:  # noqa: BLE001 - the upload happened; only the bookkeeping failed
            log.exception(
                "export: could not record the %s upload of call %s; retrying the write",
                done.service,
                claimed.call_id,
            )
            self._rollback_quietly(session)
        for delay in OUTCOME_RETRY_DELAYS_S:
            self._sleep(delay)
            fresh = self.session_factory()
            try:
                self._write_success(fresh, claimed, done, with_activity=False)
            except Exception as exc:  # noqa: BLE001 - try again, then remember it
                log.warning(
                    "export: recording the %s upload of call %s failed again: %s",
                    done.service,
                    claimed.call_id,
                    exc,
                )
                self._rollback_quietly(fresh)
                fresh.close()
                continue
            # The ledger row is what stops a second send; the activity line is only the
            # record of it, and must not be able to undo the first.
            try:
                self._write_activity(fresh, claimed, done)
                fresh.commit()
            except Exception:  # noqa: BLE001
                log.exception("export: could not log the %s upload of call %s", done.service, claimed.call_id)
                self._rollback_quietly(fresh)
            finally:
                fresh.close()
            return True
        with self._state_lock:
            self._unrecorded[int(claimed.export_id)] = done
        log.error(
            "export: call %s went to %s but the ledger could not be updated; it will be "
            "recorded, not sent again, when row %s next comes up",
            claimed.call_id,
            done.service,
            claimed.export_id,
        )
        return True

    def _write_success(
        self, session: Session, claimed: ledger.Claimed, done: _Success, *, with_activity: bool
    ) -> None:
        ledger.mark_uploaded(
            session,
            claimed.export_id,
            remote_id=done.result.remote_id,
            response=done.result.response,
            response_code=done.result.response_code,
            uploaded_by=claims.KEYUP,
        )
        if done.claim_id is not None:
            claims.finish(
                session,
                done.claim_id,
                outcome="uploaded",
                response=done.result.response,
                call_id=claimed.call_id,
            )
        if with_activity:
            self._write_activity(session, claimed, done)
        session.commit()

    @staticmethod
    def _write_activity(session: Session, claimed: ledger.Claimed, done: _Success) -> None:
        activity.record(
            session,
            source="export",
            level="info",
            kind="export.uploaded",
            message=f"uploaded to {done.service}",
            call_id=claimed.call_id,
            meta=done.meta,
        )

    @staticmethod
    def _rollback_quietly(session: Session) -> None:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - a dead connection cannot even roll back
            log.debug("export: rollback failed", exc_info=True)

    def _bump_fresh(self, claimed: ledger.Claimed, *, attempts: int, error: str) -> None:
        """Push a row whose outcome could not be written back by its backoff, in a new transaction.

        The rollback that precedes this leaves the row ``pending`` and due *now*; without
        the bump the next pass sends it again straight away, and the one after that.
        """
        fresh = self.session_factory()
        try:
            ledger.bump_attempt(fresh, claimed.export_id, attempts=attempts, error=error)
            fresh.commit()
        except Exception:  # noqa: BLE001 - nothing left to fall back to; the pass guard holds
            log.exception("export: could not even back off row %s", claimed.export_id)
            self._rollback_quietly(fresh)
        finally:
            fresh.close()

    def _fail(
        self,
        session: Session,
        client: ExportClient,
        claimed: ledger.Claimed,
        *,
        error: BaseException,
        settings: Settings,
        tried: int = 1,
    ) -> None:
        """Record one failed attempt: retry, park or skip, plus an activity line.

        Writes inside the claim's transaction — no rollback first.  The claim's FOR UPDATE
        lock is what stops a second worker process from picking this row up mid-attempt,
        and releasing it before the attempt has been counted would let exactly that happen.
        When the write itself fails (the error was the database's, or the ledger refused),
        the transaction is rolled back and the attempt is counted in a fresh one
        (:meth:`_bump_fresh`) so the row is not due again at once.

        Three rules on top of the backoff curve:

        * **ambiguous** (:class:`~app.exporters.base.AmbiguousExportError`): the service may
          have the call.  Retried only after :data:`~app.exporters.ledger.VERIFY_DELAY_S`
          and only behind a lookup (``last_error`` carries
          :data:`~app.exporters.ledger.AMBIGUOUS_PREFIX`); a client that cannot look calls up
          skips the row as :data:`~app.exporters.ledger.UNCERTAIN_REASON` instead.
        * **Broadcastify with the skew window on**: no retry is scheduled, because none can
          land in time — the immediate retry already happened in :meth:`_handle`.  Past the
          window the row is skipped as late (with the failure kept); inside it, parked.
        * a **permanent** refusal parks at once.
        """
        service = client.service
        before = claimed.attempts + max(1, tried) - 1
        used = before + 1
        max_attempts = max(1, int(getattr(settings, "export_max_attempts", 5) or 5))
        permanent = isinstance(error, PermanentExportError)
        ambiguous = isinstance(error, AmbiguousExportError)
        message = str(error)
        if (ambiguous or claimed.verify_first) and not message.startswith(ledger.AMBIGUOUS_PREFIX):
            message = f"{ledger.AMBIGUOUS_PREFIX} {message}"
        # A permanent refusal is not worth four more tries; park it now.
        if permanent:
            max_attempts = used
        retry_in: float | None = None
        skip_reason: str | None = None
        if ambiguous and not callable(getattr(client, "find_uploaded", None)):
            skip_reason = f"{ledger.UNCERTAIN_REASON}: {service} may already have it ({error})"
        elif ambiguous:
            retry_in = max(ledger.VERIFY_DELAY_S, ledger.backoff_seconds(before))
        elif service == BROADCASTIFY_SERVICE and _skew_window_on(settings) and not permanent:
            late = late_reason(claimed.call, settings)
            if late is not None:
                skip_reason = f"{late}; last attempt failed: {error}"
            else:
                max_attempts = used
        try:
            if skip_reason is not None:
                ledger.mark_skipped(
                    session,
                    claimed.export_id,
                    skip_reason,
                    response=getattr(error, "response", None),
                    response_code=getattr(error, "response_code", None),
                    attempts=used,
                    error=message,
                )
                parked = True
            else:
                parked = ledger.mark_failed(
                    session,
                    claimed.export_id,
                    attempts=before,
                    error=message,
                    max_attempts=max_attempts,
                    # What the service said, when it said anything: a transcode error or a
                    # refused connection carries no response and must not blank one an
                    # earlier attempt recorded (``ledger._said``).
                    response=getattr(error, "response", None),
                    response_code=getattr(error, "response_code", None),
                    retry_in_s=retry_in,
                )
            activity.record(
                session,
                source="export",
                # An error level is reserved for a call we have given up on; a single
                # retryable failure is a warning, because the next cycle usually fixes it.
                level="error" if parked and skip_reason is None else "warn",
                kind="export.failed",
                message=f"{service} upload failed: {error}"[:2000],
                call_id=claimed.call_id,
                meta={
                    "service": service,
                    "attempts": used,
                    "max_attempts": max_attempts,
                    "parked": parked,
                    "permanent": permanent,
                    "ambiguous": ambiguous,
                    "skipped": skip_reason,
                    "response": getattr(error, "response", None),
                },
            )
            session.commit()
        except Exception:  # noqa: BLE001 - even the failure bookkeeping must not raise
            log.exception("export: could not record the %s failure for call %s", service, claimed.call_id)
            self._rollback_quietly(session)
            self._bump_fresh(claimed, attempts=before, error=message)

    # ------------------------------------------------------------------ lifecycle

    def _poll_seconds(self, settings: Settings | None = None) -> float:
        """``export_poll_seconds``, floored at one second."""
        source = settings if settings is not None else self.settings
        return max(1.0, float(getattr(source, "export_poll_seconds", 15.0) or 15.0))

    def _feed_pass(self) -> float:
        """One reconcile of the Broadcastify Feed.  Returns the poll interval it read."""
        session = self.session_factory()
        try:
            try:
                settings = self.runtime(session)
            except Exception:  # noqa: BLE001 - the database is somebody else's problem
                log.exception(
                    "export: could not read the settings table; leaving the feed as it is "
                    "(watermark untouched)"
                )
                session.rollback()
                return self._poll_seconds()
            try:
                self._run_feed(session, settings)
            except Exception:  # noqa: BLE001 - the feed must not stop anything
                log.exception("export: the broadcastify feed cycle failed")
                session.rollback()
            return self._poll_seconds(settings)
        finally:
            session.close()

    def _service_pass(self, service: str) -> float:
        """One discover-and-upload pass for a single service.  Returns its poll interval.

        The same body :meth:`cycle` runs for one client, with its own session: a session is
        not shareable between threads, so each service's loop opens and closes its own.
        Settings are re-read and the client rebuilt every pass, exactly as :meth:`cycle`
        does, so a key typed in on the Exports page takes effect on the next pass.
        """
        session = self.session_factory()
        try:
            try:
                settings = self.runtime(session)
            except Exception:  # noqa: BLE001 - see :meth:`runtime`
                log.exception(
                    "export: could not read the settings table; skipping this %s pass "
                    "entirely (watermarks left untouched)",
                    service,
                )
                session.rollback()
                return self._poll_seconds()
            clients = build_clients(settings) if self._rebuild_clients else self.clients
            for client in clients:
                if client.service != service:
                    continue
                try:
                    self._run_service(session, client, settings)
                except Exception:  # noqa: BLE001 - one pass must not end the thread
                    log.exception("export: the %s cycle failed", service)
                    session.rollback()
            return self._poll_seconds(settings)
        finally:
            session.close()

    def _service_loop(self, stop: threading.Event, service: str) -> None:
        """One service's own loop, forever until ``stop``.  Never raises."""
        log.debug("export: %s loop started", service)
        while not stop.is_set():
            interval = self._poll_seconds()
            try:
                interval = self._service_pass(service)
            except Exception:  # noqa: BLE001 - the thread outlives any one pass
                log.exception("export: the %s loop caught an unexpected error", service)
            stop.wait(interval)
        log.debug("export: %s loop stopped", service)

    def run(self, stop: threading.Event) -> None:
        """Run until ``stop`` is set: the feed here, and one thread per call-export service.

        This used to be one sequential loop, and that is what put Broadcastify's skew
        window out of reach: a single cycle ran the feed, then Broadcastify's discovery and
        up to a batch of uploads, then OpenMHz's — so a call waited behind another
        service's whole batch, and the ledger row that should have been registered three
        seconds after ingest was registered fifteen or twenty seconds after it.  Uploading
        is almost entirely waiting on somebody else's server, so the two queues have no
        reason to take turns: each service now polls on its own ``export_poll_seconds``,
        with its own session and its own client, and neither can delay the other.

        The feed stays in *this* thread.  It is a reconcile rather than a queue — it starts
        and stops a source thread of its own — and it is owned by this loop so that whatever
        stops the worker closes the mount, rather than leaving a daemon thread holding
        somebody else's Icecast source slot until the process exits.
        """
        threads = [
            threading.Thread(
                target=self._service_loop,
                args=(stop, service),
                name=f"export-{service}",
                daemon=True,
            )
            for service in CALL_EXPORT_SERVICES
        ]
        try:
            for thread in threads:
                thread.start()
            while not stop.is_set():
                try:
                    interval = self._feed_pass()
                except Exception:  # noqa: BLE001 - the loop outlives any one pass
                    log.exception("export: feed cycle failed")
                    interval = self._poll_seconds()
                stop.wait(interval)
        finally:
            self.feed.shutdown()
            for thread in threads:
                # Bounded: a thread in the middle of a 30-second upload must not hold up
                # the shutdown of the process, and it is a daemon precisely so it cannot.
                thread.join(timeout=5.0)


def run_export_worker(
    stop: threading.Event,
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory = SessionLocal,
) -> None:
    """Entry point for :class:`app.worker.Worker`."""
    ExportWorker(settings=settings, session_factory=session_factory).run(stop)
