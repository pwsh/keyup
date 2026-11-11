"""Editing radio systems: ``PATCH``, ``DELETE`` and ``merge-into``.

The archive fixture ingests six real recordings under ``Example System``, so every test here
starts with one system that genuinely holds calls, sites, talkgroups and radios.  The merge
tests add a *second* system carrying twins of all three — the shape a mislabeled recorder
leaves behind, and the one production actually grew — because a merge that only moves rows
with no counterpart proves nothing: the interesting half is what happens to the duplicates,
and the promise that no call is ever among them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import Call, Radio, Site, System, Talkgroup
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

STARTED = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)


def _call(session, *, system_id, site_id, talkgroup_id, tgid, name, radio_id=None) -> Call:  # noqa: ANN001
    call = Call(
        system_id=system_id,
        site_id=site_id,
        talkgroup_id=talkgroup_id,
        radio_id=radio_id,
        talkgroup_ids=[tgid],
        recorded_at=STARTED + timedelta(seconds=3),
        started_at=STARTED,
        started_local=STARTED.replace(tzinfo=None),
        ended_at=STARTED + timedelta(seconds=3),
        duration_s=Decimal("3.000"),
        file_path=f"20260830/{name}.mp3",
        file_name=f"{name}.mp3",
    )
    session.add(call)
    session.flush()
    return call


@pytest.fixture
def example(client, archive) -> dict:  # noqa: ANN001, ARG001
    return next(row for row in client.get("/api/systems").json() if row["name"] == "Example System")


@pytest.fixture
def empty_system(session, archive):  # noqa: ANN001, ARG001
    """A system nothing hangs off — the only kind that may be deleted."""
    row = System(name="Lakeside Metro", wacn=101010)
    session.add(row)
    session.commit()
    return row.id


@pytest.fixture
def twinned(session, archive, example):  # noqa: ANN001, ARG001
    """A second system holding twins of a site, a talkgroup and a radio, plus a singleton.

    ``Central`` and talkgroup ``1701`` already exist under ``Example System``; ``Eastside`` and
    ``1999`` do not.  The fold therefore has to do both things at once — merge the twins
    and re-parent the strangers — which is exactly what a mislabeled recorder produces.
    """
    real_id = example["id"]
    real_central = session.scalars(
        select(Site).where(Site.system_id == real_id, Site.name == "Central")
    ).one()
    real_tg = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == real_id, Talkgroup.tgid == 1701)
    ).one()
    real_tg.call_count = 7
    # Radio 9200007 is already in the archive: it is the FROM of one of the six ingested
    # recordings, which is exactly why it makes a realistic twin.
    real_radio = session.scalars(
        select(Radio).where(Radio.system_id == real_id, Radio.radio_id == 9_200_007)
    ).one()
    real_radio.call_count = 3

    ghost = System(name="upload")
    session.add(ghost)
    session.flush()

    ghost_central = Site(system_id=ghost.id, name="Central", nac=0x34)
    eastside = Site(system_id=ghost.id, name="Eastside", site_number=4)
    ghost_tg = Talkgroup(system_id=ghost.id, tgid=1701, call_count=2)
    stranger_tg = Talkgroup(system_id=ghost.id, tgid=1999, alias="EC EVENT 9", call_count=1)
    ghost_radio = Radio(
        system_id=ghost.id, radio_id=9_200_007, wacn=0, home_system_id=0, alias="E1", call_count=4
    )
    session.add_all([ghost_central, eastside, ghost_tg, stranger_tg, ghost_radio])
    session.flush()

    twin_call = _call(
        session,
        system_id=ghost.id,
        site_id=ghost_central.id,
        talkgroup_id=ghost_tg.id,
        tgid=1701,
        name="ghost",
        radio_id=ghost_radio.id,
    )
    stranger_call = _call(
        session,
        system_id=ghost.id,
        site_id=eastside.id,
        talkgroup_id=stranger_tg.id,
        tgid=1999,
        name="eastside",
    )
    session.commit()
    return {
        "real": real_id,
        "ghost": ghost.id,
        "real_central": real_central.id,
        "ghost_central": ghost_central.id,
        "eastside": eastside.id,
        "real_tg": real_tg.id,
        "ghost_tg": ghost_tg.id,
        "stranger_tg": stranger_tg.id,
        "real_radio": real_radio.id,
        "ghost_radio": ghost_radio.id,
        "calls": (twin_call.id, stranger_call.id),
    }


# --------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------


def test_a_system_says_whether_it_could_be_deleted_and_what_holds_it(client, archive, empty_system) -> None:  # noqa: ANN001, ARG001
    rows = {row["name"]: row for row in client.get("/api/systems").json()}

    held = rows["Example System"]
    assert held["deletable"] is False
    assert held["blocking"]["calls"] == held["call_count"] > 0
    assert held["blocking"]["sites"] == held["site_count"] > 0
    # Talkgroups and radios are blockers too, and they are counted off the schema's own
    # foreign keys rather than a hand-written list.
    assert held["blocking"]["talkgroups"] > 0

    free = rows["Lakeside Metro"]
    assert free["deletable"] is True
    assert free["blocking"] == {}
    assert free["id"] == empty_system


# --------------------------------------------------------------------------------------
# PATCH
# --------------------------------------------------------------------------------------


def test_patching_a_system_writes_only_the_keys_present(client, archive, example, write_token) -> None:  # noqa: ANN001, ARG001
    response = client.patch(
        f"/api/systems/{example['id']}",
        json={"description": "Example County simulcast"},
        headers=bearer(write_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Example County simulcast"
    assert body["name"] == "Example System"
    # What the air announced about itself is untouched, and the counts still come back.
    assert body["wacn"] == example["wacn"]
    assert body["call_count"] == example["call_count"] > 0


def test_renaming_a_system_onto_another_is_a_409(client, archive, example, empty_system, write_token) -> None:  # noqa: ANN001, ARG001
    clash = client.patch(
        f"/api/systems/{example['id']}",
        json={"name": "Lakeside Metro"},
        headers=bearer(write_token),
    )
    assert clash.status_code == 409
    assert str(empty_system) in clash.json()["detail"]
    assert "already exists" in clash.json()["detail"]


def test_a_system_can_be_renamed_to_something_free(client, archive, example, write_token) -> None:  # noqa: ANN001, ARG001
    response = client.patch(
        f"/api/systems/{example['id']}", json={"name": "  Example Net  "}, headers=bearer(write_token)
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Example Net"
    assert client.patch(
        f"/api/systems/{example['id']}", json={"name": ""}, headers=bearer(write_token)
    ).status_code == 422


def test_patching_a_system_needs_a_write_scope(client, archive, example, read_token) -> None:  # noqa: ANN001, ARG001
    assert client.patch(f"/api/systems/{example['id']}", json={"description": "x"}).status_code == 403
    denied = client.patch(
        f"/api/systems/{example['id']}", json={"description": "x"}, headers=bearer(read_token)
    )
    assert denied.status_code == 403


def test_patching_a_missing_system_is_a_404(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    response = client.patch("/api/systems/999999", json={"description": "x"}, headers=bearer(write_token))
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# DELETE
# --------------------------------------------------------------------------------------


def test_an_empty_system_can_be_deleted(client, archive, empty_system, admin_token, session) -> None:  # noqa: ANN001, ARG001
    response = client.delete(f"/api/systems/{empty_system}", headers=bearer(admin_token))
    assert response.status_code == 204
    assert session.get(System, empty_system) is None

    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    deleted = [row for row in events if row["kind"] == "system_deleted"]
    assert deleted and "Lakeside Metro" in deleted[0]["message"]


def test_a_system_holding_anything_cannot_be_deleted(client, archive, example, admin_token, session) -> None:  # noqa: ANN001, ARG001
    """The guard the cascade makes essential: every FK onto ``systems`` is ON DELETE CASCADE."""
    before = session.scalar(select(func.count()).select_from(Call))

    response = client.delete(f"/api/systems/{example['id']}", headers=bearer(admin_token))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "calls" in detail and "sites" in detail
    assert "merge it into another system" in detail

    # Refused means refused — not one call went with it.
    assert session.get(System, example["id"]) is not None
    assert session.scalar(select(func.count()).select_from(Call)) == before > 0


def test_deleting_a_system_is_admin_only(client, archive, empty_system, write_token) -> None:  # noqa: ANN001, ARG001
    assert client.delete(f"/api/systems/{empty_system}").status_code == 403
    denied = client.delete(f"/api/systems/{empty_system}", headers=bearer(write_token))
    assert denied.status_code == 403


def test_deleting_a_missing_system_is_a_404(client, archive, admin_token) -> None:  # noqa: ANN001, ARG001
    assert client.delete("/api/systems/999999", headers=bearer(admin_token)).status_code == 404


# --------------------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------------------


def test_merging_folds_the_twins_and_re_parents_the_strangers(client, archive, twinned, admin_token, session) -> None:  # noqa: ANN001, ARG001
    before = session.scalar(select(func.count()).select_from(Call))

    response = client.post(
        f"/api/systems/{twinned['ghost']}/merge-into/{twinned['real']}", headers=bearer(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == {"id": twinned["ghost"], "name": "upload"}
    assert body["target"]["id"] == twinned["real"]
    assert body["merged"] == {"sites": 1, "talkgroups": 1, "radios": 1}
    assert body["reparented"] == {"sites": 1, "talkgroups": 1}
    assert body["deleted"]["systems"] == 1

    session.expire_all()

    # No call was lost, and every one of them now hangs off the surviving system.
    assert session.scalar(select(func.count()).select_from(Call)) == before
    assert {c.system_id for c in session.scalars(select(Call))} == {twinned["real"]}

    # The duplicate site, talkgroup and radio are gone; the call that pointed at each of
    # them now points at the row that survived, not at nothing.
    twin_call = session.get(Call, twinned["calls"][0])
    assert twin_call.site_id == twinned["real_central"]
    assert twin_call.talkgroup_id == twinned["real_tg"]
    assert twin_call.radio_id == twinned["real_radio"]
    assert session.get(Site, twinned["ghost_central"]) is None
    assert session.get(Talkgroup, twinned["ghost_tg"]) is None
    assert session.get(Radio, twinned["ghost_radio"]) is None

    # The rows with no counterpart moved house rather than being dropped.
    assert session.get(Site, twinned["eastside"]).system_id == twinned["real"]
    assert session.get(Talkgroup, twinned["stranger_tg"]).system_id == twinned["real"]
    assert session.get(Call, twinned["calls"][1]).site_id == twinned["eastside"]

    # Counters are summed across the fold, and the blanks on the survivor are filled from
    # the row that lost — never the other way round.
    assert session.get(Talkgroup, twinned["real_tg"]).call_count == 9
    assert session.get(Radio, twinned["real_radio"]).call_count == 7
    assert session.get(Radio, twinned["real_radio"]).alias == "E1"
    assert session.get(Site, twinned["real_central"]).nac == 0x34

    # And the source system itself is gone.
    assert session.get(System, twinned["ghost"]) is None
    assert [row["name"] for row in client.get("/api/systems").json()] == ["Example System"]


def test_a_merge_is_recorded_in_the_activity_log(client, archive, twinned, admin_token) -> None:  # noqa: ANN001, ARG001
    client.post(
        f"/api/systems/{twinned['ghost']}/merge-into/{twinned['real']}", headers=bearer(admin_token)
    )
    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    merged = [row for row in events if row["kind"] == "system_merged"]
    assert merged and "Merged system upload into Example System" in merged[0]["message"]


def test_merging_a_system_into_itself_is_a_409(client, archive, example, admin_token) -> None:  # noqa: ANN001, ARG001
    response = client.post(
        f"/api/systems/{example['id']}/merge-into/{example['id']}", headers=bearer(admin_token)
    )
    assert response.status_code == 409
    assert "cannot be merged into itself" in response.json()["detail"]


def test_merging_names_the_side_that_is_missing(client, archive, example, admin_token) -> None:  # noqa: ANN001, ARG001
    gone = client.post(f"/api/systems/999999/merge-into/{example['id']}", headers=bearer(admin_token))
    assert gone.status_code == 404
    assert "no system 999999" in gone.json()["detail"]

    no_target = client.post(
        f"/api/systems/{example['id']}/merge-into/999999", headers=bearer(admin_token)
    )
    assert no_target.status_code == 404
    assert "no system 999999" in no_target.json()["detail"]


def test_merging_is_admin_only(client, archive, twinned, write_token) -> None:  # noqa: ANN001, ARG001
    path = f"/api/systems/{twinned['ghost']}/merge-into/{twinned['real']}"
    assert client.post(path).status_code == 403
    assert client.post(path, headers=bearer(write_token)).status_code == 403
