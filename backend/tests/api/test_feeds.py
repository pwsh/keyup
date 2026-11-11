"""Syndication feeds: RSS, Atom, the token that is the credential, and the audio grant.

The suite's job is the one thing that can really go wrong here: a feed URL is a bearer
credential handed to software, so it has to publish *exactly* the filter it was built from
and nothing wider — including through the audio enclosure, which is the one place a feed
over one talkgroup could become a key to the whole archive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select, update

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

ATOM = "{http://www.w3.org/2005/Atom}"

#: ``transcript_status`` values that hold an item back (app.api.feeds).
IN_FLIGHT = ("pending", "queued", "running")


@pytest.fixture(autouse=True)
def ingestion_finished(session, archive):  # noqa: ANN001, ANN201
    """The archive as it looks once the pipeline has let go of it.

    A feed publishes a call only when the record is complete, and the ``archive`` fixture
    is deliberately caught mid-flight: every one of its calls has an open ``audio_jobs``
    row and three are still queued for transcription. Left alone, every document in this
    module would be empty and the rest of the suite would be testing the gate and nothing
    else — so the gate gets its own tests, and everything else sees finished rows.
    """
    from app.models import AudioJob, Call

    session.execute(update(AudioJob).values(status="done", finished_at=datetime.now(UTC)))
    session.execute(
        update(Call)
        .where(Call.id.in_(archive.call_ids), Call.transcript_status.in_(IN_FLIGHT))
        .values(transcript_status="empty")
    )
    session.commit()


def make_feed(client, token, **body):  # noqa: ANN001, ANN201
    payload = {"name": "Fire", "query": {}, **body}
    response = client.post("/api/feeds", json=payload, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


def token_of(feed) -> str:  # noqa: ANN001
    return feed["rss_url"].rsplit("/", 1)[-1].removesuffix(".rss")


# ----------------------------------------------------------------------- creation ---


def test_creating_a_feed_normalises_the_query_and_returns_both_urls(client, archive, write_token) -> None:  # noqa: ANN001
    """The SPA's filter object carries whatever else lives in the address bar; what gets
    stored is what the filter language actually understood."""
    feed = make_feed(
        client,
        write_token,
        name="Event 6",
        query={"talkgroup": [1701], "page": 3, "view": "cards", "nonsense": True},
    )
    assert feed["query"] == {"talkgroup": [1701], "include_duplicates": False, "include_shared": False}
    assert "page" not in feed["query"] and "view" not in feed["query"]
    assert feed["summary"].startswith("talkgroups EXAMPLE EVENT 1")  # the alias, not the id the picker sent
    assert feed["rss_url"].endswith(".rss") and feed["atom_url"].endswith(".atom")
    assert token_of(feed) in feed["atom_url"]
    assert len(token_of(feed)) >= 32


def test_max_items_is_capped_at_two_hundred(client, archive, write_token) -> None:  # noqa: ANN001
    """200 is the ceiling, and asking for more is refused rather than quietly trimmed:
    a feed that silently publishes a quarter of what was asked for is a bug report."""
    assert make_feed(client, write_token, name="Two hundred", max_items=200)["max_items"] == 200
    for bad_value in (5000, 0):
        bad = client.post(
            "/api/feeds",
            json={"name": f"Bad {bad_value}", "query": {}, "max_items": bad_value},
            headers=bearer(write_token),
        )
        assert bad.status_code == 422, bad.text
    assert make_feed(client, write_token, name="Default")["max_items"] == 50


def test_describe_filters_says_what_a_feed_publishes(client, archive, write_token) -> None:  # noqa: ANN001
    from app.api.feeds import describe_filters
    from app.api.filters import filters_from_mapping

    assert describe_filters(filters_from_mapping({})) == "Every call in the archive"
    described = describe_filters(
        filters_from_mapping({"q": "structure fire", "talkgroup": [1701], "emergency": True})
    )
    assert 'matching "structure fire"' in described
    assert "talkgroups 1701" in described
    assert "emergency only" in described
    # A long list is summarised rather than printed in full.
    many = describe_filters(filters_from_mapping({"talkgroup": list(range(1, 20))}))
    assert "+13 more" in many


# ------------------------------------------------------------------------ the documents ---


def test_rss_is_wellformed_and_carries_the_matching_calls(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Event 6", query={"talkgroup": [1701]})
    response = client.get(f"/api/feeds/{token_of(feed)}.rss")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert response.headers["cache-control"] == "max-age=60"

    root = ET.fromstring(response.text)
    channel = root.find("channel")
    assert channel.findtext("title") == "Event 6"
    assert channel.findtext("ttl") == "5"
    assert channel.findtext("lastBuildDate")
    assert "talkgroups EXAMPLE EVENT 1" in channel.findtext("description")
    self_link = channel.find("{http://www.w3.org/2005/Atom}link")
    assert self_link.get("rel") == "self"
    assert self_link.get("href").endswith(f"{token_of(feed)}.rss")

    items = channel.findall("item")
    expected = client.get("/api/calls", params={"talkgroup": 1701}).json()
    assert len(items) == expected["total"]
    assert [i.findtext("guid").rsplit("/", 1)[-1] for i in items] == [
        str(c["id"]) for c in expected["items"]
    ]
    first = items[0]
    assert first.find("guid").get("isPermaLink") == "true"
    assert first.findtext("link") == first.findtext("guid")
    assert first.findtext("pubDate")
    assert " · " in first.findtext("title")


def test_the_transcript_is_the_item_body(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="All", query={})
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    bodies = {
        int(i.findtext("guid").rsplit("/", 1)[-1]): i.findtext("description")
        for i in root.find("channel").findall("item")
    }
    assert "structure fire on Cherry Court" in bodies[archive.transcribed_id]
    # ... followed by a line of metadata, so an item is readable on its own.
    assert "radio E1" in bodies[archive.transcribed_id]
    assert "EXAMPLEFD" in bodies[archive.transcribed_id]
    others = [b for cid, b in bodies.items() if cid != archive.transcribed_id]
    assert all(b.startswith(("No transcript", "Transcript pending")) for b in others), others


def test_a_humans_correction_wins_over_the_engines_text(client, archive, session, write_token) -> None:  # noqa: ANN001
    """What a feed publishes is the archive's best text, not the first one it got."""
    from app.models import Transcript

    row = session.scalar(
        select(Transcript).where(Transcript.call_id == archive.transcribed_id, Transcript.is_primary)
    )
    row.edited_text = "Engine 1 responding to the structure fire on Cherry Street."
    session.commit()

    feed = make_feed(client, write_token, name="All", query={})
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    body = next(
        item.findtext("description")
        for item in root.find("channel").findall("item")
        if item.findtext("guid").endswith(f"/{archive.transcribed_id}")
    )
    assert "Cherry Street" in body
    assert "Cherry Court" not in body


