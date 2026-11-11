"""Hot times: when people listen, when the radio is busy, and whether the two agree.

Everything here is cut on the **local** clock (``TZ``), because the
question is about the dispatcher's week: "Tuesday 17:00" is a shift change, and the same
instant in UTC is nothing in particular.  The two matrices are therefore 7x24 in local
time — row 0 is Monday, matching ISO weekday order, which is how a week is read here — and
they are built from the *same* buckets so the correlation between them is meaningful.

The listener side reads ``bcfy_feed_hourly`` rather than the samples: it is already one row
per hour, so a 90-day window is 2 160 rows instead of 130 000, and the rollup is the only
copy of anything older than the sample retention anyway.

**The two halves do not cover the same history, and the page must not pretend they do.**
Calls are this deployment's own data and go back to 2024, so a 365-day call heatmap is a
real 365-day call heatmap.  Listeners begin at the first poll, because the owner API
answers with one instant's count and no history at all -- there is nothing to backfill,
ever.  So a listener cell with no samples is ``None``, not ``0``: a year-long window on a
month-old poller is mostly *unmeasured*, and rendering that as zero would say "nobody
listens at 03:00 on a Tuesday" about an hour nobody has ever looked at.
``listeners_hours_covered`` and ``first_sample_at`` are in the summary so the page can say
which window the listener half really covers.

The quiet side is the maintenance question -- "when can the feed go down without anybody
noticing?" -- and it is deliberately the *inverse* of the hot side rather than a second
statistic: ``quietest_hours`` ranks single cells, ``quiet_windows`` ranks contiguous runs
of at least two hours (a restart takes longer than one cell), and both say what they were
computed from, because with no listener history at all the honest answer is "this is the
quietest *radio* traffic, which is not the same claim".

The correlation is Pearson's r over the hours the window covers, pairing each hour's
average listener count with the number of calls in that same hour.  It answers "does
traffic bring listeners?" and nothing stronger: r is a linear association over hourly
totals and says nothing about which way any of it points.  It is ``None`` — not 0 — when
there are fewer than :data:`MIN_CORRELATION_HOURS` paired hours or when either series
never varies, because an r computed from three hours is a number with no information in it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from app.bcfy_stats.store import IMPORT_SOURCE, PAGE_SOURCE
from app.models import BcfyFeedDaily, BcfyFeedHourly, BcfyFeedSample, Call

log = logging.getLogger("app.bcfy_stats.summary")

__all__ = [
    "MAX_WINDOW_HOURS",
    "MIN_CORRELATION_HOURS",
    "MIN_WINDOW_HOURS",
    "QUIET_WINDOWS",
    "PEAK_SOURCES",
    "HourOfWeek",
    "correlation",
    "daily_rows",
    "history_from",
    "quiet_windows",
    "source_hours",
    "summary",
    "window_peak",
]

#: Below this many paired hours the correlation is not reported at all.
MIN_CORRELATION_HOURS = 24

#: The shortest run of hours that counts as a maintenance window.  One hour is not a
#: window: a restart, a re-encode and the wait to see whether the feed came back do not fit
#: in a cell, and a one-hour "window" would only ever be the single quietest hour again.
MIN_WINDOW_HOURS = 2

#: The longest run considered.  Past half a day a "quiet window" has stopped being a window
#: and started being a description of the night.
MAX_WINDOW_HOURS = 12

#: How many maintenance windows are suggested.
QUIET_WINDOWS = 3

#: How many single cells the hottest and quietest rankings each return.
RANK_SIZE = 10

#: Rows and columns of both matrices: Monday..Sunday x 00..23, local time.
DAYS = 7
HOURS = 24


def _empty_matrix() -> list[list[float]]:
    return [[0.0] * HOURS for _ in range(DAYS)]


@dataclass
class HourOfWeek:
    """One cell of the 7x24 grid, in the form the API returns it.

    ``listeners_avg`` is ``None`` for a cell no poll ever fell in -- the distinction between
    "nobody listened" and "nobody was watching" is the whole point of the listener half of
    this page.  ``samples`` is how many polls the cell is built from, which is what turns a
    striking-looking cell into a believable one, or fails to.
    """

    weekday: int
    hour: int
    listeners_avg: float | None
    calls: int
    samples: int = 0


def correlation(pairs: list[tuple[float, float]]) -> float | None:
    """Pearson's r over ``(x, y)``, or ``None`` when it would be meaningless.

    Computed from the sums rather than by building intermediate lists: the input is at most
    a few thousand hours and this keeps the whole thing one pass and allocation-free.  A
    zero variance in either series returns ``None`` — an unchanging series has no linear
    relationship with anything, and 0 would read as "no correlation, measured", which is a
    stronger claim than the data supports.
    """
    n = len(pairs)
    if n < MIN_CORRELATION_HOURS:
        return None
    sum_x = sum_y = sum_xx = sum_yy = sum_xy = 0.0
    for x, y in pairs:
        sum_x += x
        sum_y += y
        sum_xx += x * x
        sum_yy += y * y
        sum_xy += x * y
    cov = n * sum_xy - sum_x * sum_y
    var_x = n * sum_xx - sum_x * sum_x
    var_y = n * sum_yy - sum_y * sum_y
    if var_x <= 0 or var_y <= 0:
        return None
    denominator = math.sqrt(var_x) * math.sqrt(var_y)
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, cov / denominator))


def _calls_clause(settings: Any) -> Any:
    """The talkgroup selection the Broadcastify **feed** carries, read from the exporters.

    This page is about the live feed, so it asks the feed's own selection: the shared
    ``export_broadcastify_talkgroup_mode`` applied to ``talkgroups.export_broadcastify``,
    which is the feed's checkbox since Broadcastify's two products stopped sharing one
    (:data:`app.exporters.gate.FLAG_COLUMNS`).  Reading it here rather than adding a second
    selection means the call-volume heatmap is about the traffic the feed actually carries,
    and stays that way when the operator changes their mind in one place.
    """
    from app.exporters import gate  # noqa: PLC0415 - avoids importing httpx at module load

    try:
        return gate.selection_clause(settings, "broadcastify_feed")
    except Exception:  # noqa: BLE001 - a selection we cannot read must not break the page
        log.warning("bcfy-stats: could not read the Broadcastify talkgroup selection")
        return None


def call_hours(
    session: Session, *, start: datetime, end: datetime, settings: Any
) -> dict[datetime, int]:
    """Calls per UTC hour over the window, restricted to the feed's talkgroups.

    One statement, aggregated in the database: a ``date_trunc`` ``GROUP BY`` over a range
    scan of ``ix_calls_started_at``, returning at most one row per hour -- 8 760 of them for
    a year -- rather than a row per call, and never a query per hour.  That is what lets the
    window be a year: the whole cost is the range scan, and the range scan is an index one.
    """
    bucket = func.date_trunc("hour", Call.started_at)
    query = (
        select(bucket.label("hour"), func.count().label("calls"))
        .where(Call.started_at >= start, Call.started_at < end)
        .group_by(bucket)
    )
    clause = _calls_clause(settings)
    if clause is not None:
        query = query.where(clause)
    return {row.hour: int(row.calls) for row in session.execute(query)}


def listener_hours(
    session: Session, *, feed_id: str, start: datetime, end: datetime
) -> dict[datetime, tuple[float, int, int]]:
    """``hour -> (listeners_avg, listeners_max, samples)`` from the rollup, over the window.

    One row per hour that was actually *polled*.  An hour with no row is an hour nobody
    measured, and it is absent rather than zero, which is what lets every caller here keep
    the two apart.
    """
    rows = session.execute(
        select(
            BcfyFeedHourly.hour,
            BcfyFeedHourly.listeners_avg,
            BcfyFeedHourly.listeners_max,
            BcfyFeedHourly.samples,
        )
        .where(
            BcfyFeedHourly.feed_id == str(feed_id),
            BcfyFeedHourly.hour >= start,
            BcfyFeedHourly.hour < end,
        )
        .order_by(BcfyFeedHourly.hour)
    )
    return {
        row.hour: (
            float(row.listeners_avg or 0.0),
            int(row.listeners_max or 0),
            int(row.samples or 0),
        )
        for row in rows
    }


def page_hours(session: Session, *, feed_id: str, start: datetime, end: datetime) -> int:
    """How many of the window's listener hours came from the **public page**, not a poll.

    Those hours are in every grid and in ``listeners_hours_covered`` — a reconstructed hour
    is far better than a hatched one, and the page is the only backfill that exists — but
    they are somebody else's rounded figure for an hour nobody here was watching.  So the
    count is exposed rather than buried, and the page says *"N of these hours came from the
    public feed page"* instead of implying the poller was running all along.
    """
    return source_hours(session, feed_id=feed_id, start=start, end=end, source=PAGE_SOURCE)


def source_hours(
    session: Session, *, feed_id: str, start: datetime, end: datetime, source: str
) -> int:
    """How many of the window's hourly rows carry a given ``source``.

    One counting query, parameterized on the source, because the page asks the same
    question of ``'page'`` and of ``'import'`` and the two answers mean different things to
    a reader: *N from the public page* is "somebody's sparkline stood in for a poll", *M
    imported* is "the dashboard's own figure stood in for a poll".  Both belong in the
    coverage line, and neither should be allowed to pass as a poll.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(BcfyFeedHourly)
            .where(
                BcfyFeedHourly.feed_id == str(feed_id),
                BcfyFeedHourly.hour >= start,
                BcfyFeedHourly.hour < end,
                BcfyFeedHourly.source == source,
            )
        )
        or 0
    )


