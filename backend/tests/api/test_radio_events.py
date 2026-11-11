"""Radio events end to end: the batch push, the fold, the reads (docs/RADIO_EVENTS.md).

The scenario is the one docs/NON-VOICE-DATA.md §3.1 measured on the county control channel:
a radio registers, affiliates to a talkgroup and later de-registers, a foreign radio shows
up fully qualified, a vendor message nobody has modeled arrives, and the recorder resends
the whole batch because it could not tell whether the first attempt landed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Radio, RadioAffiliation, RadioEvent, RecorderObservation, Site, Talkgroup
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

SYSTEM = {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52}
SITE = {"label": "Central", "rfss": 10, "site": 1, "lra": 10}


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


def event(**overrides) -> dict:  # noqa: ANN003
    base = {
        "at": NOW.isoformat(),
        "kind": "unit_registration",
        "opcode": 44,
        "mfid": 0,
        "radio_id": 5201234,
        "talkgroup": None,
        "result": "accept",
    }
    base.update(overrides)
    return base


def batch(*events: dict, site: dict | None = SITE) -> dict:
    return {
        "recorder": "trunkd",
        "recorder_version": "0.0.1",
        "system": SYSTEM,
        "site": site,
        "events": list(events),
    }


def post(client, token: str, document: dict):  # noqa: ANN001, ANN201
    return client.post("/api/recorder/events", json=document, headers=bearer(token))


# --------------------------------------------------------------------------------- auth ---


def test_events_need_a_token_with_the_upload_scope(client, session, read_token, upload_token) -> None:  # noqa: ANN001, ARG001
    document = batch(event())

    assert client.post("/api/recorder/events", json=document).status_code == 401
    assert post(client, "kup_not-a-token", document).status_code == 401
    assert post(client, read_token, document).status_code == 403
    assert post(client, upload_token, document).status_code == 200


# ------------------------------------------------------------------------------- storing ---


def test_a_batch_is_stored_and_a_retry_is_free(client, session, upload_token) -> None:  # noqa: ANN001
    document = batch(
        event(),
        event(at=(NOW + timedelta(seconds=2)).isoformat(), kind="group_affiliation", opcode=40, talkgroup=1101),
        event(at=(NOW + timedelta(seconds=9)).isoformat(), kind="unit_deregistration", opcode=47, result=None),
    )

    first = post(client, upload_token, document).json()
    assert (first["stored"], first["duplicates"], first["rejected"]) == (3, 0, 0)
    assert first["site_id"] is not None

    # The recorder spools batches and resends what it could not confirm: the resend must
    # store nothing and fail nothing.
    again = post(client, upload_token, document).json()
    assert (again["stored"], again["duplicates"], again["rejected"]) == (0, 3, 0)
    assert len(session.scalars(select(RadioEvent)).all()) == 3


def test_an_unknown_kind_is_stored_as_other_and_never_costs_the_batch(client, session, upload_token) -> None:  # noqa: ANN001
    body = post(
        client,
        upload_token,
        batch(
            event(kind="harris_osp_unknown_15", opcode=15, mfid=164, meta={"hex": "8fa40a57"}),
            event(at=(NOW + timedelta(seconds=1)).isoformat(), kind="affiliation", opcode=40, talkgroup=1101),
            {"kind": "unit_registration", "radio_id": 1},  # no timestamp at all
            {"at": NOW.isoformat(), "kind": "unit_registration", "radio_id": 1, "opcode": 44},
        ),
    ).json()

    assert (body["stored"], body["rejected"]) == (3, 1)
    assert body["errors"] and "timestamp" in body["errors"][0]

    kinds = {e.kind: e for e in session.scalars(select(RadioEvent))}
    assert set(kinds) == {"other", "group_affiliation", "unit_registration"}
    # ``other`` keeps everything needed to reclassify it later without a new capture.
    assert kinds["other"].opcode == 15
    assert kinds["other"].mfid == 164
    assert kinds["other"].meta["kind_reported"] == "harris_osp_unknown_15"
    # An alias the recorder spells its own way still lands on the modeled kind.
    assert kinds["group_affiliation"].opcode == 40


def test_a_batch_naming_no_system_is_refused_without_an_error(client, upload_token) -> None:  # noqa: ANN001
    response = post(client, upload_token, batch(event(), site=None) | {"system": {"label": "  "}})
    assert response.status_code == 200
    assert response.json()["stored"] == 0
    assert response.json()["rejected"] == 1


# --------------------------------------------------------------------------------- folding ---


def test_events_fold_into_radios_without_touching_call_count(client, session, upload_token) -> None:  # noqa: ANN001
    post(
        client,
        upload_token,
        batch(
            event(),
            event(at=(NOW + timedelta(minutes=1)).isoformat(), kind="group_affiliation", opcode=40, talkgroup=1101),
            event(at=(NOW + timedelta(hours=8)).isoformat(), kind="unit_deregistration", opcode=47, result=None),
        ),
    )

    radio = session.scalars(select(Radio).where(Radio.radio_id == 5201234)).one()
    assert radio.call_count == 0, "presence is not a transmission"
    assert radio.first_seen_at == NOW
    assert radio.last_seen_at == NOW + timedelta(hours=8)
    assert radio.last_registered_at == NOW
    assert radio.last_deregistered_at == NOW + timedelta(hours=8)

    # The talkgroup an affiliation names is created, and its call_count stays at zero too.
    talkgroup = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1101)).one()
    assert talkgroup.call_count == 0


def test_an_accepted_affiliation_writes_a_row_a_denied_one_does_not(client, session, upload_token) -> None:  # noqa: ANN001
    post(
        client,
        upload_token,
        batch(
            event(kind="group_affiliation", opcode=40, talkgroup=1101, result="accept"),
            event(
                at=(NOW + timedelta(seconds=5)).isoformat(),
                kind="group_affiliation",
                opcode=40,
                talkgroup=1999,
                result="deny",
            ),
        ),
    )

    rows = {
        session.get(Talkgroup, a.talkgroup_id_fk).tgid: a
        for a in session.scalars(select(RadioAffiliation))
    }
    assert set(rows) == {1101}, "a denied affiliation is a fact about the site, not presence"
    affiliation = rows[1101]
    assert affiliation.source == "affiliation"
    assert affiliation.last_affiliated_at == NOW
    assert affiliation.call_count == 0
    assert affiliation.last_heard_at is None


def test_affiliation_wins_over_call_and_never_goes_back(client, session, upload_token) -> None:  # noqa: ANN001
    """A pair backed by both sources keeps ``affiliation``: it is the stronger fact."""
    from app.ingest.service import touch_affiliation, upsert_radio, upsert_system, upsert_talkgroup

    system_id = upsert_system(session, "Example System", protocol=None, wacn=101010)
    radio_pk = upsert_radio(
        session, system_id, radio_id=5201234, wacn=0, home_system=0, seen_at=NOW - timedelta(days=1)
    )
    tg = upsert_talkgroup(session, system_id, 1101, alias="EC LAW 1", seen_at=NOW - timedelta(days=1))
    touch_affiliation(session, radio_pk, tg.id, NOW - timedelta(days=1))
    session.commit()

    before = session.get(RadioAffiliation, session.scalar(select(RadioAffiliation.id)))
    assert (before.source, before.call_count) == ("call", 1)

    post(client, upload_token, batch(event(kind="group_affiliation", opcode=40, talkgroup=1101)))
    session.expire_all()

    after = session.scalars(select(RadioAffiliation)).one()
    assert after.source == "affiliation"
    assert after.call_count == 1, "the affiliation must not have counted as a transmission"
    assert after.last_heard_at == NOW - timedelta(days=1)
    assert after.last_affiliated_at == NOW


def test_wacn_and_home_system_are_filled_only_where_they_are_empty(client, session, upload_token) -> None:  # noqa: ANN001
    """A qualified sighting fills a bare row; a bare one afterwards joins it, not a new row."""
    post(client, upload_token, batch(event(radio_id=5701025)))
    radio = session.scalars(select(Radio).where(Radio.radio_id == 5701025)).one()
    assert (radio.wacn, radio.home_system_id) == (0, 0), "nothing said where it is from yet"

    post(
        client,
        upload_token,
        batch(
            event(
                at=(NOW + timedelta(minutes=1)).isoformat(),
                radio_id=5701025,
                radio_wacn=101010,
                radio_system=57,
            )
        ),
    )
    session.expire_all()
    radio = session.scalars(select(Radio).where(Radio.radio_id == 5701025)).one()
    assert (radio.id, radio.wacn, radio.home_system_id) == (radio.id, 101010, 57)

    # LC 9 rides some messages and not others, so the *next* bare sighting must find the
    # row it just qualified rather than filing the unit a second time.
    post(client, upload_token, batch(event(at=(NOW + timedelta(minutes=2)).isoformat(), radio_id=5701025)))
    session.expire_all()
    same = session.scalars(select(Radio).where(Radio.radio_id == 5701025)).one()
    assert same.id == radio.id
    assert same.last_seen_at == NOW + timedelta(minutes=2)


def test_a_different_home_system_is_a_different_radio(client, session, upload_token) -> None:  # noqa: ANN001
    """101010.57.5701025 and 101010.82.5701025 are two units; the fold never merges them."""
    post(client, upload_token, batch(event(radio_id=5701025, radio_wacn=101010, radio_system=57)))
    post(
        client,
        upload_token,
        batch(
            event(
                at=(NOW + timedelta(minutes=5)).isoformat(),
                radio_id=5701025,
                radio_wacn=101010,
                radio_system=82,
            )
        ),
    )
    homes = sorted(
        session.scalars(select(Radio.home_system_id).where(Radio.radio_id == 5701025)).all()
    )
    assert homes == [57, 82]


# ----------------------------------------------------------------------------------- read ---


def test_the_radio_detail_carries_presence_and_a_timeline(client, session, upload_token) -> None:  # noqa: ANN001
    post(
        client,
        upload_token,
        batch(
            event(),
            event(at=(NOW + timedelta(minutes=1)).isoformat(), kind="group_affiliation", opcode=40, talkgroup=1101),
            event(at=(NOW + timedelta(minutes=2)).isoformat(), kind="sndcp_grant", opcode=20, frequency_hz=858_987_500),
        ),
    )
    radio_pk = session.scalars(select(Radio.id).where(Radio.radio_id == 5201234)).one()

    detail = client.get(f"/api/radios/{radio_pk}").json()
    assert detail["event_count"] == 3
    assert [e["kind"] for e in detail["events"]] == ["sndcp_grant", "group_affiliation", "unit_registration"]
    assert detail["events"][0]["frequency_hz"] == 858_987_500
    assert detail["events"][0]["site_name"] == "Central"
    assert detail["presence"]["last_registered_at"].startswith("2026-08-31T12:00:00")
    assert detail["presence"]["last_deregistered_at"] is None
    assert detail["presence"]["affiliations"][0]["source"] == "affiliation"
    assert detail["presence"]["affiliations"][0]["tgid"] == 1101

    page = client.get(f"/api/radios/{radio_pk}/events", params={"kind": "group_affiliation"}).json()
    assert page["total"] == 1
    assert page["items"][0]["talkgroup_tgid"] == 1101

    window = client.get(
        f"/api/radios/{radio_pk}/events",
        params={"from": (NOW + timedelta(minutes=2)).isoformat()},
    ).json()
    assert window["total"] == 1 and window["items"][0]["kind"] == "sndcp_grant"

    assert client.get("/api/radios/999999/events").status_code == 404


def test_the_site_summary_counts_kinds_results_and_registrations(client, session, upload_token) -> None:  # noqa: ANN001
    now = datetime.now(UTC).replace(microsecond=0)
    post(
        client,
        upload_token,
        batch(
            event(at=(now - timedelta(minutes=30)).isoformat()),
            event(at=(now - timedelta(minutes=29)).isoformat(), radio_id=5201235),
            event(
                at=(now - timedelta(minutes=28)).isoformat(),
                kind="group_affiliation",
                opcode=40,
                talkgroup=1101,
                result="deny",
            ),
            event(at=(now - timedelta(days=5)).isoformat(), radio_id=5209999),
        ),
    )
    site_id = session.scalar(select(RadioEvent.site_id))

    summary = client.get(f"/api/sites/{site_id}/radio-events/summary", params={"hours": 24}).json()
    assert summary["site_name"] == "Central"
    assert summary["total"] == 3, "the five-day-old event is outside the 24-hour window"
    assert {row["kind"]: row["count"] for row in summary["by_kind"]} == {
        "unit_registration": 2,
        "group_affiliation": 1,
    }
    assert {row["key"]: row["count"] for row in summary["by_result"]} == {"accept": 2, "deny": 1}
    assert sum(point["count"] for point in summary["registrations_per_hour"]) == 2
    assert {r["radio_id"] for r in summary["top_radios"]} == {5201234, 5201235}

    # The unlisted `/api/directory/sites/...` alias answers the same thing.
    alias = client.get(f"/api/directory/sites/{site_id}/radio-events/summary").json()
    assert alias["total"] == summary["total"]
    assert client.get("/api/sites/999999/radio-events/summary").status_code == 404


# ---------------------------------------------------------------------------- message mix ---


def observation(observed_at: datetime, mix: dict[str, int], **extra) -> dict:  # noqa: ANN003
    return {
        "recorder": "trunkd",
        "observed_at": observed_at.isoformat(),
        "window_s": 300,
        "system": SYSTEM,
        "site": SITE,
        "control": {"frequency_hz": 851_050_000, "channel": "1-3", "tsbk_rate": 26.4},
        "message_mix": mix,
        **extra,
    }


def test_the_message_mix_aggregates_windows(client, session, upload_token) -> None:  # noqa: ANN001
    now = datetime.now(UTC).replace(microsecond=0)
    first = now - timedelta(hours=2)
    second = now - timedelta(hours=1)
    for at, mix, exemplars in (
        (
            first,
            {"0:2": 695, "164:15": 400, "0/44": 12},
            [{"mfid": 164, "opcode": 15, "count": 400, "exemplar": "8fa40a57000a00000000edda"}],
        ),
        (
            second,
            {"0:2": 300, "164:15": 88},
            [{"mfid": 164, "opcode": 15, "count": 88, "exemplar": "8fa40a57000a00000000ffff"}],
        ),
        (now - timedelta(days=30), {"0:2": 1}, []),
    ):
        assert (
            client.post(
                "/api/recorder/observations",
                json=observation(
                    at,
                    mix,
                    unknown_exemplars=exemplars,
                    link_control_mix={"0:9": 73},
                    data_channel={"grants": 19, "channels": [858_987_500]},
                    lsd_nonzero=5,
                ),
                headers=bearer(upload_token),
            ).status_code
            == 200
        )

    site_id = session.scalar(select(Site.id))
    body = client.get(f"/api/sites/{site_id}/message-mix", params={"days": 7}).json()

    assert body["windows"] == 2, "the 30-day-old window is outside the range"
    entries = {row["key"]: row for row in body["messages"]}
    assert entries["0:2"]["count"] == 995
    assert entries["0:2"]["windows"] == 2
    # ``0/44`` and ``0:44`` are the same message; a census key must not be lost to
    # punctuation (the histograms in docs/NON-VOICE-DATA.md use a slash).
    assert entries["0:44"]["count"] == 12
    harris = entries["164:15"]
    assert harris["count"] == 488
    assert harris["exemplar"] == "8fa40a57000a00000000ffff", "the newest exemplar wins"
    assert harris["first_seen_at"].startswith(first.isoformat()[:13])
    assert body["total"] == 995 + 488 + 12
    assert [row["key"] for row in body["link_control"]] == ["0:9"]

    # The five new census keys are stored verbatim on the observation, not normalized away.
    payload = session.scalars(
        select(RecorderObservation).order_by(RecorderObservation.observed_at.desc())
    ).first().payload
    assert payload["data_channel"] == {"grants": 19, "channels": [858_987_500]}
    assert payload["lsd_nonzero"] == 5

    assert client.get("/api/sites/999999/message-mix").status_code == 404
