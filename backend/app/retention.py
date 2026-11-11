"""Retention: purge or archive audio, then delete rows that aged out (ARCHITECTURE §5.3).

Two independent clocks per call:

``keep_audio_days``
    after this many days the MP3 goes.  With an ``archive_path`` it is *moved* there and
    ``calls.file_path`` is rewritten to the new location (absolute, so the API's
    ``audio_root / file_path`` still resolves); without one it is deleted.  Either way
    ``calls.audio_state`` becomes ``archived`` / ``purged`` and ``audio_archived_at`` is
    stamped, so "no audio" is never confused with "not recorded yet".
``keep_days``
    after this many days the ``calls`` row itself goes, taking its transcripts, jobs and
    external links with it (FK ``ON DELETE CASCADE``).

Zero means "keep for ever" for both, which is the shipped default: nothing is deleted
until an operator says so.  The most specific ``retention_policies`` row wins — talkgroup,
then system, then global — and a talkgroup's own ``retention_days`` column overrides the
``keep_days`` of whichever of those applies to it, and nothing else.  **Starred calls are
never touched**, at any tier: the star is re-checked under the row lock (audio) and in the
DELETE itself (rows), so a call starred while a pass runs is spared.

Safety rules, because this pass moves and deletes bytes:

* **a crash never leaves a committed row pointing at a file that has gone.**  A move to the
  archive commits its row before the next file is touched, and a move that landed before
  its row did is found again at its archive target by the next pass and the row is
  repaired; a purge (no archive) marks its batch's rows ``purged`` and commits *before* it
  unlinks anything, so a crash leaves a purged row with its file still on disk;
* **a missing mount is not a purge.**  The pass refuses to run when ``AUDIO_ROOT`` is
  absent, stops when it is empty while rows still expect files under it, and aborts when
  more than :data:`MISSING_ABORT_RATIO` of a batch's
  recordings are missing.  A file that is individually missing marks its row ``missing``
  rather than ``purged``, and a later pass that finds it again puts the row back;
* **archive paths are remembered.**  Every archive path a pass uses is recorded in the
  ``retention_archive_roots`` history, which :func:`app.audio.audio_roots` allows, so a
  recording archived under a policy's path, or a path that has since changed, still plays
  and is still deleted when its row expires.  An archive directory that does not exist
  (an unmounted share) is never created: those calls are left alone and reported;
* **rows go first, files after.**  ``DELETE … RETURNING`` commits, then only the returned
  rows' files are unlinked.

The nightly walk reads only rows one of the clocks can act on — old enough to delete, or
old enough to retire and still holding audio — and only the columns it decides on, in
``(started_at, id)`` order along ``ix_calls_started_at``; audio already archived or purged
is not read again.

``radio_events`` has a third clock of its own, ``RADIO_EVENTS_KEEP_DAYS``, and it is the
one thing here that does *not* default to keeping for ever: 90 days.  Those rows are
presence data about identifiable people — which radio registered on which site at which
minute, and which talkgroup its user selected — recorded whether or not that user ever
transmitted, so the archive holds them for a shorter time than it holds the audio
(docs/RADIO_EVENTS.md).  Nothing about a call depends on them, so the purge is a single
DELETE and runs even when every audio policy says "for ever".

``activity_events`` has a fourth, for a different reason: not privacy but volume.  The
operational log behind the Activity page writes a row per call accepted, transcribed and
exported, which made it the archive's largest table by row count within two weeks of
existing (3.34 M rows, 1.2 GB) — and it is diagnostics, read only as the last few hundred
lines on a page.  ``ACTIVITY_RETENTION_DAYS`` bounds it, and like the radio events it runs
whatever the audio policies say.  Its purge is batched, because unlike the other two it is
a window being applied for the first time to a table that never had one.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session, load_only

from app import runtime_settings
from app.audio import PRESENT_STATES, audio_path, processed_path, prune_empty_dirs
from app.config import Settings
from app.models import (
    ActivityEvent,
    Call,
    RadioEvent,
    RetentionPolicy,
    Setting,
    Talkgroup,
    Transcript,
)
from app.runtime_settings import effective_settings

log = logging.getLogger("app.retention")

__all__ = [
    "Policy",
    "RetentionReport",
    "apply_retention",
    "purge_activity_events",
    "purge_radio_events",
    "remember_archive_root",
    "resolve_policies",
    "validate_archive_path",
]

#: calls are handled in batches so a first run over a large archive does not build one
#: enormous transaction
BATCH = 500

#: ``activity_events`` rows removed per statement.  Ten thousand rather than the calls'
#: 500 because there is no file and no cascade behind an activity row — the whole cost is
#: the DELETE itself — but emphatically not "all of them": the first pass after this
#: setting is turned on has millions of rows to remove, and one statement for all of them
#: would hold a transaction open for minutes, pin every dead tuple in the database against
#: vacuum for its duration, and write the lot to WAL before a single byte could be
#: reclaimed.  Ten thousand rows is a sub-second statement at any table size.
ACTIVITY_BATCH = 10_000

#: The share of a batch's recordings that may be missing before the pass stops (R4).  A
#: file or two gone by hand is a fact about those calls; a twentieth of them gone at once is
#: a fact about the disk — an NFS share that did not mount, a volume detached — and the
#: pass must not turn it into thousands of permanent state changes.
MISSING_ABORT_RATIO = 0.05
#: …and the number of missing files below which the ratio is not applied, so a night
#: whose whole batch is three calls is not stopped by the one whose file somebody deleted.
MISSING_ABORT_MIN = 5

#: The ``settings`` row holding the history of archive paths (:func:`remember_archive_root`).
ARCHIVE_ROOTS_KEY = "retention_archive_roots"


class RetentionAborted(Exception):
    """The pass stopped rather than act on what looks like a missing disk."""


@dataclass(frozen=True)
class Policy:
    """The effective retention rule for one call."""

    keep_days: int = 0
    keep_audio_days: int = 0
    keep_transcripts: bool = True
    archive_path: str | None = None
    #: 'talkgroup:<id>' | 'system:<id>' | 'global' | 'settings' — for the report/logs
    source: str = "settings"

    @property
    def inactive(self) -> bool:
        return self.keep_days <= 0 and self.keep_audio_days <= 0


@dataclass
class RetentionReport:
    """What one pass did (or, with ``dry_run``, would have done)."""

    dry_run: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    #: calls examined: old enough for the shortest row window, or for the shortest audio
    #: window with audio still to retire
    candidates: int = 0
    audio_archived: int = 0
    audio_deleted: int = 0
    audio_missing: int = 0
    transcripts_deleted: int = 0
    calls_deleted: int = 0
    #: ``radio_events`` rows aged out by ``RADIO_EVENTS_KEEP_DAYS`` (its own clock)
    radio_events_deleted: int = 0
    #: ``activity_events`` rows aged out by ``ACTIVITY_RETENTION_DAYS`` (its own clock too)
    activity_events_deleted: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    #: why the pass stopped early (a missing mount, too many missing files), else ``None``
    aborted: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "candidates": self.candidates,
            "audio_archived": self.audio_archived,
            "audio_deleted": self.audio_deleted,
            "audio_missing": self.audio_missing,
            "transcripts_deleted": self.transcripts_deleted,
            "calls_deleted": self.calls_deleted,
            "radio_events_deleted": self.radio_events_deleted,
            "activity_events_deleted": self.activity_events_deleted,
            "bytes_freed": self.bytes_freed,
            "errors": self.errors[:20],
            "aborted": self.aborted,
        }


# --------------------------------------------------------------------------------------
# policy resolution
# --------------------------------------------------------------------------------------


def resolve_policies(
    session: Session, settings: Settings | None = None
) -> tuple[Policy, dict[int, Policy], dict[int, Policy]]:
    """Return ``(default, by_system_id, by_talkgroup_id)``.

    The settings-derived policy is the floor; an enabled ``retention_policies`` row with
    scope ``global`` replaces it, and system/talkgroup rows override that for their scope.
    A talkgroup's ``retention_days`` column is folded in as a talkgroup-scoped override of
    ``keep_days`` alone, because that is the one knob the directory UI exposes.

    The settings floor comes from :func:`app.runtime_settings.effective_settings`, so the
    Admin page's retention defaults are the ones the nightly pass actually uses.
    """
    settings = effective_settings(session, base=settings)
    default = Policy(
        keep_days=settings.retention_keep_days,
        keep_audio_days=settings.retention_keep_audio_days,
        keep_transcripts=settings.retention_keep_transcripts,
        archive_path=settings.retention_archive_path or None,
        source="settings",
    )

    by_system: dict[int, Policy] = {}
    by_talkgroup: dict[int, Policy] = {}
    # ``scope`` is an enum declared global, system, talkgroup, so the global row — which
    # the others inherit an archive path from — is read first.
    for row in session.execute(
        select(RetentionPolicy)
        .where(RetentionPolicy.enabled.is_(True))
        .order_by(RetentionPolicy.scope, RetentionPolicy.id)
    ).scalars():
        policy = Policy(
            keep_days=row.keep_days,
            keep_audio_days=row.keep_audio_days,
            keep_transcripts=row.keep_transcripts,
            archive_path=row.archive_path or default.archive_path,
            source=f"{row.scope}:{row.scope_id}" if row.scope_id is not None else row.scope,
        )
        if row.scope == "global":
            default = policy
        elif row.scope == "system" and row.scope_id is not None:
            by_system[row.scope_id] = policy
        elif row.scope == "talkgroup" and row.scope_id is not None:
            by_talkgroup[row.scope_id] = policy

    for tg_id, system_id, days in session.execute(
        select(Talkgroup.id, Talkgroup.system_id, Talkgroup.retention_days).where(
            Talkgroup.retention_days.isnot(None)
        )
    ).all():
        # The column overrides ``keep_days`` of the policy that would otherwise apply —
        # the talkgroup's own row, else its system's, else the default.  Falling straight
        # through to the default dropped a system policy's audio clock and archive path, so
        # a talkgroup with a row lifetime set had its audio *deleted* where its system said
        # to archive it (R2).
        base = by_talkgroup.get(tg_id) or by_system.get(system_id) or default
        by_talkgroup[tg_id] = Policy(
            keep_days=days,
            keep_audio_days=base.keep_audio_days,
            keep_transcripts=base.keep_transcripts,
            archive_path=base.archive_path,
            source=f"talkgroup:{tg_id}",
        )

    return default, by_system, by_talkgroup


def _policy_for(
    call: Call,
    default: Policy,
    by_system: dict[int, Policy],
    by_talkgroup: dict[int, Policy],
) -> Policy:
    if call.talkgroup_id is not None and call.talkgroup_id in by_talkgroup:
        return by_talkgroup[call.talkgroup_id]
    if call.system_id in by_system:
        return by_system[call.system_id]
    return default


def _shortest_window_days(policies: list[Policy]) -> int | None:
    """Smallest non-zero day count across every policy, or ``None`` when all keep for ever."""
    days = [
        d
        for policy in policies
        for d in (policy.keep_days, policy.keep_audio_days)
        if d and d > 0
    ]
    return min(days) if days else None


def _eligible(policies: list[Policy], now: datetime) -> ColumnElement[bool]:
    """The rows one of the two clocks could act on tonight, as a WHERE clause.

    A row is read only if it is old enough for the shortest row window (it may be deleted),
    or old enough for the shortest audio window *and* still has audio to retire.  Every
    policy's clock is at least as long as those two, so this never drops a row the per-row
    policy check would act on; what it drops is the archive's already-retired audio, which
    the walk used to read in full every night only to skip it in Python.
    """
    audio_days = [p.keep_audio_days for p in policies if p.keep_audio_days > 0]
    row_days = [p.keep_days for p in policies if p.keep_days > 0]
    clauses: list[ColumnElement[bool]] = []
    if audio_days:
        clauses.append(
            and_(
                Call.started_at < now - timedelta(days=min(audio_days)),
                Call.audio_state.in_(("present", "missing")),
            )
        )
    if row_days:
        clauses.append(Call.started_at < now - timedelta(days=min(row_days)))
    return or_(*clauses)


# --------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------


def _resolve_audio(call: Call, settings: Settings) -> Path | None:
    """The recording's path, or ``None`` when the row names something outside the tree.

    This pass *deletes* files, so the containment check in :mod:`app.audio` is not a
    nicety here: a ``file_path`` that escaped ``AUDIO_ROOT`` — however it got written —
    must cost a skipped call and a log line, never an unlink.
    """
    return audio_path(call, settings)


def _archive_target(call: Call, source: Path, audio_root: Path, archive_root: Path) -> Path:
    """Where an archived recording lands: the same relative layout under the archive root.

    The leaf is prefixed with the call id.  sdrtrunk names a file from the timestamp, the
    talkgroup and the radio, so two systems can produce the same leaf under different
    trees — and ``shutil.move`` onto an existing name destroys the older recording
    silently (L11).
    """
    try:
        relative = source.relative_to(audio_root)
    except ValueError:
        relative = Path(call.file_name or source.name)
    return archive_root / relative.parent / f"{call.id}-{relative.name}"


# --------------------------------------------------------------------------------------
# archive paths
# --------------------------------------------------------------------------------------


def _allowed_archive_parents(settings: Settings) -> list[Path]:
    raw = str(getattr(settings, "retention_archive_allowed_roots", "") or "")
    return [Path(part.strip()).resolve() for part in raw.split(",") if part.strip()]


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _archive_path_problem(path: str, settings: Settings) -> str | None:
    """Why ``path`` cannot hold archived audio right now, or ``None`` when it can.

    The checks the pass itself makes before moving anything into a directory: absolute,
    apart from the live audio tree (an archive inside ``AUDIO_ROOT`` would be walked by
    ingest and re-imported as new calls; one containing it would make every recording
    "archived" already), and **already there**.  The last is the mount check: the pass
    never creates an archive root, because an absent NFS share's mount point is an empty
    directory — or no directory — on the root filesystem, and filling that is how a disk
    fills up behind everybody's back.
    """
    text = (path or "").strip()
    if not text:
        return "the archive path is blank"
    candidate = Path(text)
    if not candidate.is_absolute():
        return f"{text!r} is not an absolute path"
    resolved = candidate.resolve()
    audio_root = Path(settings.audio_root).resolve()
    if _overlaps(resolved, audio_root):
        return f"{text!r} overlaps AUDIO_ROOT ({audio_root})"
    if not resolved.is_dir():
        return f"{text!r} is not an existing directory (is the archive mounted?)"
    return None


def validate_archive_path(path: str, settings: Settings) -> str:
    """The normalised archive path an operator may save, or :class:`ValueError` saying why not.

    Everything :func:`_archive_path_problem` checks, plus the boundary: the directory must
    lie under one of ``RETENTION_ARCHIVE_ALLOWED_ROOTS``.  An archive path becomes an
    allowed root for serving audio (:func:`app.audio.audio_roots`), so an unchecked one —
    ``/`` — would turn any ``calls.file_path`` into a file the audio route will read.
    Beside ``AUDIO_ROOT``, the other data directories keyup writes are refused as well.
    """
    problem = _archive_path_problem(path, settings)
    if problem is not None:
        raise ValueError(problem)
    resolved = Path(path.strip()).resolve()
    parents = _allowed_archive_parents(settings)
    if not any(resolved.is_relative_to(parent) and resolved != parent for parent in parents):
        allowed = ", ".join(str(p) for p in parents) or "(none configured)"
        raise ValueError(
            f"{path.strip()!r} is not inside an allowed archive location "
            f"(RETENTION_ARCHIVE_ALLOWED_ROOTS: {allowed})"
        )
    for name in ("export_dir", "waveform_cache_dir", "event_log_root"):
        other = getattr(settings, name, None)
        if other and _overlaps(resolved, Path(other).resolve()):
            raise ValueError(f"{path.strip()!r} overlaps {name.upper()} ({Path(other).resolve()})")
    return str(resolved)


def remember_archive_root(session: Session, path: str | None) -> bool:
    """Add ``path`` to the archive-path history; ``True`` when it was new.

    Does not commit — it rides in the caller's transaction, so a policy save and the
    history that makes its archive playable land together.  Past values are never removed:
    a directory drops out of the history only by hand, once nothing lives under it.
    """
    text = (path or "").strip()
    if not text or not Path(text).is_absolute():
        return False
    value = str(Path(text).resolve())
    row = session.get(Setting, ARCHIVE_ROOTS_KEY, with_for_update=True)
    known = [str(v) for v in (row.value or [])] if row is not None and isinstance(row.value, list) else []
    if value in known:
        return False
    if row is None:
        row = Setting(key=ARCHIVE_ROOTS_KEY, value=[*known, value])
        session.add(row)
    else:
        row.value = [*known, value]
    row.updated_by = "retention"
    runtime_settings.invalidate()
    return True


def _prepare_archive_paths(
    session: Session,
    settings: Settings,
    policies: Iterable[Policy],
    *,
    dry_run: bool,
    report: RetentionReport,
) -> tuple[Settings, dict[str, str]]:
    """Check every archive path in play; remember the good ones.  Returns the settings with
    the history applied and ``{path: problem}`` for the unusable ones."""
    unusable: dict[str, str] = {}
    added = False
    for path in sorted({p.archive_path for p in policies if p.archive_path}):
        problem = _archive_path_problem(path, settings)
        if problem is not None:
            unusable[path] = problem
            report.errors.append(f"archive {problem}; its calls keep their audio this pass")
            continue
        if not dry_run and remember_archive_root(session, path):
            added = True
    if added:
        session.commit()
        runtime_settings.invalidate()
    roots = list(getattr(settings, "retention_archive_roots", None) or [])
    for path in sorted({p.archive_path for p in policies if p.archive_path} - set(unusable)):
        resolved = str(Path(path).resolve())
        if resolved not in roots:
            roots.append(resolved)
    return settings.model_copy(update={"retention_archive_roots": roots}), unusable


def _audio_root_problem(root: Path) -> str | None:
    """``None`` when ``AUDIO_ROOT`` is a readable directory."""
    try:
        if not root.is_dir():
            return f"AUDIO_ROOT {root} is not a directory — is the audio volume mounted?"
        next(root.iterdir(), None)
    except OSError as exc:
        return f"AUDIO_ROOT {root} cannot be read: {exc}"
    return None


def _is_empty(root: Path) -> bool:
    """An empty ``AUDIO_ROOT`` is what an unmounted volume looks like from inside a container.

    Not a reason to stop on its own — a root whose every recording has been retired is
    empty too — but together with a single row that expects a file under it, it is.
    """
    try:
        return next(root.iterdir(), None) is None
    except OSError:
        return True


EMPTY_ROOT = "AUDIO_ROOT {root} is empty but calls expect recordings under it — is the audio volume mounted?"


def _too_many_missing(missing: int, checked: int) -> bool:
    return missing >= MISSING_ABORT_MIN and missing > checked * MISSING_ABORT_RATIO


def purge_radio_events(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    dry_run: bool = False,
    report: RetentionReport | None = None,
) -> int:
    """Delete ``radio_events`` older than ``RADIO_EVENTS_KEEP_DAYS``; return how many.

    One DELETE rather than the batched walk the calls get: nothing hangs off these rows —
    no file on disk, no cascade worth watching — and 90 days of them is ~1.4 M rows at the
    measured 600/hour, which Postgres removes on an index range without help.
    """
    keep_days = int(getattr(settings, "radio_events_keep_days", 0) or 0)
    if keep_days <= 0:
        return 0
    cutoff = now - timedelta(days=keep_days)
    if dry_run:
        doomed = int(
            session.scalar(select(func.count()).select_from(RadioEvent).where(RadioEvent.at < cutoff)) or 0
        )
    else:
        # The DELETE says how many it removed; counting first read the same range twice.
        doomed = int(session.execute(delete(RadioEvent).where(RadioEvent.at < cutoff)).rowcount or 0)
        session.commit()
        if doomed:
            log.info("retention: purged %s radio_events older than %s days", doomed, keep_days)
    if report is not None:
        report.radio_events_deleted += doomed
    return doomed


def purge_activity_events(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    dry_run: bool = False,
    report: RetentionReport | None = None,
) -> int:
    """Delete ``activity_events`` older than ``activity_retention_days``; return how many.

    The operational log behind the Activity page: one row per call accepted, transcribed,
    exported or failed.  It is the fastest-growing table in the archive by row count —
    3.34 M rows and 1.2 GB two weeks after it was introduced, more rows than ``calls``
    itself — and nothing reads it but a human looking at the last few hundred lines.  So it
    gets a clock of its own, like ``radio_events``, and unlike the audio it does *not*
    default to keeping for ever.

    Batched, unlike :func:`purge_radio_events`.  The difference is the first run: radio
    events have been expiring on a 90-day window since they existed, so that pass only ever
    meets a day's worth, while this window is being applied to a table that has never had
    one — the first pass deletes everything past the cutoff in one go, and on a deployment
    that has been running for months that is millions of rows.  Each batch is its own
    committed transaction, so a pass interrupted half way keeps the rows it removed and the
    next night's pass picks up from there.

    ``ORDER BY id`` rather than by ``at``: ids are assigned in arrival order, so the oldest
    rows are the lowest ids, and paging by the primary key gives each batch a cheap,
    contiguous range instead of re-walking a timestamp index that the previous batch just
    churned.
    """
    keep_days = int(getattr(settings, "activity_retention_days", 0) or 0)
    if keep_days <= 0:
        return 0
    cutoff = now - timedelta(days=keep_days)

    if dry_run:
        doomed = int(
            session.scalar(select(func.count()).select_from(ActivityEvent).where(ActivityEvent.at < cutoff))
            or 0
        )
        if report is not None:
            report.activity_events_deleted += doomed
        return doomed

    removed = 0
    while True:
        batch = select(ActivityEvent.id).where(ActivityEvent.at < cutoff).order_by(ActivityEvent.id).limit(
            ACTIVITY_BATCH
        )
        result = session.execute(
            delete(ActivityEvent).where(ActivityEvent.id.in_(batch.scalar_subquery()))
        )
        session.commit()
        count = int(result.rowcount or 0)
        removed += count
        if count < ACTIVITY_BATCH:
            break

    if report is not None:
        report.activity_events_deleted += removed
    if removed:
        log.info("retention: purged %s activity_events older than %s days", removed, keep_days)
    return removed


def apply_retention(
    session: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    audio_root: Path | None = None,
) -> RetentionReport:
    """Run one retention pass and report what happened.

    The caller decides what to do with the report (the worker logs it); this function
    deliberately writes no run row of its own.
    """
    settings = effective_settings(session, base=settings)
    root = Path(audio_root) if audio_root is not None else Path(settings.audio_root)
    now = now or datetime.now(UTC)
    report = RetentionReport(dry_run=dry_run)

    # Independent of every call policy, and therefore before the early return below: a
    # deployment that keeps its audio for ever still expires its presence data — and its
    # operational log, which is about the archive rather than about anybody's calls and is
    # the one table here that would grow without bound if this were skipped.
    purge_radio_events(session, settings=settings, now=now, dry_run=dry_run, report=report)
    purge_activity_events(session, settings=settings, now=now, dry_run=dry_run, report=report)

    default, by_system, by_talkgroup = resolve_policies(session, settings)
    policies = [default, *by_system.values(), *by_talkgroup.values()]
    horizon = _shortest_window_days(policies)
    if horizon is None:
        log.info("retention: every call policy keeps for ever — %s", report.as_dict())
        report.finished_at = datetime.now(UTC)
        return report

    # Before any file is looked at: a pass over an unmounted audio volume would find every
    # recording missing (R4).
    problem = _audio_root_problem(root)
    if problem is not None:
        return _abort(session, report, problem, dry_run=dry_run)

    root_empty = _is_empty(root)
    settings, unusable = _prepare_archive_paths(
        session, settings, policies, dry_run=dry_run, report=report
    )

    cutoff = now - timedelta(days=horizon)
    eligible = _eligible(policies, now)
    doomed: list[int] = []

    def flush_doomed() -> None:
        """Delete the aged-out calls collected so far, a batch at a time.

        Called as the list fills rather than once at the end: a first pass over a large
        archive can find hundreds of thousands of rows, and holding every id until the
        walk finished meant one enormous ``IN`` list and a memory footprint proportional
        to the archive (L12).
        """
        while doomed:
            chunk = doomed[:BATCH]
            del doomed[:BATCH]
            _delete_batch(session, chunk, settings, report, root_empty=root_empty)

    # Keyset over ``(started_at, id)``, the walk's own order: ``ix_calls_started_at`` gives
    # every batch an index range that ends at the cutoff.  Paging by ``id`` instead filtered
    # the primary key on ``started_at`` — ids are not in time order (backfilled archives) —
    # so the batch that found the last candidate walked the rest of the table to prove it.
    after: tuple[datetime, int] | None = None
    try:
        while True:
            stmt = select(
                Call.id,
                Call.system_id,
                Call.talkgroup_id,
                Call.started_at,
                Call.audio_state,
                Call.file_path,
                Call.file_name,
            ).where(Call.starred.is_(False), Call.started_at < cutoff, eligible)
            if after is not None:
                stmt = stmt.where(
                    Call.started_at >= after[0],
                    or_(Call.started_at > after[0], Call.id > after[1]),
                )
            rows = session.execute(stmt.order_by(Call.started_at, Call.id).limit(BATCH)).all()
            if not rows:
                break
            after = (rows[-1].started_at, rows[-1].id)

            retire: list[tuple[object, Policy]] = []
            for row in rows:
                report.candidates += 1
                policy = _policy_for(row, default, by_system, by_talkgroup)  # type: ignore[arg-type]
                age = now - row.started_at

                if policy.keep_days > 0 and age > timedelta(days=policy.keep_days):
                    doomed.append(row.id)
                    if dry_run:
                        report.calls_deleted += 1
                    continue

                if (
                    policy.keep_audio_days > 0
                    and age > timedelta(days=policy.keep_audio_days)
                    and row.audio_state in ("present", "missing")
                ):
                    retire.append((row, policy))

            _check_missing(retire, root, settings, root_empty=root_empty)
            purge: list[tuple[int, Policy]] = []
            for row, policy in retire:
                if not policy.archive_path:
                    purge.append((row.id, policy))
                    continue
                _retire_audio(
                    session, row.id, policy, root, settings, report, dry_run=dry_run, unusable=unusable
                )
            _purge_audio(session, purge, settings, report, dry_run=dry_run)

            if dry_run:
                doomed.clear()
            else:
                session.commit()
                if len(doomed) >= BATCH:
                    flush_doomed()

        if not dry_run:
            flush_doomed()
    except RetentionAborted as exc:
        session.rollback()
        return _abort(session, report, str(exc), dry_run=dry_run)

    report.finished_at = datetime.now(UTC)
    log.info("retention: %s", report.as_dict())
    return report


def _abort(session: Session, report: RetentionReport, reason: str, *, dry_run: bool) -> RetentionReport:
    """Stop the pass, say so in the report, the log and (for a real pass) the Activity page."""
    report.aborted = reason
    report.errors.append(f"pass stopped: {reason}")
    report.finished_at = datetime.now(UTC)
    log.error("retention: pass stopped — %s — %s", reason, report.as_dict())
    if not dry_run:
        from app import activity  # noqa: PLC0415 - activity imports the models this does

        activity.record(
            session,
            source="system",
            level="error",
            kind="retention.aborted",
            message=f"retention pass stopped: {reason}",
            meta=report.as_dict(),
        )
        session.commit()
    return report


def _check_missing(
    retire: list[tuple[object, Policy]], root: Path, settings: Settings, *, root_empty: bool = False
) -> None:
    """Raise :class:`RetentionAborted` when too many of a batch's recordings are missing.

    Only rows that still claim ``present`` count: a row already marked ``missing`` is being
    re-checked, and counting it would let a handful of genuinely lost files stop every
    future pass.  A file already sitting at its archive target is not missing — it is a move
    whose row did not commit, which :func:`_retire_audio` repairs.
    """
    checked = missing = 0
    for row, policy in retire:
        if getattr(row, "audio_state", None) != "present":
            continue
        path = audio_path(row, settings)
        if path is None:
            continue
        checked += 1
        if path.is_file():
            continue
        if policy.archive_path and _archive_target(row, path, root, Path(policy.archive_path)).is_file():  # type: ignore[arg-type]
            continue
        missing += 1
    if root_empty and missing:
        raise RetentionAborted(EMPTY_ROOT.format(root=root))
    if _too_many_missing(missing, checked):
        raise RetentionAborted(
            f"{missing} of {checked} recordings due to be retired are missing from disk — "
            "is the audio volume mounted? Nothing was changed for them"
        )


def _drop_processed(call: Call, settings: Settings, report: RetentionReport) -> int:
    """Remove the call's processed copy (§5.5).  Returns the bytes it freed.

    The processed copy is derived data that lives beside the original, so it goes wherever
    the original goes: purging a recording and leaving its ``.proc.mp3`` behind would leave
    the archive holding audio for a call the policy said to forget, which is the one thing
    retention exists to prevent.
    """
    path = processed_path(call, settings)
    if path is None:
        return 0
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError as exc:
        report.errors.append(f"unlink {path}: {exc}")
        return 0
    return size


def _delete_batch(
    session: Session,
    call_ids: list[int],
    settings: Settings,
    report: RetentionReport,
    *,
    root_empty: bool = False,
) -> None:
    """Delete the rows of calls past ``keep_days``, then their recordings.

    ``DELETE … RETURNING`` with ``starred IS FALSE`` in the statement itself, so a call
    starred after the walk collected it is not deleted; the files are unlinked only for the
    rows that statement returned, and only after it committed (R5).  A crash in between
    leaves a file with no row — found by a sweep — never a row with no file.

    The missing-file guard runs on the returned rows *before* the commit: if too many of
    them have no file, the DELETE is rolled back and the pass stops (R4).
    """
    deleted = session.execute(
        delete(Call)
        .where(Call.id.in_(call_ids), Call.starred.is_(False))
        .returning(Call.id, Call.file_path, Call.file_name, Call.processed_path, Call.audio_state)
    ).all()

    files: list[Path] = []
    checked = missing = 0
    for row in deleted:
        derived = processed_path(row, settings)
        if derived is not None:
            files.append(derived)
        if row.audio_state not in PRESENT_STATES and row.audio_state != "missing":
            continue  # purged / discarded: nothing on disk to take away
        path = audio_path(row, settings)
        if path is None:
            report.errors.append(f"call {row.id}: file_path is outside the archive; not unlinked")
            continue
        if row.audio_state in PRESENT_STATES:
            checked += 1
            if not path.is_file():
                missing += 1
                continue
        if path.is_file():
            files.append(path)

    if root_empty and missing:
        session.rollback()
        raise RetentionAborted(EMPTY_ROOT.format(root=settings.audio_root) + "; no row was deleted")
    if _too_many_missing(missing, checked):
        session.rollback()
        raise RetentionAborted(
            f"{missing} of {checked} recordings of calls due for deletion are missing from disk — "
            "is the audio or archive volume mounted? No row was deleted"
        )
    session.commit()
    report.calls_deleted += len(deleted)

    for path in files:
        try:
            report.bytes_freed += path.stat().st_size
            path.unlink()
        except OSError as exc:  # noqa: PERF203 - one bad file must not stop the pass
            report.errors.append(f"unlink {path}: {exc}")
            continue
        # A retention pass expires whole days at a time, so the last call out of a partition
        # is the common case rather than the odd one; the empty ``uploads/2022/03/11/7f/``
        # it leaves behind would otherwise outlive the archive it belonged to.
        prune_empty_dirs(path.parent, settings)


def _retire_audio(
    session: Session,
    call_id: int,
    policy: Policy,
    root: Path,
    settings: Settings,
    report: RetentionReport,
    *,
    dry_run: bool,
    unusable: dict[str, str] | None = None,
) -> None:
    """Archive one recording's audio and mark the row — in one transaction of its own,
    committed before the next recording is touched (R3).

    The row is re-read under ``FOR UPDATE`` with ``starred IS FALSE``, so a star that
    landed after the walk read the batch spares the call (R5).  Only the columns this needs
    are loaded: a full ``calls`` row carries ``search_tsv``, the transcript and
    ``audio_meta``, none of which a file move reads.

    A policy without an archive path has nothing to move, so its recordings take the
    batched purge (:func:`_purge_audio`); the walk sends them there a batch at a time.
    """
    if not policy.archive_path:
        _purge_audio(session, [(call_id, policy)], settings, report, dry_run=dry_run)
        return
    stmt = (
        select(Call)
        .options(load_only(Call.id, Call.file_path, Call.file_name, Call.processed_path, Call.audio_state))
        .where(
            Call.id == call_id,
            Call.starred.is_(False),
            Call.audio_state.in_(("present", "missing")),
        )
    )
    if not dry_run:
        stmt = stmt.with_for_update()
    call = session.execute(stmt.execution_options(populate_existing=True)).scalar_one_or_none()
    if call is None:
        return  # starred, deleted or retired since the walk read it

    def done() -> None:
        if not dry_run:
            session.commit()

    source = _resolve_audio(call, settings)
    if source is None:
        report.errors.append(f"call {call.id}: file_path is outside the archive; left alone")
        done()
        return

    if not source.is_file():
        target = _archive_target(call, source, root, Path(policy.archive_path))
        if target.is_file():
            # A previous pass moved the file and stopped before its row committed.  The
            # recording is safe in the archive; the row only has to be told where.
            report.audio_archived += 1
            if not dry_run:
                call.file_path = str(target)
                call.audio_state = "archived"
                call.audio_archived_at = datetime.now(UTC)
                call.processed_path = None
            done()
            return
        if call.audio_state == "present":
            # ``missing``, not ``purged``: the row said there was a file and there is not,
            # which is a fact about the disk, not a decision this pass made.  A later pass
            # that finds the file again puts the row back (R4).
            report.audio_missing += 1
            if not dry_run:
                call.audio_state = "missing"
        done()
        return

    # The file is there — including for a row an earlier pass marked ``missing``, whose
    # file has come back; it is retired like any other.
    if unusable and policy.archive_path in unusable:
        done()  # reported once by _prepare_archive_paths; the audio stays where it is
        return

    target = _archive_target(call, source, root, Path(policy.archive_path))
    report.audio_archived += 1
    if dry_run:
        _drop_transcripts(session, [call.id], policy, report, dry_run=True)
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        report.audio_archived -= 1
        report.errors.append(f"archive {source}: {exc}")
        session.rollback()
        return
    # Whether this call is being archived or purged, its processed copy is not kept:
    # archiving is for the recorder's own bytes, and a regenerable derivative is not
    # worth the archive tier's space.  A restored call reprocesses on demand.
    report.bytes_freed += _drop_processed(call, settings, report)
    # absolute, so ``audio_root / file_path`` still resolves for the API
    call.file_path = str(target)
    call.audio_state = "archived"
    call.audio_archived_at = datetime.now(UTC)
    # The derivative was dropped above; the column must not outlive it.
    call.processed_path = None
    prune_empty_dirs(source.parent, settings)

    if not policy.keep_transcripts:
        _drop_transcripts(session, [call.id], policy, report, dry_run=False)
        call.transcript_text = None
        call.transcript_quality = None
    session.commit()


def _purge_audio(
    session: Session,
    items: list[tuple[int, Policy]],
    settings: Settings,
    report: RetentionReport,
    *,
    dry_run: bool,
) -> None:
    """Delete the recordings of a batch of calls whose policy keeps no archive.

    Rows first, files after — as :func:`_delete_batch` does for whole calls, and for the
    same reason: one locked read of the batch (``FOR UPDATE`` with ``starred IS FALSE``, so
    a star that landed after the walk spares the call, R5), a set-based UPDATE per outcome,
    one commit, and only then the unlinks.  A crash in between leaves a ``purged`` row
    whose file is still on disk, never a ``present`` row whose file has gone — so nothing a
    later pass reads can mistake a finished purge for a missing mount (R3, R4).  The walk's
    missing-file guard (:func:`_check_missing`) has already run on the batch.

    An unlink that fails puts its row back as it was, so the next pass tries again.
    """
    if not items:
        return
    policies = dict(items)
    stmt = select(Call.id, Call.file_path, Call.file_name, Call.processed_path, Call.audio_state).where(
        Call.id.in_(list(policies)),
        Call.starred.is_(False),
        Call.audio_state.in_(("present", "missing")),
    )
    if not dry_run:
        stmt = stmt.with_for_update()
    rows = session.execute(stmt).all()

    doomed: list[tuple[object, Path, int]] = []
    missing: list[int] = []
    for row in rows:
        source = _resolve_audio(row, settings)  # type: ignore[arg-type]
        if source is None:
            report.errors.append(f"call {row.id}: file_path is outside the archive; left alone")
            continue
        if not source.is_file():
            if row.audio_state == "present":
                # ``missing``, not ``purged``: the row said there was a file and there is
                # not, which is a fact about the disk, not a decision this pass made.  A
                # later pass that finds the file again puts the row back (R4).
                report.audio_missing += 1
                missing.append(row.id)
            continue
        # The file is there — including for a row an earlier pass marked ``missing``,
        # whose file has come back; it is retired like any other.
        size = source.stat().st_size
        doomed.append((row, source, size))
        report.audio_deleted += 1
        report.bytes_freed += size

    drop_text = [r.id for r, _, _ in doomed if not policies[r.id].keep_transcripts]  # type: ignore[attr-defined]
    if dry_run:
        _drop_transcripts(session, drop_text, None, report, dry_run=True)
        return

    if missing:
        session.execute(update(Call).where(Call.id.in_(missing)).values(audio_state="missing"))
    if doomed:
        stamp = datetime.now(UTC)
        keep_text = [r.id for r, _, _ in doomed if policies[r.id].keep_transcripts]  # type: ignore[attr-defined]
        purged = {"audio_state": "purged", "audio_archived_at": stamp, "processed_path": None}
        if keep_text:
            session.execute(update(Call).where(Call.id.in_(keep_text)).values(**purged))
        if drop_text:
            session.execute(
                update(Call)
                .where(Call.id.in_(drop_text))
                .values(**purged, transcript_text=None, transcript_quality=None)
            )
            _drop_transcripts(session, drop_text, None, report, dry_run=False)
    session.commit()

    for row, source, size in doomed:
        try:
            source.unlink()
        except FileNotFoundError:
            report.bytes_freed -= size  # gone already; the row is right either way
        except OSError as exc:
            report.audio_deleted -= 1
            report.bytes_freed -= size
            report.errors.append(f"purge {source}: {exc}")
            session.execute(
                update(Call)
                .where(Call.id == row.id, Call.audio_state == "purged")  # type: ignore[attr-defined]
                .values(
                    audio_state=row.audio_state,  # type: ignore[attr-defined]
                    audio_archived_at=None,
                    processed_path=row.processed_path,  # type: ignore[attr-defined]
                )
            )
            session.commit()
            continue
        report.bytes_freed += _drop_processed(row, settings, report)  # type: ignore[arg-type]
        prune_empty_dirs(source.parent, settings)


def _drop_transcripts(
    session: Session, call_ids: list[int], policy: Policy | None, report: RetentionReport, *, dry_run: bool
) -> int:
    """With ``keep_transcripts`` off, the text goes with the audio.  Returns how many went.

    ``policy`` ``None`` means the caller already picked the calls whose policy drops it.
    The caller clears ``calls.transcript_text`` in the UPDATE it is already making.
    """
    if not call_ids or (policy is not None and policy.keep_transcripts):
        return 0
    if dry_run:
        count = int(
            session.scalar(
                select(func.count()).select_from(Transcript).where(Transcript.call_id.in_(call_ids))
            )
            or 0
        )
    else:
        count = int(session.execute(delete(Transcript).where(Transcript.call_id.in_(call_ids))).rowcount or 0)
    report.transcripts_deleted += count
    return count
