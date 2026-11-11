"""The audio discard pass — the per-talkgroup **Record** toggle's second half (§5.6).

The interesting behaviour is not "it deletes a file".  It is the three things it refuses to
delete under: a call younger than the grace (export discovery has not run yet), one whose
transcript is still being produced, and one with an upload still ``pending`` in the ledger.
Each of those is a reader that still needs the bytes, and getting any of them wrong means
deleting the input to a job the operator did ask for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def archive_root(tmp_path: Path):  # noqa: ANN201
    """Settings pointed at a throwaway tree, so the pass may really unlink."""
    from app.config import Settings
    from tests.conftest import TEST_DATABASE_URL

    root = tmp_path / "audio"
    root.mkdir()
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=root,
        tz="America/Winnipeg",
    )


def _talkgroup(session, *, record: bool = False, transcribe: bool = False, tgid: int = 1701):  # noqa: ANN001, ANN202
    from app.models import System, Talkgroup

    system = session.scalar(select(System).where(System.name == "Example-System"))
    if system is None:
        system = System(name="Example-System", protocol="P25", trunked=True)
        session.add(system)
        session.flush()
    talkgroup = Talkgroup(
        system_id=system.id, tgid=tgid, alias=f"TG {tgid}", record=record, transcribe=transcribe
    )
    session.add(talkgroup)
    session.flush()
    return talkgroup


def _call(  # noqa: ANN202, PLR0913
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
    talkgroup,  # noqa: ANN001
    *,
    policy: str = "discard",
    transcript_status: str = "skipped",
    age_s: int = 600,
    processed: bool = False,
):
    """One stored call with a real file (and, optionally, a real processed copy)."""
    from app.models import Call

    now = datetime.now(UTC)
    relative = f"uploads/{talkgroup.tgid}-{age_s}-{transcript_status}-{policy}.mp3"
    path = Path(settings.audio_root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb" + b"0" * 400)

    processed_rel = None
    if processed:
        processed_rel = relative.replace(".mp3", ".proc.mp3")
        (Path(settings.audio_root) / processed_rel).write_bytes(b"\xff\xfb" + b"1" * 200)

    call = Call(
        system_id=talkgroup.system_id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[talkgroup.tgid],
        started_at=now - timedelta(seconds=age_s),
        started_local=(now - timedelta(seconds=age_s)).replace(tzinfo=None),
        ended_at=now - timedelta(seconds=age_s) + timedelta(seconds=4.25),
        recorded_at=now - timedelta(seconds=age_s),
        ingested_at=now - timedelta(seconds=age_s),
        duration_s=4.25,
        file_path=relative,
        file_name=path.name,
        file_size=path.stat().st_size,
        processed_path=processed_rel,
        audio_state="present",
        audio_policy=policy,
        transcript_status=transcript_status,
        ingest_source="upload",
        time_source="upload",
    )
    session.add(call)
    session.flush()
    return call


# --------------------------------------------------------------------------------- go --


def test_a_discardable_call_loses_its_audio_and_keeps_everything_else(
    session, archive_root  # noqa: ANN001
) -> None:
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session)
    call = _call(session, archive_root, talkgroup, processed=True)
    original = Path(archive_root.audio_root) / call.file_path
    derived = Path(archive_root.audio_root) / call.processed_path

    report = run_discard(session, archive_root)

    assert report.discarded == 1
    assert not original.exists()
    # The derived copy goes with it — leaving one behind is playable audio for a call the
    # operator said not to keep.
    assert not derived.exists()
    assert report.bytes_freed > 0

    session.refresh(call)
    assert call.audio_state == "discarded"
    assert call.processed_path is None
    assert call.file_size is None
    # …and the row is still a row: the transmission happened, and every count says so.
    assert float(call.duration_s) == 4.25
    assert call.audio_archived_at is not None


def test_the_immediate_case_is_eligible_on_the_first_pass(session, archive_root) -> None:  # noqa: ANN001
    """Record off *and* transcribe off: nothing ever queues, so nothing is waited on.

    The only thing between ingest and the delete is the grace, which is there for the
    exporters rather than for the transcriber.
    """
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, record=False, transcribe=False)
    # `skipped` is what `enqueue_transcription` writes for a talkgroup with transcribe off.
    call = _call(session, archive_root, talkgroup, transcript_status="skipped", age_s=200)

    assert run_discard(session, archive_root).discarded == 1
    session.refresh(call)
    assert call.audio_state == "discarded"


def test_the_grace_protects_a_call_export_discovery_has_not_seen(session, archive_root) -> None:  # noqa: ANN001
    from app.audio_discard import GRACE_SECONDS, run_discard

    talkgroup = _talkgroup(session)
    call = _call(session, archive_root, talkgroup, age_s=GRACE_SECONDS - 30)

    assert run_discard(session, archive_root).discarded == 0
    session.refresh(call)
    assert call.audio_state == "present"
    assert (Path(archive_root.audio_root) / call.file_path).exists()

    # …and it goes on the pass that runs after the grace has elapsed.
    later = datetime.now(UTC) + timedelta(seconds=GRACE_SECONDS)
    assert run_discard(session, archive_root, now=later).discarded == 1


@pytest.mark.parametrize("status", ["pending", "queued", "running"])
def test_a_transcript_still_being_produced_keeps_the_audio(session, archive_root, status) -> None:  # noqa: ANN001
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, record=False, transcribe=True)
    call = _call(session, archive_root, talkgroup, transcript_status=status)

    assert run_discard(session, archive_root).discarded == 0
    session.refresh(call)
    assert call.audio_state == "present"


@pytest.mark.parametrize("status", ["done", "empty", "skipped", "failed"])
def test_a_finished_transcript_releases_the_audio(session, archive_root, status) -> None:  # noqa: ANN001
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, record=False, transcribe=True, tgid=1200)
    _call(session, archive_root, talkgroup, transcript_status=status)

    assert run_discard(session, archive_root).discarded == 1


def test_a_pending_upload_keeps_the_audio_until_it_has_gone(session, archive_root) -> None:  # noqa: ANN001
    """The exporter has a ledger row and has not sent the bytes yet."""
    from app.audio_discard import run_discard
    from app.models import CallExport

    talkgroup = _talkgroup(session)
    call = _call(session, archive_root, talkgroup)
    ledger = CallExport(call_id=call.id, service="broadcastify", status="pending")
    session.add(ledger)
    session.flush()

    assert run_discard(session, archive_root).discarded == 0
    assert (Path(archive_root.audio_root) / call.file_path).exists()

    # Uploaded — the service is done with the file, and so is keyup.
    ledger.status = "uploaded"
    session.flush()
    assert run_discard(session, archive_root).discarded == 1


@pytest.mark.parametrize("status", ["uploaded", "skipped", "failed"])
def test_a_settled_ledger_row_does_not_hold_the_audio(session, archive_root, status) -> None:  # noqa: ANN001
    from app.audio_discard import run_discard
    from app.models import CallExport

    talkgroup = _talkgroup(session, tgid=1300)
    call = _call(session, archive_root, talkgroup)
    session.add(CallExport(call_id=call.id, service="openmhz", status=status))
    session.flush()

    assert run_discard(session, archive_root).discarded == 1


def test_a_keep_call_is_never_touched(session, archive_root) -> None:  # noqa: ANN001
    """The whole archive, on every deployment that has not turned Record off."""
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, record=True, transcribe=True, tgid=1400)
    call = _call(session, archive_root, talkgroup, policy="keep", transcript_status="done")

    report = run_discard(session, archive_root)
    assert report.examined == 0
    assert report.discarded == 0
    session.refresh(call)
    assert call.audio_state == "present"
    assert (Path(archive_root.audio_root) / call.file_path).exists()


def test_a_starred_call_keeps_its_audio(session, archive_root) -> None:  # noqa: ANN001
    """R6: a star beats the Record toggle, as it beats retention."""
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, tgid=1401)
    starred = _call(session, archive_root, talkgroup, age_s=900)
    starred.starred = True
    plain = _call(session, archive_root, talkgroup, age_s=901, transcript_status="done")
    session.commit()

    report = run_discard(session, archive_root)
    session.commit()

    assert report.discarded == 1
    session.refresh(starred)
    session.refresh(plain)
    assert starred.audio_state == "present"
    assert (Path(archive_root.audio_root) / starred.file_path).exists()
    assert plain.audio_state == "discarded"


def test_the_queued_audio_job_is_cancelled_so_it_cannot_rewrite_the_file(
    session, archive_root  # noqa: ANN001
) -> None:
    """A job that ran afterwards would write a processed copy of a deleted recording."""
    from app.audio_discard import run_discard
    from app.models import AudioJob

    talkgroup = _talkgroup(session)
    call = _call(session, archive_root, talkgroup)
    job = AudioJob(call_id=call.id, status="queued", priority=100)
    session.add(job)
    session.flush()

    report = run_discard(session, archive_root)
    assert report.discarded == 1
    assert report.audio_jobs_cancelled == 1
    session.refresh(job)
    assert job.status == "cancelled"
    assert job.finished_at is not None


def test_a_running_audio_job_is_left_alone(session, archive_root) -> None:  # noqa: ANN001
    """Same rule reconciliation uses: a running job is mid-ffmpeg and not ours to pull."""
    from app.audio_discard import run_discard
    from app.models import AudioJob

    talkgroup = _talkgroup(session, tgid=1500)
    call = _call(session, archive_root, talkgroup)
    job = AudioJob(call_id=call.id, status="running", priority=100)
    session.add(job)
    session.flush()

    assert run_discard(session, archive_root).audio_jobs_cancelled == 0
    session.refresh(job)
    assert job.status == "running"


def test_one_activity_line_per_batch_and_none_for_an_empty_pass(session, archive_root) -> None:  # noqa: ANN001
    from app.audio_discard import run_discard
    from app.models import ActivityEvent

    def lines() -> list[ActivityEvent]:
        return list(
            session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "audio.discarded"))
        )

    assert run_discard(session, archive_root).discarded == 0
    assert lines() == []

    talkgroup = _talkgroup(session)
    for index in range(3):
        _call(session, archive_root, talkgroup, age_s=600 + index)

    assert run_discard(session, archive_root).discarded == 3
    rows = lines()
    assert len(rows) == 1
    assert rows[0].meta["discarded"] == 3


def test_a_missing_file_still_marks_the_row(session, archive_root) -> None:  # noqa: ANN001
    """Leaving it ``present`` would have the API promise a recording that is not there."""
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, tgid=1600)
    call = _call(session, archive_root, talkgroup)
    (Path(archive_root.audio_root) / call.file_path).unlink()

    report = run_discard(session, archive_root)
    assert report.discarded == 1
    assert report.missing == 1
    session.refresh(call)
    assert call.audio_state == "discarded"


def test_the_batch_limit_is_honoured(session, archive_root) -> None:  # noqa: ANN001
    from app.audio_discard import run_discard

    talkgroup = _talkgroup(session, tgid=1700)
    for index in range(5):
        _call(session, archive_root, talkgroup, age_s=600 + index)

    assert run_discard(session, archive_root, limit=2).discarded == 2
    assert run_discard(session, archive_root, limit=2).discarded == 2
    assert run_discard(session, archive_root, limit=2).discarded == 1
    assert run_discard(session, archive_root, limit=2).discarded == 0
