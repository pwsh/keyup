"""Editing many rows at once: ``POST /{talkgroups,radios,units,personnel}/bulk`` (§4.2).

The interesting behavior is not "it writes the columns" — it is the three refusals that keep
a bulk edit from being a way to lose data: the per-row identity fields that cannot be shared
(a unit's callsign, a radio's alias and unit id), the cap that stops one request from holding
a transaction open over the whole directory, and the all-or-nothing resolve that would rather
404 than quietly update ninety-seven of a hundred rows.

The endpoints are POST on a literal ``/bulk`` rather than a PATCH on the collection because
``PATCH /talkgroups/{id}`` already owns that shape: ``/talkgroups/bulk`` would be parsed as an
id and 422 before any handler ran.  ``test_the_literal_path_wins_over_the_id_route`` is the
guard on that, since adding a ``POST /talkgroups/{id}`` later would silently steal it.
"""

from __future__ import annotations

from sqlalchemy import select

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def talkgroup_ids(client) -> list[int]:  # noqa: ANN001
    return [t["id"] for t in client.get("/api/talkgroups").json()["items"]]


def radio_ids(client) -> list[int]:  # noqa: ANN001
    return [r["id"] for r in client.get("/api/radios").json()["items"]]


def make_units(client, token, callsigns) -> list[int]:  # noqa: ANN001
    ids = []
    for callsign in callsigns:
        response = client.post(
            "/api/units", json={"callsign": callsign, "kind": "vehicle"}, headers=bearer(token)
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


def make_people(client, token, names) -> list[int]:  # noqa: ANN001
    ids = []
    for name in names:
        response = client.post("/api/personnel", json={"name": name}, headers=bearer(token))
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    return ids


# --------------------------------------------------------------------------------------
# talkgroups
# --------------------------------------------------------------------------------------


def test_one_patch_reaches_every_selected_talkgroup(client, archive, write_token) -> None:
    ids = talkgroup_ids(client)[:3]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"category": "fire", "transcribe": False, "priority": 7}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == len(ids)
    assert {item["id"] for item in body["items"]} == set(ids)
    assert all(item["category"] == "fire" for item in body["items"])
    assert all(item["transcribe"] is False for item in body["items"])
    assert all(item["priority"] == 7 for item in body["items"])

    # And it is actually in the table, not just in the response.
    stored = {t["id"]: t for t in client.get("/api/talkgroups").json()["items"]}
    assert all(stored[i]["category"] == "fire" for i in ids)


def test_a_key_absent_from_changes_is_left_alone(client, archive, write_token) -> None:
    """``exclude_unset``, exactly as the single PATCH: the untouched columns keep their values."""
    ids = talkgroup_ids(client)[:2]
    before = {t["id"]: t for t in client.get("/api/talkgroups").json()["items"]}
    client.patch(
        f"/api/talkgroups/{ids[0]}", json={"color": "#ff0000"}, headers=bearer(write_token)
    )

    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"category": "ems"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}
    assert items[ids[0]]["color"] == "#ff0000"
    assert items[ids[1]]["alias"] == before[ids[1]]["alias"]


def test_a_key_present_with_null_clears_it_for_all(client, archive, write_token) -> None:
    """The single PATCH lets `service_tag: null` clear the tag; so does the bulk one."""
    ids = talkgroup_ids(client)[:2]
    client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"service_tag": "Fire Dispatch"}},
        headers=bearer(write_token),
    )
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"service_tag": None}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert all(item["service_tag"] is None for item in response.json()["items"])


def test_the_three_retag_columns_go_up_together(client, session, archive, write_token) -> None:
    """Agency, category and service tag in one request.

    The three the drawer added for a reason: retagging a shift's channels is one
    decision spelled three ways, and the point of doing it in bulk is that it is one
    request rather than three passes over the same selection. The route whitelists
    nothing — every key `TalkgroupPatch` accepts arrives here — so the guard worth
    having is that all three actually land and the activity line names all three.
    """
    from app.models import ActivityEvent

    ids = talkgroup_ids(client)[:3]
    response = client.post(
        "/api/talkgroups/bulk",
        json={
            "ids": ids,
            "changes": {"agency_code": "EXAMPLEFD", "category": "fire", "service_tag": "Fire Dispatch"},
        },
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert response.json()["updated"] == len(ids)
    assert all(item["agency_code"] == "EXAMPLEFD" for item in items)
    assert all(item["category"] == "fire" for item in items)
    assert all(item["service_tag"] == "Fire Dispatch" for item in items)

    event = session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "talkgroups_bulk_edited")
    ).one()
    assert event.meta["fields"] == ["agency_code", "category", "service_tag"]
    assert event.message == "Edited 3 talkgroups (agency_code, category, service_tag)"


