"""``GET /api/stats/gaps`` — the range, the system filter and the page (§3.7).

A CAD consumer reads this endpoint to answer "was the recorder down during my incident", which
is a question about *overlap*: the most interesting outage for a two-hour incident is the
one that began yesterday and is still running, and a containment test would be the one
shape that misses it.  These tests pin that, plus the paging the two-year backlog made
necessary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.conftest import requires_db

pytestmark = requires_db

DAY = datetime(2026, 8, 20, tzinfo=UTC)


@pytest.fixture
def outages(session, archive):  # noqa: ANN001, ANN201, ARG001
    """Four stored gaps across two systems, arranged around 2026-08-20 local."""
    from app.models import RecorderGap, System

    example = session.scalars(select(System).where(System.name == "Example System")).one()
    other = System(name="Example County")
    session.add(other)
    session.flush()

    rows = {
        # name: (system, started_at, ended_at)
        "before": (example, DAY - timedelta(days=2), DAY - timedelta(days=2) + timedelta(hours=1)),
        "inside": (example, DAY + timedelta(hours=12), DAY + timedelta(hours=13)),
        "spanning": (example, DAY - timedelta(days=1), DAY + timedelta(days=2)),
        "other_system": (other, DAY + timedelta(hours=14), DAY + timedelta(hours=15)),
    }
    ids = {}
    for name, (system, started, ended) in rows.items():
        gap = RecorderGap(
            system_id=system.id,
            started_at=started,
            ended_at=ended,
            duration_s=(ended - started).total_seconds(),
            note=name,
        )
        session.add(gap)
        session.flush()
        ids[name] = gap.id
    session.commit()
    return {"ids": ids, "example": example.id, "other": other.id}


def _notes(response) -> list[str]:  # noqa: ANN001
    return [row["note"] for row in response.json()]


def test_a_range_matches_every_gap_that_overlaps_it(client, outages) -> None:  # noqa: ANN001, ARG001
    """Including the one that covers the whole day and neither starts nor ends in it."""
    response = client.get("/api/stats/gaps", params={"from": "2026-08-20", "to": "2026-08-20"})
    assert _notes(response) == ["other_system", "inside", "spanning"]  # newest start first


def test_a_bare_date_covers_the_whole_local_day(client, outages) -> None:  # noqa: ANN001, ARG001
    """``to=2026-08-19`` runs to the end of that *local* day, so the span still overlaps."""
    assert _notes(client.get("/api/stats/gaps", params={"to": "2026-08-19"})) == [
        "spanning",
        "before",
    ]


def test_a_timestamp_is_accepted_as_well_as_a_date(client, outages) -> None:  # noqa: ANN001, ARG001
    response = client.get(
        "/api/stats/gaps",
        params={"from": "2026-08-20T13:30:00+00:00", "to": "2026-08-20T23:59:59+00:00"},
    )
    assert _notes(response) == ["other_system", "spanning"]  # "inside" had already ended


def test_the_system_filter_narrows_to_one_recorder(client, outages) -> None:  # noqa: ANN001
    response = client.get(
        "/api/stats/gaps",
        params={"from": "2026-08-20", "to": "2026-08-20", "system_id": outages["example"]},
    )
    assert _notes(response) == ["inside", "spanning"]


def test_limit_and_offset_page_the_same_ordering(client, outages) -> None:  # noqa: ANN001, ARG001
    params = {"from": "2026-08-20", "to": "2026-08-20", "limit": 1}
    assert _notes(client.get("/api/stats/gaps", params=params)) == ["other_system"]
    assert _notes(client.get("/api/stats/gaps", params={**params, "offset": 1})) == ["inside"]
    assert _notes(client.get("/api/stats/gaps", params={**params, "offset": 3})) == []


def test_a_negative_offset_is_a_422_rather_than_a_silent_reversal(client, outages) -> None:  # noqa: ANN001, ARG001
    assert client.get("/api/stats/gaps", params={"offset": -1}).status_code == 422


def test_hours_still_means_what_it_meant(client, outages, session) -> None:  # noqa: ANN001, ARG001
    """The window is unchanged and independent of the new range parameters."""
    from app.models import RecorderGap

    now = datetime.now(UTC)
    session.add(
        RecorderGap(
            system_id=outages["example"],
            started_at=now - timedelta(hours=3),
            ended_at=now - timedelta(hours=1),
            duration_s=7200,
            note="recent",
        )
    )
    session.commit()
    assert _notes(client.get("/api/stats/gaps", params={"hours": 2})) == ["recent"]
