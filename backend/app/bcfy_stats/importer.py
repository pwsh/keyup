"""Reading the two CSVs the Broadcastify feed-owner dashboard offers.

**This is the only backfill that reaches further than a day.**  The owner API answers with
one instant's listener count and no history at all; the public feed page's sparkline
reaches 24 hours (docs/BCFY_STATS.md).  The dashboard — Broadcastify -> Manage -> the feed
-> Statistics — offers two *exports*, and between them they carry a week of hours and half
a year of days.  Importing them is what turns a listener grid that begins at this
deployment's first poll into one that begins months earlier.

Two formats, told apart by their header and nothing else:

``Hour,Online Checks,Online Reports,Max Listeners``
    One row per hour, newest first, about seven days of them.  ``Online Checks`` is how
    many times Broadcastify looked at the feed in that hour (roughly 15, one every four
    minutes) and ``Online Reports`` is how many of those looks found it up; the pair is the
    hour's uptime and is kept for that reason.  ``Max Listeners`` is the hour's **peak**,
    and it is the only listener figure the export carries — there is no average in it.

``Day,Uptime %,Max Listeners``
    One row per day, newest first, about 180 of them.  ``Day`` is a UTC date rendered as a
    midnight timestamp; ``Uptime %`` is the day's percentage.

**The timestamps are UTC.**  Nothing in the export says so, and the dashboard renders every
other time on the operator's local clock, so this was checked rather than assumed: on the
day the fixture was taken the ``2026-09-04 04:00:00`` row reports 10 of 15 online checks,
and this feed was down from 03:57 to 04:19 **UTC** that morning — 22 minutes, which is five
or six four-minute checks, which is the five checks the row is missing.  On local time the
same row would have to describe an hour in which nothing happened.  Every timestamp here is
therefore attached to UTC directly, which is also what ``bcfy_feed_hourly.hour`` holds, so
no zone conversion happens anywhere in this module.

**Tolerance, and what it is for.**  This is somebody else's export, downloaded through a
browser, possibly opened in a spreadsheet and saved again on the way here.  So: a UTF-8 BOM
is stripped, CRLF and LF both end a line, blank lines are ignored, columns are matched by
*name* rather than by position (an extra column Broadcastify adds later changes nothing),
and either row order is accepted because the parser sorts what it keeps.  A row that cannot
be read is **counted with its reason and skipped** — never guessed at, and never fatal.  A
file of 180 rows with three bad ones is 177 rows of history and a report saying so, which is
strictly better than a 422.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger("app.bcfy_stats.importer")

__all__ = [
    "DAILY_COLUMNS",
    "HOURLY_COLUMNS",
    "IMPORT_SOURCE",
    "ImportFormatError",
    "Parsed",
    "detect_format",
    "import_csv",
    "parse_daily",
    "parse_hourly",
]

#: ``bcfy_feed_hourly.source`` / ``bcfy_feed_daily.source`` on a row that came from one of
#: these exports.  It sits between ``'page'`` (a guess read off a public chart) and ``NULL``
#: (this deployment's own polls) in the precedence order — see :mod:`app.bcfy_stats.store`.
IMPORT_SOURCE = "import"

#: The hourly export's columns, normalized.  Order is irrelevant and extra columns are
#: ignored; these are the four that have to be present.
HOURLY_COLUMNS = ("hour", "onlinechecks", "onlinereports", "maxlisteners")

#: The daily export's columns, normalized.  ``Uptime %`` loses its percent sign to
#: :func:`_normalize`, which is why the name here is bare.
DAILY_COLUMNS = ("day", "uptime", "maxlisteners")

#: A day's uptime outside this range is not a rounding artefact, it is a misread column.
MAX_UPTIME_PCT = 100.0


class ImportFormatError(ValueError):
    """The file is not one of the two exports — raised before anything is read or written."""


def _normalize(name: str) -> str:
    """``"Uptime %"`` -> ``"uptime"``: lowercase, letters and digits only.

    The same normalization the owner client's field mapping uses, and for the same reason:
    only the letters of a column name are load-bearing, so a header that gains a space, a
    unit or a change of case is still the same column.
    """
    return "".join(character for character in name.lower() if character.isalnum())


@dataclass
class Parsed:
    """What one export turned into: the rows, and an account of what was dropped.

    ``skipped_reasons`` is a count per reason rather than a list per row: a file whose every
    row is malformed produces one line saying so instead of 180, and the reason is what
    tells an operator whether they exported the wrong thing or hit a real bad row.
    """

    format: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)


def _rows_of(text: str) -> tuple[list[str], list[list[str]]]:
    """``(normalized header, the rest of the rows)``.

    The BOM is stripped from the *string* — the caller may already have decoded — so one
    that survived the decode is not read as part of the first column's name.
    ``io.StringIO(newline="")`` leaves CRLF, and a quoted field containing a newline, to
    :mod:`csv`, which is the module that already knows about both.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    header: list[str] = []
    body: list[list[str]] = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        if not header:
            header = [_normalize(cell) for cell in row]
            continue
        body.append(row)
    return header, body


