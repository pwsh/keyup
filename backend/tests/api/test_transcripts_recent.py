"""``GET /api/transcripts/recent`` — the first screen of the embeddable live view.

What must hold: the filter means exactly what it means on ``GET /calls`` (it is the same
code, and the comparison test below keeps it that way), the window is the last N minutes
and no more, a duplicate never appears, and the view's own row rule — a finished
transcript that says something — is the only thing added on top.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import requires_db

pytestmark = requires_db

URL = "/api/transcripts/recent"


@pytest.fixture
def recent(session, archive):  # noqa: ANN001, ANN201
    """Two transcribed calls on 1701 (a *fire* channel in the fixture directory), 5 and 30
    minutes ago, and a still-pending one on the same channel a minute ago."""
    from app.models import Call

    now = datetime.now(UTC)
    fresh = session.get(Call, archive.transcribed_id)
    fresh.started_at = now - timedelta(minutes=5)
    older_id = next(i for i in archive.by_tgid[1701] if i != archive.transcribed_id)
    older = session.get(Call, older_id)
    older.started_at = now - timedelta(minutes=30)
    older.transcript_text = "Medic 4 on scene."
    older.transcript_status = "done"
    older.transcript_quality = "good"
    pending_id = next(
        i for i in archive.by_tgid[1701] if i not in (archive.transcribed_id, older_id)
    )
    pending = session.get(Call, pending_id)
    pending.started_at = now - timedelta(minutes=1)
    pending.transcript_status = "queued"
    pending.transcript_text = None
    session.commit()
    return {"fresh": fresh.id, "older": older.id, "pending": pending.id}


def ids(client, **params) -> list[int]:  # noqa: ANN001
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


def test_the_default_window_is_the_last_fifteen_minutes(client, recent) -> None:  # noqa: ANN001
    assert ids(client) == [recent["fresh"]]
    assert ids(client, minutes=60) == [recent["fresh"], recent["older"]]


def test_a_row_carries_only_what_the_view_prints(client, recent) -> None:  # noqa: ANN001
    body = client.get(URL).json()
    assert body["minutes"] == 15
    row = body["items"][0]
    assert set(row) == {
        "id",
        "started_at",
        "started_local",
        "duration_s",
        "tgid",
        "talkgroup_alias",
        "radio_label",
        "transcript_text",
        "transcript_status",
        "transcript_quality",
    }
    assert row["tgid"] == 1701
    assert row["transcript_text"].startswith("Engine 1 responding")
    assert client.get(URL).headers["cache-control"] == "no-store"


def test_minutes_is_bounded(client, recent) -> None:  # noqa: ANN001
    assert client.get(URL, params={"minutes": 240}).status_code == 200
    assert client.get(URL, params={"minutes": 241}).status_code == 422
    assert client.get(URL, params={"minutes": 0}).status_code == 422
    assert client.get(URL, params={"limit": 501}).status_code == 422


def test_limit_keeps_the_newest(client, recent) -> None:  # noqa: ANN001
    assert ids(client, minutes=60, limit=1) == [recent["fresh"]]


def test_a_duplicate_is_never_shown(client, session, recent) -> None:  # noqa: ANN001
    from app.models import Call

    session.get(Call, recent["older"]).duplicate_of = recent["fresh"]
    session.commit()
    assert ids(client, minutes=60) == [recent["fresh"]]
    # Not even when the caller asks for them: the view is a list of transmissions.
    assert ids(client, minutes=60, include_duplicates="true") == [recent["fresh"]]


def test_pending_and_rejected_transcripts_wait_unless_asked_for(client, session, recent) -> None:  # noqa: ANN001
    from app.models import Call

    assert recent["pending"] not in ids(client)
    assert ids(client, show_pending="true")[0] == recent["pending"]

    older = session.get(Call, recent["older"])
    older.transcript_quality = "noise"
    session.commit()
    assert recent["older"] not in ids(client, minutes=60)
    older.transcript_quality = "good"
    older.transcript_text = "   "
    session.commit()
    assert recent["older"] not in ids(client, minutes=60)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"category": "fire"},
        {"category": "law"},
        {"talkgroup": 1701},
        {"talkgroup": 1601},
        {"q": "structure fire"},
        {"agency": "EXAMPLEFD"},
        {"radio": 999_999},
    ],
)
def test_the_filter_means_what_it_means_on_the_calls_page(client, recent, params) -> None:  # noqa: ANN001
    """Same parameters, same window, same rows — minus only the view's transcript rule."""
    body = client.get(URL, params={**params, "minutes": 60}).json()
    listed = client.get(
        "/api/calls", params={**params, "date_from": body["since"], "page_size": 200}
    ).json()["items"]
    expected = [
        c["id"]
        for c in listed
        if c["transcript_status"] == "done"
        and (c["transcript_text"] or "").strip()
        and c["transcript_quality"] != "noise"
    ]
    assert [row["id"] for row in body["items"]] == expected


