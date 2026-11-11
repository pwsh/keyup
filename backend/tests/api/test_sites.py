"""Editing tower sites: ``PATCH``, ``DELETE`` and ``merge-into`` (§4.2).

The archive fixture ingests six fixture recordings under ``Example System`` / ``Central``, so
every test here starts with one site that genuinely holds calls.  A second system with a
site of the *same name* is added where it matters, because that is the case the whole
"System / Site" labeling exists for: a site name alone does not identify a site.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import AdjacentSite, Call, RecorderObservation, Site, SiteFrequency, System
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def central(client, archive) -> dict:  # noqa: ANN001, ARG001
    return next(row for row in client.get("/api/sites").json() if row["name"] == "Central")


@pytest.fixture
def twin_named_site(session, archive):  # noqa: ANN001, ARG001
    """A second system whose site is called ``Central`` too — the ambiguity, on purpose."""
    other = System(name="Lakeside Metro", wacn=101010)
    session.add(other)
    session.flush()
    site = Site(system_id=other.id, name="Central", site_number=9)
    session.add(site)
    session.commit()
    return {"system_id": other.id, "site_id": site.id}


# --------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------


def test_every_site_row_names_its_system(client, archive, twin_named_site) -> None:  # noqa: ANN001, ARG001
    response = client.get("/api/sites")
    rows = response.json()

    assert response.headers["X-Total-Count"] == str(len(rows))
    centrals = [row for row in rows if row["name"] == "Central"]
    assert len(centrals) == 2
    # Two sites, one name, two systems: the row carries what tells them apart.
    assert {row["system_name"] for row in centrals} == {"Example System", "Lakeside Metro"}
    assert all(row["system_name"] for row in rows)


def test_a_site_says_whether_it_could_be_deleted_and_what_holds_it(client, archive, twin_named_site) -> None:  # noqa: ANN001, ARG001
    rows = {(row["system_name"], row["name"]): row for row in client.get("/api/sites").json()}

    held = rows[("Example System", "Central")]
    assert held["deletable"] is False
    assert held["blocking"]["calls"] == held["call_count"] > 0

    free = rows[("Lakeside Metro", "Central")]
    assert free["deletable"] is True
    assert free["blocking"] == {}

    # …and the single-site read agrees with the listing.
    one = client.get(f"/api/sites/{held['id']}").json()
    assert one["deletable"] is False
    assert one["blocking"] == held["blocking"]
    assert client.get("/api/sites/999999").status_code == 404


def test_the_listing_can_be_narrowed_to_one_system(client, archive, twin_named_site) -> None:  # noqa: ANN001, ARG001
    rows = client.get("/api/sites", params={"system": twin_named_site["system_id"]}).json()
    assert [row["id"] for row in rows] == [twin_named_site["site_id"]]


# --------------------------------------------------------------------------------------
# PATCH
# --------------------------------------------------------------------------------------


def test_patching_a_site_writes_only_the_keys_present(client, archive, central, write_token) -> None:  # noqa: ANN001, ARG001
    response = client.patch(
        f"/api/sites/{central['id']}",
        json={"name": "Example County", "notes": "Simulcast; the school traffic was refiled."},
        headers=bearer(write_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Example County"
    assert body["notes"] == "Simulcast; the school traffic was refiled."
    # Untouched keys keep what the observations put there.
    assert body["site_number"] == central["site_number"]
    assert body["system_name"] == "Example System"


def test_the_radioreference_mapping_is_editable_and_clearable(client, archive, central, write_token) -> None:  # noqa: ANN001, ARG001
    set_it = client.patch(
        f"/api/sites/{central['id']}", json={"rr_site_id": 9001}, headers=bearer(write_token)
    )
    assert set_it.json()["rr_site_id"] == 9001

    cleared = client.patch(
        f"/api/sites/{central['id']}", json={"rr_site_id": None}, headers=bearer(write_token)
    )
    assert cleared.json()["rr_site_id"] is None

    bad = client.patch(
        f"/api/sites/{central['id']}", json={"rr_site_id": "nine"}, headers=bearer(write_token)
    )
    assert bad.status_code == 422
    assert client.patch(
        f"/api/sites/{central['id']}", json={"rr_site_id": 0}, headers=bearer(write_token)
    ).status_code == 422


def test_renaming_a_site_onto_a_sibling_is_a_409(client, archive, central, session, write_token) -> None:  # noqa: ANN001, ARG001
    session.add(Site(system_id=central["system_id"], name="Eastside"))
    session.commit()

    clash = client.patch(
        f"/api/sites/{central['id']}", json={"name": "Eastside"}, headers=bearer(write_token)
    )
    assert clash.status_code == 409
    assert "already has a site named" in clash.json()["detail"]


def test_a_name_used_on_another_system_is_fine(client, archive, central, twin_named_site, write_token) -> None:  # noqa: ANN001, ARG001
    """``sites`` is unique per system, so two systems may both have a ``Central``."""
    response = client.patch(
        f"/api/sites/{twin_named_site['site_id']}",
        json={"name": "Central"},
        headers=bearer(write_token),
    )
    assert response.status_code == 200


def test_patching_a_site_needs_a_write_scope(client, archive, central, read_token) -> None:  # noqa: ANN001, ARG001
    # An unauthenticated browser-shaped call is stopped by the CSRF guard before the
    # scope check ever runs; a token without `write` is stopped by the scope check.
    assert client.patch(f"/api/sites/{central['id']}", json={"notes": "x"}).status_code == 403
    denied = client.patch(
        f"/api/sites/{central['id']}", json={"notes": "x"}, headers=bearer(read_token)
    )
    assert denied.status_code == 403
    assert client.patch(
        "/api/sites/999999", json={"notes": "x"}, headers=bearer(read_token)
    ).status_code == 403


# --------------------------------------------------------------------------------------
# DELETE
# --------------------------------------------------------------------------------------


def test_an_unreferenced_site_can_be_deleted(client, archive, twin_named_site, admin_token, session) -> None:  # noqa: ANN001, ARG001
    site_id = twin_named_site["site_id"]
    response = client.delete(f"/api/sites/{site_id}", headers=bearer(admin_token))
    assert response.status_code == 204
    assert session.get(Site, site_id) is None

    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    deleted = [row for row in events if row["kind"] == "site_deleted"]
    assert deleted and "Lakeside Metro / Central" in deleted[0]["message"]


def test_a_site_holding_calls_cannot_be_deleted(client, archive, central, admin_token, session) -> None:  # noqa: ANN001, ARG001
    response = client.delete(f"/api/sites/{central['id']}", headers=bearer(admin_token))
    assert response.status_code == 409
    assert "calls" in response.json()["detail"]
    # Refused means refused: the site and its calls are all still there.
    assert session.get(Site, central["id"]) is not None
    assert session.scalar(
        select(Call.id).where(Call.site_id == central["id"]).limit(1)
    ) is not None


def test_an_observation_alone_blocks_a_delete(client, archive, twin_named_site, admin_token, session) -> None:  # noqa: ANN001, ARG001
    session.add(
        RecorderObservation(
            recorder="trunkd",
            observed_at=datetime.now(UTC),
            system_id=twin_named_site["system_id"],
            site_id=twin_named_site["site_id"],
            payload={"site": {"label": "Central"}},
        )
    )
    session.commit()

    response = client.delete(f"/api/sites/{twin_named_site['site_id']}", headers=bearer(admin_token))
    assert response.status_code == 409
    assert "recorder_observations" in response.json()["detail"]


def test_deleting_a_site_is_admin_only(client, archive, twin_named_site, write_token) -> None:  # noqa: ANN001, ARG001
    assert client.delete(f"/api/sites/{twin_named_site['site_id']}").status_code == 403
    denied = client.delete(
        f"/api/sites/{twin_named_site['site_id']}", headers=bearer(write_token)
    )
    assert denied.status_code == 403
    assert client.delete("/api/sites/999999", headers=bearer(write_token)).status_code == 403


# --------------------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------------------


@pytest.fixture
def duplicate_site(session, archive, client):  # noqa: ANN001, ARG001
    """A second row for the same tower, as a rename or a respelling would leave it."""
    central = session.scalars(select(Site).where(Site.name == "Central")).one()
    duplicate = Site(
        system_id=central.system_id, name="Central (old)", site_number=1, nac=0x34, description="dup"
    )
    session.add(duplicate)
    session.flush()
    session.add_all(
        [
            SiteFrequency(
                site_id=duplicate.id,
                frequency_hz=771_056_250,
                role="primary_control",
                source="observed",
            ),
            SiteFrequency(
                site_id=central.id,
                frequency_hz=771_056_250,
                role="primary_control",
                source="observed",
            ),
            SiteFrequency(
                site_id=duplicate.id, frequency_hz=772_306_250, role="voice", source="observed"
            ),
            AdjacentSite(
                site_id=duplicate.id,
                system_id_p25=39,
                rfss=1,
                site_number=4,
                last_seen_at=datetime.now(UTC),
            ),
            RecorderObservation(
                recorder="trunkd",
                observed_at=datetime.now(UTC),
                system_id=central.system_id,
                site_id=duplicate.id,
                payload={"site": {"label": "Central"}},
            ),
        ]
    )
    # Half the archive's calls were filed under the duplicate.
    moved = list(session.scalars(select(Call).where(Call.site_id == central.id).limit(3)))
    for call in moved:
        call.site_id = duplicate.id
    session.commit()
    return {"keep": central.id, "drop": duplicate.id, "calls": [c.id for c in moved]}


def test_merging_moves_everything_and_deletes_the_source(client, duplicate_site, admin_token, session) -> None:  # noqa: ANN001
    response = client.post(
        f"/api/sites/{duplicate_site['drop']}/merge-into/{duplicate_site['keep']}",
        headers=bearer(admin_token),
    )
    assert response.status_code == 200
    body = response.json()

    assert body["source"]["id"] == duplicate_site["drop"]
    assert body["target"]["id"] == duplicate_site["keep"]
    assert body["moved"]["calls"] == 3
    assert body["moved"]["recorder_observations"] == 1
    assert body["moved"]["adjacent_sites"] == 1
    # The control channel both rows had heard is dropped rather than moved; the voice
    # frequency only the duplicate heard comes across.
    assert body["moved"]["site_frequencies_dropped"] == 1
    assert body["moved"]["site_frequencies"] == 1

    session.expire_all()
    assert session.get(Site, duplicate_site["drop"]) is None
    for call_id in duplicate_site["calls"]:
        assert session.get(Call, call_id).site_id == duplicate_site["keep"]
    frequencies = session.scalars(
        select(SiteFrequency.frequency_hz).where(SiteFrequency.site_id == duplicate_site["keep"])
    ).all()
    assert sorted(frequencies) == [771_056_250, 772_306_250]


def test_a_merge_fills_the_targets_blanks_and_never_overwrites(client, duplicate_site, admin_token, session) -> None:  # noqa: ANN001
    target = session.get(Site, duplicate_site["keep"])
    target.description = "the configured Central"
    target.nac = None
    session.commit()

    client.post(
        f"/api/sites/{duplicate_site['drop']}/merge-into/{duplicate_site['keep']}",
        headers=bearer(admin_token),
    )
    session.expire_all()
    kept = session.get(Site, duplicate_site["keep"])
    # Its own description survives; the blank NAC is filled from the row that went.
    assert kept.description == "the configured Central"
    assert kept.nac == 0x34


def test_merging_across_systems_is_a_409(client, archive, central, twin_named_site, admin_token, session) -> None:  # noqa: ANN001, ARG001
    response = client.post(
        f"/api/sites/{central['id']}/merge-into/{twin_named_site['site_id']}",
        headers=bearer(admin_token),
    )
    assert response.status_code == 409
    assert "refile-calls" in response.json()["detail"]
    assert session.get(Site, central["id"]) is not None


def test_merging_a_site_into_itself_is_a_409(client, archive, central, admin_token) -> None:  # noqa: ANN001, ARG001
    response = client.post(
        f"/api/sites/{central['id']}/merge-into/{central['id']}", headers=bearer(admin_token)
    )
    assert response.status_code == 409
    assert "into itself" in response.json()["detail"]


def test_merging_an_unknown_site_is_a_404(client, archive, central, admin_token) -> None:  # noqa: ANN001, ARG001
    assert (
        client.post(f"/api/sites/999999/merge-into/{central['id']}", headers=bearer(admin_token))
        .status_code
        == 404
    )
    assert (
        client.post(f"/api/sites/{central['id']}/merge-into/999999", headers=bearer(admin_token))
        .status_code
        == 404
    )


def test_a_merge_is_admin_only_and_is_logged(client, duplicate_site, admin_token, write_token) -> None:  # noqa: ANN001
    path = f"/api/sites/{duplicate_site['drop']}/merge-into/{duplicate_site['keep']}"
    assert client.post(path).status_code == 403
    assert client.post(path, headers=bearer(write_token)).status_code == 403

    assert client.post(path, headers=bearer(admin_token)).status_code == 200
    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    merged = [row for row in events if row["kind"] == "site_merged"]
    assert merged
    assert "Example System / Central (old)" in merged[0]["message"]
    assert merged[0]["meta"]["moved"]["calls"] == 3


def test_site_frequencies_carry_the_export_flags(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """The conventional-channel twin of the talkgroup checkboxes, and only they are editable."""
    site = session.scalars(select(Site)).first()
    row = SiteFrequency(
        site_id=site.id,
        frequency_hz=154_265_000,
        role="voice",
        source="observed",
        channel_ref="1-640",
    )
    session.add(row)
    session.commit()

    listing = client.get(f"/api/sites/{site.id}/frequencies")
    assert listing.status_code == 200
    entry = next(item for item in listing.json() if item["id"] == row.id)
    assert entry["export_broadcastify"] is True
    assert entry["export_openmhz"] is True

    patched = client.patch(
        f"/api/site-frequencies/{row.id}",
        json={"export_openmhz": False},
        headers=bearer(write_token),
    )
    assert patched.status_code == 200
    assert patched.json()["export_openmhz"] is False
    assert patched.json()["export_broadcastify"] is True
    # The observed half of the row is not writable: an unknown key is simply not applied.
    assert patched.json()["frequency_hz"] == 154_265_000

    assert client.get("/api/sites/999999/frequencies").status_code == 404
    assert (
        client.patch(
            "/api/site-frequencies/999999",
            json={"export_openmhz": False},
            headers=bearer(write_token),
        ).status_code
        == 404
    )


def test_editing_a_site_frequency_needs_write(client, session, archive) -> None:  # noqa: ANN001, ARG001
    site = session.scalars(select(Site)).first()
    row = SiteFrequency(site_id=site.id, frequency_hz=155_100_000, role="voice", source="observed")
    session.add(row)
    session.commit()

    response = client.patch(
        f"/api/site-frequencies/{row.id}", json={"export_openmhz": False}
    )
    assert response.status_code in (401, 403)
