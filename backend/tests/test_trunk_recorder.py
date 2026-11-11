"""trunk-recorder's dialect end to end — parser, ingest, watcher, upload, audio route.

The fixtures under ``tests/fixtures/trunk_recorder/EXAMPLE/2026/8/29/`` are the four shapes
the ingest has to survive, laid out exactly the way trunk-recorder lays them out (note the
un-padded month and day):

``…-call_1``
    a plain call: one WAV, one sidecar, **two** ``srcList`` entries.
``…-call_2``
    a patch: ``patched_talkgroups``, an emergency flag, and both a WAV *and* an M4A of the
    same audio, so ``TR_PREFER_AUDIO`` has something to choose between.
``…-call_3``
    an encrypted call: a sidecar and no audio at all.
``…-call_4``
    audio with no sidecar — the recorder died between writing the two, or a custom
    ``filenameFormat`` dropped it.

The audio is generated (``ffmpeg -f lavfi -i sine=…``) rather than real traffic, because
what is under test is metadata, and a 2.5-second sine is a perfectly good 2.5 seconds.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import call_upload as upload_module
from app.api.call_upload import (
    audio_extension,
    is_trunk_recorder_form,
    parse_patches,
    parse_sources,
    router,
)
from app.api.uploads import body_size_guard
from app.config import Settings
from app.db import get_session
from app.ingest.parser import ParseError, build_call_record, probe_audio
from app.ingest.service import (
    await_trunk_recorder_call,
    ingest_file,
    ingest_tree,
    iter_recordings,
)
from app.ingest.tr_parser import (
    audio_for_sidecar,
    is_trunk_recorder_call,
    is_trunk_recorder_name,
    parse_tr_filename,
    parse_tr_sidecar,
)
from app.models import ApiToken, Call, CallSource, IngestFile, Radio, RadioAffiliation, Talkgroup
from tests.conftest import FIXTURES, TEST_DATABASE_URL, requires_db

TR_ROOT = FIXTURES / "trunk_recorder"
DAY = TR_ROOT / "EXAMPLE" / "2026" / "8" / "29"

#: the epoch every fixture is built around: 2026-08-29 14:00:00 UTC
BASE_EPOCH = 1788012000

PLAIN_WAV = DAY / f"1701-{BASE_EPOCH}.123_852137500-call_1.wav"
PLAIN_JSON = DAY / f"1701-{BASE_EPOCH}.123_852137500-call_1.json"
PATCH_WAV = DAY / f"1198-{BASE_EPOCH + 60}.500_852137500-call_2.wav"
PATCH_M4A = DAY / f"1198-{BASE_EPOCH + 60}.500_852137500-call_2.m4a"
PATCH_JSON = DAY / f"1198-{BASE_EPOCH + 60}.500_852137500-call_2.json"
ENCRYPTED_JSON = DAY / f"1300-{BASE_EPOCH + 130}.000_851012500-call_3.json"
NO_SIDECAR_WAV = DAY / f"1777-{BASE_EPOCH + 200}.000_851012500.1-call_4.wav"


@pytest.fixture(scope="session", autouse=True)
def _fixtures_present() -> None:
    missing = [p.name for p in (PLAIN_WAV, PATCH_M4A, ENCRYPTED_JSON, NO_SIDECAR_WAV) if not p.is_file()]
    if missing:  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip(f"trunk-recorder fixtures missing: {missing}")


@pytest.fixture
def tr_settings(settings, tmp_path):  # noqa: ANN001, ANN201
    """The standard test settings pointed at a private copy of the fixture day.

    A copy, not the fixtures themselves, because several tests here add and remove files to
    provoke the sidecar-wait behavior.
    """
    root = tmp_path / "capture"
    shutil.copytree(TR_ROOT, root)
    return settings.model_copy(update={"audio_root": root})


def tr_root(tr_settings) -> Path:  # noqa: ANN001
    return Path(tr_settings.audio_root)


def day_of(tr_settings) -> Path:  # noqa: ANN001
    return tr_root(tr_settings) / "EXAMPLE" / "2026" / "8" / "29"


# --------------------------------------------------------------------------------------
# recognizing the dialect
# --------------------------------------------------------------------------------------


def test_the_call_suffix_is_what_identifies_a_trunk_recorder_file() -> None:
    assert is_trunk_recorder_name("1701-1788012000.123_852137500-call_1.wav")
    assert is_trunk_recorder_name("1701-1788012000.123_852137500.1-call_12.json")
    # a custom filenameFormat still appends -call_<n>
    assert is_trunk_recorder_name("dispatch_2026-08-29_0900-call_3.m4a")
    # sdrtrunk's name has no such suffix
    assert not is_trunk_recorder_name(
        "20260829_090328_Example-System_Central_T-Control__TO_1701_FROM_5226037.mp3"
    )


def test_a_sidecar_beside_a_file_identifies_it_even_with_an_unrecognizable_name(tmp_path: Path) -> None:
    audio = tmp_path / "whatever.wav"
    audio.write_bytes(b"")
    assert not is_trunk_recorder_call(audio)
    (tmp_path / "whatever.json").write_text("{}")
    assert is_trunk_recorder_call(audio)


def test_the_filename_carries_the_talkgroup_time_frequency_and_system() -> None:
    parts = parse_tr_filename(NO_SIDECAR_WAV)
    assert parts.talkgroup == 1777
    assert parts.frequency_hz == 851012500
    assert parts.tdma_slot == 1
    assert parts.call_num == 4
    # <captureDir>/<shortName>/<Y>/<M>/<D>/ — the system is four directories up
    assert parts.short_name == "EXAMPLE"
    assert parts.started_at == datetime.fromtimestamp(BASE_EPOCH + 200, tz=UTC)


def test_a_fractional_second_is_padded_not_counted() -> None:
    """``.5`` is half a second, not five milliseconds."""
    parts = parse_tr_filename(PATCH_WAV)
    assert parts.started_at == datetime.fromtimestamp(BASE_EPOCH + 60.5, tz=UTC)


# --------------------------------------------------------------------------------------
# the sidecar
# --------------------------------------------------------------------------------------


def test_parse_tr_sidecar_reads_every_field_the_columns_need() -> None:
    meta = parse_tr_sidecar(PLAIN_JSON)
    assert meta.short_name == "EXAMPLE"
    assert meta.talkgroup == 1701
    assert meta.frequency_hz == 852137500
    assert meta.audio_type == "digital"
    assert meta.signal_db == -47
    assert meta.noise_db == -102
    assert meta.freq_error == -13
    assert meta.call_num == 1
    assert meta.duration_s == 2.5
    assert meta.started_at == datetime.fromtimestamp(BASE_EPOCH + 0.123, tz=UTC)
    # 0/1 flags, not booleans, on the wire
    assert meta.emergency is False
    assert meta.encrypted is False
    assert len(meta.freq_list) == 1


def test_the_alias_comes_from_talkgroup_group_tag_not_talkgroup_tag() -> None:
    """§1.7: their JSON names are shifted against their CSV, and reading them literally
    would put the service tag ("Law Dispatch") in front of users as a channel name."""
    meta = parse_tr_sidecar(PLAIN_JSON)
    assert meta.talkgroup_alias == "EX SO DISP"  # talkgroup_group_tag — the alpha tag
    assert meta.talkgroup_tag == "Law Dispatch"  # the service tag, kept but not shown as the alias
    assert meta.talkgroup_group == "Example County"  # the category -> talkgroups.group_name
    assert meta.talkgroup_description == "Example County sheriff primary dispatch"


def test_a_sidecar_that_is_not_json_is_a_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "x-call_1.json"
    bad.write_text("{not json")
    with pytest.raises(ParseError):
        parse_tr_sidecar(bad)


def test_a_sidecar_that_is_a_list_is_a_parse_error(tmp_path: Path) -> None:
    bad = tmp_path / "x-call_1.json"
    bad.write_text("[1, 2, 3]")
    with pytest.raises(ParseError):
        parse_tr_sidecar(bad)


# --------------------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------------------


def test_a_plain_call_takes_its_times_from_the_sidecar() -> None:
    record = build_call_record(PLAIN_WAV, TR_ROOT)
    assert record.ingest_source == "trunk_recorder"
    assert record.time_source == "sidecar"
    assert record.system == "EXAMPLE"
    assert record.tgid == 1701
    assert record.talkgroup_ids == (1701,)
    assert record.call_type == "group"
    assert record.duration_s == 2.5
    assert record.started_at == datetime.fromtimestamp(BASE_EPOCH + 0.123, tz=UTC)
    assert record.ended_at == datetime.fromtimestamp(BASE_EPOCH + 2.623, tz=UTC)
    assert record.audio_type == "digital"
    assert record.audio_state == "present"
    assert record.talkgroup_alias == "EX SO DISP"
    assert record.talkgroup_group == "Example County"
    # the whole sidecar is kept verbatim
    assert record.raw_meta["talkgroup_tag"] == "Law Dispatch"


def test_the_source_list_becomes_offsets_with_lengths_derived_from_the_next_one() -> None:
    record = build_call_record(PLAIN_WAV, TR_ROOT)
    first, second = record.sources
    assert (first.src, first.pos_s, first.tag, first.seq) == (5250057, 0.0, "UNIT 41", 0)
    # the first radio held the channel until the second keyed up
    assert first.len_s == 1.4
    # ...and the last one until the end of the call
    assert (second.src, second.pos_s, second.seq) == (5226037, 1.4, 1)
    assert second.len_s == pytest.approx(1.1)
    # the first source is the one denormalized onto calls.radio_id
    assert record.radio is not None and record.radio.radio_id == 5250057
    assert record.radio_alias == "UNIT 41"


def test_a_patch_carries_every_member_and_prefers_the_m4a() -> None:
    record = build_call_record(PATCH_JSON, TR_ROOT)
    assert record.call_type == "patch"
    # the primary stays the talkgroup the radio was keyed to; the patched ones follow
    assert record.tgid == 1198
    assert record.talkgroup_ids == (1198, 1701, 1400)
    # trunk-recorder gives a patch no id of its own, only a member list
    assert record.patch_id is None
    assert record.emergency is True
    assert record.audio_type == "digital tdma"
    assert Path(record.file_path).suffix == ".m4a"


def test_prefer_audio_wav_selects_the_wav_of_the_same_call() -> None:
    record = build_call_record(PATCH_JSON, TR_ROOT, prefer_audio="wav")
    assert Path(record.file_path).suffix == ".wav"
    # same call either way: the sidecar decided every fact but the bytes
    assert record.duration_s == 4.0
    assert record.started_at == datetime.fromtimestamp(BASE_EPOCH + 60.5, tz=UTC)


def test_an_encrypted_call_is_a_record_with_no_audio() -> None:
    record = build_call_record(ENCRYPTED_JSON, TR_ROOT)
    assert record.encrypted is True
    assert record.audio_state == "purged"
    # the row points at the sidecar, because that is the only file there is
    assert record.file_path.endswith(".json")
    assert record.duration_s == 7.0
    assert record.bitrate is None


def test_without_a_sidecar_the_filename_answers_and_says_so() -> None:
    record = build_call_record(NO_SIDECAR_WAV, TR_ROOT)
    assert record.ingest_source == "trunk_recorder"
    # nothing corroborates the filename's epoch, so it is labeled as approximate
    assert record.time_source == "file_write"
    assert record.tgid == 1777
    assert record.system == "EXAMPLE"
    assert record.duration_s == pytest.approx(1.8, abs=0.05)  # measured from the audio
    assert record.sources == ()
    assert record.emergency is None


def test_probe_audio_reads_wav_and_m4a() -> None:
    wav = probe_audio(PATCH_WAV)
    m4a = probe_audio(PATCH_M4A)
    assert wav.sample_rate == 16000
    assert wav.channels == 1
    assert wav.duration_s == pytest.approx(4.0, abs=0.01)
    assert m4a.duration_s == pytest.approx(4.0, abs=0.1)


# --------------------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------------------


def test_the_walk_yields_one_path_per_call(tr_settings) -> None:  # noqa: ANN001
    found = {p.name for p in iter_recordings(tr_root(tr_settings))}
    assert found == {
        PLAIN_WAV.name,
        PATCH_M4A.name,  # the m4a wins; the wav of the same call is not a second call
        ENCRYPTED_JSON.name,  # a sidecar on its own is a call
        NO_SIDECAR_WAV.name,
    }


def test_the_walk_honours_tr_prefer_audio(tr_settings) -> None:  # noqa: ANN001
    found = {p.name for p in iter_recordings(tr_root(tr_settings), prefer_audio="wav")}
    assert PATCH_WAV.name in found
    assert PATCH_M4A.name not in found


def test_the_walk_ignores_the_raw_baseband_capture(tr_settings) -> None:  # noqa: ANN001
    (day_of(tr_settings) / "1701-1788012000.123_852137500-call_9.raw.wav").write_bytes(b"x" * 100)
    assert not any(p.name.endswith(".raw.wav") for p in iter_recordings(tr_root(tr_settings)))


def test_the_walk_ignores_a_json_that_is_not_a_sidecar(tr_settings) -> None:  # noqa: ANN001
    (tr_root(tr_settings) / "config.json").write_text('{"captureDir": "/x"}')
    assert not any(p.name == "config.json" for p in iter_recordings(tr_root(tr_settings)))


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------


@requires_db
def test_ingesting_a_plain_call_writes_a_source_row_per_keying_radio(
    session, tr_settings  # noqa: ANN001
) -> None:
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / PLAIN_WAV.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    assert result.status == "ingested"

    call = session.get(Call, result.call_id)
    assert call.ingest_source == "trunk_recorder"
    assert call.time_source == "sidecar"
    assert call.audio_type == "digital"
    assert (call.signal_db, call.noise_db, call.freq_error) == (-47, -102, -13)
    assert call.recorder_call_num == 1
    assert call.emergency is False and call.encrypted is False
    assert call.raw_meta["talkgroup_group_tag"] == "EX SO DISP"
    assert float(call.duration_s) == 2.5

    sources = session.scalars(
        select(CallSource).where(CallSource.call_id == call.id).order_by(CallSource.seq)
    ).all()
    assert [s.src for s in sources] == [5250057, 5226037]
    assert [float(s.pos_s) for s in sources] == [0.0, 1.4]
    assert sources[0].tag == "UNIT 41"
    # the first source is the call's radio
    assert call.radio_id == sources[0].radio_id

    # every source is a radio in the directory, with the recorder's tag as its alias
    radios = {r.radio_id: r for r in session.scalars(select(Radio)).all()}
    assert set(radios) == {5250057, 5226037}
    assert radios[5250057].alias == "UNIT 41"
    assert radios[5226037].alias is None  # that entry carried no tag

    # ...and every source counts towards "who was on this channel", not just the first
    assert session.scalar(select(RadioAffiliation).where(RadioAffiliation.radio_id_fk == radios[5226037].id))


@requires_db
def test_the_talkgroup_takes_the_alpha_tag_as_its_alias(session, tr_settings) -> None:  # noqa: ANN001
    """The gotcha, at the far end: what a user sees in the talkgroup list."""
    root = tr_root(tr_settings)
    ingest_file(session, root / PLAIN_WAV.relative_to(TR_ROOT), settings=tr_settings, audio_root=root)
    tg = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1701))
    assert tg.alias == "EX SO DISP"
    assert tg.group_name == "Example County"
    assert tg.description == "Example County sheriff primary dispatch"


@requires_db
def test_a_patched_call_carries_every_member_talkgroup(session, tr_settings) -> None:  # noqa: ANN001
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / PATCH_JSON.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    call = session.get(Call, result.call_id)
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [1198, 1701, 1400]
    assert call.emergency is True
    # the m4a is the file, and the wav of the same call was not ingested as a second one
    assert call.file_path.endswith(".m4a")
    # every member talkgroup exists so "calls involving 1701" finds this one
    assert {t.tgid for t in session.scalars(select(Talkgroup)).all()} == {1198, 1701, 1400}


@requires_db
def test_an_encrypted_call_is_ingested_with_purged_audio(session, tr_settings) -> None:  # noqa: ANN001
    """It has no audio and is still worth having: it proves an agency held the talkgroup."""
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / ENCRYPTED_JSON.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    assert result.status == "ingested"
    call = session.get(Call, result.call_id)
    assert call.encrypted is True
    assert call.audio_state == "purged"
    assert call.file_path.endswith(".json")
    assert float(call.duration_s) == 7.0
    # nothing to transcribe, so it is never queued
    assert call.transcript_status == "skipped"
    assert result.job_id is None


@requires_db
def test_a_scan_of_the_whole_tree_finds_four_calls(session, tr_settings) -> None:  # noqa: ANN001
    from app.db import SessionLocal

    root = tr_root(tr_settings)
    # The no-sidecar fixture is only deferred while it is *fresh*; age it past the wait.
    old = time.time() - 3600
    for path in root.rglob("*"):
        if path.is_file():
            import os

            os.utime(path, (old, old))

    summary = ingest_tree(SessionLocal, root, workers=1, settings=tr_settings, audio_root=root)
    assert summary.files_seen == 4
    assert summary.files_new == 4
    assert summary.files_failed == 0

    calls = session.scalars(select(Call).order_by(Call.started_at)).all()
    assert [c.talkgroup_ids[0] for c in calls] == [1701, 1198, 1300, 1777]
    assert [c.time_source for c in calls] == ["sidecar", "sidecar", "sidecar", "file_write"]


@requires_db
def test_a_scan_defers_audio_whose_sidecar_may_still_be_coming(
    session, tr_settings  # noqa: ANN001
) -> None:
    import os

    from app.db import SessionLocal

    root = tr_root(tr_settings)
    # remove every sidecar, and make every recording look as if it had just been written
    for path in root.rglob("*.json"):
        path.unlink()
    now = time.time()
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (now, now))
    summary = ingest_tree(SessionLocal, root, workers=1, settings=tr_settings, audio_root=root)
    assert summary.files_new == 0
    assert summary.files_skipped == summary.files_seen


@requires_db
def test_ingesting_the_same_call_by_its_sidecar_and_its_audio_is_one_row(
    session, tr_settings  # noqa: ANN001
) -> None:
    root = tr_root(tr_settings)
    first = ingest_file(
        session, root / PLAIN_JSON.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    second = ingest_file(
        session, root / PLAIN_WAV.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    assert first.status == "ingested"
    # the sidecar resolved to the audio, so the second call is the same file_path
    assert second.status == "skipped"
    assert second.call_id == first.call_id
    assert session.scalar(select(Call.id).where(Call.id != first.call_id)) is None


# --------------------------------------------------------------------------------------
# the watcher's sidecar wait
# --------------------------------------------------------------------------------------


def test_the_wait_returns_as_soon_as_the_sidecar_lands(tmp_path: Path) -> None:
    audio = tmp_path / "1701-1788012000.000_852137500-call_1.wav"
    sidecar = tmp_path / "1701-1788012000.000_852137500-call_1.json"
    audio.write_bytes(b"RIFF")

    def write_sidecar() -> None:
        time.sleep(0.3)
        sidecar.write_text("{}")

    thread = threading.Thread(target=write_sidecar)
    thread.start()
    started = time.monotonic()
    resolved = await_trunk_recorder_call(audio, wait_seconds=10, poll_seconds=0.05)
    elapsed = time.monotonic() - started
    thread.join()

    assert resolved == audio
    assert sidecar.is_file()
    # it waited for the sidecar, and did not wait the whole ten seconds for it
    assert 0.25 <= elapsed < 5


def test_the_wait_gives_up_and_ingests_the_audio_alone(tmp_path: Path) -> None:
    audio = tmp_path / "1701-1788012000.000_852137500-call_1.wav"
    audio.write_bytes(b"RIFF")
    started = time.monotonic()
    resolved = await_trunk_recorder_call(audio, wait_seconds=0.3, poll_seconds=0.05)
    assert resolved == audio
    assert time.monotonic() - started >= 0.3


def test_the_wait_collapses_all_three_files_of_a_call_onto_one_path(tmp_path: Path) -> None:
    """A wav, an m4a and a json fire three events; they must resolve to one ingest."""
    stem = "1701-1788012000.000_852137500-call_1"
    for suffix in (".wav", ".m4a", ".json"):
        (tmp_path / f"{stem}{suffix}").write_bytes(b"x")
    resolved = {
        await_trunk_recorder_call(tmp_path / f"{stem}{suffix}", wait_seconds=0.1, poll_seconds=0.05)
        for suffix in (".wav", ".m4a", ".json")
    }
    assert resolved == {tmp_path / f"{stem}.m4a"}


def test_the_wait_falls_back_to_the_sidecar_when_there_is_no_audio(tmp_path: Path) -> None:
    sidecar = tmp_path / "1300-1788012130.000_851012500-call_3.json"
    sidecar.write_text("{}")
    assert await_trunk_recorder_call(sidecar, wait_seconds=0.1) == sidecar


def test_audio_for_sidecar_is_none_when_the_call_was_encrypted() -> None:
    assert audio_for_sidecar(ENCRYPTED_JSON) is None
    assert audio_for_sidecar(PATCH_JSON) == PATCH_M4A


@requires_db
def test_the_watcher_waits_for_the_sidecar_before_ingesting(
    session, tr_settings, tmp_path  # noqa: ANN001
) -> None:
    """The audio lands first; the call must still come out with the sidecar's exact time."""
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "watched"
    (tree / "EXAMPLE" / "2026" / "8" / "29").mkdir(parents=True)
    local = tr_settings.model_copy(
        update={
            "audio_root": tree,
            "ingest_settle_seconds": 0.2,
            "ingest_scan_minutes": 60,
            "tr_sidecar_wait_seconds": 10.0,
        }
    )
    destination = tree / "EXAMPLE" / "2026" / "8" / "29"

    watcher = IngestWatcher(local, root=tree)
    watcher.start(workers=1, initial_scan=False)
    try:
        shutil.copy2(PLAIN_WAV, destination / PLAIN_WAV.name)
        time.sleep(1.0)  # the audio is there and the sidecar is not: nothing may be written
        assert session.scalar(select(Call.id)) is None
        shutil.copy2(PLAIN_JSON, destination / PLAIN_JSON.name)

        deadline = time.monotonic() + 20
        call = None
        while time.monotonic() < deadline and call is None:
            session.rollback()
            call = session.scalar(select(Call))
            if call is None:
                time.sleep(0.25)
    finally:
        watcher.shutdown()

    assert call is not None
    assert call.time_source == "sidecar"
    assert call.started_at == datetime.fromtimestamp(BASE_EPOCH + 0.123, tz=UTC)
    assert session.scalar(select(IngestFile.status)) == "ingested"


