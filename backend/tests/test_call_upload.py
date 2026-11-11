"""``POST /api/call-upload`` — sdrtrunk's Rdio-Scanner contract, end to end.

The bodies are literal because sdrtrunk string-matches them; the assertions here are
therefore byte-exact on purpose.  Two audio shapes are covered: a real corpus recording
with sdrtrunk's ID3 frames, and the same file with every tag stripped, which is what the
streaming module sends when it re-encodes — then the form fields are all we have.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mutagen.mp3 import MP3
from sqlalchemy import select

from app.api import call_upload as upload_module
from app.api.call_upload import router
from app.api.uploads import body_size_guard
from app.config import Settings
from app.db import get_session
from app.models import ApiToken, Call, CallExport, IngestFile, Patch, Talkgroup, TranscriptionJob
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

TAGGED = "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
KEY = "test-upload-key"
#: the literal sdrtrunk string-matches on success
OK = "Call imported successfully."


@pytest.fixture
def upload_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
        conversation_gap_seconds=20,
    )


@pytest.fixture
def client(session, upload_settings, monkeypatch):  # noqa: ANN001, ANN201
    """The router on a throwaway app, wired to the test session and a tmp audio root."""
    monkeypatch.setattr(upload_module, "get_settings", lambda: upload_settings)
    app = FastAPI()
    # The same body-size middleware the real application installs (H4): the size check has
    # to happen before the multipart parse, so it cannot be a route dependency.
    app.middleware("http")(body_size_guard)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(session):  # noqa: ANN001, ANN201
    row = ApiToken(
        label="sdrtrunk",
        token_hash=hashlib.sha256(KEY.encode()).hexdigest(),
        token_prefix=KEY[:8],
        scopes=["upload"],
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def tagged_mp3(fixture_audio) -> Path:  # noqa: ANN001
    path = fixture_audio / TAGGED
    if not path.is_file():  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip(f"fixture recording missing: {path}")
    return path


@pytest.fixture
def stripped_mp3(tagged_mp3, tmp_path_factory) -> Path:  # noqa: ANN001
    """The same audio with every ID3 frame removed — the streaming module's shape."""
    target = tmp_path_factory.mktemp("stripped") / "raw.mp3"
    target.write_bytes(tagged_mp3.read_bytes())
    audio = MP3(str(target))
    audio.delete()
    audio.save()
    return target


def post(client, mp3: Path, **fields):  # noqa: ANN001, ANN003
    """One upload, with sdrtrunk's field names and its defaults."""
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())),
        "talkgroup": "1701",
        "source": "9200006",
        "frequency": "852162500",
        "systemLabel": "Example System",
        "talkgroupLabel": "EXAMPLE EVENT 1",
        "talkgroupGroup": "Example County",
        "talkerAlias": "IC ENG 1",
    }
    form.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": (fields.get("audioName") or "call.mp3", mp3.read_bytes(), "audio/mpeg")},
    )


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


def test_a_bad_key_is_rejected(client, token, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, key="nope")
    assert response.status_code == 401
    assert response.text == "invalid key"


def test_a_missing_key_is_rejected(client, token, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, key="")
    assert response.status_code == 401
    assert response.text == "invalid key"


def test_a_token_without_the_upload_scope_is_rejected(client, session, tagged_mp3) -> None:  # noqa: ANN001
    session.add(
        ApiToken(
            label="reader",
            token_hash=hashlib.sha256(KEY.encode()).hexdigest(),
            scopes=["read"],
        )
    )
    session.commit()

    response = post(client, tagged_mp3)
    assert response.status_code == 401
    assert response.text == "invalid key"


def test_a_revoked_token_is_rejected(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    token.revoked_at = datetime.now(UTC)
    session.commit()
    assert post(client, tagged_mp3).text == "invalid key"


# --------------------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------------------


def test_the_test_probe_answers_with_the_literal_sdrtrunk_expects(client, token) -> None:  # noqa: ANN001
    response = client.post("/api/call-upload", data={"key": KEY, "system": "1", "test": "1"})
    assert response.status_code == 200
    assert response.text == "incomplete call data: no talkgroup"


def test_a_call_without_a_talkgroup_gets_the_same_answer(client, token, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, talkgroup=None)
    assert response.status_code == 200
    assert response.text == "incomplete call data: no talkgroup"


# --------------------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------------------


def test_a_tagged_recording_round_trips(client, session, token, tagged_mp3, tmp_path) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, audioName="1701-1756494000.mp3")

    assert response.status_code == 200
    assert response.text == "Call imported successfully."

    call = session.execute(select(Call)).scalar_one()
    assert call.ingest_source == "upload"
    assert call.time_source == "upload"
    # dateTime is the recorder's own call start, used verbatim
    assert call.started_at == datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)
    assert call.ended_at > call.started_at
    assert call.talker_alias == "IC ENG 1"
    assert call.talkgroup_ids == [1701]
    assert call.frequency_hz == 852162500
    assert call.radio_raw == "9200006"
    assert call.sha256 and call.file_size

    # stored under uploads/YYYY/MM/DD in the recorder's own zone (19:17 UTC = 14:17 CDT)
    # stored under the *call's* date, in the recorder's own zone, in the sha256 bucket
    assert call.file_path == f"uploads/2026/08/29/{call.sha256[:2]}/1701-1756494000.mp3"
    assert (tmp_path / call.file_path).is_file()

    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    assert talkgroup.tgid == 1701
    assert talkgroup.group_name == "Example County"

    job = session.execute(select(TranscriptionJob)).scalar_one()
    assert job.call_id == call.id and job.status == "queued"
    assert call.transcript_status == "queued"

    ledger = session.execute(select(IngestFile)).scalar_one()
    assert (ledger.status, ledger.call_id) == ("ingested", call.id)


def test_an_untagged_recording_falls_back_to_the_form(client, session, token, stripped_mp3) -> None:  # noqa: ANN001
    """No ID3, no sdrtrunk filename — every field has to come from the form."""
    response = post(client, stripped_mp3, audioName="stream-chunk.mp3", talkgroup="1753")

    assert response.text == "Call imported successfully."
    call = session.execute(select(Call)).scalar_one()
    assert call.system.name == "Example System"
    assert call.talkgroup_alias_at_record == "EXAMPLE EVENT 1"
    assert call.talkgroup_ids == [1753]
    assert call.started_at == datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)
    assert float(call.duration_s) > 0  # the MP3 header still gives us the duration
    assert session.get(Talkgroup, call.talkgroup_id).alias == "EXAMPLE EVENT 1"


