"""Talkgroups, radios, systems, patches, conversations and agencies (§4.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_talkgroups_list_and_search(client, archive) -> None:
    body = client.get("/api/talkgroups").json()
    assert body["total"] >= 4
    assert client.get("/api/talkgroups").headers["X-Total-Count"] == str(body["total"])

    hit = client.get("/api/talkgroups", params={"q": "1701"}).json()
    assert hit["total"] == 1
    assert hit["items"][0]["tgid"] == 1701

    by_alias = client.get("/api/talkgroups", params={"q": "EVENT"}).json()
    assert by_alias["total"] >= 1


def test_talkgroup_detail_and_stats(client, archive) -> None:
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    detail = client.get(f"/api/talkgroups/{talkgroup['id']}").json()
    assert detail["system_name"] == "Example System"
    assert detail["audio_seconds"] > 0
    assert detail["radio_count"] >= 1

    stats = client.get(f"/api/talkgroups/{talkgroup['id']}/stats").json()
    assert stats["call_count"] == 3
    assert len(stats["hourly"]) == 24
    assert len(stats["spark"]) == 24
    assert sum(h["count"] for h in stats["hourly"]) == stats["call_count"]


def test_patching_a_talkgroup_changes_what_the_filters_see(client, archive, write_token) -> None:
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    response = client.patch(
        f"/api/talkgroups/{talkgroup['id']}",
        json={"category": "law", "agency_code": "EXAMPLEPD", "priority": 5, "transcribe": False},
        headers=bearer(write_token),
    )
    assert response.status_code == 200
    assert response.json()["category"] == "law"
    assert client.get("/api/calls", params={"agency": "EXAMPLEPD"}).json()["total"] == 3


def test_a_talkgroup_priority_has_to_fit_the_queue_it_feeds(client, archive, write_token) -> None:
    """A talkgroup's priority is copied onto its live transcription jobs, and its backlog
    re-runs the same number ``BACKLOG_OFFSET`` (500) higher in a queue that stops at 999.
    So 499 is the ceiling: accepting 900 here would hand that channel a backlog priority
    off the end of the scale. 0 is refused from the other end, because 1 is worked first."""
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]

    for bad in (0, -1, 500, 999):
        response = client.patch(
            f"/api/talkgroups/{talkgroup['id']}",
            json={"priority": bad},
            headers=bearer(write_token),
        )
        assert response.status_code == 422, bad

    for good in (1, 100, 499):
        response = client.patch(
            f"/api/talkgroups/{talkgroup['id']}",
            json={"priority": good},
            headers=bearer(write_token),
        )
        assert response.status_code == 200, good
        assert response.json()["priority"] == good


def test_a_service_tag_is_stored_in_radioreferences_own_spelling(client, archive, write_token) -> None:
    """The tag is RR's classification, so the API answers with RR's casing, not the caller's."""
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    response = client.patch(
        f"/api/talkgroups/{talkgroup['id']}",
        json={"service_tag": "law dispatch"},
        headers=bearer(write_token),
    )
    assert response.status_code == 200
    assert response.json()["service_tag"] == "Law Dispatch"

    detail = client.get(f"/api/talkgroups/{talkgroup['id']}").json()
    assert detail["service_tag"] == "Law Dispatch"

    cleared = client.patch(
        f"/api/talkgroups/{talkgroup['id']}", json={"service_tag": None}, headers=bearer(write_token)
    )
    assert cleared.json()["service_tag"] is None


def test_a_service_tag_radioreference_does_not_publish_is_refused(client, archive, write_token) -> None:
    """``category`` is this archive's free text; ``service_tag`` claims to be RR's, so it
    may only hold something RR would recognize."""
    talkgroup = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    response = client.patch(
        f"/api/talkgroups/{talkgroup['id']}",
        json={"service_tag": "Weather Net"},
        headers=bearer(write_token),
    )
    assert response.status_code == 422


def test_an_unknown_agency_code_is_refused(client, archive, write_token) -> None:
    talkgroup = client.get("/api/talkgroups").json()["items"][0]
    response = client.patch(
        f"/api/talkgroups/{talkgroup['id']}",
        json={"agency_code": "NOPE"},
        headers=bearer(write_token),
    )
    assert response.status_code == 422
    assert "NOPE" in response.json()["detail"]


def test_creating_a_talkgroup_that_has_never_been_heard(client, archive, write_token) -> None:
    created = client.post(
        "/api/talkgroups",
        json={"tgid": 99999, "alias": "TEST CHANNEL", "category": "interop"},
        headers=bearer(write_token),
    )
    assert created.status_code == 201
    assert created.json()["call_count"] == 0
    again = client.post("/api/talkgroups", json={"tgid": 99999}, headers=bearer(write_token))
    assert again.status_code == 409


