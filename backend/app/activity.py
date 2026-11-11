"""The operational activity log (Admin → Activity, ``keyup activity``).

A thin write/read layer over :class:`~app.models.ActivityEvent`.  Workers call
:func:`record` right where they already ``notify`` the live bus, in the same session and
transaction, so an activity row and the work it records commit together.  The recording is
**defensive**: an activity-log failure is logged and swallowed, never raised into the
caller — the archive must keep ingesting and transcribing even if this table is missing or
full.

The read side (:func:`recent`) is a newest-first, keyset-paginated query the API and CLI
share; :func:`prune` keeps the table bounded and is wired into the nightly retention pass
(:meth:`app.worker.Runner.run_retention`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import notify
from app.models import ActivityEvent

log = logging.getLogger("app.activity")

#: The stages an event can come from; validated softly (a bad value is still recorded).
#: ``arbiter`` is the standby promotion job (:mod:`app.arbiter`): it is its own source
#: rather than folded into ``system`` because "why did my spare recorder come on at 03:12"
#: is a question an operator asks by filtering the log, and the answer must be one click.
#: ``stt`` is the same argument for the transcription engine's own misbehavior — a
#: discarded hallucination is not the *work* ``transcribe`` reports, it is the engine
#: failing, and an operator tuning the prompt wants only those rows.  ``site`` is what the
#: air said about itself rather than what the pipeline did with it — a control channel
#: transmitting a message kind it never has before (``site.unknown_new``) is not an ingest
#: event, and filtering the log to it is how that question is asked.
SOURCES = (
    "ingest",
    "upload",
    "transcribe",
    "stt",
    "export",
    "rr",
    "import",
    "system",
    "arbiter",
    "site",
)
#: Severity levels, least to most.
LEVELS = ("info", "warn", "error")

#: The live-bus event name.  The payload carries the row id only; a consumer re-reads it.
BUS_EVENT = "activity"


def record(
    session: Session,
    *,
    source: str,
    level: str,
    kind: str,
    message: str,
    call_id: int | None = None,
    meta: dict[str, Any] | None = None,
    notify_bus: bool = True,
) -> ActivityEvent | None:
    """Insert one activity row, flush it for its id, and announce it on the live bus.

    Runs in the caller's session and transaction — it does **not** commit — so the row
    lands atomically with the work that produced it.  Never raises: any failure is logged
    and ``None`` is returned, because losing a log line must not fail ingest or
    transcription.

    The work runs in a SAVEPOINT: swallowing a failed statement is not enough on
    PostgreSQL, which aborts the caller's whole transaction — and the caller's COMMIT then
    rolls everything back without raising.  Only the log line may be lost.
    """
    try:
        with session.begin_nested():
            row = ActivityEvent(
                source=source,
                level=level,
                kind=kind,
                message=message[:2000],
                call_id=call_id,
                meta=meta or None,
            )
            session.add(row)
            session.flush()  # assign row.id without ending the caller's transaction
            if notify_bus:
                notify(session, BUS_EVENT, {"id": row.id})
        return row
    except Exception:  # noqa: BLE001 - a log line is never worth a failed ingest/transcription
        log.warning("activity: could not record %s/%s %r", source, kind, message, exc_info=True)
        return None


def recent(
    session: Session,
    *,
    limit: int,
    before_id: int | None = None,
    source: str | None = None,
    level: str | None = None,
    since: datetime | None = None,
) -> list[ActivityEvent]:
    """The newest ``limit`` activity rows matching the filters, newest first.

    Pagination is keyset by id: pass the smallest id you have seen as ``before_id`` to get
    the page before it.  ``since`` bounds by wall-clock (``at``); ``source``/``level`` are
    exact-match filters.
    """
    stmt = select(ActivityEvent)
    if before_id is not None:
        stmt = stmt.where(ActivityEvent.id < before_id)
    if source is not None:
        stmt = stmt.where(ActivityEvent.source == source)
    if level is not None:
        stmt = stmt.where(ActivityEvent.level == level)
    if since is not None:
        stmt = stmt.where(ActivityEvent.at >= since)
    stmt = stmt.order_by(ActivityEvent.id.desc()).limit(max(1, limit))
    return list(session.scalars(stmt))


def get(session: Session, event_id: int) -> ActivityEvent | None:
    """One activity row by id, or ``None`` — used by the SSE stream to re-read a bus event."""
    return session.get(ActivityEvent, event_id)


def prune(session: Session, *, keep_days: int) -> int:
    """Delete activity rows older than ``keep_days``; return how many went.

    ``keep_days <= 0`` means "keep for ever" and deletes nothing.  Commits its own work.
    """
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    result = session.execute(delete(ActivityEvent).where(ActivityEvent.at < cutoff))
    session.commit()
    return int(result.rowcount or 0)


def to_dict(row: ActivityEvent) -> dict[str, Any]:
    """The wire/JSON shape the API and CLI both emit."""
    return {
        "id": row.id,
        "at": row.at.isoformat() if row.at is not None else None,
        "source": row.source,
        "level": row.level,
        "kind": row.kind,
        "message": row.message,
        "call_id": row.call_id,
        "meta": row.meta,
    }


__all__ = ["BUS_EVENT", "LEVELS", "SOURCES", "get", "prune", "recent", "record", "to_dict"]