def test_the_audio_name_is_sanitized(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    post(client, tagged_mp3, audioName="../../etc/pass wd")
    call = session.execute(select(Call)).scalar_one()
    assert call.file_path == f"uploads/2026/08/29/{call.sha256[:2]}/pass_wd.mp3"


def test_a_missing_audio_name_is_generated(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    post(client, tagged_mp3)
    call = session.execute(select(Call)).scalar_one()
    assert call.file_path.startswith("uploads/2026/08/29/")
    assert call.file_path.endswith(".mp3")


def test_patches_become_talkgroup_ids_and_a_patch_row(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    post(client, tagged_mp3, talkgroup="1157", patches="[1157, 1812, 1561, 1811]")

    call = session.execute(select(Call)).scalar_one()
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [1157, 1812, 1561, 1811]
    patch = session.get(Patch, call.patch_id)
    assert patch.patch_id == 1157
    assert patch.member_tgids == [1812, 1561, 1811]


def test_a_trunkd_patch_call_links_its_supergroup_and_members(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    # trunkd files the call under its supergroup (``talkgroup``) and posts the members as a
    # bare list, so the supergroup is the patch id even though it never appears in ``patches``.
    post_trunkd(
        client,
        ("call.mp3", tagged_mp3.read_bytes(), "audio/mpeg"),
        talkgroup="14001",
        patches="[1350, 1753]",
    )

    call = session.execute(select(Call)).scalar_one()
    assert call.ingest_source == "trunkd"
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [14001, 1350, 1753]
    patch = session.get(Patch, call.patch_id)
    assert patch.patch_id == 14001
    assert patch.member_tgids == [1350, 1753]


# --------------------------------------------------------------------------------------
# dedupe and limits
# --------------------------------------------------------------------------------------


def test_the_same_call_twice_is_answered_as_a_duplicate_but_kept(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """The wire contract is unchanged; what happens behind it is not.

    sdrtrunk string-matches ``duplicate call rejected``, so that is still the answer — but
    reconciliation keeps the second copy now (docs/RECONCILIATION.md), because the copy a
    *second recorder* pushes may be the better one and the old path threw it away
    unexamined.  Two identical pushes are the degenerate case: same bytes, so the sha256
    rung matches, the newcomer loses every tie and the first row stays primary.
    """
    assert post(client, tagged_mp3).text == "Call imported successfully."

    response = post(client, tagged_mp3)

    assert response.status_code == 200
    assert response.text == "duplicate call rejected"
    calls = session.execute(select(Call).order_by(Call.id)).scalars().all()
    assert len(calls) == 2
    assert calls[0].duplicate_of is None
    assert calls[1].duplicate_of == calls[0].id
    assert calls[1].reconcile_reason["match"]["rule"] == "identical sha256"
    # the second copy is not transcribed again, and the directory counted the call once
    assert calls[1].transcript_status == "skipped"
    assert session.execute(select(Talkgroup.call_count)).scalar_one() == 1


def test_an_oversized_upload_is_refused(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    """The running-size abort: the cap is enforced on bytes, not on a claim (H4)."""
    monkeypatch.setattr(upload_module, "MAX_UPLOAD_BYTES", 128)
    response = post(client, tagged_mp3)
    assert response.status_code == 413
    assert response.text == "audio file too large"


def test_a_declared_length_over_the_cap_is_refused_before_the_parse(client, token) -> None:  # noqa: ANN001
    """H4: `Content-Length` decides, before a byte of the body is read."""
    from app.api.uploads import MAX_UPLOAD_BYTES

    response = client.post(
        "/api/call-upload",
        headers={"Content-Length": str(MAX_UPLOAD_BYTES + 1), "Content-Type": "application/octet-stream"},
        content=b"",
    )
    assert response.status_code == 413


def test_an_upload_without_a_content_length_is_refused(client, token) -> None:  # noqa: ANN001
    """Nothing that pushes calls here streams chunked; a body of unknown size is not read."""
    response = client.post(
        "/api/call-upload",
        headers={"Transfer-Encoding": "chunked", "Content-Type": "application/octet-stream"},
        content=iter([b"x" * 16]),
    )
    assert response.status_code == 411


def test_an_empty_body_is_refused(client, token, tmp_path) -> None:  # noqa: ANN001
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    response = post(client, empty)
    assert response.status_code == 400
    assert response.text == "incomplete call data: no audio"


def test_unusable_audio_leaves_nothing_behind(client, session, token, tmp_path) -> None:  # noqa: ANN001
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not an mp3 at all" * 10)

    response = post(client, junk)

    assert response.status_code == 400
    assert response.text == "incomplete call data: no audio"
    assert session.execute(select(Call)).first() is None
    assert not list((tmp_path / "uploads").rglob("*.mp3"))


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def test_parse_patches() -> None:
    assert upload_module.parse_patches("[9230, 1282, 1551]") == (9230, (1282, 1551))
    assert upload_module.parse_patches("[1701]") == (None, ())
    assert upload_module.parse_patches("") == (None, ())
    assert upload_module.parse_patches(None) == (None, ())


def test_safe_filename() -> None:
    assert upload_module.safe_filename("a b.mp3", "x") == "a_b.mp3"
    assert upload_module.safe_filename("../../etc/shadow", "x") == "shadow.mp3"
    assert upload_module.safe_filename(None, "generated") == "generated.mp3"
    assert upload_module.safe_filename("...", "generated") == "generated.mp3"
    # L7: the cap applies to the stem, so a very long name keeps the suffix the rest of
    # the pipeline dispatches on.
    long_name = upload_module.safe_filename("z" * 400 + ".mp3", "x")
    assert long_name.endswith(".mp3")
    assert len(long_name) <= upload_module.MAX_NAME_LENGTH
    # L6: a name that sanitizes away to nothing, and a fallback that would too.
    assert upload_module.safe_filename("___...", "..") == "call.mp3"


def test_upload_path_never_overwrites_an_existing_recording(tmp_path, settings) -> None:  # noqa: ANN001
    from datetime import UTC, datetime

    when = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    first = upload_module.upload_path(tmp_path, when, "call.mp3", settings)
    first.write_bytes(b"one")
    second = upload_module.upload_path(tmp_path, when, "call.mp3", settings)
    # reserved, not merely chosen: the returned name exists and is empty, so a second
    # push racing this one cannot be handed the same one
    assert second != first and second.exists() and second.stat().st_size == 0


# --------------------------------------------------------------------------------------
# trunkd (§1.6, docs/RECORDERS.md §5)
# --------------------------------------------------------------------------------------


def wav_bytes(seconds: float = 1.5, rate: int = 8000) -> bytes:
    """A real 8 kHz mono 16-bit WAV — the optional format trunkd can send."""
    import io
    import math
    import struct
    import wave

    frames = int(seconds * rate)
    samples = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate))) for n in range(frames)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples)
    return buffer.getvalue()


#: what trunkd adds to the trunk-recorder dialect: its own name, the receive site, and the
#: P25 facts no other recorder reports.
TRUNKD_FIELDS = {
    "recorder": "trunkd",
    "site": "Riverton",
    "nac": "0x4A3",
    "sources": '[{"pos": 0, "src": 9250041, "emergency": false, "tag": "SO 41"}]',
    "frequencies": '[{"freq": 852162500, "pos": 0, "len": 1.5}]',
}


def post_trunkd(client, audio: tuple[str, bytes, str] | None, **fields):  # noqa: ANN001, ANN003
    """One trunkd upload.  ``audio=None`` posts no audio part at all."""
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())),
        "talkgroup": "1701",
        "source": "9250041",
        "frequency": "852162500",
        "systemLabel": "Example System",
        "talkgroupLabel": "EXAMPLE EVENT 1",
        "talkgroupGroup": "Example County",
        **TRUNKD_FIELDS,
    }
    form.update({k: str(v) for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    if audio is None:
        return client.post("/api/call-upload", data=form)
    return client.post("/api/call-upload", data=form, files={"audio": audio})


def test_an_encrypted_call_with_no_audio_is_kept_as_metadata(client, session, token) -> None:  # noqa: ANN001
    """P25 encrypts the voice payload only: the row proves the talkgroup was held."""
    response = post_trunkd(
        client,
        None,
        encrypted="1",
        algorithm="AES-256",
        keyId="0x2B",
        durationMs="4200",
        audioName="1701-1756494000.mp3",
    )

    assert response.status_code == 200
    assert response.text == "Call imported successfully."

    call = session.execute(select(Call)).scalar_one()
    assert call.audio_state == "purged"
    assert call.encrypted is True
    assert call.ingest_source == "trunkd"
    assert call.time_source == "upload"
    assert float(call.duration_s) == 4.2
    assert call.started_at == datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)
    assert call.ended_at == datetime(2026, 8, 29, 19, 17, 7, 200000, tzinfo=UTC)
    assert call.talkgroup_ids == [1701]
    assert call.frequency_hz == 852162500
    assert call.sha256 is None and call.file_size is None

    # everything the recorder knew is on the row and in raw_meta
    assert call.site.name == "Riverton"
    assert call.raw_meta["nac"] == "0x4A3"
    assert call.raw_meta["algorithm"] == "AES-256"
    assert call.raw_meta["keyId"] == "0x2B"
    assert call.raw_meta["recorder"] == "trunkd"
    assert [s.src for s in call.sources] == [9250041]

    # nothing to transcribe, and nothing written to disk
    assert session.execute(select(TranscriptionJob)).first() is None
    assert call.transcript_status == "skipped"


def test_an_encrypted_call_with_no_audio_answers_410(client, session, token, upload_settings) -> None:  # noqa: ANN001
    """The audio route's contract for a purged row — the whole point of the state."""
    from app.audio import AudioUnavailable, resolve_audio

    post_trunkd(client, None, encrypted="1", durationMs="4200")
    call = session.execute(select(Call)).scalar_one()

    assert call.file_path.startswith("uploads/2026/08/29/")
    with pytest.raises(AudioUnavailable) as caught:
        resolve_audio(call, upload_settings)
    assert caught.value.status == 410


def test_an_encrypted_call_with_an_empty_audio_part_is_kept_too(client, session, token) -> None:  # noqa: ANN001
    """Some clients always send the part; an empty one is the same "no audio"."""
    response = post_trunkd(client, ("call.mp3", b"", "audio/mpeg"), encrypted="1", durationMs="900")

    assert response.text == "Call imported successfully."
    call = session.execute(select(Call)).scalar_one()
    assert call.audio_state == "purged"
    assert float(call.duration_s) == 0.9


def test_an_encrypted_call_with_no_duration_still_places_the_talkgroup(client, session, token) -> None:  # noqa: ANN001
    response = post_trunkd(client, None, encrypted="1", durationMs=None)

    assert response.text == "Call imported successfully."
    call = session.execute(select(Call)).scalar_one()
    assert float(call.duration_s) == 0.0
    assert call.audio_state == "purged"


def test_a_clear_call_with_no_audio_is_still_an_error(client, session, token) -> None:  # noqa: ANN001
    response = post_trunkd(client, None, encrypted="0", durationMs="4200")

    assert response.status_code == 400
    assert response.text == "incomplete call data: no audio"
    assert session.execute(select(Call)).first() is None


def test_the_same_encrypted_call_twice_is_a_duplicate(client, session, token) -> None:  # noqa: ANN001
    """Two posts of one metadata-only call: matched on the interval, not on a hash.

    An encrypted call carries no audio and therefore no ``sha256``, so the exact rung
    cannot fire — the two are recognized as one transmission because they claim the same
    talkgroup at the same second.
    """
    assert post_trunkd(client, None, encrypted="1", durationMs="4200").text == "Call imported successfully."

    response = post_trunkd(client, None, encrypted="1", durationMs="4200")

    assert response.text == "duplicate call rejected"
    calls = session.execute(select(Call).order_by(Call.id)).scalars().all()
    assert len(calls) == 2
    assert [c.duplicate_of for c in calls] == [None, calls[0].id]


def test_two_encrypted_calls_with_no_audio_are_not_copies_of_each_other(client, session, token) -> None:  # noqa: ANN001
    """A missing hash is not a matching hash.

    ``calls.sha256`` is NULL for every metadata-only call, and ``sha256 = NULL`` renders as
    ``IS NULL`` in SQL, which would make the first audio-less call the "original" of every
    one that followed it.  Different talkgroups, minutes apart: two primaries.
    """
    assert post_trunkd(client, None, encrypted="1", durationMs="4200").text == "Call imported successfully."
    assert (
        post_trunkd(
            client,
            None,
            encrypted="1",
            durationMs="4200",
            talkgroup="1999",
            dateTime=str(int(datetime(2026, 8, 29, 19, 27, 3, tzinfo=UTC).timestamp())),
        ).text
        == "Call imported successfully."
    )

    calls = session.execute(select(Call).order_by(Call.id)).scalars().all()
    assert len(calls) == 2
    assert [c.duplicate_of for c in calls] == [None, None]


def test_a_trunkd_mp3_falls_back_to_the_form_without_complaining(client, session, token, stripped_mp3, caplog) -> None:  # noqa: ANN001
    """trunkd's MP3 is LAME output: no ID3 frames, and a name the sdrtrunk parser cannot read."""
    caplog.set_level("WARNING")

    response = post_trunkd(
        client,
        ("1701-1756494000.mp3", stripped_mp3.read_bytes(), "audio/mpeg"),
        audioName="1701-1756494000.mp3",
    )

    assert response.text == "Call imported successfully."
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}], caplog.text

    call = session.execute(select(Call)).scalar_one()
    assert call.ingest_source == "trunkd"
    assert call.file_path == f"uploads/2026/08/29/{call.sha256[:2]}/1701-1756494000.mp3"
    assert call.system.name == "Example System"
    assert call.talkgroup_ids == [1701]
    assert call.talkgroup_alias_at_record == "EXAMPLE EVENT 1"
    assert call.started_at == datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)
    assert float(call.duration_s) > 0  # the MP3 header still gives us the duration
    assert call.audio_state == "present"
    assert session.execute(select(TranscriptionJob)).scalar_one().call_id == call.id