def test_talkgroup_csv_export_round_trips_into_the_importer(client, archive, write_token) -> None:
    csv_text = client.get("/api/talkgroups/export").text
    assert csv_text.splitlines()[0].startswith("tgid,alias,group_name,category,service_tag")
    assert "1701" in csv_text

    edited = csv_text.replace("EXAMPLE EVENT 1", "RENAMED CHANNEL")
    result = client.post(
        "/api/talkgroups/import",
        json={"content": edited, "format": "csv"},
        headers=bearer(write_token),
    )
    assert result.status_code == 200
    assert result.json()["updated"] >= 1
    assert client.get("/api/talkgroups", params={"q": "RENAMED"}).json()["total"] == 1


def test_playlist_xml_import_names_channels(client, archive, write_token) -> None:
    xml = """<?xml version="1.0"?>
    <playlist>
      <alias name="EC EVENT SIX" group="Example County" color="-65536">
        <id xsi:type="talkgroup" value="1701" protocol="APCO25"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"/>
      </alias>
    </playlist>"""
    result = client.post(
        "/api/talkgroups/import", json={"content": xml, "format": "xml"}, headers=bearer(write_token)
    )
    assert result.status_code == 200
    renamed = client.get("/api/talkgroups", params={"q": "1701"}).json()["items"][0]
    assert renamed["alias"] == "EC EVENT SIX"
    assert renamed["group_name"] == "Example County"


def test_import_as_a_multipart_upload(client, archive, write_token, tmp_path) -> None:
    path = tmp_path / "aliases.csv"
    path.write_text("tgid,alias\n1701,FROM A FILE\n")
    with path.open("rb") as handle:
        result = client.post(
            "/api/talkgroups/import",
            files={"file": ("aliases.csv", handle, "text/csv")},
            headers=bearer(write_token),
        )
    assert result.status_code == 200
    assert client.get("/api/talkgroups", params={"q": "FROM A FILE"}).json()["total"] == 1


def test_radios_list_detail_affiliations_and_history(client, archive) -> None:
    radios = client.get("/api/radios").json()
    assert radios["total"] >= 4

    with_unit = client.get("/api/radios", params={"has_unit": True}).json()
    assert with_unit["total"] == 1
    radio = with_unit["items"][0]
    assert radio["unit_id"] == "E1"

    detail = client.get(f"/api/radios/{radio['id']}").json()
    assert detail["agency_name"] == "CENTRAL CITY FIRE DEPARTMENT"
    assert detail["talkgroup_count"] >= 1

    affiliations = client.get(f"/api/radios/{radio['id']}/affiliations").json()
    assert affiliations
    assert affiliations[0]["unit_id"] == "E1"
    assert affiliations[0]["call_count"] >= 1

    history = client.get(f"/api/radios/{radio['id']}/history").json()
    assert history["call_count"] >= 1
    assert len(history["hourly"]) == 24
    assert history["talkgroups"]


def test_radio_search_matches_the_on_air_number(client, archive) -> None:
    radio = client.get("/api/radios").json()["items"][0]
    found = client.get("/api/radios", params={"q": str(radio["radio_id"])}).json()
    assert radio["id"] in [r["id"] for r in found["items"]]


def test_radios_include_silent_seen_but_never_transmitted(client, archive, session) -> None:
    """A radio seen on the control channel (last_seen_at set, call_count == 0) but which
    never keyed up must still appear by default, be reachable via ``activity=seen``, and be
    excluded by ``activity=transmitted`` (docs/RADIO_EVENTS.md)."""
    from app.models import Radio  # noqa: PLC0415

    system_id = session.scalars(select(Radio.system_id)).first()
    silent = Radio(
        system_id=system_id,
        radio_id=9_999_001,
        call_count=0,
        last_seen_at=datetime.now(UTC),
        last_registered_at=datetime.now(UTC),
    )
    session.add(silent)
    session.commit()

    all_radios = client.get("/api/radios").json()
    ids = [r["id"] for r in all_radios["items"]]
    assert silent.id in ids, "a silent, seen radio must show with default params"

    seen = client.get("/api/radios", params={"activity": "seen"}).json()
    seen_ids = [r["id"] for r in seen["items"]]
    assert silent.id in seen_ids
    assert all(r["call_count"] == 0 for r in seen["items"])

    transmitted = client.get("/api/radios", params={"activity": "transmitted"}).json()
    transmitted_ids = [r["id"] for r in transmitted["items"]]
    assert silent.id not in transmitted_ids
    assert all(r["call_count"] > 0 for r in transmitted["items"])

    # The three totals partition the corpus.
    assert seen["total"] + transmitted["total"] == all_radios["total"]


def test_systems_sites_and_patches(client, archive) -> None:
    systems = client.get("/api/systems").json()
    assert len(systems) == 1
    assert systems[0]["name"] == "Example System"
    assert systems[0]["call_count"] == len(archive.call_ids)
    assert systems[0]["site_count"] == 1

    sites = client.get("/api/sites").json()
    assert sites[0]["name"] == "Central"

    patches = client.get("/api/patches").json()
    assert patches["total"] == 1
    patch = patches["items"][0]
    assert len(patch["member_tgids"]) == 3
    assert len(patch["member_aliases"]) == 3


