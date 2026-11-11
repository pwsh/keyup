"""Priority classes and admission control for the push path (ARCHITECTURE §4.1, §5.1).

Two producers share one transcription queue: **trunkd**, pushing live calls that are only
worth transcribing while they are still news, and the **migration client**
(``tools/migrate_sdrtrunk.py``), pushing a decade of archived sdrtrunk recordings as fast
as the API will take them.  Without a policy the second buries the first — the queue is
FIFO within a priority and the backlog is unbounded — and the only signal a recorder gets
is a request that takes longer and longer to answer.

This module is that policy, in three pieces:

:func:`parse_priority` / :class:`PriorityChoice`
    the ``priority`` form field (or ``X-Keyup-Priority`` header) as a *class* — ``urgent``,
    ``live``, ``backlog``, ``bulk`` or a bare integer — and how it becomes the
    ``transcription_jobs.priority`` a call is enqueued at.  ``live`` deliberately resolves
    to *the talkgroup's own* priority, so today's behavior is exactly the default.
:func:`gauge`
    a cheap in-process view of the queue: how many jobs are open in each band, how many
    seconds of audio that is, and how long it will take to drain.  One SQL query, refreshed
    at most every :data:`GAUGE_TTL_S` seconds, so the hot upload path costs nothing.
:func:`admit`
    the decision, taken **before the audio is read off the wire**: store it, or refuse with
    ``429``/``507`` and a ``Retry-After`` the client can obey.

The band split is the whole trick.  A job at priority < :data:`BACKLOG_BAND` is *live or
better* and is only ever refused when the live queue is genuinely out of control; a job at
or above it is backlog and is refused early and often, because a refused backlog upload
costs nothing (the migration client retries) while a starved live call is gone.

Accepted backlog calls are **stored immediately** — searchable, playable, in the archive.
Only their transcription waits behind live traffic.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Call, TranscriptionJob

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

log = logging.getLogger("app.ingest.flow")

__all__ = [
    "BACKLOG_BAND",
    "FILTER_CLASSES",
    "GAUGE_TTL_S",
    "Admission",
    "PriorityChoice",
    "QueueCounts",
    "QueueGauge",
    "admit",
    "gauge",
    "headers",
    "invalidate",
    "parse_priority",
    "priority_class",
    "priority_class_clause",
    "set_router",
]

# --------------------------------------------------------------------------------------
# priority classes
# --------------------------------------------------------------------------------------

#: A job at or above this priority is *backlog*: it may be starved by live traffic for as
#: long as live traffic lasts.  Below it is live-or-better and is never deliberately held.
BACKLOG_BAND = 500

#: ``transcription_jobs.priority`` is a small positive integer, 1 first.
MIN_PRIORITY = 1
MAX_PRIORITY = 999

#: ``urgent`` — ahead of every recorder, behind only the operator's forced re-transcribe (1).
URGENT_PRIORITY = 10
#: ``bulk`` — the back of the queue, whatever the talkgroup thinks of itself.
BULK_PRIORITY = 900
#: ``backlog`` — the talkgroup's own priority, pushed into the backlog band.
BACKLOG_OFFSET = BACKLOG_BAND

#: The talkgroup priority ``enqueue_transcription`` falls back to (``talkgroups.priority``
#: server default), used when a class has to be resolved without a talkgroup row.
DEFAULT_TALKGROUP_PRIORITY = 100

CLASS_NAMES = ("urgent", "live", "backlog", "bulk")


@dataclass(frozen=True, slots=True)
class PriorityChoice:
    """What the client asked for, and how to turn it into a job priority.

    ``fixed`` pins the priority regardless of the talkgroup (``urgent``, ``bulk``, an
    explicit integer); ``offset`` is added to the talkgroup's own priority when it does not
    (``live`` is ``+0``, ``backlog`` is ``+500``).
    """

    name: str
    fixed: int | None
    offset: int = 0
    #: did the client actually send a ``priority``?  A recorder that did not keeps the
    #: legacy plain-text contract and is never refused for backlog reasons.
    explicit: bool = True

    def resolve(self, talkgroup_priority: int | None) -> int:
        base = self.fixed if self.fixed is not None else (
            talkgroup_priority if talkgroup_priority is not None else DEFAULT_TALKGROUP_PRIORITY
        )
        return max(MIN_PRIORITY, min(MAX_PRIORITY, int(base) + self.offset))

    @property
    def is_backlog(self) -> bool:
        """Does this choice land in the backlog band for *every* talkgroup?

        ``backlog`` and ``bulk`` do by construction.  A bare integer is judged on its value;
        ``live``/``urgent`` never do, because ``talkgroups.priority`` is validated below the
        band and a live call that fell into it would be silently deprioritized.
        """
        if self.offset >= BACKLOG_BAND:
            return True
        return self.fixed is not None and self.fixed >= BACKLOG_BAND


#: The default when nothing is sent: today's behavior, the talkgroup's own priority.
DEFAULT_CHOICE = PriorityChoice(name="live", fixed=None, offset=0, explicit=False)

_CLASSES: dict[str, PriorityChoice] = {
    "urgent": PriorityChoice(name="urgent", fixed=URGENT_PRIORITY),
    "live": PriorityChoice(name="live", fixed=None, offset=0),
    "backlog": PriorityChoice(name="backlog", fixed=None, offset=BACKLOG_OFFSET),
    "bulk": PriorityChoice(name="bulk", fixed=BULK_PRIORITY),
}


class BadPriority(ValueError):
    """The ``priority`` field was neither a known class nor an integer in range."""


def parse_priority(form_value: str | None, header_value: str | None = None) -> PriorityChoice:
    """``priority`` (form wins) as a :class:`PriorityChoice`.

    The form field wins over ``X-Keyup-Priority`` because the form is what the Rdio-Scanner
    contract carries and the header only exists for clients that find a header easier to
    set per-request; a client sending both meant the one it had to spell out.

    Absent → :data:`DEFAULT_CHOICE` (``live``, not explicit).  Unparseable → raises
    :class:`BadPriority`: a client that sends ``priority`` has opted into the JSON contract
    and is better told about its typo than silently promoted to live.
    """
    raw = form_value if form_value is not None and str(form_value).strip() else header_value
    text = str(raw or "").strip().lower()
    if not text:
        return DEFAULT_CHOICE
    if text in _CLASSES:
        return _CLASSES[text]
    try:
        value = int(text)
    except ValueError:
        raise BadPriority(
            f"unknown priority {text!r}: one of {', '.join(CLASS_NAMES)} or an integer "
            f"{MIN_PRIORITY}–{MAX_PRIORITY}"
        ) from None
    if not MIN_PRIORITY <= value <= MAX_PRIORITY:
        raise BadPriority(f"priority {value} is outside {MIN_PRIORITY}–{MAX_PRIORITY}")
    return PriorityChoice(name=str(value), fixed=value)


# --------------------------------------------------------------------------------------
# reading a stored class back (``calls.ingest_priority``, ``?priority=``)
# --------------------------------------------------------------------------------------

#: The vocabulary a *reader* filters by, which is deliberately three values where the
#: push path accepts five.  ``urgent`` is an operator's re-transcribe jumping the queue,
#: not a different kind of traffic, so it reads back as ``live``; a bare integer is judged
#: against the same two band edges the queue itself uses.  The question ``?priority=``
#: answers is "was this call recorded as it happened, or did it arrive in a bulk import
#: months later" — and for that, three answers is the whole vocabulary.
FILTER_CLASSES = ("live", "backlog", "bulk")

#: Class names the push path stores verbatim, mapped to the class they read back as.
_STORED_NAMES = {"urgent": "live", "live": "live", "backlog": "backlog", "bulk": "bulk"}


def priority_class(raw: object) -> str | None:
    """Which of :data:`FILTER_CLASSES` a stored ``ingest_priority`` is, or ``None``.

    ``None`` for NULL, for blank, and for a string that is neither a known class name nor
    an integer — the same fail-closed reading :func:`app.exporters.gate.is_live_band`
    takes, and for the same reason: a value nobody can interpret must not be counted as
    live traffic.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in _STORED_NAMES:
        return _STORED_NAMES[text]
    try:
        value = int(text)
    except ValueError:
        return None
    if value < BACKLOG_BAND:
        return "live"
    return "backlog" if value < BULK_PRIORITY else "bulk"


