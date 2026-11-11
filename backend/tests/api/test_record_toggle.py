"""The per-talkgroup **Record** toggle end to end (§5.6, docs/TALKGROUPS.md).

Four rows of one table, and they are the whole feature:

| record | transcribe | an exporter selects it | what happens                                  |
| off    | on         | either                 | ingest, transcribe, export, then delete       |
| off    | off        | yes                    | ingest, export, then delete; never transcribed |
| off    | off        | no                     | the recorder is told not to upload it at all  |
| on     | anything   | anything               | today's behaviour, and nothing new is written |

The first three are `calls.audio_policy` and the discard pass (`tests/test_audio_discard.py`);
the fourth column of the desired state — `recorder.skip_talkgroups` — is what this file is
mostly about, because it is the one that stops traffic at the source and so is the one that
must fail closed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


def _set(session, key: str, value) -> None:  # noqa: ANN001
    """Write one runtime setting the way the Admin page does."""
    from app import runtime_settings
    from app.models import Setting

    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = value
    session.commit()
    runtime_settings.invalidate()


def _trunked_system(session):  # noqa: ANN001, ANN202
    from app.models import System

    system = System(name="Test-Trunked", protocol="P25", trunked=True)
    session.add(system)
    session.flush()
    return system


def _talkgroup(session, system, tgid: int, **kwargs):  # noqa: ANN001, ANN202
    from app.models import Talkgroup

    row = Talkgroup(system_id=system.id, tgid=tgid, alias=f"TG {tgid}", **kwargs)
    session.add(row)
    session.flush()
    return row


def _skip_block(client, upload_token: str, instance: str = "rec-skip"):  # noqa: ANN001, ANN202
    body = client.get(
        "/api/recorder/desired-state", params={"instance": instance}, headers=bearer(upload_token)
    ).json()
    return body["recorder"]["skip_talkgroups"]


# ------------------------------------------------------------------ the column itself ---


def test_a_new_talkgroup_records_by_default(client, archive) -> None:  # noqa: ANN001, ARG001
    """The column changes nothing on its own — that is the point of the default."""
    items = client.get("/api/talkgroups").json()["items"]
    assert items
    assert all(item["record"] is True for item in items)


def test_record_is_patchable_and_readable_back(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    tgid = client.get("/api/talkgroups").json()["items"][0]["id"]
    response = client.patch(
        f"/api/talkgroups/{tgid}",
        json={"record": False},
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200, response.text
    assert response.json()["record"] is False
    assert client.get(f"/api/talkgroups/{tgid}").json()["record"] is False


def test_record_is_a_bulk_edit_field(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    ids = [t["id"] for t in client.get("/api/talkgroups").json()["items"]][:3]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"record": False}},
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == len(ids)
    assert all(item["record"] is False for item in response.json()["items"])

    stored = {t["id"]: t for t in client.get("/api/talkgroups").json()["items"]}
    assert all(stored[i]["record"] is False for i in ids)


def test_a_reader_may_not_write_it(client, archive, read_token) -> None:  # noqa: ANN001, ARG001
    tgid = client.get("/api/talkgroups").json()["items"][0]["id"]
    response = client.patch(
        f"/api/talkgroups/{tgid}",
        json={"record": False},
        headers={**bearer(read_token), **CSRF_HEADERS},
    )
    assert response.status_code == 403


def test_the_export_csv_carries_it_beside_transcribe(client, archive) -> None:  # noqa: ANN001, ARG001
    header = client.get("/api/talkgroups/export").text.splitlines()[0].split(",")
    assert "record" in header
    assert header.index("record") == header.index("transcribe") - 1


# ------------------------------------------------------------------------- the ingest ---


def test_ingest_writes_discard_when_record_is_off(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """`audio_policy` is read off the toggle exactly as `transcript_status` is."""
    from app.ingest.service import ingest_file
    from app.models import Call, Talkgroup
    from tests.api.conftest import FIXTURE_FILES

    first, second = FIXTURE_FILES[2], FIXTURE_FILES[3]  # both TG 1701
    result = ingest_file(session, fixture_audio / first, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    # Nothing has been turned off yet, so the archive's whole existing behaviour is `keep`.
    assert call.audio_policy == "keep"

    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.record = False
    session.commit()

    result = ingest_file(session, fixture_audio / second, settings=settings, audio_root=fixture_audio)
    later = session.get(Call, result.call_id)
    assert later.audio_policy == "discard"
    # The audio is still stored: transcription and the exporters both read the file, and
    # the discard pass is what takes it away afterwards.
    assert later.audio_state == "present"
    assert later.file_path


def test_a_call_with_no_talkgroup_is_always_kept(session) -> None:  # noqa: ANN001
    """Conventional traffic and private calls have no toggle to read."""
    from app.ingest.service import audio_policy_for

    assert audio_policy_for(None) == "keep"


def test_the_policy_is_not_re_derived_when_the_toggle_moves_back(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """Turning Record back on must not make yesterday's calls keepers, or the reverse."""
    from app.ingest.service import ingest_file
    from app.models import Call, Talkgroup
    from tests.api.conftest import FIXTURE_FILES

    result = ingest_file(
        session, fixture_audio / FIXTURE_FILES[2], settings=settings, audio_root=fixture_audio
    )
    call = session.get(Call, result.call_id)
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.record = False
    session.commit()
    session.refresh(call)
    assert call.audio_policy == "keep"