# --------------------------------------------------------------------------------------
# the upload endpoint
# --------------------------------------------------------------------------------------

KEY = "trunk-recorder-key"


@pytest.fixture
def upload_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path / "audio",
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
        conversation_gap_seconds=20,
    )


@pytest.fixture
def client(session, upload_settings, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(upload_module, "get_settings", lambda: upload_settings)
    app = FastAPI()
    app.middleware("http")(body_size_guard)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(session):  # noqa: ANN001, ANN201
    row = ApiToken(
        label="trunk-recorder",
        token_hash=hashlib.sha256(KEY.encode()).hexdigest(),
        token_prefix=KEY[:8],
        scopes=["upload"],
    )
    session.add(row)
    session.commit()
    return row


def post_tr(client, audio_path: Path, media_type: str, **fields):  # noqa: ANN001, ANN003
    """One upload with trunk-recorder's ``rdioscanner_uploader`` field set."""
    form = {
        "key": KEY,
        "system": "EXAMPLE",
        "dateTime": str(BASE_EPOCH + 60),
        "talkgroup": "1198",
        "source": "5250057",
        "frequency": "852137500",
        "systemLabel": "EXAMPLE",
        "talkgroupLabel": "EX INTEROP",
        "talkgroupName": "County-wide interop patch",
        "talkgroupGroup": "Interoperability",
        "talkgroupTag": "Interop",
        "audioType": media_type,
        "patches": "[1701, 1400]",
        "sources": json.dumps(
            [
                {"pos": 0.0, "src": 5250057, "tag": "UNIT 41"},
                {"pos": 2.5, "src": 5226037, "tag": "SO 12"},
            ]
        ),
        "frequencies": json.dumps([{"freq": 852137500, "pos": 0.0, "len": 4.0, "error_count": 0}]),
    }
    form.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": (audio_path.name, audio_path.read_bytes(), media_type)},
    )


