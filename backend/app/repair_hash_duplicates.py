"""Free the calls reconciliation ruled duplicates on a hash **from another day**.

The sibling of :mod:`app.repair_self_duplicates`, and the same shape of thing: an operator
command, run by hand, that cleans up after a bug the archive already swallowed.  It reuses
that module's restore helpers verbatim, because the state the two bugs leave behind is
identical — a row hidden behind ``duplicate_of``, out of its conversation, its pending
transcript cancelled to ``skipped``.

What happened
-------------
:func:`app.ingest.reconcile._find`'s exact rung read an identical ``sha256`` as proof of the
same transmission *whatever the time*.  Identical bytes are only proof of the same
recording, and a short recording with no voice in it — a squelch tail, a grant with nothing
on it, 0.14-2 s — encodes to byte-identical MP3 bytes every time.  One hash therefore
spreads across hundreds of unrelated calls, months and talkgroups apart, and the first row
carrying it became the "original" of every one that followed.

A large archive can carry **hundreds** of such rows (mostly group calls, a few unit calls)
whose primary started more than 300 s away; one hash alone can cover over a hundred of them.
A 1.7 s unit call, for example, was ruled a duplicate of a 1.7 s *group* call on another
talkgroup a week earlier, rule ``identical sha256``.  Each one is invisible: every API listing
filters on ``duplicate_of IS NULL``.

The repair
----------
:func:`repair_far_hash_duplicates` walks the rows whose ``reconcile_reason.match.rule`` is
``identical sha256`` and whose primary's ``started_at`` is more than ``max_skew_s`` away,
and restores each exactly as the self-duplicate repair does: ``duplicate_of`` and
``reconciled_at`` cleared, ``reconcile_reason`` replaced with a ``repair:far-hash-duplicate``
record keeping the bogus one under ``previous``, the conversation reattached (only when the
row is not already in one, so the thread's ``call_count`` stays honest), and a
wrongly-``skipped`` transcript requeued in the band the row was ingested in.

Committed every ``batch`` rows, and idempotent: a restored row no longer has a
``duplicate_of``, so it leaves the set.  Note that the *next* batch reconciliation pass will
re-examine every row this frees — with the fixed rung, which now compares the two starts —
and may legitimately rule some of them duplicates again on the interval rung.  That is the
intent: the repair returns the rows to the archive, and reconciliation gets to answer the
question properly.

The bug itself is fixed in :func:`app.ingest.reconcile._find` (an exact-hash hit counts only
within :data:`~app.ingest.reconcile.EXACT_HASH_MAX_SKEW_S` of the caller's start).  This
module exists only for the rows written before that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.config import Settings
from app.models import Call
from app.repair_self_duplicates import (
    DEFAULT_BATCH_SIZE,
    _has_transcript,
    _requeue_transcription,
)

log = logging.getLogger("app.repair_hash_duplicates")

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_SKEW_SECONDS",
    "FarHashRepairReport",
    "repair_far_hash_duplicates",
]

#: How far apart two copies' starts may be and still be one transmission — the same
#: allowance the fixed rung applies (:data:`app.ingest.reconcile.EXACT_HASH_MAX_SKEW_S`),
#: named again here so the repair can be run with a wider or narrower one.
DEFAULT_MAX_SKEW_SECONDS = 60.0

#: the ``reconcile_reason`` ruling this repair looks for
HASH_MATCH_RULE = "identical sha256"


@dataclass
class FarHashRepairReport:
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


def _far_hash_query(cursor: int, max_skew_s: float, batch: int):  # noqa: ANN202 - a Select of Call
    """The rows this repair owns: ruled a duplicate on a hash, primary far away in time.

    A join to the primary rather than a walk of ``duplicate_of`` in Python — the set is
    small but the predicate is the whole definition of the bug, and it belongs in one place.
    ``reconcile_reason`` is JSONB, so the rule is read with the ``->'match'->>'rule'`` path
    the production census used.
    """
    primary = aliased(Call)
    return (
        select(Call)
        .join(primary, Call.duplicate_of == primary.id)
        .where(
            Call.id > cursor,
            Call.duplicate_of.isnot(None),
            Call.reconcile_reason["match"]["rule"].astext == HASH_MATCH_RULE,
            func.abs(func.extract("epoch", Call.started_at - primary.started_at)) > max_skew_s,
        )
        .order_by(Call.id)
        .limit(batch)
    )


def repair_far_hash_duplicates(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool = False,
    batch: int = DEFAULT_BATCH_SIZE,
    max_skew_s: float = DEFAULT_MAX_SKEW_SECONDS,
) -> FarHashRepairReport:
    """Restore every call ruled a duplicate on an identical hash from another time.

    ``dry_run`` reads and counts and writes nothing — including the counts of what *would*
    be reattached and requeued, which are answered from the row's own state
    (``conversation_id IS NULL``, ``transcript_status = 'skipped'`` with no transcript)
    rather than by doing the work and rolling back.
    """
    from app.ingest.service import assign_conversation  # noqa: PLC0415 - cycle

    report = FarHashRepairReport(dry_run=dry_run)
    batch = max(1, int(batch))
    max_skew_s = float(max_skew_s)
    gap_seconds = settings.conversation_gap_seconds
    cursor = 0

    while True:
        rows = list(session.scalars(_far_hash_query(cursor, max_skew_s, batch)))
        if not rows:
            break
        # A dry run changes nothing, so the predicate keeps answering the same rows; the
        # cursor is what walks it.  A real run does not need it — a repaired row leaves the
        # set — but carrying it keeps the two modes on one code path.
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
                "rule": "repair:far-hash-duplicate",
                "note": (
                    "ruled a duplicate on an identical sha256 whose primary started more "
                    f"than {max_skew_s:g} s away; a recording with no voice in it encodes "
                    "to the same bytes every time, so the hash was not evidence of one "
                    "transmission"
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
        "repair-far-hash-duplicates%s: scanned %s, restored %s, %s conversations, %s requeued",
        " (dry-run)" if dry_run else "",
        report.scanned,
        report.restored,
        report.conversations_reattached,
        report.transcriptions_requeued,
    )
    return report