# ---------------------------------------------------------- recorder.skip_talkgroups ---


def _enable_exporters(session) -> None:  # noqa: ANN001
    """Switch all three services on: only an enabled service can keep a talkgroup coming."""
    for key in ("export_broadcastify_enabled", "export_broadcastify_feed_enabled", "export_openmhz_enabled"):
        _set(session, key, True)


def test_nothing_is_skipped_in_all_mode(client, session, upload_token) -> None:  # noqa: ANN001
    """The shipped mode. Every service selects everything, so nothing can be skipped."""
    system = _trunked_system(session)
    _talkgroup(session, system, 1055, record=False, transcribe=False)
    session.commit()
    _enable_exporters(session)

    assert _skip_block(client, upload_token) is None


def test_a_flag_on_a_disabled_service_keeps_nothing(client, session, upload_token) -> None:  # noqa: ANN001
    """OpenMHz ticked but OpenMHz switched off: the flag has no use, so the talkgroup is skipped."""
    system = _trunked_system(session)
    _talkgroup(
        session, system, 1055, record=False, transcribe=False,
        export_broadcastify=False, export_bcfy_calls=False, export_openmhz=True,
    )
    session.commit()
    for service in ("broadcastify", "broadcastify_feed", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")
    _set(session, "export_broadcastify_enabled", True)
    _set(session, "export_broadcastify_feed_enabled", True)
    _set(session, "export_openmhz_enabled", False)

    assert (_skip_block(client, upload_token) or {}).get("tgids") == [1055]


def test_a_talkgroup_nothing_wants_is_skipped(client, session, upload_token) -> None:  # noqa: ANN001
    import hashlib

    import orjson

    system = _trunked_system(session)
    _talkgroup(
        session,
        system,
        1055,
        record=False,
        transcribe=False,
        export_broadcastify=False,
        export_bcfy_calls=False,
        export_openmhz=False,
    )
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    block = _skip_block(client, upload_token)
    assert block == {
        "tgids": [1055],
        "revision": hashlib.sha256(orjson.dumps([1055])).hexdigest()[:12],
    }


@pytest.mark.parametrize(
    "flag", ["export_broadcastify", "export_bcfy_calls", "export_openmhz"]
)
def test_any_one_export_selection_keeps_the_talkgroup_coming(client, session, upload_token, flag) -> None:  # noqa: ANN001
    """One service still publishing it is enough: the audio has a use."""
    system = _trunked_system(session)
    flags = {
        "export_broadcastify": False,
        "export_bcfy_calls": False,
        "export_openmhz": False,
        flag: True,
    }
    _talkgroup(session, system, 1055, record=False, transcribe=False, **flags)
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")
    _enable_exporters(session)

    assert _skip_block(client, upload_token) is None


def test_transcribe_on_keeps_the_talkgroup_coming(client, session, upload_token) -> None:  # noqa: ANN001
    """Row two of the table: the audio is still the transcriber's input."""
    system = _trunked_system(session)
    _talkgroup(
        session,
        system,
        1055,
        record=False,
        transcribe=True,
        export_broadcastify=False,
        export_bcfy_calls=False,
        export_openmhz=False,
    )
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    assert _skip_block(client, upload_token) is None


def test_record_on_keeps_the_talkgroup_coming(client, session, upload_token) -> None:  # noqa: ANN001
    """Row four: the operator wants the recording, so of course it is uploaded."""
    system = _trunked_system(session)
    _talkgroup(
        session,
        system,
        1055,
        record=True,
        transcribe=False,
        export_broadcastify=False,
        export_bcfy_calls=False,
        export_openmhz=False,
    )
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    assert _skip_block(client, upload_token) is None


def test_deny_mode_reads_the_same_column(client, session, upload_token) -> None:  # noqa: ANN001
    """`allow` and `deny` differ in what a default-true row means, not in what they read."""
    system = _trunked_system(session)
    _talkgroup(
        session,
        system,
        1055,
        record=False,
        transcribe=False,
        export_broadcastify=False,
        export_bcfy_calls=False,
        export_openmhz=False,
    )
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "deny")

    assert _skip_block(client, upload_token)["tgids"] == [1055]


