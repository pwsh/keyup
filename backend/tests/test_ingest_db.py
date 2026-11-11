"""Ingest against a real PostgreSQL: migration, idempotency, dedupe, patches,
conversations, the transcription queue and the failure ledger."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.ingest.parser import build_call_record
from app.ingest.service import (
    assign_conversation,
    find_duplicate,
    ingest_file,
    ingest_status,
    ingest_tree,
    retry_failed,
)
from app.models import (
    Call,
    Conversation,
    IngestFile,
    IngestRun,
    Patch,
    Radio,
    RadioAffiliation,
    Talkgroup,
    TranscriptionJob,
)
from tests.conftest import EXAMPLE_AGENCIES, requires_db
from tests.test_parser import (
    LONGEST,
    NO_FROM,
    PATCH3,
    PATCH4_BARE_TO,
    PLAIN,
    SHORTEST,
)

pytestmark = requires_db


# --------------------------------------------------------------------------------------
# the migration itself
# --------------------------------------------------------------------------------------


def test_migration_builds_the_schema(session) -> None:  # noqa: ANN001
    """``alembic upgrade head`` on an empty database — the conftest fixture ran it."""
    tables = set(
        session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
        .scalars()
        .all()
    )
    for expected in (
        "systems",
        "sites",
        "talkgroups",
        "radios",
        "patches",
        "radio_affiliations",
        "calls",
        "transcripts",
        "transcription_jobs",
        "conversations",
        "external_links",
        "ingest_runs",
        "ingest_files",
        "recorder_gaps",
        "retention_policies",
        "settings",
        "users",
        "user_sessions",
        "api_tokens",
        "saved_searches",
        "favorites",
        "alert_rules",
        "notifications",
        "agencies",
    ):
        assert expected in tables


def test_search_tsv_is_a_stored_generated_column(session) -> None:  # noqa: ANN001
    generated = session.execute(
        text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name='calls' AND column_name='search_tsv'"
        )
    ).scalar_one()
    assert generated == "ALWAYS"


def test_gin_indexes_exist(session) -> None:  # noqa: ANN001
    defs = dict(
        session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='calls'")
        ).all()
    )
    assert "gin" in defs["ix_calls_talkgroup_ids"]
    assert "gin" in defs["ix_calls_search_tsv"]
    assert "gin" in defs["ix_calls_tags"]
    assert "started_at DESC" in defs["ix_calls_started_at"]
    assert "WHERE starred" in defs["ix_calls_starred"]


def test_agency_seed(session) -> None:  # noqa: ANN001
    from app.seed import seed_agencies

    assert seed_agencies(session, path=EXAMPLE_AGENCIES) == 3
    session.commit()
    codes = session.execute(text("SELECT code FROM agencies")).scalars().all()
    assert "EXAMPLEPD" in codes  # Central City PD
    # re-seeding is a refresh, not a duplicate
    assert seed_agencies(session, path=EXAMPLE_AGENCIES) == 3
    session.commit()
    assert session.execute(text("SELECT count(*) FROM agencies")).scalar_one() == 3


# --------------------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------------------


def test_ingest_writes_the_directory_and_the_call(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    assert result.status == "ingested"

    call = session.get(Call, result.call_id)
    assert call.file_path == PLAIN
    assert call.call_type == "group"
    assert call.talkgroup_ids == [1421]
    assert call.talkgroup_alias_at_record == "EXAMPLE EMS OPS"
    assert call.decoder == "P25 Phase 1"
    assert call.frequency_hz == 852137500
    assert call.channel == "1-90"
    assert call.radio_raw == "101010.57.9831120"
    assert call.radio_resolved is True
    assert call.time_source == "file_write"
    assert call.ended_at == call.recorded_at
    assert call.sha256 and call.bitrate == 32000

    radio = session.get(Radio, call.radio_id)
    assert (radio.radio_id, radio.wacn, radio.home_system_id) == (9831120, 101010, 57)
    assert radio.kind == "unknown"  # ingest never guesses
    assert radio.call_count == 1

    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    assert (talkgroup.tgid, talkgroup.alias, talkgroup.call_count) == (1421, "EXAMPLE EMS OPS", 1)
    assert talkgroup.first_seen_at == talkgroup.last_seen_at == call.started_at

    affiliation = session.execute(select(RadioAffiliation)).scalar_one()
    assert (affiliation.radio_id_fk, affiliation.talkgroup_id_fk) == (radio.id, talkgroup.id)
    assert affiliation.call_count == 1


def test_ingest_is_idempotent(session, settings, fixture_audio) -> None:  # noqa: ANN001
    first = ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    second = ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    assert second.status == "skipped"
    assert second.call_id == first.call_id
    assert session.execute(select(Talkgroup.call_count)).scalar_one() == 1
    assert session.execute(select(Radio.call_count)).scalar_one() == 1
    assert len(session.execute(select(Call)).scalars().all()) == 1


def test_unresolved_radio_leaves_no_radio_row(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / NO_FROM, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert call.radio_id is None
    assert call.radio_raw is None
    assert call.radio_resolved is False
    assert session.execute(select(Radio)).scalars().all() == []


def test_patch_members_become_talkgroup_ids(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / PATCH3, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert call.call_type == "patch"
    assert call.talkgroup_ids == [1812, 1561, 1811]
    assert call.talkgroup_alias_at_record == "PATCH 1157"

    patch = session.get(Patch, call.patch_id)
    assert patch.patch_id == 1157
    assert patch.member_tgids == [1812, 1561, 1811]

    # every member exists as a talkgroup, named from TIT2, and counted
    aliases = dict(session.execute(select(Talkgroup.tgid, Talkgroup.alias)).all())
    assert aliases == {1812: "EXAMPLE LAW 3", 1561: "EXAMPLE INTEROP 1", 1811: "EXAMPLE LAW 4"}
    assert all(c == 1 for c in session.execute(select(Talkgroup.call_count)).scalars())
    # the primary talkgroup of a patch is its first member
    assert session.get(Talkgroup, call.talkgroup_id).tgid == 1812


def test_patch_found_by_member_set_even_when_the_filename_hides_it(
    session, settings, fixture_audio  # noqa: ANN001
) -> None:
    result = ingest_file(
        session, fixture_audio / PATCH4_BARE_TO, settings=settings, audio_root=fixture_audio
    )
    call = session.get(Call, result.call_id)
    assert call.call_type == "patch"
    assert len(call.talkgroup_ids) == 4
    assert session.get(Patch, call.patch_id).patch_id == 1038


def test_a_patch_call_is_found_by_any_member(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """The GIN index on talkgroup_ids is what makes patched traffic visible to a member."""
    ingest_file(session, fixture_audio / PATCH3, settings=settings, audio_root=fixture_audio)
    found = session.execute(select(Call).where(Call.talkgroup_ids.overlap([1561]))).scalars().all()
    assert len(found) == 1


# --------------------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------------------


def test_identical_bytes_under_a_second_path_are_a_duplicate(
    session, settings, fixture_audio, tmp_path  # noqa: ANN001
) -> None:
    original = fixture_audio / PLAIN
    tree = tmp_path / "audio"
    (tree / "a").mkdir(parents=True)
    (tree / "b").mkdir(parents=True)
    shutil.copy2(original, tree / "a" / original.name)
    shutil.copy2(original, tree / "b" / original.name)
    local = settings.model_copy(update={"audio_root": tree})

    first = ingest_file(session, tree / "a" / original.name, settings=local, audio_root=tree)
    second = ingest_file(session, tree / "b" / original.name, settings=local, audio_root=tree)

    assert first.status == "ingested"
    assert second.status == "duplicate"
    assert second.duplicate_of == first.call_id
    # a duplicate is kept, but must not inflate the counters or be transcribed twice
    assert session.execute(select(Talkgroup.call_count)).scalar_one() == 1
    assert session.get(Call, second.call_id).transcript_status == "skipped"
    assert len(session.execute(select(TranscriptionJob)).scalars().all()) == 1


def _synthetic_call(session, record, *, started_at, duration) -> Call:  # noqa: ANN001, ANN202
    """A minimal call row standing in for one already in the archive."""
    from app.ingest.service import upsert_system, upsert_talkgroup

    system_id = upsert_system(session, record.system, protocol=record.decoder, wacn=None)
    tg = upsert_talkgroup(
        session, system_id, record.tgid, alias=record.talkgroup_alias, seen_at=started_at
    )
    call = Call(
        system_id=system_id,
        talkgroup_id=tg.id,
        talkgroup_ids=list(record.talkgroup_ids),
        recorded_at=started_at + timedelta(seconds=duration),
        started_at=started_at,
        started_local=started_at.replace(tzinfo=None),
        ended_at=started_at + timedelta(seconds=duration),
        duration_s=Decimal(str(duration)),
        file_path=f"synthetic/{started_at.timestamp()}-{duration}.mp3",
        file_name="synthetic.mp3",
        sha256="0" * 64,
    )
    session.add(call)
    session.flush()
    return call


def test_fuzzy_dedupe_window_and_tolerance(session, settings, fixture_audio) -> None:  # noqa: ANN001
    record = build_call_record(fixture_audio / LONGEST, fixture_audio, tz=settings.tz)

    # inside both the 1 s window and the 10 % duration tolerance
    near = _synthetic_call(
        session,
        record,
        started_at=record.started_at + timedelta(seconds=0.4),
        duration=round(record.duration_s * 1.05, 3),
    )
    assert find_duplicate(session, record, settings) == near.id


def test_fuzzy_dedupe_rejects_a_call_outside_the_window(session, settings, fixture_audio) -> None:  # noqa: ANN001
    record = build_call_record(fixture_audio / LONGEST, fixture_audio, tz=settings.tz)
    _synthetic_call(
        session,
        record,
        started_at=record.started_at + timedelta(seconds=5),
        duration=round(record.duration_s, 3),
    )
    assert find_duplicate(session, record, settings) is None


def test_fuzzy_dedupe_rejects_a_different_duration(session, settings, fixture_audio) -> None:  # noqa: ANN001
    record = build_call_record(fixture_audio / LONGEST, fixture_audio, tz=settings.tz)
    _synthetic_call(
        session,
        record,
        started_at=record.started_at,
        duration=round(record.duration_s * 0.5, 3),
    )
    assert find_duplicate(session, record, settings) is None


# --------------------------------------------------------------------------------------
# conversations
# --------------------------------------------------------------------------------------


def test_conversation_groups_calls_inside_the_gap(session, settings, fixture_audio) -> None:  # noqa: ANN001
    record = build_call_record(fixture_audio / PLAIN, fixture_audio, tz=settings.tz)
    base = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    a = _synthetic_call(session, record, started_at=base, duration=3.0)
    b = _synthetic_call(session, record, started_at=base + timedelta(seconds=10), duration=3.0)
    # 60 s after b ends, well past CONVERSATION_GAP_SECONDS=20
    c = _synthetic_call(session, record, started_at=base + timedelta(seconds=90), duration=3.0)

    for call in (a, b, c):
        assign_conversation(session, call, settings.conversation_gap_seconds)
    session.commit()

    assert a.conversation_id == b.conversation_id
    assert c.conversation_id != a.conversation_id

    first = session.get(Conversation, a.conversation_id)
    assert first.call_count == 2
    assert first.first_call_id == a.id
    assert first.started_at == a.started_at
    assert first.ended_at == b.ended_at


def test_ingest_assigns_a_conversation(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert call.conversation_id is not None
    conversation = session.get(Conversation, call.conversation_id)
    assert conversation.talkgroup_id == call.talkgroup_id
    assert conversation.call_count == 1


# --------------------------------------------------------------------------------------
# the transcription queue
# --------------------------------------------------------------------------------------


def test_long_enough_calls_are_queued(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / LONGEST, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert call.transcript_status == "queued"
    job = session.get(TranscriptionJob, result.job_id)
    assert job.call_id == call.id
    assert job.status == "queued"
    assert job.priority == 100  # the talkgroup default
    assert job.max_attempts == settings.transcribe_max_attempts


def test_squelch_tails_are_skipped(session, settings, fixture_audio) -> None:  # noqa: ANN001
    result = ingest_file(session, fixture_audio / SHORTEST, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert float(call.duration_s) < settings.transcribe_min_seconds
    assert call.transcript_status == "skipped"
    assert result.job_id is None
    assert session.execute(select(TranscriptionJob)).scalars().all() == []


def test_talkgroup_policy_turns_transcription_off(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """A talkgroup with transcribe=False is never queued, however long the call."""
    from app.ingest.service import upsert_system

    system_id = upsert_system(session, "Example System", protocol="P25 Phase 1", wacn=None)
    session.add(Talkgroup(system_id=system_id, tgid=1601, alias="EXAMPLE LAW 1", transcribe=False))
    session.commit()

    result = ingest_file(session, fixture_audio / LONGEST, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)
    assert float(call.duration_s) > 40  # far past the minimum
    assert call.transcript_status == "skipped"
    assert result.job_id is None
    assert session.execute(select(TranscriptionJob)).scalars().all() == []


def test_talkgroup_minimum_overrides_the_global_one(session, settings, fixture_audio) -> None:  # noqa: ANN001
    from app.ingest.service import upsert_system

    system_id = upsert_system(session, "Example System", protocol="P25 Phase 1", wacn=None)
    # 0.235 s is below the global 0.75 s but above this talkgroup's own floor
    session.add(
        Talkgroup(system_id=system_id, tgid=1701, alias="EXAMPLE EVENT 1", transcribe_min_seconds=0.1)
    )
    session.commit()

    result = ingest_file(session, fixture_audio / SHORTEST, settings=settings, audio_root=fixture_audio)
    assert session.get(Call, result.call_id).transcript_status == "queued"
    assert result.job_id is not None


# --------------------------------------------------------------------------------------
# the ledger and the batch scan
# --------------------------------------------------------------------------------------


def test_ledger_records_every_outcome(session, settings, fixture_audio, tmp_path) -> None:  # noqa: ANN001
    ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    ledger = session.execute(select(IngestFile).where(IngestFile.file_path == PLAIN)).scalar_one()
    assert ledger.status == "ingested"
    assert ledger.call_id is not None
    assert ledger.attempts == 1

    # a file that parses to nothing usable is filed as failed rather than dropped
    tree = tmp_path / "audio"
    tree.mkdir()
    broken = tree / "20260829_120000_Example-System_Central_T-Control__TO_1701_FROM_1.mp3"
    broken.write_bytes(b"not an mp3 at all")
    local = settings.model_copy(update={"audio_root": tree})
    result = ingest_file(session, broken, settings=local, audio_root=tree)
    assert result.status == "failed"
    failed = session.execute(
        select(IngestFile).where(IngestFile.file_path == broken.name)
    ).scalar_one()
    assert failed.status == "failed"
    assert failed.error
    assert failed.call_id is None


def test_retry_failed_reingests_a_repaired_file(
    session, settings, fixture_audio, tmp_path  # noqa: ANN001
) -> None:
    tree = tmp_path / "audio"
    tree.mkdir()
    name = Path(PLAIN).name
    target = tree / name
    target.write_bytes(b"truncated")
    local = settings.model_copy(update={"audio_root": tree})

    assert ingest_file(session, target, settings=local, audio_root=tree).status == "failed"
    shutil.copy2(fixture_audio / PLAIN, target)  # the recorder finished writing it

    from app.db import SessionLocal

    summary = retry_failed(SessionLocal, settings=local)
    assert summary.files_new == 1
    assert summary.files_failed == 0
    assert (
        session.execute(select(IngestFile.status).where(IngestFile.file_path == name)).scalar_one()
        == "ingested"
    )


def test_scan_a_tree_and_rerun_it(session, settings, fixture_audio) -> None:  # noqa: ANN001
    from app.db import SessionLocal

    first = ingest_tree(
        SessionLocal, fixture_audio, workers=2, settings=settings, audio_root=fixture_audio
    )
    assert first.files_seen == 13
    assert first.files_failed == 0
    assert first.files_new + first.files_duplicate == 13

    # every path is now in the ledger, so a rerun is all skips and costs one query
    second = ingest_tree(
        SessionLocal, fixture_audio, workers=2, settings=settings, audio_root=fixture_audio
    )
    assert second.files_seen == 13
    assert second.files_skipped == 13
    assert second.files_new == 0

    runs = session.execute(select(IngestRun).order_by(IngestRun.id)).scalars().all()
    assert len(runs) == 2
    assert runs[0].finished_at is not None
    assert runs[0].files_new == first.files_new

    status = ingest_status(session)
    assert status["calls"] == 13
    assert status["ledger"]["ingested"] >= 12
    assert status["last_run"]["files_skipped"] == 13


def test_call_new_is_notified(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """The live bus is Postgres LISTEN/NOTIFY (5.1 step 6); listen first, then ingest."""
    import psycopg

    from app.db import SessionLocal
    from tests.conftest import TEST_DATABASE_URL

    dsn = TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as listener:
        listener.execute("LISTEN keyup_events")

        worker = SessionLocal()
        try:
            ingest_file(worker, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
        finally:
            worker.close()

        payloads = [n.payload for n in listener.notifies(timeout=5, stop_after=1)]

    assert payloads, "no call.new notification arrived"
    event = json.loads(payloads[0])
    assert event["event"] == "call.new"
    assert event["talkgroup_ids"] == [1421]
    assert event["duplicate"] is False
    assert session.get(Call, event["id"]) is not None


# --------------------------------------------------------------------------------------


PLAYLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<playlist version="4" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <alias name="EXAMPLE EMS OPS RENAMED" list="Central" group="EMS" color="-16776961">
    <id xsi:type="talkgroup" value="1421" protocol="APCO25"/>
    <id xsi:type="radio" value="9831120" protocol="APCO25"/>
  </alias>
  <alias name="NEVER HEARD" list="Central" group="Law" color="-65536">
    <id xsi:type="talkgroup" value="99999" protocol="APCO25"/>
  </alias>
  <alias name="A DMR GROUP" list="Other" group="Other">
    <id xsi:type="talkgroup" value="1234" protocol="DMR"/>
  </alias>
</playlist>
"""