def test_a_preset_supplies_its_categories(client, recent) -> None:  # noqa: ANN001
    body = client.get(URL, params={"preset": "emergency", "minutes": 60}).json()
    assert body["preset"] == "emergency"
    assert body["filter"]["category"] == ["law", "fire", "ems", "multi", "interop"]
    assert [row["id"] for row in body["items"]] == [recent["fresh"], recent["older"]]
    # ...and the live filter is rolling: no date bounds, nothing a socket would choke on.
    assert "date_from" not in body["filter"]
    assert "date_to" not in body["filter"]
    assert "sort" not in body["filter"]

    assert ids(client, preset="law", minutes=60) == []
    assert ids(client, preset="all", minutes=60) == [recent["fresh"], recent["older"]]
    assert ids(client, preset="fire", minutes=60) == [recent["fresh"], recent["older"]]


def test_an_explicit_category_overrides_the_preset(client, recent) -> None:  # noqa: ANN001
    body = client.get(URL, params={"preset": "emergency", "category": "law", "minutes": 60}).json()
    assert body["filter"]["category"] == ["law"]
    assert body["items"] == []


def test_an_unknown_preset_is_a_404(client, recent) -> None:  # noqa: ANN001
    assert client.get(URL, params={"preset": "nope"}).status_code == 404
    assert client.get(URL, params={"preset": "nope", "category": "fire"}).status_code == 404


def test_the_filter_round_trips_through_the_live_socket(client, recent) -> None:  # noqa: ANN001
    """The echoed filter is accepted as a ``WS /api/live`` first frame as-is."""
    body = client.get(URL, params={"preset": "emergency", "talkgroup": 1701}).json()
    with client.websocket_connect("/api/live") as socket:
        socket.send_json(body["filter"])
        ack = socket.receive_json()
    assert ack["type"] == "filter.ok"
    assert ack["filter"]["category"] == body["filter"]["category"]
    assert ack["filter"]["talkgroup"] == [1701]


def test_the_preset_list_is_offered_on_request(client, recent) -> None:  # noqa: ANN001
    assert client.get(URL).json()["presets"] is None
    presets = client.get(URL, params={"presets": "true"}).json()["presets"]
    slugs = [p["slug"] for p in presets]
    assert slugs[0] == "all"
    assert "emergency" in slugs
    assert {"slug": "emergency", "name": "Emergency dispatch"} in presets


def test_scrolling_past_the_first_screen_reads_on_before_the_window(client, recent) -> None:  # noqa: ANN001
    """The first screen is the last 15 minutes; its ``next_cursor`` continues into what came
    before — the 30-minute-old call — however far outside the window that is."""
    first = client.get(URL).json()
    assert [row["id"] for row in first["items"]] == [recent["fresh"]]
    assert first["next_cursor"]

    older = client.get(URL, params={"cursor": first["next_cursor"]}).json()
    assert recent["older"] in [row["id"] for row in older["items"]]
    assert recent["fresh"] not in [row["id"] for row in older["items"]]
    # A short cursor page is the end of the archive for this filter.
    assert older["next_cursor"] is None


def test_a_full_cursor_page_offers_the_next_one_and_pages_never_overlap(client, recent) -> None:  # noqa: ANN001
    first = client.get(URL, params={"minutes": 60, "limit": 1}).json()
    assert [row["id"] for row in first["items"]] == [recent["fresh"]]
    second = client.get(URL, params={"cursor": first["next_cursor"], "limit": 1}).json()
    assert [row["id"] for row in second["items"]] == [recent["older"]]
    assert second["next_cursor"]  # full page: there may be more


def test_an_empty_first_screen_still_scrolls_into_the_past(client, recent, session) -> None:  # noqa: ANN001
    from app.models import Call

    fresh = session.get(Call, recent["fresh"])
    fresh.started_at = datetime.now(UTC) - timedelta(minutes=20)
    session.commit()
    first = client.get(URL).json()
    assert first["items"] == []
    assert first["next_cursor"]
    older = client.get(URL, params={"cursor": first["next_cursor"]}).json()
    assert [row["id"] for row in older["items"]][:2] == [recent["fresh"], recent["older"]]


def test_a_cursor_that_is_not_one_is_a_422(client, recent) -> None:  # noqa: ANN001
    assert client.get(URL, params={"cursor": "not-a-cursor"}).status_code == 422
