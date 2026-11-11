"""The tuner front-end settings: what an operator may write, and what the tuner refuses.

Every rule here is checked against the tuner's last **reported** status, so each test posts
a status first and then edits.  The three shapes worth keeping apart:

* a tuner that reported a Soapy front end with named gain elements — everything is checkable;
* a tuner that reported ``hackrf`` — the step rules apply and the Soapy fields are refused;
* a tuner that has reported nothing at all — nothing that needs the reported state is
  checked, because a brand-new tuner must still be configurable.
"""

from __future__ import annotations

import pytest

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


#: A Soapy tuner that has described its front end: three gain stages with real ranges, a
#: span to place control candidates in, and the capabilities the dialog reads.
SOAPY_STATUS = {
    "instance_id": "rec-soapy",
    "tuners": [
        {
            "tuner_key": "airspy-0",
            "backend": "soapy",
            "center_hz": 855_000_000,
            "span_hz": 20_000_000,
            "sample_rate": 10_000_000,
            "gain": 22.5,
            "gains": {"LNA": 12.0, "MIX": 8.0, "IF": 2.5},
            "agc": False,
            "ppm": 1.5,
            "auto_ppm": True,
            "auto_ppm_applied": 1.8,
            "bandwidth_hz": 8_000_000,
            "rx_port": "LNAW",
            "settings": {"biastee": "true"},
            "data_follow": False,
            "control_frequencies": [851_050_000],
            "gain_elements": [
                {"name": "LNA", "min": 0, "max": 15, "step": 1},
                {"name": "MIX", "min": 0, "max": 15, "step": 1},
                {"name": "IF", "min": 0, "max": 15, "step": 1},
            ],
            "capabilities": {
                "runtime_gain": True,
                "runtime_ppm": False,
                "restart_on": ["sample_rate", "bandwidth_hz"],
            },
        }
    ],
}

HACKRF_STATUS = {
    "instance_id": "rec-hackrf",
    "tuners": [
        {
            "tuner_key": "hackrf-0",
            "backend": "hackrf",
            "center_hz": 855_000_000,
            "span_hz": 20_000_000,
            "lna_db": 24,
            "vga_db": 20,
            "amp": False,
        }
    ],
}


def _select_tuner(instance_id, key):  # noqa: ANN001, ANN201
    """``select`` for one tuner by recorder instance and key — the tests read it often."""
    from sqlalchemy import select

    from app.models import Recorder, Tuner

    return (
        select(Tuner)
        .join(Recorder, Tuner.recorder_id == Recorder.id)
        .where(Recorder.instance_id == instance_id, Tuner.tuner_key == key)
    )


def _seed(client, upload_token, admin_token, status) -> int:  # noqa: ANN001
    """Post a status and return the recorder id the operator edits."""
    resp = client.post("/api/recorder/tuners/status", json=status, headers=bearer(upload_token))
    assert resp.status_code == 200, resp.text
    return resp.json()["recorder_id"]


def _put(client, admin_token, recorder_id, key, body):  # noqa: ANN001
    return client.put(
        f"/api/admin/recorders/{recorder_id}/tuners/{key}",
        json=body,
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )


# ------------------------------------------------------------------ reported storage ---


def test_reported_front_end_is_stored_verbatim(client, upload_token, admin_token, session) -> None:
    """Every new reported field lands in ``tuners.reported`` exactly as it was sent."""
    _seed(client, upload_token, admin_token, SOAPY_STATUS)
    session.expire_all()
    tuner = session.scalars(_select_tuner("rec-soapy", "airspy-0")).one()
    reported = tuner.reported
    assert reported["backend"] == "soapy"
    assert reported["gain"] == 22.5
    assert reported["gains"] == {"LNA": 12.0, "MIX": 8.0, "IF": 2.5}
    assert reported["agc"] is False
    assert reported["ppm"] == 1.5
    assert reported["auto_ppm"] is True
    assert reported["auto_ppm_applied"] == 1.8
    assert reported["bandwidth_hz"] == 8_000_000
    assert reported["rx_port"] == "LNAW"
    assert reported["settings"] == {"biastee": "true"}
    assert reported["sample_rate"] == 10_000_000
    assert reported["data_follow"] is False
    assert reported["control_frequencies"] == [851_050_000]
    assert [e["name"] for e in reported["gain_elements"]] == ["LNA", "MIX", "IF"]
    assert reported["capabilities"]["restart_on"] == ["sample_rate", "bandwidth_hz"]


def test_hackrf_reported_fields_are_stored(client, upload_token, admin_token, session) -> None:
    _seed(client, upload_token, admin_token, HACKRF_STATUS)
    session.expire_all()
    tuner = session.scalars(_select_tuner("rec-hackrf", "hackrf-0")).one()
    assert tuner.reported["lna_db"] == 24
    assert tuner.reported["vga_db"] == 20
    assert tuner.reported["amp"] is False


