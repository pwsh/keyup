"""Free the calls reconciliation ruled duplicates **of themselves**.

The sibling of :mod:`app.repair` and :mod:`app.repair_timestamps`: an operator command,
run by hand, that cleans up after a bug the archive already swallowed.

What happened
-------------
:func:`app.ingest.reconcile._find`'s exact rung matched on ``sha256`` and then followed the
row it found to its primary (:func:`~app.ingest.reconcile._primary_of`).  ``exclude`` — the
caller's own id, plus whatever the batch pass had already resolved — was applied to the
*hit*, never to the primary the hit resolves to.  So when the same bytes were stored twice
and the second row already pointed at the first (``B.duplicate_of == A.id``, a recorder
retransmitting a file), a pass over ``A`` found ``B``, resolved it back to ``A``, and handed
:func:`~app.ingest.reconcile.reconcile_pair` the pair ``(A, A)``.  Two identical rows score
identically, a tie leaves the *existing* primary in place, and the tie's loser is the
newcomer — the same row — so the write was ``A.duplicate_of = A.id``.

A large archive carried **tens of thousands** of such rows: ``reconcile_reason.rule = 'tie'``, ``match.rule =
'identical sha256'``, a ``scores`` dict with a single call id in it.  Live calls every
day, and every migrated backlog recording.  Each one is invisible:
every API listing filters on ``duplicate_of IS NULL``, the row was taken out of its
conversation, and a transcript that was pending or queued was set to ``skipped`` with its
jobs cancelled.  The call is not lost — the bytes, the metadata and any finished transcript
are all still on the row — it simply cannot be reached.

The repair
----------
:func:`repair_self_duplicates` walks ``duplicate_of = id`` and, for each row:

* clears ``duplicate_of`` and ``reconciled_at``, and replaces ``reconcile_reason`` with a
  ``repair:self-duplicate`` record that keeps the bogus one under ``previous`` — the audit
  trail is the point, and the old reason is the evidence that this was the tie bug and not
  an operator's manual ruling;
* offers it to :func:`app.ingest.service.assign_conversation` again, because
  :func:`~app.ingest.reconcile._leave_conversation` took it out of its thread and
  decremented that thread's ``call_count``.  Only a row that is *not* in a conversation is
  offered, which is what keeps the count honest on a second run;
* puts a wrongly-``skipped`` transcript back in the queue, at the band the row was ingested
  in — a 2022 backlog recording queues behind the live feed, not in front of it.  A row
  that has a transcript, or that was ``empty``/``done``, is left exactly as it is: those
  statuses are the truth about the bytes and were never the bug's doing.

Committed every ``batch`` rows, for the reason the other two repairs spell out: 19,000 rows
in one transaction would hold every conversation lock the live feed needs from the first
row to the last.  Idempotent — a repaired row no longer satisfies ``duplicate_of = id``, so
a second run finds nothing.

The bug itself is fixed in :func:`app.ingest.reconcile._find` (the primary is checked
against ``exclude`` too) and guarded in
:func:`~app.ingest.reconcile.reconcile_pair` (ruling a row a duplicate of itself is a
logged no-op).  This module exists only for the rows written before that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Call, Talkgroup, Transcript

log = logging.getLogger("app.repair_self_duplicates")

__all__ = ["DEFAULT_BATCH_SIZE", "SelfDupRepairReport", "repair_self_duplicates"]

#: Rows per committed transaction.  Small enough that the conversation locks the reattach
#: takes are never held for long, large enough that 19,000 rows is 38 commits.
DEFAULT_BATCH_SIZE = 500

#: What the ingest path sets on a fresh call before the queue rules on it
#: (:func:`app.ingest.service.store_call`).  ``enqueue_transcription`` moves it to
#: ``queued`` or back to ``skipped`` according to the talkgroup's own policy.
FRESH_TRANSCRIPT_STATUS = "pending"


@dataclass
class SelfDupRepairReport:
    """One repair pass, in the shape the CLI prints."""

    scanned: int = 0
    restored: int = 0
    conversations_reattached: int = 0
    transcriptions_requeued: int = 0
    commits: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "restored": self.restored,
            "conversations_reattached": self.conversations_reattached,
            "transcriptions_requeued": self.transcriptions_requeued,
            "commits": self.commits,
            "dry_run": self.dry_run,
        }


def _has_transcript(session: Session, call_id: int) -> bool:
    return (
        session.scalars(select(Transcript.id).where(Transcript.call_id == call_id).limit(1)).first()
        is not None
    )


def _requeue_transcription(session: Session, call: Call, settings: Settings) -> int | None:
    """Put a wrongly-skipped transcript back in the queue, in the row's own band.

    Returns the job id, or ``None`` when nothing had to be queued — which covers the rows
    that are *not* the bug (``done``, ``empty``, a row that has a transcript, an
    encrypted row with no audio) as well as the ones the talkgroup's own policy declines to
    transcribe once they are offered again.
    """
    if call.transcript_status != "skipped":
        return None
    # ``empty`` and ``done`` are handled by the check above; a row still carrying text under
    # a ``skipped`` status is a re-run nobody asked for.
    if (call.audio_state or "present") != "present" or _has_transcript(session, call.id):
        return None

    from app.ingest.flow import parse_priority, priority_class  # noqa: PLC0415 - cycle
    from app.ingest.service import enqueue_transcription  # noqa: PLC0415 - cycle

    talkgroup = session.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    # The band the row was *ingested* in, not the band the repair runs in: the backlog is
    # what most of these rows are, and re-transcribing 2022 in front of today's traffic is
    # the one way this repair could hurt.  Resolved against the talkgroup exactly as the
    # push path resolves an upload's (:meth:`app.ingest.flow.PriorityChoice.resolve`).
    band = "backlog" if priority_class(call.ingest_priority) in ("backlog", "bulk") else "live"
    priority = parse_priority(band).resolve(talkgroup.priority if talkgroup is not None else None)
    # Back to what a fresh call carries first; ``enqueue_transcription`` then rules on it
    # and moves it to ``queued``, or back to ``skipped`` when the talkgroup does not
    # transcribe or the recording is shorter than the floor.
    call.transcript_status = FRESH_TRANSCRIPT_STATUS
    return enqueue_transcription(session, call, talkgroup, settings, priority=priority)


def repair_self_duplicates(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    batch: int = DEFAULT_BATCH_SIZE,
) -> SelfDupRepairReport:
    """Restore every call whose ``duplicate_of`` points at its own id.

    ``dry_run`` reads and counts and writes nothing — including the counts of what *would*
    be reattached and requeued, which are answered from the row's own state
    (``conversation_id IS NULL``, ``transcript_status = 'skipped'`` with no transcript)
    rather than by doing the work and rolling back.
    """
    from app.ingest.service import assign_conversation  # noqa: PLC0415 - cycle

    report = SelfDupRepairReport(dry_run=dry_run)
    batch = max(1, int(batch))
    gap_seconds = settings.conversation_gap_seconds
    cursor = 0

    while True:
        rows = list(
            session.scalars(
                select(Call)
                .where(Call.duplicate_of == Call.id, Call.id > cursor)
                .order_by(Call.id)
                .limit(batch)
            )
        )
        if not rows:
            break
        # A dry run changes nothing, so the predicate above keeps answering the same rows;
        # the cursor is what walks it.  A real run does not need the cursor to make
        # progress — a repaired row leaves the set — but carrying it costs nothing and keeps
        # the two modes on one code path.
        cursor = rows[-1].id

        for call in rows:
            report.scanned += 1
            if dry_run:
                report.restored += 1
                if call.conversation_id is None and call.talkgroup_id is not None:
                    report.conversations_reattached += 1
                if (
                    call.transcript_status == "skipped"
                    and (call.audio_state or "present") == "present"
                    and not _has_transcript(session, call.id)
                ):
                    report.transcriptions_requeued += 1
                continue

            previous = call.reconcile_reason
            call.duplicate_of = None
            call.reconciled_at = None
            call.reconcile_reason = {
                "rule": "repair:self-duplicate",
                "note": (
                    "duplicate_of pointed at this row's own id; the exact-hash rung "
                    "resolved a copy of it back to it and reconcile_pair tied with itself"
                ),
                "at": datetime.now(UTC).isoformat(),
                "previous": previous,
            }
            report.restored += 1

            # Only a row that is *not* in a thread: ``_leave_conversation`` decremented the
            # count on the way out, and offering a row that already has a conversation_id
            # would increment a second time and inflate the thread the UI shows.
            if (
                call.conversation_id is None
                and call.talkgroup_id is not None
                and assign_conversation(session, call, gap_seconds) is not None
            ):
                report.conversations_reattached += 1

            if _requeue_transcription(session, call, settings) is not None:
                report.transcriptions_requeued += 1

        if not dry_run:
            session.commit()
            report.commits += 1

    log.info(
        "repair-self-duplicates%s: scanned %s, restored %s, %s conversations, %s requeued",
        " (dry-run)" if dry_run else "",
        report.scanned,
        report.restored,
        report.conversations_reattached,
        report.transcriptions_requeued,
    )
    return report
