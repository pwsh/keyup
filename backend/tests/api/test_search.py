"""Full-text search: ranking, highlights and suggestions (§4.3)."""

from __future__ import annotations

from tests.conftest import requires_db

pytestmark = requires_db


def test_search_ranks_and_highlights(client, archive) -> None:
    response = client.get("/api/search", params={"q": "structure fire"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    hit = body["items"][0]
    assert hit["call"]["id"] == archive.transcribed_id
    assert hit["rank"] > 0
    assert "<mark>" in hit["highlight"]
    assert "fire" in hit["highlight"].lower()
    assert response.headers["X-Total-Count"] == "1"


def test_search_understands_websearch_syntax(client, archive) -> None:
    assert client.get("/api/search", params={"q": '"heavy smoke"'}).json()["total"] == 1
    assert client.get("/api/search", params={"q": '"smoke heavy"'}).json()["total"] == 0
    assert client.get("/api/search", params={"q": "fire -Cherry"}).json()["total"] == 0
    assert client.get("/api/search", params={"q": "fire or nonexistentword"}).json()["total"] >= 1


def test_search_matches_the_recorded_alias_when_there_is_no_transcript(client, archive) -> None:
    """An untranscribed call is still findable by the channel it was on, and says so."""
    hits = client.get("/api/search", params={"q": "EVENT"}).json()["items"]
    assert hits
    assert any(h["call"]["transcript_text"] is None for h in hits)
    assert any("<mark>" in h["highlight"] for h in hits)


def test_search_accepts_the_call_filters(client, archive) -> None:
    both = client.get("/api/search", params={"q": "fire"}).json()["total"]
    narrowed = client.get(
        "/api/search", params={"q": "fire", "talkgroup": 1421}
    ).json()["total"]
    assert narrowed < both


def test_search_returns_facets_unless_asked_not_to(client, archive) -> None:
    with_facets = client.get("/api/search", params={"q": "fire"}).json()
    assert with_facets["facets"]["total"] == with_facets["total"]
    without = client.get("/api/search", params={"q": "fire", "facets": False}).json()
    assert without["facets"]["total"] == 0


def test_search_without_a_query_is_a_plain_list(client, archive) -> None:
    body = client.get("/api/search").json()
    assert body["total"] == len(archive.call_ids)
    assert body["items"][0]["highlight"] == ""


def test_suggest_offers_aliases_and_units(client, archive) -> None:
    suggestions = client.get("/api/search/suggest", params={"q": "EVENT"}).json()
    assert any(s["kind"] == "talkgroup" and "EVENT" in s["value"] for s in suggestions)

    units = client.get("/api/search/suggest", params={"q": "E1"}).json()
    assert any(s["kind"] == "unit" and s["value"] == "E1" for s in units)


def test_suggest_includes_user_tags(client, archive, write_token) -> None:
    client.patch(
        f"/api/calls/{archive.call_ids[0]}",
        json={"tags": ["hazmat"]},
        headers={"Authorization": f"Bearer {write_token}"},
    )
    suggestions = client.get("/api/search/suggest", params={"q": "hazm"}).json()
    assert any(s["kind"] == "tag" and s["value"] == "hazmat" for s in suggestions)


def test_search_pages_by_offset_in_rank_order(client, archive) -> None:
    """The Search page scrolls with ``page``: consecutive pages never repeat a call."""
    whole = client.get("/api/search", params={"q": "EVENT", "facets": False}).json()
    assert whole["total"] >= 2
    seen: list[int] = []
    for page in range(1, whole["total"] + 1):
        body = client.get(
            "/api/search", params={"q": "EVENT", "facets": False, "page": page, "page_size": 1}
        ).json()
        assert body["page"] == page
        seen.extend(hit["call"]["id"] for hit in body["items"])
    assert seen == [hit["call"]["id"] for hit in whole["items"]][: len(seen)]
    assert len(seen) == len(set(seen)) == whole["total"]


def test_search_says_when_its_total_is_capped(client, archive, monkeypatch) -> None:  # noqa: ANN001
    from app.api import filters
    from app.services import calls as call_service

    body = client.get("/api/search", params={"q": "EVENT", "facets": False}).json()
    assert body["total_capped"] is False
    monkeypatch.setattr(
        call_service, "capped_count", lambda session, f: filters.capped_count(session, f, cap=1)
    )
    capped = client.get("/api/search", params={"q": "EVENT", "facets": False}).json()
    assert capped["total"] == 1
    assert capped["total_capped"] is True