def test_older_recorder_without_any_of_it_is_fine(client, upload_token, session) -> None:
    """Every new field is optional: a binary that knows none of them still reports."""
    resp = client.post(
        "/api/recorder/tuners/status",
        json={"instance_id": "rec-old", "tuners": [{"tuner_key": "rtl-0", "locked": True}]},
        headers=bearer(upload_token),
    )
    assert resp.status_code == 200
    assert resp.json()["tuners_upserted"] == 1


# ---------------------------------------------------------------------- rejected ---


def test_rejected_is_exposed_and_does_not_hold_the_tuner_pending(
    client, upload_token, admin_token
) -> None:
    """A refused field shows up on the tuner, and the acknowledged version still clears drift."""
    recorder_id = _seed(client, upload_token, admin_token, HACKRF_STATUS)
    assert _put(client, admin_token, recorder_id, "hackrf-0", {"lna_db": 32}).status_code == 200

    # Before the recorder answers, the edit is pending — nothing has applied it.
    tuner = _tuner(client, admin_token, recorder_id, "hackrf-0")
    assert tuner["desired_version"] == 1

    # The recorder acknowledges the version *and* says it could not use the field.
    client.post(
        "/api/recorder/tuners/status",
        json={
            "instance_id": "rec-hackrf",
            "tuners": [
                {
                    "tuner_key": "hackrf-0",
                    "backend": "hackrf",
                    "applied_version": 1,
                    "rejected": {"lna_db": "the front end is fixed on this board"},
                }
            ],
        },
        headers=bearer(upload_token),
    )
    tuner = _tuner(client, admin_token, recorder_id, "hackrf-0")
    # The version matched, so the tuner is *not* drifted — a refusal is not a pending edit.
    assert tuner["drift"] is False
    # …and the refusal is the only trace left, which is why it is lifted out of `reported`.
    assert tuner["rejected"] == {"lna_db": "the front end is fixed on this board"}


def test_rejected_is_null_when_the_recorder_refused_nothing(
    client, upload_token, admin_token
) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    assert _tuner(client, admin_token, recorder_id, "airspy-0")["rejected"] is None


def _tuner(client, admin_token, recorder_id, key):  # noqa: ANN001
    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    recorder = next(r for r in rows if r["id"] == recorder_id)
    return next(t for t in recorder["tuners"] if t["tuner_key"] == key)


# ------------------------------------------------------------- desired passthrough ---


def test_every_new_field_reaches_the_desired_state_verbatim(
    client, upload_token, admin_token
) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    body = {
        "gain": 20.0,
        "gains": {"LNA": 10.0, "IF": 4.0},
        "agc": True,
        "ppm": -3.5,
        "auto_ppm": True,
        "auto_ppm_limit": 25.0,
        "bandwidth_hz": 6_000_000,
        "rx_port": "LNAW",
        "settings": {"biastee": "true"},
        "data_follow": True,
        "control_frequencies": [851_050_000, 852_162_500],
        "sample_rate": 2_400_000,
    }
    assert _put(client, admin_token, recorder_id, "airspy-0", body).status_code == 200

    config = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-soapy"},
        headers=bearer(upload_token),
    ).json()["tuners"]["airspy-0"]["config"]
    for field, value in body.items():
        assert config[field] == value, field


def test_an_explicit_null_clears_one_setting(client, upload_token, admin_token) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    _put(client, admin_token, recorder_id, "airspy-0", {"ppm": 4.0, "agc": True})
    _put(client, admin_token, recorder_id, "airspy-0", {"ppm": None})
    config = client.get(
        "/api/recorder/desired-state",
        params={"instance": "rec-soapy"},
        headers=bearer(upload_token),
    ).json()["tuners"]["airspy-0"]["config"]
    assert "ppm" not in config
    # …and leaves the field that was not named alone.
    assert config["agc"] is True