def test_a_trunkd_wav_round_trips(client, session, token, tmp_path) -> None:  # noqa: ANN001
    """The optional format: 8 kHz mono 16-bit, stored as .wav and probed as one."""
    response = post_trunkd(
        client,
        ("1701-1756494000.wav", wav_bytes(), "audio/wav"),
        audioName="1701-1756494000.wav",
        audioType="audio/wav",
    )

    assert response.text == "Call imported successfully."
    call = session.execute(select(Call)).scalar_one()
    assert call.file_path == f"uploads/2026/08/29/{call.sha256[:2]}/1701-1756494000.wav"
    assert (tmp_path / call.file_path).is_file()
    assert call.sample_rate == 8000
    assert call.channels == 1
    assert 1.4 < float(call.duration_s) < 1.6
    assert call.ingest_source == "trunkd"
    assert call.audio_state == "present"

    from app.api.calls import audio_media_type

    assert audio_media_type(tmp_path / call.file_path) == "audio/wav"


def test_trunkd_patches_are_a_bare_member_list(client, session, token) -> None:  # noqa: ANN001
    """trunk-recorder's dialect, not sdrtrunk's: no supergroup at the head of ``patches``.

    The supergroup is the call's own talkgroup instead, so the patch still links (that is
    what :func:`test_a_trunkd_patch_call_links_its_supergroup_and_members` checks); here the
    point is only that every id in ``patches`` is read as a member, none as the supergroup.
    """
    post_trunkd(client, None, encrypted="1", durationMs="4200", patches="[1812, 1561]")

    call = session.execute(select(Call)).scalar_one()
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [1701, 1812, 1561]
    patch = session.get(Patch, call.patch_id)
    assert patch.patch_id == 1701
    assert patch.member_tgids == [1812, 1561]


