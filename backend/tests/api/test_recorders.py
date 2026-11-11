"""Multi-tuner management: desired-state poll, status push, operator view/edit, occupancy.

The recorder authenticates with an ``upload``-scoped Bearer token (the same one the call
and observation pushes use); the operator routes need ``admin``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    """A raw ``kup_…`` token carrying only the ``upload`` scope — a recorder's credential."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


@pytest.fixture
def captured_events(monkeypatch) -> list[dict]:  # noqa: ANN001
    """Capture every live-bus event the recorder routes emit, in-process."""
    events: list[dict] = []
    from app.api import recorders

    real = recorders.notify

    def spy(db, event, payload):  # noqa: ANN001, ANN202
        events.append({"event": event, **payload})
        return real(db, event, payload)

    monkeypatch.setattr(recorders, "notify", spy)
    return events


# --------------------------------------------------------------------- desired-state ---


def test_desired_state_needs_upload_token(client) -> None:
    # Anonymous is refused; the shape is the recorder token check.
    assert client.get("/api/recorder/desired-state", params={"instance": "rec-1"}).status_code == 401


def test_desired_state_rejects_non_upload_scope(client, read_token) -> None:
    resp = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-1"}, headers=bearer(read_token)
    )
    assert resp.status_code == 403


def test_desired_state_autocreates_recorder(client, upload_token, session) -> None:
    from app.models import Recorder

    resp = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-auto"}, headers=bearer(upload_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    # The recorder block is always present; a null `spool_only` is "keyup has no opinion".
    assert body == {
        "version": 0,
        "tuners": {},
        # Every recorder-level field is null on a recorder nobody has configured: keyup has
        # no opinion about any of them, and the recorder keeps its own TOML throughout.
        "recorder": {
            "version": 0,
            "spool_only": None,
            "event_log_archive": None,
            "link": None,
            # …and no Broadcastify block: the shipped `broadcastify_upload_at` is `keyup`,
            # so the recorder is not asked to upload anything itself.
            "broadcastify": None,
            # …and nothing to skip: every export service ships in `all` mode, so every
            # talkgroup is selected by one and none of them is skippable (§5.6).
            "skip_talkgroups": None,
        },
    }

    # The row appeared on first contact, like a site from its first observation.
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-auto"))
    assert recorder is not None
    assert recorder.token_id is not None


def test_desired_state_returns_saved_desired(client, upload_token, admin_token, session) -> None:
    # Create the recorder and a tuner status so the operator has something to edit.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-d", "tuners": [{"tuner_key": "rtl-0", "span_hz": 20_000_000, "center_hz": 855_000_000}]},
        headers=bearer(upload_token),
    )
    recorder_id = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]

    # An operator sets the desired state.
    put = client.put(
        f"/api/admin/recorders/{recorder_id}/tuners/rtl-0",
        json={"role": "control", "center_frequency": 851_000_000, "enabled": True, "spectrum": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert put.status_code == 200

    # The recorder polls and gets exactly that, and the version has moved off zero.
    body = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-d"}, headers=bearer(upload_token)
    ).json()
    assert body["version"] >= 1
    tuner = body["tuners"]["rtl-0"]
    # Each tuner carries its own version alongside its config, so drift is implementable.
    assert tuner["version"] == 1
    assert tuner["config"]["role"] == "control"
    assert tuner["config"]["center_frequency"] == 851_000_000
    assert tuner["config"]["enabled"] is True


# ------------------------------------------------------------------------- status POST ---


def test_status_upserts_and_notifies(client, upload_token, captured_events, session) -> None:
    from app.models import Recorder, Tuner

    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-s",
            "recorder_version": "trunkd 0.9.1",
            "tuners": [
                {"tuner_key": "rtl-0", "backend": "rtlsdr", "center_hz": 855_000_000, "role": "control", "locked": True, "applied_version": 0},
                {"tuner_key": "airspy-1", "backend": "airspy", "role": "voice", "pool_in_use": 3, "pool_size": 8},
            ],
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tuners_upserted"] == 2
    assert body["tuners_skipped"] == 0

    # Identifiers-only event on the bus.
    assert any(e["event"] == "tuner.status" and "recorder_id" in e for e in captured_events)

    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-s"))
    assert recorder.recorder_version == "trunkd 0.9.1"
    assert recorder.last_seen_at is not None
    tuners = session.scalars(select(Tuner).where(Tuner.recorder_id == recorder.id)).all()
    assert {t.tuner_key for t in tuners} == {"rtl-0", "airspy-1"}
    rtl = next(t for t in tuners if t.tuner_key == "rtl-0")
    assert rtl.reported["backend"] == "rtlsdr"
    assert rtl.reported_at is not None

    # A second push updates the same rows rather than duplicating them.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-s", "tuners": [{"tuner_key": "rtl-0", "locked": False}]},
        headers=bearer(upload_token),
    )
    session.expire_all()
    tuners = session.scalars(select(Tuner).where(Tuner.recorder_id == recorder.id)).all()
    assert len(tuners) == 2
    rtl = next(t for t in tuners if t.tuner_key == "rtl-0")
    assert rtl.reported["locked"] is False


# ------------------------------------------------- station, counters, tuner extras ---

#: The whole extended body a current trunkd posts: the recorder describes itself, its
#: counters, and each tuner's hardware, gain, level and frequency error.
FULL_BODY = {
    "instance_id": "rec-station",
    "recorder_version": "trunkd 1.2.0",
    "station": {
        "name": "Central roof",
        "location": "Riverton, XX",
        "latitude": 38.8814,
        "longitude": -94.8191,
        "elevation_m": 320.0,
        "contact": "N0CALL <ops@example.invalid>",
        "notes": "Deaf to the north-west behind the water tower.",
    },
    "counters": {
        "calls_started": 41_221,
        "calls_ended": 41_218,
        "uploaded": 41_100,
        "spooled": 118,
        "failed": 3,
        "spool_bytes": 9_431_040,
        "disk_free_bytes": 12_884_901_888,
    },
    "tuners": [
        {
            "tuner_key": "ctrl",
            "backend": "hackrf",
            "role": "control",
            "locked": True,
            "control_hz": 851_050_000,
            "description": {"receiver": "HackRF One", "antenna": "Diamond X50 @ 8m", "height_m": 8.0},
            "gain": {"lna_db": 24, "vga_db": 20, "amp": False},
            "rssi_dbfs": -38.4,
            "freq_error_hz": -137.25,
        }
    ],
}


def test_status_stores_station_counters_and_tuner_extras(client, upload_token, admin_token) -> None:
    resp = client.post("/api/recorder/tuners/status", json=FULL_BODY, headers=bearer(upload_token))
    assert resp.status_code == 200

    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-station"
    )
    # The station comes back exactly as the operator entered it — keyup never rewrites it.
    assert rec["station"]["name"] == "Central roof"
    assert rec["station"]["location"] == "Riverton, XX"
    assert rec["station"]["latitude"] == pytest.approx(38.8814)
    assert rec["station"]["longitude"] == pytest.approx(-94.8191)
    assert rec["station"]["elevation_m"] == pytest.approx(320.0)
    assert rec["station"]["contact"].startswith("N0CALL")
    assert "water tower" in rec["station"]["notes"]
    assert rec["reported_at"] is not None

    assert rec["counters"]["calls_started"] == 41_221
    assert rec["counters"]["failed"] == 3
    assert rec["counters"]["disk_free_bytes"] == 12_884_901_888

    # The per-tuner extras ride inside the tuner dict and land in `reported` with the rest.
    tuner = rec["tuners"][0]
    assert tuner["reported"]["description"]["receiver"] == "HackRF One"
    assert tuner["reported"]["description"]["antenna"] == "Diamond X50 @ 8m"
    assert tuner["reported"]["description"]["height_m"] == pytest.approx(8.0)
    assert tuner["reported"]["gain"] == {"gain": None, "lna_db": 24.0, "vga_db": 20.0, "amp": False}
    assert tuner["reported"]["rssi_dbfs"] == pytest.approx(-38.4)
    assert tuner["reported"]["freq_error_hz"] == pytest.approx(-137.25)


def test_status_without_station_keeps_the_stored_one(client, upload_token, admin_token) -> None:
    # The rule: absent means "the recorder did not say", not "clear it". An older binary — or
    # a push from a recorder whose station block is empty — must not wipe what a newer one
    # reported, because nothing would distinguish that loss from a never-described station.
    client.post(
        "/api/recorder/tuners/status",
        json={**FULL_BODY, "instance_id": "rec-keep"},
        headers=bearer(upload_token),
    )
    old_style = {"instance_id": "rec-keep", "tuners": [{"tuner_key": "ctrl", "locked": False}]}
    assert client.post("/api/recorder/tuners/status", json=old_style, headers=bearer(upload_token)).status_code == 200

    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-keep"
    )
    assert rec["station"]["name"] == "Central roof"  # still there
    assert rec["counters"]["calls_started"] == 41_221
    assert rec["tuners"][0]["reported"]["locked"] is False  # the old-style push did land

    # And a push that *does* carry a station replaces it wholesale.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-keep", "station": {"name": "Garage shelf"}, "tuners": []},
        headers=bearer(upload_token),
    )
    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-keep"
    )
    assert rec["station"] == {"name": "Garage shelf"}  # replaced, and no nulls stored