def priority_class_clause(name: str) -> Any:
    """The SQL half of :func:`priority_class` for one class name.

    ``ingest_priority`` is free text, so every integer comparison is guarded by a regex
    test before the cast: a row holding something unparseable has to *fail* the filter,
    not fail the query.
    """
    from sqlalchemy import and_, cast, or_  # noqa: PLC0415 - only this function needs them
    from sqlalchemy.types import Integer  # noqa: PLC0415

    column = Call.ingest_priority
    numeric = column.op("~")("^[0-9]+$")
    names = sorted(key for key, value in _STORED_NAMES.items() if value == name)
    if name == "live":
        band = cast(column, Integer) < BACKLOG_BAND
    elif name == "backlog":
        band = and_(cast(column, Integer) >= BACKLOG_BAND, cast(column, Integer) < BULK_PRIORITY)
    elif name == "bulk":
        band = cast(column, Integer) >= BULK_PRIORITY
    else:  # pragma: no cover - the caller validates against FILTER_CLASSES
        raise ValueError(f"unknown priority class {name!r}")
    return and_(column.is_not(None), or_(column.in_(names), and_(numeric, band)))


# --------------------------------------------------------------------------------------
# the queue gauge
# --------------------------------------------------------------------------------------

#: How long one queue measurement is reused.  Long enough that a burst of uploads costs one
#: query, short enough that a client polling ``/api/upload/flow`` sees its own effect.
GAUGE_TTL_S = 2.0