def test_a_trunk_recorder_push_is_still_trunk_recorder(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """The `recorder` field is what makes a call trunkd; without it nothing changes."""
    post(client, tagged_mp3, sources='[{"pos": 0, "src": 9250041}]')

    call = session.execute(select(Call)).scalar_one()
    assert call.ingest_source == "trunk_recorder"


def test_is_trunkd_form() -> None:
    assert upload_module.is_trunkd_form({"recorder": "trunkd"})
    assert upload_module.is_trunkd_form({"recorder": " TrunkD "})
    assert not upload_module.is_trunkd_form({"recorder": "trunk-recorder"})
    assert not upload_module.is_trunkd_form({})


def test_the_source_id_extension_fills_the_radio_home(client, session, token) -> None:  # noqa: ANN001
    """`sourceWacn`/`sourceSystem` are P25 LC opcode 9 (docs/NON-VOICE-DATA.md §3.2).

    They arrive beside a plain `source`, so ``radios.wacn`` / ``home_system_id`` — which
    have been in the schema since it was written and NULL-or-zero ever since — are filled
    in place rather than filing the unit a second time.
    """
    from app.models import Radio

    post_trunkd(client, None, encrypted="1", durationMs="4200", sourceWacn="101010", sourceSystem="57")

    radio = session.execute(select(Radio)).scalar_one()
    assert (radio.radio_id, radio.wacn, radio.home_system_id) == (9250041, 101010, 57)
    assert radio.call_count == 1

    call = session.execute(select(Call)).scalar_one()
    assert call.radio_id == radio.id
    # Kept on the call as well, exactly as the recorder sent them: the row records what
    # arrived, not only what was normalized out of it.
    assert call.raw_meta["sourceWacn"] == "101010"
    assert call.raw_meta["sourceSystem"] == "57"


def test_the_source_id_extension_never_overwrites_a_known_home(client, session, token) -> None:  # noqa: ANN001
    """101010.57.9250041 and 101010.82.9250041 are two units, and stay two rows."""
    from app.models import Radio

    post_trunkd(client, None, encrypted="1", durationMs="4200", sourceWacn="101010", sourceSystem="57")
    post_trunkd(
        client,
        None,
        encrypted="1",
        durationMs="4200",
        dateTime=str(int(datetime(2026, 8, 29, 19, 20, 3, tzinfo=UTC).timestamp())),
        sourceWacn="101010",
        sourceSystem="82",
    )

    homes = sorted(session.scalars(select(Radio.home_system_id)).all())
    assert homes == [57, 82], "the first answer is never rewritten by a later claim"
    assert sorted(session.scalars(select(Radio.call_count)).all()) == [1, 1]


def test_a_call_without_the_extension_still_files_one_radio(client, session, token) -> None:  # noqa: ANN001
    """LC 9 rides some transmissions and not others; both must land on one row."""
    from app.models import Radio

    post_trunkd(client, None, encrypted="1", durationMs="4200", sourceWacn="101010", sourceSystem="57")
    post_trunkd(
        client,
        None,
        encrypted="1",
        durationMs="4200",
        dateTime=str(int(datetime(2026, 8, 29, 19, 25, 3, tzinfo=UTC).timestamp())),
    )

    radio = session.execute(select(Radio)).scalar_one()
    assert (radio.wacn, radio.home_system_id) == (101010, 57)
    assert radio.call_count == 2


# --------------------------------------------------------------------------------------
# the system a push belongs to (never invented)
# --------------------------------------------------------------------------------------


def test_a_push_without_a_system_label_joins_the_only_system_there_is(
    client, session, token, stripped_mp3  # noqa: ANN001
) -> None:
    """The bug that grew a phantom ``System("upload")`` in production.

    The fallback used to be the literal string ``"upload"``, and ``upsert_system`` creates
    rows from names — so every unlabeled push minted a second system, with its own
    duplicate sites, beside the real one.
    """
    from app.models import Site, System

    session.add(System(name="Example System", wacn=101010))
    session.commit()

    assert post(client, stripped_mp3, systemLabel=None, site="Eastside").text == OK

    session.expire_all()
    assert [s.name for s in session.scalars(select(System)).all()] == ["Example System"]
    call = session.scalars(select(Call)).one()
    assert call.system_id == session.scalars(select(System)).one().id
    # The site the recorder named still lands — under the real system, not a phantom.
    assert [s.name for s in session.scalars(select(Site)).all()] == ["Eastside"]


def test_a_push_without_a_system_label_reads_the_one_in_the_id3(
    client, session, token, tagged_mp3  # noqa: ANN001
) -> None:
    """sdrtrunk only ever says the system in its COMM frames, exactly like the site.

    The site line always read them; the system line did not, which is how an upload whose
    filename the parser rejects recovered ``Central`` and lost ``Example System``.
    """
    from app.models import System

    assert post(client, tagged_mp3, systemLabel=None, audioName="not-an-sdrtrunk-name.mp3").text == OK

    session.expire_all()
    assert [s.name for s in session.scalars(select(System)).all()] == ["Example System"]


def test_a_push_with_no_system_anywhere_and_no_archive_is_refused(
    client, session, token, stripped_mp3  # noqa: ANN001
) -> None:
    from app.models import System

    response = post(client, stripped_mp3, systemLabel=None)

    assert response.status_code == 400
    assert response.text == "incomplete call data: no system"
    session.expire_all()
    assert session.scalars(select(System)).all() == []
    assert session.scalars(select(Call)).all() == []


def test_a_push_with_no_system_label_and_several_systems_is_refused(
    client, session, token, stripped_mp3  # noqa: ANN001
) -> None:
    """Guessing would file the traffic under the wrong system, which is worse than a 400."""
    from app.models import System

    session.add_all([System(name="Example System"), System(name="West County")])
    session.commit()

    response = post(client, stripped_mp3, systemLabel=None)

    assert response.status_code == 400
    assert response.text == "incomplete call data: no system"
    session.expire_all()
    assert session.scalars(select(Call)).all() == []


# --------------------------------------------------------------------------------------
# a push with no time of its own (docs/MIGRATION.md, "Timestamps")
# --------------------------------------------------------------------------------------

#: the pre-2025 sdrtrunk rendering: no underscore after the stamp.  The backlog migration
#: sends this as ``audioName`` and no ``dateTime`` at all.
LEGACY_NAME = "20240223_210414Example_System_Central_T-Control__TO_1102_FROM_5226020.mp3"


@pytest.fixture
def fresh_rate_limit(monkeypatch):  # noqa: ANN001, ANN201
    """The arrival-fallback warning is rate limited in module state; start each test cold."""
    monkeypatch.setattr(upload_module, "_time_fallback", [0.0, 0.0])


def test_a_backlog_push_with_no_derivable_time_is_refused(
    client, session, token, stripped_mp3, tmp_path, fresh_rate_limit  # noqa: ANN001
) -> None:
    """The guard on the failure that put a million calls on their upload date.

    A backlog upload is a file that still exists: refusing it costs one retry, and filing
    it under the moment it arrived costs the archive its chronology.
    """
    from app.models import ActivityEvent

    response = post(
        client, stripped_mp3, dateTime=None, audioName="stream-chunk.mp3", priority="backlog"
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "no_call_time"
    assert body["class"] == "backlog"
    assert "dateTime" in body["detail"]
    session.expire_all()
    assert session.scalars(select(Call)).all() == []
    # nothing kept on disk, and the refusal is in the activity log
    assert list((tmp_path / "uploads").rglob("*.mp3")) == []
    kinds = session.scalars(select(ActivityEvent.kind)).all()
    assert "call.no_time" in kinds


def test_a_backlog_push_whose_name_carries_the_stamp_is_accepted(
    client, session, token, stripped_mp3, fresh_rate_limit  # noqa: ANN001
) -> None:
    """What the migration actually sends, once the parser can read a pre-2025 name.

    The stamp is the instant sdrtrunk closed the file — the call's end — so ``ended_at``
    is it, in the archive's zone, and ``started_at`` is that minus the measured duration.
    """
    response = post(
        client, stripped_mp3, dateTime=None, audioName=LEGACY_NAME, priority="backlog"
    )

    assert response.status_code == 200
    assert response.text == OK
    call = session.execute(select(Call)).scalar_one()
    assert call.ended_at == datetime(2024, 2, 24, 3, 4, 14, tzinfo=UTC)
    # the stored duration is rounded to milliseconds; the derivation is not
    offset = (call.ended_at - call.started_at).total_seconds()
    assert abs(offset - float(call.duration_s)) < 0.001
    # The *file* is still filed under the day it arrived: the directory is chosen from
    # the form before the recording is parsed, and moving a million MP3s is not worth
    # the churn.  ``calls.file_path`` is the only place the upload date survives.
    assert call.file_path.endswith(LEGACY_NAME)


def test_a_live_push_with_no_derivable_time_still_lands_and_says_so(
    client, session, token, stripped_mp3, caplog, fresh_rate_limit  # noqa: ANN001
) -> None:
    """A live call has no second chance, so the fallback stays — but it is never silent."""
    from app.models import ActivityEvent

    with caplog.at_level("WARNING", logger="app.api.call_upload"):
        response = post(
            client, stripped_mp3, dateTime=None, audioName="stream-chunk.mp3", priority="live"
        )

    assert response.status_code == 200
    assert response.text == OK
    call = session.execute(select(Call)).scalar_one()
    assert call.time_source == "upload"
    assert (datetime.now(UTC) - call.ended_at).total_seconds() < 120
    assert any("filed under its arrival" in record.message for record in caplog.records)
    event = session.execute(
        select(ActivityEvent).where(ActivityEvent.kind == "call.time_fallback")
    ).scalar_one()
    assert event.meta["audioName"] == "stream-chunk.mp3"


def test_the_arrival_warning_is_rate_limited(
    client, session, token, stripped_mp3, caplog, fresh_rate_limit  # noqa: ANN001
) -> None:
    """A bulk failure must not produce a log line per call — that is what buried the last one."""
    with caplog.at_level("WARNING", logger="app.api.call_upload"):
        for n in range(3):
            post(
                client,
                stripped_mp3,
                dateTime=None,
                audioName=f"stream-chunk-{n}.mp3",
                priority="live",
            )

    warnings = [r for r in caplog.records if "filed under its arrival" in r.message]
    assert len(warnings) == 1
    assert upload_module._time_fallback[1] == 2  # the two it swallowed are still counted


def test_the_time_basis_is_what_the_push_actually_supplied(
    client, session, token, stripped_mp3, upload_settings, tmp_path  # noqa: ANN001, ARG001
) -> None:
    """``time_source`` cannot say this: the endpoint writes ``upload`` for every push."""
    from app.api.call_upload import (
        TIME_BASIS_ARRIVAL,
        TIME_BASIS_FORM,
        TIME_BASIS_PARSED,
        build_upload_record,
    )

    target = tmp_path / "call.mp3"
    target.write_bytes(stripped_mp3.read_bytes())
    base = {"talkgroup": "1701", "systemLabel": "Example System"}

    epoch = build_upload_record(target, tmp_path, {**base, "dateTime": "1740337454"}, upload_settings)
    assert epoch.time_basis == TIME_BASIS_FORM

    named = tmp_path / LEGACY_NAME
    named.write_bytes(stripped_mp3.read_bytes())
    assert build_upload_record(named, tmp_path, base, upload_settings).time_basis == TIME_BASIS_PARSED
    assert build_upload_record(target, tmp_path, base, upload_settings).time_basis == TIME_BASIS_ARRIVAL


def test_a_push_whose_name_will_not_parse_still_reads_its_id3_date(
    client, session, token, tagged_mp3, fresh_rate_limit  # noqa: ANN001
) -> None:
    """The rung the endpoint's own docs promised and the code did not have.

    ID3 was only ever consulted through the *filename* parse: ``build_call_record`` reads
    the ``COMM`` ``Date`` frame, but it is never called when the name does not parse, and
    then the time chain fell straight from "no ``dateTime``" to the arrival fallback — the
    branch that put a million backlog calls on their upload date.  A re-encoded stream
    chunk still carries the frames sdrtrunk wrote, so the recording can say when it was
    recorded even when its name cannot.
    """
    response = post(
        client, tagged_mp3, dateTime=None, audioName="stream-chunk.mp3", priority="backlog"
    )

    assert response.status_code == 200, response.text
    assert response.text == OK
    call = session.execute(select(Call)).scalar_one()
    # 2026-08-29 01:52:13 America/Winnipeg, the instant sdrtrunk closed the file
    assert call.ended_at == datetime(2026, 8, 29, 6, 52, 13, 979000, tzinfo=UTC)
    offset = (call.ended_at - call.started_at).total_seconds()
    assert abs(offset - float(call.duration_s)) < 0.001


# --------------------------------------------------------------------------------------
# the pre-2025 System_Site_Name block, re-split against the archive
# --------------------------------------------------------------------------------------

#: every legacy shape in the production archive, with the truth about each one.  The third
#: is the one no right-anchored guess can get: a two-token channel name.
LEGACY_SHAPES = [
    ("Example_System_Central_T-Control", "Example System", "Central", "T-Control"),
    ("Example_System_Central_TRAFFIC", "Example System", "Central", "TRAFFIC"),
    ("Example_System_Central_Control", "Example System", "Central", "Control"),
    (
        "EXAMPLE_SCHOOL_DISTRICT_Eastside_District_Schools",
        "EXAMPLE SCHOOL DISTRICT",
        "Eastside",
        "District Schools",
    ),
]


@pytest.fixture
def archive_systems(session):  # noqa: ANN001, ANN201
    """The two systems and their sites, as the production archive holds them."""
    from app.ingest.service import upsert_site, upsert_system

    for name, site in (
        ("Example System", "Central"),
        ("EXAMPLE SCHOOL DISTRICT", "Eastside"),
    ):
        upsert_site(session, upsert_system(session, name, protocol=None, wacn=None), site)
    session.commit()


@pytest.mark.parametrize(("sysite", "system", "site", "channel"), LEGACY_SHAPES)
def test_a_legacy_sysite_is_split_against_the_systems_that_exist(
    session, archive_systems, sysite, system, site, channel  # noqa: ANN001, ARG001
) -> None:
    """The archive is the marker the filename does not carry."""
    from app.ingest.service import resolve_legacy_sysite

    assert resolve_legacy_sysite(session, sysite) == (system, site, channel)


def test_a_legacy_sysite_naming_no_known_system_is_left_to_the_parser(
    session, archive_systems  # noqa: ANN001, ARG001
) -> None:
    """It may only move a call onto a system that exists — never invent one."""
    from app.ingest.service import resolve_legacy_sysite

    assert resolve_legacy_sysite(session, "SOME_OTHER_NETWORK_Site_Channel") is None


@pytest.mark.parametrize(("sysite", "system", "site", "channel"), LEGACY_SHAPES)
def test_a_legacy_push_lands_on_the_system_it_names(
    client, session, token, stripped_mp3, archive_systems, sysite, system, site, channel  # noqa: ANN001, ARG001, PLR0913
) -> None:
    """End to end: no ``systemLabel``, a pre-2025 name, and no phantom system minted."""
    from app.models import Site, System

    response = post(
        client,
        stripped_mp3,
        systemLabel=None,
        dateTime=None,
        priority="backlog",
        audioName=f"20240223_210414{sysite}__TO_1102_FROM_5226020.mp3",
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    call = session.execute(select(Call)).scalar_one()
    assert session.get(System, call.system_id).name == system
    assert session.get(Site, call.site_id).name == site
    # the whole point: two systems went in, two systems are there
    assert len(session.scalars(select(System.id)).all()) == 2


def test_a_modern_name_is_split_exactly_as_before(
    client, session, token, stripped_mp3, archive_systems  # noqa: ANN001, ARG001
) -> None:
    """The dash rendering says where the system ends, so nothing here applies to it."""
    from app.models import Site, System

    response = post(
        client,
        stripped_mp3,
        systemLabel=None,
        dateTime=None,
        priority="backlog",
        audioName="20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3",
    )

    assert response.status_code == 200, response.text
    session.expire_all()
    call = session.execute(select(Call)).scalar_one()
    assert session.get(System, call.system_id).name == "Example System"
    assert session.get(Site, call.site_id).name == "Central"


# --------------------------------------------------------------------------------------
# threading (docs/RECORDERS.md, "Threading and slow uploads")
# --------------------------------------------------------------------------------------
#
# 2026-09-03: one backlog push took 122,682 ms and for that whole window the API logged
# nothing and answered nothing — every recorder POST timed out, then 38 of them failed at
# once when it came back.  The route was ``async def`` doing sync SQLAlchemy, sync file
# I/O, an ffprobe-shaped probe and a locking dedupe *on the event loop*, so one slow upload
# was an outage.  These two tests are the regression: the first proves the loop keeps
# answering while an upload is stalled, the second proves a slow one says which stage was.


def _threaded_app(session_factory, upload_settings, monkeypatch):  # noqa: ANN001, ANN202
    """The upload router and ``GET /api/status`` on one app, each request its own session.

    The other fixtures in this file hand every request the *same* session, which is fine
    when requests are serialized and is exactly the thing that cannot be shared once two of
    them are genuinely in flight — so this one hands out a real session per request, which
    is what ``app.db.get_session`` does in production anyway.
    """
    from app.api.status import router as status_router

    monkeypatch.setattr(upload_module, "get_settings", lambda: upload_settings)
    app = FastAPI()
    app.middleware("http")(body_size_guard)
    app.include_router(router, prefix="/api")
    app.include_router(status_router, prefix="/api")
    app.dependency_overrides[get_session] = session_factory
    return app


def test_a_stalled_upload_does_not_block_the_event_loop(
    session, token, upload_settings, tagged_mp3, monkeypatch  # noqa: ANN001, ARG001
) -> None:
    """A 2 s stall inside the upload's *sync* work must not cost ``GET /api/status`` 2 s.

    The stall is planted in ``build_upload_record``, which is representative: it is where
    the hashing and the audio probe happen, and it is sync by nature.  With the route back
    on the event loop this test fails by about two seconds.
    """
    import asyncio
    import threading
    import time as _time

    from httpx import ASGITransport, AsyncClient

    from app.db import SessionLocal

    opened = []

    def session_factory():  # noqa: ANN202
        db = SessionLocal()
        opened.append(db)
        try:
            yield db
        finally:
            db.close()

    app = _threaded_app(session_factory, upload_settings, monkeypatch)

    entered = threading.Event()
    real = upload_module.build_upload_record

    def slow_build(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        entered.set()
        _time.sleep(2.0)
        return real(*args, **kwargs)

    monkeypatch.setattr(upload_module, "build_upload_record", slow_build)

    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())),
        "talkgroup": "1701",
        "source": "9200006",
        "systemLabel": "Example System",
        "talkgroupLabel": "EXAMPLE EVENT 1",
    }
    audio = {"audio": ("call.mp3", tagged_mp3.read_bytes(), "audio/mpeg")}

    async def drive():  # noqa: ANN202
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://upload.test") as http:
            origin = _time.monotonic()
            upload = asyncio.create_task(
                http.post("/api/call-upload", data=form, files=audio, timeout=30.0)
            )
            # Wait for the stall to have *started* — otherwise the status request could win
            # the race by arriving before the upload had reached the slow stage, and the
            # test would pass on a route that never left the event loop.  The wait itself
            # is handed to a thread, because on a *blocked* loop nothing else can resolve.
            await asyncio.get_running_loop().run_in_executor(None, entered.wait, 10.0)
            assert entered.is_set(), "the upload never reached the stalled stage"

            began = _time.monotonic()
            status = await http.get("/api/status", timeout=30.0)
            answered = _time.monotonic() - began
            at_status = _time.monotonic() - origin

            stored = await upload
            at_upload = _time.monotonic() - origin
            return status, answered, at_status, stored, at_upload

    status, answered, at_status, stored, at_upload = asyncio.run(drive())

    # The whole point, and the only assertion that can tell the two worlds apart: the
    # status answer landed while the upload was still inside its 2 s stall.  Measuring the
    # status request alone proves nothing — on a blocked loop *every* clock the test can
    # read is read after the block has already cleared, and the status request then looks
    # instant too.  Overlap is the fact: the two must not have been serialized.
    assert status.status_code == 200, status.text
    assert answered < 1.0, f"GET /api/status itself took {answered:.2f}s"
    assert at_upload > 2.0, f"the upload was not actually stalled ({at_upload:.2f}s)"
    assert at_status < at_upload - 1.0, (
        f"GET /api/status answered at {at_status:.2f}s and the stalled upload at "
        f"{at_upload:.2f}s — they were serialized, so the upload was on the event loop"
    )
    # ...and the upload itself still did its job, stall and all.
    assert stored.status_code == 200, stored.text
    assert stored.text == OK

    for db in opened:
        db.close()


def test_a_slow_upload_names_the_stage_that_was_slow(
    client, session, token, tagged_mp3, monkeypatch, caplog  # noqa: ANN001, ARG001
) -> None:
    """Past ``SLOW_UPLOAD_SECONDS`` one WARNING carries the per-stage breakdown.

    The threshold is monkeypatched to zero rather than the request slowed to five seconds:
    what is under test is that the line is emitted, names the slowest stage and renders
    every stage's milliseconds, not the clock.
    """
    monkeypatch.setattr(upload_module, "SLOW_UPLOAD_SECONDS", 0.0)

    with caplog.at_level("WARNING", logger="app.api.call_upload"):
        response = post(client, tagged_mp3)

    assert response.status_code == 200, response.text
    slow = [r for r in caplog.records if "slow upload" in r.getMessage()]
    assert len(slow) == 1, [r.getMessage() for r in caplog.records]
    message = slow[0].getMessage()
    # Every stage a stored upload runs, with a number on each, and one of them named.
    for stage in ("receive", "parse", "move", "lookup", "store"):
        assert f"{stage}=" in message, message
    assert "slowest stage" in message


# --------------------------------------------------------------------------------------
# the recorder's own Broadcastify upload (docs/EXPORTERS.md, "Recorder-direct upload")
# --------------------------------------------------------------------------------------
#
# With ``broadcastify_upload_at = recorder`` the call reaches Broadcastify before it
# reaches keyup.  These pin the half that keeps keyup the system of record anyway: the
# recorder's report becomes the ledger row, and nothing about it can cost a recording.

DUPLICATE_LINE = "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL"


def _ledger(session, call_id: int):  # noqa: ANN001, ANN202
    from app.models import CallExport

    return session.scalar(
        select(CallExport).where(CallExport.call_id == call_id, CallExport.service == "broadcastify")
    )


def _stored(session):  # noqa: ANN001, ANN202
    return session.scalars(select(Call).order_by(Call.id.desc())).first()


@pytest.mark.parametrize(
    ("outcome", "extra", "expected"),
    [
        ("uploaded", {"broadcastifyRemoteId": "abc123"}, ("uploaded", None)),
        ("duplicate", {}, ("skipped", "broadcastify already has this call")),
        ("late", {}, ("skipped", "past the Broadcastify skew window (at trunkd-central-01)")),
        ("claim_denied", {"broadcastifyHolder": "trunkd-central-02"}, ("skipped", "claimed by recorder trunkd-central-02")),
        ("failed", {}, ("pending", None)),
        ("skipped", {}, ("skipped", f"recorder: {DUPLICATE_LINE}")),
    ],
)
def test_each_reported_outcome_becomes_its_ledger_row(  # noqa: ANN001
    client, session, token, tagged_mp3, outcome, extra, expected
) -> None:
    """One row per outcome, worded the way the worker words the same answer."""
    status, reason = expected
    response = post(
        client,
        tagged_mp3,
        broadcastifyOutcome=outcome,
        broadcastifyCode="1",
        broadcastifyResponse=DUPLICATE_LINE,
        sourceQuality='{"instance": "trunkd-central-01", "voice_frames": 90}',
        **extra,
    )
    assert response.status_code == 200, response.text

    row = _ledger(session, _stored(session).id)
    assert row is not None
    assert row.status == status
    assert row.response == DUPLICATE_LINE
    assert row.response_code == 1
    if reason is not None:
        assert row.skip_reason == reason
    if outcome == "uploaded":
        assert row.remote_id == "abc123"
        assert row.uploaded_by == "trunkd-central-01"
        assert row.uploaded_at is not None
    if outcome == "failed":
        # Back in the queue with the recorder's try counted, not parked, and due *now*: the
        # worker's retry has to land inside Broadcastify's 15 s skew window, and the first
        # step of the backoff curve (30 s) never could.
        assert row.attempts == 1
        assert row.last_error == DUPLICATE_LINE
        assert row.next_attempt_at <= datetime.now(UTC) + timedelta(seconds=1)


def test_no_outcome_writes_no_ledger_row(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """Every recorder that predates this sends nothing, and nothing changes for it."""
    assert post(client, tagged_mp3).status_code == 200
    assert _ledger(session, _stored(session).id) is None


def test_a_nonsense_outcome_is_ignored_and_the_call_is_still_stored(client, session, token, tagged_mp3, caplog) -> None:  # noqa: ANN001
    """An archive must never lose a recording over bookkeeping about somebody else's."""
    with caplog.at_level("WARNING", logger="app.api.call_upload"):
        response = post(client, tagged_mp3, broadcastifyOutcome="banana")
    assert response.status_code == 200
    assert _stored(session) is not None
    assert _ledger(session, _stored(session).id) is None
    assert "banana" in caplog.text


def test_the_reported_row_is_the_one_discover_finds(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """The `(call_id, service)` unique is what stops a second upload, as it always was."""
    from app.config import Settings
    from app.exporters import ledger

    assert post(client, tagged_mp3, broadcastifyOutcome="uploaded").status_code == 200
    call = _stored(session)
    call.ingest_priority = "live"
    session.commit()

    created = ledger.discover(
        session,
        service="broadcastify",
        watermark=call.started_at - timedelta(hours=1),
        settings=Settings(_env_file=None),
    )
    session.commit()
    assert created == 0
    rows = session.scalars(
        select(CallExport).where(CallExport.call_id == call.id, CallExport.service == "broadcastify")
    ).all()
    assert len(rows) == 1
    assert rows[0].status == "uploaded"


def test_the_recorders_claim_is_stamped_with_the_call_it_was_about(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """A claim is made before the call exists here; this is where the two are joined."""
    from app.exporters import claims
    from app.models import BcfyClaim

    started = int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())
    held = claims.acquire(session, instance="trunkd-central-01", tgid=1701, ts=started)
    session.commit()

    assert post(
        client,
        tagged_mp3,
        broadcastifyOutcome="uploaded",
        broadcastifyRemoteId="abc123",
        sourceQuality='{"instance": "trunkd-central-01"}',
    ).status_code == 200

    session.expire_all()
    row = session.get(BcfyClaim, held.claim_id)
    assert row.outcome == "uploaded"
    assert row.call_id == _stored(session).id


def _committed_calls() -> list[int]:
    """Call ids as a *fresh* connection sees them — what actually reached the database.

    The request shares the test's session, so asking that session would read its own
    identity map; a second session answers only from what COMMIT really wrote.
    """
    from app.db import SessionLocal

    with SessionLocal() as fresh:
        return list(fresh.scalars(select(Call.id)))


def test_a_database_error_in_the_outcome_writer_does_not_roll_back_the_call(  # noqa: ANN001
    client, session, token, tagged_mp3
) -> None:
    """An aborted statement in the bookkeeping must not turn COMMIT into ROLLBACK.

    ``broadcastifyCode`` past int32 makes the ledger INSERT fail with a DataError inside
    the upload's own transaction.  Without a savepoint PostgreSQL aborts the whole
    transaction and the later COMMIT silently rolls the call back — while the recorder is
    told "imported" and deletes its spool copy.
    """
    response = post(client, tagged_mp3, broadcastifyOutcome="uploaded", broadcastifyCode="99999999999")
    assert response.status_code == 200, response.text
    assert response.text == OK

    ids = _committed_calls()
    assert len(ids) == 1
    assert _ledger(session, ids[0]) is None
    # The rest of the transaction survived with it: the transcription job is there.
    assert session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == ids[0])).first()


def test_a_database_error_queueing_audio_processing_does_not_roll_back_the_call(  # noqa: ANN001
    client, session, token, tagged_mp3, monkeypatch
) -> None:
    """The same swallow in :func:`app.ingest.service.enqueue_audio_process`."""
    from sqlalchemy import text

    from app.audioproc import jobs as audio_jobs

    def broken(session, call, settings, *, priority=None):  # noqa: ANN001, ANN202, ARG001
        session.execute(text("SELECT 1 / 0"))  # aborts the transaction it runs in

    monkeypatch.setattr(audio_jobs, "enqueue", broken)
    response = post(client, tagged_mp3, broadcastifyOutcome="uploaded")
    assert response.status_code == 200, response.text

    ids = _committed_calls()
    assert len(ids) == 1
    # Everything written after the failed enqueue landed too.
    assert _ledger(session, ids[0]) is not None
    assert session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == ids[0])).first()