def test_status_without_new_fields_reports_nothing_rather_than_nulls(
    client, upload_token, admin_token
) -> None:
    # A recorder that has never described itself has null station/counters, not empty objects,
    # so the operator view can tell "not reported" from "reported as blank".
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-plain", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-plain"
    )
    assert rec["station"] is None
    assert rec["counters"] is None
    assert rec["reported_at"] is None


def test_status_rejects_unusable_self_description(client, upload_token) -> None:
    headers = bearer(upload_token)

    def post(body: dict) -> int:
        return client.post("/api/recorder/tuners/status", json={"instance_id": "rec-bad-desc", **body}, headers=headers).status_code

    def post_raw(payload: str) -> int:
        # httpx refuses to serialize NaN itself, so the non-finite cases go over the wire as
        # the literal JSON a rogue recorder would send.
        return client.post(
            "/api/recorder/tuners/status",
            content=payload,
            headers={**headers, "Content-Type": "application/json"},
        ).status_code

    # An over-long antenna is a 422, exactly as an over-long serial or tuner_key already is:
    # truncating would store half a sentence that still looks like the operator's words.
    assert post({"tuners": [{"tuner_key": "t", "description": {"antenna": "x" * 257}}]}) == 422
    assert post({"tuners": [{"tuner_key": "t", "description": {"notes": "x" * 1025}}]}) == 422
    assert post({"station": {"name": "x" * 257}, "tuners": []}) == 422
    assert post({"station": {"notes": "x" * 1025}, "tuners": []}) == 422
    # One character under each cap is fine.
    assert post({"tuners": [{"tuner_key": "t", "description": {"antenna": "x" * 256, "notes": "x" * 1024}}]}) == 200

    # A level that is not a number says nothing at all — NaN and inf are refused at parse.
    assert post_raw('{"instance_id":"rec-bad-desc","tuners":[{"tuner_key":"t","rssi_dbfs":NaN}]}') == 422
    assert post_raw('{"instance_id":"rec-bad-desc","tuners":[{"tuner_key":"t","rssi_dbfs":Infinity}]}') == 422
    assert post_raw('{"instance_id":"rec-bad-desc","tuners":[{"tuner_key":"t","freq_error_hz":NaN}]}') == 422

    # A coordinate is range-checked, never clamped: a clamped longitude is a silent lie.
    assert post({"station": {"longitude": -1810.0}, "tuners": []}) == 422
    assert post({"station": {"latitude": 91.0}, "tuners": []}) == 422
    assert post_raw('{"instance_id":"rec-bad-desc","station":{"latitude":NaN},"tuners":[]}') == 422

    # Counters are non-negative totals, and a wrapped u64 is refused rather than displayed.
    assert post({"counters": {"failed": -1}, "tuners": []}) == 422
    assert post({"counters": {"spool_bytes": 2**63}, "tuners": []}) == 422

    # Gains are bounded; an unknown key inside a self-description object is simply ignored.
    assert post({"tuners": [{"tuner_key": "t", "gain": {"lna_db": 900}}]}) == 422
    assert post({"station": {"name": "ok", "moon_phase": "waxing"}, "tuners": []}) == 200


def test_status_scrubs_nul_from_the_station(client, upload_token, session) -> None:
    from app.models import Recorder

    # The station takes the same NUL scrub as the tuner payload: Postgres JSONB rejects the
    # byte, and it would blow up at flush rather than anywhere catchable.
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-nul-st", "station": {"name": "Central\x00 roof"}, "tuners": []},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-nul-st"))
    assert recorder.station == {"name": "Central roof"}


def test_status_skips_malformed_entries(client, upload_token) -> None:
    # An entry with an empty tuner_key is skipped, never fatal.
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-bad", "tuners": [{"tuner_key": ""}, {"tuner_key": "ok-0"}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tuners_upserted"] == 1
    assert body["tuners_skipped"] == 1


# ------------------------------------------------------------------ operator GET/PUT ---


def test_admin_recorders_requires_admin(client, read_token) -> None:
    assert client.get("/api/admin/recorders").status_code == 401
    assert client.get("/api/admin/recorders", headers=bearer(read_token)).status_code == 403


