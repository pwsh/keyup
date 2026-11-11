"""Parser unit tests against a dozen synthetic recordings and the strings sdrtrunk writes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest.parser import (
    ParseError,
    build_call_record,
    parse_comm,
    parse_filename,
    parse_id3,
    parse_tit2,
    parse_tpe1,
    probe_audio,
)

PLAIN = "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3"
LOCAL_RADIO = "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
NO_FROM = "20260829/EXAMPLE EVENT 1/20260829_030253_Example-System_Central_T-Control__TO_1701.mp3"
V2 = "20260829/EXAMPLE MED 2/20260829_135155_Example-System_Central_T-Control__TO_1634_FROM_9200114_V2.mp3"
V2_SIBLING = "20260829/EXAMPLE MED 2/20260829_135155_Example-System_Central_T-Control__TO_1634_FROM_9200114.mp3"
PATCH2 = "20260829/PATCH 1899/20260829_193221_Example-System_Central_T-Control__TO_P1899-[1561--1310]_FROM_101010_7_900004.mp3"
PATCH3 = "20260829/PATCH 1157/20260829_165914_Example-System_Central_T-Control__TO_P1157-[1812--1561--1811]_FROM_101010_57_9700005.mp3"
PATCH4_BARE_TO = "20260829/PATCH 1038/20260829_085845_Example-System_Central_T-Control__TO_1038_FROM_101010_92_9299201.mp3"
PATCH_NO_NAME = "20260828/EXAMPLE INTEROP 1/20260828_232527_Example-System_Central_T-Control__TO_P1097-[1561--1612]_FROM_9200006.mp3"
SHORTEST = "20260829/EXAMPLE EVENT 1/20260829_141703_Example-System_Central_T-Control__TO_1701_FROM_9200006.mp3"
LONGEST = "20260829/EXAMPLE LAW 1/20260829_200945_Example-System_Central_T-Control__TO_1601_FROM_9200102.mp3"
#: pre-2025 sdrtrunk: no underscore after the time, and the system's space is an
#: underscore rather than a dash.  The shape of a large real backlog — this
#: exact shape was a million rows of it (docs/MIGRATION.md, "Timestamps").
LEGACY = "20240223_210414Example_System_Central_T-Control__TO_1102_FROM_5226020.mp3"


# --------------------------------------------------------------------------------------
# filenames — pure strings, no files needed
# --------------------------------------------------------------------------------------


def test_plain_filename() -> None:
    parts = parse_filename(Path(PLAIN).name)
    assert parts.stamp == datetime(2026, 8, 28, 5, 52, 19)
    assert (parts.system, parts.site, parts.channel_name) == ("Example-System", "Central", "T-Control")
    assert parts.tgid == 1421
    assert parts.patch_id is None
    assert parts.radio is not None
    assert (parts.radio.wacn, parts.radio.home_system, parts.radio.radio_id) == (101010, 57, 9831120)
    assert parts.radio.is_fully_qualified
    assert parts.version is None


def test_bare_radio_is_not_fully_qualified() -> None:
    parts = parse_filename(Path(LOCAL_RADIO).name)
    assert parts.radio is not None
    assert parts.radio.radio_id == 9250041
    assert (parts.radio.wacn, parts.radio.home_system) == (0, 0)
    assert not parts.radio.is_fully_qualified


def test_missing_from() -> None:
    parts = parse_filename(Path(NO_FROM).name)
    assert parts.from_raw is None
    assert parts.radio is None
    assert parts.tgid == 1701


def test_v2_suffix_is_stripped_before_the_radio_is_read() -> None:
    parts = parse_filename(Path(V2).name)
    assert parts.version == 2
    assert parts.radio is not None
    assert parts.radio.radio_id == 9200114
    assert parts.radio.raw == "9200114"


def test_patch_to_field() -> None:
    parts = parse_filename(Path(PATCH3).name)
    assert parts.patch_id == 1157
    assert parts.member_tgids == (1812, 1561, 1811)
    assert parts.tgid is None
    assert parts.is_patch


def test_patch_written_as_a_bare_id_in_the_filename() -> None:
    # The filename says 1038, which looks like an ordinary talkgroup; only TIT2 knows it
    # is a patch.  This is why ID3 is authoritative.
    parts = parse_filename(Path(PATCH4_BARE_TO).name)
    assert parts.tgid == 1038
    assert parts.patch_id is None


def test_pre_2025_name_without_the_underscore_after_the_time() -> None:
    """The regression the backlog was mis-stamped by.

    Every field has to come out of it, not merely the stamp: the parse is what the push
    path falls back on when the recorder sends no ``dateTime``, and a name that raises
    leaves the call with no time at all.
    """
    parts = parse_filename(LEGACY)
    assert parts.stamp == datetime(2024, 2, 23, 21, 4, 14)
    assert (parts.system, parts.site, parts.channel_name) == ("Example-System", "Central", "T-Control")
    assert parts.tgid == 1102
    assert parts.radio is not None
    assert parts.radio.radio_id == 5226020
    assert not parts.radio.is_fully_qualified


def test_the_legacy_system_is_not_read_as_a_system_of_its_own() -> None:
    """``Example_System`` is one system with a space in it, not a system called ``Example``.

    Splitting it the modern way would have ``upsert_system`` mint a second system beside
    the real one — the same damage the phantom ``upload`` system did.
    """
    parts = parse_filename(LEGACY)
    assert parts.system is not None
    assert parts.system.replace("-", " ") == "Example System"


def test_modern_names_are_unaffected_by_the_optional_underscore() -> None:
    for name in (PLAIN, LOCAL_RADIO, NO_FROM, V2, PATCH2, PATCH3, PATCH4_BARE_TO, LONGEST):
        parts = parse_filename(Path(name).name)
        assert parts.system == "Example-System"
        assert (parts.site, parts.channel_name) == ("Central", "T-Control")
        assert parts.stamp is not None


def test_a_legacy_name_with_a_single_word_system_still_splits_by_position() -> None:
    parts = parse_filename("20240223_210414Metro_Central_T-Control__TO_1102.mp3")
    assert (parts.system, parts.site, parts.channel_name) == ("Metro", "Central", "T-Control")


@pytest.mark.parametrize(
    "name",
    [
        "not-a-recording.mp3",
        "20260828_055219_Example-System_Central_T-Control.mp3",
        "readme.txt",
    ],
)
def test_unparseable_names_raise(name: str) -> None:
    with pytest.raises(ParseError):
        parse_filename(name)


# --------------------------------------------------------------------------------------
# ID3 strings
# --------------------------------------------------------------------------------------


def test_parse_comm_keeps_colons_inside_values() -> None:
    comm = parse_comm(
        "Date:2026-08-28 05:52:19.894;System:Example System;Site:Central;Name:T-Control;"
        "Decoder:P25 Phase 1;Channel:1-90;Frequency:852137500;"
    )
    assert comm["Date"] == "2026-08-28 05:52:19.894"
    assert comm["System"] == "Example System"
    assert comm["Frequency"] == "852137500"


def test_parse_tit2_plain() -> None:
    tgid, patch, members, aliases = parse_tit2('1421"EXAMPLE EMS OPS"')
    assert (tgid, patch, members, aliases) == (1421, None, (), ("EXAMPLE EMS OPS",))


def test_parse_tit2_patch() -> None:
    tgid, patch, members, aliases = parse_tit2(
        'P:9230 [1282, 1551, 1561, 1501]"PATCH 9230","EXAMPLE LAW 5 PATCH",'
        '"EXAMPLE INTEROP 2","EXAMPLE INTEROP 1","EXAMPLE LAW 5"'
    )
    assert tgid is None
    assert patch == 9230
    assert members == (1282, 1551, 1561, 1501)
    assert aliases[0] == "PATCH 9230"
    assert len(aliases) == len(members) + 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("101010.57.9831120", (9831120, 101010, 57)),
        ("5250057", (5250057, 0, 0)),
    ],
)
def test_parse_tpe1(raw: str, expected: tuple[int, int, int]) -> None:
    radio = parse_tpe1(raw)
    assert radio is not None
    assert (radio.radio_id, radio.wacn, radio.home_system) == expected


# --------------------------------------------------------------------------------------
# the fixture recordings
# --------------------------------------------------------------------------------------


def test_id3_of_a_plain_call(fixture_audio: Path) -> None:
    meta = parse_id3(fixture_audio / PLAIN)
    assert meta.date == datetime(2026, 8, 28, 5, 52, 19, 894000)
    assert meta.system == "Example System"
    assert meta.site == "Central"
    assert meta.channel_name == "T-Control"
    assert meta.decoder == "P25 Phase 1"
    assert meta.channel == "1-90"
    assert meta.frequency_hz == 852137500
    assert meta.tgid == 1421
    assert meta.talkgroup_alias == "EXAMPLE EMS OPS"
    assert meta.recorder_version.startswith("sdrtrunk nightly")


def test_probe_matches_the_corpus_encoding(fixture_audio: Path) -> None:
    probe = probe_audio(fixture_audio / PLAIN)
    assert (probe.bitrate, probe.sample_rate, probe.channels) == (32000, 22050, 1)
    assert probe.duration_s == pytest.approx(2.377, abs=0.01)
    assert len(probe.sha256) == 64


def test_time_is_the_file_write_instant(fixture_audio: Path) -> None:
    """docs/ARCHITECTURE.md 1.2: the stamp is when the file was written, ~ the call end."""
    record = build_call_record(fixture_audio / PLAIN, fixture_audio, tz="America/Winnipeg")
    assert record.recorded_at == datetime(2026, 8, 28, 10, 52, 19, 894000, tzinfo=UTC)  # CDT = UTC-5
    assert record.ended_at == record.recorded_at
    assert record.started_at == record.recorded_at - __import__("datetime").timedelta(
        seconds=record.duration_s
    )
    assert record.time_source == "file_write"
    # naive local time (here America/Winnipeg), the shape CAD systems store
    assert record.started_local.tzinfo is None
    assert record.started_local.hour == 5


def test_folder_alias_is_recorded_but_not_relied_on(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / PATCH2, fixture_audio)
    assert record.folder_alias == "PATCH 1899"
    # the talkgroup identity comes from TIT2, not from the folder
    assert record.patch_id == 1899
    assert record.file_path == PATCH2


def test_patch_with_two_members(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / PATCH2, fixture_audio)
    assert record.call_type == "patch"
    assert record.member_tgids == (1561, 1310)
    assert record.talkgroup_ids == (1561, 1310)
    assert record.tgid == 1561  # the primary talkgroup of a patch is its first member
    assert record.talkgroup_alias == "PATCH 1899"
    assert record.member_aliases == {1561: "EXAMPLE INTEROP 1", 1310: "EXAMPLE SHERIFF"}


def test_patch_with_three_members(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / PATCH3, fixture_audio)
    assert record.member_tgids == (1812, 1561, 1811)
    assert record.member_aliases == {
        1812: "EXAMPLE LAW 3",
        1561: "EXAMPLE INTEROP 1",
        1811: "EXAMPLE LAW 4",
    }


def test_patch_recognized_from_id3_when_the_filename_hides_it(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / PATCH4_BARE_TO, fixture_audio)
    assert record.call_type == "patch"
    assert record.patch_id == 1038
    assert len(record.member_tgids) == 4
    assert record.filename_parts.patch_id is None  # the filename said "1038"


def test_patch_whose_tit2_prints_no_patch_name(fixture_audio: Path) -> None:
    """The P1409x family lists one alias per member and no patch name of its own."""
    record = build_call_record(fixture_audio / PATCH_NO_NAME, fixture_audio)
    assert record.id3.patch_alias is None
    assert record.member_aliases == {1561: "EXAMPLE INTEROP 1", 1612: "EXAMPLE LAW 2"}
    assert record.talkgroup_alias == "EXAMPLE INTEROP 1"


def test_unresolved_radio(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / NO_FROM, fixture_audio)
    assert record.radio is None
    assert record.radio_resolved is False


def test_fully_qualified_radio(fixture_audio: Path) -> None:
    record = build_call_record(fixture_audio / PLAIN, fixture_audio)
    assert record.radio is not None
    assert record.radio.raw == "101010.57.9831120"  # ID3 form, not the filename's
    assert record.radio_resolved is True


def test_shortest_and_longest(fixture_audio: Path) -> None:
    shortest = build_call_record(fixture_audio / SHORTEST, fixture_audio)
    longest = build_call_record(fixture_audio / LONGEST, fixture_audio)
    assert shortest.duration_s < 0.75  # below TRANSCRIBE_MIN_SECONDS: a squelch tail
    assert longest.duration_s == pytest.approx(44.513, abs=0.01)


def test_v2_pair_collides_on_the_write_second_not_on_the_call(fixture_audio: Path) -> None:
    """``_V2`` marks a *filename* collision, which is not the same as a duplicate call.

    Both files were written in the same second — that is what made sdrtrunk rename the
    second one — but the write instant is the call's *end*, so two calls that finished
    together started 16 seconds apart and are plainly different transmissions (different
    channels, different frequencies, different audio).  Dedupe compares ``started_at``
    and duration precisely so this pair survives as two calls.
    """
    a = build_call_record(fixture_audio / V2_SIBLING, fixture_audio)
    b = build_call_record(fixture_audio / V2, fixture_audio)
    assert a.tgid == b.tgid == 1634
    assert b.filename_parts.version == 2
    assert a.filename_parts.stamp == b.filename_parts.stamp
    # same write second...
    assert abs((a.recorded_at - b.recorded_at).total_seconds()) < 1.0
    # ...but a 16 s call and a 0.8 s call, so different starts and different bytes
    assert abs((a.started_at - b.started_at).total_seconds()) > 10
    assert a.duration_s > 10 > b.duration_s
    assert a.sha256 != b.sha256
    assert a.channel != b.channel


def test_every_fixture_parses(fixture_audio: Path) -> None:
    files = sorted(fixture_audio.rglob("*.mp3"))
    assert len(files) >= 12
    for path in files:
        record = build_call_record(path, fixture_audio)
        assert record.system == "Example System"
        assert record.talkgroup_ids
        assert record.duration_s > 0
        assert record.started_at < record.ended_at