def test_one_system_still_wanting_a_tgid_keeps_it_off_the_list(client, session, upload_token) -> None:  # noqa: ANN001
    """A recorder has a number, not a system id, so the answer has to hold everywhere."""
    from app.models import System

    first = _trunked_system(session)
    second = System(name="Test-Trunked-2", protocol="P25", trunked=True)
    session.add(second)
    session.flush()
    off = {
        "record": False,
        "transcribe": False,
        "export_broadcastify": False,
        "export_bcfy_calls": False,
        "export_openmhz": False,
    }
    _talkgroup(session, first, 1055, **off)
    _talkgroup(session, second, 1055, **{**off, "transcribe": True})
    _talkgroup(session, first, 1099, **off)
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    assert _skip_block(client, upload_token)["tgids"] == [1099]


def test_the_revision_moves_only_when_the_list_does(client, session, upload_token) -> None:  # noqa: ANN001
    off = {
        "record": False,
        "transcribe": False,
        "export_broadcastify": False,
        "export_bcfy_calls": False,
        "export_openmhz": False,
    }
    system = _trunked_system(session)
    first = _talkgroup(session, system, 1055, **off)
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    before = _skip_block(client, upload_token)["revision"]
    assert _skip_block(client, upload_token)["revision"] == before

    _talkgroup(session, system, 1099, **off)
    session.commit()
    after = _skip_block(client, upload_token)
    assert after["tgids"] == [1055, 1099]
    assert after["revision"] != before

    # …and the recorder-level `version` is untouched throughout: this is derived from the
    # talkgroup table, not from an operator's edit of the recorder.
    body = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-skip"}, headers=bearer(upload_token)
    ).json()
    assert body["recorder"]["version"] == 0
    assert first.tgid == 1055


def test_a_conventional_system_is_never_on_the_list(client, session, upload_token) -> None:  # noqa: ANN001
    """The list is what a *trunked* recorder drops; conventional traffic is not keyed by tgid."""
    from app.models import System

    system = System(name="Test-Conventional", protocol="FMN", trunked=False)
    session.add(system)
    session.flush()
    _talkgroup(
        session,
        system,
        1055,
        record=False,
        transcribe=False,
        export_broadcastify=False,
        export_bcfy_calls=False,
        export_openmhz=False,
    )
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")

    assert _skip_block(client, upload_token) is None