def detect_format(text: str) -> str:
    """``"hourly"`` or ``"daily"``, decided by the header alone.

    By the header rather than by the shape of the first data row: the two exports both start
    with a timestamp and both end with a listener count, so a row-shape sniff would read a
    daily file as an hourly one that had lost a column, and write 180 days of listener peaks
    into the wrong table — silently, and hard to undo.  A header that is neither raises.
    """
    header, _body = _rows_of(text)
    columns = set(header)
    if set(HOURLY_COLUMNS) <= columns:
        return "hourly"
    if set(DAILY_COLUMNS) <= columns:
        return "daily"
    raise ImportFormatError(
        "not a Broadcastify dashboard export: expected a header of "
        "'Hour,Online Checks,Online Reports,Max Listeners' or 'Day,Uptime %,Max Listeners', "
        f"got {', '.join(header) or '(nothing)'}"
    )


def _cell(row: list[str], index: dict[str, int], column: str) -> str:
    position = index[column]
    return row[position].strip() if position < len(row) else ""


def _moment(value: str) -> datetime:
    """One export timestamp as an aware UTC instant.

    ``2026-09-04 16:00:00`` is the observed form; ISO-8601 with a ``T`` is accepted too,
    because a spreadsheet round trip produces one.  A value that already carries a zone is
    converted rather than overridden — if Broadcastify ever starts writing an offset, that
    offset is the truth and this module's UTC finding stops applying to that file.
    """
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _count(value: str) -> int:
    """A non-negative integer, tolerating the thousands separator a locale may add."""
    number = int(float(value.replace(",", "")))
    if number < 0:
        raise ValueError("negative count")
    return number


def _hourly_row(raw: list[str], index: dict[str, int]) -> tuple[Any, dict[str, Any]]:
    hour = _moment(_cell(raw, index, "hour")).replace(minute=0, second=0, microsecond=0)
    return hour, {
        "hour": hour,
        "online_checks": _count(_cell(raw, index, "onlinechecks")),
        "online_reports": _count(_cell(raw, index, "onlinereports")),
        "listeners_max": _count(_cell(raw, index, "maxlisteners")),
    }


def _daily_row(raw: list[str], index: dict[str, int]) -> tuple[Any, dict[str, Any]]:
    day = _moment(_cell(raw, index, "day")).date()
    uptime = float(_cell(raw, index, "uptime").replace("%", "").replace(",", ""))
    if not 0.0 <= uptime <= MAX_UPTIME_PCT:
        raise ValueError("uptime outside 0-100")
    return day, {
        "day": day,
        "uptime_pct": uptime,
        "listeners_max": _count(_cell(raw, index, "maxlisteners")),
    }


def _reason(exc: ValueError) -> str:
    """One reason per *kind* of failure, not per row.

    Three unreadable numbers are one line in the report rather than three, and the line
    still says which half of the row went wrong — which is the difference between "you
    exported the wrong thing" and "row 84 is odd".
    """
    message = str(exc)
    if "fromisoformat" in message or "isoformat" in message:
        return "not a date"
    if "uptime outside" in message:
        return "uptime outside 0-100"
    if "negative" in message:
        return "negative count"
    return "not a number"