def test_clearing_the_three_retag_columns_in_bulk(client, archive, write_token) -> None:
    """The drawer's Clear on each of them: `null` empties the column, it does not skip it."""
    ids = talkgroup_ids(client)[:2]
    client.post(
        "/api/talkgroups/bulk",
        json={
            "ids": ids,
            "changes": {"agency_code": "EXAMPLEFD", "category": "fire", "service_tag": "Fire Dispatch"},
        },
        headers=bearer(write_token),
    )
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"agency_code": None, "service_tag": None}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert all(item["agency_code"] is None for item in items)
    assert all(item["service_tag"] is None for item in items)
    # `category` was not in `changes`, so it is untouched — the whole point of the tri-state.
    assert all(item["category"] == "fire" for item in items)


def test_bulk_validates_the_service_tag_but_not_the_category(client, archive, write_token) -> None:
    """The asymmetry `_service_tag_validator` documents, held across the bulk route too.

    `category` is this archive's own bucket and an operator may invent one, so it is free
    text; `service_tag` claims to be *RadioReference's* classification, so a value RR would
    not recognize is a 422 rather than a quiet lie in the column the sync reads. The drawer
    offers closed pickers for both, but the route is what enforces it — and the enforcement
    has to be the same here as on the single-row PATCH or a bulk edit becomes the back door.
    """
    ids = talkgroup_ids(client)[:2]
    invented = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"category": "harbor_patrol"}},
        headers=bearer(write_token),
    )
    assert invented.status_code == 200, invented.text
    assert all(item["category"] == "harbor_patrol" for item in invented.json()["items"])

    nonsense = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"service_tag": "Interpretive Dance"}},
        headers=bearer(write_token),
    )
    assert nonsense.status_code == 422, nonsense.text
    # And the canonical spelling is what lands, from whatever case the client sent.
    canonical = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"service_tag": "fire dispatch"}},
        headers=bearer(write_token),
    )
    assert canonical.status_code == 200, canonical.text
    assert all(item["service_tag"] == "Fire Dispatch" for item in canonical.json()["items"])


def test_the_literal_path_wins_over_the_id_route(client, archive, write_token) -> None:
    """`bulk` must not be parsed as a talkgroup id — that is why this is a POST sub-path."""
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": talkgroup_ids(client)[:1], "changes": {"is_hidden": True}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    # The id route is a PATCH, and it still refuses the word.
    assert client.patch("/api/talkgroups/bulk", json={}, headers=bearer(write_token)).status_code == 422


def test_an_unknown_agency_code_is_refused_once_for_the_batch(client, archive, write_token) -> None:
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": talkgroup_ids(client)[:3], "changes": {"agency_code": "NOPE"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422
    assert "NOPE" in response.json()["detail"]
    # Nothing was written: the check runs before the rows are even fetched.
    assert all(t["agency_code"] != "NOPE" for t in client.get("/api/talkgroups").json()["items"])


def test_more_than_five_hundred_ids_is_refused(client, archive, write_token) -> None:
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": list(range(1, 502)), "changes": {"transcribe": True}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422
    # And 500 exactly is not over the cap — it fails on the missing rows instead.
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": list(range(900000, 900500)), "changes": {"transcribe": True}},
        headers=bearer(write_token),
    )
    assert response.status_code == 404


def test_an_empty_selection_or_an_empty_change_is_refused(client, archive, write_token) -> None:
    ids = talkgroup_ids(client)[:1]
    assert (
        client.post(
            "/api/talkgroups/bulk",
            json={"ids": [], "changes": {"transcribe": True}},
            headers=bearer(write_token),
        ).status_code
        == 422
    )
    empty = client.post(
        "/api/talkgroups/bulk", json={"ids": ids, "changes": {}}, headers=bearer(write_token)
    )
    assert empty.status_code == 422
    assert "at least one field" in empty.json()["detail"]


def test_an_id_that_is_not_there_fails_the_whole_batch(client, archive, write_token) -> None:
    ids = talkgroup_ids(client)[:2]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": [*ids, 987654], "changes": {"category": "law"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 404
    assert "987654" in response.json()["detail"]
    stored = {t["id"]: t for t in client.get("/api/talkgroups").json()["items"]}
    assert all(stored[i]["category"] != "law" for i in ids)


def test_a_repeated_id_is_the_same_row(client, archive, write_token) -> None:
    one = talkgroup_ids(client)[:1]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": one * 3, "changes": {"is_hidden": True}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 1