def test_playlist_import(session, settings, fixture_audio, tmp_path) -> None:  # noqa: ANN001
    from app.ingest.playlist_import import import_file

    ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    playlist = tmp_path / "default.xml"
    playlist.write_text(PLAYLIST_XML)

    summary = import_file(session, playlist)
    session.commit()

    assert summary.talkgroups_updated == 1  # 1421 already existed
    assert summary.talkgroups_created == 1  # 99999 had never been heard
    assert summary.skipped_non_apco25 == 1  # the DMR entry
    aliases = dict(session.execute(select(Talkgroup.tgid, Talkgroup.alias)).all())
    assert aliases[1421] == "EXAMPLE EMS OPS RENAMED"  # an import does replace the alias
    assert aliases[99999] == "NEVER HEARD"
    unheard = session.execute(select(Talkgroup).where(Talkgroup.tgid == 99999)).scalar_one()
    assert unheard.call_count == 0
    assert unheard.group_name == "Law"
    assert unheard.color == "#ff0000"
    # the radio that was heard picked up the playlist's name
    assert session.execute(select(Radio.alias)).scalar_one() == "EXAMPLE EMS OPS RENAMED"


BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE playlist [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
]>
<playlist version="4"><alias name="&d;"><id type="talkgroup" value="1" protocol="APCO25"/></alias></playlist>
"""


def test_a_playlist_may_not_define_entities(tmp_path) -> None:  # noqa: ANN001
    """M3: the importer parses an upload, so entity expansion is refused outright."""
    from defusedxml.common import EntitiesForbidden

    from app.ingest.playlist_import import parse_playlist

    playlist = tmp_path / "default.xml"
    playlist.write_text(BILLION_LAUGHS)
    with pytest.raises(EntitiesForbidden):
        parse_playlist(playlist)


def test_csv_import(session, settings, fixture_audio, tmp_path) -> None:  # noqa: ANN001
    from app.ingest.playlist_import import import_file

    ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    csv_path = tmp_path / "talkgroups.csv"
    csv_path.write_text(
        "tgid,alias,group,category,service_tag,agency_code,color\n"
        "1421,EXAMPLE EMS OPS,Central EMS,ems,ems dispatch,,#00ff00\n"
    )
    summary = import_file(session, csv_path)
    session.commit()
    assert summary.talkgroups_updated == 1
    tg = session.execute(select(Talkgroup).where(Talkgroup.tgid == 1421)).scalar_one()
    assert (tg.category, tg.group_name, tg.color) == ("ems", "Central EMS", "#00ff00")
    # Stored in RadioReference's spelling whatever the file wrote.
    assert tg.service_tag == "EMS Dispatch"


def test_csv_import_accepts_trunk_recorders_tag_column(session, settings, fixture_audio, tmp_path) -> None:  # noqa: ANN001
    """``tag`` is trunk-recorder's name for the same column, and a bad value costs only
    itself: the row still imports, and the summary says how many were dropped."""
    from app.ingest.playlist_import import import_file

    ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    csv_path = tmp_path / "trunk-recorder.csv"
    csv_path.write_text(
        "tgid,alias,group,tag\n"
        "1421,EXAMPLE EMS OPS,Central EMS,Fire Dispatch\n"
        "24822,EC EMS TAC,Central EMS,Weather Net\n"
    )
    summary = import_file(session, csv_path)
    session.commit()

    assert summary.skipped_service_tags == 1
    rows = {
        row.tgid: row.service_tag
        for row in session.scalars(select(Talkgroup).where(Talkgroup.tgid.in_([1421, 24822])))
    }
    assert rows[1421] == "Fire Dispatch"
    assert rows[24822] is None  # the nonsense tag was dropped, the row was not


def test_gap_detection(session, settings, fixture_audio) -> None:  # noqa: ANN001
    from app.ingest.gaps import detect_gaps
    from app.models import RecorderGap

    record = build_call_record(fixture_audio / PLAIN, fixture_audio, tz=settings.tz)
    base = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    _synthetic_call(session, record, started_at=base, duration=3.0)
    _synthetic_call(session, record, started_at=base + timedelta(minutes=5), duration=3.0)
    _synthetic_call(session, record, started_at=base + timedelta(minutes=200), duration=3.0)
    session.commit()

    summary = detect_gaps(session, settings=settings, gap_minutes=30)
    session.commit()
    assert summary.gaps_found == 1
    assert summary.gaps_new == 1

    gap = session.execute(select(RecorderGap)).scalar_one()
    assert gap.started_at == base + timedelta(minutes=5, seconds=3)
    assert float(gap.duration_s) == pytest.approx(195 * 60 - 3, abs=1)

    # running it again refreshes rather than duplicates
    again = detect_gaps(session, settings=settings, gap_minutes=30)
    session.commit()
    assert again.gaps_found == 1
    assert again.gaps_new == 0
    assert len(session.execute(select(RecorderGap)).scalars().all()) == 1


def test_two_workers_racing_one_recording_write_one_call(session, settings, fixture_audio) -> None:
    """M4: the duplicate check and the insert are one critical section, per talkgroup-second."""
    import threading

    from app.db import SessionLocal

    source = fixture_audio / PLAIN
    results: list[object] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        own = SessionLocal()
        try:
            barrier.wait(timeout=10)
            results.append(ingest_file(own, source, settings=settings, audio_root=fixture_audio))
        finally:
            own.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    session.expire_all()
    calls = session.execute(select(Call)).scalars().all()
    assert len(calls) == 1, [r.status for r in results]
    assert sorted(r.status for r in results) == ["ingested", "skipped"]
