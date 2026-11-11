"""Which recorder *instance* said a thing — stored on both pushes, filterable on both reads.

``recorder`` has always named the software ("trunkd"); it never named the machine. Two boxes
watching one site therefore wrote into the same rows and could not be told apart afterwards,
which is exactly the question an operator asks when two recorders disagree. ``instance`` is
that name, and these tests are about it surviving the round trip on both paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


SYSTEM = {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52}
SITE = {"label": "Central", "site": 3, "rfss": 1}


def _observation(instance: str | None, **extra) -> dict:  # noqa: ANN003
    body = {
        "recorder": "trunkd",
        "observed_at": datetime.now(UTC).isoformat(),
        "window_s": 300,
        "system": SYSTEM,
        "site": SITE,
        "control": {"frequency_hz": 851_050_000, "channel": "1-638"},
        **extra,
    }
    if instance is not None:
        body["instance"] = instance
    return body


def _events(instance: str | None, radio_id: int, count: int = 2) -> dict:
    now = datetime.now(UTC)
    body = {
        "recorder": "trunkd",
        "system": SYSTEM,
        "site": SITE,
        "events": [
            {
                "at": (now - timedelta(seconds=i)).isoformat(),
                "kind": "unit_registration",
                "opcode": 44,
                "radio_id": radio_id,
                # The identity key does not include the instance, so two recorders reporting
                # *the same* message collapse to one row.  Different radios keep them apart.
                "result": "accept",
            }
            for i in range(count)
        ],
    }
    if instance is not None:
        body["instance"] = instance
    return body


# --------------------------------------------------------------------- observations ---


def test_an_observation_stores_its_instance(client, upload_token, session) -> None:
    from sqlalchemy import select

    from app.models import RecorderObservation

    resp = client.post(
        "/api/recorder/observations", json=_observation("trunkd-garage"), headers=bearer(upload_token)
    )
    assert resp.status_code == 200, resp.text
    session.expire_all()
    row = session.scalars(select(RecorderObservation)).one()
    assert row.instance == "trunkd-garage"
    # …and it is in the stored document too, because the payload is kept verbatim.
    assert row.payload["instance"] == "trunkd-garage"


def test_an_observation_without_an_instance_stores_null(client, upload_token, session) -> None:
    """An older recorder sends none, and a guess would be worse than the honest null."""
    from sqlalchemy import select

    from app.models import RecorderObservation

    assert client.post(
        "/api/recorder/observations", json=_observation(None), headers=bearer(upload_token)
    ).status_code == 200
    session.expire_all()
    assert session.scalars(select(RecorderObservation)).one().instance is None


def test_message_mix_filters_by_instance(client, upload_token, admin_token) -> None:
    """Two boxes on one site are compared rather than summed."""
    client.post(
        "/api/recorder/observations",
        json=_observation("trunkd-garage", message_mix={"0:2": 100, "164:15": 10}),
        headers=bearer(upload_token),
    )
    resp = client.post(
        "/api/recorder/observations",
        json=_observation("trunkd-tower", message_mix={"0:2": 7}),
        headers=bearer(upload_token),
    )
    site_id = resp.json()["site_id"]

    both = client.get(f"/api/sites/{site_id}/message-mix").json()
    assert both["instance"] is None
    assert both["windows"] == 2
    assert next(m for m in both["messages"] if m["key"] == "0:2")["count"] == 107

    garage = client.get(f"/api/sites/{site_id}/message-mix", params={"instance": "trunkd-garage"}).json()
    assert garage["instance"] == "trunkd-garage"
    assert garage["windows"] == 1
    assert next(m for m in garage["messages"] if m["key"] == "0:2")["count"] == 100

    # A name nobody reported matches nothing rather than everything.
    assert client.get(
        f"/api/sites/{site_id}/message-mix", params={"instance": "nobody"}
    ).json()["windows"] == 0


# -------------------------------------------------------------------- radio events ---


def test_a_batch_stamps_its_instance_on_every_row(client, upload_token, session) -> None:
    from sqlalchemy import select

    from app.models import RadioEvent

    resp = client.post(
        "/api/recorder/events", json=_events("trunkd-garage", 5_200_001, count=3), headers=bearer(upload_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] == 3
    session.expire_all()
    rows = session.scalars(select(RadioEvent)).all()
    assert len(rows) == 3
    assert {r.instance for r in rows} == {"trunkd-garage"}


def test_a_batch_without_an_instance_stores_null(client, upload_token, session) -> None:
    from sqlalchemy import select

    from app.models import RadioEvent

    client.post("/api/recorder/events", json=_events(None, 5_200_002), headers=bearer(upload_token))
    session.expire_all()
    assert {r.instance for r in session.scalars(select(RadioEvent)).all()} == {None}


def test_a_radio_timeline_filters_by_instance(client, upload_token, session) -> None:
    from sqlalchemy import select

    from app.models import Radio

    client.post(
        "/api/recorder/events", json=_events("trunkd-garage", 5_200_010, count=2), headers=bearer(upload_token)
    )
    client.post(
        "/api/recorder/events", json=_events("trunkd-tower", 5_200_010, count=3), headers=bearer(upload_token)
    )
    session.expire_all()
    radio = session.scalars(select(Radio).where(Radio.radio_id == 5_200_010)).one()

    everything = client.get(f"/api/radios/{radio.id}/events").json()
    assert everything["total"] == 5

    garage = client.get(f"/api/radios/{radio.id}/events", params={"instance": "trunkd-garage"}).json()
    assert garage["total"] == 2
    assert {row["instance"] for row in garage["items"]} == {"trunkd-garage"}

    tower = client.get(f"/api/radios/{radio.id}/events", params={"instance": "trunkd-tower"}).json()
    assert tower["total"] == 3

    # A name nobody reported matches nothing — including the rows that carry no instance.
    assert client.get(
        f"/api/radios/{radio.id}/events", params={"instance": "nobody"}
    ).json()["total"] == 0


def test_the_site_summary_filters_by_instance(client, upload_token) -> None:
    client.post(
        "/api/recorder/events", json=_events("trunkd-garage", 5_200_020, count=2), headers=bearer(upload_token)
    )
    resp = client.post(
        "/api/recorder/events", json=_events("trunkd-tower", 5_200_021, count=3), headers=bearer(upload_token)
    )
    site_id = resp.json()["site_id"]

    both = client.get(f"/api/sites/{site_id}/radio-events/summary").json()
    assert both["instance"] is None
    assert both["total"] == 5

    tower = client.get(
        f"/api/sites/{site_id}/radio-events/summary", params={"instance": "trunkd-tower"}
    ).json()
    assert tower["instance"] == "trunkd-tower"
    assert tower["total"] == 3
    assert sum(k["count"] for k in tower["by_kind"]) == 3


def test_a_channel_with_no_frequency_yet_does_not_fail_the_report(client, upload_token, session) -> None:
    """A site can announce a secondary control channel before the IDEN_UP for its band.

    The recorder then knows the channel number and not the frequency, and sends ``null``.
    That entry is skipped; the other eleven, and the rest of the report, still land.  It
    used to be a 422 for the whole document, every five minutes, until the band was heard.
    """
    from sqlalchemy import select

    from app.models import RecorderObservation

    document = _observation(
        "trunkd-garage",
        secondary_control=[
            {"frequency_hz": 851_050_000, "channel": "1-3", "service_class": ["backup_control"]},
            {"frequency_hz": None, "channel": "7-12", "service_class": ["backup_control"]},
        ],
        traffic=[{"frequency_hz": None, "channel": "7-40", "grants": 3, "calls": 1}],
    )
    resp = client.post("/api/recorder/observations", json=document, headers=bearer(upload_token))
    assert resp.status_code == 200, resp.text
    session.expire_all()
    row = session.scalars(select(RecorderObservation)).one()
    assert [c["channel"] for c in row.payload["secondary_control"]] == ["1-3", "7-12"]


def test_the_services_bitmap_is_kept_in_the_stored_document(client, upload_token, session) -> None:
    """SYS_SVC_BCAST reached the API from the first day and was dropped on re-serialisation."""
    from sqlalchemy import select

    from app.models import RecorderObservation

    services = {
        "available": ["composite", "data", "voice"],
        "supported": ["composite", "data", "voice", "registration"],
        "available_raw": "0x2c2c68",
        "supported_raw": "0x2c6801",
        "request_priority": 3,
    }
    document = _observation("trunkd-garage", services=services)
    resp = client.post("/api/recorder/observations", json=document, headers=bearer(upload_token))
    assert resp.status_code == 200, resp.text
    session.expire_all()
    row = session.scalars(select(RecorderObservation)).one()
    assert row.payload["services"]["available_raw"] == "0x2c2c68"
    assert row.payload["services"]["available"] == ["composite", "data", "voice"]