def test_the_bulk_edit_leaves_one_activity_line(client, session, archive, write_token) -> None:
    from app.models import ActivityEvent

    ids = talkgroup_ids(client)[:3]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"category": "fire", "color": "#112233"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text

    rows = list(
        session.scalars(
            select(ActivityEvent).where(ActivityEvent.kind == "talkgroups_bulk_edited")
        )
    )
    assert len(rows) == 1, "a hundred-row edit is one operator action, so it is one log line"
    event = rows[0]
    assert event.message == "Edited 3 talkgroups (category, color)"
    assert event.meta["fields"] == ["category", "color"]
    assert sorted(event.meta["ids"]) == sorted(ids)
    assert event.meta["count"] == 3
    assert event.meta["by"]


def test_bulk_needs_a_credential(client, archive, write_token, read_token) -> None:
    """The same three answers every other mutation gives — the guard is application-wide."""
    ids = talkgroup_ids(client)[:1]
    body = {"ids": ids, "changes": {"transcribe": False}}
    # No CSRF proof at all: a cross-site page could have sent this.
    assert client.post("/api/talkgroups/bulk", json=body).status_code == 403
    # CSRF proof, no credential.
    assert client.post("/api/talkgroups/bulk", json=body, headers=CSRF_HEADERS).status_code == 401
    # A credential that may read but not write.
    assert client.post("/api/talkgroups/bulk", json=body, headers=bearer(read_token)).status_code == 403


# --------------------------------------------------------------------------------------
# radios
# --------------------------------------------------------------------------------------