# --------------------------------------------------------------------------------------
# millisecond call start (``startMs`` / ``endMs``, trunkd 2026-09-10)
# --------------------------------------------------------------------------------------


class TestMillisecondCallStart:
    """``dateTime`` is whole seconds, and a 0.65 s call cannot survive that rounding.

    Production calls 2395971 and 2395972 are one 0.648 s key-up on TRANSIT, heard by both
    recorders and stamped 04:59:32 and 04:59:33 — two intervals that never touch, so
    reconciliation had nothing to measure and stored both as primaries.  trunkd sends
    ``startMs`` alongside ``dateTime`` from 2026-09-10; ``dateTime`` stays the fallback
    *and* the sanity check, because a recorder that has got its units wrong must not be
    allowed to file a call in 1970.
    """

    @staticmethod
    def _record(target, tmp_path, upload_settings, **form):  # noqa: ANN001, ANN205
        from app.api.call_upload import build_upload_record

        base = {"talkgroup": "1701", "systemLabel": "Example System"}
        return build_upload_record(target, tmp_path, {**base, **form}, upload_settings)

    @pytest.fixture
    def target(self, stripped_mp3, tmp_path):  # noqa: ANN001, ANN201
        path = tmp_path / "call.mp3"
        path.write_bytes(stripped_mp3.read_bytes())
        return path

    def test_start_ms_wins_and_keeps_its_milliseconds(
        self, target, tmp_path, upload_settings  # noqa: ANN001
    ) -> None:
        from app.api.call_upload import TIME_BASIS_FORM

        start = datetime(2026, 9, 10, 4, 59, 32, 640_000, tzinfo=UTC)
        record = self._record(
            target,
            tmp_path,
            upload_settings,
            dateTime=str(int(start.timestamp())),
            startMs=str(int(start.timestamp() * 1000)),
            endMs=str(int(start.timestamp() * 1000) + 648),
            durationMs="648",
        )
        assert record.time_basis == TIME_BASIS_FORM
        assert record.started_at == start
        # …and the milliseconds survive all the way to the far edge.
        assert record.ended_at - record.started_at == timedelta(seconds=record.duration_s)
        assert record.started_at.microsecond == 640_000

    def test_a_start_ms_that_disagrees_with_datetime_is_disbelieved(
        self, target, tmp_path, upload_settings  # noqa: ANN001
    ) -> None:
        """A recorder sending seconds in a field spelled ``Ms`` would file 2026 in 1970."""
        start = datetime(2026, 9, 10, 4, 59, 32, tzinfo=UTC)
        record = self._record(
            target,
            tmp_path,
            upload_settings,
            dateTime=str(int(start.timestamp())),
            # thirteen digits, but a fortnight away — a different clock, not clock skew
            startMs=str(int(start.timestamp() * 1000) + 14 * 86_400_000),
            durationMs="648",
        )
        assert record.started_at == start

        # …and so is one that is not milliseconds at all: ten digits is seconds.
        seconds_shaped = self._record(
            target,
            tmp_path,
            upload_settings,
            dateTime=str(int(start.timestamp())),
            startMs=str(int(start.timestamp())),
            durationMs="648",
        )
        assert seconds_shaped.started_at == start

    def test_a_push_without_start_ms_is_exactly_what_it_was(
        self, target, tmp_path, upload_settings  # noqa: ANN001
    ) -> None:
        start = datetime(2026, 9, 10, 4, 59, 32, tzinfo=UTC)
        record = self._record(
            target, tmp_path, upload_settings, dateTime=str(int(start.timestamp())), durationMs="648"
        )
        assert record.started_at == start
        assert record.started_at.microsecond == 0

    def test_end_ms_never_shortens_a_call(
        self, target, tmp_path, upload_settings  # noqa: ANN001
    ) -> None:
        """``endMs`` is a cross-check only.

        trunkd falls back to the audio length for ``durationMs`` when its wall clock read
        non-positive, and then ``endMs`` equals ``startMs`` on a call that really lasted
        0.648 s.  The length has to keep coming from ``durationMs``.
        """
        start = datetime(2026, 9, 10, 4, 59, 32, 640_000, tzinfo=UTC)
        record = self._record(
            target,
            tmp_path,
            upload_settings,
            dateTime=str(int(start.timestamp())),
            startMs=str(int(start.timestamp() * 1000)),
            endMs=str(int(start.timestamp() * 1000)),
            durationMs="648",
        )
        # The recording itself is 1.67 s; ``endMs`` claiming a zero-length call changes
        # nothing about either edge except the start it was believed for.
        assert record.duration_s > 1.0
        assert record.started_at == start
        assert record.ended_at == start + timedelta(seconds=record.duration_s)

    def test_the_endpoint_stores_the_milliseconds(
        self, client, session, token, stripped_mp3  # noqa: ANN001, ARG002
    ) -> None:
        start = datetime(2026, 8, 29, 19, 41, 3, 250_000, tzinfo=UTC)
        response = post(
            client,
            stripped_mp3,
            dateTime=str(int(start.timestamp())),
            startMs=str(int(start.timestamp() * 1000)),
            endMs=str(int(start.timestamp() * 1000) + 648),
            durationMs="648",
        )
        assert response.text == OK
        call = session.scalars(select(Call).order_by(Call.id.desc())).first()
        assert call.started_at == start
        assert call.started_at.microsecond == 250_000


