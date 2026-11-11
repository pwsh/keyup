"""Whole-archive parse test.

Runs only where the recordings are (the development host); it is the guard that keeps a
parser change from quietly failing on one of the 8,027 real files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.parser import build_call_record
from tests.conftest import requires_corpus

#: the size of the development host's corpus when this was written
EXPECTED_FILES = 8027


@requires_corpus
def test_every_recording_in_the_archive_parses(corpus_root: Path) -> None:
    files = sorted(corpus_root.rglob("*.mp3"))
    assert len(files) >= EXPECTED_FILES, f"expected at least {EXPECTED_FILES} recordings"

    failures: list[tuple[str, str]] = []
    patches = 0
    unresolved_radios = 0
    talkgroups: set[int] = set()
    systems: set[str] = set()

    for path in files:
        try:
            record = build_call_record(path, corpus_root)
        except Exception as exc:  # noqa: BLE001 - the whole point is to collect them
            failures.append((str(path.relative_to(corpus_root)), f"{type(exc).__name__}: {exc}"))
            continue

        systems.add(record.system)
        assert record.talkgroup_ids, path
        assert record.talkgroup_alias, path
        assert record.duration_s > 0, path
        assert record.ended_at == record.recorded_at
        assert record.started_at < record.ended_at
        assert record.started_local.tzinfo is None
        if record.call_type == "patch":
            patches += 1
            assert record.member_tgids, path
            # every member of a patch is named in TIT2
            assert set(record.member_aliases) == set(record.member_tgids), path
        if not record.radio_resolved:
            unresolved_radios += 1
        talkgroups.update(record.talkgroup_ids)

    assert failures == [], f"{len(failures)} recordings failed to parse: {failures[:5]}"
    assert len(systems) == 1, f"one recorder, one system: {sorted(systems)}"
    assert patches > 0
    assert unresolved_radios == 51  # 0.6 % of the corpus has no FROM
    assert len(talkgroups) >= 50


@requires_corpus
def test_alias_folders_are_informational_only(corpus_root: Path) -> None:
    """Two folders hold more than one talkgroup, which is why the parser never trusts them."""
    sample = next(iter(sorted(corpus_root.rglob("*.mp3"))))
    record = build_call_record(sample, corpus_root)
    assert record.folder_alias == sample.parent.name
    # the identity came from ID3, not from the folder
    assert record.id3.tgid is not None or record.id3.patch_id is not None


@requires_corpus
@pytest.mark.parametrize("suffix", ["_V2"])
def test_collision_suffixes_are_understood(corpus_root: Path, suffix: str) -> None:
    hits = [p for p in corpus_root.rglob(f"*{suffix}.mp3")]
    assert hits, f"no {suffix} recordings in the archive"
    for path in hits:
        record = build_call_record(path, corpus_root)
        assert record.filename_parts.version == int(suffix[2:])
        assert record.radio is not None  # the suffix must not be eaten by the radio id