def test_the_dialect_is_told_apart_by_the_fields_only_trunk_recorder_sends() -> None:
    assert is_trunk_recorder_form({"sources": "[]"})
    assert is_trunk_recorder_form({"frequencies": "[{}]"})
    assert not is_trunk_recorder_form({"patches": "[9230, 1282]"})


def test_patches_are_read_as_a_supergroup_or_a_member_list_per_dialect() -> None:
    # sdrtrunk: the supergroup comes first
    assert parse_patches("[9230, 1282, 1551]") == (9230, (1282, 1551))
    # trunk-recorder: patched_talkgroups verbatim, no supergroup at all
    assert parse_patches("[1701, 1400]", supergroup_first=False) == (None, (1701, 1400))


def test_parse_sources_derives_each_length_from_the_next_offset() -> None:
    sources = parse_sources(
        json.dumps([{"pos": 2.5, "src": 2}, {"pos": 0, "src": 1, "tag": "UNIT 41"}]), 4.0
    )
    assert [(s.src, s.pos_s, s.len_s, s.seq) for s in sources] == [
        (1, 0.0, 2.5, 0),
        (2, 2.5, 1.5, 1),
    ]


def test_parse_sources_shrugs_off_junk() -> None:
    assert parse_sources("not json", 4.0) == ()
    assert parse_sources("", 4.0) == ()
    assert parse_sources('{"src": 1}', 4.0) == ()