#: Assumed real-time factor when no backend has reported one yet (1 s of audio per second
#: of wall clock — deliberately pessimistic for a GPU and optimistic for nothing).
FALLBACK_RTF = 1.0


@dataclass(frozen=True, slots=True)
class QueueCounts:
    """Open ``transcription_jobs`` split by band, with the audio behind them."""

    live: int = 0
    backlog: int = 0
    audio_seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.live + self.backlog


@dataclass(frozen=True, slots=True)
class QueueGauge:
    """:class:`QueueCounts` plus what the policy makes of them."""

    live: int
    backlog: int
    audio_seconds: float
    eta_seconds: float
    accepting_live: bool
    accepting_backlog: bool
    paused_backlog: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "backlog": self.backlog,
            "audio_seconds": round(self.audio_seconds, 1),
            "eta_seconds": round(self.eta_seconds, 1),
            "accepting": {"live": self.accepting_live, "backlog": self.accepting_backlog},
            "paused_backlog": self.paused_backlog,
        }


_lock = threading.Lock()
_counts: QueueCounts | None = None
_counts_at: float = 0.0
_router: Any = None


def set_router(router: Any) -> None:
    """Let this process' STT router supply measured throughput.

    The worker has one and the API does not, so the API's ETA is computed from the
    configured concurrency at :data:`FALLBACK_RTF`; the worker's is computed from the real-
    time factors its backends have actually delivered.
    """
    global _router
    _router = router


def invalidate() -> None:
    """Drop the cached measurement (tests, and the moment after a burst is enqueued)."""
    global _counts, _counts_at
    with _lock:
        _counts = None
        _counts_at = 0.0


def _measure(session: Session) -> QueueCounts:
    """One query: open jobs per band and the audio seconds behind them.

    ``running`` counts as well as ``queued``: a job a backend is chewing on is still work
    the next upload has to wait behind, and leaving it out made the gauge read empty at
    exactly the moment the queue was saturated.
    """
    band = case((TranscriptionJob.priority < BACKLOG_BAND, 1), else_=0).label("live_band")
    rows = session.execute(
        select(
            band,
            func.count().label("jobs"),
            func.coalesce(func.sum(Call.duration_s), 0).label("audio_s"),
        )
        .select_from(TranscriptionJob)
        .join(Call, Call.id == TranscriptionJob.call_id)
        .where(TranscriptionJob.status.in_(("queued", "running")))
        .group_by(band)
    ).all()
    live = backlog = 0
    audio = 0.0
    for is_live, jobs, audio_s in rows:
        if is_live:
            live = int(jobs)
        else:
            backlog = int(jobs)
        audio += float(audio_s or 0.0)
    return QueueCounts(live=live, backlog=backlog, audio_seconds=audio)


def counts(session: Session, *, force: bool = False) -> QueueCounts:
    """The cached measurement, refreshed at most every :data:`GAUGE_TTL_S` seconds.

    The cache makes the caps *soft* by design: a burst arriving inside one TTL is measured
    against the reading at the start of it, so the backlog band can overshoot
    ``UPLOAD_MAX_QUEUE_BACKLOG`` by whatever the client managed in two seconds.  That is the
    trade — a per-upload ``COUNT(*)`` over a queue with a hundred thousand rows in it is a
    real cost, and ``UPLOAD_BACKLOG_MAX_CONCURRENCY`` bounds the overshoot anyway.  Nothing
    downstream needs the cap to be exact; it needs the queue to stop growing.
    """
    global _counts, _counts_at
    now = time.monotonic()
    if not force:
        with _lock:
            if _counts is not None and now - _counts_at < GAUGE_TTL_S:
                return _counts
    try:
        # In a SAVEPOINT: a failed statement swallowed here would otherwise leave the
        # upload's transaction aborted for everything that follows it.
        with session.begin_nested():
            measured = _measure(session)
    except Exception:  # noqa: BLE001 - a broken gauge must not refuse every upload
        log.exception("flow: could not measure the queue; assuming it is empty")
        measured = QueueCounts()
    with _lock:
        _counts = measured
        _counts_at = time.monotonic()
    return measured