def _parse(text: str, *, want: str) -> Parsed:
    """The shared body of :func:`parse_hourly` and :func:`parse_daily`.

    The row order in the file is not trusted at all — both exports are newest first today,
    and this sorts ascending on the way out — so a file somebody re-sorted in a spreadsheet
    imports identically.  A key seen twice keeps its **first** row and counts the rest as
    duplicates, rather than letting the later one win: two rows for one hour is a fault in
    the file, and picking either silently would hide it.
    """
    found = detect_format(text)
    if found != want:
        raise ImportFormatError(f"this is the {found} export, not the {want} one")
    header, body = _rows_of(text)
    index = {name: position for position, name in enumerate(header)}
    columns = HOURLY_COLUMNS if want == "hourly" else DAILY_COLUMNS
    build = _hourly_row if want == "hourly" else _daily_row

    rows: dict[Any, dict[str, Any]] = {}
    reasons: Counter[str] = Counter()
    for raw in body:
        if any(_cell(raw, index, column) == "" for column in columns):
            reasons["missing a value"] += 1
            continue
        try:
            key, row = build(raw, index)
        except ValueError as exc:
            reasons[_reason(exc)] += 1
            continue
        if key in rows:
            reasons["duplicate"] += 1
            continue
        rows[key] = row

    return Parsed(
        format=want,
        rows=[rows[key] for key in sorted(rows)],
        skipped=sum(reasons.values()),
        skipped_reasons=dict(reasons),
    )


def parse_hourly(text: str) -> Parsed:
    """``Hour,Online Checks,Online Reports,Max Listeners`` -> rows, oldest first.

    Each row is ``{hour, online_checks, online_reports, listeners_max}`` with ``hour`` an
    aware UTC instant truncated to the hour — the same key ``bcfy_feed_hourly`` uses.
    """
    return _parse(text, want="hourly")


def parse_daily(text: str) -> Parsed:
    """``Day,Uptime %,Max Listeners`` -> rows, oldest first.

    Each row is ``{day, uptime_pct, listeners_max}`` with ``day`` a plain UTC
    :class:`datetime.date`.
    """
    return _parse(text, want="daily")


def import_csv(session: Session, feed_id: str, text: str) -> dict[str, Any]:
    """Parse one export and write it; returns the report the route and the CLI print.

    The two formats go to two different tables and are **not** derived from each other.
    Hourly rows land in ``bcfy_feed_hourly`` beside the polled and page-derived hours, under
    the precedence rule in :mod:`app.bcfy_stats.store`; daily rows land in
    ``bcfy_feed_daily``, which nothing else writes.  Rolling the hourly export up into daily
    rows would produce a *different* number from the daily export for the same day — a week
    of overlap in which two sources disagree — and rolling the daily export down into hours
    would invent 24 identical peaks a day.  The summary puts the two side by side at read
    time instead, which is the only place they need to meet.

    ``rows_written`` is smaller than ``rows_parsed`` exactly to the extent this deployment
    had already polled those hours: an import never overwrites a poll.
    """
    from app.bcfy_stats import store  # noqa: PLC0415 - importer <-> store would be circular

    fmt = detect_format(text)
    parsed = parse_hourly(text) if fmt == "hourly" else parse_daily(text)
    key = "hour" if fmt == "hourly" else "day"
    if fmt == "hourly":
        written = store.record_import_hours(session, feed_id=str(feed_id), rows=parsed.rows)
    else:
        written = store.upsert_daily(session, feed_id=str(feed_id), rows=parsed.rows)
    log.info(
        "bcfy-stats: imported the %s export for feed %s: %d of %d rows written",
        fmt,
        feed_id,
        written,
        len(parsed.rows),
    )
    return {
        "format": fmt,
        "rows_parsed": len(parsed.rows),
        "rows_written": written,
        "rows_skipped": parsed.skipped,
        "first": parsed.rows[0][key] if parsed.rows else None,
        "last": parsed.rows[-1][key] if parsed.rows else None,
        "skipped_reasons": parsed.skipped_reasons,
    }
