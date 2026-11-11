"""The activity feed API: ``GET /api/activity`` (admin only) and its filters."""

from __future__ import annotations

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_activity_requires_admin(client, admin_token, read_token) -> None:
    # Anonymous → 401, a non-admin credential → 403, admin → 200.
    assert client.get("/api/activity").status_code == 401
    assert client.get("/api/activity", headers=bearer(read_token)).status_code == 403
    assert client.get("/api/activity", headers=bearer(admin_token)).status_code == 200


def test_activity_lists_ingest_rows_newest_first(client, archive, admin_token) -> None:
    rows = client.get("/api/activity", headers=bearer(admin_token)).json()
    assert isinstance(rows, list)
    assert rows, "the ingested archive should have produced activity rows"
    # Every ingested call produced a call.accepted (or call.duplicate) row.
    kinds = {row["kind"] for row in rows}
    assert "call.accepted" in kinds
    # Newest first: ids strictly decreasing.
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids, reverse=True)
    # The wire shape.
    first = rows[0]
    assert set(first) >= {"id", "at", "source", "level", "kind", "message", "call_id", "meta"}


def test_activity_filters_and_paginates(client, archive, admin_token) -> None:
    headers = bearer(admin_token)
    ingest = client.get("/api/activity", params={"source": "ingest"}, headers=headers).json()
    assert ingest and all(row["source"] == "ingest" for row in ingest)

    # limit is honoured and before_id keysets to the previous page.
    page1 = client.get("/api/activity", params={"limit": 2}, headers=headers).json()
    assert len(page1) == 2
    page2 = client.get(
        "/api/activity", params={"limit": 2, "before_id": page1[-1]["id"]}, headers=headers
    ).json()
    assert all(row["id"] < page1[-1]["id"] for row in page2)

    # An out-of-range limit is rejected by validation.
    assert client.get("/api/activity", params={"limit": 9999}, headers=headers).status_code == 422


def test_activity_stream_requires_admin(client, read_token) -> None:
    assert client.get("/api/activity/stream").status_code == 401
    assert client.get("/api/activity/stream", headers=bearer(read_token)).status_code == 403