# --------------------------------------------------------------------------------------
# An impossible call time is a 400, and no error path leaves a temp body behind
# --------------------------------------------------------------------------------------


def incoming(root: Path) -> list[Path]:
    """Everything left under ``uploads/.incoming`` — always empty once a request is over."""
    staging = root / "uploads" / ".incoming"
    return [p for p in staging.rglob("*")] if staging.is_dir() else []


@pytest.mark.parametrize(
    "stamp",
    [
        # milliseconds where seconds were promised — the case that used to 500
        str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp() * 1000)),
        # ~1e11, which ``fromtimestamp`` happily read as the year 5138
        "99999999999",
        # before 2000, and negative
        "946684799",
        "-5",
        # more than a day ahead
        str(int((datetime.now(UTC) + timedelta(days=2)).timestamp())),
    ],
)
def test_a_datetime_outside_2000_to_tomorrow_is_a_400(  # noqa: ANN001
    client, session, token, tagged_mp3, tmp_path, stamp
) -> None:
    response = post(client, tagged_mp3, dateTime=stamp)

    assert response.status_code == 400
    assert response.text == "incomplete call data: dateTime out of range"
    assert session.execute(select(Call)).first() is None
    assert incoming(tmp_path) == []


def test_a_datetime_within_a_day_ahead_is_still_accepted(client, session, token, stripped_mp3) -> None:  # noqa: ANN001
    """Clock skew is not a unit error: an hour fast is stored, as it always was."""
    ahead = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    assert post(client, stripped_mp3, dateTime=str(ahead)).text == OK


