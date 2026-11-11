"""The audio-profile API: the catalog, CRUD and its guards, the preview and the apply.

The interesting behavior here is not the CRUD, it is the *refusals*: a builtin cannot be
deleted, a profile a channel is still on cannot be deleted, and a chain holding anything the
whitelist does not describe cannot be stored or previewed.  Those are what stand between an
operator's text box and an ffmpeg command line, so each of them gets a case.

``seed_audio_profiles`` is called explicitly: the ``session`` fixture truncates every table,
including the three rows the migration inserted, so a test that wants the builtins asks for
them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.api.conftest import CSRF_HEADERS

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **CSRF_HEADERS}


AIRCRAFT = [
    {"kind": "highpass", "params": {"f": 250}},
    {"kind": "equalizer", "params": {"f": 1950, "width": 500, "gain_db": -40}},
    {"kind": "afftdn", "params": {"nf": -28, "nr": 12}},
]


@pytest.fixture
def builtins(session):  # noqa: ANN001, ANN201
    """The three shipped profiles, as a fresh database has them."""
    from app.seed import seed_audio_profiles

    seed_audio_profiles(session)
    session.commit()
    return session


def profile_named(client, token: str, name: str) -> dict:  # noqa: ANN001
    listing = client.get("/api/admin/audio/profiles", headers=auth(token)).json()
    return next(row for row in listing if row["name"] == name)


# --------------------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------------------


def test_the_step_catalog_is_readable_and_complete(client, read_token) -> None:  # noqa: ANN001
    body = client.get("/api/admin/audio/profiles/steps", headers=auth(read_token)).json()
    assert body["max_steps"] == 12
    kinds = {entry["kind"] for entry in body["steps"]}
    assert {"highpass", "equalizer", "afftdn", "agate"} <= kinds
    equalizer = next(e for e in body["steps"] if e["kind"] == "equalizer")
    gain = next(p for p in equalizer["params"] if p["name"] == "gain_db")
    assert (gain["low"], gain["high"], gain["default"]) == (-60.0, 20.0, -40.0)


# --------------------------------------------------------------------------------------
# listing and the builtins
# --------------------------------------------------------------------------------------


def test_the_builtins_are_listed_with_their_rendered_chains(client, builtins, read_token) -> None:  # noqa: ANN001, ARG001
    rows = client.get("/api/admin/audio/profiles", headers=auth(read_token)).json()
    assert [row["name"] for row in rows] == ["Aircraft cabin", "Default", "Speech band-pass"]
    assert all(row["builtin"] for row in rows)
    aircraft = next(row for row in rows if row["name"] == "Aircraft cabin")
    assert aircraft["rendered"] == (
        "highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h,afftdn=nf=-28:nr=12,"
        "loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50"
    )
    assert aircraft["talkgroup_count"] == 0


def test_seeding_twice_adds_nothing(session, builtins) -> None:  # noqa: ANN001, ARG001
    from app.models import AudioProfile
    from app.seed import seed_audio_profiles

    assert seed_audio_profiles(session) == 0
    assert len(list(session.scalars(select(AudioProfile)))) == 3


# --------------------------------------------------------------------------------------
# create / edit / delete
# --------------------------------------------------------------------------------------


def test_create_edit_and_delete_a_profile(client, admin_token) -> None:  # noqa: ANN001
    created = client.post(
        "/api/admin/audio/profiles",
        json={"name": "Helicopter", "description": "rotor", "chain": AIRCRAFT},
        headers=auth(admin_token),
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["builtin"] is False
    assert row["rendered"].startswith("highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h")
    # the stored chain carries the defaults the operator did not type
    assert row["chain"][0] == {"kind": "highpass", "params": {"f": 250.0}}

    edited = client.patch(
        f"/api/admin/audio/profiles/{row['id']}",
        json={"name": "Helicopter cabin", "chain": [{"kind": "lowpass", "params": {"f": 3400}}]},
        headers=auth(admin_token),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Helicopter cabin"
    assert edited.json()["rendered"].startswith("lowpass=f=3400,loudnorm=")
    # the description was not in the body, so it was not touched
    assert edited.json()["description"] == "rotor"

    assert client.delete(f"/api/admin/audio/profiles/{row['id']}", headers=auth(admin_token)).status_code == 204
    assert client.get("/api/admin/audio/profiles", headers=auth(admin_token)).json() == []


def test_a_duplicate_name_is_a_conflict(client, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    clash = client.post(
        "/api/admin/audio/profiles", json={"name": "Default", "chain": []}, headers=auth(admin_token)
    )
    assert clash.status_code == 409
    assert "already exists" in clash.json()["detail"]


def test_renaming_onto_another_profile_is_a_conflict(client, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    aircraft = profile_named(client, admin_token, "Aircraft cabin")
    response = client.patch(
        f"/api/admin/audio/profiles/{aircraft['id']}",
        json={"name": "Default"},
        headers=auth(admin_token),
    )
    assert response.status_code == 409


def test_a_builtin_is_editable_but_not_deletable(client, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    default = profile_named(client, admin_token, "Default")
    edited = client.patch(
        f"/api/admin/audio/profiles/{default['id']}",
        json={"chain": [{"kind": "afftdn", "params": {"nf": -30}}]},
        headers=auth(admin_token),
    )
    assert edited.status_code == 200
    assert edited.json()["rendered"].startswith("afftdn=nf=-30,loudnorm=")
    assert edited.json()["builtin"] is True

    refused = client.delete(f"/api/admin/audio/profiles/{default['id']}", headers=auth(admin_token))
    assert refused.status_code == 409
    assert "built-in" in refused.json()["detail"]


def test_deleting_a_profile_in_use_is_refused_with_the_count(client, session, archive, admin_token) -> None:  # noqa: ANN001
    from app.models import Talkgroup

    made = client.post(
        "/api/admin/audio/profiles",
        json={"name": "Helicopter", "chain": AIRCRAFT},
        headers=auth(admin_token),
    ).json()
    talkgroups = list(session.scalars(select(Talkgroup).limit(2)))
    for talkgroup in talkgroups:
        talkgroup.audio_profile_id = made["id"]
    session.commit()

    refused = client.delete(f"/api/admin/audio/profiles/{made['id']}", headers=auth(admin_token))
    assert refused.status_code == 409
    assert f"{len(talkgroups)} talkgroup(s)" in refused.json()["detail"]
    assert profile_named(client, admin_token, "Helicopter")["talkgroup_count"] == len(talkgroups)


def test_a_missing_profile_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    assert client.patch("/api/admin/audio/profiles/9999", json={}, headers=auth(admin_token)).status_code == 404
    assert client.delete("/api/admin/audio/profiles/9999", headers=auth(admin_token)).status_code == 404


def test_writing_needs_the_admin_scope(client, write_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/profiles", json={"name": "Nope", "chain": []}, headers=auth(write_token)
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# the whitelist, over HTTP
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chain",
    [
        [{"kind": "amovie", "params": {}}],
        [{"kind": "highpass", "params": {"f": 300, "rm": "-rf"}}],
        [{"kind": "equalizer", "params": {"f": 1950, "width": 500, "gain_db": -400}}],
        [{"kind": "highpass", "params": {"f": 300}}] * 13,
    ],
)
def test_a_chain_the_whitelist_refuses_cannot_be_stored(client, admin_token, chain) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/profiles", json={"name": "Bad", "chain": chain}, headers=auth(admin_token)
    )
    assert response.status_code == 422, response.text


# --------------------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_preview_returns_audio_and_stores_nothing(client, session, archive, admin_token) -> None:  # noqa: ANN001
    from app.models import Call

    call_id = archive.call_ids[0]
    before = session.get(Call, call_id)
    was = (before.processed_path, before.audio_meta)

    response = client.post(
        "/api/admin/audio/profiles/preview",
        json={"call_id": call_id, "chain": AIRCRAFT, "seconds": 3},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/mpeg"
    assert len(response.content) > 100
    assert response.headers["x-keyup-filter"].startswith("highpass=f=250,equalizer=")

    session.expire_all()
    after = session.get(Call, call_id)
    assert (after.processed_path, after.audio_meta) == was


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_an_empty_preview_chain_is_the_loudness_tail_alone(client, archive, admin_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/profiles/preview",
        json={"call_id": archive.call_ids[0], "chain": []},
        headers=auth(admin_token),
    )
    assert response.status_code == 200
    assert response.headers["x-keyup-filter"] == (
        "loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50"
    )


def test_preview_refuses_a_bad_chain_before_it_reaches_ffmpeg(client, archive, admin_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/profiles/preview",
        json={"call_id": archive.call_ids[0], "chain": [{"kind": "amovie", "params": {}}]},
        headers=auth(admin_token),
    )
    assert response.status_code == 422


def test_preview_of_a_missing_call_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/profiles/preview",
        json={"call_id": 999_999, "chain": []},
        headers=auth(admin_token),
    )
    assert response.status_code == 404


def test_preview_caps_the_length_it_will_render(client, archive, admin_token) -> None:  # noqa: ANN001
    """61 seconds is refused by the schema, not silently clamped somewhere downstream."""
    response = client.post(
        "/api/admin/audio/profiles/preview",
        json={"call_id": archive.call_ids[0], "chain": [], "seconds": 61},
        headers=auth(admin_token),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# assignment and apply
# --------------------------------------------------------------------------------------


def test_a_talkgroup_can_be_pointed_at_a_profile_and_back(  # noqa: ANN001
    client, session, archive, builtins, admin_token
) -> None:  # noqa: ARG001
    from app.models import Talkgroup

    aircraft = profile_named(client, admin_token, "Aircraft cabin")
    talkgroup = session.scalars(select(Talkgroup)).first()

    patched = client.patch(
        f"/api/talkgroups/{talkgroup.id}",
        json={"audio_profile_id": aircraft["id"]},
        headers=auth(admin_token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["audio_profile_id"] == aircraft["id"]
    detail = client.get(f"/api/talkgroups/{talkgroup.id}", headers=auth(admin_token)).json()
    assert detail["audio_profile_name"] == "Aircraft cabin"

    cleared = client.patch(
        f"/api/talkgroups/{talkgroup.id}", json={"audio_profile_id": None}, headers=auth(admin_token)
    )
    assert cleared.json()["audio_profile_id"] is None


def test_an_unknown_profile_is_refused_on_a_talkgroup(client, session, archive, admin_token) -> None:  # noqa: ANN001
    from app.models import Talkgroup

    talkgroup = session.scalars(select(Talkgroup)).first()
    response = client.patch(
        f"/api/talkgroups/{talkgroup.id}", json={"audio_profile_id": 4242}, headers=auth(admin_token)
    )
    assert response.status_code == 422
    assert "no audio profile 4242" in response.json()["detail"]


def test_the_bulk_drawer_can_assign_a_profile(client, session, archive, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    from app.models import Talkgroup

    aircraft = profile_named(client, admin_token, "Aircraft cabin")
    ids = [tg.id for tg in session.scalars(select(Talkgroup).limit(2))]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"audio_profile_id": aircraft["id"]}},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == len(ids)
    assert all(item["audio_profile_id"] == aircraft["id"] for item in response.json()["items"])


def test_apply_assigns_and_requeues_recent_calls(client, session, archive, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    from app.models import AudioJob, Call, Talkgroup

    session.query(AudioJob).delete()
    session.commit()

    call = session.get(Call, archive.transcribed_id)
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    aircraft = profile_named(client, admin_token, "Aircraft cabin")

    response = client.post(
        f"/api/admin/audio/profiles/{aircraft['id']}/apply",
        json={"talkgroup_ids": [talkgroup.id], "hours": 24 * 365},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile_name"] == "Aircraft cabin"
    assert body["assigned"] == 1
    assert body["talkgroups"] == 1
    assert body["queued"] >= 1
    assert body["matched"] == body["queued"] + body["already_open"]

    session.expire_all()
    assert session.get(Talkgroup, talkgroup.id).audio_profile_id == aircraft["id"]
    jobs = list(session.scalars(select(AudioJob)))
    assert jobs and {job.priority for job in jobs} == {600}
    assert {job.call_id for job in jobs} <= set(archive.call_ids)


def test_apply_with_all_with_profile_sweeps_without_assigning(  # noqa: ANN001
    client, session, archive, builtins, admin_token
) -> None:  # noqa: ARG001
    from app.models import AudioJob, Call, Talkgroup

    call = session.get(Call, archive.transcribed_id)
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    aircraft = profile_named(client, admin_token, "Aircraft cabin")
    talkgroup.audio_profile_id = aircraft["id"]
    session.commit()
    session.query(AudioJob).delete()
    session.commit()

    response = client.post(
        f"/api/admin/audio/profiles/{aircraft['id']}/apply",
        json={"all_with_profile": True, "hours": 24 * 365},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["assigned"] == 0
    assert response.json()["talkgroups"] == 1
    assert response.json()["queued"] >= 1


def test_apply_while_audio_is_off_assigns_but_queues_nothing(  # noqa: ANN001
    client, session, archive, builtins, admin_token
) -> None:  # noqa: ARG001
    """The assignment is configuration and stands; the re-render would only be cancelled
    by the worker on its next idle cycle."""
    from app import runtime_settings
    from app.models import AudioJob, Call, Setting, Talkgroup

    session.query(AudioJob).delete()
    session.add(Setting(key="audio_process_enabled", value=False))
    session.commit()
    runtime_settings.invalidate()
    call = session.get(Call, archive.transcribed_id)
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    aircraft = profile_named(client, admin_token, "Aircraft cabin")

    response = client.post(
        f"/api/admin/audio/profiles/{aircraft['id']}/apply",
        json={"talkgroup_ids": [talkgroup.id], "hours": 24 * 365},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["assigned"], body["talkgroups"]) == (1, 1)
    assert (body["matched"], body["queued"], body["already_open"]) == (0, 0, 0)
    session.expire_all()
    assert session.get(Talkgroup, talkgroup.id).audio_profile_id == aircraft["id"]
    assert session.query(AudioJob).count() == 0


def test_apply_on_a_profile_nobody_uses_queues_nothing(client, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    speech = profile_named(client, admin_token, "Speech band-pass")
    response = client.post(
        f"/api/admin/audio/profiles/{speech['id']}/apply",
        json={"all_with_profile": True},
        headers=auth(admin_token),
    )
    assert response.status_code == 200
    assert (response.json()["talkgroups"], response.json()["queued"]) == (0, 0)


def test_apply_refuses_a_talkgroup_that_does_not_exist(client, builtins, admin_token) -> None:  # noqa: ANN001, ARG001
    aircraft = profile_named(client, admin_token, "Aircraft cabin")
    response = client.post(
        f"/api/admin/audio/profiles/{aircraft['id']}/apply",
        json={"talkgroup_ids": [8888]},
        headers=auth(admin_token),
    )
    assert response.status_code == 422
    assert "no talkgroup 8888" in response.json()["detail"]


# --------------------------------------------------------------------------------------
# the processing pass
# --------------------------------------------------------------------------------------


def scratch_settings(call, settings, tmp_path: Path):  # noqa: ANN001, ANN201
    """``settings`` rooted at ``tmp_path``, with a copy of ``call``'s recording in it.

    ``process_call`` writes ``<stem>.proc.mp3`` beside the recording; pointed at the shared
    fixture tree it leaves one behind there, which the ingest-walk tests then count.
    """
    from app.audio import audio_path

    copy = tmp_path / call.file_path
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(audio_path(call, settings), copy)
    return settings.model_copy(update={"audio_root": tmp_path})


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_a_call_on_a_profiled_talkgroup_is_processed_through_it(  # noqa: ANN001
    session, settings, archive, builtins, tmp_path
) -> None:  # noqa: ARG001
    """The whole point, end to end: the profile's filters are in the string ffmpeg ran."""
    from app.audioproc.jobs import process_call
    from app.models import AudioProfile, Call, Talkgroup

    call = session.get(Call, archive.call_ids[0])
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    aircraft = session.scalar(select(AudioProfile).where(AudioProfile.name == "Aircraft cabin"))
    talkgroup.audio_profile_id = aircraft.id
    session.commit()

    meta = process_call(session, call, scratch_settings(call, settings, tmp_path))
    assert meta["profile"] == "Aircraft cabin"
    assert "equalizer=f=1950:w=500:g=-40:t=h" in meta["filter"]
    assert "agate=" not in meta["filter"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_a_call_with_no_profile_still_gets_the_global_chain(  # noqa: ANN001
    session, settings, archive, tmp_path
) -> None:
    from app.audioproc.jobs import process_call
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    meta = process_call(session, call, scratch_settings(call, settings, tmp_path))
    assert meta["profile"] is None
    assert "highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18" in meta["filter"]
    assert "agate=threshold=0.003162" in meta["filter"]


def test_a_stored_chain_that_will_not_render_falls_back_to_the_global_chain(  # noqa: ANN001
    session, archive, builtins
) -> None:  # noqa: ARG001
    """Impossible through the API; a talkgroup must not be un-processable if it happens."""
    from app.audioproc.jobs import profile_for
    from app.models import AudioProfile, Call, Talkgroup

    call = session.get(Call, archive.call_ids[0])
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    aircraft = session.scalar(select(AudioProfile).where(AudioProfile.name == "Aircraft cabin"))
    aircraft.chain = [{"kind": "amovie", "params": {}}]
    talkgroup.audio_profile_id = aircraft.id
    session.commit()

    assert profile_for(session, call) == (None, None)
