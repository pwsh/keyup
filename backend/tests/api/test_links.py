"""The per-minute timeline and persisted links to outside records (§4.6)."""

from __future__ import annotations

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_timeline_is_per_minute_activity(client, archive) -> None:
    points = client.get(
        "/api/timeline",
        params={"from": "2026-08-28T00:00:00-05:00", "to": "2026-08-30T00:00:00-05:00"},
    ).json()
    assert points
    assert sum(p["count"] for p in points) == len(archive.call_ids)
    assert all(p["minute"].endswith(":00Z") or p["minute"].endswith(":00+00:00") for p in points)


def test_links_persist_a_confirmed_match(client, archive, write_token) -> None:
    call_id = archive.transcribed_id
    created = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260829123",
            "link_confidence": "strong",
            "link_method": "unit",
            "link_note": "Engine 1 on the fire ground at the dispatch time.",
        },
        headers=bearer(write_token),
    )
    assert created.status_code == 201
    link_id = created.json()["id"]
    assert created.json()["created_by"].startswith("token:")

    # Re-posting the same triple updates rather than duplicating.  A non-manual source has
    # to say how sure it is *and* how it decided on every post, update included — see
    # ``test_a_machine_written_link_must_say_how_sure_it_is``.
    again = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260829123",
            "link_confidence": "probable",
            "link_method": "unit",
        },
        headers=bearer(write_token),
    )
    assert again.json()["id"] == link_id
    assert again.json()["link_confidence"] == "probable"
    assert client.get("/api/links").json()["total"] == 1

    by_record = client.get("/api/links/cad/20260829123").json()
    assert by_record["count"] == 1
    assert by_record["calls"][0]["id"] == call_id

    # …and the call detail carries it, so one request tells the whole story.
    assert client.get(f"/api/calls/{call_id}").json()["links"][0]["external_id"] == "20260829123"

    assert client.delete(f"/api/links/{link_id}", headers=bearer(write_token)).status_code == 204
    assert client.get("/api/links").json()["total"] == 0


def test_a_link_needs_a_call_or_a_conversation(client, archive, write_token) -> None:
    response = client.post(
        "/api/links", json={"source": "manual", "external_id": "x"}, headers=bearer(write_token)
    )
    assert response.status_code == 422
    missing = client.post(
        "/api/links",
        json={"call_id": 999999, "source": "manual", "external_id": "x"},
        headers=bearer(write_token),
    )
    assert missing.status_code == 404


def test_a_machine_written_link_must_say_how_sure_it_is(client, archive, write_token) -> None:
    """A named `source` is a machine claiming authorship; the defaults belong to a person.

    ``{call_id, external_id, source: "cad"}`` used to be filed as ``strong``/``manual``
    — a guess wearing a librarian's signature.  It is now a 422 that names the two fields
    the caller left out, so an integrator finds this at their first post rather than in the
    archive months later.
    """
    call_id = archive.transcribed_id
    response = client.post(
        "/api/links",
        json={"call_id": call_id, "source": "cad", "external_id": "20260901001"},
        headers=bearer(write_token),
    )
    assert response.status_code == 422, response.text
    # The app flattens pydantic's error list into one ``field: message`` string, so the
    # 422 names both fields rather than saying "invalid request" and leaving the integrator
    # to guess which of the six keys was wrong.
    detail = response.json()["detail"]
    assert "link_confidence: Field required" in detail
    assert "link_method: Field required" in detail
    assert "cad" in detail

    # Naming one of the two is still not naming both.
    half = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260901001",
            "link_confidence": "probable",
        },
        headers=bearer(write_token),
    )
    assert half.status_code == 422
    assert half.json()["detail"].startswith("link_method: Field required")
    assert "link_confidence" not in half.json()["detail"]

    # An explicit null is a missing value, not a way round the rule.
    nulled = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260901001",
            "link_confidence": None,
            "link_method": "unit",
        },
        headers=bearer(write_token),
    )
    assert nulled.status_code == 422
    assert nulled.json()["detail"].startswith("link_confidence: Field required")

    # And with both, it is a link.
    ok = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260901001",
            "link_confidence": "probable",
            "link_method": "time+agency",
        },
        headers=bearer(write_token),
    )
    assert ok.status_code == 201, ok.text


def test_the_manual_defaults_are_unchanged(client, archive, write_token) -> None:
    """A person's link is still ``{call_id, external_id}`` — the UI posts exactly that."""
    call_id = archive.transcribed_id
    implied = client.post(
        "/api/links",
        json={"call_id": call_id, "external_id": "20260901002"},
        headers=bearer(write_token),
    )
    assert implied.status_code == 201, implied.text
    assert implied.json()["source"] == "manual"
    assert implied.json()["link_confidence"] == "strong"
    assert implied.json()["link_method"] == "manual"

    # Spelling the manual source out is the same request, not a stricter one.
    spelled = client.post(
        "/api/links",
        json={"call_id": call_id, "source": "manual", "external_id": "20260901003"},
        headers=bearer(write_token),
    )
    assert spelled.status_code == 201, spelled.text
    assert spelled.json()["link_confidence"] == "strong"
    assert spelled.json()["link_method"] == "manual"


def test_a_transcript_match_is_a_link_method_of_its_own(client, archive, write_token) -> None:
    """``text`` and ``time+text``: evidence Keyup cannot see but a consumer can.

    A CAD integration re-scores our candidates against the words of its own incident. Before these
    values existed that had to be filed as ``time`` or as ``manual`` — both of which hide
    the strongest evidence in the link.
    """
    call_id = archive.transcribed_id
    for index, method in enumerate(("text", "time+text")):
        response = client.post(
            "/api/links",
            json={
                "call_id": call_id,
                "source": "cad",
                "external_id": f"2026090200{index}",
                "link_confidence": "probable",
                "link_method": method,
            },
            headers=bearer(write_token),
        )
        assert response.status_code == 201, response.text
        assert response.json()["link_method"] == method

    # …and the vocabulary is still a vocabulary.
    unknown = client.post(
        "/api/links",
        json={
            "call_id": call_id,
            "source": "cad",
            "external_id": "20260902999",
            "link_confidence": "probable",
            "link_method": "vibes",
        },
        headers=bearer(write_token),
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"].startswith("link_method: Input should be ")
