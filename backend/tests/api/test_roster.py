"""Units, personnel and radio assignments (§3.1, §4.2).

The interesting behavior is not the CRUD — it is the two invariants around it: the
database refusing an assignment row that means two things at once or a radio with two
current holders, and ``radios.unit_id`` staying in step with the current unit assignment
without ever overruling something an operator typed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import EXAMPLE_AGENCIES, requires_db

pytestmark = requires_db


def a_radio(session):  # noqa: ANN001, ANN201
    """The radio with the most traffic in the fixture archive."""
    from app.models import Radio

    return session.scalars(select(Radio).order_by(Radio.call_count.desc())).first()


def make_unit(client, token, **kw) -> dict:  # noqa: ANN001, ANN003
    body = {"callsign": "E1", "name": "Engine 1", "kind": "vehicle", **kw}
    response = client.post("/api/units", json=body, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


def make_person(client, token, **kw) -> dict:  # noqa: ANN001, ANN003
    body = {"name": "J. Smith", "rank": "Captain", **kw}
    response = client.post("/api/personnel", json=body, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------------------
# the constraints the application cannot enforce for itself
# --------------------------------------------------------------------------------------


def test_an_assignment_must_mean_exactly_one_thing(session, archive) -> None:  # noqa: ANN001, ARG001
    """The CHECK: a row assigns a unit or a person, never both and never neither."""
    from app.models import Person, RadioAssignment, Unit

    radio = a_radio(session)
    unit = Unit(callsign="E1")
    person = Person(name="J. Smith")
    session.add_all([unit, person])
    session.flush()

    session.add(RadioAssignment(radio_pk=radio.id, unit_id=unit.id, person_id=person.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    session.add(RadioAssignment(radio_pk=a_radio(session).id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_a_radio_has_at_most_one_current_assignment_of_each_kind(session, archive) -> None:  # noqa: ANN001, ARG001
    """The partial unique indexes — the race two operators clicking 'assign' would win."""
    from app.models import Person, RadioAssignment, Unit

    radio = a_radio(session)
    engine, ladder = Unit(callsign="E1"), Unit(callsign="L1")
    smith = Person(name="J. Smith")
    session.add_all([engine, ladder, smith])
    session.flush()

    session.add(RadioAssignment(radio_pk=radio.id, unit_id=engine.id))
    session.flush()
    # A *person* alongside the unit is fine: they are independent facts.
    session.add(RadioAssignment(radio_pk=radio.id, person_id=smith.id))
    session.flush()

    session.add(RadioAssignment(radio_pk=radio.id, unit_id=ladder.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_a_closed_assignment_leaves_room_for_the_next_one(session, archive) -> None:  # noqa: ANN001, ARG001
    """The uniqueness is over *current* rows, so history stacks up freely."""
    from app.models import RadioAssignment, Unit

    radio = a_radio(session)
    engine, ladder = Unit(callsign="E1"), Unit(callsign="L1")
    session.add_all([engine, ladder])
    session.flush()
    now = datetime.now(UTC)
    session.add(
        RadioAssignment(
            radio_pk=radio.id,
            unit_id=engine.id,
            effective_from=now - timedelta(days=2),
            effective_to=now - timedelta(days=1),
        )
    )
    session.add(RadioAssignment(radio_pk=radio.id, unit_id=ladder.id, effective_from=now))
    session.flush()

    assert session.scalar(select(RadioAssignment).where(RadioAssignment.effective_to.is_(None)))


def test_the_same_callsign_may_exist_once_per_agency_and_once_without_one(session, archive) -> None:  # noqa: ANN001, ARG001
    from app.models import Unit
    from app.seed import seed_agencies

    seed_agencies(session, path=EXAMPLE_AGENCIES)
    session.add_all(
        [
            Unit(callsign="E1", agency_code="EXAMPLEFD"),
            Unit(callsign="E1", agency_code="EXAMPLEPD"),
            Unit(callsign="E1"),
        ]
    )
    session.flush()

    session.add(Unit(callsign="E1"))
    with pytest.raises(IntegrityError):  # the partial index over the NULL-agency namespace
        session.flush()
    session.rollback()


# --------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------


def test_units_are_created_listed_searched_and_edited(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token)
    make_unit(client, write_token, callsign="A21", name="Ambulance 21")

    listed = client.get("/api/units", headers=bearer(write_token)).json()
    assert listed["total"] == 2
    assert client.get("/api/units", headers=bearer(write_token)).headers["X-Total-Count"] == "2"
    assert [u["callsign"] for u in listed["items"]] == ["A21", "E1"]

    assert client.get("/api/units", params={"q": "engine"}, headers=bearer(write_token)).json()["total"] == 1
    assert client.get("/api/units", params={"kind": "vehicle"}, headers=bearer(write_token)).json()["total"] == 2
    assert client.get("/api/units", params={"sort": "-callsign"}, headers=bearer(write_token)).json()["items"][0]["callsign"] == "E1"
    assert client.get("/api/units", params={"sort": "nope"}, headers=bearer(write_token)).status_code == 422

    again = client.post("/api/units", json={"callsign": "E1"}, headers=bearer(write_token))
    assert again.status_code == 409

    patched = client.patch(
        f"/api/units/{unit['id']}", json={"name": "Engine One"}, headers=bearer(write_token)
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Engine One"
    assert client.get(f"/api/units/{unit['id']}", headers=bearer(write_token)).json()["agency_name"] is None
    assert client.get("/api/units/999999", headers=bearer(write_token)).status_code == 404


def test_a_unit_refuses_an_agency_code_that_is_not_in_the_table(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    response = client.post(
        "/api/units", json={"callsign": "E1", "agency_code": "NOPE"}, headers=bearer(write_token)
    )
    assert response.status_code == 422
    assert "NOPE" in response.json()["detail"]


def test_personnel_are_created_listed_and_deactivated(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    person = make_person(client, write_token)
    make_person(client, write_token, name="R. Jones", rank="Firefighter")

    assert client.get("/api/personnel", headers=bearer(write_token)).json()["total"] == 2
    assert client.get("/api/personnel", params={"q": "captain"}, headers=bearer(write_token)).json()["total"] == 1

    client.patch(f"/api/personnel/{person['id']}", json={"active": False}, headers=bearer(write_token))
    assert client.get("/api/personnel", params={"active": True}, headers=bearer(write_token)).json()["total"] == 1
    # The row stays: a retired name is still the right answer about last year's traffic.
    assert client.get(f"/api/personnel/{person['id']}", headers=bearer(write_token)).json()["active"] is False


def test_writing_the_roster_needs_a_credential(client, archive) -> None:  # noqa: ANN001, ARG001
    """CSRF proof but no credential is a 401; no proof at all is the 403 above it."""
    assert client.post("/api/units", json={"callsign": "E1"}, headers=CSRF_HEADERS).status_code == 401
    assert client.post("/api/units", json={"callsign": "E1"}).status_code == 403
    assert (
        client.post("/api/personnel", json={"name": "J. Smith"}, headers=CSRF_HEADERS).status_code == 401
    )


# --------------------------------------------------------------------------------------
# assignments and the denormalized label
# --------------------------------------------------------------------------------------


def test_assigning_a_unit_writes_the_callsign_onto_the_radio(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7", name="Engine 7")
    radio = a_radio(session)

    created = client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"unit_id": unit["id"], "note": "swapped in"},
        headers=bearer(write_token),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "unit"
    assert body["current"] is True
    assert body["unit"]["callsign"] == "E7"

    detail = client.get(f"/api/radios/{radio.id}", headers=bearer(write_token)).json()
    assert detail["unit_id"] == "E7"  # the label the call lists render
    assert detail["current_unit"]["name"] == "Engine 7"
    assert detail["current_person"] is None
    assert client.get(f"/api/units/{unit['id']}/radios", headers=bearer(write_token)).json()[0]["radio_pk"] == radio.id


def test_reassigning_closes_the_old_row_and_replaces_the_label(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    engine = make_unit(client, write_token, callsign="E7")
    ladder = make_unit(client, write_token, callsign="L3")
    radio = a_radio(session)

    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": engine["id"]}, headers=bearer(write_token)
    )
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": ladder["id"]}, headers=bearer(write_token)
    )

    history = client.get(f"/api/radios/{radio.id}/assignments", headers=bearer(write_token)).json()
    assert [h["unit"]["callsign"] for h in history] == ["L3", "E7"]  # current first
    assert history[0]["current"] is True
    assert history[1]["current"] is False
    assert history[1]["effective_to"] is not None  # closed, not deleted
    assert client.get(f"/api/radios/{radio.id}").json()["unit_id"] == "L3"


def test_ending_a_unit_assignment_clears_the_label(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )

    ended = client.delete(
        f"/api/radios/{radio.id}/assignments/current",
        params={"kind": "unit"},
        headers=bearer(write_token),
    )
    assert ended.status_code == 200
    assert ended.json()["current"] is False
    assert client.get(f"/api/radios/{radio.id}").json()["unit_id"] is None

    # Nothing left to end.
    again = client.delete(
        f"/api/radios/{radio.id}/assignments/current",
        params={"kind": "unit"},
        headers=bearer(write_token),
    )
    assert again.status_code == 404


def test_ending_an_assignment_never_overrules_an_operators_own_label(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """The label is cleared only when it is still the one the assignment put there."""
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )
    client.patch(f"/api/radios/{radio.id}", json={"unit_id": "E7-SPARE"}, headers=bearer(write_token))

    client.delete(
        f"/api/radios/{radio.id}/assignments/current",
        params={"kind": "unit"},
        headers=bearer(write_token),
    )

    assert client.get(f"/api/radios/{radio.id}").json()["unit_id"] == "E7-SPARE"


def test_a_person_assignment_leaves_the_unit_label_alone(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """`unit_id` names apparatus; handing the radio to somebody does not change which."""
    person = make_person(client, write_token)
    radio = a_radio(session)
    client.patch(f"/api/radios/{radio.id}", json={"unit_id": "M4"}, headers=bearer(write_token))

    client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"person_id": person["id"]},
        headers=bearer(write_token),
    )

    detail = client.get(f"/api/radios/{radio.id}", headers=bearer(write_token)).json()
    assert detail["unit_id"] == "M4"
    assert detail["current_person"]["name"] == "J. Smith"
    assert detail["current_unit"] is None
    assert client.get(f"/api/personnel/{person['id']}/radios", headers=bearer(write_token)).json()[0]["radio_pk"] == radio.id
    assert client.get(f"/api/personnel/{person['id']}", headers=bearer(write_token)).json()["radio_count"] == 1


def test_re_asserting_the_current_assignment_keeps_the_note_it_was_made_with(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """Re-posting the assignment a radio already has is a no-op, notes included."""
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"unit_id": unit["id"], "note": "swapped in at the shop"},
        headers=bearer(write_token),
    )

    again = client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"unit_id": unit["id"], "note": "wrong radio, sorry"},
        headers=bearer(write_token),
    )
    assert again.status_code == 201
    assert again.json()["note"] == "swapped in at the shop"

    history = client.get(f"/api/radios/{radio.id}/assignments", headers=bearer(write_token)).json()
    assert len(history) == 1  # no second row, and the first one is untouched
    assert history[0]["note"] == "swapped in at the shop"


def test_the_loser_of_a_concurrent_assignment_gets_a_409_not_a_500(client, session, archive, write_token, monkeypatch) -> None:  # noqa: ANN001, ARG001
    """Two operators assigning one radio: the index picks a winner, the loser retries.

    The race is that both requests read the same current assignment and only one INSERT
    survives ``uq_radio_assignments_current_unit``.  Blinding the lookup reproduces the
    loser's view of the world — it sees no current row, inserts, and hits the real index.
    """
    from app.services import roster as roster_service

    engine = make_unit(client, write_token, callsign="E7")
    ladder = make_unit(client, write_token, callsign="L3")
    radio = a_radio(session)
    first = client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": engine["id"]}, headers=bearer(write_token)
    )
    assert first.status_code == 201

    monkeypatch.setattr(roster_service, "_current", lambda *args, **kwargs: None)
    lost = client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": ladder["id"]}, headers=bearer(write_token)
    )
    assert lost.status_code == 409
    assert "retry" in lost.json()["detail"]

    monkeypatch.undo()
    # The winner's assignment stands, and nothing half-written is left behind.
    history = client.get(f"/api/radios/{radio.id}/assignments", headers=bearer(write_token)).json()
    assert [h["unit"]["callsign"] for h in history] == ["E7"]
    assert client.get(f"/api/radios/{radio.id}").json()["unit_id"] == "E7"


def test_a_radio_can_hold_a_unit_and_a_person_at_once(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7")
    person = make_person(client, write_token)
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )
    client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"person_id": person["id"]},
        headers=bearer(write_token),
    )

    detail = client.get(f"/api/radios/{radio.id}", headers=bearer(write_token)).json()
    assert detail["current_unit"]["callsign"] == "E7"
    assert detail["current_person"]["name"] == "J. Smith"

    # Ending one leaves the other standing.
    client.delete(
        f"/api/radios/{radio.id}/assignments/current",
        params={"kind": "person"},
        headers=bearer(write_token),
    )
    after = client.get(f"/api/radios/{radio.id}", headers=bearer(write_token)).json()
    assert after["current_unit"]["callsign"] == "E7"
    assert after["current_person"] is None


def test_the_radio_list_carries_the_resolved_identity(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )

    items = client.get("/api/radios", headers=bearer(write_token)).json()["items"]
    row = next(item for item in items if item["id"] == radio.id)
    assert row["current_unit"]["callsign"] == "E7"


def test_an_assignment_body_must_name_exactly_one_side(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    radio = a_radio(session)
    assert client.post(
        f"/api/radios/{radio.id}/assignments", json={}, headers=bearer(write_token)
    ).status_code == 422
    assert client.post(
        f"/api/radios/{radio.id}/assignments",
        json={"unit_id": 1, "person_id": 1},
        headers=bearer(write_token),
    ).status_code == 422
    assert client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": 999999}, headers=bearer(write_token)
    ).status_code == 422


def test_renaming_a_unit_follows_through_to_the_radios_wearing_it(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )

    client.patch(f"/api/units/{unit['id']}", json={"callsign": "E70"}, headers=bearer(write_token))

    assert client.get(f"/api/radios/{radio.id}").json()["unit_id"] == "E70"


def test_renaming_a_unit_onto_a_callsign_that_is_taken_is_a_409(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """The same clash `POST /units` refuses, refused the same way on the way in sideways."""
    make_unit(client, write_token, callsign="E7")
    ladder = make_unit(client, write_token, callsign="L3")

    clash = client.patch(
        f"/api/units/{ladder['id']}", json={"callsign": "E7"}, headers=bearer(write_token)
    )
    assert clash.status_code == 409
    assert "E7" in clash.json()["detail"]
    assert client.get(f"/api/units/{ladder['id']}", headers=bearer(write_token)).json()["callsign"] == "L3"

    # The agency namespaces are separate, so the same callsign under one is fine.
    moved = client.patch(
        f"/api/units/{ladder['id']}",
        json={"callsign": "E7", "agency_code": "EXAMPLEFD"},
        headers=bearer(write_token),
    )
    assert moved.status_code == 200, moved.text
    # A no-op rename onto its own callsign is not a clash with itself.
    assert client.patch(
        f"/api/units/{ladder['id']}", json={"callsign": "E7"}, headers=bearer(write_token)
    ).status_code == 200


def test_a_unit_detail_sums_the_traffic_of_the_radios_it_holds(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E7")
    radio = a_radio(session)
    client.post(
        f"/api/radios/{radio.id}/assignments", json={"unit_id": unit["id"]}, headers=bearer(write_token)
    )

    detail = client.get(f"/api/units/{unit['id']}", headers=bearer(write_token)).json()
    assert detail["radio_count"] == 1
    assert detail["call_count"] == radio.call_count
    assert detail["last_call_at"] is not None


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------


UNITS_CSV = """callsign,name,agency_code,kind,notes
E1,Engine 1,EXAMPLEFD,vehicle,front line
A21,Ambulance 21,,vehicle,
S3,Station 3,,station,
X9,Ghost,NOPE,vehicle,unknown agency
,nameless,,,
"""

PERSONNEL_CSV = """name,rank,badge,agency_code,notes
J. Smith,Captain,4471,EXAMPLEFD,
R. Jones,Firefighter,,,
"""


def test_importing_a_unit_roster_creates_then_updates(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    first = client.post(
        "/api/units/import", json={"content": UNITS_CSV, "format": "csv"}, headers=bearer(write_token)
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 3
    assert first.json()["updated"] == 0
    # The unknown agency code is skipped rather than imported without its agency; the row
    # with no callsign is not a row at all.
    assert first.json()["skipped"] == 1
    assert client.get("/api/units", headers=bearer(write_token)).json()["total"] == 3

    second = client.post(
        "/api/units/import",
        json={"content": UNITS_CSV.replace("Engine 1", "Engine One"), "format": "csv"},
        headers=bearer(write_token),
    )
    assert second.json() == {**first.json(), "created": 0, "updated": 3, "source": second.json()["source"]}
    assert client.get("/api/units", params={"q": "E1"}, headers=bearer(write_token)).json()["items"][0]["name"] == "Engine One"


def test_the_unit_csv_export_round_trips_into_the_importer(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    client.post(
        "/api/units/import", json={"content": UNITS_CSV, "format": "csv"}, headers=bearer(write_token)
    )
    exported = client.get("/api/units/export", headers=bearer(write_token)).text
    assert exported.splitlines()[0] == "callsign,name,agency_code,kind,notes"
    assert "Engine 1" in exported

    again = client.post(
        "/api/units/import", json={"content": exported, "format": "csv"}, headers=bearer(write_token)
    )
    assert again.json()["created"] == 0
    assert again.json()["updated"] == 3


def test_importing_personnel_matches_on_the_name_within_an_agency(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    first = client.post(
        "/api/personnel/import",
        json={"content": PERSONNEL_CSV, "format": "csv"},
        headers=bearer(write_token),
    )
    assert first.json()["created"] == 2

    second = client.post(
        "/api/personnel/import",
        json={"content": PERSONNEL_CSV.replace("J. Smith", "j. smith"), "format": "csv"},
        headers=bearer(write_token),
    )
    assert second.json() == {**second.json(), "created": 0, "updated": 2}
    assert client.get("/api/personnel", headers=bearer(write_token)).json()["total"] == 2
    # Matched case-insensitively — and the file's capitalization won, so the second import
    # edited the row it found instead of adding a second J. Smith.
    assert {p["name"] for p in client.get("/api/personnel", headers=bearer(write_token)).json()["items"]} == {
        "j. smith",
        "R. Jones",
    }

    exported = client.get("/api/personnel/export", headers=bearer(write_token)).text
    assert exported.splitlines()[0] == "name,rank,badge,agency_code,notes,active"


def test_importing_a_roster_needs_a_credential(client, archive) -> None:  # noqa: ANN001, ARG001
    response = client.post(
        "/api/units/import", json={"content": UNITS_CSV, "format": "csv"}, headers=CSRF_HEADERS
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------------------
# the roster reaches the rest of the archive
# --------------------------------------------------------------------------------------


def test_suggest_offers_units_and_personnel(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    make_unit(client, write_token, callsign="E7", name="Engine 7")
    make_person(client, write_token, name="Smithers")

    units = client.get("/api/search/suggest", params={"q": "E7"}).json()
    assert {"kind": "unit", "value": "E7"} in [{"kind": s["kind"], "value": s["value"]} for s in units]
    assert next(s for s in units if s["value"] == "E7")["label"] == "Engine 7"

    people = client.get("/api/search/suggest", params={"q": "smith"}).json()
    assert any(s["kind"] == "person" and s["value"] == "Smithers" for s in people)


# --------------------------------------------------------------------------------------
# the roster is not public
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/units", "/api/units/export", "/api/personnel", "/api/personnel/export"],
)
def test_the_roster_is_not_readable_without_a_credential(client, archive, path) -> None:  # noqa: ANN001, ARG001
    # Units and personnel are names, ranks and badge numbers the operator typed — unlike
    # the talkgroups and radios, they were never broadcast. The UI hides the pages from a
    # guest; this is the half that a `curl` cannot walk around.
    assert client.get(path).status_code == 401


def test_a_read_credential_is_enough_for_the_roster(client, archive, read_token, write_token) -> None:  # noqa: ANN001, ARG001
    unit = make_unit(client, write_token, callsign="E9")
    person = make_person(client, write_token, name="Reader Test")

    assert client.get("/api/units", headers=bearer(read_token)).status_code == 200
    assert client.get(f"/api/units/{unit['id']}", headers=bearer(read_token)).status_code == 200
    assert client.get("/api/personnel", headers=bearer(read_token)).status_code == 200
    assert client.get(f"/api/personnel/{person['id']}", headers=bearer(read_token)).status_code == 200


def test_a_radios_own_assignment_history_stays_public(client, archive, session) -> None:  # noqa: ANN001, ARG001
    # The radio detail page is public, and the assignment history is part of it — so the
    # roster's 401 stops at the two directory lists, not at everything in this router.
    radio = a_radio(session)
    assert client.get(f"/api/radios/{radio.id}/assignments").status_code == 200