def test_admin_recorders_lists_with_drift(client, upload_token, admin_token) -> None:
    # One reported tuner that has applied version 0.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-g", "tuners": [{"tuner_key": "rtl-0", "applied_version": 0}]},
        headers=bearer(upload_token),
    )
    recorders = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    rec = next(r for r in recorders if r["instance_id"] == "rec-g")
    tuner = rec["tuners"][0]
    # applied_version 0 == desired_version 0 → no drift yet.
    assert tuner["drift"] is False
    assert tuner["reported"]["applied_version"] == 0

    # Change the desired state: now applied (0) lags desired (1) → drift.
    client.put(
        f"/api/admin/recorders/{rec['id']}/tuners/rtl-0",
        json={"role": "voice"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    recorders = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    rec = next(r for r in recorders if r["instance_id"] == "rec-g")
    assert rec["tuners"][0]["drift"] is True
    assert rec["tuners"][0]["desired_version"] == 1


def test_admin_put_requires_admin(client, upload_token, admin_token, read_token) -> None:
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-p", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    body = {"role": "voice"}
    # Anonymous → 401, non-admin → 403, admin → 200.
    assert client.put(f"/api/admin/recorders/{rid}/tuners/rtl-0", json=body, headers=CSRF_HEADERS).status_code == 401
    assert (
        client.put(
            f"/api/admin/recorders/{rid}/tuners/rtl-0", json=body, headers={**bearer(read_token), **CSRF_HEADERS}
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/admin/recorders/{rid}/tuners/rtl-0", json=body, headers={**bearer(admin_token), **CSRF_HEADERS}
        ).status_code
        == 200
    )


def test_admin_put_validates_role(client, upload_token, admin_token) -> None:
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-r", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"role": "banana"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 422


def test_admin_put_retunes_to_another_band_and_checks_candidates_against_the_new_span(
    client, upload_token, admin_token
) -> None:
    # The tuner reports a 20 MHz span centered at 855 MHz → 845–865 MHz.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-c", "tuners": [{"tuner_key": "rtl-0", "center_hz": 855_000_000, "span_hz": 20_000_000}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    # A retune to another band is allowed: a spare tuner parked on VHF is pointed at an
    # 800 MHz control channel from here.  The candidates are checked against the span the
    # tuner will have, not the one it has — 851 MHz is not inside 890–910.
    bad = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"center_frequency": 900_000_000, "control_frequencies": [851_050_000]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert bad.status_code == 422, bad.text
    good = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"center_frequency": 900_000_000, "control_frequencies": [900_012_500]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert good.status_code == 200, good.text
    # …and a narrower requested width narrows that check too.
    narrow = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"center_frequency": 900_000_000, "sample_rate": 2_000_000, "control_frequencies": [905_000_000]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert narrow.status_code == 422, narrow.text


def test_admin_put_unknown_recorder_is_404(client, admin_token) -> None:
    resp = client.put(
        "/api/admin/recorders/999999/tuners/rtl-0",
        json={"role": "voice"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 404


def test_drift_ignores_missing_applied_version(client, upload_token, admin_token) -> None:
    # A tuner that reports no applied_version at all must not read as drifted after an edit:
    # keyup simply does not know yet whether the recorder has caught up.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-na", "tuners": [{"tuner_key": "rtl-0", "backend": "rtlsdr"}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"role": "voice"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    rec = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    tuner = rec["tuners"][0]
    assert tuner["desired_version"] == 1
    assert tuner["drift"] is False  # applied_version is unknown → not drift


def test_admin_put_clears_a_field_with_null(client, upload_token, admin_token) -> None:
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-clear", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    headers = {**bearer(admin_token), **CSRF_HEADERS}

    # Set a focus site, then clear it with an explicit null. Meanwhile role is left out and kept.
    client.put(f"/api/admin/recorders/{rid}/tuners/rtl-0", json={"role": "voice", "site_id": 7}, headers=headers)
    after_set = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["tuners"][0]
    assert after_set["desired"]["site_id"] == 7

    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0", json={"site_id": None}, headers=headers
    )
    assert resp.status_code == 200
    desired = resp.json()["desired"]
    assert "site_id" not in desired  # cleared, not left at 7
    assert desired["role"] == "voice"  # an omitted field is untouched


# ------------------------------------------------------------------------- band ranges ---
#
# A tuner's antenna and bandpass may cover several disjoint ranges, and `bands` is how an
# operator says so from keyup rather than by editing TOML on a headless box.  Everything
# below exists because trunkd drops a tuner's *whole* desired entry over one malformed
# band, quietly, on a poll nobody is watching — so keyup has to refuse it at the edit.


def _bands_recorder(client, upload_token, admin_token, instance: str, **reported) -> int:  # noqa: ANN001
    """A recorder with one tuner, reporting whatever the case needs; returns its id."""
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": instance, "tuners": [{"tuner_key": "rtl-0", **reported}]},
        headers=bearer(upload_token),
    )
    return next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == instance
    )["id"]


def test_admin_put_accepts_labels_and_normalizes_them(client, upload_token, admin_token) -> None:
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-lab")
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": ["VHF", " 800 ", "900"]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    # Case-folded and trimmed, and in the order the operator gave them.
    assert resp.json()["desired"]["bands"] == ["vhf", "800", "900"]


def test_admin_put_accepts_explicit_hz_ranges(client, upload_token, admin_token) -> None:
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-hz")
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": ["856000000-866000000", "851000000 - 854000000"]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    # Stored as bare digits either side of one hyphen, whatever spacing arrived.
    assert resp.json()["desired"]["bands"] == ["856000000-866000000", "851000000-854000000"]


def test_admin_put_dedupes_bands(client, upload_token, admin_token) -> None:
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-dup")
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": ["800", "VHF", "vhf", "800000000-810000000", "800000000 - 810000000"]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    assert resp.json()["desired"]["bands"] == ["800", "vhf", "800000000-810000000"]


@pytest.mark.parametrize(
    "bands",
    [
        ["banana"],  # no such label
        ["800", "7oo"],  # a typo in the second entry, which must fail the whole list
        ["866000000-856000000"],  # runs backwards
        ["-5-10"],  # not two non-negative integers
        ["800000000-"],  # half a range
        [f"{i}" for i in range(17)],  # over the 16-entry cap (and not labels either)
        ["8" * 65],  # over the 64-character cap
    ],
)
def test_admin_put_refuses_a_malformed_band_list(client, upload_token, admin_token, bands) -> None:  # noqa: ANN001
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-bad")
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": bands},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 422
    # Nothing was written: a refused list must not leave half of itself in `desired`.
    tuner = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    tuner = next(r for r in tuner if r["id"] == rid)["tuners"][0]
    assert "bands" not in (tuner["desired"] or {})


def test_admin_put_names_the_entry_that_is_wrong(client, upload_token, admin_token) -> None:
    # A band list is typed by hand: which entry is wrong is the whole of the message.
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-msg")
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": ["800", "7oo"]},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 422
    assert "bands[1]" in str(resp.json()["detail"])


def test_admin_put_at_the_sixteen_band_cap_is_accepted(client, upload_token, admin_token) -> None:
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-cap")
    bands = [f"{i}000000-{i}500000" for i in range(100, 116)]  # exactly 16
    resp = client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": bands},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    assert len(resp.json()["desired"]["bands"]) == 16


def test_bands_absent_empty_and_null_are_three_different_things(client, upload_token, admin_token) -> None:
    """Absent leaves it alone, `[]` clears the restriction, `null` stops managing it.

    The middle one is the subtle one: `[]` is a real desired value ("record anything in your
    span") that the recorder applies, while `null` takes the key back out of the desired
    state and leaves the recorder on whatever its own TOML says.
    """
    rid = _bands_recorder(client, upload_token, admin_token, "rec-b-three")
    headers = {**bearer(admin_token), **CSRF_HEADERS}
    url = f"/api/admin/recorders/{rid}/tuners/rtl-0"

    client.put(url, json={"bands": ["800"], "role": "voice"}, headers=headers)

    # Absent: another field is written and the band list is untouched.
    after = client.put(url, json={"enabled": True}, headers=headers).json()["desired"]
    assert after["bands"] == ["800"]

    # `[]`: set to *no restriction*, which is a value the recorder applies.
    after = client.put(url, json={"bands": []}, headers=headers).json()["desired"]
    assert after["bands"] == []

    # `null`: the key goes away entirely, exactly as it does for site_id.
    after = client.put(url, json={"bands": None}, headers=headers).json()["desired"]
    assert "bands" not in after
    assert after["role"] == "voice"  # and nothing else moved


def test_bands_reach_the_recorder_in_the_shape_it_polls_for(client, upload_token, admin_token) -> None:
    # The wire shape trunkd parses: `tuners[key].config.bands`, a flat array of strings.
    rid = _bands_recorder(
        client, upload_token, admin_token, "rec-b-wire", center_hz=855_000_000, span_hz=20_000_000
    )
    client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": ["800", "851000000-854000000"], "enabled": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    body = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-b-wire"}, headers=bearer(upload_token)
    ).json()
    assert body["tuners"]["rtl-0"]["config"]["bands"] == ["800", "851000000-854000000"]
    assert body["tuners"]["rtl-0"]["version"] == 1

    # And an emptied list travels as an empty array rather than vanishing from the config.
    client.put(
        f"/api/admin/recorders/{rid}/tuners/rtl-0",
        json={"bands": []},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    body = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-b-wire"}, headers=bearer(upload_token)
    ).json()
    assert body["tuners"]["rtl-0"]["config"]["bands"] == []


def test_status_stores_the_applied_bands(client, upload_token, admin_token) -> None:
    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-b-rep",
            "tuners": [{"tuner_key": "rtl-0", "bands": ["800", "851000000-854000000"]}],
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-b-rep"
    )
    assert rec["tuners"][0]["reported"]["bands"] == ["800", "851000000-854000000"]

    # An unrestricted tuner omits the key, and keyup stores the absence as a null rather
    # than inventing an empty list the recorder never sent.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-b-rep", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-b-rep"
    )
    assert rec["tuners"][0]["reported"]["bands"] is None


def test_status_refuses_a_band_list_over_the_caps(client, upload_token) -> None:
    # The reported list is not grammar-checked — that is the recorder's own TOML talking —
    # but it is capped, like every other reported string, so a rogue recorder cannot grow
    # keyup's storage without bound.
    headers = bearer(upload_token)
    too_many = {
        "instance_id": "rec-b-cap2",
        "tuners": [{"tuner_key": "rtl-0", "bands": [f"{i}-{i}" for i in range(17)]}],
    }
    assert client.post("/api/recorder/tuners/status", json=too_many, headers=headers).status_code == 422
    too_long = {
        "instance_id": "rec-b-cap2",
        "tuners": [{"tuner_key": "rtl-0", "bands": ["8" * 65]}],
    }
    assert client.post("/api/recorder/tuners/status", json=too_long, headers=headers).status_code == 422


def test_status_keeps_an_unusual_reported_band_rather_than_dropping_the_push(client, upload_token, admin_token) -> None:
    # A recorder whose TOML says something keyup would refuse on the way *in* still gets its
    # whole status stored: refusing it would cost the operator every tuner in the report to
    # make a point about a spelling keyup does not own.
    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-b-odd",
            "tuners": [
                {"tuner_key": "rtl-0", "bands": ["800"], "locked": True},
                {"tuner_key": "rtl-1", "bands": ["ham-2m"], "locked": True},
            ],
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    assert resp.json()["tuners_upserted"] == 2
    rec = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-b-odd"
    )
    assert {t["tuner_key"]: t["reported"]["bands"] for t in rec["tuners"]} == {
        "rtl-0": ["800"],
        "rtl-1": ["ham-2m"],
    }


def test_status_caps_reject_oversized_reports(client, upload_token) -> None:
    headers = bearer(upload_token)
    # Too many tuners in one report → 422 (the contract's per-report cap).
    too_many = {"instance_id": "rec-cap", "tuners": [{"tuner_key": f"t{i}"} for i in range(65)]}
    assert client.post("/api/recorder/tuners/status", json=too_many, headers=headers).status_code == 422
    # An over-long tuner_key → 422.
    long_key = {"instance_id": "rec-cap", "tuners": [{"tuner_key": "x" * 65}]}
    assert client.post("/api/recorder/tuners/status", json=long_key, headers=headers).status_code == 422
    # An over-long instance_id → 422.
    long_instance = {"instance_id": "i" * 129, "tuners": []}
    assert client.post("/api/recorder/tuners/status", json=long_instance, headers=headers).status_code == 422


def test_status_strips_nul_bytes(client, upload_token, session) -> None:
    from app.models import Recorder, Tuner

    # A NUL in a reported string would blow up at flush and 500 the push; it must be scrubbed.
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-nul", "tuners": [{"tuner_key": "rtl-0", "backend": "rtl\x00sdr"}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    assert resp.json()["tuners_upserted"] == 1
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-nul"))
    tuner = session.scalars(select(Tuner).where(Tuner.recorder_id == recorder.id)).one()
    assert tuner.reported["backend"] == "rtlsdr"


