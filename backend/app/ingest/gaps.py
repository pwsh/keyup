"""Recorder gap detection (docs/ARCHITECTURE.md 3.4, 5.3).

A system that normally carries four thousand calls a day going quiet for half an hour is
almost always the recorder, not the radios.  A gap is the stretch between one call's end
and the next call's start when that stretch is longer than ``GAP_MINUTES`` — which means
gaps only exist *inside* the observed range, never before the first call or after the
last, where "quiet" and "not recording yet" are indistinguishable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import TextClause, delete, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import RecorderGap
from app.runtime_settings import effective_settings

log = logging.getLogger("app.ingest.gaps")

#: How far back the worker's hourly pass looks (:func:`recent_window_start`).  The whole
#: table is 4.6 million primaries and the unbounded pass read all of them every hour — a
#: sequential scan and a 190 MB on-disk sort, 4.5 s for the query alone and 24 s for the
#: whole job (it upserts every one of the ~6,000 stored gaps again), measured on production
#: 2026-09-23 — to re-confirm gaps settled months ago.  Seven days is a range of
#: ``ix_calls_system_started``, ~0.6 s.  The window
#: bounds only the hourly pass: a gap is found once traffic resumes, so a new one is always
#: recent, and what the window cannot see — a hole filled by a backlog upload older than
#: the window, an outage longer than it — is reconciled by the nightly whole-table pass on
#: ``CRON_GAPS`` (the worker's ``run_gaps_full``).
RECENT_WINDOW = timedelta(days=7)


def recent_window_start(now: datetime | None = None) -> datetime:
    """``since`` for the hourly pass."""
    return (now or datetime.now(UTC)) - RECENT_WINDOW


def _gaps_sql(*, system_id: int | None, since: datetime | None) -> TextClause:
    """One window pass over ``calls``: LAG gives the previous call's end per system.

    The optional bounds are written into the statement only when given, rather than as
    ``(:since IS NULL OR started_at >= :since)``: psycopg prepares a statement after five
    executions, and a generic plan cannot use that disjunction as an index condition —
    the long-lived worker would be back to scanning the whole table within a few hours.
    With ``since`` the scan is a range of ``ix_calls_system_started``.
    """
    bounds = ""
    if system_id is not None:
        bounds += " AND system_id = :system_id"
    if since is not None:
        bounds += " AND started_at >= :since"
    return text(
        f"""
        WITH ordered AS (
            SELECT id,
                   system_id,
                   started_at,
                   ended_at,
                   LAG(ended_at) OVER (PARTITION BY system_id ORDER BY started_at, id) AS prev_end
            FROM calls
            WHERE duplicate_of IS NULL{bounds}
        )
        SELECT system_id, prev_end AS gap_start, started_at AS gap_end, id AS resumed_call_id
        FROM ordered
        WHERE prev_end IS NOT NULL
          AND started_at - prev_end > make_interval(secs => CAST(:gap_seconds AS double precision))
        ORDER BY system_id, prev_end
        """  # noqa: S608 - the interpolated text is two fixed clauses; the values are binds
    )


@dataclass
class GapSummary:
    scanned_systems: int = 0
    gaps_found: int = 0
    gaps_new: int = 0
    gaps_removed: int = 0
    longest_s: float = 0.0


def _prune(
    session: Session,
    *,
    keys: list[tuple[int, datetime]],
    ends: list[tuple[int, datetime]],
    system_id: int | None,
    since: datetime | None,
) -> int:
    """Delete stored gaps this pass no longer detects.  Returns how many went.

    Two deletions, and the difference between them is the whole of the scoping rule:

    **Stale rows inside the scanned window.**  A gap is a hole, and a hole that has since
    been filled — the backlog migration landing the calls that were missing — is no longer
    a gap at all, but the old row keyed on the *old* ``started_at`` would sit there for
    ever.  So anything the fresh detection did not produce goes, restricted to exactly what
    this pass could have re-detected: the same ``system_id`` filter, and with ``since``
    given only rows starting at or after it.  A gap that began before the window could not
    have been re-detected in it, and deleting it would be deleting evidence rather than
    stale evidence.

    **Near-duplicates of a gap that *was* detected.**  A gap is keyed by the call that
    *resumed* traffic, so ``(system_id, ended_at)`` names it uniquely — two gaps on one
    system cannot share a resuming call.  When the previous call's ``ended_at`` is
    re-stamped by reconciliation the gap's ``started_at`` moves by a fraction of a second
    and the upsert writes a second row beside the first.  Any stored row sharing an
    ``ended_at`` with a fresh gap but not its ``started_at`` is that ghost, and it is
    deleted whatever ``since`` says: this pass just established, positively, where that gap
    begins.
    """
    removed = 0
    scope = [RecorderGap.system_id == system_id] if system_id is not None else []
    fresh_key = tuple_(RecorderGap.system_id, RecorderGap.started_at)

    if keys:
        dupes = delete(RecorderGap).where(
            *scope,
            tuple_(RecorderGap.system_id, RecorderGap.ended_at).in_(ends),
            fresh_key.not_in(keys),
        )
        removed += int(session.execute(dupes).rowcount or 0)

    stale = delete(RecorderGap).where(*scope)
    if since is not None:
        stale = stale.where(RecorderGap.started_at >= since)
    if keys:
        stale = stale.where(fresh_key.not_in(keys))
    return removed + int(session.execute(stale).rowcount or 0)


def detect_gaps(
    session: Session,
    *,
    settings: Settings | None = None,
    system_id: int | None = None,
    since: datetime | None = None,
    gap_minutes: int | None = None,
) -> GapSummary:
    """Find and store recorder gaps.

    Idempotent — a known gap is refreshed, not doubled — and *authoritative* over the scope
    it scanned: after the fresh set is upserted, stored gaps this pass no longer produces
    are deleted (see :func:`_prune`).  The worker's hourly pass calls this with ``since``
    at :func:`recent_window_start`; its nightly pass, and ``keyup gaps detect`` without
    ``--days``, with neither ``system_id`` nor ``since``, which reconciles the whole table.
    """
    settings = effective_settings(session, base=settings)
    minutes = gap_minutes if gap_minutes is not None else settings.gap_minutes
    params: dict[str, object] = {"gap_seconds": minutes * 60}
    if system_id is not None:
        params["system_id"] = system_id
    if since is not None:
        params["since"] = since
    rows = session.execute(_gaps_sql(system_id=system_id, since=since), params).all()

    summary = GapSummary(gaps_found=len(rows))
    systems: set[int] = set()
    now = datetime.now(UTC)

    for row in rows:
        systems.add(row.system_id)
        duration = Decimal(str(round((row.gap_end - row.gap_start).total_seconds(), 3)))
        summary.longest_s = max(summary.longest_s, float(duration))
        stmt = (
            pg_insert(RecorderGap)
            .values(
                system_id=row.system_id,
                started_at=row.gap_start,
                ended_at=row.gap_end,
                duration_s=duration,
                resumed_call_id=row.resumed_call_id,
                detected_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_recorder_gaps_system_started",
                set_={
                    "ended_at": row.gap_end,
                    "duration_s": duration,
                    "resumed_call_id": row.resumed_call_id,
                    "detected_at": now,
                },
            )
            .returning(text("(xmax = 0)"))
        )
        if session.execute(stmt).scalar_one():
            summary.gaps_new += 1

    summary.scanned_systems = len(systems)
    summary.gaps_removed = _prune(
        session,
        keys=[(row.system_id, row.gap_start) for row in rows],
        ends=[(row.system_id, row.gap_end) for row in rows],
        system_id=system_id,
        since=since,
    )
    log.info(
        "gap detection: %d gaps (%d new, %d removed) over %d systems",
        summary.gaps_found,
        summary.gaps_new,
        summary.gaps_removed,
        len(systems),
    )
    return summary


__all__ = ["RECENT_WINDOW", "GapSummary", "detect_gaps", "recent_window_start"]