# ------------------------------------------------------------------------ validation ---


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"ppm": 500.0}, "ppm"),
        ({"ppm": -500.0}, "ppm"),
        ({"auto_ppm_limit": -1.0}, "auto_ppm_limit"),
        ({"auto_ppm_limit": 900.0}, "auto_ppm_limit"),
        ({"gains": {"NOPE": 4.0}}, "gains"),
        ({"gains": {"LNA": 99.0}}, "gains"),
        ({"gain": 900.0}, "gain"),
        ({"control_frequencies": [851_050_001]}, "control_frequencies[0]"),
        ({"control_frequencies": [700_000_000]}, "control_frequencies[0]"),
        ({"sample_rate": 2_400_001}, "sample_rate"),
        ({"sample_rate": 12_500 * 3}, "sample_rate"),
        ({"sample_rate": 12_500 * 2}, "sample_rate"),
        ({"bandwidth_hz": -1}, "bandwidth_hz"),
    ],
)
def test_soapy_refusals_name_the_field(client, upload_token, admin_token, body, field) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    resp = _put(client, admin_token, recorder_id, "airspy-0", body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith(f"{field}:"), resp.text


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"lna_db": 25}, "lna_db"),
        ({"lna_db": 48}, "lna_db"),
        ({"lna_db": -8}, "lna_db"),
        ({"vga_db": 21}, "vga_db"),
        ({"vga_db": 64}, "vga_db"),
    ],
)
def test_hackrf_step_refusals(client, upload_token, admin_token, body, field) -> None:
    recorder_id = _seed(client, upload_token, admin_token, HACKRF_STATUS)
    resp = _put(client, admin_token, recorder_id, "hackrf-0", body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith(f"{field}:"), resp.text


@pytest.mark.parametrize("body", [{"lna_db": 24}, {"vga_db": 20}, {"amp": True}])
def test_hackrf_steps_on_the_grid_are_accepted(client, upload_token, admin_token, body) -> None:
    recorder_id = _seed(client, upload_token, admin_token, HACKRF_STATUS)
    assert _put(client, admin_token, recorder_id, "hackrf-0", body).status_code == 200


@pytest.mark.parametrize("field", ["gain", "gains", "agc", "rx_port", "settings"])
def test_soapy_only_fields_are_refused_on_a_hackrf(
    client, upload_token, admin_token, field
) -> None:
    recorder_id = _seed(client, upload_token, admin_token, HACKRF_STATUS)
    value = {"gain": 20.0, "gains": {"LNA": 1.0}, "agc": True, "rx_port": "RX", "settings": {"a": "b"}}
    resp = _put(client, admin_token, recorder_id, "hackrf-0", {field: value[field]})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith(f"{field}:")
    assert "hackrf" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["lna_db", "vga_db", "amp"])
def test_hackrf_only_fields_are_refused_on_a_soapy_device(
    client, upload_token, admin_token, field
) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    value = {"lna_db": 24, "vga_db": 20, "amp": True}
    resp = _put(client, admin_token, recorder_id, "airspy-0", {field: value[field]})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"].startswith(f"{field}:")


def test_per_element_gain_needs_reported_elements(client, upload_token, admin_token) -> None:
    """A tuner that never said which stages it has cannot be given per-stage gain."""
    recorder_id = _seed(
        client,
        upload_token,
        admin_token,
        {"instance_id": "rec-bare", "tuners": [{"tuner_key": "sdr-0", "backend": "soapy"}]},
    )
    resp = _put(client, admin_token, recorder_id, "sdr-0", {"gains": {"LNA": 4.0}})
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("gains:")


def test_an_unreported_tuner_is_still_configurable(client, upload_token, admin_token) -> None:
    """No reported backend, no backend rule; no reported span, no span rule.

    The point of the whole check is to catch a value *this* tuner cannot use.  A tuner that
    has never pushed a status has told keyup nothing, and refusing an edit on that basis
    would make a fresh recorder unconfigurable until it happened to report.
    """
    recorder_id = _seed(
        client,
        upload_token,
        admin_token,
        {"instance_id": "rec-blank", "tuners": [{"tuner_key": "new-0"}]},
    )
    body = {
        "lna_db": 24,
        "gains": None,
        "gain": 900.0,
        "control_frequencies": [151_000_000],
        "ppm": 12.0,
    }
    assert _put(client, admin_token, recorder_id, "new-0", body).status_code == 200


def test_clearing_a_field_is_never_refused(client, upload_token, admin_token) -> None:
    """Withdrawing an opinion is allowed whatever the backend turns out to be."""
    recorder_id = _seed(client, upload_token, admin_token, HACKRF_STATUS)
    # `gain` is Soapy-only and this is a HackRF — but a null is a *clear*, not a set.
    assert _put(client, admin_token, recorder_id, "hackrf-0", {"gain": None}).status_code == 200


def test_settings_are_capped(client, upload_token, admin_token) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    resp = _put(
        client,
        admin_token,
        recorder_id,
        "airspy-0",
        {"settings": {f"k{i}": "v" for i in range(200)}},
    )
    assert resp.status_code == 422
    assert "settings" in resp.json()["detail"]


def test_control_frequency_list_is_capped(client, upload_token, admin_token) -> None:
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    resp = _put(
        client,
        admin_token,
        recorder_id,
        "airspy-0",
        {"control_frequencies": [851_050_000] * 64},
    )
    assert resp.status_code == 422


def test_a_refused_edit_leaves_the_desired_state_untouched(
    client, upload_token, admin_token
) -> None:
    """The validation runs before the merge, so a bad field cannot half-apply a body."""
    recorder_id = _seed(client, upload_token, admin_token, SOAPY_STATUS)
    _put(client, admin_token, recorder_id, "airspy-0", {"ppm": 2.0})
    before = _tuner(client, admin_token, recorder_id, "airspy-0")
    resp = _put(client, admin_token, recorder_id, "airspy-0", {"agc": True, "ppm": 5000.0})
    assert resp.status_code == 422
    after = _tuner(client, admin_token, recorder_id, "airspy-0")
    assert after["desired"] == before["desired"]
    assert after["desired_version"] == before["desired_version"]
