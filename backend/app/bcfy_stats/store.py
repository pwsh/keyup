"""Writing what the owner API said: one sample, its hourly rollup, and the archive listing.

The rollup is maintained *here*, on the write path, rather than by a nightly job.  Two
reasons: the current hour is then always current, so a chart and the samples never
disagree; and the samples themselves can be aged out (:data:`SAMPLE_KEEP_DAYS`) without
taking the history with them, because the hour rows are the history a months-long chart
reads.

Every write is an upsert.  A poller that is restarted mid-hour, or two workers overlapping
for a tick, must not double-count an hour or duplicate an archive block, and
``ON CONFLICT`` is what makes that true at the database rather than in a lock.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.bcfy_stats import client as bcfy_client
from app.models import BcfyArchive, BcfyFeedDaily, BcfyFeedHourly, BcfyFeedSample

log = logging.getLogger("app.bcfy_stats.store")

__all__ = [
    "IMPORT_SOURCE",
    "PAGE_SOURCE",
    "SAMPLE_KEEP_DAYS",
    "prune_samples",
    "record_import_hours",
    "record_page_hours",
    "record_sample",
    "stored_archive_days",
    "upsert_archives",
    "upsert_daily",
]

#: The value of ``bcfy_feed_hourly.source`` on a row reconstructed from the public feed
#: page rather than polled.  ``NULL`` — a polled row — means "this deployment measured
#: it", which is the truth; this one means "read off somebody's chart for an hour nobody
#: was here for".
PAGE_SOURCE = "page"

#: The value of ``source`` on a row that came from the feed dashboard's CSV export
#: (:mod:`app.bcfy_stats.importer`).  It outranks :data:`PAGE_SOURCE` and is outranked by
#: a poll — see :func:`record_import_hours` for the whole order and why it is that way
#: round.
IMPORT_SOURCE = "import"

#: How long a raw per-poll sample is kept.  400 days is "a year and the month either side
#: of it", so a year-on-year comparison never falls off the end mid-question.  The hourly
#: rollup is not pruned at all: 8 760 rows a year is nothing, and it is the only copy of
#: anything older than this.
SAMPLE_KEEP_DAYS = 400


def _hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def record_sample(
    session: Session,
    *,
    feed_id: str,
    document: Any,
    polled_at: datetime | None = None,
) -> BcfyFeedSample:
    """Store one ``a=feed`` response and fold its listener count into the hour.

    ``document`` is kept whole in ``raw``; :func:`app.bcfy_stats.client.extract` decides
    what the mapping could make of it.  A response with no recognizable listener count is
    still stored — that is a fact about the feed (or about the mapping) and is exactly what
    the probe is for — it simply does not move the rollup.
    """
    moment = polled_at or datetime.now(UTC)
    extracted = bcfy_client.extract(document)
    listeners = extracted.get("listeners")
    listeners = listeners if isinstance(listeners, int) else None
    online = extracted.get("online")
    online = online if isinstance(online, bool) else None

    sample = BcfyFeedSample(
        feed_id=str(feed_id),
        polled_at=moment,
        listeners=listeners,
        online=online,
        raw=document,
        extracted=extracted or None,
    )
    session.add(sample)
    if listeners is not None:
        roll_up(session, feed_id=str(feed_id), hour=_hour(moment), listeners=listeners)
    return sample


def roll_up(session: Session, *, feed_id: str, hour: datetime, listeners: int) -> None:
    """Fold one listener reading into its hour row, creating the row if it is the first.

    The average is maintained incrementally — ``(avg*n + x) / (n+1)`` computed in SQL from
    the row's own columns — so the pass costs one statement and never reads the samples
    back.  ``max`` is a ``GREATEST`` for the same reason.

    **Except against a backfilled row.**  An hour reconstructed from the public feed page
    (:func:`record_page_hours`) or read out of the dashboard export
    (:func:`record_import_hours`) carries ``samples = 0``, and it is somebody else's
    summary standing in for a measurement.  The first real sample to land in such an hour
    therefore **replaces** it outright rather than averaging with it: ``(backfilled + x)
    / 1`` would quietly bake a rounded hourly figure into this deployment's own average for
    ever, and ``GREATEST`` would keep a peak nothing here ever saw.  The ``samples = 0``
    test is what distinguishes the two cases — it is true of a page row and of an import
    row alike, which is why a poll beats both without needing to know which it displaced —
    and ``source``, ``online_checks`` and ``online_reports`` are all cleared, because from
    that moment the row is a polled row and the export's uptime pair describes an hour it
    no longer holds.
    """
    polled_over_backfill = BcfyFeedHourly.samples == 0
    statement = insert(BcfyFeedHourly).values(
        feed_id=feed_id,
        hour=hour,
        listeners_avg=float(listeners),
        listeners_max=listeners,
        samples=1,
        source=None,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[BcfyFeedHourly.feed_id, BcfyFeedHourly.hour],
            set_={
                "listeners_avg": case(
                    (polled_over_backfill, statement.excluded.listeners_avg),
                    else_=(
                        func.coalesce(statement.excluded.listeners_avg, 0.0)
                        + func.coalesce(BcfyFeedHourly.listeners_avg, 0.0) * BcfyFeedHourly.samples
                    )
                    / (BcfyFeedHourly.samples + 1),
                ),
                "listeners_max": case(
                    (polled_over_backfill, statement.excluded.listeners_max),
                    else_=func.greatest(
                        func.coalesce(BcfyFeedHourly.listeners_max, 0), statement.excluded.listeners_max
                    ),
                ),
                "samples": BcfyFeedHourly.samples + 1,
                "source": None,
                # The export's uptime pair belongs to the import row this poll is
                # displacing, not to the polled hour that replaces it.
                "online_checks": case(
                    (polled_over_backfill, None), else_=BcfyFeedHourly.online_checks
                ),
                "online_reports": case(
                    (polled_over_backfill, None), else_=BcfyFeedHourly.online_reports
                ),
            },
        )
    )


def record_page_hours(
    session: Session,
    *,
    feed_id: str,
    bars: list[int],
    now: datetime | None = None,
) -> int:
    """Lay the public page's hourly sparkline into the rollup; returns hours written.

    Bar ``k`` of ``n`` is the hour ``floor(now) - (n-1-k)`` hours: the bars are oldest
    first and the last one is the hour in progress, which is what the page's own "Now"
    figure agrees with.  ``n`` is taken from the list rather than assumed to be 24, because
    the bar count is somebody else's rendering decision (see
    :mod:`app.bcfy_stats.public_page`).

    **The merge rule, and the whole reason this is not just an upsert.**  A page value is a
    number Broadcastify rounded for a tooltip; a poll is this deployment's own measurement.
    So the write is conditional — ``ON CONFLICT … DO UPDATE … WHERE samples = 0`` — and:

    * an hour with **no row** gets one: the bar as both average and max, ``samples = 0``,
      ``source = 'page'``;
    * an hour whose row is itself page-derived (``samples = 0``) is refreshed, because a
      later render of the page is a later look at the same hour;
    * an hour that was **polled** (``samples > 0``) is left exactly alone.  Not averaged,
      not maxed — untouched.  Our own polls are the truth and a scrape never edits them;
    * an hour that came from the **dashboard export** (``source = 'import'``) is left alone
      too.  Both are Broadcastify's own numbers, but the export is the figure their own
      dashboard reports and the sparkline is a bar whose height had to be measured, so the
      export outranks it.  That is the middle rung of the order polled > import > page.

    ``samples`` stays 0 on a page row on purpose: it is the flag the reverse direction
    (:func:`roll_up`) tests, it keeps the page value out of every sample-weighted mean the
    summary computes, and it makes "how many polls is this cell built from" keep meaning
    polls.
    """
    if not bars:
        return 0
    moment = _hour(now or datetime.now(UTC))
    span = len(bars)
    written = 0
    for index, value in enumerate(bars):
        hour = moment - timedelta(hours=span - 1 - index)
        statement = insert(BcfyFeedHourly).values(
            feed_id=str(feed_id),
            hour=hour,
            listeners_avg=float(value),
            listeners_max=int(value),
            samples=0,
            source=PAGE_SOURCE,
        )
        result = session.execute(
            statement.on_conflict_do_update(
                index_elements=[BcfyFeedHourly.feed_id, BcfyFeedHourly.hour],
                set_={
                    "listeners_avg": statement.excluded.listeners_avg,
                    "listeners_max": statement.excluded.listeners_max,
                    "source": statement.excluded.source,
                },
                # The merge rule, enforced by the database rather than by a read-then-write:
                # a polled hour is never updated, and neither is an imported one, whatever
                # else is happening concurrently.  `IS DISTINCT FROM` rather than `!=`
                # because `source` is NULL on the rows this must still be able to touch.
                where=(BcfyFeedHourly.samples == 0)
                & BcfyFeedHourly.source.is_distinct_from(IMPORT_SOURCE),
            ).returning(BcfyFeedHourly.hour)
            # RETURNING rather than `rowcount`, which a conditional ON CONFLICT does not
            # report: a row the WHERE declined returns nothing, so counting what came back
            # *is* counting what was written.
        )
        written += len(result.fetchall())
    return written


def record_import_hours(session: Session, *, feed_id: str, rows: list[dict[str, Any]]) -> int:
    """Lay the dashboard's **hourly export** into the rollup; returns hours written.

    Each row is ``{hour, online_checks, online_reports, listeners_max}`` as
    :func:`app.bcfy_stats.importer.parse_hourly` produces it.

    **The whole precedence order, and why it is this way round.**  Three things can claim
    an hour, and they are not equally good:

    ==========================  ===========================  ==========================
    The hour already has        An **import** row does       A **poll** does
    ==========================  ===========================  ==========================
    nothing                     writes it (``samples = 0``)  writes it as it always did
    a **page** row              **overwrites** it            replaces it whole
    an **import** row           refreshes it                 replaces it whole
    a **polled** row            **nothing at all**           folds in, as before
    ==========================  ===========================  ==========================

    An import overwrites a page row because the two are not the same quality of number: the
    sparkline bar was a height read off an SVG that Broadcastify rounded for a tooltip,
    while the export is the figure their own dashboard reports for that hour.  Where they
    disagree the export is right, so the better number is allowed to replace the worse one
    — which is the one direction the page rule never allowed, because until now there was
    nothing better than the page.

    Re-importing refreshes import rows for the same reason a second page fetch refreshes a
    page row: exporting the same week twice is an operator asking for the current figures,
    not asking for the old ones to be kept.

    And a poll still beats an import, in :func:`roll_up`, exactly as it beats a page row:
    the ``samples = 0`` test there does not care which kind of backfill it is displacing.
    This deployment's own measurement of an hour is the one thing better than
    Broadcastify's summary of it, because it is the only one with the *times within* the
    hour in it.

    ``listeners_avg`` is set to ``listeners_max``.  **The export carries only the hourly
    peak** — there is no average anywhere in it — and the alternative, leaving the average
    NULL, would put a hole in the middle of every grid and series that reads
    ``listeners_avg``, which is most of the page.  So the peak stands in for the average on
    these rows, ``source = 'import'`` says that it is doing so, and ``samples = 0`` keeps
    it out of every sample-weighted mean exactly as it keeps a page value out.
    """
    if not rows:
        return 0
    written = 0
    for row in rows:
        peak = int(row["listeners_max"])
        statement = insert(BcfyFeedHourly).values(
            feed_id=str(feed_id),
            hour=row["hour"],
            # The export has a peak and no average; see the docstring.
            listeners_avg=float(peak),
            listeners_max=peak,
            samples=0,
            source=IMPORT_SOURCE,
            online_checks=row.get("online_checks"),
            online_reports=row.get("online_reports"),
        )
        result = session.execute(
            statement.on_conflict_do_update(
                index_elements=[BcfyFeedHourly.feed_id, BcfyFeedHourly.hour],
                set_={
                    "listeners_avg": statement.excluded.listeners_avg,
                    "listeners_max": statement.excluded.listeners_max,
                    "source": statement.excluded.source,
                    "online_checks": statement.excluded.online_checks,
                    "online_reports": statement.excluded.online_reports,
                },
                # Never a polled hour — the same one-way rule the page fetch obeys, and
                # enforced in the same place, so a poll landing mid-import still wins.
                where=BcfyFeedHourly.samples == 0,
            ).returning(BcfyFeedHourly.hour)
        )
        written += len(result.fetchall())
    return written


def upsert_daily(session: Session, *, feed_id: str, rows: list[dict[str, Any]]) -> int:
    """Write the dashboard's **daily export**; returns rows written.

    Each row is ``{day, uptime_pct, listeners_max}``.  Nothing else writes this table, so
    there is no precedence rule to apply and no measurement to protect: a re-import is
    simply the newer answer and replaces what is there.
    """
    if not rows:
        return 0
    written = 0
    for row in rows:
        statement = insert(BcfyFeedDaily).values(
            feed_id=str(feed_id),
            day=row["day"],
            uptime_pct=row.get("uptime_pct"),
            listeners_max=row.get("listeners_max"),
            source=IMPORT_SOURCE,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[BcfyFeedDaily.feed_id, BcfyFeedDaily.day],
                set_={
                    "uptime_pct": statement.excluded.uptime_pct,
                    "listeners_max": statement.excluded.listeners_max,
                    "source": statement.excluded.source,
                },
            )
        )
        written += 1
    return written


def upsert_archives(session: Session, *, feed_id: str, rows: list[dict[str, Any]]) -> int:
    """Insert or refresh one day's archive blocks; returns how many rows were written.

    The download URL is time-limited on Broadcastify's side, so a re-listed block updates
    rather than being skipped: the newest listing is the one whose link still works.
    """
    written = 0
    for row in rows:
        statement = insert(BcfyArchive).values(
            feed_id=str(feed_id),
            day=row["day"],
            start_at=row["start_at"],
            end_at=row.get("end_at"),
            url=row.get("url"),
            size_bytes=row.get("size_bytes"),
            raw=row.get("raw"),
            synced_at=datetime.now(UTC),
        )
        session.execute(
            statement.on_conflict_do_update(
                constraint="uq_bcfy_archives_feed_start",
                set_={
                    "day": statement.excluded.day,
                    "end_at": statement.excluded.end_at,
                    "url": statement.excluded.url,
                    "size_bytes": statement.excluded.size_bytes,
                    "raw": statement.excluded.raw,
                    "synced_at": statement.excluded.synced_at,
                },
            )
        )
        written += 1
    return written


def stored_archive_days(session: Session, *, feed_id: str) -> set[date]:
    """Every day this feed already has archive rows for.

    One ``DISTINCT`` over the day column, which is what the backfill diffs the API's day
    list against.  A feed with years of archives has a few hundred distinct days, so this is
    a small set and holding it in memory beats a round trip per candidate day.
    """
    rows = session.scalars(
        select(BcfyArchive.day).where(BcfyArchive.feed_id == str(feed_id)).distinct()
    )
    return set(rows)


def prune_samples(session: Session, *, keep_days: int = SAMPLE_KEEP_DAYS) -> int:
    """Delete samples older than ``keep_days``; the hourly rollup keeps the history."""
    if keep_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    result = session.execute(delete(BcfyFeedSample).where(BcfyFeedSample.polled_at < cutoff))
    return int(result.rowcount or 0)


def latest_sample(session: Session, feed_id: str) -> BcfyFeedSample | None:
    """The most recent poll, which is what the status card reads."""
    return session.scalars(
        select(BcfyFeedSample)
        .where(BcfyFeedSample.feed_id == str(feed_id))
        .order_by(BcfyFeedSample.polled_at.desc())
        .limit(1)
    ).first()
