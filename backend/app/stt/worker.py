"""The database half of the transcription worker (ARCHITECTURE §5.2).

``app.stt.runner`` owns the loop; this module owns the four callbacks it needs and the
rows they touch:

``claim``
    ``SELECT … FOR UPDATE SKIP LOCKED`` the next due ``transcription_jobs`` row, mark it
    ``running`` with this worker's name, and hand the runner an absolute audio path.
``options_for``
    build the per-request :class:`~app.stt.types.TranscribeOptions`, including the domain
    prompt assembled from the directory (talkgroup aliases, unit ids, place names).  The
    vocabulary is cached: it changes on the timescale of a playlist import, not of a job.
``complete``
    write the ``transcripts`` row, demote the previous primary, denormalize the cleaned
    text onto ``calls`` and emit ``call.transcribed`` on the live bus.
``fail``
    count the attempt, remember the error and requeue with exponential backoff until
    ``max_attempts``, after which the job is parked as ``failed`` — as recoverable when the
    router called it an outage (:func:`unpark_backend_outage` brings it back, at most
    :data:`OUTAGE_REQUEUE_CAP` times), for good when every backend failed on the recording.

``complete`` and ``fail`` write only a job this claim still owns: the row must still be
``running`` with the ``started_at`` the claim stamped (:attr:`~app.stt.runner.Job.claimed_at`).
A job cancelled by a forced re-transcribe or by the audio pass, or reaped as stale and
claimed again, belongs to somebody else by then, and the late result is discarded — no
transcript row, no ``call.transcribed``.  While a job runs, :meth:`SttWorker.heartbeat`
keeps its ``locked_at`` fresh, so the stale reaper only ever takes a claim whose process
has stopped beating.

Each callback opens and closes its own session, because the runner calls them from
several threads and psycopg connections are not shared across threads.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, cast, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, aliased

from app import activity, wake
from app.audio import AudioUnavailable, resolve_playback
from app.audioproc.process import CONTENT_STATIC
from app.audioproc.process import NO_CONTENT as NO_SPEECH_CONTENTS
from app.config import Settings, get_settings
from app.db import SessionLocal, lock_calls, notify
from app.exporters.ledger import NO_SPEECH_REASON, STATIC_REASON
from app.ingest.flow import BACKLOG_BAND, gauge, set_router
from app.models import (
    Call,
    Person,
    Radio,
    Talkgroup,
    Transcript,
    TranscriptionJob,
    TranscriptStatusEnum,
    Unit,
)
from app.runtime_settings import effective_settings

from .config_store import entries_for
from .config_store import version as config_version
from .hotswap import RouterHandle, RouterReloader
from .normalize import ffmpeg_audio_filter
from .postprocess import (
    assess,  # noqa: F401 - re-exported for callers that re-score
    compile_patterns,
    hallucination_phrase,
)
from .prompt import DEFAULT_PLACES, build_prompt
from .router import (
    BackendRouter,
    NoBackendAvailableError,
    NoBackendEnabledError,
    RequestFailedEverywhereError,
    build_backends,
)
from .runner import Job, run_jobs
from .stats_store import StatsPublisher, override_provider
from .types import Quality, QualityAssessment, TranscribeOptions, TranscriptResult

log = logging.getLogger("app.stt.worker")

__all__ = [
    "OUTAGE_REQUEUE_CAP",
    "PARKED_BACKEND_UNAVAILABLE",
    "PARKED_OUTAGE_REQUEUE_CAP",
    "SttWorker",
    "a_backend_is_healthy",
    "hallucination_reason",
    "request_timeout_s",
    "requeue_failed",
    "requeue_stale",
    "run_stt_worker",
    "unpark_backend_outage",
]


def prompt_places(settings: Settings) -> tuple[str, ...]:
    """The place names for the domain prompt: :data:`DEFAULT_PLACES`, then ``STT_PLACES``.

    ``STT_PLACES`` is a comma-separated list; blanks are dropped here and duplicates later,
    by :func:`app.stt.prompt.build_prompt`.
    """
    extra = (part.strip() for part in settings.stt_places.split(","))
    return (*DEFAULT_PLACES, *(part for part in extra if part))


#: first retry 60 s after the first failure, then 120 s, 240 s… (``30 s * 2**attempts``)
BACKOFF_BASE_S = 30

#: how long a job waits when no backend is enabled at all.  Not backoff — nothing failed —
#: so it is flat and short: the queue should start draining within seconds of the operator
#: switching a backend back on.
NO_BACKEND_RETRY_S = 20

#: ``transcription_jobs.parked_reason`` for a job parked while every backend was failing.
#: The one value the unpark pass looks for; a content failure parks with NULL.
PARKED_BACKEND_UNAVAILABLE = "backend_unavailable"

#: how many jobs one unpark pass moves.  The pass runs every two minutes, so a 395-job
#: outage clears on the first one and a pathological backlog drains over a few.
UNPARK_BATCH = 500

#: stamped on the error a parked job kept, so the row still says what went wrong the first
#: time and also says that nobody had to come and rescue it
AUTO_REQUEUE_PREFIX = "(auto-requeued after outage) "

#: How many times the unpark pass may requeue one job (``transcription_jobs.outage_requeues``).
#: A real outage costs a job one requeue per outage, because the pass only runs once a
#: backend is healthy again; three separate outages landing on the same job before it gets
#: through is not a thing that happens.  What *does* happen is a recording misread as an
#: outage — every backend failing on it while serving everybody else — and without a cap
#: that is a loop: three attempts, parked, requeued with ``attempts = 0``, for ever (the
#: looping BOLO call, two hours until it was parked by hand).  The router now tells the
#: two apart (``RequestFailedEverywhereError``); this is the backstop for when it cannot.
OUTAGE_REQUEUE_CAP = 3

#: ``parked_reason`` for a job that failed like an outage once more after using up
#: :data:`OUTAGE_REQUEUE_CAP`.  Not :data:`PARKED_BACKEND_UNAVAILABLE`, so the unpark pass
#: leaves it alone and the operator can see why; their requeue resets the count.
PARKED_OUTAGE_REQUEUE_CAP = "outage_requeue_cap"

#: The per-request client deadline, scaled to the clip: ``base + per_audio_s * duration``,
#: never more than the old flat 120 s.  Measured on production (7 days, 1.1 M transcripts):
#: a 40-50 s call takes 16 s on average and 31 s at p95, a 2 s call 4 s; the base is mostly
#: headroom for waiting behind the other requests on the same Speaches server (twelve worker
#: threads share three servers).  Fewer than 0.06 % of past requests ran longer than this,
#: and that time includes the in-backend retries, so the true share is lower still.  A clip
#: that does run over fails over to the next server rather than failing the job.
REQUEST_TIMEOUT_BASE_S = 45.0
REQUEST_TIMEOUT_PER_AUDIO_S = 1.5
REQUEST_TIMEOUT_MAX_S = 120.0


def request_timeout_s(duration_s: float | None) -> float:
    """The client deadline for one transcription request of ``duration_s`` seconds of audio.

    Unknown duration gets the old flat ceiling, :data:`REQUEST_TIMEOUT_MAX_S`.
    """
    if duration_s is None or duration_s <= 0:
        return REQUEST_TIMEOUT_MAX_S
    return min(REQUEST_TIMEOUT_MAX_S, REQUEST_TIMEOUT_BASE_S + REQUEST_TIMEOUT_PER_AUDIO_S * duration_s)


#: how long the assembled domain prompt is reused before the directory is re-read
VOCAB_TTL_S = 600.0

#: how many aliases/units the prompt query pulls; ``build_prompt`` trims to its char budget
_VOCAB_LIMIT = 200

#: How long an idle transcription thread sleeps when nothing rings it (:mod:`app.wake`).
#: New calls ring the bell through the worker's LISTEN connection, so this is only the
#: fallback: a retry whose back-off has run out, a job requeued by hand or by the API, a
#: notification lost while that connection was reconnecting.  It used to be the only way
#: in, at two seconds.
IDLE_POLL_S = 10.0

#: how often the worker logs the queue gauge (jobs per band, audio seconds, drain estimate)
GAUGE_LOG_S = 60.0

#: how often a running job's ``locked_at`` is refreshed.  Far inside ``stt_stale_minutes``
#: (10): one job can legitimately outlive that cutoff — a 300 s wait for a backend slot plus
#: three 120 s timeouts per backend, repeated on failover — exactly when a backend hangs.
HEARTBEAT_S = 60.0

#: …but not for ever.  A claim older than this stops being refreshed, so a thread wedged
#: on a request that never returns is reaped ``stt_stale_minutes`` later; if it does wake
#: up, its result is discarded because the job is no longer its claim.
HEARTBEAT_MAX_AGE_S = 3600.0

# ------------------------------------------------------------------ sanity guard
# The last line of defense against a transcript that is not speech.  ``postprocess``
# already labels the failures it can see in the model's own numbers; these two catch the
# one it cannot, because the decode looks confident and the segments look fine: whisper
# takes ``initial_prompt`` as prior text and, on a clip with nothing decodable in it, simply
# continues writing the prompt — round and round.  The typical case: a 1.5 s call
# stored 500-odd characters that were the system's own STT vocabulary ("LKPD DISP,
# NF PW 1507, Millbrook Road, EC LAW 4, …") repeated eight times.
#
# Both numbers are module constants rather than settings on purpose: start opinionated, and
# promote them to ``SETTINGS_SPEC`` only if a real transcript ever trips one.

#: Characters of transcript per second of audio above which the text cannot have been
#: spoken.  Real dispatch traffic in this corpus runs 15-20 chars/s and the fastest human
#: reading of a plate or an address touches 20; 40 is double that, so only an engine
#: writing words nobody said can exceed it.  (``postprocess`` scores *words* per second at
#: a suspect-only threshold; this one is a hard stop and is measured in characters, which
#: is what a prompt-echo loop actually inflates.)
MAX_CHARS_PER_SECOND = 40.0

#: …except on very short calls, where one legitimate sentence over 1.5 s really is 60
#: chars/s.  Nothing under this many characters is ever judged on its rate: a real
#: "Copy, en route" must not be thrown away to catch a loop that is, by definition, long.
MIN_HALLUCINATION_CHARS = 80

#: A run of >= 12 characters repeated >= 4 times back to back is a decoder loop.  Twelve is
#: past the length of any phrase a dispatcher repeats verbatim ("10-4, 10-4"), and four
#: repetitions is past the two or three a real rebroadcast reaches ("dark in color, Buick
#: four-door sedan," twice in the 44.5 s BOLO sample).  Both repeat rules sit above the
#: :data:`MIN_HALLUCINATION_CHARS` floor as well: a six-second radio check really is
#: "Unit 12 copy." four times over, and fifty-odd characters of it is an acknowledgment,
#: not a loop.  A loop is by definition long, so nothing short is ever judged as one.
MIN_LOOP_UNIT_CHARS = 12
MIN_LOOP_REPEATS = 4

#: …and so is text that shrinks by more than this fraction when consecutive repeated
#: phrases are collapsed *and* the collapse dropped at least :data:`MIN_COLLAPSED_PHRASES`
#: of them.  The shrink fraction alone cannot tell "10-4, 10-4." from an echo — a
#: two-phrase transcript that says the same thing twice shrinks by half whatever its
#: content — so the count is what separates genuine repetition from a decoder going round.
#: Three dropped phrases is past "Copy, copy." (one) and "Roger. Roger. Roger." (two): a
#: dispatcher saying a thing four times is doing it deliberately, which is why the
#: character floor guards this rule too.
MAX_REPEAT_SHRINK = 0.60
MIN_COLLAPSED_PHRASES = 3

#: guard against pathological backtracking in the loop regex on a runaway transcript; a
#: loop is visible in the first few thousand characters or it is not a loop.
_LOOP_SCAN_CHARS = 4_000

_LOOP_RE = re.compile(rf"(.{{{MIN_LOOP_UNIT_CHARS},}}?)\1{{{MIN_LOOP_REPEATS - 1},}}", re.DOTALL)
_PHRASE_SPLIT_RE = re.compile(r"[,.;:!?]+")


def _collapse_phrases(text: str) -> tuple[str, int]:
    """``text`` with each phrase that merely repeats the one before it dropped.

    Returns the collapsed text and how many phrases the collapse removed — the caller
    needs both, because how *much* shorter the text got says nothing on its own about
    whether the repetition was a person or a decoder.
    """
    kept: list[str] = []
    previous: str | None = None
    dropped = 0
    for phrase in _PHRASE_SPLIT_RE.split(text):
        key = " ".join(phrase.lower().split())
        if not key:
            continue
        if key == previous:
            dropped += 1
            continue
        kept.append(key)
        previous = key
    return " ".join(kept), dropped


def hallucination_reason(
    text: str,
    duration_s: float | None,
    patterns: Sequence[re.Pattern[str]] | None = None,
    *,
    no_speech_prob: float | None = None,
) -> str | None:
    """Why ``text`` cannot be a transcript of ``duration_s`` seconds of audio, or ``None``.

    Three independent tests, any of which condemns the text:

    ``hallucination_phrase``
        the text is a caption-credit or sign-off artifact — the exact phrase list, the
        builtin pattern families, or an operator pattern from ``stt_hallucination_patterns``
        passed in as ``patterns``.  The same call ``assess`` makes, ``duration_s`` and
        ``no_speech_prob`` and all (they gate the sign-off family and the conversational
        words), so the worker and the quality label never disagree about the same text.

    ``chars_per_second``
        more characters than :data:`MAX_CHARS_PER_SECOND` allows, above the
        :data:`MIN_HALLUCINATION_CHARS` floor.

    ``repeat_loop``
        a substring of :data:`MIN_LOOP_UNIT_CHARS` characters or more repeated
        :data:`MIN_LOOP_REPEATS` times in a row, or at least
        :data:`MIN_COLLAPSED_PHRASES` consecutive repeated phrases collapsing away more
        than :data:`MAX_REPEAT_SHRINK` of the text — both above the
        :data:`MIN_HALLUCINATION_CHARS` floor, so that a short transcript that is nothing
        but acknowledgments ("10-4, 10-4.", "Unit 12 copy." four times) is kept.
    """
    text = text.strip()
    if not text:
        return None

    phrase = hallucination_phrase(text, patterns, duration_s, no_speech_prob=no_speech_prob)
    if phrase is not None:
        return f"hallucination_phrase:{phrase}"

    if duration_s and duration_s > 0 and len(text) > max(
        MIN_HALLUCINATION_CHARS, duration_s * MAX_CHARS_PER_SECOND
    ):
        return f"chars_per_second={len(text) / duration_s:.0f}"

    if len(text) < MIN_HALLUCINATION_CHARS:
        # Too short to be a loop, whatever it looks like.  Real traffic is full of
        # acknowledgments said two, three, four times over in a few seconds; discarding
        # those loses the only words the call had.
        return None

    window = text[:_LOOP_SCAN_CHARS]
    match = _LOOP_RE.search(window)
    if match:
        unit = " ".join(match.group(1).split())
        return f"repeat_loop:{unit[:40]!r}"

    flat = " ".join(window.lower().split())
    collapsed, dropped = _collapse_phrases(window)
    if (
        flat
        and dropped >= MIN_COLLAPSED_PHRASES
        and len(collapsed) < (1.0 - MAX_REPEAT_SHRINK) * len(flat)
    ):
        return "repeat_loop:phrases"
    return None

SessionFactory = Callable[[], Session]


def _claim_sql(*, include_backlog: bool = True) -> Any:
    """The claim statement.

    One statement, so the row is selected, locked and marked ``running`` atomically: two
    workers polling the same queue can never be handed the same job, and a worker that
    dies leaves a ``running`` row with a stale ``locked_at`` for :func:`requeue_stale`.

    ``priority ASC``: everything that writes a job treats 1 as "do this first" — the
    forced re-transcribe from ``POST /calls/{id}/transcribe`` asks for priority 1 while
    ingest enqueues at the default — and ordering the other way ran the queue backwards,
    serving the least urgent job first (M6).

    ``started_at`` is the claim's ownership token (:attr:`~app.stt.runner.Job.claimed_at`):
    ``clock_timestamp()`` rather than ``now()`` so that it is the instant of *this*
    statement, and no two claims of one row can share it.

    ``include_backlog=False`` adds ``priority < 500`` — the fairness guard.  Ordering alone
    is not enough when the backlog is a hundred thousand jobs deep: every thread would be
    forty seconds into a migrated recording at the moment a live call arrives, and
    ``priority ASC`` cannot preempt work already running.  Reserving threads is what bounds
    that wait (:attr:`SttWorker.backlog_limit`).
    """
    band = "" if include_backlog else f"AND priority < {BACKLOG_BAND}"
    return text(
        f"""
        WITH claimed AS (
            SELECT id
            FROM transcription_jobs
            WHERE status = 'queued' AND scheduled_at <= now() {band}
            ORDER BY priority ASC, scheduled_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE transcription_jobs AS j
        SET status = 'running',
            locked_by = :worker,
            locked_at = now(),
            started_at = clock_timestamp()
        FROM claimed
        WHERE j.id = claimed.id
        RETURNING j.id, j.call_id, j.priority, j.max_attempts, j.attempts, j.backend_name,
                  j.started_at, j.last_error
        """
    )


class SttWorker:
    """Bundles the four runner callbacks around one session factory and one router."""

    def __init__(
        self,
        *,
        router: BackendRouter,
        settings: Settings | None = None,
        session_factory: SessionFactory = SessionLocal,
        worker_name: str | None = None,
        audio_root: Path | None = None,
        worker_count: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.router = router
        self.session_factory = session_factory
        self.audio_root = Path(audio_root) if audio_root is not None else Path(self.settings.audio_root)
        self.name = worker_name or f"{socket.gethostname()}:{threading.get_ident()}"
        self._vocab_lock = threading.Lock()
        self._prompt: str | None = None
        self._prompt_at = 0.0
        self._prompt_extra: tuple[str, str] | None = None
        # Fairness (§5.2): how many of this process' threads may be inside a backlog-band
        # job at once, and which job ids are currently holding one of those slots.
        self.worker_count = max(
            1, int(worker_count if worker_count is not None else self.settings.stt_workers)
        )
        self._backlog_lock = threading.Lock()
        self._backlog_held: set[int] = set()
        self._gauge_at = 0.0
        # The claims this process is working on, ``job id -> claimed_at``: what the
        # heartbeat refreshes, and nothing else.
        self._active_lock = threading.Lock()
        self._active: dict[int, datetime] = {}

    @property
    def backlog_limit(self) -> int:
        """Threads that may hold a backlog job at once — ``STT_BACKLOG_WORKER_SHARE`` of them.

        Floored at one, so a single-threaded deployment still drains its backlog eventually
        rather than never; with the default 0.5 and two threads, one thread is always free
        for live traffic no matter how deep the migration queue is.
        """
        share = float(getattr(self.settings, "stt_backlog_worker_share", 0.5))
        share = max(0.0, min(1.0, share))
        return max(1, int(self.worker_count * share))

    def _release_backlog(self, job_id: int) -> None:
        with self._backlog_lock:
            self._backlog_held.discard(job_id)
        with self._active_lock:
            self._active.pop(job_id, None)

    def heartbeat(self) -> int:
        """Refresh ``locked_at`` on every job this process is still working on.

        Returns how many rows were touched.  Only rows that are still ``running`` under the
        exact claim this process made are refreshed, and ``FOR NO KEY UPDATE SKIP LOCKED``
        means the beat never waits on anybody: a row somebody else holds is skipped this
        time and caught on the next beat.  It touches ``transcription_jobs`` only, never
        ``calls``, so it takes no part in the §5.2 lock order.
        """
        horizon = datetime.now(UTC) - timedelta(seconds=HEARTBEAT_MAX_AGE_S)
        with self._active_lock:
            claims = {job_id: at for job_id, at in self._active.items() if at > horizon}
        if not claims:
            return 0
        session = self.session_factory()
        try:
            rows = session.execute(
                select(TranscriptionJob.id, TranscriptionJob.started_at)
                .where(TranscriptionJob.id.in_(list(claims)), TranscriptionJob.status == "running")
                .with_for_update(key_share=True, skip_locked=True)
            ).all()
            mine = [row.id for row in rows if row.started_at == claims.get(row.id)]
            if mine:
                session.execute(
                    update(TranscriptionJob)
                    .where(TranscriptionJob.id.in_(mine))
                    .values(locked_at=func.now())
                    .execution_options(synchronize_session=False)
                )
            session.commit()
            return len(mine)
        except Exception:  # noqa: BLE001 - a missed beat is harmless; the next one retries
            session.rollback()
            log.exception("stt: heartbeat failed")
            return 0
        finally:
            session.close()

    def _heartbeat_loop(self, stop_event: threading.Event, interval_s: float = HEARTBEAT_S) -> None:
        while not stop_event.wait(interval_s):
            self.heartbeat()

    def _log_gauge(self, session: Session) -> None:
        """One queue line a minute: the operator's view of a migration in progress."""
        now = time.monotonic()
        if now - self._gauge_at < GAUGE_LOG_S:
            return
        self._gauge_at = now
        try:
            view = gauge(session, self.settings)
        except Exception:  # noqa: BLE001 - a log line is never worth a failed claim
            return
        log.info(
            "stt queue: live=%d backlog=%d audio=%.0fs eta=%.0fs (backlog threads %d/%d)",
            view.live,
            view.backlog,
            view.audio_seconds,
            view.eta_seconds,
            len(self._backlog_held),
            self.backlog_limit,
        )

    # ------------------------------------------------------------------ claim

    def claim(self) -> Job | None:
        """Take the next due job, or ``None`` when the queue is empty."""
        session = self.session_factory()
        try:
            self._log_gauge(session)
            with self._backlog_lock:
                include_backlog = len(self._backlog_held) < self.backlog_limit
            row = session.execute(
                _claim_sql(include_backlog=include_backlog), {"worker": self.name}
            ).first()
            if row is None:
                session.rollback()
                return None
            # The claim statement takes the *job* row's lock — that is what claiming is —
            # so the transaction that took it ends here, before this one goes anywhere
            # near the call.  Everything below re-opens with the call row locked first,
            # which is the one lock order these two tables are ever taken in (§5.2,
            # :func:`app.db.lock_calls`).  A process that dies in the gap leaves a
            # ``running`` row with a stale ``locked_at``, which is exactly what
            # :func:`requeue_stale` already exists to pick up.
            session.commit()
            if not lock_calls(session, row.call_id):  # pragma: no cover - FK cascade
                session.execute(
                    update(TranscriptionJob)
                    .where(TranscriptionJob.id == row.id)
                    .values(status="failed", last_error="call row is gone", finished_at=datetime.now(UTC))
                )
                session.commit()
                return None
            # Re-read the job under the call's lock.  The audio pass cancels the
            # transcription of a call it has just found no speech in, and it may have done
            # so in the gap above — in which case this job is finished and there is nothing
            # here to do, not even an error: the pass that cancelled it already wrote why.
            status_now = session.scalar(
                select(TranscriptionJob.status).where(TranscriptionJob.id == row.id)
            )
            if status_now != "running":
                log.info(
                    "stt: job %s was %s before it could start — leaving it alone",
                    row.id,
                    status_now or "deleted",
                )
                session.commit()
                return None
            call = session.execute(
                select(
                    Call.id,
                    Call.file_path,
                    Call.duration_s,
                    Call.talkgroup_alias_at_record,
                    Call.audio_state,
                    Call.processed_path,
                    Call.audio_meta,
                ).where(Call.id == row.call_id)
            ).first()
            if call is None:  # pragma: no cover - FK cascade makes this unreachable
                session.execute(
                    update(TranscriptionJob)
                    .where(TranscriptionJob.id == row.id)
                    .values(status="failed", last_error="call row is gone", finished_at=datetime.now(UTC))
                )
                session.commit()
                return None
            # Nobody said anything.  The audio pass measures the seconds of speech in the
            # processed copy and writes ``audio_meta.content`` (§5.5), but it runs on its own
            # queue: transcription is enqueued at ingest, so this job may well have been
            # written before that measurement existed.  Hence the *re-check here*, when the
            # job is claimed, rather than a filter at enqueue time — by now the pass has
            # usually landed, and a mic key-up never reaches Whisper, which would spend a
            # GPU second on it and hand back a hallucinated "Thank you."
            meta = call.audio_meta or {}
            if meta.get("content") in NO_SPEECH_CONTENTS:
                log.info(
                    "stt: job %s skipped — %s in call %s",
                    row.id,
                    meta.get("content"),
                    call.id,
                )
                session.execute(
                    update(TranscriptionJob)
                    .where(TranscriptionJob.id == row.id)
                    .values(
                        # ``cancelled``, not a new status: ``job_status`` is a Postgres enum
                        # and this is exactly the situation ``empty_clip_meta`` already
                        # cancels a job for — there is nothing to transcribe.  The *call*
                        # says ``skipped``, which is the vocabulary the UI reads.
                        status="cancelled",
                        # ``static`` says which of the two it was: a mic key-up with
                        # nothing behind it and a channel of broadband noise are both
                        # untranscribable, and they are different faults.
                        last_error=(
                            STATIC_REASON
                            if meta.get("content") == CONTENT_STATIC
                            else NO_SPEECH_REASON
                        ),
                        finished_at=datetime.now(UTC),
                        locked_by=None,
                        locked_at=None,
                    )
                )
                session.execute(
                    update(Call)
                    .where(Call.id == call.id)
                    .values(transcript_status=call_status("skipped"))
                    .execution_options(synchronize_session=False)
                )
                session.commit()
                return None

            # The audio has to be there before a backend is asked to transcribe it: a
            # purged or archived-away recording would otherwise be a request that fails
            # `max_attempts` times before anybody sees why.
            try:
                # ``audio_root`` may be overridden per worker (the tests do); the rest of
                # the containment rule — the archive root — comes from the live settings.
                where = self.runtime().model_copy(update={"audio_root": self.audio_root})
                # The processed copy when there is one (§5.5): it is the same speech with
                # the dead air gone, the tones down and the level even, which is exactly
                # what a feature extractor wants.  Falls back to the original whenever the
                # processing pass has not run, failed, or was switched off.
                path, processed = resolve_playback(call, where)
            except AudioUnavailable as exc:
                log.info("stt: job %s skipped — %s", row.id, exc.detail)
                session.execute(
                    update(TranscriptionJob)
                    .where(TranscriptionJob.id == row.id)
                    .values(
                        status="failed",
                        last_error=f"no audio: {exc.detail}",
                        finished_at=datetime.now(UTC),
                        locked_by=None,
                        locked_at=None,
                    )
                )
                session.execute(
                    update(Call)
                    .where(Call.id == call.id)
                    .values(transcript_status=call_status("skipped"))
                    .execution_options(synchronize_session=False)
                )
                session.commit()
                return None
            session.execute(
                update(Call)
                .where(Call.id == call.id)
                .values(transcript_status=call_status("running"))
                .execution_options(synchronize_session=False)
            )
            session.commit()
            if row.priority >= BACKLOG_BAND:
                # Taken only here, on the path that actually hands a job to a thread: the
                # early returns above finish the row themselves and hold no slot.  Given
                # back in complete()/fail(), which the runner calls for every job it takes.
                with self._backlog_lock:
                    self._backlog_held.add(row.id)
            with self._active_lock:
                self._active[row.id] = row.started_at
            return Job(
                id=row.id,
                call_id=call.id,
                path=str(path),
                priority=row.priority,
                talkgroup_alias=call.talkgroup_alias_at_record,
                duration_s=float(call.duration_s) if call.duration_s is not None else None,
                prefer=getattr(row, "backend_name", None) or None,
                processed=processed,
                claimed_at=row.started_at,
                without_prompt=_failed_on_the_recording(row.attempts, row.last_error),
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ options

    def _vocabulary(self) -> tuple[list[str], list[str]]:
        """Talkgroup names and unit identifiers, straight from the directory and the roster.

        The roster (``units``, ``personnel``) leads the unit list because it is curated:
        ``E1`` / ``Engine 1`` and the names of the people on the air are exactly the proper
        nouns whisper mangles, and they are worth more of the prompt's character budget than
        a radio alias ingest happened to record.  ``build_prompt`` trims from the end, so
        ordering *is* the priority.

        Only *active* personnel are offered: a retired name biases the decoder toward
        something that will not be said again, and the row is kept for history, not for
        this.
        """
        session = self.session_factory()
        try:
            talkgroups: list[str] = []
            for alias, group in session.execute(
                select(Talkgroup.alias, Talkgroup.group_name)
                .where(Talkgroup.is_hidden.is_(False))
                .order_by(Talkgroup.call_count.desc())
                .limit(_VOCAB_LIMIT)
            ).all():
                talkgroups.extend(v for v in (alias, group) if v)
            units: list[str] = []
            for callsign, name in session.execute(
                select(Unit.callsign, Unit.name).order_by(Unit.callsign).limit(_VOCAB_LIMIT)
            ).all():
                units.extend(v for v in (callsign, name) if v)
            units.extend(
                session.scalars(
                    select(Person.name)
                    .where(Person.active.is_(True))
                    .order_by(Person.name)
                    .limit(_VOCAB_LIMIT)
                ).all()
            )
            units.extend(
                v
                for row in session.execute(
                    select(Radio.unit_id, Radio.alias)
                    .where(Radio.unit_id.isnot(None) | Radio.alias.isnot(None))
                    .order_by(Radio.call_count.desc())
                    .limit(_VOCAB_LIMIT)
                ).all()
                for v in row
                if v
            )
            return talkgroups, units
        finally:
            session.close()

    def prompt(self, settings: Settings | None = None) -> str:
        """The domain prompt, rebuilt from the directory at most every ``VOCAB_TTL_S``."""
        settings = settings or self.settings
        extra = (settings.stt_prompt_extra, settings.stt_places)
        with self._vocab_lock:
            now = time.monotonic()
            fresh = now - self._prompt_at < VOCAB_TTL_S and self._prompt_extra == extra
            if self._prompt is not None and fresh:
                return self._prompt
            try:
                talkgroups, units = self._vocabulary()
            except Exception:  # noqa: BLE001 - a prompt is a nicety, not a reason to stop
                log.exception("stt: could not read the vocabulary; using places only")
                talkgroups, units = [], []
            self._prompt = build_prompt(
                talkgroup_aliases=talkgroups,
                unit_aliases=units,
                places=prompt_places(settings),
                extra=settings.stt_prompt_extra,
            )
            self._prompt_at = now
            self._prompt_extra = extra
            return self._prompt

    def runtime(self) -> Settings:
        """This worker's settings with the ``settings`` table layered on top.

        The language, the VAD flag, the word-timestamp flag and the extra prompt are all
        editable from the Admin page, and a transcription worker runs for weeks: reading
        them per job (behind the few-second cache in ``app.runtime_settings``) is what
        makes changing them mean anything before the next restart.
        """
        session = self.session_factory()
        try:
            return effective_settings(session, base=self.settings)
        except Exception:  # noqa: BLE001 - the database is the runner's problem, not ours
            log.exception("stt: could not read the settings table; using the process settings")
            return self.settings
        finally:
            session.close()

    def options_for(self, job: Job) -> TranscribeOptions:
        """The per-request options.  One prompt for every job; the filter is per job.

        A job running on a processed copy gets **no** normalize filter: the stored file has
        already been trimmed, gated and normalized to a target, and running ``speechnorm``
        over it again would expand what is already even and re-raise the noise floor the
        gate just pushed down.  ``stt_normalize_always`` forces the old behavior back for an
        operator who wants to A/B it.

        The prompt is left out of a retry after every backend failed on the recording
        (:attr:`~app.stt.runner.Job.without_prompt`).  The looping BOLO call, a 43 s
        BOLO: with the prompt, large-v3 transcribed the first 17 s and then repeated that
        text in 0.5 s segments for 268 s of server time; without it, the same request came
        back in 52 s with the whole broadcast.  Speaches offers no other lever (no
        ``condition_on_previous_text``, no token cap, a single temperature, so no fallback).
        """
        settings = self.runtime()
        skip = job.processed and not bool(getattr(settings, "stt_normalize_always", False))
        if job.without_prompt:
            log.info("stt: job %s (call %s) retries without the prompt", job.id, job.call_id)
        return TranscribeOptions(
            language=settings.stt_language or None,
            prompt=None if job.without_prompt else (self.prompt(settings) or None),
            vad=settings.stt_vad,
            word_timestamps=settings.stt_word_timestamps,
            normalize_filter=None if skip else ffmpeg_audio_filter(settings),
            hallucination_patterns=getattr(settings, "stt_hallucination_patterns", "") or "",
            timeout_s=request_timeout_s(job.duration_s),
        )

    # ------------------------------------------------------------------ complete

    def complete(self, job: Job, result: TranscriptResult, quality: QualityAssessment) -> None:
        """Store the transcript, denormalize it onto the call and finish the job."""
        self._release_backlog(job.id)
        settings = self.runtime()
        session = self.session_factory()
        try:
            # calls before transcription_jobs, the one lock order (§5.2).
            lock_calls(session, job.call_id)
            # Ownership first, under the call's lock: every path that takes a running job
            # away — a forced re-transcribe, the audio pass finding the clip empty, the
            # stale reaper — locks the call too, so the answer cannot change under us.
            if not self._finish_owned(session, job, result):
                session.rollback()
                log.info(
                    "stt: job %s (call %s) is no longer this claim's — result from %s discarded",
                    job.id,
                    job.call_id,
                    result.backend_name,
                )
                return
            cleaned = quality.cleaned_text.strip()
            # The sanity guard: a transcript that cannot be speech is dropped here,
            # before it is denormalized onto the call and shown to anybody.  The raw text
            # still lands on the transcript row — nothing is ever discarded, so an operator
            # can see what the engine actually said — but the call keeps no text at all.
            probs = [s.no_speech_prob for s in result.segments if s.no_speech_prob is not None]
            hallucination = hallucination_reason(
                cleaned,
                job.duration_s,
                compile_patterns(getattr(settings, "stt_hallucination_patterns", "")),
                no_speech_prob=max(probs) if probs else None,
            )
            if hallucination is not None:
                log.warning(
                    "stt: call %s transcript rejected as a hallucination (%s): %d chars"
                    " over %.3f s — %r",
                    job.call_id,
                    hallucination,
                    len(cleaned),
                    job.duration_s or 0.0,
                    cleaned[:200],
                )
                activity.record(
                    session,
                    source="stt",
                    level="warn",
                    kind="stt.hallucination",
                    message=(
                        f"discarded a hallucinated transcript for call {job.call_id}"
                        f" ({hallucination}): {len(cleaned)} chars over"
                        f" {job.duration_s or 0.0:.3f} s"
                    ),
                    call_id=job.call_id,
                    meta={
                        "reason": hallucination,
                        "chars": len(cleaned),
                        "duration_s": job.duration_s,
                        "engine": result.engine,
                        "backend": result.backend_name,
                        "text": cleaned[:500],
                    },
                )
                quality = QualityAssessment(
                    quality=Quality.NOISE,
                    reasons=[*quality.reasons, f"hallucination:{hallucination}"],
                    cleaned_text="",
                )
                cleaned = ""
            segments = [s.model_dump() for s in result.segments]
            words = [w.model_dump() for w in result.words] if result.words else None

            # A human correction is the best text this call has, and a re-transcribe used
            # to demote it to make room for the engine's fresh guess (M7).  The new
            # transcript is still stored — the operator can compare them — but it only
            # becomes primary when the one it replaces was not edited by hand.
            edited = session.scalar(
                select(Transcript.id).where(
                    Transcript.call_id == job.call_id,
                    Transcript.is_primary.is_(True),
                    Transcript.edited_by_user.is_(True),
                )
            )
            becomes_primary = edited is None
            if becomes_primary:
                session.execute(
                    update(Transcript)
                    .where(Transcript.call_id == job.call_id, Transcript.is_primary.is_(True))
                    .values(is_primary=False)
                )
            transcript = Transcript(
                call_id=job.call_id,
                engine=result.engine,
                backend_name=result.backend_name,
                model=result.model,
                language=result.language,
                text_=result.text,
                segments=segments,
                words=words,
                avg_logprob=_mean(s.avg_logprob for s in result.segments),
                no_speech_prob=_mean(s.no_speech_prob for s in result.segments),
                compression_ratio=_mean(s.compression_ratio for s in result.segments),
                quality=str(quality.quality),
                quality_reasons=list(quality.reasons),
                prompt_used=None if job.without_prompt else (self.prompt(settings) or None),
                vad_used=settings.stt_vad,
                processing_ms=result.processing_ms,
                is_primary=becomes_primary,
            )
            session.add(transcript)
            session.flush()

            status = "empty" if (quality.quality is Quality.NOISE or not cleaned) else "done"
            values: dict[str, Any] = {"transcript_status": status}
            if becomes_primary:
                values["transcript_text"] = cleaned or None
                values["transcript_quality"] = str(quality.quality)
            else:
                status = "done"  # the call already has text a person stands behind
                values["transcript_status"] = status
            session.execute(update(Call).where(Call.id == job.call_id).values(**values))
            notify(
                session,
                "call.transcribed",
                {
                    "id": job.call_id,
                    "job_id": job.id,
                    "transcript_id": transcript.id,
                    "status": status,
                    "quality": str(quality.quality),
                    "backend": result.backend_name,
                    "processing_ms": result.processing_ms,
                },
            )
            # Beside the bus event and in the same transaction as the transcript, so a
            # consumer is told about text that actually committed (app/webhooks.py).
            from app import webhooks  # noqa: PLC0415 - avoids an import cycle at module load

            webhooks.enqueue_call_event(session, "call.transcribed", job.call_id)
            activity.record(
                session,
                source="transcribe",
                level="info",
                kind="transcribe.done",
                message=(
                    f"transcribed call {job.call_id} via {result.engine}"
                    f" ({status}, {result.processing_ms} ms)"
                ),
                call_id=job.call_id,
                meta={
                    "engine": result.engine,
                    "backend": result.backend_name,
                    "model": result.model,
                    "status": status,
                    "quality": str(quality.quality),
                    "processing_ms": result.processing_ms,
                },
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _owned(job: Job) -> list[Any]:
        """The predicate for "this row is still the job this claim took"."""
        conditions: list[Any] = [TranscriptionJob.id == job.id, TranscriptionJob.status == "running"]
        if job.claimed_at is not None:
            conditions.append(TranscriptionJob.started_at == job.claimed_at)
        return conditions

    def _finish_owned(self, session: Session, job: Job, result: TranscriptResult) -> bool:
        """Mark the job ``done`` if this claim still owns it.  ``False`` when it does not."""
        row = session.execute(
            update(TranscriptionJob)
            .where(*self._owned(job))
            .values(
                status="done",
                finished_at=datetime.now(UTC),
                backend_name=result.backend_name,
                model=result.model,
                last_error=None,
                locked_by=None,
                locked_at=None,
            )
            .returning(TranscriptionJob.id)
            .execution_options(synchronize_session=False)
        ).first()
        return row is not None

    # ------------------------------------------------------------------ fail

    def fail(self, job: Job, exc: Exception) -> None:
        """Count the attempt and requeue with backoff, or park the job as ``failed``."""
        self._release_backlog(job.id)
        session = self.session_factory()
        try:
            # calls before transcription_jobs (§5.2).  Taken explicitly because the job row
            # below is mutated through the ORM with ``autoflush=False``: its UPDATE lands at
            # commit, *after* the ``update(Call)`` statements, so the write order alone does
            # not settle which row this transaction locks first.
            lock_calls(session, job.call_id)
            # Only a job this claim still owns (see complete()): a late failure of a job
            # that was cancelled or re-claimed must not requeue or park somebody else's row.
            row = session.scalar(
                select(TranscriptionJob).where(*self._owned(job)).with_for_update(key_share=True)
            )
            if row is None:
                session.rollback()
                log.info(
                    "stt: job %s (call %s) is no longer this claim's — failure discarded: %s",
                    job.id,
                    job.call_id,
                    exc,
                )
                return
            row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            row.locked_by = None
            row.locked_at = None
            if isinstance(exc, NoBackendEnabledError):
                # Nothing failed — there was nowhere to send the work, because every backend
                # is disabled or disqualified.  Counting an attempt here would park the whole
                # queue as ``failed`` within three polls of an operator switching the last
                # backend off, so the job waits instead and is retried when one comes back.
                row.status = "queued"
                row.scheduled_at = datetime.now(UTC) + timedelta(seconds=NO_BACKEND_RETRY_S)
                row.started_at = None
                session.execute(
                    update(Call)
                    .where(Call.id == job.call_id)
                    .values(transcript_status=call_status("queued"))
                    .execution_options(synchronize_session=False)
                )
                log.warning(
                    "stt: no backend is enabled; job %s (call %s) waits %ds",
                    job.id,
                    job.call_id,
                    NO_BACKEND_RETRY_S,
                )
                session.commit()
                return
            row.attempts += 1
            if row.attempts >= row.max_attempts:
                row.status = "failed"
                row.finished_at = datetime.now(UTC)
                # Why it is parked, not just that it is (§5.2).  ``NoBackendAvailableError``
                # is the router saying *every* backend failed — an outage, which says
                # nothing about this recording — so the row is parked (the operator must
                # still be able to see the failure) but marked as recoverable, and
                # :func:`unpark_backend_outage` requeues it once a backend is healthy again.
                # Every other exception is a verdict on the content and stays NULL, because
                # retrying it for ever would be a loop nobody asked for — and that includes
                # ``RequestFailedEverywhereError``, the router saying every backend reached
                # its server and failed on *this recording* while serving everybody else.
                # It is deliberately not a ``NoBackendAvailableError``.
                if isinstance(exc, NoBackendAvailableError):
                    if row.outage_requeues >= OUTAGE_REQUEUE_CAP:
                        # The backstop: this "outage" has already been requeued as many times
                        # as a real one ever needs.  Parked where the unpark pass will not
                        # look, so a misreading cannot go round for ever.
                        row.parked_reason = PARKED_OUTAGE_REQUEUE_CAP
                        log.warning(
                            "stt: job %s (call %s) failed like an outage after %d automatic"
                            " requeue(s); parked for good (%s)",
                            job.id,
                            job.call_id,
                            row.outage_requeues,
                            PARKED_OUTAGE_REQUEUE_CAP,
                        )
                    else:
                        row.parked_reason = PARKED_BACKEND_UNAVAILABLE
                session.execute(
                    update(Call)
                    .where(Call.id == job.call_id)
                    .values(transcript_status=call_status("failed"))
                    .execution_options(synchronize_session=False)
                )
                activity.record(
                    session,
                    source="transcribe",
                    level="error",
                    kind="transcribe.failed",
                    message=f"transcription failed for call {job.call_id}: {row.last_error}",
                    call_id=job.call_id,
                    meta={
                        "engine": row.backend_name,
                        "attempts": row.attempts,
                        "error": row.last_error,
                        "parked_reason": row.parked_reason,
                        "outage_requeues": row.outage_requeues,
                    },
                )
            else:
                row.status = "queued"
                row.scheduled_at = datetime.now(UTC) + timedelta(seconds=BACKOFF_BASE_S * 2**row.attempts)
                row.started_at = None
                session.execute(
                    update(Call)
                    .where(Call.id == job.call_id)
                    .values(transcript_status=call_status("queued"))
                    .execution_options(synchronize_session=False)
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def call_status(status: str) -> Any:
    """``status`` for the call — or ``done`` when a hand-typed caption is its primary transcript.

    A caption on a call nobody had transcribed becomes a primary ``manual`` transcript
    (:func:`app.api.calls.patch_call`) and the call is ``done``: it has text a person
    stands behind, and :meth:`SttWorker.complete` keeps that text primary whatever the
    engine says.  The job queued before the caption still runs, though, and every other
    write the worker makes to the call's status — ``running`` at the claim, ``queued`` or
    ``failed`` from :meth:`SttWorker.fail`, ``skipped`` when the audio says there is
    nothing to hear, ``queued`` from the requeue passes — is about that *job*.  Written
    as is, it would tell the UI a captioned call is failed or still waiting.

    Evaluated in the ``UPDATE`` itself, under the call's row lock the caller already holds,
    so a caption that lands between the read and the write cannot be missed.
    """
    captioned = (
        select(Transcript.id)
        .where(
            Transcript.call_id == Call.id,
            Transcript.is_primary.is_(True),
            Transcript.engine == "manual",
        )
        .exists()
    )
    return cast(
        case((captioned, literal("done")), else_=literal(status)),
        TranscriptStatusEnum,
    )


def _failed_on_the_recording(attempts: int, last_error: str | None) -> bool:
    """Did the previous attempt fail on every backend because of the recording itself?

    Read off what :meth:`SttWorker.fail` wrote (``"<ExceptionName>: <message>"``), so an
    operator's requeue, which clears ``last_error``, gives the prompt back.
    """
    return attempts > 0 and (last_error or "").startswith(f"{RequestFailedEverywhereError.__name__}:")


def _mean(values: Any) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    return sum(numbers) / len(numbers) if numbers else None


# --------------------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------------------


def requeue_stale(session: Session, older_than_minutes: int | None = None) -> int:
    """Requeue jobs left ``running`` by a worker that died.  Returns how many moved.

    A live worker refreshes ``locked_at`` every :data:`HEARTBEAT_S` while it transcribes
    (:meth:`SttWorker.heartbeat`), so a row stale by ``stt_stale_minutes`` belongs to a
    process that has stopped beating — or to a claim past :data:`HEARTBEAT_MAX_AGE_S`.  If
    the old thread does finish after all, its result is discarded: the row is no longer its
    claim.
    """
    minutes = older_than_minutes if older_than_minutes is not None else get_settings().stt_stale_minutes
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    # The calls first, then their jobs (§5.2): the ids are read without locking, the call
    # rows are locked, and only then is ``transcription_jobs`` written.  A job that stops
    # being stale in between is simply not updated by the statement below.
    stale = list(
        session.execute(
            select(TranscriptionJob.id, TranscriptionJob.call_id).where(
                TranscriptionJob.status == "running",
                TranscriptionJob.locked_at.is_not(None),
                TranscriptionJob.locked_at < cutoff,
            )
        )
    )
    if not stale:
        session.commit()
        return 0
    lock_calls(session, [r.call_id for r in stale])
    rows = (
        session.execute(
            update(TranscriptionJob)
            .where(
                TranscriptionJob.id.in_([r.id for r in stale]),
                TranscriptionJob.status == "running",
                TranscriptionJob.locked_at.is_not(None),
                TranscriptionJob.locked_at < cutoff,
            )
            .values(
                status="queued",
                locked_by=None,
                locked_at=None,
                started_at=None,
                scheduled_at=datetime.now(UTC),
                last_error="requeued: worker lock went stale",
            )
            .returning(TranscriptionJob.call_id)
        )
        .scalars()
        .all()
    )
    if rows:
        session.execute(
            update(Call)
            .where(Call.id.in_(list(rows)))
            .values(transcript_status=call_status("queued"))
            .execution_options(synchronize_session=False)
        )
        log.warning("stt: requeued %d stale job(s) older than %d min", len(rows), minutes)
    session.commit()
    return len(rows)


def requeue_failed(
    session: Session,
    *,
    job_ids: Sequence[int] | None = None,
    parked_reason: str | None = None,
    limit: int | None = None,
    error_prefix: str | None = None,
    automatic: bool = False,
) -> int:
    """Move ``failed`` jobs back to the front of the queue.  Returns how many moved.

    The one place a parked job is un-parked, shared by the operator's
    ``POST /api/admin/jobs/requeue`` and by the automatic pass that follows a backend
    outage, so the two cannot drift apart.  The differences between the two callers are the
    arguments and nothing else:

    ``job_ids``
        restrict to these rows (the admin route's ``{"job_ids": [...]}`` body); with none
        given, every failed job qualifies.
    ``parked_reason``
        restrict to rows parked for this reason — the automatic pass asks only for
        :data:`PARKED_BACKEND_UNAVAILABLE`, because a job that failed on its *content* would
        simply fail again.
    ``limit``
        take at most this many, oldest first, so one pass cannot rewrite a hundred thousand
        rows in a single transaction.
    ``error_prefix``
        keep ``last_error`` and prefix it.  The operator's requeue clears the error — they
        have read it — while the automatic one keeps it and says who moved the row, so an
        operator looking at a call an hour later can still see what the outage looked like.
    ``automatic``
        the unpark pass: count the requeue in ``outage_requeues`` and skip a row that has
        already had :data:`OUTAGE_REQUEUE_CAP` of them.  A person's requeue is a fresh
        decision and resets the count to zero.

    **One row per call, and never beside an open job.**  ``ix_transcription_jobs_open_call``
    allows one queued-or-running job per call, and a call easily has two failed rows (a
    forced re-transcribe during an outage).  Requeuing both aborted the whole batch — and
    the automatic pass, which retries the same oldest rows every two minutes, then never
    recovered anything again.  So the newest failed row of each call is taken, and a call
    that already has a job open is skipped (the audio queue's rule,
    :func:`app.audioproc.jobs.requeue_failed`).

    Runs in the caller's session and does **not** commit: the activity row the automatic
    pass writes has to land in the same transaction as the requeue.
    """
    open_job = aliased(TranscriptionJob)
    has_open_job = (
        select(open_job.id)
        .where(open_job.call_id == TranscriptionJob.call_id, open_job.status.in_(("queued", "running")))
        .exists()
    )
    newest = (
        select(TranscriptionJob.id, TranscriptionJob.call_id)
        .ext(distinct_on(TranscriptionJob.call_id))
        .where(TranscriptionJob.status == "failed", ~has_open_job)
        .order_by(TranscriptionJob.call_id, TranscriptionJob.id.desc())
    )
    if job_ids is not None:
        ids = [int(i) for i in job_ids]
        if not ids:
            return 0
        newest = newest.where(TranscriptionJob.id.in_(ids))
    if parked_reason is not None:
        newest = newest.where(TranscriptionJob.parked_reason == parked_reason)
    if automatic:
        newest = newest.where(TranscriptionJob.outage_requeues < OUTAGE_REQUEUE_CAP)
    picked = newest.subquery()
    stmt = select(picked.c.id, picked.c.call_id).order_by(picked.c.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    selected_rows = list(session.execute(stmt))
    selected = [int(r.id) for r in selected_rows]
    if not selected:
        return 0
    # calls first, then transcription_jobs (§5.2).
    lock_calls(session, [r.call_id for r in selected_rows])
    last_error: Any = None
    if error_prefix:
        last_error = func.concat(error_prefix, func.coalesce(TranscriptionJob.last_error, ""))
    call_ids = (
        session.execute(
            update(TranscriptionJob)
            # Re-checked under the call locks: a job opened since the read above would
            # otherwise still trip the unique index.
            .where(TranscriptionJob.id.in_(selected), TranscriptionJob.status == "failed", ~has_open_job)
            .values(
                status="queued",
                attempts=0,
                outage_requeues=(TranscriptionJob.outage_requeues + 1) if automatic else 0,
                last_error=last_error,
                parked_reason=None,
                locked_by=None,
                locked_at=None,
                started_at=None,
                finished_at=None,
                scheduled_at=datetime.now(UTC),
            )
            .returning(TranscriptionJob.call_id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    if call_ids:
        session.execute(
            update(Call)
            .where(Call.id.in_(list(call_ids)))
            .values(transcript_status=call_status("queued"))
            .execution_options(synchronize_session=False)
        )
    return len(call_ids)


def a_backend_is_healthy(session: Session) -> bool:
    """Is at least one STT backend enabled *and* healthy right now?

    Read from exactly what ``GET /api/admin/stt/backends`` serves — the ``stt_backend_stats``
    rows the transcription worker publishes every ten seconds, folded over the configuration
    by :func:`app.api.admin_config.backend_rows` — so the automatic unpark and the operator
    staring at the admin page are looking at the same answer.  Nothing here probes the
    network: this process is not the one that transcribes, an outage is precisely when a
    probe would hang, and the worker's own verdict is fresher than anything a probe on a
    two-minute tick could learn.

    A ``stale`` row is not evidence: it describes a worker that has stopped publishing, and
    requeuing the queue at a worker nobody can hear would park it all over again.
    """
    from app.api import admin_config  # noqa: PLC0415 - avoids an import cycle at module load

    try:
        rows = admin_config.backend_rows(session)
    except Exception:  # noqa: BLE001 - an unreadable table means "not now", not a crash
        log.exception("stt: could not read the backend health rows")
        return False
    return any(row.enabled and row.healthy and not row.stale for row in rows)


def unpark_backend_outage(session: Session, *, limit: int = UNPARK_BATCH) -> int:
    """Requeue the jobs an STT outage parked, once a backend is answering again.

    Before this existed, an outage was a two-part failure: every job in flight burned its
    three attempts against a backend that was down, and *then* nothing ever looked at those
    rows again.  The queue drained itself into ``failed`` and stayed there — 395 jobs on
    2026-09-07 — until an operator noticed and requeued them by hand.

    So the park stays (a failure the operator cannot see is worse), and it is annotated:
    :meth:`SttWorker.fail` stamps :data:`PARKED_BACKEND_UNAVAILABLE` on a job whose last
    error was the router reporting that every backend failed, and this pass moves exactly
    those back when :func:`a_backend_is_healthy` says there is somewhere to send them.  A
    job parked on its *content* has no reason recorded and is never touched: it would fail
    again, and re-parking it every two minutes is a loop, not a recovery.

    Each job gets at most :data:`OUTAGE_REQUEUE_CAP` of these requeues, counted in
    ``outage_requeues``, because this pass resets ``attempts``: a recording misread as an
    outage would otherwise go round for ever (the looping BOLO call).
    """
    if not a_backend_is_healthy(session):
        return 0
    moved = requeue_failed(
        session,
        parked_reason=PARKED_BACKEND_UNAVAILABLE,
        limit=limit,
        error_prefix=AUTO_REQUEUE_PREFIX,
        automatic=True,
    )
    if moved:
        log.info("stt: requeued %d job(s) parked while every backend was failing", moved)
        activity.record(
            session,
            source="transcribe",
            level="info",
            kind="transcribe.unparked",
            message=f"{moved} jobs requeued after the STT outage ended",
            meta={"jobs": moved, "reason": PARKED_BACKEND_UNAVAILABLE},
        )
    session.commit()
    return moved


# --------------------------------------------------------------------------------------
# process entry
# --------------------------------------------------------------------------------------


def build_router(
    settings: Settings | None = None,
    *,
    session_factory: SessionFactory | None = SessionLocal,
    fallback_to_env: bool = True,
) -> BackendRouter:
    """Instantiate the configured backends behind one router.

    The backend list comes from the ``stt_backends`` table when it has any rows and from
    ``STT_BACKENDS`` when it does not — the one precedence rule, applied in
    :func:`app.stt.config_store.entries` and nowhere else.

    Backends marked disabled are built too, and handed to the router as its
    ``config_disabled`` set: the operator can switch one on from the admin page without a
    restart, and the router simply does not route to it until they do.  The override reader
    is what makes that click reach this process (:mod:`app.stt.stats_store`).

    ``fallback_to_env`` is true only at start-up, where a router on the environment's
    backends beats no router at all.  A *reload* passes false: see
    :func:`app.stt.config_store.entries_for`.
    """
    settings = settings or get_settings()
    entries = entries_for(session_factory, settings, fallback_to_env=fallback_to_env)
    backends = build_backends(entries, include_disabled=True)
    if not backends:
        raise RuntimeError("no STT backend is configured (stt_backends is empty and so is STT_BACKENDS)")
    disabled = {
        (entry.get("name") or entry.get("kind"))
        for entry in entries
        if entry.get("enabled") is False
    }
    provider = override_provider(session_factory) if session_factory is not None else None
    return BackendRouter(backends, config_disabled=disabled, override_provider=provider)


def build_router_handle(
    settings: Settings | None = None, *, session_factory: SessionFactory | None = SessionLocal
) -> RouterHandle:
    """A :class:`~app.stt.hotswap.RouterHandle` around a fresh router, stamped with the
    configuration version it was built from.

    Reading the version *before* the rows would be the wrong order: a save landing between
    the two would leave the handle claiming to be at a version whose rows it never saw, and
    the reloader — which only ever compares — would never correct it.  Read after, and the
    worst case is one redundant rebuild on the next tick.
    """
    router = build_router(settings, session_factory=session_factory)
    applied = 0
    if session_factory is not None:
        session = session_factory()
        try:
            applied = config_version(session)
        except Exception:  # noqa: BLE001 - an unreadable version means "reload on the first tick"
            log.exception("stt: could not read the configuration version at start-up")
        finally:
            session.close()
    return RouterHandle(router, config_version=applied)


def run_stt_worker(
    stop_event: threading.Event,
    *,
    workers: int | None = None,
    settings: Settings | None = None,
    router: BackendRouter | RouterHandle | None = None,
    session_factory: SessionFactory = SessionLocal,
    poll_s: float = IDLE_POLL_S,
    bell: wake.Doorbell | None = wake.STT,
) -> None:
    """Transcribe until ``stop_event`` is set.  Blocking; run me in a thread or a process.

    Idle threads wait on ``bell`` (rung for every new live call, :mod:`app.wake`) and fall
    back to claiming every ``poll_s``.  ``bell=None`` is the old fixed poll.
    """
    settings = settings or get_settings()
    # A handle rather than the router itself, so the operator's backend edits can be picked
    # up without a restart: the reloader below swaps a new router in behind it and every
    # consumer here — the job runner, the queue gauge, the publisher — follows automatically
    # (:mod:`app.stt.hotswap`).  A router passed in by a test is wrapped just the same.
    handle = (
        RouterHandle(router)
        if router is not None and not isinstance(router, RouterHandle)
        else router or build_router_handle(settings, session_factory=session_factory)
    )
    count = workers if workers is not None else settings.stt_workers
    # The queue gauge the upload path decides on estimates its ETA from real-time factors;
    # this process is the only one that measures any, so lend it the router.
    set_router(handle)
    worker = SttWorker(
        router=handle, settings=settings, session_factory=session_factory, worker_count=count
    )
    log.info(
        "stt worker: %d thread(s) over backends %s, %d of them may take backlog jobs",
        count,
        ", ".join(b.name for b in handle.backends),
        worker.backlog_limit,
    )
    # The counters this process is about to run up are invisible to the API process, which
    # is where the admin page asks; the publisher is the only reason that page shows work.
    # Its tick doubles as the reload check — see RouterReloader for why they share a beat.
    reloader = RouterReloader(
        handle,
        session_factory,
        # No env fallback on a reload: a database that cannot be read must leave the
        # running router alone, not swap the operator's rows out for the host's .env.
        lambda: build_router(settings, session_factory=session_factory, fallback_to_env=False),
    )
    publisher = StatsPublisher(
        handle,
        session_factory,
        worker_instance=f"{socket.gethostname()}:{os.getpid()}",
        on_tick=reloader.poll,
    )
    handle.on_health_change = publisher.wake
    publisher.start()
    # Keeps ``locked_at`` fresh on the jobs this process is working on, so the stale reaper
    # never takes a job that is merely slow.  Stops with the runner, which sets stop_event.
    threading.Thread(
        target=worker._heartbeat_loop, args=(stop_event,), name="stt-heartbeat", daemon=True
    ).start()
    try:
        runner = run_jobs(
            worker.claim,
            worker.complete,
            worker.fail,
            handle,
            worker.options_for,
            workers=count,
            poll_s=poll_s,
            stop_event=stop_event,
            wake=bell,
        )
    finally:
        publisher.stop()
    log.info("stt worker: stopped after %d done, %d failed", runner.processed, runner.failed)