def test_audio_type_decides_the_stored_extension() -> None:
    assert audio_extension("audio/mp4") == ".m4a"
    assert audio_extension("audio/wav") == ".wav"
    assert audio_extension("audio/mpeg") == ".mp3"
    assert audio_extension(None) == ".mp3"
    # a type we do not know falls back to the posted name, then to MP3
    assert audio_extension("application/octet-stream", "call.m4a") == ".m4a"
    assert audio_extension("application/octet-stream", "call.bin") == ".mp3"


@requires_db
def test_an_m4a_push_is_stored_as_m4a_with_its_sources_and_patches(
    client, token, session, upload_settings  # noqa: ANN001, ARG001
) -> None:
    response = post_tr(client, PATCH_M4A, "audio/mp4")
    assert response.status_code == 200
    assert response.text == "Call imported successfully."

    call = session.scalar(select(Call))
    assert call.ingest_source == "trunk_recorder"
    assert call.file_path.endswith(".m4a")
    assert (Path(upload_settings.audio_root) / call.file_path).is_file()
    assert call.audio_type == "audio/mp4"
    # patches are the member list, not a supergroup id
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [1198, 1701, 1400]
    assert call.patch_id is None
    # the per-transmission lists are kept verbatim
    assert call.raw_meta["frequencies"][0]["freq"] == 852137500

    sources = session.scalars(
        select(CallSource).where(CallSource.call_id == call.id).order_by(CallSource.seq)
    ).all()
    assert [(s.src, float(s.pos_s), s.tag) for s in sources] == [
        (5250057, 0.0, "UNIT 41"),
        (5226037, 2.5, "SO 12"),
    ]
    assert {r.radio_id for r in session.scalars(select(Radio)).all()} == {5250057, 5226037}

    tg = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1198))
    assert tg.alias == "EX INTEROP"
    assert tg.group_name == "Interoperability"
    assert tg.description == "County-wide interop patch"


