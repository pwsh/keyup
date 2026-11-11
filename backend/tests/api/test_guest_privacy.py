"""What a guest may not read while the archive is open to guests.

A radio's roster link names a person or a unit, and the roster itself is closed to guests.
A hidden talkgroup is one the operator took off the public site. Both are enforced in the
API: a guest can call any of these routes without the UI's help.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.filters import CallFilters, filters_from_mapping
from app.models import Call, Conversation, Person, RadioAssignment, Talkgroup, Transcript, Unit
from tests.api.conftest import Archive

HIDDEN_TGID = 1601
PUBLIC_TGID = 1701


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def hide(session: Session, *tgids: int) -> None:
    for tgid in tgids:
        talkgroup = session.query(Talkgroup).filter(Talkgroup.tgid == tgid).one()
        talkgroup.is_hidden = True
    session.commit()


def hidden_call(session: Session, archive: Archive) -> int:
    hide(session, HIDDEN_TGID)
    return archive.by_tgid[HIDDEN_TGID][0]


def listed_ids(body: dict) -> set[int]:
    return {row["id"] for row in body["items"]}


# ---- the roster link on a radio ------------------------------------------------------


def linked_radio(session: Session, archive: Archive) -> int:
    radio_pk = session.get(Call, archive.transcribed_id).radio_id
    person = Person(name="J. Smith")
    unit = Unit(callsign="E1")
    session.add_all([person, unit])
    session.flush()
    session.add_all(
        [
            RadioAssignment(radio_pk=radio_pk, person_id=person.id),
            RadioAssignment(radio_pk=radio_pk, unit_id=unit.id),
        ]
    )
    session.commit()
    return radio_pk


def test_guest_radio_list_omits_roster_identity(client: TestClient, session: Session, archive: Archive) -> None:
    radio_pk = linked_radio(session, archive)
    row = next(r for r in client.get("/api/radios").json()["items"] if r["id"] == radio_pk)
    assert row["current_person"] is None
    assert row["current_unit"] is None


def test_guest_radio_detail_omits_roster_identity(client: TestClient, session: Session, archive: Archive) -> None:
    radio_pk = linked_radio(session, archive)
    body = client.get(f"/api/radios/{radio_pk}").json()
    assert body["current_person"] is None
    assert body["current_unit"] is None


def test_signed_in_radio_keeps_roster_identity(
    client: TestClient, session: Session, archive: Archive, read_token: str
) -> None:
    radio_pk = linked_radio(session, archive)
    body = client.get(f"/api/radios/{radio_pk}", headers=bearer(read_token)).json()
    assert body["current_person"]["name"] == "J. Smith"
    assert body["current_unit"]["callsign"] == "E1"


# ---- hidden talkgroups: lists ---------------------------------------------------------


def test_guest_call_list_omits_hidden_talkgroup(client: TestClient, session: Session, archive: Archive) -> None:
    call_id = hidden_call(session, archive)
    ids = listed_ids(client.get("/api/calls").json())
    assert call_id not in ids
    assert set(archive.by_tgid[PUBLIC_TGID]) <= ids


def test_signed_in_call_list_keeps_hidden_talkgroup(
    client: TestClient, session: Session, archive: Archive, read_token: str
) -> None:
    call_id = hidden_call(session, archive)
    assert call_id in listed_ids(client.get("/api/calls", headers=bearer(read_token)).json())


def test_guest_search_omits_hidden_talkgroup(client: TestClient, session: Session, archive: Archive) -> None:
    target = session.get(Call, archive.transcribed_id)
    talkgroup = session.get(Talkgroup, target.talkgroup_id)
    talkgroup.is_hidden = True
    session.commit()
    words = session.query(Transcript).filter(Transcript.call_id == target.id).one().text_.split()[0]
    body = client.get("/api/search", params={"q": words}).json()
    assert target.id not in {hit["call"]["id"] for hit in body["items"]}


def test_guest_call_list_omits_patch_with_hidden_member(
    client: TestClient, session: Session, archive: Archive
) -> None:
    member = session.get(Call, archive.patch_call_id).talkgroup_ids[-1]
    primary = session.get(Call, archive.patch_call_id).talkgroup_id
    candidates = session.query(Talkgroup).filter(Talkgroup.tgid == member, Talkgroup.id != primary).all()
    if candidates:
        candidates[0].is_hidden = True
    else:
        session.add(Talkgroup(system_id=session.get(Call, archive.patch_call_id).system_id, tgid=member, is_hidden=True))
    session.commit()
    assert archive.patch_call_id not in listed_ids(client.get("/api/calls").json())


def test_guest_with_nothing_hidden_gets_the_unfiltered_query(
    client: TestClient, session: Session, archive: Archive  # noqa: ARG001 - seeds the archive
) -> None:
    from app.api.filters import guest_aware_filters

    assert guest_aware_filters(CallFilters(), None, session).guest_view is False


def test_guest_flag_cannot_be_set_from_a_filter_frame() -> None:
    assert filters_from_mapping({"guest_view": True}).guest_view is False


def test_guest_flag_separates_cache_keys() -> None:
    assert CallFilters().echo() != CallFilters(guest_view=True).echo()
    assert "guest_view" not in CallFilters().echo()


def test_guest_flag_survives_replace() -> None:
    assert CallFilters(guest_view=True).replace(q="x").guest_view is True


# ---- hidden talkgroups: one call ------------------------------------------------------


def test_guest_call_detail_is_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    assert client.get(f"/api/calls/{hidden_call(session, archive)}").status_code == 404


def test_guest_audio_is_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    assert client.get(f"/api/calls/{hidden_call(session, archive)}/audio").status_code == 404


def test_guest_waveform_is_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    assert client.get(f"/api/calls/{hidden_call(session, archive)}/waveform").status_code == 404


def test_guest_transcripts_are_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    assert client.get(f"/api/calls/{hidden_call(session, archive)}/transcripts").status_code == 404


def test_signed_in_audio_still_plays_hidden(
    client: TestClient, session: Session, archive: Archive, read_token: str
) -> None:
    call_id = hidden_call(session, archive)
    assert client.get(f"/api/calls/{call_id}/audio", headers=bearer(read_token)).status_code == 200


def test_guest_public_call_still_plays(client: TestClient, session: Session, archive: Archive) -> None:
    hide(session, HIDDEN_TGID)
    assert client.get(f"/api/calls/{archive.by_tgid[PUBLIC_TGID][0]}/audio").status_code == 200


# ---- hidden talkgroups: the directory and threads -------------------------------------


def hidden_talkgroup(session: Session) -> int:
    hide(session, HIDDEN_TGID)
    return session.query(Talkgroup).filter(Talkgroup.tgid == HIDDEN_TGID).one().id


def test_guest_cannot_list_hidden_talkgroups(client: TestClient, session: Session, archive: Archive) -> None:  # noqa: ARG001
    talkgroup_id = hidden_talkgroup(session)
    body = client.get("/api/talkgroups", params={"include_hidden": "true"}).json()
    assert talkgroup_id not in {row["id"] for row in body["items"]}


def test_signed_in_can_list_hidden_talkgroups(
    client: TestClient, session: Session, archive: Archive, read_token: str  # noqa: ARG001
) -> None:
    talkgroup_id = hidden_talkgroup(session)
    body = client.get("/api/talkgroups", params={"include_hidden": "true"}, headers=bearer(read_token)).json()
    assert talkgroup_id in {row["id"] for row in body["items"]}


def test_guest_talkgroup_detail_is_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:  # noqa: ARG001
    assert client.get(f"/api/talkgroups/{hidden_talkgroup(session)}").status_code == 404


def test_guest_talkgroup_stats_are_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:  # noqa: ARG001
    assert client.get(f"/api/talkgroups/{hidden_talkgroup(session)}/stats").status_code == 404


def test_guest_talkgroup_export_omits_hidden(client: TestClient, session: Session, archive: Archive) -> None:  # noqa: ARG001
    talkgroup_id = hidden_talkgroup(session)
    alias = "Secret channel"
    session.get(Talkgroup, talkgroup_id).alias = alias
    session.commit()
    assert alias not in client.get("/api/talkgroups/export").text


def conversation_of_hidden(session: Session, archive: Archive) -> int:
    call_id = hidden_call(session, archive)
    call = session.get(Call, call_id)
    if call.conversation_id is None:
        row = Conversation(
            talkgroup_id=call.talkgroup_id, started_at=call.started_at, ended_at=call.started_at, call_count=1
        )
        session.add(row)
        session.flush()
        call.conversation_id = row.id
        session.commit()
    return call.conversation_id


def test_guest_conversation_is_404_for_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    assert client.get(f"/api/conversations/{conversation_of_hidden(session, archive)}").status_code == 404


def test_guest_conversation_list_omits_hidden(client: TestClient, session: Session, archive: Archive) -> None:
    conversation_id = conversation_of_hidden(session, archive)
    assert conversation_id not in listed_ids(client.get("/api/conversations").json())


# ---- hidden talkgroups: the live socket -----------------------------------------------


def test_guest_live_frame_withholds_hidden_call(session: Session, archive: Archive) -> None:
    from app.api.live import _guest_filters, _render

    call_id = hidden_call(session, archive)
    event = {"event": "call.new", "id": call_id, "seq": 1}
    assert _render(event, _guest_filters(CallFilters(), is_guest=True)) is None
    assert _render({**event, "seq": 2}, _guest_filters(CallFilters(), is_guest=False)) is not None


def test_guest_cannot_read_a_radios_assignment_history(client: TestClient, session: Session, archive: Archive) -> None:
    radio_pk = linked_radio(session, archive)
    response = client.get(f"/api/radios/{radio_pk}/assignments")
    assert response.status_code == 200
    assert response.json() == []


def test_signed_in_can_read_a_radios_assignment_history(
    client: TestClient, session: Session, archive: Archive, read_token: str
) -> None:
    radio_pk = linked_radio(session, archive)
    assert len(client.get(f"/api/radios/{radio_pk}/assignments", headers=bearer(read_token)).json()) == 2
