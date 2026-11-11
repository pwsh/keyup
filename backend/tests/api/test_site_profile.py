"""``GET /api/sites/{id}/profile`` — everything known, and everything not, about one site.

Every fact here arrives the way production's does: through
``POST /api/recorder/observations``.  The profile is a *fold* of stored windows, so a test
that built ``recorder_observations`` rows by hand would be testing the fold against its own
idea of the payload rather than against the one the recorder posts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import RrSite, Site, System
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


def window(**overrides) -> dict:  # noqa: ANN003
    """A full five-minute window for Central, of the shape trunkd posts today."""
    document = {
        "recorder": "trunkd",
        "recorder_version": "0.9.0",
        "instance": "laptop-01",
        "observed_at": NOW.isoformat(),
        "window_s": 300,
        "system": {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52},
        "site": {"label": "Central", "rfss": 10, "site": 1, "lra": 10},
        "control": {"frequency_hz": 851_050_000, "channel": "1-3", "tsbk_rate": 32.1},
        "services": {
            "available": ["Composite", "Group Voice", "Data", "Registration"],
            "supported": ["Composite", "Group Voice", "Data", "Registration", "Encryption"],
            "available_raw": "0x2F1000",
            "supported_raw": "0x2F1400",
            "request_priority": 2,
        },
        "secondary_control": [
            {"frequency_hz": 851_650_000, "channel": "1-27", "service_class": ["control"]},
            {"frequency_hz": None, "channel": "3-14", "service_class": ["control"]},
        ],
        "configured_control": [851_050_000, 851_650_000],
        "bands": [
            {
                "id": 1,
                "base_hz": 851_006_250,
                "spacing_hz": 6_250,
                "offset_hz": -45_000_000,
                "bandwidth_hz": 12_500,
                "tdma": False,
            }
        ],
        "traffic": [
            {"frequency_hz": 851_200_000, "channel": "1-9", "grants": 4, "calls": 3},
            {"frequency_hz": 851_300_000, "channel": "1-13", "grants": 19, "calls": 17},
        ],
        "adjacent_sites": [
            {
                "system_id": 52,
                "rfss": 10,
                "site": 4,
                "lra": 10,
                "control_frequency_hz": 852_000_000,
                "channel": "1-2",
                "flags": {"valid": True},
            },
            {
                "system_id": 52,
                "rfss": 10,
                "site": 7,
                "lra": 10,
                "control_frequency_hz": 852_500_000,
                "channel": "1-6",
                "flags": {},
            },
        ],
        "message_mix": {"0:2": 695, "164:15": 488},
        "unknown_exemplars": [{"mfid": 164, "opcode": 15, "count": 488, "exemplar": "a4ff00"}],
        "data_channel": {"grants": 19, "pages": 2, "pdus": 3, "channels": [858_987_500]},
        "lsd_nonzero": 7,
    }
    document.update(overrides)
    return document


def post(client, token: str, document: dict) -> dict:  # noqa: ANN001
    response = client.post("/api/recorder/observations", json=document, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def central_id(session) -> int:  # noqa: ANN001
    return session.scalars(select(Site).where(Site.name == "Central")).one().id


# --------------------------------------------------------------------------------------
# one window
# --------------------------------------------------------------------------------------


def test_one_observation_becomes_the_whole_profile(client, session, archive, upload_token) -> None:  # noqa: ANN001, ARG001
    post(client, upload_token, window())
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()

    assert profile["site_id"] == site_id
    assert profile["site_name"] == "Central"
    identity = profile["identity"]
    assert identity["system_name"] == "Example System"
    assert identity["system_label"] == "Example System"
    assert identity["wacn"] == 101010
    assert identity["p25_system_id"] == 52
    assert identity["nac"] == 52
    assert identity["nac_hex"] == "0x34"
    assert (identity["rfss"], identity["site_number"], identity["lra"]) == (10, 1, 10)

    control = profile["control"]
    assert control["frequency_hz"] == 851_050_000
    assert control["channel"] == "1-3"
    assert control["tsbk_rate"] == pytest.approx(32.1)
    assert [entry["channel"] for entry in control["secondary_control"]] == ["1-27", "3-14"]
    assert control["secondary_control"][1]["frequency_hz"] is None
    assert control["configured_control"] == [851_050_000, 851_650_000]

    services = profile["services"]
    assert services["available"] == ["Composite", "Group Voice", "Data", "Registration"]
    assert services["supported_not_available"] == ["Encryption"]
    assert services["available_raw"] == "0x2F1000"
    assert services["request_priority"] == 2

    assert [band["id"] for band in profile["bands"]] == [1]
    # busiest first, not the order the recorder listed them in
    assert [row["grants"] for row in profile["traffic"]] == [19, 4]
    assert profile["data_channel"] == {
        "grants": 19,
        "pages": 2,
        "pdus": 3,
        "channels": [858_987_500],
    }
    assert profile["lsd_nonzero"] == 7
    # Both census keys are in the built-in P25 table (GRP_VCH_GRNT_UPD and the Harris
    # beacon), so they arrive named — a site starts named, and "unknown" means nobody
    # anywhere has named it.
    assert profile["message_kinds"] == {"known": 2, "unknown": 0, "noise": 0, "total_seen": 2}
    assert [entry["instance"] for entry in profile["observed_by"]] == ["laptop-01"]
    assert profile["observed_by"][0]["control_frequency_hz"] == 851_050_000


def test_services_appear_on_the_site_list_row(client, session, archive, upload_token) -> None:  # noqa: ANN001, ARG001
    post(client, upload_token, window())
    site_id = central_id(session)

    rows = client.get("/api/sites").json()
    row = next(entry for entry in rows if entry["id"] == site_id)

    assert row["services_available"] == ["Composite", "Group Voice", "Data", "Registration"]
    # And on the single-site route, which fetches it for one row rather than a page.
    assert client.get(f"/api/sites/{site_id}").json()["services_available"] == row["services_available"]


def test_a_site_nobody_decoded_services_for_says_null_not_empty(
    client, session, archive, upload_token
) -> None:  # noqa: ANN001, ARG001
    post(client, upload_token, window(services=None))
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()

    assert profile["services"]["available"] is None
    assert profile["services"]["supported"] is None
    assert client.get("/api/sites").json()[0]["services_available"] is None
    assert any("service bitmap" in line for line in profile["unknowns"])


# --------------------------------------------------------------------------------------
# two boxes on one tower
# --------------------------------------------------------------------------------------


def test_the_newer_instance_wins_field_by_field(client, session, archive, upload_token) -> None:  # noqa: ANN001, ARG001
    """The older box still contributes what the newer one did not see.

    ``signal`` posts later but reports no band plan and no services; the merge must take
    its control channel (it is the current one) while keeping the band plan the other box
    is the only witness to.  The naive "newest window wins" would blank both.
    """
    post(client, upload_token, window())
    post(
        client,
        upload_token,
        window(
            instance="recorder-a",
            observed_at=(NOW + timedelta(minutes=5)).isoformat(),
            control={"frequency_hz": 851_650_000, "channel": "1-27", "tsbk_rate": 30.0},
            services=None,
            bands=[],
            traffic=[],
            secondary_control=[],
        ),
    )
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()

    assert profile["control"]["frequency_hz"] == 851_650_000  # the newer box
    assert [band["id"] for band in profile["bands"]] == [1]  # the older one's band plan
    assert profile["services"]["available"] is not None  # and its services
    assert [entry["instance"] for entry in profile["observed_by"]] == ["recorder-a", "laptop-01"]
    assert profile["observed_by"][0]["tsbk_rate"] == pytest.approx(30.0)


# --------------------------------------------------------------------------------------
# neighbours
# --------------------------------------------------------------------------------------


def test_an_announced_neighbour_is_named_when_keyup_knows_it(
    client, session, archive, upload_token
) -> None:  # noqa: ANN001, ARG001
    post(client, upload_token, window())
    system = session.scalars(select(System).where(System.name == "Example System")).one()
    system.system_id = 52
    session.add(Site(system_id=system.id, name="North Co", rfss=10, site_number=4))
    session.commit()
    site_id = central_id(session)

    neighbours = client.get(f"/api/sites/{site_id}/profile").json()["adjacent_sites"]

    by_number = {entry["site_number"]: entry for entry in neighbours}
    assert by_number[4]["name"] == "North Co"
    assert by_number[4]["known"] is True
    assert by_number[4]["flags"] == {"valid": True}
    assert by_number[4]["source"] == "keyup"
    assert by_number[7]["name"] is None
    assert by_number[7]["known"] is False
    assert by_number[7]["source"] is None


def test_a_neighbour_keyup_never_heard_is_named_from_radioreference(
    client, session, archive, upload_token
) -> None:  # noqa: ANN001, ARG001
    """The name RadioReference has for a tower is worth more than "RFSS 10 site 7".

    Central's neighbours on the example system are West Co and East Co: no recorder
    here will ever hear them, so Keyup has no site row, but the ``rr_sites`` cache the
    channel audit resolves against knows every tower by name.
    """
    post(client, upload_token, window())
    system = session.scalars(select(System).where(System.name == "Example System")).one()
    session.add(
        RrSite(
            system_id=system.id,
            rr_site_id=9007,
            rr_system_id=52,
            rfss=10,
            site_number=7,
            name="West Co Simulcast",
            county="West Co",
            control_frequency_hz=852_500_000,
        )
    )
    session.commit()
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()
    neighbour = next(row for row in profile["adjacent_sites"] if row["site_number"] == 7)

    assert neighbour["name"] == "West Co Simulcast"
    assert neighbour["source"] == "radioreference"
    # RR documenting it is not Keyup knowing it: `known` still means "a site row here".
    assert neighbour["known"] is False
    # And a neighbour that has a name from either source is not an unnamed one.
    assert not any("neighbour(s) match no site" in line for line in profile["unknowns"])


def test_unknowns_names_every_hole(client, session, archive, upload_token) -> None:  # noqa: ANN001, ARG001
    post(
        client,
        upload_token,
        window(
            services=None,
            bands=[],
            configured_control=[],
            adjacent_sites=[],
            # 7:35 is a vendor pair nothing names, seen often enough to be real; 200:9 is
            # the single-decode rubbish that must not be counted as a kind nobody named.
            message_mix={"0:2": 695, "7:35": 9, "200:9": 1},
            unknown_exemplars=[],
        ),
    )
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()
    unknowns = " ".join(profile["unknowns"])

    assert "service bitmap" in unknowns
    assert "band plan" in unknowns
    assert "configured control-channel list" in unknowns
    assert "adjacent sites announced" in unknowns
    assert "1 recurring control-channel message kind(s) nobody has named." in profile["unknowns"]
    assert profile["message_kinds"] == {"known": 1, "unknown": 2, "noise": 1, "total_seen": 3}
    assert "RadioReference" in unknowns  # the site is not mapped
    # A secondary control channel with no frequency is its own line.
    with_secondary = window(services=None, message_mix={"7:35": 9}, unknown_exemplars=[])
    post(client, upload_token, with_secondary)
    later = client.get(f"/api/sites/{site_id}/profile").json()
    assert any("band plan has not been heard" in line for line in later["unknowns"])


def test_an_observation_from_a_binary_with_no_instance_is_not_an_observer(
    client, session, archive, upload_token
) -> None:  # noqa: ANN001, ARG001
    """It still fills the document; it is just not a recorder anyone can name."""
    post(client, upload_token, window(instance=None))
    site_id = central_id(session)

    profile = client.get(f"/api/sites/{site_id}/profile").json()

    assert profile["observed_by"] == []
    assert profile["control"]["frequency_hz"] == 851_050_000  # the window still counted


def test_a_site_with_no_observation_says_so(client, session, archive) -> None:  # noqa: ANN001, ARG001
    system = session.scalars(select(System).where(System.name == "Example System")).one()
    quiet = Site(system_id=system.id, name="Quiet Ridge")
    session.add(quiet)
    session.commit()

    profile = client.get(f"/api/sites/{quiet.id}/profile").json()

    assert profile["unknowns"] == [
        "No recorder has ever posted an observation window for this site."
    ]
    assert profile["observed_by"] == []
    assert profile["services"]["available"] is None


def test_missing_site_is_a_404(client, archive) -> None:  # noqa: ANN001, ARG001
    response = client.get("/api/sites/999999/profile")

    assert response.status_code == 404
    assert "999999" in response.json()["detail"]


def test_a_neighbour_sharing_the_pair_but_not_the_system_is_not_the_keyup_site() -> None:
    """Millbrook (6 / RFSS 10 / site 10) must not resolve to Ashby (92 / 10 / 10)."""
    from types import SimpleNamespace

    from app.rr.audit import resolve_known_site

    ashby = SimpleNamespace(name="Ashby", nac=92)
    index = {(92, 10, 10): ashby, (None, 10, 10): ashby}
    assert resolve_known_site(index, 92, 10, 10) is ashby
    assert resolve_known_site(index, None, 10, 10) is ashby
    assert resolve_known_site(index, 6, 10, 10) is None
