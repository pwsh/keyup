"""Give the rows that pre-date ``calls.ingest_priority`` the class they arrived under.

``ingest_priority`` was added with the push path's admission control
(:mod:`app.ingest.flow`) and is written by every recorder push since.  Everything before
it is NULL — 1.08 million rows — and NULL is not a neutral value here.  It fails
:func:`app.exporters.gate.is_live_band` closed, it matches no ``?priority=`` class, and it
is what an archive migration's rows look like as well as what live traffic looks like, so
the one question the column exists to answer ("is this today's radio, or a decade of
sdrtrunk somebody pushed last Tuesday?") is unanswerable for the bulk of the archive.

Two facts still on the rows are enough to recover it:

``ingest_source = 'trunkd'``
    trunkd is the live recorder.  It pushes each call as it finishes and has never done
    anything else, so every one of its rows is ``live``.
``ingest_source = 'upload'``
    the generic push, used both by trunk-recorder-style clients and by
    ``tools/migrate_sdrtrunk.py``.  These are told apart by *lag*: a call whose
    ``ingested_at`` is more than :data:`BACKLOG_AFTER` past its ``started_at`` was
    uploaded long after it was recorded, which is the definition of backlog.  Under a
    day and it is a live push that spooled through a network blip.

``file`` and ``trunk_recorder`` rows are left NULL on purpose.  Those came off disk, the
archive has no record of when the recorder wrote them relative to now, and inventing a
class for them would make the exporters' "we cannot prove this is live" guard start
publishing things it currently refuses.

**This is a CLI, not a migration.**  An ``UPDATE calls SET …`` over a million rows inside
``alembic upgrade`` takes a lock the live feed waits on for the length of the rewrite, and
a deploy that has to finish it before serving a request is a deploy that can fail halfway
with the API down.  Here the work is keyset-paged by ``id`` and committed per batch, so
the live feed flows between batches, Ctrl-C loses at most one batch, and re-running it
resumes: "outstanding" is "``ingest_priority IS NULL`` and one of the two sources", which
a finished row no longer is.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Call

log = logging.getLogger("app.backfill_priority")

#: Rows repriced per committed transaction.  Small enough that a batch's lock is measured
#: in milliseconds, large enough that a million rows is a few thousand transactions.
DEFAULT_BATCH_SIZE = 5000

#: An ``upload`` row ingested more than this after it was recorded is backlog.  A day is
#: deliberately generous: a live recorder that lost its network overnight and spooled is
#: still live traffic, and calling it backlog would hide it from an exporter for ever.
BACKLOG_AFTER = timedelta(days=1)

#: The two sources whose class can be recovered.  Anything else stays NULL.
SOURCES = ("trunkd", "upload")


def classify(source: str, started_at: Any, ingested_at: Any) -> str | None:
    """The class one row should carry, or ``None`` when it cannot be recovered."""
    if source == "trunkd":
        return "live"
    if source != "upload":
        return None
    if started_at is None or ingested_at is None:  # pragma: no cover - both are NOT NULL
        return "live"
    return "backlog" if (ingested_at - started_at) > BACKLOG_AFTER else "live"


def backfill_ingest_priority(
    session: Session,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    on_batch: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Walk the NULL rows in id order and stamp each one's class.  Returns a report.

    ``limit`` bounds how many rows are *examined*, which is what makes a dry run over a
    million-row table optional rather than mandatory: the scan cannot use
    ``ix_calls_ingest_priority`` (that index is partial on NOT NULL), so a full pass reads
    the table, and a spot check of the first 50,000 rows answers "does this look right?"
    for the price of a page.
    """
    scanned = 0
    counts = {"live": 0, "backlog": 0}
    skipped = 0
    last_id = 0
    batch_number = 0

    while True:
        take = batch_size if limit <= 0 else min(batch_size, limit - scanned)
        if take <= 0:
            break
        rows = session.execute(
            select(Call.id, Call.ingest_source, Call.started_at, Call.ingested_at)
            .where(
                Call.ingest_priority.is_(None),
                Call.ingest_source.in_(SOURCES),
                Call.id > last_id,
            )
            .order_by(Call.id)
            .limit(take)
        ).all()
        if not rows:
            break

        batch_number += 1
        scanned += len(rows)
        last_id = int(rows[-1].id)
        buckets: dict[str, list[int]] = {"live": [], "backlog": []}
        for row in rows:
            name = classify(str(row.ingest_source), row.started_at, row.ingested_at)
            if name is None:  # pragma: no cover - the WHERE clause already excludes these
                skipped += 1
                continue
            buckets[name].append(int(row.id))

        for name, ids in buckets.items():
            counts[name] += len(ids)
            if ids and not dry_run:
                session.execute(
                    update(Call).where(Call.id.in_(ids)).values(ingest_priority=name)
                )

        if dry_run:
            # Nothing was written, but the keyset has moved on; roll back so the read
            # transaction does not grow for the length of a million-row scan.
            session.rollback()
        else:
            session.commit()

        report = {
            "batch": batch_number,
            "scanned": scanned,
            "live": counts["live"],
            "backlog": counts["backlog"],
            "last_id": last_id,
        }
        if on_batch is not None:
            on_batch(report)

    return {
        "dry_run": dry_run,
        "batch_size": batch_size,
        "limit": limit,
        "scanned": scanned,
        "live": counts["live"],
        "backlog": counts["backlog"],
        "skipped": skipped,
        "last_id": last_id,
        "batches": batch_number,
    }


def remaining(session: Session) -> int:
    """How many rows are still outstanding — the "is it done?" read, capped by nothing."""
    from sqlalchemy import func  # noqa: PLC0415 - only this helper needs it

    return int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.ingest_priority.is_(None), Call.ingest_source.in_(SOURCES))
        )
        or 0
    )


__all__ = [
    "BACKLOG_AFTER",
    "DEFAULT_BATCH_SIZE",
    "SOURCES",
    "backfill_ingest_priority",
    "classify",
    "remaining",
]
