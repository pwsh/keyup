"""The ``audio_jobs`` queue and the thread that drains it (ARCHITECTURE §5.5).

Structurally the transcription worker's smaller sibling: one claim statement with
``FOR UPDATE SKIP LOCKED``, one row marked ``running`` in the same statement, exponential
backoff on failure and a park at ``max_attempts``.  Two differences are deliberate.

**It has a table of its own.**  ``transcription_jobs`` is read by the admin jobs page, by
the flow gauge that decides whether an upload is admitted and by the drain estimate, and
every one of those means *transcription* by "a job".  A ``kind`` column would have made all
of them silently wrong until each was audited; a second table makes them right by
construction and costs one more claim query per poll.

**A failure is cheap.**  A call whose processing failed is a call that plays back raw,
which is exactly what happened before this stage existed — so the job parks and the
pipeline moves on.  Transcription is never blocked on it: :func:`enqueue` is called *before*
the transcription job, and the transcription worker uses ``processed_path`` only if it is
there.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import any_, func, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import wake
from app.audio import (
    PRESENT_STATES,
    AudioUnavailable,
    processed_path_for,
    prune_empty_dirs,
    resolve_audio,
)
from app.audioproc.chain import PIPELINE_VERSION, AudioConfig
from app.audioproc.process import (
    NO_CONTENT,
    AudioProcessError,
    empty_clip_reason,
    process_file,
)
from app.audioproc.profiles import ProfileError, render_steps
from app.config import Settings, get_settings
from app.db import SessionLocal, lock_calls
from app.models import AudioJob, AudioProfile, Call, Talkgroup, TranscriptionJob
from app.runtime_settings import effective_settings

log = logging.getLogger("app.audioproc.jobs")

__all__ = [
    "BACKOFF_BASE_S",
    "AudioProcessWorker",
    "RecordingMoved",
    "counts",
    "empty_clip_meta",
    "enqueue",
    "enqueue_many",
    "process_call",
    "profile_for",
    "requeue_failed",
    "requeue_stale",
    "run_audio_worker",
    "select_for_reprocess",
    "stale_processed",
]

SessionFactory = Callable[[], Session]

#: first retry 30 s after the first failure, then 60 s, 120 s… — the transcription
#: worker's schedule, for the same reason: a transient ffmpeg or disk problem clears in
#: seconds and a real one is not helped by hammering it.
BACKOFF_BASE_S = 30

#: How long the loop sleeps when the queue is empty and nothing rings it.  A new live
#: call rings :data:`app.wake.AUDIO` through the worker's LISTEN connection, so this is
#: only the fallback (backlog jobs, retries whose back-off has run out, a notification
#: lost while that connection reconnected).  It was the only way in, at three seconds.
IDLE_POLL_S = 10.0

#: a ``running`` audio job older than this has lost its worker and is requeued.  Its own
#: number, not the transcription queue's ten minutes: one pass can run about six
#: ffmpeg/ffprobe calls at up to 180 s each (:mod:`app.audioproc.process`), and nothing
#: refreshes ``locked_at`` while they run.
STALE_MINUTES = 30


def enqueue(
    session: Session,
    call: Call,
    settings: Settings,
    *,
    priority: int | None = None,
    force: bool = False,
) -> int | None:
    """Queue one call for processing, unless it already has an open job.

    Returns the new job id, or ``None`` when nothing was queued — the feature is off, the
    call has no audio to process, or a pass is already queued or running for it.  The
    partial unique index is what makes the last case a no-op rather than a race.
    """
    if not force and not bool(getattr(settings, "audio_process_enabled", True)):
        return None
    if (call.audio_state or "present") not in {"present", "archived"}:
        return None
    stmt = (
        pg_insert(AudioJob)
        .values(
            call_id=call.id,
            status="queued",
            priority=100 if priority is None else priority,
            max_attempts=int(getattr(settings, "audio_process_max_attempts", 3) or 3),
        )
        .on_conflict_do_nothing(
            index_elements=["call_id"],
            index_where=text("status IN ('queued', 'running')"),
        )
        .returning(AudioJob.id)
    )
    return session.execute(stmt).scalar_one_or_none()


#: Rows per ``INSERT``.  Not a tuning knob: PostgreSQL's wire protocol allows 65,535 bind
#: parameters in one statement and each row here spends four of them, so a single-statement
#: insert breaks at 16,384 calls.  ``keyup process-audio`` over a 16,834-call backlog is
#: exactly how that was found — the whole run died with an ``OperationalError`` and queued
#: nothing.  2,000 rows is 8,000 parameters, an eighth of the ceiling.
ENQUEUE_CHUNK = 2_000


def enqueue_many(
    session: Session,
    call_ids: list[int],
    *,
    priority: int = 600,
    max_attempts: int = 3,
    chunk_size: int = ENQUEUE_CHUNK,
    commit: bool = False,
    on_chunk: Callable[[int, int, int], None] | None = None,
) -> int:
    """Queue a backfill set at backlog priority.  Returns how many rows were added.

    600 rather than the talkgroup's own: a reprocess is never more urgent than the live
    traffic arriving while it runs, and the band matches the transcription queue's own
    backlog band so the two read alike in the admin pages.

    **Written in chunks of** :data:`ENQUEUE_CHUNK` — see that constant for why one
    statement is not an option.  ``commit`` commits each chunk as it lands, which a long
    CLI run wants: the insert is idempotent (the partial unique index on ``call_id`` for
    the open statuses makes a re-queue a no-op), so a run interrupted half way through has
    simply queued the first half and re-running it queues the rest.  ``on_chunk`` is
    called after each one with ``(rows queued so far, calls offered so far, total)``, which
    is how the command prints progress.
    """
    if not call_ids:
        return 0
    queued = 0
    total = len(call_ids)
    step = max(1, int(chunk_size))
    for start in range(0, total, step):
        batch = call_ids[start : start + step]
        rows = [
            {"call_id": cid, "status": "queued", "priority": priority, "max_attempts": max_attempts}
            for cid in batch
        ]
        stmt = (
            pg_insert(AudioJob)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["call_id"], index_where=text("status IN ('queued', 'running')")
            )
            .returning(AudioJob.id)
        )
        queued += len(session.execute(stmt).scalars().all())
        if commit:
            session.commit()
        if on_chunk is not None:
            on_chunk(queued, min(start + step, total), total)
    return queued


def counts(session: Session) -> dict[str, int]:
    """``{status: n}`` over the whole queue — what the Audio admin tab shows."""
    return {
        status: int(number)
        for status, number in session.execute(
            select(AudioJob.status, func.count()).group_by(AudioJob.status)
        ).all()
    }


_CLAIM = text(
    """
    WITH claimed AS (
        SELECT id
        FROM audio_jobs
        WHERE status = 'queued' AND scheduled_at <= now()
        ORDER BY priority ASC, scheduled_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE audio_jobs AS j
    SET status = 'running', locked_by = :worker, locked_at = now(), started_at = clock_timestamp()
    FROM claimed
    WHERE j.id = claimed.id
    RETURNING j.id, j.call_id, j.attempts, j.max_attempts, j.started_at
    """
)


def _relative(path: Path, settings: Settings) -> str:
    """``processed_path`` in the same terms ``file_path`` uses.

    Relative to ``AUDIO_ROOT`` for anything under it — which is what ingest writes and what
    :func:`app.audio.audio_path` re-joins — and absolute for anything under the retention
    archive, which is exactly the convention retention already established for ``file_path``
    when it moves a recording out of the tree.
    """
    root = Path(settings.audio_root).resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(root):
        return str(resolved.relative_to(root))
    return str(resolved)


def empty_clip_meta(session: Session, call: Call, reason: str) -> dict[str, Any]:
    """Record that this call's recording holds no audio, and take it out of the queues.

    trunkd emitted about forty of these a day: a 288-byte MP3 that is a LAME header with no
    frames behind it.  Every one became a *failed* audio job — three attempts, a park, and
    a red row in the admin page — for a call that is not broken in any way anybody can act
    on.  Nothing was recorded, so the pass has nothing to do and the job is **done**.

    ``transcript_status`` becomes ``empty``, the vocabulary the STT worker already uses for
    a clip that turned out to hold no speech (:mod:`app.stt.worker`), and any transcription
    job still open on the call is cancelled: there is no point handing Whisper a file with
    no frames, and left queued it would only fail there too.  A transcript that already
    exists is left alone — this says what the *audio* is, and does not erase a result
    somebody may have corrected by hand.
    """
    # The call row's lock first, then the job rows (§5.2, :func:`app.db.lock_calls`).  The
    # assignments below are ORM writes and the session is ``autoflush=False``, so they land
    # at commit — *after* the cancel statement — and taking the lock here is what keeps this
    # transaction on the one lock order those two tables are ever taken in.
    lock_calls(session, call.id)
    meta: dict[str, Any] = {"empty": True, "reason": reason}
    call.audio_meta = meta
    call.processed_path = None
    call.tone_detected = False
    call.alert = False
    # ``running`` too: the transcription job about to be cancelled below may be mid-flight,
    # and its late result is discarded (it no longer owns the job), so nothing else would
    # ever move the call off ``running``.
    if call.transcript_status in {"pending", "queued", "running", "failed", "skipped"}:
        call.transcript_status = "empty"
    session.execute(
        update(TranscriptionJob)
        .where(
            TranscriptionJob.call_id == call.id,
            TranscriptionJob.status.in_(("queued", "running")),
        )
        .values(
            status="cancelled",
            last_error=f"empty recording: {reason}",
            finished_at=datetime.now(UTC),
            locked_by=None,
            locked_at=None,
        )
    )
    log.info("audioproc: call %s holds no audio (%s) — marked empty", call.id, reason)
    return meta


def profile_for(session: Session, call: Call) -> tuple[list[str] | None, str | None]:
    """This call's talkgroup's filter stages and the profile's name, or ``(None, None)``.

    ``(None, None)`` — no talkgroup, no profile on it, or a chain that no longer renders —
    means the global chain, which is what every talkgroup got before profiles existed.  The
    *primary* talkgroup decides: a patch call is carried on several, and the one the call
    is filed under is the one whose noise it has.

    A stored chain that fails to render is logged and ignored rather than failing the job.
    It should be impossible — a chain is validated when it is written — but the alternative
    is a talkgroup whose every call parks in the queue because of one bad row, and a call
    processed by the global chain is a call an operator can still hear.
    """
    if call.talkgroup_id is None:
        return None, None
    row = session.execute(
        select(AudioProfile.name, AudioProfile.chain)
        .join(Talkgroup, Talkgroup.audio_profile_id == AudioProfile.id)
        .where(Talkgroup.id == call.talkgroup_id)
    ).first()
    if row is None:
        return None, None
    try:
        return render_steps(row.chain or []), str(row.name)
    except ProfileError:
        log.warning(
            "audioproc: audio profile %r does not render; call %s falls back to the global chain",
            row.name,
            call.id,
        )
        return None, None


class RecordingMoved(Exception):  # noqa: N818 - it is an outcome, not a fault
    """The recording was archived, purged or deleted while ffmpeg was running on it.

    There is nothing left to process and nothing to retry: the job ends ``cancelled``.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _keep_or_discard(
    session: Session,
    call: Call,
    target: Path,
    settings: Settings,
    before: tuple[str | None, str | None],
) -> None:
    """Lock the call and confirm the recording is still where it was when ffmpeg started.

    ffmpeg runs without the call lock — it takes seconds, and holding the row that long
    would stall uploads and reconcile on it.  Retention (and the discard pass, and a
    better-source replacement) can therefore move or delete the source meanwhile, and the
    encode will have re-created the pruned directory and put a ``.proc.mp3`` in the live
    tree that no row will ever point at again (A2).  So the row is locked *after* the
    encode and re-read: if its ``audio_state`` or ``file_path`` changed, the new file is
    deleted and nothing is written.  Holding the lock from here to the commit is what stops
    retention from moving it again between this check and the row write.

    Raises :class:`RecordingMoved` when the recording is gone from the live tree, and
    :class:`AudioProcessError` (a retry, which will resolve the new path) when it is still
    present somewhere else.
    """
    locked = lock_calls(session, call.id)
    now = (
        session.execute(
            select(Call.audio_state, Call.file_path).where(Call.id == call.id)
        ).one_or_none()
        if locked
        else None
    )
    if now is not None and (now.audio_state, now.file_path) == before:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - an orphan to report, not a reason to crash
        log.warning("audioproc: could not remove %s", target, exc_info=True)
    prune_empty_dirs(target.parent, settings)
    if now is None:
        raise RecordingMoved("the call was deleted while it was being processed")
    if (now.audio_state or "present") not in PRESENT_STATES or now.audio_state == "archived":
        raise RecordingMoved(
            f"the recording became {now.audio_state!r} while it was being processed; "
            "the processed copy was discarded"
        )
    raise AudioProcessError("the recording moved while it was being processed; retrying at its new path")


def process_call(
    session: Session,
    call: Call,
    settings: Settings,
    *,
    config: AudioConfig | None = None,
) -> dict[str, Any]:
    """Run the chain on one call and write the result back onto its row.

    Returns the meta it stored.  The row is *changed*, not committed: the caller finishes
    the job in the same transaction, so a pass that wrote a file but could not write its
    row does not leave a ``.proc.mp3`` nobody knows about **and** a job marked done.

    A decode that fails is asked one question before it is treated as a fault
    (:func:`app.audioproc.process.empty_clip_reason`): is there anything in the file at
    all?  When there is not, the call goes down the empty-clip path instead of the failure
    path — see :func:`empty_clip_meta`.

    The chain is the global one unless the call's talkgroup names an audio profile, which
    :func:`profile_for` resolves; the profile's name goes into ``audio_meta.profile`` beside
    the literal filter string, so a call that sounds wrong can be traced to the chain that
    made it.

    The Admin preview does not come through here — it calls
    :func:`app.audioproc.process.process_file` into a temporary directory, because "run the
    chain but do not touch anything" is a different operation, not a flag on this one.
    """
    cfg = config or AudioConfig.from_settings(settings)
    before = (call.audio_state, call.file_path)
    source = resolve_audio(call, settings)
    target = processed_path_for(source)
    profile, profile_name = profile_for(session, call)
    try:
        result = process_file(source, cfg, dst=target, profile=profile, profile_name=profile_name)
    except AudioProcessError:
        reason = empty_clip_reason(source)
        if reason is None:
            raise
        return empty_clip_meta(session, call, reason)
    # The copy is on disk; before the row may name it, the recording must still be where
    # it was when the encode started (A2).  From here to the commit the call is locked.
    _keep_or_discard(session, call, target, settings, before)
    call.processed_path = _relative(target, settings)
    call.audio_meta = result.meta
    call.tone_detected = result.tone_detected
    call.alert = result.alert
    # Nobody said anything, or nothing but static (``audio_meta.content``, §5.5).  The row
    # stays — the archive still knows this radio keyed up on this talkgroup at this second,
    # and that is the fact a pattern is read from — but there is no reason to keep half a
    # second of a click, or of noise, on disk,
    # so the call joins the ones the discard pass collects after their grace (§5.6).  Only
    # ever tightened here: a call already marked ``discard`` for its talkgroup's Record
    # toggle is left as it is, and a call with speech in it is never demoted.
    if (
        result.meta.get("content") in NO_CONTENT
        and bool(getattr(settings, "audio_discard_no_speech", True))
        and call.audio_policy != "discard"
    ):
        call.audio_policy = "discard"
        log.info(
            "audioproc: call %s holds no speech (%s) — its audio will be discarded",
            call.id,
            result.meta.get("content"),
        )
    return result.meta


class AudioProcessWorker:
    """One thread's worth of the queue: claim, process, complete or fail."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: SessionFactory = SessionLocal,
        worker_name: str | None = None,
        bell: wake.Doorbell | None = None,
        stt_bell: wake.Doorbell | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.name = worker_name or f"{socket.gethostname()}:{threading.get_ident()}"
        #: What an idle loop waits on (:mod:`app.wake`); ``None`` is the plain poll.
        self.bell = bell
        #: Rung after every pass: the call's transcription job was left for the processed
        #: copy (:data:`app.wake.AUDIO_FIRST`), and that copy now exists — or never will.
        self.stt_bell = stt_bell

    def runtime(self) -> Settings:
        session = self.session_factory()
        try:
            return effective_settings(session, base=self.settings)
        except Exception:  # noqa: BLE001 - a settings read must never stop the queue
            log.exception("audioproc: could not read the settings table; using the process settings")
            return self.settings
        finally:
            session.close()

    def _cancel_queued(self) -> None:
        """With the stage off, a queued job is a promise nobody will keep: cancel it.

        Jobs still arrive while the switch is off — an ingest path handed a settings object
        older than the switch, a reconcile that made a new primary — and left queued they
        do two wrong things: the queue reads as backlog, and the syndication feeds hold the
        call back for ever, since an open audio job is their "not finished yet" signal.
        One cheap UPDATE per idle cycle keeps both honest; switching the stage back on
        re-queues nothing, exactly as a call ingested while it was off is not processed.
        """
        session = self.session_factory()
        try:
            gone = session.execute(
                update(AudioJob)
                .where(AudioJob.status == "queued")
                .values(status="cancelled", last_error="audio processing is off", finished_at=func.now())
            ).rowcount
            session.commit()
            if gone:
                log.info("audio: cancelled %d queued job(s) — audio processing is off", gone)
        except Exception:  # noqa: BLE001 - housekeeping must never take the worker down
            log.exception("audio: could not cancel queued jobs")
            session.rollback()
        finally:
            session.close()

    def cycle(self) -> bool:
        """Claim and run at most one job.  ``True`` when one was taken."""
        settings = self.runtime()
        enabled = bool(getattr(settings, "audio_process_enabled", True))
        if self.stt_bell is not None:
            # Tells the LISTEN thread whether a new call's transcription should wait for
            # this pass (app/wake.py).  Only a worker that rings the transcription bell
            # when it is done may say so, or that transcription would wait for nothing.
            if enabled:
                wake.AUDIO_FIRST.set()
            else:
                wake.AUDIO_FIRST.clear()
        if not enabled:
            self._cancel_queued()
            return False
        session = self.session_factory()
        try:
            row = session.execute(_CLAIM, {"worker": self.name}).first()
            if row is None:
                session.rollback()
                return False
            session.commit()
            job_id, call_id = int(row.id), int(row.call_id)
        except Exception:
            session.rollback()
            session.close()
            raise
        else:
            session.close()
        try:
            self._run(job_id, call_id, int(row.attempts), int(row.max_attempts), settings, row.started_at)
        finally:
            if self.stt_bell is not None:
                self.stt_bell.ring()
        return True

    def _run(
        self,
        job_id: int,
        call_id: int,
        attempts: int,
        max_attempts: int,
        settings: Settings,
        claimed_at: datetime | None = None,
    ) -> None:
        session = self.session_factory()
        try:
            call = session.get(Call, call_id)
            if call is None:  # pragma: no cover - the FK cascade makes this unreachable
                self._finish(session, job_id, "failed", "call row is gone", claimed_at)
                return
            started = time.monotonic()
            meta = process_call(session, call, settings)
            # The call row before the job row, always (§5.2): ``process_call`` writes the
            # call through the ORM, and with ``autoflush=False`` that write would otherwise
            # wait for the commit below and land after ``audio_jobs``.
            session.flush()
            self._finish(session, job_id, "done", None, claimed_at)
            log.debug(
                "audioproc: call %s in %.2fs — gain %+.1f dB, trim %.2f/%.2f s, tones %s",
                call_id, time.monotonic() - started, meta.get("gain_db", 0.0),
                meta.get("head_trim_s", 0.0), meta.get("tail_trim_s", 0.0), meta.get("tone_pattern"),
            )  # fmt: skip
        except RecordingMoved as exc:
            # Retention (or a delete) got there while ffmpeg ran; the copy is already gone
            # and there is nothing left to process.
            session.rollback()
            log.info("audioproc: job %s cancelled — %s", job_id, exc.detail)
            self._finish(session, job_id, "cancelled", exc.detail, claimed_at)
        except AudioUnavailable as exc:
            # Not a retry: a purged or moved-away recording will not be there in 30 seconds
            # either, and three attempts on it only delays the queue behind it.  The STT
            # worker parks the same case for the same reason.
            session.rollback()
            log.info("audioproc: job %s skipped — %s", job_id, exc.detail)
            self._finish(session, job_id, "failed", f"no audio: {exc.detail}", claimed_at)
        except (AudioProcessError, OSError) as exc:
            session.rollback()
            self._fail(session, job_id, attempts, max_attempts, str(exc), claimed_at)
        except Exception as exc:  # noqa: BLE001 - a bad clip must not stop the queue
            session.rollback()
            log.exception("audioproc: job %s failed unexpectedly", job_id)
            self._fail(session, job_id, attempts, max_attempts, repr(exc), claimed_at)
        finally:
            session.close()

    @staticmethod
    def _owned(job_id: int, claimed_at: datetime | None) -> list[Any]:
        """The row is still ``running`` under the claim this thread made.

        ``started_at`` is the claim's token, as in the transcription queue
        (:mod:`app.stt.worker`).  A job the stale reaper requeued, or that somebody cancelled,
        is no longer this thread's to finish, and whatever this pass computed is dropped.
        """
        conditions: list[Any] = [AudioJob.id == job_id, AudioJob.status == "running"]
        if claimed_at is not None:
            conditions.append(AudioJob.started_at == claimed_at)
        return conditions

    def _write_owned(
        self, session: Session, job_id: int, claimed_at: datetime | None, values: dict[str, Any]
    ) -> None:
        """Apply ``values`` to the job if this claim still owns it, and commit — or roll the
        whole transaction back (the call-row writes of the pass included) if it does not."""
        owned = session.execute(
            update(AudioJob)
            .where(*self._owned(job_id, claimed_at))
            .values(**values)
            .returning(AudioJob.id)
            .execution_options(synchronize_session=False)
        ).first()
        if owned is None:
            session.rollback()
            log.info("audioproc: job %s is no longer this claim's — result discarded", job_id)
            return
        session.commit()

    def _finish(
        self,
        session: Session,
        job_id: int,
        status: str,
        error: str | None,
        claimed_at: datetime | None = None,
    ) -> None:
        self._write_owned(
            session,
            job_id,
            claimed_at,
            {
                "status": status,
                "last_error": error,
                "finished_at": datetime.now(UTC),
                "locked_by": None,
                "locked_at": None,
            },
        )

    def _fail(
        self,
        session: Session,
        job_id: int,
        attempts: int,
        max_attempts: int,
        detail: str,
        claimed_at: datetime | None = None,
    ) -> None:
        attempts += 1
        if attempts >= max_attempts:
            log.warning("audioproc: job %s parked after %d attempts: %s", job_id, attempts, detail[:200])
            values: dict[str, Any] = {
                "status": "failed",
                "attempts": attempts,
                "last_error": detail[:1000],
                "finished_at": datetime.now(UTC),
                "locked_by": None,
                "locked_at": None,
            }
        else:
            delay = BACKOFF_BASE_S * (2**attempts)
            values = {
                "status": "queued",
                "attempts": attempts,
                "last_error": detail[:1000],
                "scheduled_at": func.now() + text(f"interval '{delay} seconds'"),
                "locked_by": None,
                "locked_at": None,
                "started_at": None,
            }
        self._write_owned(session, job_id, claimed_at, values)

    def run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                # Snapshot before the claim, so a ring between an empty claim and the wait
                # is not lost (app/wake.py).
                seen = self.bell.seq if self.bell is not None else 0
                try:
                    worked = self.cycle()
                except Exception:  # noqa: BLE001 - the database went away; back off and retry
                    log.exception("audioproc: cycle failed")
                    worked = False
                if worked:
                    continue
                if self.bell is not None:
                    # Rung for a new live call.  With the stage off that is still worth a
                    # cycle: it cancels the call's queued job at once, and the syndication
                    # feeds hold a call back while it has an open audio job.
                    self.bell.wait(seen, IDLE_POLL_S, stop)
                else:
                    stop.wait(IDLE_POLL_S)
        finally:
            if self.stt_bell is not None:
                wake.AUDIO_FIRST.clear()


def requeue_failed(session: Session, *, limit: int = 100_000) -> int:
    """Put parked ``failed`` jobs back on the queue with a clean attempt count.

    The one-shot for a fault that has since been fixed in the code rather than in the
    recording — the 38 jobs parked on "could not find codec parameters" over trunkd's
    empty MP3s, which now finish ``done`` and mark the call empty instead.  Requeued at the
    backlog band, never in front of the live feed, and only where nothing is open for the
    call already, which the partial unique index would refuse anyway.
    """
    open_call_ids = select(AudioJob.call_id).where(AudioJob.status.in_(("queued", "running")))
    # One job per call: a call can park several failed rows (each retry-wave leaves one
    # behind), and requeueing two of them would trip the partial unique index on open
    # jobs.  The newest row is the one that carries the most recent error.
    ids = list(
        session.scalars(
            select(AudioJob.id)
            .ext(distinct_on(AudioJob.call_id))
            .where(AudioJob.status == "failed", AudioJob.call_id.not_in(open_call_ids))
            .order_by(AudioJob.call_id, AudioJob.id.desc())
            .limit(limit)
        )
    )
    if not ids:
        return 0
    result = session.execute(
        update(AudioJob)
        .where(AudioJob.id.in_(ids))
        .values(
            status="queued",
            attempts=0,
            last_error=None,
            scheduled_at=func.now(),
            started_at=None,
            finished_at=None,
            locked_by=None,
            locked_at=None,
        )
    )
    return int(result.rowcount or 0)


def requeue_stale(session: Session, older_than_minutes: int = STALE_MINUTES) -> int:
    """Put ``running`` jobs whose worker died back on the queue.  Returns how many.

    A worker that was merely slow and finishes after this has no claim left to finish:
    its ``_finish``/``_fail`` match nothing and its result is dropped.
    """
    result = session.execute(
        update(AudioJob)
        .where(
            AudioJob.status == "running",
            AudioJob.locked_at < func.now() - text(f"interval '{int(older_than_minutes)} minutes'"),
        )
        .values(status="queued", locked_by=None, locked_at=None, started_at=None)
    )
    session.commit()
    return int(result.rowcount or 0)


def run_audio_worker(
    stop: threading.Event,
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory = SessionLocal,
    bell: wake.Doorbell | None = wake.AUDIO,
    stt_bell: wake.Doorbell | None = wake.STT,
) -> None:
    """The worker thread's entry point.  One thread: ffmpeg is the bottleneck, not the queue.

    It waits on ``bell`` when idle and rings ``stt_bell`` after every pass (:mod:`app.wake`).
    """
    worker = AudioProcessWorker(
        settings=settings,
        session_factory=session_factory,
        bell=bell,
        stt_bell=stt_bell,
    )
    log.info("audioproc: worker started (pipeline v%d)", PIPELINE_VERSION)
    worker.run(stop)


def stale_processed(call: Call, settings: Settings) -> bool:
    """Whether this call's processed copy is missing or was made by an older pipeline."""
    meta = call.audio_meta or {}
    wanted = max(
        PIPELINE_VERSION, int(getattr(settings, "audio_reprocess_version", PIPELINE_VERSION) or 1)
    )
    if not call.processed_path:
        return True
    try:
        return int(meta.get("version") or 0) < wanted
    except (TypeError, ValueError):
        return True


def select_for_reprocess(
    session: Session,
    settings: Settings,
    *,
    since: datetime | None = None,
    talkgroup: int | None = None,
    talkgroups: list[int] | None = None,
    ids: list[int] | None = None,
    only_tone_flagged: bool = False,
    force: bool = False,
    limit: int = 100_000,
) -> list[int]:
    """The call ids a backfill should queue, newest first.

    Without ``force`` this is *lazy*: only calls with no processed copy, or one written by
    a pipeline older than :func:`stale_processed` considers current.  That is what makes
    "bump ``audio_reprocess_version`` and run the backfill" the whole reprocess procedure —
    a second run after a partial one picks up exactly where the first stopped, and running
    it against an archive that is already current queues nothing.

    ``only_tone_flagged`` narrows it to calls whose stored pass found a tone, which is the
    selection for a detector change: those are the rows whose ``tone_pattern``, ``alert``
    and attenuation may now be wrong.

    ``talkgroup`` takes one tgid, ``talkgroups`` a set of them — the selection an audio
    profile is applied over, where the caller has just assigned the profile to a handful of
    channels and wants their recent traffic re-rendered through it.

    Only ``present``/``archived`` rows: a purged call has no bytes to process.  ``limit``
    is a guard rail on a single invocation, not a page — a bigger archive is done by
    running the command again.
    """
    stmt = select(Call).where(Call.audio_state.in_(("present", "archived")))
    if since is not None:
        stmt = stmt.where(Call.started_at >= since)
    if talkgroup is not None:
        stmt = stmt.where(any_(Call.talkgroup_ids) == talkgroup)
    if talkgroups:
        # `overlap` (`&&`) rather than a chain of `any`: a patch call carries several
        # tgids, and the GIN index on `talkgroup_ids` answers the array-overlap operator
        # directly.  Applying a profile to a shift's worth of channels is one query.
        stmt = stmt.where(Call.talkgroup_ids.overlap(talkgroups))
    if ids:
        stmt = stmt.where(Call.id.in_(ids))
    if only_tone_flagged:
        # What a change to the tone detector has to be re-run over: every call the previous
        # pass heard something in.  A call it heard nothing in cannot have been attenuated
        # wrongly, so re-running the whole archive to find them costs ffmpeg passes for no
        # change — and the lazy version check still applies on top of this.
        stmt = stmt.where(Call.tone_detected.is_(True))
    stmt = stmt.order_by(Call.started_at.desc()).limit(limit)
    rows = session.scalars(stmt).all()
    return [call.id for call in rows if force or stale_processed(call, settings)]