def test_start_ms_is_bounded_on_its_own() -> None:
    """With no ``dateTime`` to agree with, a 13-digit value was believed up to 2286."""
    in_2200 = str(int(datetime(2200, 1, 1, tzinfo=UTC).timestamp() * 1000))
    assert len(in_2200) == 13  # noqa: PLR2004 - the digit count _ms_epoch insists on
    assert upload_module._ms_epoch(in_2200, near=None) is None  # noqa: SLF001
    fine = datetime(2026, 8, 29, 19, 17, 3, 250_000, tzinfo=UTC)
    assert upload_module._ms_epoch(str(int(fine.timestamp() * 1000)), near=None) == fine.timestamp()  # noqa: SLF001


def test_an_unexpected_parse_error_is_a_500_and_leaves_nothing_behind(  # noqa: ANN001
    client, session, token, tagged_mp3, tmp_path, monkeypatch
) -> None:
    """Only ParseError/ValueError were handled; anything else escaped with the body in
    ``.incoming``."""

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("a MutagenError the parser does not translate")

    monkeypatch.setattr(upload_module, "build_upload_record", explode)
    response = post(client, tagged_mp3)

    assert response.status_code == 500
    assert response.text == "import failed: RuntimeError"
    assert incoming(tmp_path) == []
    assert not list((tmp_path / "uploads").rglob("*.mp3"))


