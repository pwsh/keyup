"""DB-agnostic transcription job loop.

The worker owns the *what* (claim a row from ``transcription_jobs``, write the transcript, mark
the job done); this module owns the *how many at once, in what order, and what happens when it
throws*.  Keeping the two apart means the whole loop is testable with plain callables and no
database — see ``backend/tests/stt/test_runner.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.wake import Doorbell

from .postprocess import assess
from .router import BackendRouter
from .types import QualityAssessment, TranscribeOptions, TranscriptResult

log = logging.getLogger(__name__)

__all__ = ["Job", "JobRunner", "run_jobs"]


@dataclass(slots=True)
class Job:
    """One unit of work, mirroring the columns the worker reads from ``transcription_jobs``."""

    id: int
    call_id: int
    path: str
    priority: int = 0
    talkgroup_alias: str | None = None
    duration_s: float | None = None
    prefer: str | None = None  # backend requested for this job (POST /calls/{id}/transcribe)
    #: ``path`` is the processed copy (§5.5), not the recorder's own file.  It has already
    #: been trimmed, gated and loudness-normalized, so the audio-prep pass must not
    #: normalize it a second time.
    processed: bool = False
    #: This claim's ownership token: the ``started_at`` the claim statement stamped on the
    #: row.  ``complete``/``fail`` write only while the row is still ``running`` under this
    #: exact claim — a job cancelled, reaped as stale or re-claimed in the meantime belongs
    #: to somebody else, and this thread's result is discarded.
    claimed_at: datetime | None = None
    #: send this attempt without the domain prompt: the last one failed on every backend on
    #: this recording (``RequestFailedEverywhereError``).  See ``SttWorker.options_for``.
    without_prompt: bool = False


ClaimFn = Callable[[], Job | None]
CompleteFn = Callable[[Job, TranscriptResult, QualityAssessment], None]
FailFn = Callable[[Job, Exception], None]
OptionsFn = Callable[[Job], TranscribeOptions]


class JobRunner:
    """Runs ``workers`` threads that each claim, transcribe and complete jobs until stopped."""

    def __init__(
        self,
        *,
        claim: ClaimFn,
        complete: CompleteFn,
        fail: FailFn,
        router: BackendRouter,
        options_for: OptionsFn,
        workers: int = 2,
        poll_s: float = 2.0,
        stop_event: threading.Event | None = None,
        wake: Doorbell | None = None,
    ) -> None:
        self.claim = claim
        self.complete = complete
        self.fail = fail
        self.router = router
        self.options_for = options_for
        self.workers = max(1, int(workers))
        self.poll_s = poll_s
        self.stop_event = stop_event or threading.Event()
        #: When given, an idle thread waits on this bell and ``poll_s`` is only the fallback
        #: (:mod:`app.wake`); without it the loop polls every ``poll_s`` as it always did.
        self.wake = wake
        # Claiming is the one shared step: two threads must not hand back the same row, and a
        # caller's claim() may well be a plain non-thread-safe function.
        self._claim_lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self._counter_lock = threading.Lock()

    def stop(self) -> None:
        self.stop_event.set()

    def _claim_one(self) -> Job | None:
        with self._claim_lock:
            return self.claim()

    def _handle(self, job: Job) -> None:
        try:
            # Inside the try: options are read from the database (settings, vocabulary), and
            # a raise here used to kill the thread and leave the job ``running`` — holding a
            # backlog slot — until the stale reaper noticed.
            options = self.options_for(job)
            result = self.router.transcribe(Path(job.path), options, prefer=job.prefer)
            quality = assess(result, job.duration_s, options)
        except Exception as exc:  # noqa: BLE001 - a bad job must never kill the worker thread
            log.warning("stt: job %s (call %s) failed: %s", job.id, job.call_id, exc)
            with self._counter_lock:
                self.failed += 1
            try:
                self.fail(job, exc)
            except Exception:  # noqa: BLE001 - the failure handler itself may be broken
                log.exception("stt: fail() raised for job %s", job.id)
            return
        with self._counter_lock:
            self.processed += 1
        try:
            self.complete(job, result, quality)
        except Exception:  # noqa: BLE001
            log.exception("stt: complete() raised for job %s", job.id)

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            # Snapshot before the claim, so a ring that lands between an empty claim and the
            # wait below still wakes this thread (app/wake.py).
            seen = self.wake.seq if self.wake is not None else 0
            try:
                job = self._claim_one()
            except Exception:  # noqa: BLE001 - a transient DB error must not end the loop
                log.exception("stt: claim() raised")
                job = None
            if job is None:
                if self.wake is not None:
                    # Only the *idle* wait changes: which job a woken thread may take is
                    # still decided by claim() — priority order and the backlog share.
                    self.wake.wait(seen, self.poll_s, self.stop_event)
                    if self.stop_event.is_set():
                        return
                # Event.wait doubles as the idle sleep so stop() is honoured immediately.
                elif self.stop_event.wait(self.poll_s):
                    return
                continue
            self._handle(job)

    def run(self) -> None:
        """Start the worker threads and block until ``stop_event`` is set."""
        threads = [
            threading.Thread(target=self._worker, name=f"stt-worker-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in threads:
            thread.start()
        try:
            while any(t.is_alive() for t in threads) and not self.stop_event.is_set():
                time.sleep(0.05)
        finally:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=30)


def run_jobs(
    claim: ClaimFn,
    complete: CompleteFn,
    fail: FailFn,
    router: BackendRouter,
    options_for: OptionsFn,
    *,
    workers: int = 2,
    poll_s: float = 2.0,
    stop_event: threading.Event | None = None,
    wake: Doorbell | None = None,
) -> JobRunner:
    """Run the transcription loop until ``stop_event`` is set; returns the runner for its counters."""
    runner = JobRunner(
        claim=claim,
        complete=complete,
        fail=fail,
        router=router,
        options_for=options_for,
        workers=workers,
        poll_s=poll_s,
        stop_event=stop_event,
        wake=wake,
    )
    runner.run()
    return runner
