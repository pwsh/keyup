"""The storage layout: partition by the call's own date, and the re-file that gets there.

Three things are under test, and they are the same rule seen from three sides: the upload
endpoint files a new recording under the day it was *recorded*, ``keyup refile-audio`` moves
the archive's existing million onto that rule, and the ingest walk no longer enumerates the
subtree either of them writes into.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mutagen.mp3 import MP3
from sqlalchemy import select
from typer.testing import CliRunner

from app.api import call_upload as upload_module
from app.api.call_upload import router
from app.api.uploads import body_size_guard
from app.audio import prune_empty_dirs, upload_partition
from app.config import Settings
from app.db import get_session
from app.ingest.service import UnknownSystem, walk_recordings
from app.models import ApiToken, Call, IngestFile
from app.refile_audio import refile_audio, target_for
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

TAGGED = "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
KEY = "test-upload-key"
OK = "Call imported successfully."
#: a pre-2025 sdrtrunk name — no underscore after the time — whose call is from 2024
LEGACY_NAME = "20240223_210414Example_System_Central_T-Control__TO_1102_FROM_5226020.mp3"


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def upload_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
    )


@pytest.fixture
def flat_settings(upload_settings):  # noqa: ANN001, ANN201
    """The same archive with ``AUDIO_BUCKET_UPLOADS=false``."""
    return upload_settings.model_copy(update={"audio_bucket_uploads": False})


def _client(session, settings, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setattr(upload_module, "get_settings", lambda: settings)
    app = FastAPI()
    app.middleware("http")(body_size_guard)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture
def client(session, upload_settings, monkeypatch):  # noqa: ANN001, ANN201
    with _client(session, upload_settings, monkeypatch) as test_client:
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
    """The same audio with every ID3 frame gone, so only the filename says when it was."""
    target = tmp_path_factory.mktemp("stripped") / "raw.mp3"
    target.write_bytes(tagged_mp3.read_bytes())
    audio = MP3(str(target))
    audio.delete()
    audio.save()
    return target


def post(client, mp3: Path, **fields):  # noqa: ANN001, ANN003
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())),
        "talkgroup": "1701",
        "source": "9200006",
        "systemLabel": "Example System",
    }
    form.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": (fields.get("audioName") or "call.mp3", mp3.read_bytes(), "audio/mpeg")},
    )


def incoming(root: Path) -> list[Path]:
    """Everything left under ``uploads/.incoming`` — always empty once a request is over."""
    staging = root / "uploads" / ".incoming"
    return [p for p in staging.rglob("*") if p.is_file()] if staging.is_dir() else []


# --------------------------------------------------------------------------------------
# a new upload goes where its own date says
# --------------------------------------------------------------------------------------


def test_a_live_push_lands_in_the_partition_its_datetime_names(client, session, token, tagged_mp3) -> None:  # noqa: ANN001
    """19:17 UTC is 14:17 CDT, so the call's day is the 29th in the archive's zone."""
    assert post(client, tagged_mp3).text == OK
    call = session.execute(select(Call)).scalar_one()
    assert call.file_path.startswith(f"uploads/2026/08/29/{call.sha256[:2]}/")


def test_a_legacy_backlog_push_lands_in_2024_not_today(  # noqa: ANN001
    client, session, token, stripped_mp3, upload_settings
) -> None:
    """The whole point.  No ``dateTime``, a pre-2025 name, and the file goes to 2024.

    This is the shape that put nearly a million files in one directory: the old ``upload_path`` took
    its date from the form *before* the recording was parsed, so a 2024 call filed itself
    under the afternoon it was pushed.
    """
    response = post(
        client,
        stripped_mp3,
        audioName=LEGACY_NAME,
        dateTime=None,
        talkgroup="1102",
        priority="backlog",
    )
    assert response.text == OK

    call = session.execute(select(Call)).scalar_one()
    assert call.started_at.astimezone(UTC).year == 2024
    assert call.file_path == f"uploads/2024/02/23/{call.sha256[:2]}/{LEGACY_NAME}"
    assert (Path(upload_settings.audio_root) / call.file_path).is_file()
    # and the ledger names where the bytes actually are
    assert session.execute(select(IngestFile.file_path)).scalar_one() == call.file_path


def test_the_bucket_is_the_content_hash_and_can_be_turned_off(  # noqa: ANN001
    session, token, tagged_mp3, flat_settings, monkeypatch
) -> None:
    with _client(session, flat_settings, monkeypatch) as client:
        assert post(client, tagged_mp3).text == OK
    call = session.execute(select(Call)).scalar_one()
    assert call.file_path == "uploads/2026/08/29/call.mp3"

    # the same call, bucketed, would have been one level deeper and named by its own bytes
    bucketed = flat_settings.model_copy(update={"audio_bucket_uploads": True})
    assert upload_partition(call.started_at, bucketed, sha256=call.sha256) == (
        f"uploads/2026/08/29/{call.sha256[:2]}"
    )


def test_two_recordings_sharing_a_name_do_not_overwrite_each_other(  # noqa: ANN001
    session, token, tagged_mp3, stripped_mp3, flat_settings, monkeypatch
) -> None:
    """Two different calls, one generated name, one partition: two files, two rows.

    Unbucketed, because that is where a collision is reachable — with the bucket on, two
    recordings only share a directory when they share a content hash, and then they are the
    same bytes.  The ``_1`` walk is the same code either way.
    """
    later = str(int(datetime(2026, 8, 29, 19, 25, 40, tzinfo=UTC).timestamp()))
    with _client(session, flat_settings, monkeypatch) as client:
        assert post(client, tagged_mp3, talkgroup="1701").text == OK
        assert post(client, stripped_mp3, talkgroup="1104", dateTime=later).text == OK

    root = Path(flat_settings.audio_root)
    paths = set(session.execute(select(Call.file_path)).scalars())
    assert paths == {"uploads/2026/08/29/call.mp3", "uploads/2026/08/29/call_1.mp3"}
    assert (root / "uploads/2026/08/29/call.mp3").read_bytes() == tagged_mp3.read_bytes()
    assert (root / "uploads/2026/08/29/call_1.mp3").read_bytes() == stripped_mp3.read_bytes()


# --------------------------------------------------------------------------------------
# the two-step write
# --------------------------------------------------------------------------------------


def test_a_stored_upload_leaves_nothing_in_the_staging_directory(  # noqa: ANN001
    client, session, token, tagged_mp3, upload_settings
) -> None:
    assert post(client, tagged_mp3).text == OK
    assert incoming(Path(upload_settings.audio_root)) == []


def test_a_failed_parse_leaves_nothing_behind_at_all(  # noqa: ANN001
    client, session, token, tagged_mp3, upload_settings, monkeypatch
) -> None:
    """The body is refused *before* the rename, so no partition is created for it either."""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise ValueError("unreadable")

    monkeypatch.setattr(upload_module, "build_upload_record", boom)
    assert post(client, tagged_mp3).status_code == 400

    root = Path(upload_settings.audio_root)
    assert incoming(root) == []
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_a_failure_after_the_rename_takes_the_partition_with_it(  # noqa: ANN001
    client, session, token, tagged_mp3, upload_settings, monkeypatch
) -> None:
    """A refusal raised past the rename unlinks the file and prunes the empty day."""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise UnknownSystem("no system")

    monkeypatch.setattr(upload_module, "reconcile_lookup", boom)
    assert post(client, tagged_mp3).status_code == 400

    root = Path(upload_settings.audio_root)
    assert [p for p in root.rglob("*") if p.is_file()] == []
    assert not (root / "uploads" / "2026").exists()


# --------------------------------------------------------------------------------------
# prune_empty_dirs
# --------------------------------------------------------------------------------------


def test_prune_stops_at_a_directory_that_still_holds_something(tmp_path, upload_settings) -> None:  # noqa: ANN001
    keep = tmp_path / "uploads" / "2024" / "02" / "23" / "ab"
    sibling = tmp_path / "uploads" / "2024" / "02" / "23" / "cd"
    keep.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (sibling / "other.mp3").write_bytes(b"x")

    assert prune_empty_dirs(keep, upload_settings) == 1
    assert not keep.exists()
    assert sibling.exists()


def test_prune_never_removes_the_root(tmp_path, upload_settings) -> None:  # noqa: ANN001
    empty = tmp_path / "uploads"
    empty.mkdir()
    prune_empty_dirs(empty, upload_settings)
    assert tmp_path.is_dir()


# --------------------------------------------------------------------------------------
# the enumerator
# --------------------------------------------------------------------------------------


def _tree(root: Path, relatives: list[str]) -> None:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00")


def test_the_walk_does_not_descend_into_the_uploads_tree(tmp_path) -> None:
    """It must not merely *filter out* the pushed files — it must never look at them."""
    _tree(
        tmp_path,
        [
            "20260828/EXAMPLE EMS OPS/one.mp3",
            "uploads/2024/02/23/ab/pushed.mp3",
            "uploads/2024/02/23/cd/pushed2.mp3",
            "uploads/.incoming/deadbeef/half-written.mp3",
        ],
    )

    found, cost = walk_recordings(tmp_path)

    assert [p.name for p in found] == ["one.mp3"]
    # the root and the one day directory below it, and nothing under uploads/
    assert cost.dirs == 3
    assert cost.pruned == 1
    assert cost.entries < 6  # noqa: PLR2004 - four entries: the day dir, uploads, and their children


def test_an_explicit_scan_of_uploads_still_skips_the_staging_directory(tmp_path) -> None:
    """An operator may point a scan straight at ``uploads/``; ``.incoming`` is never a call."""
    _tree(
        tmp_path,
        [
            "2024/02/23/ab/pushed.mp3",
            ".incoming/deadbeef/half-written.mp3",
        ],
    )
    found, cost = walk_recordings(tmp_path)
    assert [p.name for p in found] == ["pushed.mp3"]
    assert cost.pruned == 1


def test_the_walk_skips_keyups_own_processed_copies(tmp_path) -> None:
    """``<stem>.proc.mp3`` sits beside the original in any tree audio processing touched.

    Ingested, it became a second call per processed recording — different bytes, so only
    dedupe stood in the way.  Its ``.proc-*.mp3`` temp neither.
    """
    from app.ingest.parser import is_recording_name

    _tree(
        tmp_path,
        [
            "20260828/EXAMPLE EMS OPS/one.mp3",
            "20260828/EXAMPLE EMS OPS/one.proc.mp3",
            "20260828/EXAMPLE EMS OPS/.proc-k2j3h4.mp3",
            "20260828/EXAMPLE EMS OPS/two.PROC.MP3",
        ],
    )
    found, _cost = walk_recordings(tmp_path)

    assert [p.name for p in found] == ["one.mp3"]
    assert is_recording_name("one.mp3")
    assert not is_recording_name("one.proc.mp3")
    assert not is_recording_name(".proc-k2j3h4.mp3")
    assert not is_recording_name("x.raw.wav")


def test_the_walk_can_be_capped_and_says_so(tmp_path, caplog) -> None:  # noqa: ANN001
    _tree(tmp_path, [f"20260828/call-{n}.mp3" for n in range(6)])
    found, cost = walk_recordings(tmp_path, max_files=2)
    assert len(found) == 2  # noqa: PLR2004
    assert cost.truncated is True


# --------------------------------------------------------------------------------------
# keyup refile-audio
# --------------------------------------------------------------------------------------


def _system(session) -> int:  # noqa: ANN001
    from app.ingest.service import upsert_system

    return upsert_system(session, "Example System", protocol="P25 Phase 1", wacn=101010)


def _stored(  # noqa: PLR0913
    session,  # noqa: ANN001
    root: Path,
    *,
    system_id: int,
    name: str,
    started_at: datetime,
    path: str,
    body: bytes = b"audio-bytes",
    write: bool = True,
    audio_state: str = "present",
) -> Call:
    """One row as the migration left it: the right time, the wrong directory."""
    from app.ingest.service import upsert_talkgroup

    talkgroup = upsert_talkgroup(session, system_id, 1701, alias=None, seen_at=started_at)
    if write:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
    call = Call(
        system_id=system_id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[1701],
        ingest_source="upload",
        ingest_priority="backlog",
        recorded_at=started_at + timedelta(seconds=3),
        started_at=started_at,
        started_local=started_at.replace(tzinfo=None),
        ended_at=started_at + timedelta(seconds=3),
        time_source="file_write",
        duration_s=Decimal("3.000"),
        audio_state=audio_state,
        file_path=path,
        file_name=name,
        file_size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    session.add(call)
    session.flush()
    return call


@pytest.fixture
def archive(session, upload_settings):  # noqa: ANN001, ANN201
    """Four calls in the flat upload-day directory, each recorded on a different day."""
    root = Path(upload_settings.audio_root)
    system_id = _system(session)
    calls = [
        _stored(
            session,
            root,
            system_id=system_id,
            name=f"call-{n}.mp3",
            started_at=datetime(2024, 2, 23, 21, 4, 14, tzinfo=UTC) + timedelta(days=n),
            path=f"uploads/2026/09/01/call-{n}.mp3",
            body=f"audio-{n}".encode(),
        )
        for n in range(4)
    ]
    session.commit()
    return calls


def test_a_dry_run_writes_nothing(session, upload_settings, archive) -> None:  # noqa: ANN001
    root = Path(upload_settings.audio_root)
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    report = refile_audio(session, upload_settings, dry_run=True)

    assert report["dry_run"] is True
    assert report["moved"] == len(archive)
    assert report["unchanged"] == 0
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before
    session.expire_all()
    assert all(c.file_path.startswith("uploads/2026/09/01/") for c in archive)


def test_apply_moves_the_file_keeps_the_inode_and_rewrites_the_row(  # noqa: ANN001
    session, upload_settings, archive
) -> None:
    root = Path(upload_settings.audio_root)
    inodes = {c.id: (root / c.file_path).stat().st_ino for c in archive}

    report = refile_audio(session, upload_settings, dry_run=False)

    assert (report["moved"], report["failed"], report["missing"]) == (len(archive), 0, 0)
    session.expire_all()
    for call in archive:
        expected = target_for(call, upload_settings)
        assert call.file_path == expected
        assert call.file_path.startswith("uploads/2024/02/")
        moved = root / call.file_path
        assert moved.is_file()
        # a rename does not touch the inode; equal inodes are proof it is the same file
        assert moved.stat().st_ino == inodes[call.id]
    # the emptied upload-day directory went with them
    assert not (root / "uploads" / "2026").exists()


def test_a_second_run_finds_nothing_left_to_do(session, upload_settings, archive) -> None:  # noqa: ANN001
    refile_audio(session, upload_settings, dry_run=False)
    again = refile_audio(session, upload_settings, dry_run=False)
    # every row is still walked — they are still under ``uploads/`` — and every one of them
    # now agrees with its own date, so nothing on disk is touched a second time
    assert (again["moved"], again["repaired"], again["unchanged"]) == (0, 0, len(archive))
    # ...and the rows are still where the first run put them
    session.expire_all()
    assert all(c.file_path.startswith("uploads/2024/02/") for c in archive)


def test_an_interrupted_run_is_finished_by_the_next_one(  # noqa: ANN001
    session, upload_settings, archive
) -> None:
    """The file made it across, the commit did not.  The next pass repairs the row."""
    root = Path(upload_settings.audio_root)
    call = archive[0]
    target = target_for(call, upload_settings)
    (root / target).parent.mkdir(parents=True, exist_ok=True)
    os.rename(root / call.file_path, root / target)

    report = refile_audio(session, upload_settings, dry_run=False)

    assert report["repaired"] == 1
    assert report["missing"] == 0
    session.expire_all()
    assert call.file_path == target


def test_a_row_whose_file_is_gone_is_reported_not_fatal(  # noqa: ANN001
    session, upload_settings, archive
) -> None:
    root = Path(upload_settings.audio_root)
    (root / archive[1].file_path).unlink()

    report = refile_audio(session, upload_settings, dry_run=False)

    assert report["missing"] == 1
    assert report["moved"] == len(archive) - 1
    assert report["failed"] == 0
    session.expire_all()
    # the row is left exactly as it was — nothing invents a path for bytes that are gone
    assert archive[1].file_path == "uploads/2026/09/01/call-1.mp3"


def test_only_pushed_present_rows_are_touched(session, upload_settings, archive) -> None:  # noqa: ANN001
    """A watched-tree recording and a purged row keep the paths they have."""
    root = Path(upload_settings.audio_root)
    system_id = _system(session)
    watched = _stored(
        session,
        root,
        system_id=system_id,
        name="watched.mp3",
        started_at=datetime(2024, 3, 1, tzinfo=UTC),
        path="20240301/EXAMPLE EMS OPS/watched.mp3",
    )
    purged = _stored(
        session,
        root,
        system_id=system_id,
        name="gone.mp3",
        started_at=datetime(2024, 3, 2, tzinfo=UTC),
        path="uploads/2026/09/01/gone.mp3",
        write=False,
        audio_state="purged",
    )
    session.commit()

    refile_audio(session, upload_settings, dry_run=False)

    session.expire_all()
    assert watched.file_path == "20240301/EXAMPLE EMS OPS/watched.mp3"
    assert purged.file_path == "uploads/2026/09/01/gone.mp3"


def test_the_source_prefix_narrows_the_walk(session, upload_settings, archive) -> None:  # noqa: ANN001
    report = refile_audio(
        session, upload_settings, dry_run=True, source_prefix="uploads/2026/08/31"
    )
    assert (report["scanned"], report["moved"]) == (0, 0)


def test_the_cli_defaults_to_a_dry_run(session, upload_settings, archive, monkeypatch) -> None:  # noqa: ANN001
    from app import cli

    monkeypatch.setattr(cli, "get_settings", lambda: upload_settings)
    monkeypatch.setattr(cli, "_session", lambda: session)
    # the command closes the session it opened; this one belongs to the fixture
    monkeypatch.setattr(session, "close", lambda: None)

    result = CliRunner().invoke(cli.app, ["refile-audio"])

    assert result.exit_code == 0, result.output
    assert '"dry_run": true' in result.stdout
    session.expire_all()
    assert all(c.file_path.startswith("uploads/2026/09/01/") for c in archive)

    applied = CliRunner().invoke(cli.app, ["refile-audio", "--apply", "--batch-size", "2"])
    assert applied.exit_code == 0, applied.output
    session.expire_all()
    assert all(c.file_path.startswith("uploads/2024/02/") for c in archive)