def test_a_failed_move_is_a_500_and_leaves_nothing_behind(  # noqa: ANN001
    client, session, token, tagged_mp3, tmp_path, monkeypatch
) -> None:
    """The move stage sat outside every handler."""

    def full_disk(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(upload_module, "file_upload", full_disk)
    response = post(client, tagged_mp3)

    assert response.status_code == 500
    assert response.text == "import failed: OSError"
    assert incoming(tmp_path) == []
    assert session.execute(select(Call)).first() is None


def test_a_failed_receive_leaves_nothing_behind(  # noqa: ANN001
    client, session, token, tagged_mp3, tmp_path, monkeypatch
) -> None:
    def full_disk(upload, target, limit):  # noqa: ANN001, ANN202, ARG001
        target.write_bytes(b"half a body")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(upload_module, "stream_to_path", full_disk)
    with pytest.raises(OSError, match="No space"):
        post(client, tagged_mp3)
    assert incoming(tmp_path) == []


def test_a_partition_pruned_under_the_reservation_is_made_again(tmp_path, settings, monkeypatch) -> None:  # noqa: ANN001
    """``prune_empty_dirs`` can rmdir the partition between the mkdir and the O_EXCL create."""
    real = upload_module._reserve_in  # noqa: SLF001
    calls: list[Path] = []

    def pruned_first(directory: Path, name: str) -> Path:
        calls.append(directory)
        if len(calls) == 1:
            directory.rmdir()  # the concurrent prune
        return real(directory, name)

    monkeypatch.setattr(upload_module, "_reserve_in", pruned_first)
    when = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    target = upload_module.upload_path(tmp_path, when, "call.mp3", settings)

    assert len(calls) == 2  # noqa: PLR2004 - one FileNotFoundError, one retry
    assert target.is_file()


def test_the_sweep_removes_only_abandoned_incoming_entries(tmp_path, upload_settings) -> None:  # noqa: ANN001
    import os

    from app.audio import sweep_incoming

    staging = tmp_path / "uploads" / ".incoming"
    old = staging / "deadbeef" / "crashed.mp3"
    fresh = staging / "cafef00d" / "receiving.mp3"
    for path in (old, fresh):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x00")
    stale = datetime.now(UTC).timestamp() - 7 * 3600
    os.utime(old, (stale, stale))
    os.utime(old.parent, (stale, stale))

    assert sweep_incoming(upload_settings) == 1
    assert not old.parent.exists()
    assert fresh.is_file()
    # nothing to sweep is not an error
    assert sweep_incoming(upload_settings.model_copy(update={"audio_root": tmp_path / "nowhere"})) == 0


def test_two_concurrent_audio_less_pushes_get_different_paths(session, upload_settings) -> None:  # noqa: ANN001
    """The name was checked before any lock, so the second INSERT hit the unique index."""
    import threading

    from app.db import SessionLocal
    from tests.test_reconcile import _call

    when = datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)
    name = "1701-1756494000.mp3"
    first = upload_module.metadata_upload_path(session, when, name, upload_settings)
    _call(session, path=first)  # written, not committed: the lock is still held

    answer: list[str] = []
    other = SessionLocal()

    def second_push() -> None:
        try:
            answer.append(upload_module.metadata_upload_path(other, when, name, upload_settings))
        finally:
            other.rollback()
            other.close()

    thread = threading.Thread(target=second_push)
    thread.start()
    thread.join(0.5)
    assert thread.is_alive(), "the second push must wait for the first to commit"

    session.commit()
    thread.join(10)
    assert answer == [first.replace(".mp3", "_1.mp3")]