def test_recorder_autocreate_is_idempotent(client, upload_token, session) -> None:
    from app.models import Recorder

    # Two contacts (a poll then a push) for the same instance must reuse one row, not 500.
    client.get("/api/recorder/desired-state", params={"instance": "rec-once"}, headers=bearer(upload_token))
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-once", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    session.expire_all()
    rows = session.scalars(select(Recorder).where(Recorder.instance_id == "rec-once")).all()
    assert len(rows) == 1


def test_admin_version_bumps_survive_two_tuner_writes(client, upload_token, admin_token) -> None:
    # Two PUTs to different tuners of one recorder must both bump the recorder version.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-v", "tuners": [{"tuner_key": "a"}, {"tuner_key": "b"}]},
        headers=bearer(upload_token),
    )
    rid = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["id"]
    headers = {**bearer(admin_token), **CSRF_HEADERS}
    client.put(f"/api/admin/recorders/{rid}/tuners/a", json={"role": "voice"}, headers=headers)
    client.put(f"/api/admin/recorders/{rid}/tuners/b", json={"role": "data"}, headers=headers)
    version = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]["version"]
    assert version == 2


# ----------------------------------------------------------------------- occupancy ---


# ------------------------------------------------------------------ standby policy ---


def _recorder_id(client, upload_token, admin_token, instance: str) -> int:  # noqa: ANN001
    """Create a recorder through the status push and return its id."""
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": instance, "tuners": [{"tuner_key": "rtl-0", "site": "Central"}]},
        headers=bearer(upload_token),
    )
    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    return next(row["id"] for row in rows if row["instance_id"] == instance)