def test_conversations_group_consecutive_calls(client, archive) -> None:
    conversations = client.get("/api/conversations").json()
    assert conversations["total"] >= 1
    thread = conversations["items"][0]
    assert thread["call_count"] >= 1
    assert thread["duration_s"] >= 0

    detail = client.get(f"/api/conversations/{thread['id']}").json()
    assert detail["conversation"]["id"] == thread["id"]
    assert len(detail["calls"]) == thread["call_count"]
    starts = [c["started_at"] for c in detail["calls"]]
    assert starts == sorted(starts)  # oldest first, so it reads as a thread


def test_agencies_carry_their_talkgroups(client, archive) -> None:
    agencies = client.get("/api/agencies").json()
    assert len(agencies) == 3  # the example agency list, seeded
    fire = next(a for a in agencies if a["code"] == "EXAMPLEFD")
    assert fire["short_name"] == "CCFD"
    assert 1701 in fire["talkgroup_ids"]
    assert fire["radio_count"] == 1


def test_agencies_report_their_provenance(client, archive) -> None:
    """The public list carries `source`/`active`, so a picker can hide a retired agency."""
    agencies = client.get("/api/agencies").json()
    assert {a["source"] for a in agencies} == {"seed"}  # everything here came from the seed
    assert all(a["active"] for a in agencies)


def test_vocabulary_is_static_and_self_consistent(client) -> None:
    """The Help page and docs/TALKGROUPS.md both render this; no database is touched."""
    body = client.get("/api/directory/vocabulary").json()

    keys = [c["key"] for c in body["categories"]]
    assert keys[:3] == ["law", "fire", "ems"]
    assert "other" in keys
    assert all(c["description"] for c in body["categories"])

    tags = {t["tag"]: t for t in body["service_tags"]}
    assert tags["Fire Dispatch"]["category"] == "fire"
    assert tags["Hospital"]["category"] == "ems"  # the receiving end of the same call
    assert tags["Emergency Ops"]["category"] == "multi"
    # Every tag's category is one the categories list names, or the UI could not draw it.
    assert {t["category"] for t in tags.values()} <= set(keys)

    # The categories carry the reverse mapping, so either table reads on its own.
    by_key = {c["key"]: c["service_tags"] for c in body["categories"]}
    assert "Fire-Tac" in by_key["fire"]

    assert {k["key"] for k in body["agency_kinds"]} == {
        "dispatch",
        "police",
        "sheriff",
        "university",
        "fire",
        "ems",
    }
    assert {s["key"] for s in body["agency_sources"]} == {"seed", "local"}
    assert "retention" in body["category_effect"]


def test_conversations_filter_on_the_on_air_tgid_not_the_row_id(client, session) -> None:  # noqa: ANN001
    """``?talkgroup=`` is the tgid, as on ``GET /calls``.  The two number spaces collide —
    here talkgroup row 7 carries tgid 1701 while another channel's tgid *is* 7 — and the
    row-id reading returned the wrong channel's threads."""
    from app.models import Conversation, System, Talkgroup

    example = System(name="Example System")
    central = System(name="Example County")
    session.add_all([example, central])
    session.flush()
    row_seven = Talkgroup(id=7, system_id=example.id, tgid=1701, alias="CCFD Dispatch")
    tgid_seven = Talkgroup(id=9, system_id=example.id, tgid=7, alias="Seven on Example")
    tgid_seven_elsewhere = Talkgroup(id=11, system_id=central.id, tgid=7, alias="Seven on Central")
    session.add_all([row_seven, tgid_seven, tgid_seven_elsewhere])
    session.flush()
    start = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    threads = {}
    for tg in (row_seven, tgid_seven, tgid_seven_elsewhere):
        row = Conversation(talkgroup_id=tg.id, started_at=start, ended_at=start, call_count=2)
        session.add(row)
        session.flush()
        threads[tg.id] = row.id
    session.commit()

    def ids(**params) -> set[int]:  # noqa: ANN003
        response = client.get("/api/conversations", params=params)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    assert ids(talkgroup=7) == {threads[9], threads[11]}
    assert ids(talkgroup=1701) == {threads[7]}
    assert ids(talkgroup=7, system="Example System") == {threads[9]}
    assert ids(talkgroup=7, system=str(central.id)) == {threads[11]}
    assert ids(system="Example County") == {threads[11]}
    assert ids(talkgroup=7, system="No Such System") == set()
    assert ids(talkgroup=12345) == set()
    assert len(ids()) == 3
    assert client.get("/api/conversations", params={"talkgroup": 2**31}).status_code == 422