def throughput(settings: Settings) -> float:
    """Audio seconds this deployment can transcribe per wall second.

    ``sum(mean_rtf × concurrency)`` over the backends that are enabled and not known
    unhealthy.  A backend that has never reported a real-time factor counts as
    :data:`FALLBACK_RTF`, which is what makes this usable in the API process, where no
    transcription has ever run.
    """
    stats: dict[str, dict[str, Any]] = {}
    if _router is not None:
        try:
            stats = _router.stats()
        except Exception:  # noqa: BLE001 - introspection is a nicety
            stats = {}
    total = 0.0
    for backend in settings.stt_backends:
        if not backend.enabled:
            continue
        entry = stats.get(backend.name, {})
        if entry.get("healthy") is False or entry.get("disqualified"):
            continue
        rtf = entry.get("mean_rtf")
        total += (float(rtf) if rtf else FALLBACK_RTF) * max(1, int(backend.concurrency))
    return total or FALLBACK_RTF


def gauge(session: Session, settings: Settings, *, force: bool = False) -> QueueGauge:
    """The queue as the admission policy sees it, ``accepting`` flags included."""
    measured = counts(session, force=force)
    eta = measured.audio_seconds / throughput(settings)
    paused = bool(getattr(settings, "upload_pause_backlog", False))
    accepting_live = measured.live < settings.upload_max_queue_live
    accepting_backlog = (
        not paused
        and measured.backlog < settings.upload_max_queue_backlog
        and eta <= settings.upload_backlog_max_eta_seconds
    )
    return QueueGauge(
        live=measured.live,
        backlog=measured.backlog,
        audio_seconds=measured.audio_seconds,
        eta_seconds=eta,
        accepting_live=accepting_live,
        accepting_backlog=accepting_backlog,
        paused_backlog=paused,
    )


def headers(view: QueueGauge) -> dict[str, str]:
    """The five headers **every** upload response carries, accepted or refused.

    A client that reads them never has to poll ``/api/upload/flow`` at all: the answer to
    "may I send the next one" rides along with the answer to the last one.
    """
    return {
        "X-Keyup-Queue-Live": str(view.live),
        "X-Keyup-Queue-Backlog": str(view.backlog),
        "X-Keyup-Queue-Audio-Seconds": f"{view.audio_seconds:.0f}",
        "X-Keyup-Queue-Eta-Seconds": f"{view.eta_seconds:.0f}",
        "X-Keyup-Accepting-Backlog": "yes" if view.accepting_backlog else "no",
    }


# --------------------------------------------------------------------------------------
# admission
# --------------------------------------------------------------------------------------

#: ``Retry-After`` bounds for a queue-pressure refusal.  Five seconds is short enough that a
#: transient burst costs a client nothing; ten minutes is long enough that a client obeying
#: it stops hammering a saturated deployment.
MIN_RETRY_AFTER = 5
MAX_RETRY_AFTER = 600
#: Disk is not going to free itself in ten minutes; this one is an operator's problem.
DISK_RETRY_AFTER = 300


def _clamp_retry(seconds: float) -> int:
    return int(max(MIN_RETRY_AFTER, min(MAX_RETRY_AFTER, round(seconds))))


def disk_free(path: Path | str) -> int | None:
    """Free bytes under ``path``; ``None`` when it cannot be measured (never refuse then)."""
    try:
        return shutil.disk_usage(Path(path)).free
    except OSError:
        return None


_sem_lock = threading.Lock()
_sem: threading.BoundedSemaphore | None = None
_sem_size = 0


def _semaphore(size: int) -> threading.BoundedSemaphore:
    """The in-flight backlog-upload semaphore, rebuilt when the setting changes."""
    global _sem, _sem_size
    size = max(1, int(size))
    with _sem_lock:
        if _sem is None or _sem_size != size:
            _sem = threading.BoundedSemaphore(size)
            _sem_size = size
        return _sem


def reset_semaphore() -> None:
    """Forget the semaphore (tests, and after the setting is changed at runtime)."""
    global _sem, _sem_size
    with _sem_lock:
        _sem = None
        _sem_size = 0