@requires_db
def test_a_wav_push_is_stored_as_wav(client, token, session) -> None:  # noqa: ANN001, ARG001
    response = post_tr(client, PLAIN_WAV, "audio/wav", patches=None, talkgroup="1701")
    assert response.text == "Call imported successfully."
    call = session.scalar(select(Call))
    assert call.file_path.endswith(".wav")
    assert float(call.duration_s) == pytest.approx(2.5, abs=0.05)


@requires_db
def test_an_sdrtrunk_push_still_reads_patches_as_a_supergroup(
    client, token, session, fixture_audio  # noqa: ANN001, ARG001
) -> None:
    """The trunk-recorder fields are absent, so nothing about the old contract changes."""
    mp3 = fixture_audio / (
        "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
    )
    if not mp3.is_file():  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip("sdrtrunk fixture missing")
    response = client.post(
        "/api/call-upload",
        data={
            "key": KEY,
            "system": "1",
            "dateTime": str(BASE_EPOCH),
            "talkgroup": "1701",
            "source": "9200006",
            "patches": "[9230, 1282, 1551]",
        },
        files={"audio": (mp3.name, mp3.read_bytes(), "audio/mpeg")},
    )
    assert response.text == "Call imported successfully."
    call = session.scalar(select(Call))
    assert call.ingest_source == "upload"
    assert call.file_path.endswith(".mp3")
    assert call.talkgroup_ids == [1701, 1282, 1551]
    assert call.patch_id is not None  # 9230 became a patches row
    # sdrtrunk's single source still produces exactly one call_sources row, at offset 0
    sources = session.scalars(select(CallSource).where(CallSource.call_id == call.id)).all()
    assert [(s.src, float(s.pos_s)) for s in sources] == [(9200006, 0.0)]