def test_radios_take_an_agency_and_a_kind_in_bulk(client, archive, write_token) -> None:
    ids = radio_ids(client)[:3]
    response = client.post(
        "/api/radios/bulk",
        json={"ids": ids, "changes": {"agency_code": "EXAMPLEFD", "kind": "portable"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == len(ids)
    assert all(item["kind"] == "portable" for item in response.json()["items"])
    assert all(item["agency_code"] == "EXAMPLEFD" for item in response.json()["items"])


def test_a_radios_alias_and_unit_id_are_refused_in_bulk(client, archive, write_token) -> None:
    """They name one radio. Written across a selection they would erase the directory."""
    ids = radio_ids(client)[:2]
    for field, value in (("alias", "Shared"), ("unit_id", "E1")):
        response = client.post(
            "/api/radios/bulk",
            json={"ids": ids, "changes": {field: value}},
            headers=bearer(write_token),
        )
        assert response.status_code == 422, f"{field} should be refused"
        assert field in response.json()["detail"]
    # The singles still take them.
    assert (
        client.patch(f"/api/radios/{ids[0]}", json={"alias": "Shared"}, headers=bearer(write_token)).status_code
        == 200
    )


def test_a_radio_bulk_edit_refuses_an_unknown_agency(client, archive, write_token) -> None:
    response = client.post(
        "/api/radios/bulk",
        json={"ids": radio_ids(client)[:1], "changes": {"agency_code": "NOPE"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422


def test_a_radio_bulk_edit_is_logged_once(client, session, archive, write_token) -> None:
    from app.models import ActivityEvent

    ids = radio_ids(client)[:2]
    client.post(
        "/api/radios/bulk",
        json={"ids": ids, "changes": {"kind": "mobile"}},
        headers=bearer(write_token),
    )
    rows = list(
        session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "radios_bulk_edited"))
    )
    assert len(rows) == 1
    assert rows[0].message == "Edited 2 radios (kind)"


def test_radio_bulk_needs_write(client, archive, read_token) -> None:
    body = {"ids": radio_ids(client)[:1], "changes": {"kind": "mobile"}}
    assert client.post("/api/radios/bulk", json=body, headers=CSRF_HEADERS).status_code == 401
    assert client.post("/api/radios/bulk", json=body, headers=bearer(read_token)).status_code == 403


# --------------------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------------------


def test_units_take_a_kind_and_an_agency_in_bulk(client, archive, write_token) -> None:
    ids = make_units(client, write_token, ["E1", "E2", "A21"])
    response = client.post(
        "/api/units/bulk",
        json={"ids": ids, "changes": {"kind": "station", "agency_code": "EXAMPLEFD"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 3
    assert all(item["kind"] == "station" for item in response.json()["items"])


def test_a_callsign_cannot_be_set_in_bulk(client, archive, write_token) -> None:
    """It is the unique key: the same one on two units is a collision, not an edit."""
    ids = make_units(client, write_token, ["E1", "E2"])
    response = client.post(
        "/api/units/bulk",
        json={"ids": ids, "changes": {"callsign": "E9"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422
    assert "callsign" in response.json()["detail"]
    # …even alongside a field that would have been fine on its own.
    mixed = client.post(
        "/api/units/bulk",
        json={"ids": ids, "changes": {"callsign": "E9", "kind": "team"}},
        headers=bearer(write_token),
    )
    assert mixed.status_code == 422
    assert client.get(f"/api/units/{ids[0]}", headers=bearer(write_token)).json()["kind"] == "vehicle"


def test_moving_two_units_with_the_same_callsign_into_one_agency_is_a_409(
    client, archive, write_token
) -> None:
    """The unique key is per agency, so the agency-less pair only collides once moved."""
    first = make_units(client, write_token, ["E1"])[0]
    second = client.post(
        "/api/units",
        json={"callsign": "E1", "agency_code": "EXAMPLEFD", "kind": "vehicle"},
        headers=bearer(write_token),
    )
    assert second.status_code == 201, second.text
    response = client.post(
        "/api/units/bulk",
        json={"ids": [first, second.json()["id"]], "changes": {"agency_code": "EXAMPLEFD"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 409
    assert client.get(f"/api/units/{first}", headers=bearer(write_token)).json()["agency_code"] is None


def test_a_unit_bulk_edit_refuses_an_unknown_agency(client, archive, write_token) -> None:
    ids = make_units(client, write_token, ["E1"])
    response = client.post(
        "/api/units/bulk",
        json={"ids": ids, "changes": {"agency_code": "NOPE"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422


def test_a_unit_bulk_edit_is_logged_once(client, session, archive, write_token) -> None:
    from app.models import ActivityEvent

    ids = make_units(client, write_token, ["E1", "E2"])
    client.post(
        "/api/units/bulk", json={"ids": ids, "changes": {"kind": "team"}}, headers=bearer(write_token)
    )
    rows = list(
        session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "units_bulk_edited"))
    )
    assert len(rows) == 1
    assert rows[0].message == "Edited 2 units (kind)"


def test_unit_bulk_needs_write(client, archive, write_token, read_token) -> None:
    body = {"ids": make_units(client, write_token, ["E1"]), "changes": {"kind": "team"}}
    assert client.post("/api/units/bulk", json=body, headers=CSRF_HEADERS).status_code == 401
    assert client.post("/api/units/bulk", json=body, headers=bearer(read_token)).status_code == 403


# --------------------------------------------------------------------------------------
# personnel
# --------------------------------------------------------------------------------------


def test_people_take_a_rank_an_agency_and_active_in_bulk(client, archive, write_token) -> None:
    ids = make_people(client, write_token, ["J. Smith", "A. Jones", "R. Diaz"])
    response = client.post(
        "/api/personnel/bulk",
        json={"ids": ids, "changes": {"rank": "Captain", "active": False, "agency_code": "EXAMPLEFD"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 3
    assert all(item["rank"] == "Captain" for item in response.json()["items"])
    assert all(item["active"] is False for item in response.json()["items"])


def test_clearing_a_rank_in_bulk(client, archive, write_token) -> None:
    ids = make_people(client, write_token, ["J. Smith", "A. Jones"])
    client.post(
        "/api/personnel/bulk",
        json={"ids": ids, "changes": {"rank": "Captain"}},
        headers=bearer(write_token),
    )
    response = client.post(
        "/api/personnel/bulk",
        json={"ids": ids, "changes": {"rank": None}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    assert all(item["rank"] is None for item in response.json()["items"])


def test_a_personnel_bulk_edit_refuses_an_unknown_agency(client, archive, write_token) -> None:
    ids = make_people(client, write_token, ["J. Smith"])
    response = client.post(
        "/api/personnel/bulk",
        json={"ids": ids, "changes": {"agency_code": "NOPE"}},
        headers=bearer(write_token),
    )
    assert response.status_code == 422


def test_a_personnel_bulk_edit_is_logged_once(client, session, archive, write_token) -> None:
    from app.models import ActivityEvent

    ids = make_people(client, write_token, ["J. Smith", "A. Jones"])
    client.post(
        "/api/personnel/bulk",
        json={"ids": ids, "changes": {"active": False}},
        headers=bearer(write_token),
    )
    rows = list(
        session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "personnel_bulk_edited"))
    )
    assert len(rows) == 1
    assert rows[0].message == "Edited 2 people (active)"


def test_personnel_bulk_needs_write(client, archive, write_token, read_token) -> None:
    body = {"ids": make_people(client, write_token, ["J. Smith"]), "changes": {"active": False}}
    assert client.post("/api/personnel/bulk", json=body, headers=CSRF_HEADERS).status_code == 401
    assert client.post("/api/personnel/bulk", json=body, headers=bearer(read_token)).status_code == 403


def test_bulk_setting_the_export_flags(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """The two checkboxes are ordinary patch fields, so bulk edit reaches them."""
    from app.models import Talkgroup

    ids = [row.id for row in session.scalars(select(Talkgroup)).all()]
    assert ids

    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"export_broadcastify": False}},
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == len(ids)
    assert all(item["export_broadcastify"] is False for item in body["items"])
    # …and the other service's box is untouched, because only the keys present are written.
    assert all(item["export_openmhz"] is True for item in body["items"])
