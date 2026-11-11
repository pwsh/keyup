"""Put the backlog's calls back on the day they were recorded.

The sibling of :mod:`app.repair`: an operator command, run by hand, that cleans up after a
bug the archive already swallowed.  This one repairs **backlog uploads stamped with the
day they were uploaded** — a whole pre-2025 sdrtrunk archive, a million calls on a large one — and everything
that decision then poisoned.

What happened (docs/MIGRATION.md, "Timestamps")
-----------------------------------------------
``tools/migrate_sdrtrunk.py`` deliberately sends no ``dateTime``: it trusts Keyup's own
sdrtrunk parser to read the stamp out of ``audioName``.  Pre-2025 sdrtrunk writes that
name **without the underscore after the time** —
``20240223_210414Example_System_Central_T-Control__TO_1102_FROM_9226020.mp3`` — and
:data:`app.ingest.parser._NAME_RE` required one, so :func:`~app.ingest.parser.parse_filename`
raised for every one of them.  The push path's time chain then fell all the way through
(``dateTime`` absent, the parse failed, and the "mtime" it lands on for a push is the mtime
of the file Keyup itself had just written) to *now minus the duration*.  Every call in the
backlog was therefore stored on the upload date, seconds apart, with
``time_source='upload'`` claiming the recorder had said so.

Three things follow from that, and this module undoes all three:

* the **times** are wrong — a million calls on one day;
* **reconciliation** compared those times and matched most of them against each other,
  so most of the archive is hidden behind a ``duplicate_of`` pointing at an unrelated
  transmission;
* **conversations** were grouped by talkgroup and time gap over the same fiction.

The phases
----------
Each is batched, committed per batch and resumable, for the reason :mod:`app.repair`
spells out: the live feed keeps running beside a repair that touches a million rows, so no
phase may hold a lock for longer than one batch takes to write.

* **repair** (:func:`repair_timestamps`) — recompute ``recorded_at``/``ended_at``/
  ``started_at``/``started_local`` from the filename stamp exactly as the parser does for a
  file, drop ``duplicate_of``/``reconciled_at``/``reconcile_reason``, and set
  ``time_source='file_write'``, which is what the parser path sets for a stamp-derived
  time and is the truth about these rows.  It also frees the rows *pointing at* a repaired
  row — a call whose ``duplicate_of`` was never rewritten would stay hidden behind a
  transmission that has just moved to 2024 — and marks them so phase B rules on them too.
  Self-limiting: a repaired row no longer differs from its filename, so a second run finds
  nothing.
* **reconcile** (:func:`rereconcile_repaired`) — drive :func:`app.ingest.reconcile.find_match`
  and :func:`~app.ingest.reconcile.reconcile_pair` over the repaired rows so genuine
  duplicates re-link on their *true* times and everything else stays primary.  The
  scheduled pass cannot be used for this: it scans a bounded window per pass, and this is a
  million rows.
* **regroup** (:func:`regroup_repaired`, ``--regroup``) — take the repaired calls out of the
  threads built over the bogus times and re-run :func:`app.ingest.service.assign_conversation`
  over them in true chronological order, then drop the conversations left empty.

Nothing here deletes a call, and no call ever loses the transcript made from its bytes:
the bytes never changed, only the archive's belief about when they were recorded.

**Phase B does touch transcription, though**, because it is reconciliation and
reconciliation owns the queue: when it rules two rows one transmission, the loser's
``transcript_status`` becomes ``skipped`` and its *queued* jobs are cancelled, and when the
newcomer takes the primary seat (:func:`app.ingest.reconcile._swap`) the winner is enqueued
so the transmission the archive shows is the one with the text — in the **backlog** band
(:data:`REQUEUE_CLASS`), not the live path's priority 1, because a repair promotes hundreds
of thousands of primaries at once and every one of them at priority 1 is the live feed
stopped for as long as the repair runs.  A finished transcript is never deleted and a
running job is never killed — only queued work is redirected, from a copy to the row that
now represents it.  On this archive that is the point: the rulings being undone were made on
invented times, so the text was queued against the wrong row.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, text, tuple_, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.parser import ParseError, _localize, parse_filename
from app.models import Call, Conversation, ExternalLink, Setting

log = logging.getLogger("app.repair_timestamps")

__all__ = [
    "BATCH_FAULT_HOOK",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_REGROUP_BATCH_SIZE",
    "DEFAULT_TOLERANCE_SECONDS",
    "MAX_BATCH_ATTEMPTS",
    "REGROUP_ANALYZE_EVERY_BATCHES",
    "REGROUP_WATERMARK_KEY",
    "RECONCILE_DONE_KEY",
    "RECONCILE_WATERMARK_KEY",
    "REPAIR_RULE",
    "REPAIRED_TIME_SOURCE",
    "REQUEUE_CLASS",
    "TimestampRepair",
    "fix_upload_timestamps",
    "regroup_repaired",
    "repair_timestamps",
    "rereconcile_repaired",
]

#: Rows per committed transaction — :data:`app.repair.DEFAULT_BATCH_SIZE`'s reasoning, and
#: the same number, so the two repairs behave the same way beside a live feed.
DEFAULT_BATCH_SIZE = 20_000

#: Rows per committed transaction in **phase C**, which is a twentieth of the others on
#: purpose: it takes a ``pg_advisory_xact_lock`` per talkgroup and a transaction-scoped
#: advisory lock is held until the batch commits.  A 20,000-row batch would hold every
#: talkgroup it touched against the live feed for minutes; a thousand rows is seconds.
DEFAULT_REGROUP_BATCH_SIZE = 1_000

#: How often phase C re-``ANALYZE``s ``conversations``, in batches.
#:
#: The phase's cost per call is whatever the planner does with
#: :func:`app.ingest.service.assign_conversation`'s lookup, and that choice is made from
#: statistics — which this phase is in the business of invalidating.  It empties every
#: thread in the archive and builds a million new ones stamped 2022-2024, into a table whose
#: histogram says every row is stamped 2025-2026.  Under that histogram the planner reads
#: ``ended_at >= <2024>`` as matching everything and ``started_at <= <2024>`` as matching
#: one row, picks ``ix_conversations_talkgroup_started``, and then discards the talkgroup's
#: entire rebuilt history on every single call.
#:
#: Fifty batches is fifty thousand calls: often enough that the picture never drifts by
#: more than a few percent of the table, rare enough that the sampling (a few hundred
#: milliseconds, under a lock that blocks neither readers nor the live feed's writers) does
#: not show up against the batches themselves.
REGROUP_ANALYZE_EVERY_BATCHES = 50

#: How far the stored time may sit from the filename's before this counts as damage.  The
#: two disagree by milliseconds on a healthy row (the stamp is whole seconds), and by
#: months on a broken one; a minute is comfortably between them and keeps a row whose
#: recorder genuinely sent a ``dateTime`` a second off the stamp out of the repair.
DEFAULT_TOLERANCE_SECONDS = 60.0

#: What ``ingest_source`` the damage is confined to: the push path.  A file the watcher
#: ingested was never mis-stamped — it had the parse *and* the file's own mtime.
DAMAGED_INGEST_SOURCE = "upload"
#: The ``time_source`` a mis-stamped push carries: the endpoint writes ``upload`` for every
#: push, whether or not the recorder actually said when the call started.
DAMAGED_TIME_SOURCE = "upload"
#: What a stamp-derived time is called (:func:`app.ingest.parser.build_call_record`).  It is
#: also the marker the later phases select on: an ``upload`` row with a ``file_write`` time
#: is one this repair has rewritten, and nothing else in the pipeline produces that pair.
REPAIRED_TIME_SOURCE = "file_write"

#: The ``reconcile_reason['rule']`` written on a row phase A *unhid*: one whose
#: ``duplicate_of`` pointed at a row this repair rewrote.  It is both the audit trail for a
#: ruling this command dissolved and the marker phases B and C select those rows on, since
#: nothing about the row itself (it was never mis-stamped — it is a trunkd call, or an
#: unparsable name, or a live push that lost an interval match to a backlog row) says it
#: belongs to this repair.  ``rule`` is the key the call detail page and the reconciliation
#: histogram already read, so the marker prints as what it is rather than as nothing.
REPAIR_RULE = "timestamp repair"

#: ``settings`` rows holding each long phase's resume point.  Cleared when the phase
#: finishes, so a completed repair starts from the top next time rather than refusing to
#: run — both phases are idempotent, so the cost of that is time, never correctness.
RECONCILE_WATERMARK_KEY = "timestamp_repair_reconciled_through"
REGROUP_WATERMARK_KEY = "timestamp_repair_regrouped_through"

#: Set when phase B walks to the end, cleared by any later phase A that repairs or unhides
#: a row.  Without it "B finished, C was interrupted, run it again" re-walks a million rows
#: to make no decision at all — an hour of database time to confirm what the last run
#: already knew.  It is a completion marker, not a lock: it is cleared the moment there is
#: new work, and phase B is idempotent if it does run again.
RECONCILE_DONE_KEY = "timestamp_repair_reconciled_complete"

#: The priority class phase B's swaps re-enqueue a promoted primary at
#: (:func:`app.ingest.reconcile._swap`, :mod:`app.ingest.flow`).  The live path uses
#: priority 1 there, which is right for one call and catastrophic for a repair: the first
#: production run put 5,378 transcriptions of 2022-2024 calls in front of the live feed
#: before it was stopped.  ``backlog`` resolves to the talkgroup's own priority plus 500,
#: which is the band that is *meant* to be starved by live traffic.
REQUEUE_CLASS = "backlog"

#: How many unparsable names to keep for the summary.  They are the rows this repair
#: cannot help, and the operator needs to see what they look like, not all of them.
SAMPLE_LIMIT = 10

#: How many times one batch is re-run after Postgres picks it as the victim of a deadlock.
#: Five, because a retry only ever loses to a *different* transaction and the repair is the
#: only long one in the system: two collisions in a row is already unlucky, and a batch that
#: cannot get through in five is a lock problem the operator has to see rather than one more
#: sleep can fix.  Exhausting them raises, and the phase's watermark means the run resumes
#: from the last committed batch rather than from the top.
MAX_BATCH_ATTEMPTS = 5

#: The retry backoff, in seconds: a jittered sleep between these two, doubling its ceiling
#: each attempt.  Jitter matters more than the length here — two transactions that back off
#: by the same amount collide again — and the ceiling is short because what is being waited
#: for is a live ingest's transaction, which lasts milliseconds.
RETRY_MIN_SLEEP_S = 0.2
RETRY_MAX_SLEEP_S = 2.0

#: The ``psycopg`` errors that mean "your transaction lost a race; run it again".  Both are
#: retryable by definition — Postgres has already rolled the transaction back, and nothing
#: about the batch's *work* was wrong.
_RETRYABLE = frozenset({"DeadlockDetected", "SerializationFailure"})

#: A seam for the test that has to prove the retry works: called with ``(phase, attempt)``
#: at the top of every batch attempt, inside the ``try``, so a test can raise the deadlock
#: Postgres would otherwise have to be provoked into producing.  ``None`` in production.
BATCH_FAULT_HOOK: Callable[[str, int], None] | None = None

#: Called once per committed batch with a progress event; see :func:`repair_timestamps`.
Progress = Callable[[dict[str, Any]], None]


@dataclass
class TimestampRepair:
    """What the three phases did, in the shape the CLI prints."""

    dry_run: bool = True
    tolerance_s: float = DEFAULT_TOLERANCE_SECONDS
    batch_size: int = DEFAULT_BATCH_SIZE

    #: phase A
    scanned: int = 0
    repaired: int = 0
    #: rows whose ``duplicate_of`` pointed at a row phase A rewrote, and which phase A
    #: therefore made primary again — they were hidden behind a call that has just moved
    #: to 2024, and they are not themselves repairable (see :data:`REPAIR_RULE`)
    unhidden: int = 0
    already_correct: int = 0
    unparsed: int = 0
    unparsed_sample: list[str] = field(default_factory=list)
    earliest: datetime | None = None
    latest: datetime | None = None

    #: phase B
    reconcile_ran: bool = False
    #: True when phase B was asked for and found its completion marker already set
    reconcile_skipped: bool = False
    reconcile_scanned: int = 0
    reconcile_pairs: int = 0
    reconcile_swaps: int = 0

    #: phase C
    regroup_ran: bool = False
    regroup_batch_size: int = DEFAULT_REGROUP_BATCH_SIZE
    regrouped: int = 0
    conversations_emptied: int = 0
    #: emptied threads left standing because another system's record points at them
    conversations_kept_linked: int = 0
    #: rows carrying the repaired marker and still to be regrouped, when ``--regroup``
    #: was not asked for.  It is the one piece of unfinished business a successful run can
    #: leave behind, so the summary says so out loud.
    regroup_pending: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "tolerance_s": self.tolerance_s,
            "batch_size": self.batch_size,
            "scanned": self.scanned,
            "repaired": self.repaired,
            "unhidden": self.unhidden,
            "already_correct": self.already_correct,
            "unparsed": self.unparsed,
            "unparsed_sample": self.unparsed_sample,
            "earliest_repaired_start": self.earliest.isoformat() if self.earliest else None,
            "latest_repaired_start": self.latest.isoformat() if self.latest else None,
            "reconcile": {
                "ran": self.reconcile_ran,
                "skipped": self.reconcile_skipped,
                "scanned": self.reconcile_scanned,
                "pairs": self.reconcile_pairs,
                "swaps": self.reconcile_swaps,
            },
            "regroup": {
                "ran": self.regroup_ran,
                "batch_size": self.regroup_batch_size,
                "calls": self.regrouped,
                "conversations_emptied": self.conversations_emptied,
                "conversations_kept_linked": self.conversations_kept_linked,
                "pending": self.regroup_pending,
            },
        }


# --------------------------------------------------------------------------------------
# the resume points
# --------------------------------------------------------------------------------------


def _watermark(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    value = None if row is None else row.value
    return str(value) if value else None


def _set_watermark(session: Session, key: str, value: str | None) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, description="Backlog timestamp repair resume point")
        session.add(row)
    row.value = value


# --------------------------------------------------------------------------------------
# the batch: the lock order, and surviving a lost race for it
# --------------------------------------------------------------------------------------

def _lock_talkgroup(session: Session, talkgroup_id: int | None) -> bool:
    """Take the conversation lock for one talkgroup, exactly as live ingest takes it.

    :func:`app.ingest.service.assign_conversation` ends every ingest with
    ``pg_advisory_xact_lock(4242, tgid)`` and *then* writes the conversation row.  Phase C
    used to do the reverse — dirty the ``Conversation`` row by leaving it, which takes that
    row's write lock at the next flush, and only then call ``assign_conversation`` for the
    advisory one — which is an ABBA deadlock against every live call on the same talkgroup.

    So the advisory lock is taken before anything touches a conversation row, and
    ``assign_conversation``'s own request for it later in the same transaction is free (a
    session already holding an advisory lock re-acquires it without blocking).

    Held until the batch commits, which is why the batch that takes a whole set of them is
    a twentieth of the others (:data:`DEFAULT_REGROUP_BATCH_SIZE`).
    """
    if talkgroup_id is None:
        return False
    from app.ingest.service import CONVERSATION_LOCK_CLASS  # noqa: PLC0415 - cycle

    session.execute(
        text("SELECT pg_advisory_xact_lock(:cls, :key)"),
        {"cls": CONVERSATION_LOCK_CLASS, "key": talkgroup_id},
    )
    return True


def _lock_talkgroups(session: Session, talkgroup_ids: Iterable[int | None], held: set[int]) -> None:
    """Take a whole batch's conversation locks up front, **in ascending talkgroup order**.

    Taking the lock before the write is not enough on its own once a transaction wants more
    than one of them.  A batch spans thousands of talkgroups, so it accumulates thousands of
    transaction-scoped advisory locks, and two transactions each holding several deadlock on
    the pair they want *next* even though each is individually well-behaved.  That is the
    production failure: phase B took these in primary-key order as it walked its rows, the
    scheduled reconcile pass took them in its own window's order, and the two met head-on at
    86 % of a million rows.

    A total order over the keys removes the cycle outright — every transaction that wants
    two of these locks asks for the lower one first, so one of the two always already holds
    everything the other is waiting behind.  Live ingest takes exactly one, its own
    talkgroup's, which is trivially in order.

    ``held`` is what this transaction already has, so each key is asked for once per batch
    and ``assign_conversation``'s later request for it is the free re-acquisition.
    """
    for talkgroup_id in sorted({t for t in talkgroup_ids if t is not None} - held):
        _lock_talkgroup(session, talkgroup_id)
        held.add(talkgroup_id)


def _retryable(exc: BaseException) -> bool:
    """Is this the database saying "run that again", rather than something being wrong?"""
    return type(getattr(exc, "orig", None)).__name__ in _RETRYABLE


def _batch[T](session: Session, phase: str, work: Callable[[], T]) -> T:
    """Run one batch, and run it again if Postgres kills it for a deadlock.

    Every phase here is a loop of self-contained, committed batches — that is what makes
    the repair resumable — so a batch that was rolled back can simply be re-read and
    re-done.  ``work`` therefore has to include its own ``SELECT``: after a rollback the
    ORM's identity map is stale, so it is expunged and the batch starts from the database
    again.  Nothing is counted into the report until the batch commits, so a retried batch
    does not report its rows twice.

    Ordering (:func:`_lock_talkgroups`) is what makes deadlocks rare; this is what keeps the
    ones that remain — a lock taken out of order because a match crossed talkgroups, or a
    plain row-level cycle with a live ingest — costing a second instead of the whole run.
    """
    for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
        try:
            if BATCH_FAULT_HOOK is not None:
                BATCH_FAULT_HOOK(phase, attempt)
            return work()
        except DBAPIError as exc:
            session.rollback()
            session.expunge_all()
            if not _retryable(exc) or attempt == MAX_BATCH_ATTEMPTS:
                raise
            delay = random.uniform(  # noqa: S311 - jitter, not a secret
                RETRY_MIN_SLEEP_S, min(RETRY_MAX_SLEEP_S, RETRY_MIN_SLEEP_S * 2**attempt)
            )
            log.warning(
                "timestamp repair: %s batch lost a lock race (%s), attempt %d of %d; "
                "retrying in %.2fs",
                phase,
                type(exc.orig).__name__,
                attempt,
                MAX_BATCH_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------------------
# phase A — the timestamps
# --------------------------------------------------------------------------------------


@dataclass
class _RepairDelta:
    """One phase A batch's findings, held back until the batch commits.

    A batch that deadlocks is rolled back and run again (:func:`_batch`), so nothing it
    counted may reach the report before its ``COMMIT`` — otherwise a retry reports the same
    rows twice and the summary lies about a million-row run.
    """

    cursor: int = 0
    scanned: int = 0
    repaired: int = 0
    unhidden: int = 0
    already_correct: int = 0
    unparsed: int = 0
    unparsed_sample: list[str] = field(default_factory=list)
    earliest: datetime | None = None
    latest: datetime | None = None

    def merge_into(self, report: TimestampRepair) -> None:
        report.scanned += self.scanned
        report.repaired += self.repaired
        report.unhidden += self.unhidden
        report.already_correct += self.already_correct
        report.unparsed += self.unparsed
        report.unparsed_sample.extend(self.unparsed_sample)
        if self.earliest is not None:
            report.earliest = min(report.earliest or self.earliest, self.earliest)
        if self.latest is not None:
            report.latest = max(report.latest or self.latest, self.latest)


def _damaged(cursor: int, batch_size: int):  # noqa: ANN202 - a Select of four columns
    """The next page of candidate rows: a push, claiming a recorder-supplied time.

    ``ingest_source='upload'`` is already the narrowing — trunkd and trunk-recorder pushes
    carry their own and are not this dialect — and nothing narrows it further on purpose:
    a name the parser cannot read has to reach :func:`parse_filename` to be *counted* as
    one.  Those rows are the repair's own blind spot, and the summary has to name them.
    """
    return (
        select(Call.id, Call.file_name, Call.started_at, Call.duration_s)
        .where(
            Call.ingest_source == DAMAGED_INGEST_SOURCE,
            Call.time_source == DAMAGED_TIME_SOURCE,
            Call.id > cursor,
        )
        .order_by(Call.id)
        .limit(batch_size)
    )


def _unhide(session: Session, repaired_ids: list[int]) -> int:
    """Free the rows still pointing at a row phase A has just rewritten.

    Phase A clears the ruling on every row it *rewrites*, but a ``duplicate_of`` is a
    pointer between two rows and only one end of it is necessarily in scope.  The other end
    is a real population: a backlog row whose name the parser still cannot read, a live
    sdrtrunk push that sent a ``dateTime`` and was interval-matched to a mis-stamped
    backlog row, a trunkd call that *lost* a swap to one (:func:`app.ingest.reconcile._swap`
    repoints the whole chain at the winner).  None of those rows is mis-stamped, so none of
    them is repaired — and if nothing frees them they stay hidden behind a transmission
    that has just moved to 2024, which is the same invisibility this repair exists to undo.

    So the ruling is dissolved from this end too, and the row is marked with
    :data:`REPAIR_RULE` rather than blanked: phases B and C have no other way to find a row
    that belongs to this repair without belonging to the damage.
    """
    if not repaired_ids:
        return 0
    result = session.execute(
        update(Call)
        .where(Call.duplicate_of.in_(repaired_ids))
        .values(
            duplicate_of=None,
            reconciled_at=None,
            reconcile_reason={
                "rule": REPAIR_RULE,
                "note": "the row this pointed at was re-stamped from its filename; "
                "the ruling that hid this one was made on the wrong times",
                "at": datetime.now(UTC).isoformat(),
            },
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def repair_timestamps(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    tolerance_s: float = DEFAULT_TOLERANCE_SECONDS,
    report: TimestampRepair | None = None,
    on_batch: Progress | None = None,
) -> TimestampRepair:
    """Rewrite every pushed call's time from the stamp in its own filename.

    The derivation is the parser's, not a re-implementation of it: the stamp is the instant
    sdrtrunk *closed* the file, so ``recorded_at`` and ``ended_at`` are that instant in the
    archive's own zone and ``started_at`` is it minus the duration already measured off the
    audio (:func:`app.ingest.parser.build_call_record`).  ``duration_s`` is never touched —
    it came from the MP3's own frame headers and was always right.

    ``duplicate_of``, ``reconciled_at`` and ``reconcile_reason`` are cleared, because every
    one of them was a ruling made on the fiction; phase B rules again on the truth.  The
    same is done for the rows pointing *at* a repaired row from outside the damage
    (:func:`_unhide`), which would otherwise stay hidden behind a call that just moved to
    2024.

    Idempotent and resumable without a watermark: a repaired row's stored time now agrees
    with its filename to the millisecond and its ``time_source`` is no longer ``upload``,
    so it drops out of this query on the next pass.  Interrupt it and run it again.

    Each batch is a transaction that can be re-run (:func:`_batch`): this phase takes no
    advisory lock, but it updates a million rows beside a live feed that is updating some
    of the same ones, and a plain row-level cycle is a deadlock like any other.
    """
    report = report or TimestampRepair(dry_run=dry_run, tolerance_s=tolerance_s, batch_size=batch_size)
    zone = ZoneInfo(settings.tz)
    tolerance = timedelta(seconds=tolerance_s)
    cursor = 0
    batch = 0

    def run() -> _RepairDelta | None:
        rows = session.execute(_damaged(cursor, batch_size)).all()
        if not rows:
            return None
        delta = _RepairDelta(cursor=rows[-1].id)
        payloads: list[dict[str, Any]] = []

        for row in rows:
            delta.scanned += 1
            try:
                parts = parse_filename(row.file_name)
            except ParseError:
                parts = None
            if parts is None or parts.stamp is None:
                delta.unparsed += 1
                if len(report.unparsed_sample) + len(delta.unparsed_sample) < SAMPLE_LIMIT:
                    delta.unparsed_sample.append(row.file_name)
                continue

            recorded_at = _localize(parts.stamp, zone)
            started_at = recorded_at - timedelta(seconds=float(row.duration_s or 0))
            if abs(started_at - row.started_at) <= tolerance:
                delta.already_correct += 1
                continue

            delta.repaired += 1
            delta.earliest = min(delta.earliest or started_at, started_at)
            delta.latest = max(delta.latest or started_at, started_at)
            payloads.append(
                {
                    "id": row.id,
                    "recorded_at": recorded_at,
                    "ended_at": recorded_at,
                    "started_at": started_at,
                    "started_local": started_at.astimezone(zone).replace(tzinfo=None),
                    "time_source": REPAIRED_TIME_SOURCE,
                    # Every ruling below was made on the wrong times.  Phase B rules again.
                    "duplicate_of": None,
                    "reconciled_at": None,
                    "reconcile_reason": None,
                }
            )

        if dry_run:
            return delta
        if payloads:
            session.execute(update(Call), payloads)
            # After the rewrite, never before: a row repaired in this same batch has just
            # had its own ``duplicate_of`` cleared, so what is left pointing at these ids
            # is exactly the population phase A cannot repair.
            delta.unhidden += _unhide(session, [p["id"] for p in payloads])
            # There is new work for phase B, whatever a previous run finished.
            _set_watermark(session, RECONCILE_DONE_KEY, None)
        session.commit()
        return delta

    while True:
        delta = _batch(session, "repair", run)
        if delta is None:
            break
        cursor = delta.cursor
        batch += 1
        delta.merge_into(report)
        if on_batch is not None:
            on_batch(
                {
                    "phase": "repair",
                    "batch": batch,
                    "scanned": report.scanned,
                    "repaired": report.repaired,
                    "unhidden": report.unhidden,
                    "through_id": cursor,
                }
            )

    if dry_run:
        session.rollback()
    log.info(
        "timestamp repair: scanned=%d repaired=%d unhidden=%d already_correct=%d unparsed=%d%s",
        report.scanned,
        report.repaired,
        report.unhidden,
        report.already_correct,
        report.unparsed,
        " (dry run)" if dry_run else "",
    )
    return report


# --------------------------------------------------------------------------------------
# phase B — reconciliation, on the true times
# --------------------------------------------------------------------------------------


def _repair_marked():  # noqa: ANN202 - a boolean clause
    """Every row this repair touched, in either of the two ways it touches one.

    A row it **rewrote** is a push whose time now comes from its filename — a pair nothing
    else in the pipeline produces.  A row it **unhid** carries :data:`REPAIR_RULE`, because
    it was never mis-stamped and nothing about it would otherwise say it is part of this.
    Phases B and C both walk the union: an unhidden row is a primary again with no ruling
    on it and, having left its thread when it became a duplicate, no conversation either.
    """
    return or_(
        and_(
            Call.ingest_source == DAMAGED_INGEST_SOURCE,
            Call.time_source == REPAIRED_TIME_SOURCE,
        ),
        Call.reconcile_reason["rule"].astext == REPAIR_RULE,
    )


def _repaired(cursor: int, batch_size: int):  # noqa: ANN202 - a Select of one column
    """The rows this repair has rewritten or unhidden, by id."""
    return (
        select(Call.id)
        .where(_repair_marked(), Call.id > cursor)
        .order_by(Call.id)
        .limit(batch_size)
    )


def rereconcile_repaired(
    session: Session,
    settings: Settings,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    report: TimestampRepair | None = None,
    on_batch: Progress | None = None,
) -> TimestampRepair:
    """Ask reconciliation again, now that the calls are on the right day.

    Phase A left every repaired row a primary with no ruling on it, which is the state a
    freshly ingested call is in — so this is the batch pass' loop
    (:func:`app.ingest.reconcile.reconcile_batch`) restricted to those rows and paged by
    primary key with a ``COMMIT`` and a watermark per page.  The scheduled pass is not an
    option here: it takes a bounded window per run, and a million rows would need weeks of
    them.

    Like the pass it borrows from, it takes no channel lock: only primaries are examined
    and only primaries are matched, so a live push arriving mid-batch is reconciled against
    whatever is committed at that moment and the next run sees the result.

    It **does** take every conversation lock its batch will need, up front and in ascending
    talkgroup order (:func:`_lock_talkgroups`).  A swap moves the thread membership to the
    new primary, which ends in ``assign_conversation`` and its
    ``pg_advisory_xact_lock(4242, tgid)``; a 2,000-row batch is one transaction, so it
    accumulates one of those per talkgroup it touches, and taking them in the order the rows
    happen to be in is what deadlocked this phase against the scheduled reconcile pass at
    86 % of a million rows.

    A match can cross talkgroups — ``sha256`` matches on bytes alone and the interval rung
    also keys on ``frequency_hz`` — so the candidate's talkgroup is locked too, as soon as
    :func:`~app.ingest.reconcile.find_match` names it and before anything is written.  That
    one *can* be out of order, since it is not knowable before the match: it is rare (both
    copies of a transmission almost always carry the same talkgroup, and then the key is
    already held), and it is what :func:`_batch`'s retry is for.

    It is **skipped outright** once :data:`RECONCILE_DONE_KEY` says a previous run walked to
    the end and no phase A has repaired anything since.  Re-running the command to finish an
    interrupted phase C is the normal way this repair is driven, and without that marker it
    would first re-walk a million rows to make no decision at all.

    Reconciliation owns the transcription queue, so this phase moves queued work: a row it
    rules a duplicate has its ``transcript_status`` set to ``skipped`` and its queued jobs
    cancelled, and a swap enqueues the new primary at **backlog** priority.  That last is
    the one thing this phase asks reconciliation to do differently from the live path
    (``requeue_class``): a promoted primary from 2022 is not urgent, and there are hundreds
    of thousands of them — queued at priority 1 they would sit in front of every live call
    for as long as the repair ran.  Finished text is never deleted (module docstring).
    """
    from app.ingest.reconcile import (  # noqa: PLC0415 - only this phase needs it
        ReconcileConfig,
        _arrived_after,  # the batch pass' own tie-break; see reconcile_batch
        find_match,
        reconcile_pair,
    )
    from app.runtime_settings import effective_settings  # noqa: PLC0415

    report = report or TimestampRepair(dry_run=False)
    report.reconcile_ran = True
    # A swap re-queues the promoted primary's audio pass: it has to see the Admin page's
    # ``audio_process_enabled``, not the environment's default of on.
    settings = effective_settings(session, base=settings)
    cfg = ReconcileConfig.of(settings)
    if not cfg.enabled:
        log.info("timestamp repair: reconciliation disabled (RECONCILE_ENABLED=false)")
        return report

    if _watermark(session, RECONCILE_DONE_KEY):
        report.reconcile_skipped = True
        log.info(
            "timestamp repair: reconciliation already complete for this repair — "
            "nothing has been repaired since it finished"
        )
        return report

    cursor = int(_watermark(session, RECONCILE_WATERMARK_KEY) or 0)
    batch = 0

    def run() -> tuple[int, int, int, int] | None:
        ids = list(session.scalars(_repaired(cursor, batch_size)))
        if not ids:
            return None
        # Every conversation lock this batch can be *predicted* to need, lowest key first,
        # before it writes anything at all.
        held: set[int] = set()
        _lock_talkgroups(
            session,
            session.scalars(
                select(Call.talkgroup_id).distinct().where(
                    Call.id.in_(ids), Call.talkgroup_id.is_not(None)
                )
            ),
            held,
        )

        scanned = pairs = swaps = 0
        resolved: set[int] = set()
        for call_id in ids:
            call = session.get(Call, call_id)
            scanned += 1
            if call is None or call.id in resolved or call.duplicate_of is not None:
                continue
            other, match = find_match(session, call, cfg, exclude=resolved)
            if other is None:
                continue
            # The match may live on another talkgroup (sha256, or a shared frequency), and
            # a swap would then write that talkgroup's thread.  Take its lock before the
            # ruling rather than inside ``assign_conversation``, where the conversation row
            # is already dirty.
            _lock_talkgroups(session, (other.talkgroup_id,), held)
            pairs += 1
            # A tie leaves the existing primary in place, so the row that reached the
            # archive later has to be the one asking for the seat — exactly as at ingest.
            newcomer, existing = (call, other) if _arrived_after(call, other) else (other, call)
            outcome = reconcile_pair(
                session,
                newcomer,
                existing,
                settings,
                cfg=cfg,
                match=match,
                # Both rows were primaries before the ruling; the loser has to leave any
                # live list that is showing it.
                newcomer_was_primary=True,
                requeue_class=REQUEUE_CLASS,
            )
            swaps += int(outcome.swapped)
            resolved.add(outcome.duplicate_id or call.id)
        _set_watermark(session, RECONCILE_WATERMARK_KEY, str(ids[-1]))
        session.commit()
        session.expunge_all()
        return ids[-1], scanned, pairs, swaps

    while True:
        result = _batch(session, "reconcile", run)
        if result is None:
            break
        cursor, scanned, pairs, swaps = result
        batch += 1
        report.reconcile_scanned += scanned
        report.reconcile_pairs += pairs
        report.reconcile_swaps += swaps
        if on_batch is not None:
            on_batch(
                {
                    "phase": "reconcile",
                    "batch": batch,
                    "scanned": report.reconcile_scanned,
                    "pairs": report.reconcile_pairs,
                    "through_id": cursor,
                }
            )

    _set_watermark(session, RECONCILE_WATERMARK_KEY, None)
    # Walked to the end: say so, so a re-run that only wants phase C does not walk it again.
    _set_watermark(session, RECONCILE_DONE_KEY, "1")
    session.commit()
    log.info(
        "timestamp repair: reconciled scanned=%d pairs=%d swaps=%d",
        report.reconcile_scanned,
        report.reconcile_pairs,
        report.reconcile_swaps,
    )
    return report


# --------------------------------------------------------------------------------------
# phase C — conversations
# --------------------------------------------------------------------------------------


def _split_regroup_cursor(raw: str | None) -> tuple[datetime, int]:
    """``<iso>|<id>`` -> the keyset this phase resumes from."""
    if not raw or "|" not in raw:
        return datetime.min.replace(tzinfo=UTC), 0
    when, _, ident = raw.rpartition("|")
    try:
        return datetime.fromisoformat(when), int(ident)
    except ValueError:  # pragma: no cover - a hand-edited row
        return datetime.min.replace(tzinfo=UTC), 0


def _leave_conversation(session: Session, call: Call, *, clear: bool = True) -> None:
    """Take one call out of its thread, keeping the thread's ``call_count`` honest.

    The same bookkeeping reconciliation does when a copy stops representing a
    transmission; here the call is leaving a thread it only ever joined because its
    timestamp said 2026-09-01.

    ``clear=False`` does the thread's bookkeeping and leaves ``calls.conversation_id``
    alone, for the caller that is about to write it anyway.  ``calls`` is the widest table
    in the archive, and nulling the column here only to have
    :func:`app.ingest.service.assign_conversation` write it again a moment later cost two
    UPDATEs on every regrouped row: the null was flushed on its own by the autoflush the
    grouper's first query triggers.  Which conversation the grouper finds does not depend
    on the column — it searches by talkgroup and time over the ``conversations`` rows,
    whose bookkeeping is done either way — so deferring the write changes nothing but the
    number of statements.
    """
    if call.conversation_id is None:
        return
    conv = session.get(Conversation, call.conversation_id)
    if conv is not None:
        conv.call_count = max(0, conv.call_count - 1)
        if conv.first_call_id == call.id:
            conv.first_call_id = None
    if clear:
        call.conversation_id = None


def _drop_empty_conversations(session: Session, batch_size: int) -> tuple[int, int]:
    """Delete the threads the regroup emptied, except the ones another system points at.

    A conversation exists only as the grouping of the calls in it, so one with no members
    is not a thread that lost its calls — it is the bogus grouping this repair dissolved.
    Batched by id for the same reason everything else here is: the archive holds hundreds
    of thousands of them and the live feed is inserting more.

    **An emptied thread with an ``external_links`` row is kept.**  That foreign key is
    ``ON DELETE CASCADE`` (:class:`app.models.ExternalLink`), so deleting the thread would
    silently delete a CAD integration's incident link or an operator's manual one — a record
    from another system, which this repair has no business destroying to tidy up a grouping.
    They are counted and reported instead, and the paging walks by id so a kept row does not
    come back forever.

    Returns ``(dropped, kept)``.
    """
    dropped = kept = 0
    cursor = 0
    while True:
        ids = list(
            session.scalars(
                select(Conversation.id)
                .where(
                    Conversation.id > cursor,
                    ~select(Call.id).where(Call.conversation_id == Conversation.id).exists(),
                )
                .order_by(Conversation.id)
                .limit(batch_size)
            )
        )
        if not ids:
            return dropped, kept
        cursor = ids[-1]
        linked = set(
            session.scalars(
                select(ExternalLink.conversation_id).where(
                    ExternalLink.conversation_id.in_(ids)
                )
            )
        )
        droppable = [i for i in ids if i not in linked]
        kept += len(ids) - len(droppable)
        if droppable:
            session.execute(delete(Conversation).where(Conversation.id.in_(droppable)))
            dropped += len(droppable)
        session.commit()


def _analyze_conversations(session: Session) -> None:
    """Re-sample ``conversations`` so the grouper's lookup is planned on what is there now.

    Phase C is a statistics catastrophe by construction.  It moves a million calls out of a
    few enormous upload-day threads and builds a million small ones stamped across
    2022-2024, in a table whose planner statistics were gathered when every row in it was
    stamped 2025-2026.  Under that stale histogram
    :func:`app.ingest.service.assign_conversation`'s lookup —
    ``talkgroup_id = ? AND ended_at >= ? AND started_at <= ? ORDER BY ended_at DESC LIMIT 1``
    — reads its ``started_at <=`` bound as matching about one row and its ``ended_at >=``
    bound as matching every row, so the planner takes
    ``ix_conversations_talkgroup_started``, walks the talkgroup's whole rebuilt history, and
    discards it to check the bound it did not index.  That is O(calls already regrouped on
    this talkgroup) *per call*: the phase gets slower the further into the archive it gets,
    which is exactly how it was seen to behave.

    ``ix_conversations_talkgroup_ended`` gives the planner a plan that answers the whole
    question at once, but it will not choose it while its picture of the table is a fiction.
    So the phase keeps the picture current.

    ``ANALYZE`` takes ``SHARE UPDATE EXCLUSIVE``, which blocks neither readers nor the live
    feed's inserts, and it runs between batches — never inside one — so it holds nothing
    while the phase holds its per-talkgroup advisory locks.
    """
    session.execute(text("ANALYZE conversations"))
    session.commit()


def regroup_repaired(
    session: Session,
    settings: Settings,
    *,
    batch_size: int = DEFAULT_REGROUP_BATCH_SIZE,
    report: TimestampRepair | None = None,
    on_batch: Progress | None = None,
) -> TimestampRepair:
    """Rebuild the conversations of the repaired calls, in true chronological order.

    A conversation is "one talkgroup, calls no more than ``CONVERSATION_GAP_SECONDS``
    apart" (:func:`app.ingest.service.assign_conversation`), and the repaired rows were
    grouped when every one of them claimed to be seconds from every other — so a day of
    the archive became a handful of enormous threads.  Each repaired call therefore leaves
    the thread it is in and is offered to the grouping function again.

    **Ordered by ``started_at``, not by id**, which is the one thing that cannot be
    borrowed from the other phases: the grouper attaches a call to a thread it *finds*, and
    walking a talkgroup's calls out of order builds two threads where the archive should
    have one.  The keyset watermark carries the same ordering, so an interrupted regroup
    resumes without re-walking what it finished.

    **Every talkgroup the batch touches is locked first**, in ascending order, before any
    call leaves its thread (:func:`_lock_talkgroups`) — the same locks a live call being
    ingested on those talkgroups takes, asked for in an order that cannot make a cycle with
    anything else asking for more than one.  They are transaction-scoped, so the batch is a
    twentieth of the other phases' by default: it is measured in how long the live feed can
    be made to wait, not in how many rows fit in a transaction.
    """
    from app.ingest.service import assign_conversation  # noqa: PLC0415 - cycle

    report = report or TimestampRepair(dry_run=False)
    report.regroup_ran = True
    gap = settings.conversation_gap_seconds
    started_at, ident = _split_regroup_cursor(_watermark(session, REGROUP_WATERMARK_KEY))
    batch = 0
    # Before the first batch as much as between them: a resumed run starts in the middle of
    # a table phase A and an earlier phase C have already rewritten out from under the
    # statistics (:func:`_analyze_conversations`).
    _analyze_conversations(session)

    def run() -> tuple[datetime, int, int] | None:
        calls = list(
            session.scalars(
                select(Call)
                .where(
                    _repair_marked(),
                    tuple_(Call.started_at, Call.id) > (started_at, ident),
                )
                .order_by(Call.started_at, Call.id)
                .limit(batch_size)
            )
        )
        if not calls:
            return None
        # First, and for every mutation below: live ingest takes this lock and then writes
        # the conversation row, so anything that writes a conversation row must take it
        # first — and a batch that takes several must take them in one fixed order, or two
        # of these transactions deadlock on the pair they each want next.
        held: set[int] = set()
        _lock_talkgroups(session, (c.talkgroup_id for c in calls), held)
        for call in calls:
            # A duplicate is not a key-up of its own — it is the same one, twice — so it
            # leaves its thread and does not join another; the copy that won represents it.
            # So is a call with no talkgroup, which the grouper has nothing to group by.
            rejoins = call.duplicate_of is None and call.talkgroup_id is not None
            # ``clear`` only where nothing is going to write the column afterwards: one
            # UPDATE per regrouped call on ``calls`` instead of two.
            _leave_conversation(session, call, clear=not rejoins)
            if rejoins:
                assign_conversation(session, call, gap)
        last = calls[-1]
        _set_watermark(session, REGROUP_WATERMARK_KEY, f"{last.started_at.isoformat()}|{last.id}")
        through_at, through_id = last.started_at, last.id
        session.commit()
        session.expunge_all()
        return through_at, through_id, len(calls)

    while True:
        result = _batch(session, "regroup", run)
        if result is None:
            break
        started_at, ident, count = result
        batch += 1
        report.regrouped += count
        if batch % REGROUP_ANALYZE_EVERY_BATCHES == 0:
            _analyze_conversations(session)
        if on_batch is not None:
            on_batch(
                {
                    "phase": "regroup",
                    "batch": batch,
                    "calls": report.regrouped,
                    "through": started_at.isoformat(),
                }
            )

    report.regroup_batch_size = batch_size
    report.conversations_emptied, report.conversations_kept_linked = _drop_empty_conversations(
        session, batch_size
    )
    _set_watermark(session, REGROUP_WATERMARK_KEY, None)
    session.commit()
    log.info(
        "timestamp repair: regrouped %d calls, dropped %d emptied conversations, "
        "kept %d that another system links to",
        report.regrouped,
        report.conversations_emptied,
        report.conversations_kept_linked,
    )
    return report


# --------------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------------


def fix_upload_timestamps(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    regroup_batch_size: int = DEFAULT_REGROUP_BATCH_SIZE,
    tolerance_s: float = DEFAULT_TOLERANCE_SECONDS,
    reconcile: bool = True,
    regroup: bool = False,
    on_batch: Progress | None = None,
) -> dict[str, Any]:
    """The three phases in order, as ``keyup fix-upload-timestamps`` runs them.

    A dry run does phase A's arithmetic and nothing else: it reports what it *would*
    rewrite and how many rows the later phases would then have to walk, and rolls back.
    Running phases B and C for real is the only way to preview them — they are decisions,
    not calculations — and doing that against a million rows to throw the answer away is
    hours of database time for a number the summary can already state.

    Phase C keeps its own, smaller batch size: it is the only phase that holds a lock the
    live feed waits on (:data:`DEFAULT_REGROUP_BATCH_SIZE`).
    """
    report = TimestampRepair(
        dry_run=dry_run,
        tolerance_s=tolerance_s,
        batch_size=batch_size,
        regroup_batch_size=regroup_batch_size,
    )
    repair_timestamps(
        session,
        settings,
        dry_run=dry_run,
        batch_size=batch_size,
        tolerance_s=tolerance_s,
        report=report,
        on_batch=on_batch,
    )
    if not dry_run and reconcile:
        rereconcile_repaired(
            session, settings, batch_size=batch_size, report=report, on_batch=on_batch
        )
    if not dry_run and regroup:
        regroup_repaired(
            session, settings, batch_size=regroup_batch_size, report=report, on_batch=on_batch
        )

    if dry_run:
        # Nothing was written, so the later phases' work is "what phase A would have made"
        # plus whatever an interrupted earlier run already left carrying the marker.
        report.regroup_pending = report.repaired + _marked(session)
    elif not regroup:
        report.regroup_pending = _marked(session)
    if report.regroup_pending and not report.regroup_ran:
        log.warning(
            "timestamp repair: %d calls are still grouped into conversations built on the "
            "wrong times — run again with --regroup",
            report.regroup_pending,
        )
    return report.as_dict()


def _marked(session: Session) -> int:
    """How many rows carry a repair marker — the work phase C still has in front of it."""
    return int(session.scalar(select(func.count()).select_from(Call).where(_repair_marked())) or 0)