def test_the_admin_render_shows_the_count_and_not_the_list(client, session, upload_token, admin_token) -> None:  # noqa: ANN001
    off = {
        "record": False,
        "transcribe": False,
        "export_broadcastify": False,
        "export_bcfy_calls": False,
        "export_openmhz": False,
    }
    system = _trunked_system(session)
    _talkgroup(session, system, 1055, **off)
    _talkgroup(session, system, 1099, **off)
    session.commit()
    for service in ("broadcastify", "openmhz"):
        _set(session, f"export_{service}_talkgroup_mode", "allow")
    _skip_block(client, upload_token)  # create the recorder row

    rows = client.get("/api/admin/recorders", headers=bearer(admin_token)).json()
    assert rows
    summary = rows[0]["desired_skip_talkgroups"]
    assert summary["count"] == 2
    assert "tgids" not in summary


# ----------------------------------------------------------------------------- stats ---


def overview(client):  # noqa: ANN001, ANN201
    """``GET /api/stats/overview``, freshly measured.

    The endpoint caches its dozen aggregates for a minute, because ``StatusBadges`` lives in
    the app shell and every open tab polls this once a minute for as long as it is open
    (:data:`app.services.stats.OVERVIEW_TTL_S`).  These tests write a row and want the next
    read to see it, which is the one thing a dashboard cache deliberately does not promise —
    so they drop the entry rather than assert something the endpoint no longer claims.  What
    is under test here is the counting, not the cache policy.
    """
    from app.services import counts
    from app.services.stats import OVERVIEW_KEY

    counts.invalidate(OVERVIEW_KEY)
    return client.get("/api/stats/overview").json()


def test_the_overview_counts_the_discarded_audio(client, session, archive) -> None:  # noqa: ANN001
    from app.models import Call

    assert overview(client)["audio_discarded_24h"] == 0

    call = session.scalar(select(Call).where(Call.id == archive.call_ids[0]))
    call.audio_policy = "discard"
    call.audio_state = "discarded"
    session.commit()

    assert overview(client)["audio_discarded_24h"] == 1
    # …and it is still a call in every other count: only the recording went.
    assert overview(client)["calls_total"] >= 1


def test_the_overview_counts_the_calls_nobody_spoke_on(client, session, archive) -> None:  # noqa: ANN001
    """`no_speech_24h`, the footnote beside the discard count on the Stats page.

    A mic key-up is a call like any other in `calls_24h` — the radio really did key up.  It
    is simply never transcribed, never exported, and (with `audio_discard_no_speech` on) a
    day later it is one of the discards counted above.
    """
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from app.models import Call

    assert overview(client)["no_speech_24h"] == 0

    # Counted on ``started_at``, like every other 24-hour number here — it is a subset of
    # ``calls_24h`` — so the row has to be recent traffic rather than one of the corpus's.
    old = session.scalar(select(Call).where(Call.id == archive.call_ids[0]))
    started = datetime.now(UTC) - timedelta(minutes=3)
    recent = Call(
        system_id=old.system_id,
        talkgroup_id=old.talkgroup_id,
        talkgroup_ids=list(old.talkgroup_ids or []),
        recorded_at=started,
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=0.648),
        duration_s=Decimal("0.648"),
        file_path="kerchunk/2395971.mp3",
        file_name="2395971.mp3",
        audio_meta={"content": "none", "speech_s": 0.04, "speech_floor_db": -40.0},
    )
    session.add(recent)
    session.commit()

    body = overview(client)
    assert body["no_speech_24h"] == 1
    assert body["calls_24h"] >= 1

    # A call the audio pass measured speech in is not one of them.
    recent.audio_meta = {"content": "speech", "speech_s": 1.4}
    session.commit()
    assert overview(client)["no_speech_24h"] == 0
