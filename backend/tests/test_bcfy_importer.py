"""The two dashboard CSV exports: parsing them, and where their rows rank against a poll.

**Both fixtures are invented samples of the two dashboard downloads.**
``owner_hourly_sample.csv`` and ``owner_uptime_sample.csv`` are written by
``fixtures/gen_fixtures.py`` in the exact shape the feed-owner dashboard exports, and they
are asserted as such: the row counts, the first and last timestamps, the two 2026-09-04
rows that establish the UTC finding, and the three incident days whose listener counts are
so far above the rest that a clamp or a "sanity" filter anywhere in the parser would eat
them.  Everything malformed is built in the test from those files, so a tolerance case says
exactly which one thing it changed.

The precedence tests are the other half, and they are about a rule with four states — no
row, a page row, an import row, a polled row — crossed with three writers.  Each cell is
asserted separately rather than through one long scenario, because the failure mode this
protects against is one direction of one cell quietly reversing.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.bcfy_stats import store
from app.bcfy_stats.importer import (
    ImportFormatError,
    detect_format,
    import_csv,
    parse_daily,
    parse_hourly,
)
from app.models import BcfyFeedDaily, BcfyFeedHourly
from tests.conftest import requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bcfy"

HOURLY = (FIXTURES / "owner_hourly_sample.csv").read_text()
DAILY = (FIXTURES / "owner_uptime_sample.csv").read_text()

FEED = "2211"


def hour(text: str) -> datetime:
    return datetime.fromisoformat(f"{text}+00:00")


# --------------------------------------------------------------------------------------
# the two exports
# --------------------------------------------------------------------------------------


def test_the_two_exports_are_told_apart_by_their_headers_alone() -> None:
    assert detect_format(HOURLY) == "hourly"
    assert detect_format(DAILY) == "daily"


def test_the_hourly_export_parses_whole_and_oldest_first() -> None:
    parsed = parse_hourly(HOURLY)
    assert parsed.format == "hourly"
    # 180 lines, one of them the header.
    assert len(parsed.rows) == 179
    assert parsed.skipped == 0
    # The file is newest first; the parser hands back oldest first, always.
    assert parsed.rows[0]["hour"] == hour("2026-08-28 06:00:00")
    assert parsed.rows[-1]["hour"] == hour("2026-09-04 16:00:00")
    assert [row["hour"] for row in parsed.rows] == sorted(row["hour"] for row in parsed.rows)
    assert parsed.rows[-1] == {
        "hour": hour("2026-09-04 16:00:00"),
        "online_checks": 12,
        "online_reports": 12,
        "listeners_max": 7,
    }


def test_the_hourly_timestamps_are_utc_and_the_outage_row_is_why() -> None:
    """The one row that settles the zone question — see :mod:`app.bcfy_stats.importer`.

    The feed was down 03:57–04:19 **UTC** on 2026-09-04: 22 minutes, five or six of the
    four-minute checks.  The export puts the whole of that in two adjacent rows and nowhere
    else — ``04:00`` is short by five of its fifteen reports, and ``03:00`` by exactly one,
    which is the single check that fell in the last three minutes of that hour.  Every other
    hour of the day saw every check it made.  On any other zone these two rows would have to
    describe hours in which nothing happened, so the parser attaching UTC is not a default;
    it is the observation.
    """
    by_hour = {row["hour"]: row for row in parse_hourly(HOURLY).rows}
    outage = by_hour[hour("2026-09-04 04:00:00")]
    assert (outage["online_checks"], outage["online_reports"]) == (15, 10)

    edge = by_hour[hour("2026-09-04 03:00:00")]
    assert edge["online_checks"] - edge["online_reports"] == 1

    # The hours on either side of the outage saw every check they made, which is what makes
    # the two rows above a measurement of an outage rather than of a flaky checker.
    for quiet in ("2026-09-04 02:00:00", "2026-09-04 05:00:00", "2026-09-04 06:00:00"):
        row = by_hour[hour(quiet)]
        assert row["online_checks"] == row["online_reports"]


def test_the_daily_export_parses_whole_and_reaches_back_six_months() -> None:
    parsed = parse_daily(DAILY)
    assert parsed.format == "daily"
    assert len(parsed.rows) == 179
    assert parsed.skipped == 0
    assert parsed.rows[0]["day"] == date(2026, 3, 9)
    assert parsed.rows[-1] == {"day": date(2026, 9, 3), "uptime_pct": 99.72, "listeners_max": 31}
    assert [row["day"] for row in parsed.rows] == sorted(row["day"] for row in parsed.rows)


@pytest.mark.parametrize(
    ("day", "listeners"),
    [(date(2026, 5, 17), 412), (date(2026, 6, 3), 171), (date(2026, 7, 11), 118)],
)
def test_the_incident_days_survive_the_parser_intact(day: date, listeners: int) -> None:
    """The three days that are twenty times a normal one, and are not to be smoothed.

    Such days happen: an incident brings listeners, and the whole reason an operator keeps
    this history is to be able to point at those days afterwards.  A clamp, an outlier filter or
    an ``int8`` anywhere on this path would silently make them ordinary, so they are asserted
    by value.
    """
    rows = {row["day"]: row for row in parse_daily(DAILY).rows}
    assert rows[day]["listeners_max"] == listeners
    assert max(row["listeners_max"] for row in parse_daily(DAILY).rows) == 412


# --------------------------------------------------------------------------------------
# tolerance
# --------------------------------------------------------------------------------------


def test_a_bom_and_crlf_and_blank_lines_are_all_absorbed() -> None:
    """What a file that went through a browser and a spreadsheet looks like on arrival."""
    mangled = "﻿" + HOURLY.replace("\n", "\r\n") + "\r\n\r\n"
    assert detect_format(mangled) == "hourly"
    assert parse_hourly(mangled).rows == parse_hourly(HOURLY).rows


def test_either_row_order_gives_the_same_rows() -> None:
    lines = DAILY.strip().split("\n")
    reversed_file = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
    assert parse_daily(reversed_file).rows == parse_daily(DAILY).rows


def test_extra_columns_are_ignored_and_position_is_never_assumed() -> None:
    """Columns are matched by name, so a column Broadcastify adds later changes nothing."""
    lines = HOURLY.strip().split("\n")
    widened = "\n".join(
        [
            f"Something New,{lines[0]},Another" if index == 0 else f"x,{line},y"
            for index, line in enumerate(lines)
        ]
    )
    assert parse_hourly(widened).rows == parse_hourly(HOURLY).rows


def test_bad_rows_are_counted_with_a_reason_and_the_rest_of_the_file_still_imports() -> None:
    lines = HOURLY.strip().split("\n")
    broken = "\n".join(
        [
            lines[0],
            "not-a-date,15,15,4",  # unparseable timestamp
            "2026-09-04 12:00:00,fifteen,15,4",  # unparseable number
            "2026-09-04 11:00:00,15,15,",  # a column with nothing in it
            *lines[1:],
        ]
    )
    parsed = parse_hourly(broken)
    assert parsed.skipped == 3
    assert parsed.skipped_reasons == {"not a date": 1, "not a number": 1, "missing a value": 1}
    # …and every good row is still there.
    assert len(parsed.rows) == 179


def test_a_repeated_hour_keeps_the_first_and_counts_the_rest() -> None:
    lines = HOURLY.strip().split("\n")
    doubled = "\n".join([lines[0], lines[1], *lines[1:]])
    parsed = parse_hourly(doubled)
    assert parsed.skipped_reasons == {"duplicate": 1}
    assert len(parsed.rows) == 179


def test_an_impossible_uptime_is_a_misread_column_and_is_refused() -> None:
    parsed = parse_daily("Day,Uptime %,Max Listeners\n2026-09-03 00:00:00,4500,31\n")
    assert parsed.rows == []
    assert parsed.skipped_reasons == {"uptime outside 0-100": 1}


@pytest.mark.parametrize(
    "text",
    ["", "just some words\n", "a,b,c\n1,2,3\n", "Hour,Max Listeners\n2026-09-03 00:00:00,4\n"],
)
def test_a_file_that_is_neither_export_is_refused_before_anything_is_written(text: str) -> None:
    with pytest.raises(ImportFormatError):
        detect_format(text)


def test_asking_for_the_wrong_parser_is_an_error_and_not_a_silent_mismatch() -> None:
    with pytest.raises(ImportFormatError):
        parse_daily(HOURLY)
    with pytest.raises(ImportFormatError):
        parse_hourly(DAILY)


# --------------------------------------------------------------------------------------
# storage, and the precedence rule
# --------------------------------------------------------------------------------------


@requires_db
def test_importing_the_hourly_export_writes_every_hour_with_its_uptime_pair(session) -> None:  # noqa: ANN001
    report = import_csv(session, FEED, HOURLY)
    session.commit()
    assert report["format"] == "hourly"
    assert report["rows_parsed"] == report["rows_written"] == 179
    assert report["rows_skipped"] == 0
    assert report["first"] == hour("2026-08-28 06:00:00")
    assert report["last"] == hour("2026-09-04 16:00:00")

    row = session.get(BcfyFeedHourly, (FEED, hour("2026-09-04 04:00:00")))
    assert row.source == "import"
    assert row.samples == 0
    assert row.online_checks == 15
    assert row.online_reports == 10
    # The export carries a peak and no average, so the peak stands in for both — and says so
    # through `source`, which is what keeps it out of every sample-weighted mean.
    assert row.listeners_avg == float(row.listeners_max)


@requires_db
def test_importing_the_daily_export_writes_a_row_per_day_and_is_idempotent(session) -> None:  # noqa: ANN001
    first = import_csv(session, FEED, DAILY)
    session.commit()
    assert first["format"] == "daily"
    assert first["rows_written"] == 179
    assert first["first"] == date(2026, 3, 9)

    again = import_csv(session, FEED, DAILY)
    session.commit()
    assert again["rows_written"] == 179
    assert (
        session.scalar(select(BcfyFeedDaily).where(BcfyFeedDaily.day == date(2026, 5, 17))).listeners_max
        == 412
    )
    assert len(list(session.scalars(select(BcfyFeedDaily)))) == 179


@requires_db
def test_an_import_overwrites_a_page_row_because_the_export_is_the_better_number(session) -> None:  # noqa: ANN001
    """The one direction the page rule never allowed, and the reason it now does.

    A sparkline bar is a height Broadcastify rounded for a tooltip; the export is the figure
    their own dashboard reports for that hour.  Where the two disagree the export is right.
    """
    moment = hour("2026-09-04 16:00:00")
    store.record_page_hours(session, feed_id=FEED, bars=[999], now=moment)
    session.commit()
    assert session.get(BcfyFeedHourly, (FEED, moment)).listeners_max == 999

    import_csv(session, FEED, HOURLY)
    session.commit()
    row = session.get(BcfyFeedHourly, (FEED, moment))
    assert row.listeners_max == 7  # the export's figure for that hour
    assert row.source == "import"


@requires_db
def test_a_page_fetch_never_overwrites_an_imported_hour(session) -> None:  # noqa: ANN001
    """…and the same rule the other way round, which is the half a one-way test would miss."""
    moment = hour("2026-09-04 16:00:00")
    import_csv(session, FEED, HOURLY)
    session.commit()

    written = store.record_page_hours(session, feed_id=FEED, bars=[999], now=moment)
    session.commit()
    assert written == 0
    row = session.get(BcfyFeedHourly, (FEED, moment))
    assert row.listeners_max == 7
    assert row.source == "import"


@requires_db
def test_reimporting_refreshes_an_import_row(session) -> None:  # noqa: ANN001
    moment = hour("2026-09-04 16:00:00")
    edited = HOURLY.replace("2026-09-04 16:00:00,12,12,7", "2026-09-04 16:00:00,15,15,41")
    import_csv(session, FEED, HOURLY)
    session.commit()
    report = import_csv(session, FEED, edited)
    session.commit()
    assert report["rows_written"] == 179
    row = session.get(BcfyFeedHourly, (FEED, moment))
    assert row.listeners_max == 41
    assert row.online_checks == 15


@requires_db
def test_an_import_never_touches_an_hour_this_deployment_polled(session) -> None:  # noqa: ANN001
    moment = hour("2026-09-04 16:00:00")
    store.roll_up(session, feed_id=FEED, hour=moment, listeners=20)
    session.commit()

    report = import_csv(session, FEED, HOURLY)
    session.commit()
    # 179 rows parsed, 178 written: the polled hour refused it.
    assert report["rows_parsed"] == 179
    assert report["rows_written"] == 178
    row = session.get(BcfyFeedHourly, (FEED, moment))
    assert row.listeners_max == 20
    assert row.samples == 1
    assert row.source is None


@requires_db
def test_a_later_poll_replaces_an_import_row_whole(session) -> None:  # noqa: ANN001
    """The top of the order: this deployment's own measurement beats Broadcastify's summary.

    "Replaces", not "averages": ``(7 + 20) / 2`` would bake the export's rounded hourly peak
    into this archive's own average for ever, and a ``GREATEST`` would keep a peak nothing
    here ever saw.  The uptime pair goes with it, because it describes an hour the row no
    longer holds.
    """
    moment = hour("2026-09-04 16:00:00")
    import_csv(session, FEED, HOURLY)
    session.commit()

    store.roll_up(session, feed_id=FEED, hour=moment, listeners=20)
    session.commit()
    row = session.get(BcfyFeedHourly, (FEED, moment))
    assert (row.listeners_avg, row.listeners_max, row.samples) == (20.0, 20, 1)
    assert row.source is None
    assert row.online_checks is None
    assert row.online_reports is None

    # …and the second poll averages against the first, not against the import.
    store.roll_up(session, feed_id=FEED, hour=moment, listeners=30)
    session.commit()
    # `roll_up` computes in SQL, so the mapped instance in this session is stale by
    # construction; the assertion is about the row, not about the identity map.
    session.expire_all()
    fresh = session.get(BcfyFeedHourly, (FEED, moment))
    assert fresh.listeners_avg == pytest.approx(25.0)
    assert fresh.samples == 2