def test_a_hallucination_is_named_rather_than_quoted(client, archive, session, write_token) -> None:  # noqa: ANN001
    """A transcript the archive has flagged as noise must not be republished as words."""
    from app.models import Call

    call = session.get(Call, archive.transcribed_id)
    call.transcript_quality = "noise"
    session.commit()

    feed = make_feed(client, write_token, name="All", query={})
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    body = next(
        item.findtext("description")
        for item in root.find("channel").findall("item")
        if item.findtext("guid").endswith(f"/{archive.transcribed_id}")
    )
    assert body.startswith("Rejected as hallucination")
    assert "Cherry Court" not in body


def test_atom_is_wellformed_and_matches_the_rss(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Event 6", query={"talkgroup": [1701]})
    response = client.get(f"/api/feeds/{token_of(feed)}.atom")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/atom+xml")

    root = ET.fromstring(response.text)
    assert root.tag == f"{ATOM}feed"
    assert root.findtext(f"{ATOM}title") == "Event 6"
    assert any(link.get("rel") == "self" for link in root.findall(f"{ATOM}link"))
    entries = root.findall(f"{ATOM}entry")
    rss = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    assert [e.findtext(f"{ATOM}id") for e in entries] == [
        i.findtext("guid") for i in rss.find("channel").findall("item")
    ]
    assert entries[0].findtext(f"{ATOM}updated")
    assert entries[0].find(f"{ATOM}content").get("type") == "text"


def test_max_items_limits_the_document(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Two", query={}, max_items=2)
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    assert len(root.find("channel").findall("item")) == 2


def test_a_fetch_is_counted(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Counted", query={})
    assert feed["fetch_count"] == 0 and feed["last_fetched_at"] is None
    client.get(f"/api/feeds/{token_of(feed)}.rss")
    client.get(f"/api/feeds/{token_of(feed)}.atom")
    again = next(f for f in client.get("/api/feeds", headers=bearer(write_token)).json() if f["id"] == feed["id"])
    assert again["fetch_count"] == 2
    assert again["last_fetched_at"] is not None


# ---------------------------------------------------------------------------- the token ---


def test_an_unknown_or_disabled_token_is_a_flat_404(client, archive, write_token) -> None:  # noqa: ANN001
    assert client.get("/api/feeds/not-a-real-token-at-all.rss").status_code == 404
    feed = make_feed(client, write_token, name="Off", query={})
    client.patch(
        f"/api/feeds/{feed['id']}", json={"enabled": False}, headers=bearer(write_token)
    )
    off = client.get(f"/api/feeds/{token_of(feed)}.rss")
    assert off.status_code == 404
    # Disabled and never-existed are indistinguishable on purpose.
    assert off.json() == client.get("/api/feeds/not-a-real-token-at-all.rss").json()


def test_rotating_mints_a_new_token_and_kills_the_old_url(client, archive, write_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Rotate", query={})
    old = token_of(feed)
    assert client.get(f"/api/feeds/{old}.rss").status_code == 200
    rotated = client.post(
        f"/api/feeds/{feed['id']}/rotate", headers=bearer(write_token)
    ).json()
    assert token_of(rotated) != old
    assert client.get(f"/api/feeds/{old}.rss").status_code == 404
    assert client.get(f"/api/feeds/{token_of(rotated)}.rss").status_code == 200


# -------------------------------------------------------------------------- ownership ---


def test_a_feed_belongs_to_its_owner(client, archive, api_user, user_password, write_token) -> None:  # noqa: ANN001
    """A token principal's feed is not the signed-in user's, and neither may touch the
    other's — missing rather than forbidden, because a 403 confirms the id."""
    theirs = make_feed(client, write_token, name="Theirs", query={})

    login = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    assert client.get("/api/feeds").json() == []
    assert client.patch(
        f"/api/feeds/{theirs['id']}", json={"name": "Mine now"}, headers=CSRF_HEADERS
    ).status_code == 404
    assert client.delete(f"/api/feeds/{theirs['id']}", headers=CSRF_HEADERS).status_code == 404


def test_an_admin_sees_every_feed_and_may_delete_one(client, archive, write_token, admin_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Theirs", query={})
    listed = client.get("/api/feeds", headers=bearer(admin_token)).json()
    assert [f["id"] for f in listed] == [feed["id"]]
    assert client.delete(f"/api/feeds/{feed['id']}", headers=bearer(admin_token)).status_code == 204
    assert client.get(f"/api/feeds/{token_of(feed)}.rss").status_code == 404


def test_creating_a_feed_needs_write(client, archive, read_token) -> None:  # noqa: ANN001
    response = client.post("/api/feeds", json={"name": "Nope", "query": {}}, headers=bearer(read_token))
    assert response.status_code == 403


def _second_write_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-write-2", ["read", "write"], created_by="tests")
    return raw


def test_a_token_owns_its_feeds_and_no_other_token_may_touch_them(client, archive, session, write_token, read_token) -> None:  # noqa: ANN001, E501
    """Token principals have no user, so every token-made feed used to share the owner
    NULL: any read token listed every other token's secret URLs and any write token could
    edit, rotate or delete them. A token now owns by its id."""
    from app.models import ApiToken, Feed

    mine = make_feed(client, write_token, name="Mine", query={"talkgroup": [1701]})
    other = _second_write_token(session)

    row = session.get(Feed, mine["id"])
    owner = session.scalar(select(ApiToken).where(ApiToken.label == "tests-write"))
    assert (row.user_id, row.token_id) == (None, owner.id)

    # Nobody else lists it — a read token least of all, since the URLs are credentials.
    assert client.get("/api/feeds", headers=bearer(read_token)).json() == []
    assert client.get("/api/feeds", headers=bearer(other)).json() == []
    # ...and nobody else can touch it: missing, not forbidden.
    assert client.patch(
        f"/api/feeds/{mine['id']}", json={"query": {}}, headers=bearer(other)
    ).status_code == 404
    assert client.post(f"/api/feeds/{mine['id']}/rotate", headers=bearer(other)).status_code == 404
    assert client.delete(f"/api/feeds/{mine['id']}", headers=bearer(other)).status_code == 404
    assert client.get(f"/api/feeds/{token_of(mine)}.rss").status_code == 200  # untouched

    # The owner still has it, and a second token's feeds are its own.
    theirs = make_feed(client, other, name="Theirs", query={})
    assert [f["id"] for f in client.get("/api/feeds", headers=bearer(write_token)).json()] == [mine["id"]]
    assert [f["id"] for f in client.get("/api/feeds", headers=bearer(other)).json()] == [theirs["id"]]
    assert client.patch(
        f"/api/feeds/{mine['id']}", json={"name": "Still mine"}, headers=bearer(write_token)
    ).status_code == 200


def test_an_admin_sees_which_token_owns_a_feed(client, archive, write_token, admin_token) -> None:  # noqa: ANN001
    feed = make_feed(client, write_token, name="Theirs", query={})
    listed = client.get("/api/feeds", headers=bearer(admin_token)).json()
    assert [(f["id"], f["owner"]) for f in listed] == [(feed["id"], "token:tests-write")]


def test_a_feed_with_no_owner_is_an_admins_alone(client, archive, session, write_token, admin_token) -> None:  # noqa: ANN001
    """What every token-made feed from before ``token_id`` is: nobody's but the admins'."""
    from app.api.feeds import new_token
    from app.models import Feed

    orphan = Feed(name="Orphan", token=new_token(), query={})
    session.add(orphan)
    session.commit()
    assert client.get("/api/feeds", headers=bearer(write_token)).json() == []
    assert client.patch(
        f"/api/feeds/{orphan.id}", json={"name": "Mine"}, headers=bearer(write_token)
    ).status_code == 404
    assert client.post(f"/api/feeds/{orphan.id}/rotate", headers=bearer(write_token)).status_code == 404
    listed = client.get("/api/feeds", headers=bearer(admin_token)).json()
    assert [(f["id"], f["owner"]) for f in listed] == [(orphan.id, None)]
    assert client.post(f"/api/feeds/{orphan.id}/rotate", headers=bearer(admin_token)).status_code == 200


def test_deleting_a_token_leaves_its_feeds_published_for_the_admins(client, archive, session, write_token) -> None:  # noqa: ANN001
    """``ON DELETE SET NULL``: subscribers hold the URL, so the feed outlives the token."""
    from app.models import ApiToken, Feed

    feed = make_feed(client, write_token, name="Kept", query={})
    session.delete(session.scalar(select(ApiToken).where(ApiToken.label == "tests-write")))
    session.commit()
    row = session.get(Feed, feed["id"])
    session.refresh(row)
    assert row.token_id is None
    assert client.get(f"/api/feeds/{token_of(feed)}.rss").status_code == 200


# ------------------------------------------------------------------------------ audio ---


def test_the_enclosure_appears_only_when_audio_is_asked_for(client, archive, write_token) -> None:  # noqa: ANN001
    quiet = make_feed(client, write_token, name="Quiet", query={"talkgroup": [1701]})
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(quiet)}.rss").text)
    assert root.find("channel").find("item").find("enclosure") is None

    loud = make_feed(
        client, write_token, name="Loud", query={"talkgroup": [1701]}, include_audio=True
    )
    item = ET.fromstring(client.get(f"/api/feeds/{token_of(loud)}.rss").text).find("channel/item")
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.get("type").startswith("audio/")
    assert f"feed={token_of(loud)}" in enclosure.get("url")

    atom_entry = ET.fromstring(client.get(f"/api/feeds/{token_of(loud)}.atom").text).find(f"{ATOM}entry")
    assert any(link.get("rel") == "enclosure" for link in atom_entry.findall(f"{ATOM}link"))


def test_the_audio_token_opens_only_the_calls_the_feed_matches(client, archive, write_token) -> None:  # noqa: ANN001
    """The whole security property of the enclosure: a feed over one talkgroup is not a
    key to the archive."""
    feed = make_feed(
        client, write_token, name="Event 6", query={"talkgroup": [1701]}, include_audio=True
    )
    token = token_of(feed)
    inside = archive.by_tgid[1701][0]
    outside = archive.by_tgid[1601][0]

    assert client.get(f"/api/calls/{inside}/audio", params={"feed": token}).status_code == 200
    assert client.get(f"/api/calls/{outside}/audio", params={"feed": token}).status_code == 404
    assert client.get(f"/api/calls/{inside}/audio", params={"feed": "wrong-token-entirely"}).status_code == 404

    # Disabling the feed closes the enclosure too.
    client.patch(f"/api/feeds/{feed['id']}", json={"enabled": False}, headers=bearer(write_token))
    assert client.get(f"/api/calls/{inside}/audio", params={"feed": token}).status_code == 404


def test_a_closed_archive_still_serves_a_feed_and_its_enclosure(client, archive, write_token, admin_token) -> None:  # noqa: ANN001
    """``AUTH_REQUIRE_READ`` shuts the archive to anybody without an account — but a feed
    reader has no account by construction, and the feed token is the credential it carries
    instead. The exemption is exactly the two reads that check the token themselves."""
    feed = make_feed(
        client, write_token, name="Closed", query={"talkgroup": [1701]}, include_audio=True
    )
    token = token_of(feed)
    inside, outside = archive.by_tgid[1701][0], archive.by_tgid[1601][0]
    client.put(
        "/api/admin/settings", json={"auth_require_read": "true"}, headers=bearer(admin_token)
    ).raise_for_status()
    try:
        assert client.get("/api/calls").status_code == 401  # the archive really is closed
        assert client.get(f"/api/feeds/{token}.rss").status_code == 200
        assert client.get(f"/api/feeds/{token}.atom").status_code == 200
        assert client.get(f"/api/calls/{inside}/audio", params={"feed": token}).status_code == 200
        # ...and the exemption widens nothing: no token, no audio; wrong call, no audio.
        assert client.get(f"/api/calls/{inside}/audio").status_code == 401
        assert client.get(f"/api/calls/{outside}/audio", params={"feed": token}).status_code == 404
        assert client.get("/api/feeds").status_code == 401  # management is not exempt
    finally:
        client.put(
            "/api/admin/settings", json={"auth_require_read": ""}, headers=bearer(admin_token)
        )


def test_every_way_of_being_refused_audio_looks_the_same(client, archive, write_token) -> None:  # noqa: ANN001
    """A wrong token, a disabled feed and a call outside the filters must be one answer.

    Otherwise the enclosure URL becomes an oracle: vary the token, read the detail, learn
    which guesses were real feeds.
    """
    feed = make_feed(
        client, write_token, name="Event 6", query={"talkgroup": [1701]}, include_audio=True
    )
    inside, outside = archive.by_tgid[1701][0], archive.by_tgid[1601][0]
    answers = [
        client.get(f"/api/calls/{outside}/audio", params={"feed": token_of(feed)}),
        client.get(f"/api/calls/{inside}/audio", params={"feed": "no-such-token"}),
        client.get(f"/api/calls/{inside}/audio", params={"feed": "x"}),
    ]
    assert [r.status_code for r in answers] == [404, 404, 404]
    assert {r.json()["detail"] for r in answers} == {f"no call {inside}", f"no call {outside}"}


def test_a_feed_without_audio_opens_no_recording(client, archive, write_token) -> None:  # noqa: ANN001
    """A transcript-only feed publishes no enclosure, so its token must not fetch audio
    either — ids are sequential, and the grant used to ignore ``include_audio``."""
    feed = make_feed(client, write_token, name="Words only", query={"talkgroup": [1701]})
    inside = archive.by_tgid[1701][0]
    for params in ({"feed": token_of(feed)}, {"feed": token_of(feed), "raw": 1}):
        refused = client.get(f"/api/calls/{inside}/audio", params=params)
        assert refused.status_code == 404
        assert refused.json()["detail"] == f"no call {inside}"  # the same answer as any refusal

    client.patch(f"/api/feeds/{feed['id']}", json={"include_audio": True}, headers=bearer(write_token))
    assert client.get(f"/api/calls/{inside}/audio", params={"feed": token_of(feed)}).status_code == 200


def test_the_audio_grant_waits_for_the_readiness_gate(client, archive, session, write_token) -> None:  # noqa: ANN001
    """The enclosure opens exactly the calls the document publishes: not one still being
    transcribed, and not one whose file the audio pass is about to rewrite."""
    from app.models import AudioJob, Call

    feed = make_feed(
        client, write_token, name="Event 6", query={"talkgroup": [1701]}, include_audio=True
    )
    call_id = archive.transcribed_id
    url = f"/api/calls/{call_id}/audio"
    params = {"feed": token_of(feed)}
    assert client.get(url, params=params).status_code == 200

    session.get(Call, call_id).transcript_status = "queued"
    session.commit()
    assert client.get(url, params=params).status_code == 404

    session.get(Call, call_id).transcript_status = "done"
    job = AudioJob(call_id=call_id, status="running")
    session.add(job)
    session.commit()
    assert client.get(url, params=params).status_code == 404

    job.status = "done"
    session.commit()
    assert client.get(url, params=params).status_code == 200


# ------------------------------------------------------------------- the readiness gate ---


def ids_in(client, url) -> list[int]:  # noqa: ANN001
    """The call ids a feed document is publishing."""
    root = ET.fromstring(client.get(url).text)
    return [int(i.findtext("guid").rsplit("/", 1)[-1]) for i in root.find("channel").findall("item")]


def test_a_call_still_being_transcribed_is_not_published(client, archive, session, write_token) -> None:  # noqa: ANN001
    """An item is permanent — the guid is the call's permalink, so nothing re-publishes it
    when the words land. A reader that fetched two minutes early would have kept
    *Transcript pending* for ever, so the feed waits instead."""
    from app.models import Call

    feed = make_feed(client, write_token, name="Event 6", query={"talkgroup": [1701]})
    url = f"/api/feeds/{token_of(feed)}.rss"
    assert archive.transcribed_id in ids_in(client, url)

    for status in IN_FLIGHT:
        session.get(Call, archive.transcribed_id).transcript_status = status
        session.commit()
        assert archive.transcribed_id not in ids_in(client, url), status


def test_every_terminal_transcript_status_is_published(client, archive, session, write_token) -> None:  # noqa: ANN001
    """``skipped`` and ``failed`` are finished records, not pending ones: the archive has
    decided there will be no text, and an item that says so is complete."""
    from app.models import Call

    feed = make_feed(client, write_token, name="Event 6", query={"talkgroup": [1701]})
    url = f"/api/feeds/{token_of(feed)}.rss"
    for status in ("done", "skipped", "failed", "empty"):
        session.get(Call, archive.transcribed_id).transcript_status = status
        session.commit()
        assert archive.transcribed_id in ids_in(client, url), status


def test_a_call_with_an_open_audio_job_is_not_published(client, archive, session, write_token) -> None:  # noqa: ANN001
    """The other half of "complete": the enclosure must not point at a file that is about
    to be rewritten by the audio pass."""
    from app.models import AudioJob

    feed = make_feed(client, write_token, name="Event 6", query={"talkgroup": [1701]})
    url = f"/api/feeds/{token_of(feed)}.rss"
    job = AudioJob(call_id=archive.transcribed_id, status="queued")
    session.add(job)
    session.commit()
    assert archive.transcribed_id not in ids_in(client, url)

    job.status = "running"
    session.commit()
    assert archive.transcribed_id not in ids_in(client, url)

    job.status = "done"
    session.commit()
    assert archive.transcribed_id in ids_in(client, url)


def test_a_stuck_call_publishes_once_the_wait_cap_has_passed(client, archive, session, write_token, monkeypatch) -> None:  # noqa: ANN001
    """The gate waits, but not for ever.  A stuck speech or audio worker
    used to hold back every item of every feed; past ``feed_ready_max_wait_s`` after
    ingest a call publishes whatever its status — the document *and* the enclosure."""
    from datetime import UTC, datetime, timedelta

    from app.config import get_settings
    from app.models import AudioJob, Call

    feed = make_feed(
        client, write_token, name="Event 6", query={"talkgroup": [1701]}, include_audio=True
    )
    url = f"/api/feeds/{token_of(feed)}.rss"
    audio = f"/api/calls/{archive.transcribed_id}/audio"
    call = session.get(Call, archive.transcribed_id)
    call.transcript_status = "running"
    session.add(AudioJob(call_id=call.id, status="running"))
    session.commit()
    assert archive.transcribed_id not in ids_in(client, url)

    # Fourteen minutes in: still waiting.
    call.ingested_at = datetime.now(UTC) - timedelta(minutes=14)
    session.commit()
    assert archive.transcribed_id not in ids_in(client, url)
    assert client.get(audio, params={"feed": token_of(feed)}).status_code == 404

    # Past the fifteen-minute default: published, both the item and its enclosure.
    call.ingested_at = datetime.now(UTC) - timedelta(minutes=16)
    session.commit()
    assert archive.transcribed_id in ids_in(client, url)
    assert client.get(audio, params={"feed": token_of(feed)}).status_code == 200

    # 0 turns the cap off: the gate waits as long as the queues do.
    monkeypatch.setattr(get_settings(), "feed_ready_max_wait_s", 0.0)
    assert archive.transcribed_id not in ids_in(client, url)


def test_a_flagged_hallucination_is_still_a_complete_record(client, archive, session, write_token) -> None:  # noqa: ANN001
    """The gate is about *unfinished*, not about *unwanted*: a rejected transcript is a
    finished call and keeps its place, with the body that names the rejection."""
    from app.models import Call

    call = session.get(Call, archive.transcribed_id)
    call.transcript_quality = "noise"
    session.commit()

    feed = make_feed(client, write_token, name="All", query={})
    root = ET.fromstring(client.get(f"/api/feeds/{token_of(feed)}.rss").text)
    body = next(
        item.findtext("description")
        for item in root.find("channel").findall("item")
        if item.findtext("guid").endswith(f"/{archive.transcribed_id}")
    )
    assert body.startswith("Rejected as hallucination")


# -------------------------------------------------------------------- the built-in feeds ---


def test_the_presets_are_all_calls_and_the_categories_in_use(client, archive) -> None:  # noqa: ANN001
    """One feed over everything, then one per category the directory actually files a
    talkgroup under — not the whole vocabulary, because a feed for a category nothing is
    in would publish nothing for ever."""
    presets = client.get("/api/feeds/presets").json()
    # The composite "emergency" feed sits between everything and the single categories;
    # in these fixtures only fire is in use, so it spans that one category.
    assert [p["slug"] for p in presets] == ["all", "emergency", "fire"]

    everything, emergency, fire = presets
    assert emergency["query"] == {"category": ["fire"]}
    assert emergency["talkgroups"] == 1
    assert emergency["rss_url"].endswith("/api/feeds/preset/emergency.rss")
    assert everything["name"] == "Keyup · All calls"
    assert everything["query"] == {}
    assert everything["talkgroups"] == 6  # every talkgroup the fixtures ingested
    assert fire == {
        "slug": "fire",
        "name": "Keyup · Fire",
        "description": fire["description"],
        "query": {"category": ["fire"]},
        "talkgroups": 1,
        # The fixture recordings are dated, and none of them landed this week.
        "calls_7d": 0,
        "top_talkgroups": [],
        "rss_url": fire["rss_url"],
        "atom_url": fire["atom_url"],
    }
    assert fire["rss_url"].endswith("/api/feeds/preset/fire.rss")
    assert fire["atom_url"].endswith("/api/feeds/preset/fire.atom")
    assert "fire" in fire["description"].lower()


def test_a_preset_publishes_its_category_and_nothing_else(client, archive) -> None:  # noqa: ANN001
    response = client.get("/api/feeds/preset/fire.rss")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert response.headers["cache-control"] == "max-age=60"

    channel = ET.fromstring(response.text).find("channel")
    assert channel.findtext("title") == "Keyup · Fire"
    assert channel.find(f"{ATOM}link").get("href").endswith("/api/feeds/preset/fire.rss")
    # EXAMPLE EVENT 1 is the one talkgroup the fixtures file under `fire`.
    assert ids_in(client, "/api/feeds/preset/fire.rss") == sorted(archive.by_tgid[1701], reverse=True)
    assert set(ids_in(client, "/api/feeds/preset/all.rss")) == set(archive.call_ids)


def test_a_preset_says_how_busy_it_is_and_which_channels_it_covers(client, archive, session) -> None:  # noqa: ANN001
    """The list is what the Feeds page describes a built-in feed from, so it carries the
    week's volume and the busiest channels in it — computed over the feed's *own* filter
    set, so the number means "what this feed publishes" and not a second definition of the
    category."""
    from app.models import Call

    session.execute(
        update(Call).where(Call.id.in_(archive.call_ids)).values(started_at=datetime.now(UTC))
    )
    session.commit()

    presets = {p["slug"]: p for p in client.get("/api/feeds/presets").json()}
    everything, fire = presets["all"], presets["fire"]

    assert everything["calls_7d"] == len(archive.call_ids)
    assert 0 < len(everything["top_talkgroups"]) <= 5
    counts = [tg["calls_7d"] for tg in everything["top_talkgroups"]]
    assert counts == sorted(counts, reverse=True)  # busiest first
    assert all(tg["tgid"] is not None for tg in everything["top_talkgroups"])

    # EXAMPLE EVENT 1 is the one talkgroup filed under `fire`, so the fire feed is its calls.
    assert fire["calls_7d"] == len(archive.by_tgid[1701])
    assert 1701 in [tg["tgid"] for tg in fire["top_talkgroups"]]
    named = next(tg for tg in fire["top_talkgroups"] if tg["tgid"] == 1701)
    assert named["alias"] == "EXAMPLE EVENT 1"
    assert named["calls_7d"] > 0


def test_a_call_older_than_the_window_is_not_in_the_weekly_figure(client, archive, session) -> None:  # noqa: ANN001
    from app.models import Call

    session.execute(
        update(Call)
        .where(Call.id.in_(archive.call_ids))
        .values(started_at=datetime.now(UTC) - timedelta(days=8))
    )
    session.commit()
    presets = {p["slug"]: p for p in client.get("/api/feeds/presets").json()}
    assert presets["all"]["calls_7d"] == 0
    assert presets["all"]["top_talkgroups"] == []


def test_the_preset_list_is_cached(client, archive, session) -> None:  # noqa: ANN001
    """One grouped query per preset is not free, and a week's volume does not move in ten
    minutes — so the whole answer is remembered under one key in the count cache."""
    from app.api.feeds import PRESETS_CACHE_KEY
    from app.models import Talkgroup
    from app.services import counts as counts_service

    first = client.get("/api/feeds/presets").json()
    assert [p["slug"] for p in first] == ["all", "emergency", "fire"]

    # File a second talkgroup under a new category: the live answer would grow a preset.
    other = session.scalars(
        select(Talkgroup).where(Talkgroup.category.is_(None)).order_by(Talkgroup.id)
    ).first()
    other.category = "ems"
    session.commit()

    assert client.get("/api/feeds/presets").json() == first  # still the cached answer

    counts_service.invalidate(PRESETS_CACHE_KEY)
    assert [p["slug"] for p in client.get("/api/feeds/presets").json()] == ["all", "emergency", "fire", "ems"]


def test_a_preset_renders_as_atom_too(client, archive) -> None:  # noqa: ANN001
    response = client.get("/api/feeds/preset/fire.atom")
    assert response.status_code == 200
    root = ET.fromstring(response.text)
    assert root.tag == f"{ATOM}feed"
    assert root.findtext(f"{ATOM}title") == "Keyup · Fire"
    assert root.findtext(f"{ATOM}id").endswith("/api/feeds/preset/fire.atom")
    rss = ET.fromstring(client.get("/api/feeds/preset/fire.rss").text)
    assert [e.findtext(f"{ATOM}id") for e in root.findall(f"{ATOM}entry")] == [
        i.findtext("guid") for i in rss.find("channel").findall("item")
    ]


def test_a_preset_holds_a_call_back_exactly_as_a_token_feed_does(client, archive, session) -> None:  # noqa: ANN001
    from app.models import Call

    session.get(Call, archive.transcribed_id).transcript_status = "running"
    session.commit()
    assert archive.transcribed_id not in ids_in(client, "/api/feeds/preset/fire.rss")


def test_a_preset_enclosure_is_a_plain_audio_url(client, archive) -> None:  # noqa: ANN001
    """There is no token to sign it with, and while the archive is open it needs none."""
    item = ET.fromstring(client.get("/api/feeds/preset/fire.rss").text).find("channel/item")
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert "feed=" not in enclosure.get("url")
    assert enclosure.get("url").endswith("/audio")
    # ...and it plays: the same route the SPA uses, with nothing appended to it.
    path = "/api" + enclosure.get("url").split("/api", 1)[1]
    assert client.get(path).status_code == 200


def test_an_unknown_preset_is_a_404(client, archive) -> None:  # noqa: ANN001
    assert client.get("/api/feeds/preset/nonesuch.rss").status_code == 404
    assert client.get("/api/feeds/preset/nonesuch.atom").status_code == 404
    # A category in the vocabulary that nothing in this directory is filed under is a 404
    # as well: there is no feed there to subscribe to.
    assert client.get("/api/feeds/preset/military.rss").status_code == 404


def test_a_preset_is_not_a_way_past_a_closed_archive(client, archive, admin_token) -> None:  # noqa: ANN001
    """A built-in feed holds no secret, so it cannot be its own credential: when reads are
    closed it needs an account like every other read, and it drops its enclosures rather
    than publishing audio URLs that would answer 401 in a podcast app."""
    client.put(
        "/api/admin/settings", json={"auth_require_read": "true"}, headers=bearer(admin_token)
    ).raise_for_status()
    try:
        assert client.get("/api/feeds/preset/fire.rss").status_code == 401
        assert client.get("/api/feeds/preset/fire.atom").status_code == 401
        assert client.get("/api/feeds/presets").status_code == 401

        signed_in = client.get("/api/feeds/preset/fire.rss", headers=bearer(admin_token))
        assert signed_in.status_code == 200
        item = ET.fromstring(signed_in.text).find("channel/item")
        assert item.find("enclosure") is None
        assert item.findtext("title")  # ...but the items themselves are all still there
    finally:
        client.put(
            "/api/admin/settings", json={"auth_require_read": ""}, headers=bearer(admin_token)
        )