def daily_rows(
    session: Session, *, feed_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """The daily export's rows over the window: one per day, oldest first.

    The window's bounds are instants and these rows are dates, so the comparison is made on
    the UTC calendar days the window touches — the export dates its rows in UTC
    (docs/BCFY_STATS.md) and so does this.  A day the export never covered is simply
    absent: the same rule the listener hours follow, for the same reason.  There is nothing
    to average and nothing to weight here, so this is the rows as stored.
    """
    rows = session.execute(
        select(
            BcfyFeedDaily.day,
            BcfyFeedDaily.uptime_pct,
            BcfyFeedDaily.listeners_max,
        )
        .where(
            BcfyFeedDaily.feed_id == str(feed_id),
            BcfyFeedDaily.day >= start.astimezone(UTC).date(),
            BcfyFeedDaily.day <= end.astimezone(UTC).date(),
        )
        .order_by(BcfyFeedDaily.day)
    )
    return [
        {
            "day": row.day,
            "uptime_pct": float(row.uptime_pct) if row.uptime_pct is not None else None,
            "listeners_max": int(row.listeners_max) if row.listeners_max is not None else None,
        }
        for row in rows
    ]


def history_from(session: Session, *, feed_id: str) -> Any:
    """The earliest day this feed has **any** listener figure for, whatever its source.

    ``first_sample_at`` answers a narrower question — where the *hourly* history starts —
    and after an import of the daily export the two are usually months apart: 180 days of
    daily peaks and uptime, then a week of hours, then the polls.  The page says both, so
    that "history from March" and "hourly history from last week" are not confused for one
    another.
    """
    hourly = session.scalar(
        select(func.min(BcfyFeedHourly.hour)).where(BcfyFeedHourly.feed_id == str(feed_id))
    )
    daily = session.scalar(
        select(func.min(BcfyFeedDaily.day)).where(BcfyFeedDaily.feed_id == str(feed_id))
    )
    first_hour = hourly.astimezone(UTC).date() if hourly else None
    candidates = [day for day in (first_hour, daily) if day]
    return min(candidates) if candidates else None


def first_sample_at(session: Session, *, feed_id: str) -> datetime | None:
    """The first hour this feed was ever polled -- where the listener history *starts*.

    Read from the rollup rather than the samples so that it survives sample pruning, and it
    is the hour rather than the poll instant because the rollup is what every listener
    chart on the page reads.
    """
    return session.scalar(
        select(func.min(BcfyFeedHourly.hour)).where(BcfyFeedHourly.feed_id == str(feed_id))
    )


def peak_sample(
    session: Session, *, feed_id: str, start: datetime, end: datetime
) -> tuple[int, datetime] | None:
    """The window's highest single listener reading, and when it was taken.

    Read from the samples rather than the rollup because the *time* is the interesting
    half: ``listeners_max`` on an hour row says 41 people were listening some time in that
    hour, and this says it was at 17:42.
    """
    row = session.execute(
        select(BcfyFeedSample.listeners, BcfyFeedSample.polled_at)
        .where(
            BcfyFeedSample.feed_id == str(feed_id),
            BcfyFeedSample.polled_at >= start,
            BcfyFeedSample.polled_at < end,
            BcfyFeedSample.listeners.is_not(None),
        )
        .order_by(BcfyFeedSample.listeners.desc(), BcfyFeedSample.polled_at.asc())
        .limit(1)
    ).first()
    if row is None or row.listeners is None:
        return None
    return int(row.listeners), row.polled_at


#: The peak's sources, best first.  "Best" means *most precisely timed*, not largest: a
#: sample says 17:42, an hour row says "some time in the 17:00 hour", and a day row says
#: only "that Tuesday".  A tie in the count is therefore broken towards the source that can
#: say when.
PEAK_SOURCES = ("samples", "hourly", "daily")


def window_peak(
    session: Session,
    *,
    feed_id: str,
    start: datetime,
    end: datetime,
    daily: list[dict[str, Any]] | None = None,
) -> tuple[int, datetime, str] | None:
    """The window's highest listener count, when it happened, and which source knew it.

    Before the dashboard exports existed this was :func:`peak_sample` alone, and it was
    right, because the samples were the only listener figures there were.  An import
    changes that: a feed whose polling started last week can now hold the day in April when
    479 people listened — a real incident, and by a wide margin the answer to "what is the
    peak", but one that no sample and no hour row in this database has ever seen.

    So three candidates are compared and the largest wins:

    * **samples** — one poll's reading, and the only one that can say *17:42*;
    * **hourly** — an hour row's ``listeners_max``, which covers imported and page-derived
      hours too, and can say *the 17:00 hour*;
    * **daily** — the daily export's peak for a day, which can say only *that day*, and is
      reported at that day's UTC midnight because pretending to an hour would be a lie.

    ``peak_source`` is returned beside the number precisely so the page can say which of
    those three it got, rather than showing a midnight timestamp that looks like a
    measurement.  Equal counts prefer the sharper source (:data:`PEAK_SOURCES`).
    """
    candidates: list[tuple[int, datetime, str]] = []

    sampled = peak_sample(session, feed_id=feed_id, start=start, end=end)
    if sampled is not None:
        candidates.append((sampled[0], sampled[1], "samples"))

    hourly = session.execute(
        select(BcfyFeedHourly.listeners_max, BcfyFeedHourly.hour)
        .where(
            BcfyFeedHourly.feed_id == str(feed_id),
            BcfyFeedHourly.hour >= start,
            BcfyFeedHourly.hour < end,
            BcfyFeedHourly.listeners_max.is_not(None),
        )
        .order_by(BcfyFeedHourly.listeners_max.desc(), BcfyFeedHourly.hour.asc())
        .limit(1)
    ).first()
    if hourly is not None and hourly.listeners_max is not None:
        candidates.append((int(hourly.listeners_max), hourly.hour, "hourly"))

    rows = daily if daily is not None else daily_rows(session, feed_id=feed_id, start=start, end=end)
    best_day = max(
        (row for row in rows if row.get("listeners_max") is not None),
        key=lambda row: (int(row["listeners_max"]), row["day"]),
        default=None,
    )
    if best_day is not None:
        candidates.append(
            (
                int(best_day["listeners_max"]),
                datetime.combine(best_day["day"], datetime.min.time(), tzinfo=UTC),
                "daily",
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[0], -PEAK_SOURCES.index(row[2])))


def quiet_windows(
    listener_matrix: list[list[float | None]],
    call_matrix: list[list[float]],
    *,
    basis: str,
    count: int = QUIET_WINDOWS,
    min_hours: int = MIN_WINDOW_HOURS,
    max_hours: int = MAX_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    """The quietest contiguous runs of hours -- when the feed can be taken down.

    A *window*, not a cell: maintenance takes longer than an hour, and the quietest single
    hour is worth nothing if the hour on either side of it is the busiest.  Runs may wrap
    midnight (``22:00 -> 02:00`` is one window, not two) but never cross into the next
    weekday: "Sunday night" is a thing an operator schedules, and a run that started on
    Saturday and ended on Monday would be neither.

    ``basis`` chooses what "quiet" means.  With ``"listeners"`` a run only counts when
    *every* hour in it has been polled -- an unmeasured hour is not evidence of quiet, and
    averaging over the hours that happen to have data would make the least-observed windows
    look like the best ones.  With ``"calls"`` (no listener history at all yet) the same
    search runs over radio traffic instead, which is a weaker claim and is labelled as one.

    The results are the ``count`` best *non-overlapping* runs: without that, the top three
    are three descriptions of the same night, which answers the question once and takes up
    three lines doing it.  Equal means prefer the longer run -- more room for the same
    quiet.
    """
    max_hours = min(max_hours, HOURS)
    candidates: list[tuple[float, float, int, int, int, float | None]] = []
    for weekday in range(min(DAYS, len(listener_matrix), len(call_matrix))):
        listener_row = listener_matrix[weekday]
        call_row = call_matrix[weekday]
        for start in range(HOURS):
            for length in range(max(1, min_hours), max_hours + 1):
                hours = [(start + step) % HOURS for step in range(length)]
                heard = [listener_row[hour] for hour in hours if hour < len(listener_row)]
                calls = [float(call_row[hour]) for hour in hours if hour < len(call_row)]
                if len(calls) < length:
                    continue
                calls_avg = sum(calls) / length
                if basis == "listeners":
                    if len(heard) < length or any(value is None for value in heard):
                        continue
                    listeners_avg = sum(float(value or 0.0) for value in heard) / length
                    score = listeners_avg
                else:
                    listeners_avg = None
                    score = calls_avg
                candidates.append((score, calls_avg, -length, weekday, start, listeners_avg))

    # Sorted on the first five fields only: the sixth is the reported average, which is
    # `None` for the calls basis and must never reach a comparison.
    candidates.sort(key=lambda candidate: candidate[:5])
    taken: set[tuple[int, int]] = set()
    out: list[dict[str, Any]] = []
    for _score, calls_avg, negative_length, weekday, start, listeners_avg in candidates:
        if len(out) >= count:
            break
        length = -negative_length
        cells = {(weekday, (start + step) % HOURS) for step in range(length)}
        if cells & taken:
            continue
        taken |= cells
        out.append(
            {
                "weekday": weekday,
                "start_hour": start,
                # Exclusive, and modulo the day: a 22:00 window three hours long ends at
                # 01:00, which is how a person writes it down.
                "end_hour": (start + length) % HOURS,
                "listeners_avg": None if listeners_avg is None else round(listeners_avg, 2),
                "calls_avg": round(calls_avg, 2),
            }
        )
    return out


def _weekday_hour(moment: datetime, tz: str) -> tuple[int, int]:
    """``(Monday=0..Sunday=6, hour)`` for an instant, on the local clock."""
    from zoneinfo import ZoneInfo  # noqa: PLC0415 - one caller

    try:
        local = moment.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001 - an unknown zone must not break the page
        local = moment.astimezone(UTC)
    return local.weekday(), local.hour


def _cell(cell: HourOfWeek) -> dict[str, Any]:
    """One ranked cell as the API returns it."""
    return {
        "weekday": cell.weekday,
        "hour": cell.hour,
        "listeners_avg": cell.listeners_avg,
        "calls": cell.calls,
        "samples": cell.samples,
    }


def summary(
    session: Session,
    *,
    feed_id: str,
    settings: Any,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything ``GET /admin/bcfy-stats/summary`` answers, in one pass over two queries.

    The matrices are averages over the *cells*, not over the window: a Tuesday-17:00 cell in
    a 30-day window is the mean of the four or five Tuesday 17:00s it contains, so a longer
    window makes the picture steadier rather than larger.  That is what makes a 365-day
    window worth offering, and it costs two aggregate queries either way -- 8 760 call-hour
    rows and at most as many rollup rows, both range scans.

    ``listeners_hours_covered`` and ``first_sample_at`` exist so the caller can say which
    part of the window the listener half actually covers: the calls always cover all of it,
    and the listeners cover the time since the first poll, which on a new deployment is a
    small corner of a year.
    """
    end = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=max(1, days))
    tz = str(getattr(settings, "tz", "UTC") or "UTC")

    listeners = listener_hours(session, feed_id=feed_id, start=start, end=end)
    calls = call_hours(session, start=start, end=end, settings=settings)

    listener_totals = _empty_matrix()
    listener_counts = [[0] * HOURS for _ in range(DAYS)]
    listener_polls = [[0] * HOURS for _ in range(DAYS)]
    listener_peaks = [[0] * HOURS for _ in range(DAYS)]
    call_totals = _empty_matrix()
    call_counts = [[0] * HOURS for _ in range(DAYS)]

    for hour, (average, peak, polls) in listeners.items():
        weekday, of_day = _weekday_hour(hour, tz)
        listener_totals[weekday][of_day] += average
        listener_counts[weekday][of_day] += 1
        listener_polls[weekday][of_day] += polls
        listener_peaks[weekday][of_day] = max(listener_peaks[weekday][of_day], peak)
    for hour, count in calls.items():
        weekday, of_day = _weekday_hour(hour, tz)
        call_totals[weekday][of_day] += float(count)
        call_counts[weekday][of_day] += 1

    # `None`, not 0.0, for a cell no poll fell in.  A year-long window on a poller that has
    # been running a month is mostly unmeasured, and a grid of zeros would read as a grid of
    # empty hours -- the opposite of what it knows.
    listener_matrix: list[list[float | None]] = [
        [
            round(listener_totals[d][h] / listener_counts[d][h], 2)
            if listener_counts[d][h]
            else None
            for h in range(HOURS)
        ]
        for d in range(DAYS)
    ]
    listener_max_matrix: list[list[int | None]] = [
        [listener_peaks[d][h] if listener_counts[d][h] else None for h in range(HOURS)]
        for d in range(DAYS)
    ]
    listener_samples_matrix = [[listener_polls[d][h] for h in range(HOURS)] for d in range(DAYS)]
    # Calls stay 0.0 for an empty cell, and that zero is a *measurement*: this deployment
    # holds every call it ever recorded, so "no calls in this hour" is a fact about the
    # radio and not about the recorder.
    call_matrix = [
        [
            round(call_totals[d][h] / call_counts[d][h], 2) if call_counts[d][h] else 0.0
            for h in range(HOURS)
        ]
        for d in range(DAYS)
    ]

    cells = [
        HourOfWeek(
            weekday=d,
            hour=h,
            listeners_avg=listener_matrix[d][h],
            calls=int(round(call_matrix[d][h])),
            samples=listener_polls[d][h],
        )
        for d in range(DAYS)
        for h in range(HOURS)
        if listener_counts[d][h] or call_counts[d][h]
    ]
    # The hottest cells, ranked by listeners and tie-broken by call volume so two equally
    # popular hours are ordered by the one that is actually busier.  An unmeasured cell
    # sorts as 0 for the ranking and still reports `null`: it cannot be the hottest hour on
    # the strength of never having been looked at.
    hottest = sorted(
        cells,
        key=lambda cell: (-(cell.listeners_avg or 0.0), -cell.calls, cell.weekday, cell.hour),
    )

    # …and the quietest, which is the maintenance question.  Only cells that were actually
    # polled can answer it; with none at all the ranking falls back to call volume and says
    # so, because "quietest radio hour" is a different and weaker claim than "quietest
    # listening hour" and the page has to be able to tell the reader which one it got.
    heard = [cell for cell in cells if cell.listeners_avg is not None]
    basis = "listeners" if heard else "calls"
    if heard:
        quietest = sorted(
            heard,
            key=lambda cell: ((cell.listeners_avg or 0.0), cell.calls, cell.weekday, cell.hour),
        )
    else:
        quietest = sorted(cells, key=lambda cell: (cell.calls, cell.weekday, cell.hour))

    paired = [
        (average, float(calls.get(hour, 0)))
        for hour, (average, _peak, _polls) in sorted(listeners.items())
    ]

    latest = session.execute(
        select(BcfyFeedSample.listeners, BcfyFeedSample.online, BcfyFeedSample.polled_at)
        .where(BcfyFeedSample.feed_id == str(feed_id))
        .order_by(BcfyFeedSample.polled_at.desc())
        .limit(1)
    ).first()
    daily = daily_rows(session, feed_id=feed_id, start=start, end=end)
    peak = window_peak(session, feed_id=feed_id, start=start, end=end, daily=daily)

    return {
        "feed_id": str(feed_id),
        "days": max(1, days),
        "from": start,
        "to": end,
        "timezone": tz,
        "current_listeners": int(latest.listeners) if latest and latest.listeners is not None else None,
        "online": latest.online if latest else None,
        "last_poll_at": latest.polled_at if latest else None,
        "peak_listeners": peak[0] if peak else None,
        "peak_at": peak[1] if peak else None,
        "peak_source": peak[2] if peak else None,
        "hours_sampled": len(listeners),
        "listeners_hours_covered": len(listeners),
        "hours_from_page": page_hours(session, feed_id=feed_id, start=start, end=end),
        "hours_from_import": source_hours(
            session, feed_id=feed_id, start=start, end=end, source=IMPORT_SOURCE
        ),
        "daily": daily,
        "daily_days_covered": len(daily),
        "first_sample_at": first_sample_at(session, feed_id=feed_id),
        "history_from": history_from(session, feed_id=feed_id),
        "calls_hours_covered": len(calls),
        "window_hours": int((end - start).total_seconds() // 3600),
        "listeners_hour_of_week": listener_matrix,
        "listeners_max_hour_of_week": listener_max_matrix,
        "listeners_samples_hour_of_week": listener_samples_matrix,
        "calls_hour_of_week": call_matrix,
        "hottest_hours": [_cell(cell) for cell in hottest[:RANK_SIZE]],
        "quietest_hours": [_cell(cell) for cell in quietest[:RANK_SIZE]],
        "quietest_basis": basis,
        "quiet_windows": quiet_windows(listener_matrix, call_matrix, basis=basis),
        "calls_total": sum(calls.values()),
        "correlation": correlation(paired),
        "correlation_hours": len(paired),
    }


def series(
    session: Session,
    *,
    feed_id: str,
    start: datetime,
    end: datetime,
    step: str = "hour",
) -> list[dict[str, Any]]:
    """The listeners chart: one point per hour or per day over the window.

    ``hour`` reads the rollup directly.  ``day`` aggregates the rollup rather than the
    samples — a day's average is the mean of its hours weighted by how many samples each
    hour held, which is the same number the samples would have given and costs 24 rows
    instead of 1 440.
    """
    if step == "day":
        bucket = func.date_trunc("day", BcfyFeedHourly.hour)
        rows = session.execute(
            select(
                bucket.label("bucket"),
                (
                    func.sum(BcfyFeedHourly.listeners_avg * BcfyFeedHourly.samples)
                    / func.nullif(func.sum(BcfyFeedHourly.samples), 0)
                ).label("avg"),
                func.max(BcfyFeedHourly.listeners_max).label("max"),
                func.sum(BcfyFeedHourly.samples).cast(Integer).label("samples"),
            )
            .where(
                BcfyFeedHourly.feed_id == str(feed_id),
                BcfyFeedHourly.hour >= start,
                BcfyFeedHourly.hour < end,
            )
            .group_by(bucket)
            .order_by(bucket)
        )
    else:
        rows = session.execute(
            select(
                BcfyFeedHourly.hour.label("bucket"),
                BcfyFeedHourly.listeners_avg.label("avg"),
                BcfyFeedHourly.listeners_max.label("max"),
                BcfyFeedHourly.samples.label("samples"),
            )
            .where(
                BcfyFeedHourly.feed_id == str(feed_id),
                BcfyFeedHourly.hour >= start,
                BcfyFeedHourly.hour < end,
            )
            .order_by(BcfyFeedHourly.hour)
        )
    return [
        {
            "bucket": row.bucket,
            "listeners_avg": round(float(row.avg), 2) if row.avg is not None else None,
            "listeners_max": int(row.max) if row.max is not None else None,
            "samples": int(row.samples or 0),
        }
        for row in rows
    ]