@dataclass
class Admission:
    """The decision, and the semaphore slot it may be holding."""

    ok: bool
    view: QueueGauge
    choice: PriorityChoice
    status: int = 200
    error: str = ""
    detail: str = ""
    retry_after: int = 0
    _held: threading.BoundedSemaphore | None = None

    def release(self) -> None:
        """Give the in-flight slot back.  Safe to call twice; call it in a ``finally``."""
        if self._held is not None:
            held, self._held = self._held, None
            # A BoundedSemaphore raises rather than over-releasing; the guard above already
            # makes that unreachable, and this makes it unreachable twice.
            with contextlib.suppress(ValueError):  # pragma: no cover
                held.release()

    def body(self) -> dict[str, Any]:
        """The JSON a client that opted in (by sending ``priority``) receives."""
        return {
            "error": self.error,
            "class": self.choice.name,
            "detail": self.detail,
            "queue": self.view.as_dict(),
            "retry_after": self.retry_after,
        }


def _drain_estimate(view: QueueGauge, settings: Settings) -> float:
    """Seconds until the backlog band is plausibly under its threshold again.

    Two pressures, whichever is worse: too many queued backlog jobs (drain the excess at the
    measured mean job size) and too long an ETA (drain the overshoot outright).
    """
    rate = throughput(settings)
    excess_jobs = max(0, view.backlog - settings.upload_max_queue_backlog + 1)
    total_jobs = view.live + view.backlog
    mean_audio = (view.audio_seconds / total_jobs) if total_jobs else 0.0
    by_jobs = excess_jobs * mean_audio / rate
    by_eta = max(0.0, view.eta_seconds - settings.upload_backlog_max_eta_seconds)
    return max(by_jobs, by_eta)


def admit(
    choice: PriorityChoice,
    view: QueueGauge,
    settings: Settings,
    *,
    audio_root: Path | str,
) -> Admission:
    """Decide whether to take this upload — **before** the audio is read.

    Order matters.  Disk is checked first and refuses every class, because a deployment that
    cannot write the file has nothing to gain from sorting its callers.  Then the bands: a
    live call is refused only when the live queue is genuinely over
    ``UPLOAD_MAX_QUEUE_LIVE``, a backlog call is refused for any of the four backlog
    pressures.  A backlog admission takes a slot in the in-flight semaphore; the caller
    must :meth:`Admission.release` it.
    """
    free = disk_free(audio_root)
    if free is not None and free < settings.upload_min_disk_free_bytes:
        return Admission(
            ok=False,
            view=view,
            choice=choice,
            status=507,
            error="insufficient_storage",
            detail=(
                f"{free} bytes free under the audio root, below the "
                f"{settings.upload_min_disk_free_bytes}-byte floor"
            ),
            retry_after=DISK_RETRY_AFTER,
        )

    if not choice.is_backlog:
        if view.live >= settings.upload_max_queue_live:
            return Admission(
                ok=False,
                view=view,
                choice=choice,
                status=429,
                error="queue_full",
                detail=(
                    f"{view.live} live jobs queued, at or over the "
                    f"{settings.upload_max_queue_live} cap"
                ),
                retry_after=_clamp_retry(view.eta_seconds),
            )
        return Admission(ok=True, view=view, choice=choice)

    if view.paused_backlog:
        return Admission(
            ok=False,
            view=view,
            choice=choice,
            status=429,
            error="queue_full",
            detail="backlog ingestion is paused by the operator",
            retry_after=MAX_RETRY_AFTER,
        )
    if view.backlog >= settings.upload_max_queue_backlog:
        return Admission(
            ok=False,
            view=view,
            choice=choice,
            status=429,
            error="queue_full",
            detail=(
                f"{view.backlog} backlog jobs queued, at or over the "
                f"{settings.upload_max_queue_backlog} cap"
            ),
            retry_after=_clamp_retry(_drain_estimate(view, settings)),
        )
    if view.eta_seconds > settings.upload_backlog_max_eta_seconds:
        return Admission(
            ok=False,
            view=view,
            choice=choice,
            status=429,
            error="queue_full",
            detail=(
                f"the queue needs {view.eta_seconds:.0f} s to drain, over the "
                f"{settings.upload_backlog_max_eta_seconds:.0f} s backlog ceiling"
            ),
            retry_after=_clamp_retry(_drain_estimate(view, settings)),
        )

    semaphore = _semaphore(settings.upload_backlog_max_concurrency)
    if not semaphore.acquire(blocking=False):
        return Admission(
            ok=False,
            view=view,
            choice=choice,
            status=429,
            error="queue_full",
            detail=(
                f"{settings.upload_backlog_max_concurrency} backlog uploads are already in "
                "flight"
            ),
            retry_after=MIN_RETRY_AFTER,
        )
    return Admission(ok=True, view=view, choice=choice, _held=semaphore)