# --------------------------------------------------------------------------------------
# serving what was ingested
# --------------------------------------------------------------------------------------


def test_the_media_type_follows_the_extension() -> None:
    from app.api.calls import audio_media_type

    assert audio_media_type(Path("x.mp3")) == "audio/mpeg"
    assert audio_media_type(Path("x.m4a")) == "audio/mp4"
    assert audio_media_type(Path("x.wav")) == "audio/wav"
    # an extension nothing writes still gets a type, so the route never answers untyped
    assert audio_media_type(Path("x.opus")) == "audio/mpeg"


@pytest.fixture
def calls_client(session, tr_settings, monkeypatch):  # noqa: ANN001, ANN201
    """The calls router alone, reading the copied trunk-recorder tree."""
    from app.api import calls as calls_module

    monkeypatch.setattr(calls_module, "get_settings", lambda: tr_settings)
    app = FastAPI()
    app.include_router(calls_module.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@requires_db
def test_an_m4a_call_is_served_as_audio_mp4(
    calls_client, session, tr_settings  # noqa: ANN001
) -> None:
    """A browser told `audio/mpeg` for an M4A either refuses it or plays it by luck."""
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / PATCH_M4A.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    session.commit()

    response = calls_client.get(f"/api/calls/{result.call_id}/audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mp4"

    # ...and a Range request keeps the same type, because a media element seeks with one
    partial = calls_client.get(
        f"/api/calls/{result.call_id}/audio", headers={"Range": "bytes=0-1023"}
    )
    assert partial.status_code == 206
    assert partial.headers["content-type"] == "audio/mp4"


@requires_db
def test_a_wav_call_is_served_as_audio_wav(calls_client, session, tr_settings) -> None:  # noqa: ANN001
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / PLAIN_WAV.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    session.commit()
    response = calls_client.get(f"/api/calls/{result.call_id}/audio")
    assert response.headers["content-type"] == "audio/wav"


@requires_db
def test_an_encrypted_call_has_a_detail_page_and_no_audio(
    calls_client, session, tr_settings  # noqa: ANN001
) -> None:
    """The row is the whole point of ingesting it, so everything but the bytes must work."""
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / ENCRYPTED_JSON.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    session.commit()

    detail = calls_client.get(f"/api/calls/{result.call_id}").json()
    assert detail["encrypted"] is True
    assert detail["audio_type"] == "digital tdma"
    assert detail["duration_s"] == 7.0
    assert detail["raw"]["raw_meta"]["talkgroup_group_tag"] == "EX TF TAC"
    assert [s["src"] for s in detail["sources"]] == [5259001]

    # 410, not 404: the row is right and the bytes were never there to have
    assert calls_client.get(f"/api/calls/{result.call_id}/audio").status_code == 410


@requires_db
def test_the_detail_page_lists_every_source_with_its_offset(
    calls_client, session, tr_settings  # noqa: ANN001
) -> None:
    root = tr_root(tr_settings)
    result = ingest_file(
        session, root / PLAIN_WAV.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
    )
    session.commit()

    detail = calls_client.get(f"/api/calls/{result.call_id}").json()
    assert [(s["src"], s["pos_s"], s["radio_label"]) for s in detail["sources"]] == [
        (5250057, 0.0, "UNIT 41"),
        (5226037, 1.4, "5226037"),
    ]
    assert detail["audio_type"] == "digital"
    assert detail["time_source"] == "sidecar"
    assert detail["ingest_source"] == "trunk_recorder"
    # the raw panel shows the sidecar; there are no ID3 frames on a trunk-recorder call
    assert detail["raw"]["id3"] == {}
    assert detail["raw"]["raw_meta"]["short_name"] == "EXAMPLE"


@requires_db
def test_an_sdrtrunk_call_finally_shows_its_id3_frames(
    session, settings, fixture_audio  # noqa: ANN001
) -> None:
    """``raw.id3`` used to be hard-coded empty; ``raw_meta`` is where it comes from now."""
    from app.services.calls import call_detail

    relative = (
        "20260829/EXAMPLE EVENT 1/20260829_141703_Example-System_Central_T-Control__TO_1701_FROM_9200006.mp3"
    )
    if not (fixture_audio / relative).is_file():  # pragma: no cover - ships with the repo
        pytest.skip("sdrtrunk fixture missing")
    result = ingest_file(
        session, fixture_audio / relative, settings=settings, audio_root=fixture_audio
    )
    session.commit()

    detail = call_detail(session, result.call_id)
    assert detail.raw.id3
    assert any(key.startswith("COMM") for key in detail.raw.id3)
    assert detail.raw.id3["TIT2"].startswith("1701")
    assert detail.raw.raw_meta == detail.raw.id3
    # and an sdrtrunk call gets the one source it can see, at offset 0
    assert [(s.src, s.pos_s) for s in detail.sources] == [(9200006, 0.0)]


@requires_db
def test_the_emergency_and_encrypted_filters(calls_client, session, tr_settings) -> None:  # noqa: ANN001
    root = tr_root(tr_settings)
    for fixture in (PLAIN_WAV, PATCH_JSON, ENCRYPTED_JSON):
        ingest_file(
            session, root / fixture.relative_to(TR_ROOT), settings=tr_settings, audio_root=root
        )
    session.commit()

    def tgids(**params: object) -> set[int]:
        body = calls_client.get("/api/calls", params=params).json()
        return {item["tgid"] for item in body["items"]}

    assert tgids() == {1701, 1198, 1300}
    assert tgids(emergency="true") == {1198}
    assert tgids(encrypted="true") == {1300}
    assert tgids(encrypted="false") == {1701, 1198}
    assert tgids(audio_type="digital") == {1701}
    assert tgids(audio_type="digital tdma") == {1198, 1300}


def test_a_form_flag_is_tri_state() -> None:
    """Absent means "the recorder did not say", which is not the same as "no"."""
    from app.api.call_upload import _flag

    assert _flag(None) is None
    assert _flag("") is None
    assert _flag("0") is False
    assert _flag("1") is True
    assert _flag("true") is True


@requires_db
def test_an_explicit_zero_is_stored_as_false_not_null(client, token, session) -> None:  # noqa: ANN001, ARG001
    """`encrypted=false` must find the calls a recorder said were clear."""
    post_tr(client, PLAIN_WAV, "audio/wav", emergency="0", encrypted="0", patches=None)
    call = session.scalar(select(Call))
    assert call.emergency is False
    assert call.encrypted is False