def test_a_recorder_defaults_to_hot_at_priority_100(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """Every recorder that existed before standby policy did keeps behaving as it did."""
    _recorder_id(client, upload_token, admin_token, "rec-default")
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert (row["standby"], row["priority"], row["promoted_at"]) == ("hot", 100, None)


def test_admin_put_writes_the_standby_policy(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-policy")

    put = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "warm", "priority": 5, "label": "Garage spare"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert put.status_code == 200
    body = put.json()
    assert (body["standby"], body["priority"], body["label"]) == ("warm", 5, "Garage spare")

    # An omitted field is left alone, exactly as the tuner PUT merges.
    again = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"priority": 9},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert (again.json()["standby"], again.json()["priority"]) == ("warm", 9)


def test_admin_put_does_not_touch_the_tuners(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """Policy is what keyup should do *about* a recorder; only the arbiter turns it into a
    tuner write, and only by flipping ``enabled``."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-untouched")
    client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "warm"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    tuner = row["tuners"][0]
    assert (tuner["desired"], tuner["desired_version"]) == (None, 0)


def test_turning_a_recorder_cold_disowns_a_promotion(client, upload_token, admin_token, session) -> None:  # noqa: ANN001, PLR0913
    """A cold box is one the arbiter stops looking at, so it must not be left holding one."""
    from datetime import UTC, datetime

    from app.models import Recorder

    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-promoted")
    session.expire_all()
    session.get(Recorder, recorder_id).promoted_at = datetime.now(UTC)
    session.commit()

    body = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "cold"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()
    assert body["standby"] == "cold"
    assert body["promoted_at"] is None

    # Going warm again does not invent a promotion — only the arbiter sets that.
    warm = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "warm"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()
    assert warm["promoted_at"] is None


def test_admin_put_policy_rejects_a_bad_standby(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-bad")
    resp = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "lukewarm"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 422


def test_admin_put_policy_requires_admin(client, upload_token, admin_token, read_token) -> None:  # noqa: ANN001, PLR0913
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-scope")
    resp = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "warm"},
        headers={**bearer(read_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 403


def test_admin_put_policy_unknown_recorder_is_404(client, admin_token) -> None:  # noqa: ANN001
    resp = client.put(
        "/api/admin/recorders/9999",
        json={"standby": "warm"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------- upload hold ---


def test_a_recorder_starts_with_no_opinion_about_its_uploads(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The default is silence: keyup wants nothing, so the recorder keeps its own setting."""
    _recorder_id(client, upload_token, admin_token, "rec-uplink-default")
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["desired_spool_only"] is None
    assert row["desired_recorder_version"] == 0
    assert row["applied_recorder_version"] is None
    assert row["uplink"] is None
    assert row["spool_only_pending"] is False


def test_admin_put_sets_the_hold_and_bumps_the_recorder_version(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-hold")

    put = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["desired_spool_only"] is True
    assert body["desired_recorder_version"] == 1
    # Nothing has echoed it back yet, so the operator sees it as pending.
    assert body["spool_only_pending"] is True

    # Releasing it is another write, and another version.
    released = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": False},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()
    assert (released["desired_spool_only"], released["desired_recorder_version"]) == (False, 2)


def test_writing_the_same_hold_twice_does_not_bump_the_version(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The recorder polls this counter; a no-op save must not make it re-apply."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-hold-noop")
    for _ in range(3):
        body = client.put(
            f"/api/admin/recorders/{recorder_id}",
            json={"spool_only": True},
            headers={**bearer(admin_token), **CSRF_HEADERS},
        ).json()
    assert body["desired_recorder_version"] == 1


def test_the_hold_is_three_valued_and_null_clears_it(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """`null` is a write — "no opinion" — while omitting the key leaves the value alone."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-hold-null")
    client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    # An unrelated policy write leaves the hold exactly as it was.
    kept = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"standby": "warm"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()
    assert (kept["desired_spool_only"], kept["desired_recorder_version"]) == (True, 1)

    # An explicit null withdraws the opinion, and counts as a change.
    cleared = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": None},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()
    assert cleared["desired_spool_only"] is None
    assert cleared["desired_recorder_version"] == 2
    # Nothing is desired, so nothing can be pending.
    assert cleared["spool_only_pending"] is False


def test_the_hold_reaches_the_recorder_in_the_shape_it_polls_for(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-hold-poll")
    client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    desired = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-hold-poll"},
        headers=bearer(upload_token),
    ).json()
    assert desired["recorder"] == {
        "version": 1,
        "spool_only": True,
        "event_log_archive": None,
        "link": None,
        "broadcastify": None,
        "skip_talkgroups": None,
    }
    # The recorder-level counter is its own: a tuner write does not move it.
    client.put(
        f"/api/admin/recorders/{recorder_id}/tuners/rtl-0",
        json={"enabled": False},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    again = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-hold-poll"},
        headers=bearer(upload_token),
    ).json()
    assert again["recorder"]["version"] == 1
    assert again["version"] > desired["version"]


def test_status_stores_the_uplink_and_the_applied_version(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-uplink")
    client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-uplink",
            "tuners": [{"tuner_key": "rtl-0"}],
            "uplink": {
                "spool_only": True,
                "source": "keyup",
                "spooled_calls": 12,
                "spool_bytes": 4_194_304,
                "url": "https://keyup.example/api",
            },
            "recorder_applied": {"version": 1, "spool_only": True},
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200

    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["uplink"] == {
        "spool_only": True,
        "source": "keyup",
        "spooled_calls": 12,
        "spool_bytes": 4_194_304,
        "url": "https://keyup.example/api",
    }
    assert row["applied_recorder_version"] == 1
    # The echo caught up with the desire, so the badge goes out.
    assert row["spool_only_pending"] is False


def test_the_hold_is_pending_until_the_recorder_echoes_the_new_version(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-uplink-lag")
    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-uplink-lag",
            "tuners": [{"tuner_key": "rtl-0"}],
            "uplink": {"spool_only": False, "source": "config"},
            "recorder_applied": {"version": 0, "spool_only": False},
        },
        headers=bearer(upload_token),
    )
    client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )

    # Desire is at v1, the recorder is still reporting v0.
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["spool_only_pending"] is True
    assert row["uplink"]["spool_only"] is False

    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-uplink-lag",
            "tuners": [{"tuner_key": "rtl-0"}],
            "uplink": {"spool_only": True, "source": "keyup", "spooled_calls": 3},
            "recorder_applied": {"version": 1, "spool_only": True},
        },
        headers=bearer(upload_token),
    )
    settled = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert settled["spool_only_pending"] is False
    assert settled["uplink"] == {"spool_only": True, "source": "keyup", "spooled_calls": 3}


def test_an_older_recorder_omitting_both_blocks_is_still_accepted(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """A binary that predates the hold must keep reporting, and must not wipe what it does
    not know about."""
    _recorder_id(client, upload_token, admin_token, "rec-uplink-old")
    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-uplink-old",
            "tuners": [{"tuner_key": "rtl-0"}],
            "uplink": {"spool_only": True, "source": "panel"},
            "recorder_applied": {"version": 4},
        },
        headers=bearer(upload_token),
    )
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-uplink-old", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["uplink"] == {"spool_only": True, "source": "panel"}
    assert row["applied_recorder_version"] == 4


def test_the_hold_is_admin_only(client, upload_token, admin_token, read_token) -> None:  # noqa: ANN001, PLR0913
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-hold-scope")
    resp = client.put(
        f"/api/admin/recorders/{recorder_id}",
        json={"spool_only": True},
        headers={**bearer(read_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 403


def _seed_observation(session) -> int:  # noqa: ANN001
    """A system, a site and one observation carrying a band plan and traffic."""
    from app.models import RecorderObservation, Site, System

    system = System(name="Example System")
    session.add(system)
    session.flush()
    site = Site(system_id=system.id, name="Central", site_number=1)
    session.add(site)
    session.flush()
    obs = RecorderObservation(
        system_id=system.id,
        site_id=site.id,
        recorder="trunkd",
        observed_at=datetime.now(UTC),
        payload={
            "control": {"frequency_hz": 851_050_000, "channel": "1-3"},
            "bands": [{"id": 0, "base_hz": 851_000_000, "spacing_hz": 12_500, "bandwidth_hz": 12_500}],
            "traffic": [
                {"frequency_hz": 851_250_000, "channel": "1-19", "grants": 4, "calls": 3, "last_seen": datetime.now(UTC).isoformat()},
                {"frequency_hz": 852_000_000, "channel": "1-79", "grants": 1, "calls": 1},
            ],
            "data_channel": {"channels": [852_000_000]},
        },
    )
    session.add(obs)
    session.commit()
    return site.id


def test_occupancy_requires_admin(client, read_token, session) -> None:
    _seed_observation(session)
    # Occupancy lives on admin_router and is admin-gated like the rest of it.
    assert client.get("/api/admin/occupancy").status_code == 401
    assert client.get("/api/admin/occupancy", headers=bearer(read_token)).status_code == 403


def test_occupancy_folds_latest_observation(client, admin_token, session) -> None:
    site_id = _seed_observation(session)
    rows = client.get("/api/admin/occupancy", headers=bearer(admin_token)).json()
    assert len(rows) == 1
    row = rows[0]
    assert row["site_id"] == site_id
    assert row["site_name"] == "Central"
    assert row["bands"][0]["base_hz"] == 851_000_000
    channels = {c["frequency_hz"]: c for c in row["channels"]}
    # The control channel, the voice grant and the data channel are all present and typed.
    assert channels[851_050_000]["role"] == "control"
    assert channels[851_250_000]["role"] == "voice"
    assert channels[851_250_000]["grants"] == 4
    assert channels[852_000_000]["role"] == "data"
    # Sorted by frequency.
    freqs = [c["frequency_hz"] for c in row["channels"]]
    assert freqs == sorted(freqs)


def test_occupancy_filters_by_site(client, admin_token, session) -> None:
    site_id = _seed_observation(session)
    headers = bearer(admin_token)
    rows = client.get("/api/admin/occupancy", params={"site": site_id}, headers=headers).json()
    assert len(rows) == 1
    assert client.get("/api/admin/occupancy", params={"site": 999999}, headers=headers).json() == []


# ------------------------------------------------------- forgetting a recorder ---


def test_forgetting_a_live_recorder_needs_force(client, upload_token, admin_token, session) -> None:  # noqa: ANN001
    from app.models import Recorder

    # A *report*, not a poll: polling for desired state does not stamp `last_seen_at`, and
    # the guard is deliberately about a recorder that is still saying something.
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-live", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    session.expire_all()
    recorder_id = session.scalar(
        select(Recorder.id).where(Recorder.instance_id == "rec-live")
    )

    refused = client.delete(f"/api/admin/recorders/{recorder_id}", headers=bearer(admin_token))
    assert refused.status_code == 409
    assert "force=true" in refused.json()["detail"]
    assert session.get(Recorder, recorder_id) is not None

    forced = client.delete(
        f"/api/admin/recorders/{recorder_id}", params={"force": "true"}, headers=bearer(admin_token)
    )
    assert forced.status_code == 204
    session.expire_all()
    assert session.get(Recorder, recorder_id) is None


def test_a_stale_recorder_is_forgotten_without_force(client, upload_token, admin_token, session) -> None:  # noqa: ANN001
    from datetime import timedelta

    from app.models import Recorder, Tuner

    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-old", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-old"))
    recorder_id = recorder.id
    stale = datetime.now(UTC) - timedelta(days=2)
    recorder.last_seen_at = stale
    recorder.reported_at = stale
    session.commit()

    response = client.delete(f"/api/admin/recorders/{recorder_id}", headers=bearer(admin_token))
    assert response.status_code == 204
    session.expire_all()
    assert session.get(Recorder, recorder_id) is None
    # The tuners went with it rather than being left parented on nothing.
    assert session.scalars(select(Tuner).where(Tuner.recorder_id == recorder_id)).first() is None

    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    assert [row for row in events if row["kind"] == "recorder_deleted"]


def test_forgetting_a_recorder_is_admin_only(client, upload_token, write_token, session) -> None:  # noqa: ANN001
    from app.models import Recorder

    client.get(
        "/api/recorder/desired-state", params={"instance": "rec-acl"}, headers=bearer(upload_token)
    )
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-acl"))

    assert client.delete(f"/api/admin/recorders/{recorder.id}").status_code == 403
    denied = client.delete(f"/api/admin/recorders/{recorder.id}", headers=bearer(write_token))
    assert denied.status_code == 403


def test_forgetting_an_unknown_recorder_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    assert client.delete("/api/admin/recorders/999999", headers=bearer(admin_token)).status_code == 404


# --------------------------------------------------------------- token binding ---


def test_a_recorder_binds_to_the_token_it_first_reported_on(client, upload_token, session) -> None:  # noqa: ANN001
    from app.auth import hash_secret
    from app.models import ApiToken, Recorder

    client.get(
        "/api/recorder/desired-state", params={"instance": "rec-bind"}, headers=bearer(upload_token)
    )
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-bind"))
    token = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_secret(upload_token)))
    assert recorder.token_id == token.id


def test_a_second_token_is_served_but_warned_about(client, upload_token, admin_token, session) -> None:  # noqa: ANN001
    """Bind on first contact, warn on mismatch — never refuse.

    Refusing would take a recorder off the air over a routine key rotation, which is a
    worse failure than the one it would catch.  The binding is left where it was, so the
    column keeps saying which key the recorder *started* on rather than following whichever
    was used last and recording nothing.
    """
    from app.api import recorders as recorders_api
    from app.auth import create_api_token, hash_secret
    from app.models import ApiToken, Recorder

    recorders_api._token_mismatch.clear()  # noqa: SLF001 - the module's own rate limit
    client.get(
        "/api/recorder/desired-state", params={"instance": "rec-two"}, headers=bearer(upload_token)
    )
    session.expire_all()
    first = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_secret(upload_token)))

    _, other = create_api_token(session, "tests-rotated", ["upload"], created_by="tests")
    session.commit()

    served = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-two"}, headers=bearer(other)
    )
    assert served.status_code == 200

    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-two"))
    assert recorder.token_id == first.id

    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    mismatch = [row for row in events if row["kind"] == "recorder.token_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["meta"]["bound_token_id"] == first.id
    assert mismatch[0]["meta"]["seen_token_id"] != first.id

    # …and the complaint is rate limited: a recorder polls every few seconds, and one entry
    # per contact would bury the message it exists to make findable.
    client.get("/api/recorder/desired-state", params={"instance": "rec-two"}, headers=bearer(other))
    events = client.get("/api/activity", headers=bearer(admin_token)).json()
    assert len([row for row in events if row["kind"] == "recorder.token_mismatch"]) == 1


# ------------------------------------------------------- link peers and event-log status ---


def test_link_and_event_log_are_stored_verbatim(client, upload_token, admin_token, session) -> None:
    """Both are opaque: keyup renders them and decides nothing from them, so nothing is dropped."""
    from app.models import Recorder

    link = {
        "peers": [
            {
                "instance": "trunkd-tower",
                "alive": True,
                "last_seen": "2026-09-02T18:04:11Z",
                "claims": ["site:3", "site:7"],
            },
            {"instance": "trunkd-bench", "alive": False, "last_seen": None, "claims": []},
        ],
        # A key keyup has never heard of survives, which is the point of storing it opaquely.
        "quorum": 2,
    }
    event_log = {"pending": 2, "last_date": "2026-09-01", "bytes_pending": 4_194_304}

    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-link",
            "tuners": [{"tuner_key": "rtl-0"}],
            "link": link,
            "event_log": event_log,
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200, resp.text

    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-link"))
    assert recorder.link == link
    assert recorder.event_log == event_log
    assert recorder.reported_at is not None

    row = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-link"
    )
    assert row["link"] == link
    assert row["event_log"] == event_log


def test_omitting_link_or_event_log_is_no_change(client, upload_token, session) -> None:
    """The same update-only rule `station`, `counters` and `uplink` follow.

    A recorder rolled back to a binary that knows nothing of either must not wipe what a
    newer one reported: an absent key means "did not say", never "has none".
    """
    from app.models import Recorder

    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-link2",
            "tuners": [{"tuner_key": "rtl-0"}],
            "link": {"peers": [{"instance": "a", "alive": True}]},
            "event_log": {"pending": 1},
        },
        headers=bearer(upload_token),
    )
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-link2", "tuners": [{"tuner_key": "rtl-0", "locked": True}]},
        headers=bearer(upload_token),
    )
    session.expire_all()
    recorder = session.scalar(select(Recorder).where(Recorder.instance_id == "rec-link2"))
    assert recorder.link == {"peers": [{"instance": "a", "alive": True}]}
    assert recorder.event_log == {"pending": 1}


def test_a_recorder_that_reports_neither_shows_null(client, upload_token, admin_token) -> None:
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-nolink", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    row = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-nolink"
    )
    assert row["link"] is None
    assert row["event_log"] is None


# ------------------------------------------- the rest of the recorder's desired state ---
#
# The upload hold above proved the pattern; the event-log archive and the link block use
# exactly it, on the same generation counter. What is worth testing here is what is *not*
# shared: that one write of two fields bumps the version once, that the link is validated
# per field, and that the shared key gets out of exactly one route and no other.


def _link(**overrides) -> dict:  # noqa: ANN003
    """A valid link block, with whatever this test wants to be wrong about it."""
    block = {
        "enabled": True,
        "bind": "0.0.0.0:9110",
        "peers": ["recorder-a:9110"],
        "key": "not-a-real-link-key",
        "hello_interval_s": 5,
        "accept_remote_grants": True,
        "publish_grants": True,
        "defer_to_claims": False,
    }
    block.update(overrides)
    return block


def _put(client, admin_token, recorder_id: int, body: dict):  # noqa: ANN001, ANN201
    return client.put(
        f"/api/admin/recorders/{recorder_id}",
        json=body,
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )


def test_the_archive_flag_is_three_valued_like_the_hold(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-archive")
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["desired_event_log_archive"] is None
    assert row["recorder_pending"] is False

    body = _put(client, admin_token, recorder_id, {"event_log_archive": True}).json()
    assert (body["desired_event_log_archive"], body["desired_recorder_version"]) == (True, 1)
    # The whole-block badge lights, while the hold's own badge stays dark: keyup still has
    # no opinion about the uploads, and one setting must not make the other look pending.
    assert body["recorder_pending"] is True
    assert body["spool_only_pending"] is False

    # A no-op save does not move the counter the recorder polls.
    repeated = _put(client, admin_token, recorder_id, {"event_log_archive": True}).json()
    assert repeated["desired_recorder_version"] == 1
    cleared = _put(client, admin_token, recorder_id, {"event_log_archive": None}).json()
    assert (cleared["desired_event_log_archive"], cleared["desired_recorder_version"]) == (None, 2)
    assert cleared["recorder_pending"] is False


def test_two_recorder_fields_in_one_write_bump_the_version_once(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The recorder applies the block as a whole and echoes one number back.

    A counter that rose twice for one save would sit permanently ahead of the version the
    recorder can possibly report, and the page would show it pending for ever.
    """
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-onebump")
    body = _put(
        client, admin_token, recorder_id, {"spool_only": True, "event_log_archive": True}
    ).json()
    assert body["desired_recorder_version"] == 1


def test_the_link_reaches_the_recorder_with_its_key(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The desired-state poll is the one response that carries the shared secret."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-desired")
    _put(client, admin_token, recorder_id, {"link": _link(), "event_log_archive": True})

    desired = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-link-desired"},
        headers=bearer(upload_token),
    ).json()
    assert desired["recorder"]["event_log_archive"] is True
    assert desired["recorder"]["link"] == _link()
    assert desired["recorder"]["version"] == 1


def test_the_admin_read_never_carries_the_link_key(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """`key_set` and a fingerprint in every list and detail; the secret in neither."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-mask")
    saved = _put(client, admin_token, recorder_id, {"link": _link()}).json()

    assert "key" not in saved["desired_link"]
    assert saved["desired_link"]["key_set"] is True
    assert saved["desired_link"]["bind"] == "0.0.0.0:9110"
    fingerprint = saved["desired_link"]["key_fingerprint"]
    assert len(fingerprint) == 8

    listed = client.get("/api/admin/recorders", headers=bearer(admin_token))
    # Belt and braces: the secret must not appear *anywhere* in the response, not merely
    # under the key it would have been rendered at.
    assert "not-a-real-link-key" not in listed.text
    row = next(r for r in listed.json() if r["id"] == recorder_id)
    assert row["desired_link"]["key_fingerprint"] == fingerprint


def test_the_key_route_reveals_it_and_records_the_read(client, upload_token, admin_token, session) -> None:  # noqa: ANN001
    from app.models import ActivityEvent

    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-reveal")
    _put(client, admin_token, recorder_id, {"link": _link()})

    resp = client.get(f"/api/admin/recorders/{recorder_id}/link/key", headers=bearer(admin_token))
    assert resp.status_code == 200
    assert resp.json()["key"] == "not-a-real-link-key"
    assert resp.json()["key_set"] is True

    session.expire_all()
    event = session.scalar(
        select(ActivityEvent).where(ActivityEvent.kind == "recorder.link_key_read")
    )
    assert event is not None
    # The audit row names the fingerprint, never the secret: an audit trail that quoted it
    # would be a second place the key lives, and one nobody thinks of as sensitive.
    assert event.meta["key_fingerprint"] == resp.json()["key_fingerprint"]
    assert "not-a-real-link-key" not in str(event.meta)


def test_the_key_route_needs_admin(client, upload_token, admin_token, read_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-scope")
    assert client.get(f"/api/admin/recorders/{recorder_id}/link/key").status_code == 401
    denied = client.get(
        f"/api/admin/recorders/{recorder_id}/link/key", headers=bearer(read_token)
    )
    assert denied.status_code == 403


def test_generating_a_key_returns_64_hex_and_stores_nothing(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """Generating is not saving: a key pressed and then thought better of must change nothing."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-gen")
    resp = client.post(
        f"/api/admin/recorders/{recorder_id}/link/generate-key",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    key = resp.json()["key"]
    assert len(key) == 64
    int(key, 16)  # it is hex
    again = client.post(
        f"/api/admin/recorders/{recorder_id}/link/generate-key",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    ).json()["key"]
    assert key != again

    row = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["id"] == recorder_id
    )
    assert row["desired_link"] is None
    assert row["desired_recorder_version"] == 0


def test_an_update_without_a_key_keeps_the_stored_one(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The dialog shows the key masked, so an ordinary save does not have it to send."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-keepkey")
    _put(client, admin_token, recorder_id, {"link": _link()})

    without = {k: v for k, v in _link(hello_interval_s=9).items() if k != "key"}
    saved = _put(client, admin_token, recorder_id, {"link": without}).json()
    assert saved["desired_link"]["hello_interval_s"] == 9
    assert saved["desired_link"]["key_set"] is True
    revealed = client.get(
        f"/api/admin/recorders/{recorder_id}/link/key", headers=bearer(admin_token)
    ).json()
    assert revealed["key"] == "not-a-real-link-key"


def test_a_link_written_unchanged_does_not_bump_the_version(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-noop")
    _put(client, admin_token, recorder_id, {"link": _link()})
    body = _put(client, admin_token, recorder_id, {"link": _link()}).json()
    assert body["desired_recorder_version"] == 1
    # A whitespace-only difference is the same address, not a change.
    spaced = _put(client, admin_token, recorder_id, {"link": _link(bind=" 0.0.0.0:9110 ")}).json()
    assert spaced["desired_recorder_version"] == 1


def test_a_null_link_stops_managing_it(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-unmanage")
    _put(client, admin_token, recorder_id, {"link": _link()})
    cleared = _put(client, admin_token, recorder_id, {"link": None}).json()
    assert cleared["desired_link"] is None
    assert cleared["desired_recorder_version"] == 2
    desired = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-link-unmanage"},
        headers=bearer(upload_token),
    ).json()
    assert desired["recorder"]["link"] is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"bind": "0.0.0.0"}, "link.bind"),
        ({"bind": "0.0.0.0:0"}, "link.bind"),
        ({"bind": "0.0.0.0:70000"}, "link.bind"),
        ({"bind": "::1:9110"}, "link.bind"),
        ({"bind": "[::zz]:9110"}, "link.bind"),
        ({"bind": "not a host:9110"}, "link.bind"),
        ({"peers": ["recorder-a:9110", "recorder-a:9110"]}, "link.peers[1]"),
        ({"peers": ["recorder-a"]}, "link.peers[0]"),
        ({"peers": []}, "link.peers"),
        ({"key": "tooshort"}, "link.key"),
        ({"hello_interval_s": 0}, "link.hello_interval_s"),
        ({"hello_interval_s": 61}, "link.hello_interval_s"),
        ({"hello_interval_s": 1.5}, "link.hello_interval_s"),
        ({"accept_remote_grants": "yes"}, "link.accept_remote_grants"),
    ],
)
def test_a_bad_link_is_a_422_naming_the_field(client, upload_token, admin_token, overrides, field) -> None:  # noqa: ANN001
    """Every refusal reads `field: reason`, so the dialog can hang it under the input."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-bad")
    resp = _put(client, admin_token, recorder_id, {"link": _link(**overrides)})
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith(f"{field}: ")


@pytest.mark.parametrize(
    "address",
    ["192.0.2.30:9110", "recorder-a:9110", "rec.example.com:9110", "[2001:db8::1]:9110", "[::1]:1"],
)
def test_every_shape_of_address_is_accepted(client, upload_token, admin_token, address) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-addr")
    resp = _put(client, admin_token, recorder_id, {"link": _link(bind=address, peers=[address])})
    assert resp.status_code == 200, resp.text
    assert resp.json()["desired_link"]["bind"] == address


def test_a_disabled_link_may_be_staged_without_peers(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The bind address and the key are filled in before the second box exists."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-link-staged")
    resp = _put(client, admin_token, recorder_id, {"link": _link(enabled=False, peers=[])})
    assert resp.status_code == 200
    assert resp.json()["desired_link"]["peers"] == []


def test_the_recorder_echo_stores_what_it_applied_and_refused(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-echo")
    _put(client, admin_token, recorder_id, {"link": _link(), "event_log_archive": True})

    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-echo",
            "tuners": [],
            "recorder_applied": {
                "version": 1,
                "spool_only": None,
                "event_log_archive": True,
                # The recorder echoes the fingerprint, never the key itself.
                "link": {
                    "enabled": True,
                    "bind": "0.0.0.0:9110",
                    "key_set": True,
                    "key_fingerprint": "deadbeef",
                },
            },
            "rejected": {"link": "built without the link feature"},
            "capabilities": {"recorder": {"restart_on": ["link"]}},
            "event_log": {
                "archive": True,
                "source": "keyup",
                "pending": 2,
                "last_archived": "2026-09-01",
                "last_error": None,
            },
        },
        headers=bearer(upload_token),
    )

    row = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["id"] == recorder_id
    )
    assert row["applied_recorder_version"] == 1
    assert row["recorder_applied"]["event_log_archive"] is True
    assert row["recorder_applied"]["link"]["key_fingerprint"] == "deadbeef"
    assert row["recorder_rejected"] == {"link": "built without the link feature"}
    assert row["recorder_capabilities"] == {"restart_on": ["link"]}
    assert row["event_log"]["source"] == "keyup"
    # It has applied the version, so nothing is pending — a refusal leaves only the map.
    assert row["recorder_pending"] is False


def test_an_older_recorder_that_reports_none_of_it_is_fine(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """Tolerance is the point: a binary that predates all of this must still be storable."""
    client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-old", "tuners": [{"tuner_key": "rtl-0"}]},
        headers=bearer(upload_token),
    )
    row = next(
        r
        for r in client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
        if r["instance_id"] == "rec-old"
    )
    assert row["recorder_applied"] is None
    assert row["recorder_rejected"] is None
    assert row["recorder_capabilities"] is None
    assert row["desired_link"] is None


def test_a_tuner_may_be_told_to_take_remote_grants(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-remote-grants")
    resp = client.put(
        f"/api/admin/recorders/{recorder_id}/tuners/rtl-0",
        json={"remote_grants": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 200
    assert resp.json()["desired"]["remote_grants"] is True
    desired = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-remote-grants"},
        headers=bearer(upload_token),
    ).json()
    assert desired["tuners"]["rtl-0"]["config"]["remote_grants"] is True


# ------------------------------------------------------------------------- renaming ---


def test_renaming_a_recorder_carries_its_archive_with_it(client, upload_token, admin_token, session) -> None:  # noqa: ANN001
    """The rename and the rewrites are one transaction, or the archive splits in two."""
    from datetime import date

    from app.models import EventLogArchive, RadioEvent, Recorder, RecorderObservation

    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-win-01")
    session.add_all(
        [
            RadioEvent(at=datetime.now(UTC), kind="other", instance="rec-win-01"),
            EventLogArchive(
                instance="rec-win-01",
                date=date(2026, 9, 1),
                bytes=10,
                sha256="a" * 64,
                path="/tmp/a.jsonl.gz",
            ),
            RecorderObservation(
                observed_at=datetime.now(UTC),
                recorder="trunkd",
                instance="rec-win-01",
                payload={},
            ),
        ]
    )
    session.commit()

    resp = _put(client, admin_token, recorder_id, {"instance_id": "recorder-a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["instance_id"] == "recorder-a"

    session.expire_all()
    renamed = session.scalar(select(Recorder).where(Recorder.id == recorder_id))
    assert renamed.instance_id == "recorder-a"
    assert session.scalar(select(RadioEvent).where(RadioEvent.instance == "rec-win-01")) is None
    assert session.scalar(select(RadioEvent).where(RadioEvent.instance == "recorder-a")) is not None
    archive = session.scalar(select(EventLogArchive).where(EventLogArchive.instance == "recorder-a"))
    assert archive is not None
    stale = session.scalars(
        select(RecorderObservation).where(RecorderObservation.instance == "rec-win-01")
    ).all()
    assert stale == []


def test_renaming_onto_a_name_in_use_is_a_409_that_says_what_to_do(client, upload_token, admin_token) -> None:  # noqa: ANN001
    first = _recorder_id(client, upload_token, admin_token, "rec-rename-a")
    _recorder_id(client, upload_token, admin_token, "rec-rename-b")
    resp = _put(client, admin_token, first, {"instance_id": "rec-rename-b"})
    assert resp.status_code == 409
    # The message points at the route that clears the way, because "already exists" alone
    # leaves the operator with a duplicate they did not know they had.
    assert "DELETE /api/admin/recorders/" in resp.json()["detail"]


def test_renaming_to_the_same_name_is_a_no_op(client, upload_token, admin_token) -> None:  # noqa: ANN001
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-rename-same")
    resp = _put(client, admin_token, recorder_id, {"instance_id": "rec-rename-same"})
    assert resp.status_code == 200
    assert resp.json()["instance_id"] == "rec-rename-same"


def test_a_recorder_still_reporting_refuses_to_be_forgotten(client, upload_token, admin_token) -> None:  # noqa: ANN001
    """The duplicate an operator is told to delete is an *empty* row; a live one is a 409."""
    recorder_id = _recorder_id(client, upload_token, admin_token, "rec-live-delete")
    resp = client.delete(
        f"/api/admin/recorders/{recorder_id}",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert resp.status_code == 409
    assert "next poll" in resp.json()["detail"]
    forced = client.delete(
        f"/api/admin/recorders/{recorder_id}?force=true",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert forced.status_code == 204


# ------------------------------------------------- Broadcastify: the block and the claim ---
#
# Recorder-direct upload (docs/EXPORTERS.md, "Recorder-direct upload").  Two things are
# being pinned here: the block appears only when all three conditions hold, and the key it
# carries never reaches an operator.


@pytest.fixture
def calls_configured(session):  # noqa: ANN001
    """Runtime settings with the Calls exporter on and credentialled, upload still by keyup."""
    from app import runtime_settings
    from app.models import Setting

    def apply(**values: object) -> None:
        for key, value in values.items():
            session.merge(Setting(key=key, value=value))
        session.commit()
        runtime_settings.invalidate()

    apply(
        export_broadcastify_enabled=True,
        broadcastify_api_key="test-bcfy-key",
        broadcastify_system_id="6551",
        broadcastify_url="https://api.broadcastify.test/call-upload",
    )
    yield apply
    runtime_settings.invalidate()


def _desired(client, upload_token, instance: str = "rec-bcfy") -> dict:  # noqa: ANN001
    return client.get(
        "/api/recorder/desired-state", params={"instance": instance}, headers=bearer(upload_token)
    ).json()


def test_no_broadcastify_block_while_keyup_does_the_uploading(client, upload_token, calls_configured) -> None:  # noqa: ANN001, ARG001
    """The default, and the fallback: null means "keyup uploads this recorder's calls"."""
    assert _desired(client, upload_token)["recorder"]["broadcastify"] is None


def test_the_broadcastify_block_appears_when_the_setting_says_recorder(client, upload_token, calls_configured) -> None:  # noqa: ANN001
    """All three conditions hold, so the recorder is handed the job and the credentials."""
    calls_configured(broadcastify_upload_at="recorder")
    block = _desired(client, upload_token)["recorder"]["broadcastify"]
    assert block["enabled"] is True
    assert block["api_key"] == "test-bcfy-key"
    assert block["system_id"] == "6551"
    assert block["endpoint_url"] == "https://api.broadcastify.test/call-upload"
    assert block["encoding"] == "mp3"
    # `all` mode: no list to ship, because there is no rule to resolve.
    assert block["talkgroups"] is None
    assert block["claim"] is True
    assert block["claim_url"] == "/api/recorder/broadcastify/claim"
    assert len(block["revision"]) == 12


def test_the_block_is_absent_while_the_calls_switch_is_off(client, upload_token, calls_configured) -> None:  # noqa: ANN001
    """Choosing *who* uploads does not turn the exporter on."""
    calls_configured(broadcastify_upload_at="recorder", export_broadcastify_enabled=False)
    assert _desired(client, upload_token)["recorder"]["broadcastify"] is None


def test_the_block_is_absent_without_credentials(client, upload_token, calls_configured) -> None:  # noqa: ANN001
    """Half a block — an endpoint with no key — is not something to hand a recorder."""
    calls_configured(broadcastify_upload_at="recorder", broadcastify_api_key="")
    assert _desired(client, upload_token)["recorder"]["broadcastify"] is None


def test_the_block_does_not_move_the_recorder_version(client, upload_token, calls_configured) -> None:  # noqa: ANN001
    """It is derived from settings, not from a recorder edit, so it carries `revision`."""
    before = _desired(client, upload_token)["recorder"]["version"]
    calls_configured(broadcastify_upload_at="recorder")
    after = _desired(client, upload_token)["recorder"]
    assert after["version"] == before
    assert after["broadcastify"]["revision"]


def test_the_admin_read_never_carries_the_broadcastify_key(client, upload_token, admin_token, calls_configured) -> None:  # noqa: ANN001
    """The same defence as the link key: `api_key_set` and a fingerprint, never the key."""
    calls_configured(broadcastify_upload_at="recorder")
    _recorder_id(client, upload_token, admin_token, "rec-bcfy-mask")

    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    block = next(r for r in rows if r["instance_id"] == "rec-bcfy-mask")["desired_broadcastify"]
    assert "api_key" not in block
    assert block["api_key_set"] is True
    assert len(block["api_key_fingerprint"]) == 8
    assert "test-bcfy-key" not in client.get(
        "/api/admin/recorders", headers=bearer(admin_token)
    ).text


def test_the_talkgroup_selection_is_resolved_to_a_list(client, upload_token, calls_configured, session) -> None:  # noqa: ANN001
    """A recorder has no talkgroup table, so `allow`/`deny` come down as explicit tgids."""
    from app.models import System, Talkgroup

    system = System(name="Example System", protocol="P25 Phase 1", trunked=True)
    session.add(system)
    session.flush()
    session.add_all(
        [
            Talkgroup(system_id=system.id, tgid=1401, export_bcfy_calls=True),
            Talkgroup(system_id=system.id, tgid=1402, export_bcfy_calls=False),
        ]
    )
    session.commit()

    calls_configured(
        broadcastify_upload_at="recorder", export_broadcastify_talkgroup_mode="allow"
    )
    assert _desired(client, upload_token)["recorder"]["broadcastify"]["talkgroups"] == [1401]


# ------------------------------------------------------------------------ the claim ---


def test_the_claim_route_needs_an_upload_token(client, read_token) -> None:  # noqa: ANN001
    body = {"instance_id": "rec-a", "tgid": 1101, "ts": 1_757_030_462, "duration_s": 3.0}
    assert client.post("/api/recorder/broadcastify/claim", json=body).status_code == 401
    assert (
        client.post(
            "/api/recorder/broadcastify/claim", json=body, headers=bearer(read_token)
        ).status_code
        == 403
    )


def test_the_claim_route_grants_then_denies(client, upload_token) -> None:  # noqa: ANN001
    """The first recorder to finish the call uploads it; the second is told who did."""
    body = {"instance_id": "rec-a", "tgid": 1101, "ts": 1_757_030_462, "duration_s": 3.0}
    first = client.post(
        "/api/recorder/broadcastify/claim", json=body, headers=bearer(upload_token)
    ).json()
    assert (first["granted"], first["holder"]) == (True, "rec-a")

    # …a second earlier by one second: the same transmission, rounded differently.
    second = client.post(
        "/api/recorder/broadcastify/claim",
        json={**body, "instance_id": "rec-b", "ts": 1_757_030_461},
        headers=bearer(upload_token),
    ).json()
    assert (second["granted"], second["holder"]) == (False, "rec-a")
    assert second["claim_id"] == first["claim_id"]

    # …and three seconds away is a different call, which the tolerance must not swallow.
    third = client.post(
        "/api/recorder/broadcastify/claim",
        json={**body, "instance_id": "rec-b", "ts": 1_757_030_465},
        headers=bearer(upload_token),
    ).json()
    assert third["granted"] is True


def test_the_claim_route_is_idempotent(client, upload_token) -> None:  # noqa: ANN001
    """A retry must not cost a recorder the call it already holds."""
    body = {"instance_id": "rec-a", "tgid": 1101, "ts": 1_757_030_462, "duration_s": 3.0}
    first = client.post(
        "/api/recorder/broadcastify/claim", json=body, headers=bearer(upload_token)
    ).json()
    again = client.post(
        "/api/recorder/broadcastify/claim", json=body, headers=bearer(upload_token)
    ).json()
    assert again["granted"] is True
    assert again["claim_id"] == first["claim_id"]


# ------------------------------------------------------------------- live monitor ---


def _monitor_of(session, instance: str):  # noqa: ANN001, ANN202
    """The stored ``monitor`` block, read back from the row itself.

    ``GET /admin/recorders`` deliberately does not render it — the one place it is shown is
    the status page's recorder row — so the assertion goes to the column.
    """
    from sqlalchemy import select

    from app.models import Recorder

    session.expire_all()
    return session.scalar(select(Recorder.monitor).where(Recorder.instance_id == instance))


def test_status_stores_the_monitor_block_and_drops_what_it_does_not_know(  # noqa: ANN001
    client, session, upload_token, admin_token
) -> None:
    _recorder_id(client, upload_token, admin_token, "rec-monitor")
    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-monitor",
            "tuners": [{"tuner_key": "rtl-0"}],
            "monitor": {
                "enabled": True,
                "url": "http://127.0.0.1:8095",
                "stream_url": "http://127.0.0.1:8095/api/monitor/stream",
                "token_required": False,
                "streams": 0,
                "max_streams": 4,
                "something_new": "from a later recorder",
            },
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200, resp.text
    assert _monitor_of(session, "rec-monitor") == {
        "enabled": True,
        "url": "http://127.0.0.1:8095",
        "stream_url": "http://127.0.0.1:8095/api/monitor/stream",
        "token_required": False,
        "streams": 0,
        "max_streams": 4,
    }


def test_a_recorder_that_reports_no_monitor_has_none(client, session, upload_token, admin_token) -> None:  # noqa: ANN001
    """An older binary sends no block at all, and everything else about the push still works."""
    _recorder_id(client, upload_token, admin_token, "rec-no-monitor")
    resp = client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-no-monitor",
            "tuners": [{"tuner_key": "rtl-0"}],
            "uplink": {"spool_only": False, "source": "config"},
        },
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200, resp.text
    assert _monitor_of(session, "rec-no-monitor") is None
    row = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()[0]
    assert row["uplink"]["source"] == "config"


def test_a_push_without_the_block_clears_a_monitor_that_was_reported(  # noqa: ANN001
    client, session, upload_token, admin_token
) -> None:
    """The link must not outlive the panel: silence clears it rather than meaning 'no change'."""
    _recorder_id(client, upload_token, admin_token, "rec-monitor-gone")
    body = {"instance_id": "rec-monitor-gone", "tuners": [{"tuner_key": "rtl-0"}]}
    client.post(
        "/api/recorder/tuners/status",
        json={**body, "monitor": {"enabled": True, "url": "http://192.0.2.30:8095"}},
        headers=bearer(upload_token),
    )
    assert _monitor_of(session, "rec-monitor-gone") == {
        "enabled": True,
        "url": "http://192.0.2.30:8095",
    }

    client.post("/api/recorder/tuners/status", json=body, headers=bearer(upload_token))
    assert _monitor_of(session, "rec-monitor-gone") is None
