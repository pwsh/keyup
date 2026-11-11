"""``ingest_priority`` on the wire: the field and the `?priority=` filter.

The archive fixture is ingested off disk, so every row starts with a NULL class — which
is the honest starting state and the reason each test stamps the rows it means to test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Call
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def priced(session, archive):  # noqa: ANN001, ANN201
    """Give the six fixture calls one class each, so every band has a member.

    ``42`` and ``900`` are there on purpose: the column is free text and the push path
    accepts a bare integer, so the filter has to judge those against the queue's band
    edges rather than only recognizing the four class names.
    """
    calls = session.scalars(select(Call).order_by(Call.id)).all()
    classes = ["live", "urgent", "42", "backlog", "700", "bulk"]
    for call, name in zip(calls, classes, strict=False):
        call.ingest_priority = name
    session.commit()
    return {call.id: call.ingest_priority for call in calls}


def test_the_field_rides_along_on_the_row(client, archive, priced) -> None:  # noqa: ANN001
    items = client.get("/api/calls").json()["items"]
    assert {item["ingest_priority"] for item in items} == set(priced.values())


def test_the_detail_page_carries_it_too(client, archive, priced) -> None:  # noqa: ANN001
    call_id = next(iter(priced))
    assert client.get(f"/api/calls/{call_id}").json()["ingest_priority"] == priced[call_id]


def test_a_watcher_row_has_no_class(client, archive) -> None:  # noqa: ANN001
    """Off-disk ingest says nothing about admission, and `null` is the truthful answer."""
    assert all(item["ingest_priority"] is None for item in client.get("/api/calls").json()["items"])


def test_live_covers_urgent_and_the_low_integers(client, archive, priced) -> None:  # noqa: ANN001
    found = client.get("/api/calls", params={"priority": "live"}).json()
    assert {priced[item["id"]] for item in found["items"]} == {"live", "urgent", "42"}


def test_backlog_and_bulk_are_separate_bands(client, archive, priced) -> None:  # noqa: ANN001
    backlog = client.get("/api/calls", params={"priority": "backlog"}).json()
    assert {priced[item["id"]] for item in backlog["items"]} == {"backlog", "700"}

    bulk = client.get("/api/calls", params={"priority": "bulk"}).json()
    assert {priced[item["id"]] for item in bulk["items"]} == {"bulk"}


def test_classes_or_together(client, archive, priced) -> None:  # noqa: ANN001
    both = client.get("/api/calls", params=[("priority", "backlog"), ("priority", "bulk")]).json()
    assert both["total"] == 3


def test_an_unclassified_row_matches_no_class(client, session, archive) -> None:  # noqa: ANN001
    """NULL is not quietly folded into `live`: it means nobody recorded an admission class."""
    for name in ("live", "backlog", "bulk"):
        assert client.get("/api/calls", params={"priority": name}).json()["total"] == 0


def test_an_unknown_class_is_a_422(client, archive) -> None:  # noqa: ANN001
    response = client.get("/api/calls", params={"priority": "sometime"})
    assert response.status_code == 422
    assert "sometime" in response.json()["detail"]


def test_the_export_stream_carries_it(client, archive, priced) -> None:  # noqa: ANN001
    body = client.get("/api/calls/export", params={"format": "ndjson"}).text
    assert '"ingest_priority"' in body
