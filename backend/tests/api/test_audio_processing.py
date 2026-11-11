"""The API surface of the audio-processing stage (§5.5).

Serving: the audio route hands out the processed copy by default and the recorder's own
bytes on ``?raw=1``.  Filtering: ``tone_detected`` and ``alert`` narrow the list, and
neither touches ``emergency``.  Control: the reprocess endpoints queue work, the preview
endpoint runs the chain without storing anything.
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


def stored_path(path: Path) -> str:
    """``path`` as ``calls.processed_path`` holds it — relative to a *resolved* AUDIO_ROOT.

    Both ends are resolved because the audio root can be a symlink (it is, in a worktree),
    and :func:`app.audioproc.jobs._relative` resolves for the same reason.
    """
    from app.config import get_settings

    return str(path.resolve().relative_to(Path(get_settings().audio_root).resolve()))


def clear_jobs(session) -> None:  # noqa: ANN001
    """Drop the jobs ingest queued, so a test can assert on the ones *it* creates."""
    from app.models import AudioJob

    session.query(AudioJob).delete()
    session.commit()


@pytest.fixture
def processed(session, archive):  # noqa: ANN001, ANN201
    """One call given a real processed copy on disk, and one marked as a page.

    The copy is a distinct file with distinct bytes, so "which one did the route serve"
    is answerable by length alone.
    """
    from app.audio import audio_path, processed_path_for
    from app.config import get_settings
    from app.models import Call

    call = session.get(Call, archive.call_ids[0])
    source = audio_path(call, get_settings())
    target = processed_path_for(source)
    # Half the original's bytes: not a valid MP3, but this test is about routing, and a
    # different length is exactly what makes the two responses distinguishable.
    target.write_bytes(source.read_bytes()[: source.stat().st_size // 2])

    call.processed_path = stored_path(target)
    call.audio_meta = {"version": 1, "gain_db": 12.5, "mean_db": -32.0, "silent": False}
    call.tone_detected = True
    call.alert = True
    session.commit()
    try:
        yield call.id, source, target
    finally:
        target.unlink(missing_ok=True)


class TestServing:
    def test_the_processed_copy_is_served_by_default(self, client, processed) -> None:
        call_id, source, target = processed
        response = client.get(f"/api/calls/{call_id}/audio")
        assert response.status_code == 200
        assert response.headers["x-keyup-audio"] == "processed"
        assert len(response.content) == target.stat().st_size

    def test_raw_serves_the_recorders_own_bytes(self, client, processed) -> None:
        call_id, source, _ = processed
        response = client.get(f"/api/calls/{call_id}/audio?raw=1")
        assert response.status_code == 200
        assert response.headers["x-keyup-audio"] == "raw"
        assert len(response.content) == source.stat().st_size

    def test_the_two_bodies_do_not_share_an_etag(self, client, processed) -> None:
        """They are different bytes at one URL; a shared strong ETag would poison a cache."""
        call_id, _, _ = processed
        processed_etag = client.get(f"/api/calls/{call_id}/audio").headers["etag"]
        raw_etag = client.get(f"/api/calls/{call_id}/audio?raw=1").headers["etag"]
        assert processed_etag != raw_etag

    def test_a_call_with_no_processed_copy_serves_the_original(self, client, archive) -> None:
        response = client.get(f"/api/calls/{archive.call_ids[1]}/audio")
        assert response.status_code == 200
        assert response.headers["x-keyup-audio"] == "raw"

    def test_a_processed_path_that_is_not_on_disk_falls_back(self, client, session, archive) -> None:
        """The row can outlive the file — retention, a hand-deleted copy. Never a 404."""
        from app.models import Call

        call = session.get(Call, archive.call_ids[2])
        call.processed_path = "20260829/EXAMPLE EVENT 1/nothing-here.proc.mp3"
        session.commit()
        response = client.get(f"/api/calls/{call.id}/audio")
        assert response.status_code == 200
        assert response.headers["x-keyup-audio"] == "raw"

    def test_a_processed_path_outside_the_archive_is_refused(self, client, session, archive) -> None:
        from app.models import Call

        call = session.get(Call, archive.call_ids[2])
        call.processed_path = "../../../../etc/passwd"
        session.commit()
        response = client.get(f"/api/calls/{call.id}/audio")
        assert response.status_code == 200
        assert response.headers["x-keyup-audio"] == "raw"

    def test_head_agrees_with_get(self, client, processed) -> None:
        call_id, _, target = processed
        response = client.head(f"/api/calls/{call_id}/audio")
        assert response.status_code == 200
        assert int(response.headers["content-length"]) == target.stat().st_size
        assert response.headers["x-keyup-audio"] == "processed"


class TestCallFields:
    def test_the_list_carries_the_new_flags(self, client, processed) -> None:
        call_id, _, _ = processed
        row = next(
            item
            for item in client.get("/api/calls?limit=50").json()["items"]
            if item["id"] == call_id
        )
        assert row["tone_detected"] is True
        assert row["alert"] is True
        assert row["processed_audio"] is True
        assert row["audio_meta"]["gain_db"] == 12.5

    def test_an_unprocessed_call_reads_false(self, client, archive) -> None:
        row = next(
            item
            for item in client.get("/api/calls?limit=50").json()["items"]
            if item["id"] == archive.call_ids[1]
        )
        assert row["tone_detected"] is False
        assert row["alert"] is False
        assert row["processed_audio"] is False


class TestFilters:
    def test_alert_narrows_to_the_page(self, client, processed) -> None:
        call_id, _, _ = processed
        ids = [item["id"] for item in client.get("/api/calls?alert=true&limit=50").json()["items"]]
        assert ids == [call_id]

    def test_alert_false_excludes_it(self, client, processed) -> None:
        call_id, _, _ = processed
        ids = [item["id"] for item in client.get("/api/calls?alert=false&limit=50").json()["items"]]
        assert call_id not in ids
        assert ids

    def test_tone_detected_filters_independently(self, client, processed) -> None:
        call_id, _, _ = processed
        ids = [
            item["id"] for item in client.get("/api/calls?tone_detected=true&limit=50").json()["items"]
        ]
        assert ids == [call_id]

    def test_a_page_is_not_an_emergency(self, client, processed) -> None:
        """The two flags are different facts and neither may stand in for the other."""
        call_id, _, _ = processed
        ids = [
            item["id"] for item in client.get("/api/calls?emergency=true&limit=50").json()["items"]
        ]
        assert call_id not in ids


class TestReprocessEndpoints:
    def test_ingest_queues_a_pass_for_every_call_it_stores(self, session, archive) -> None:
        """The pipeline wiring: a stored call is owed a processed copy without anybody asking."""
        from app.models import AudioJob

        queued = {
            job.call_id
            for job in session.scalars(select(AudioJob).where(AudioJob.status == "queued"))
        }
        assert queued == set(archive.call_ids)

    def test_admin_can_queue_one_call(self, client, admin_token, archive, session) -> None:
        from app.models import AudioJob

        clear_jobs(session)
        call_id = archive.call_ids[0]
        response = client.post(f"/api/calls/{call_id}/reprocess-audio", headers=auth(admin_token))
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        job = session.scalars(select(AudioJob).where(AudioJob.call_id == call_id)).one()
        assert job.status == "queued"
        assert job.priority == 1

    def test_queueing_twice_is_a_conflict(self, client, admin_token, archive, session) -> None:
        """The open-job index is the lock: a second press cannot start a second render."""
        clear_jobs(session)
        call_id = archive.call_ids[0]
        client.post(f"/api/calls/{call_id}/reprocess-audio", headers=auth(admin_token))
        again = client.post(f"/api/calls/{call_id}/reprocess-audio", headers=auth(admin_token))
        assert again.status_code == 409

    def test_a_reader_may_not_queue(self, client, read_token, archive) -> None:
        response = client.post(
            f"/api/calls/{archive.call_ids[0]}/reprocess-audio", headers=auth(read_token)
        )
        assert response.status_code == 403

    def test_a_call_already_queued_by_ingest_is_not_queued_again(
        self, client, admin_token, archive
    ) -> None:
        body = client.post(
            "/api/admin/audio/reprocess", json={"force": True}, headers=auth(admin_token)
        ).json()
        assert body["matched"] == len(archive.call_ids)
        assert body["queued"] == 0
        assert body["already_open"] == len(archive.call_ids)

    def test_the_backfill_queues_everything_unprocessed(
        self, client, admin_token, archive, session
    ) -> None:
        clear_jobs(session)
        response = client.post(
            "/api/admin/audio/reprocess", json={"force": False}, headers=auth(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["queued"] == len(archive.call_ids)
        assert body["priority"] == 600

    def test_the_backfill_skips_a_current_copy(self, client, admin_token, processed, session) -> None:
        """The laziness that makes 'bump the version and re-run' the whole procedure."""
        clear_jobs(session)
        call_id, _, _ = processed
        queued = client.post(
            "/api/admin/audio/reprocess", json={"force": False}, headers=auth(admin_token)
        ).json()
        assert queued["queued"] >= 1

        second = client.post(
            "/api/admin/audio/reprocess", json={"force": False}, headers=auth(admin_token)
        ).json()
        assert second["queued"] == 0
        assert second["already_open"] == second["matched"]

    def test_force_queues_a_current_copy_too(self, client, admin_token, processed, session) -> None:
        clear_jobs(session)
        call_id, _, _ = processed
        body = client.post(
            "/api/admin/audio/reprocess",
            json={"force": True, "ids": [call_id]},
            headers=auth(admin_token),
        ).json()
        assert body["matched"] == 1
        assert body["queued"] == 1

    def test_only_tone_flagged_narrows_to_what_the_detector_heard(
        self, client, admin_token, archive, session
    ) -> None:
        """The selection an operator runs after a detector change (§5.5).

        A call the old pass heard nothing in cannot have been attenuated wrongly, so
        re-running the whole archive to find them buys ffmpeg passes and no change.
        """
        from app.models import Call

        clear_jobs(session)
        flagged = archive.call_ids[0]
        for call_id in archive.call_ids:
            session.get(Call, call_id).tone_detected = call_id == flagged
        session.commit()

        body = client.post(
            "/api/admin/audio/reprocess",
            json={"force": True, "only_tone_flagged": True},
            headers=auth(admin_token),
        ).json()
        assert body["matched"] == 1
        assert body["queued"] == 1

    def test_a_bad_since_is_a_422(self, client, admin_token) -> None:
        response = client.post(
            "/api/admin/audio/reprocess", json={"since": "yesterday"}, headers=auth(admin_token)
        )
        assert response.status_code == 422

    def test_the_backfills_refuse_while_the_stage_is_off(
        self, client, admin_token, archive, session  # noqa: ARG002
    ) -> None:
        """With the switch off the worker cancels every queued job on its next idle cycle,
        so a backfill then is rows written only to be cancelled.  The one-call
        ``reprocess-audio`` is not a bulk path and keeps its ``force``."""
        from typer.testing import CliRunner

        from app import runtime_settings
        from app.cli import app as cli
        from app.models import AudioJob, Setting

        clear_jobs(session)
        session.add(Setting(key="audio_process_enabled", value=False))
        session.commit()
        runtime_settings.invalidate()

        for path in ("/api/admin/audio/reprocess", "/api/admin/audio/repair-primaries"):
            response = client.post(path, json={"force": True}, headers=auth(admin_token))
            assert response.status_code == 409, (path, response.text)
            assert "switched off" in response.json()["detail"]
        for args in (["process-audio", "--force"], ["process-audio", "--retry-failed"]):
            result = CliRunner().invoke(cli, args)
            assert result.exit_code == 1, (args, result.output)
            assert "switched off" in result.output
        session.expire_all()
        assert session.query(AudioJob).count() == 0

        session.query(Setting).delete()
        session.commit()
        runtime_settings.invalidate()
        response = client.post(
            "/api/admin/audio/reprocess", json={"force": True}, headers=auth(admin_token)
        )
        assert response.status_code == 200
        assert response.json()["queued"] == len(archive.call_ids)


class TestAudioStatus:
    def test_the_status_counts_what_is_and_is_not_processed(self, client, processed, archive, read_token) -> None:
        body = client.get("/api/admin/audio", headers=auth(read_token)).json()
        assert body["enabled"] is True
        assert body["version"] >= 1
        assert body["processed"] == 1
        assert body["pending"] == len(archive.call_ids) - 1
        assert body["tone_today"] >= 0
        assert body["average_gain_db"] == pytest.approx(12.5)

    def test_the_counts_are_cached_and_a_backfill_refreshes_them(
        self, client, admin_token, archive, session, read_token
    ) -> None:
        """The coverage counts are full scans of ``calls``, so the tab's poll reads a cache;
        queuing a backfill drops it so the operator sees the queue they just filled."""
        clear_jobs(session)
        before = client.get("/api/admin/audio", headers=auth(read_token)).json()
        assert before["jobs"].get("queued", 0) == 0

        # A write the route knows nothing about is not seen until the entry expires…
        from app.models import Call

        session.query(Call).update({Call.processed_path: "stale/by/design.m4a"})
        session.commit()
        assert client.get("/api/admin/audio", headers=auth(read_token)).json()["processed"] == before["processed"]

        # …but a backfill queued through the API is seen at once.
        queued = client.post(
            "/api/admin/audio/reprocess", json={"force": True}, headers=auth(admin_token)
        ).json()["queued"]
        assert queued == len(archive.call_ids)
        after = client.get("/api/admin/audio", headers=auth(read_token)).json()
        assert after["jobs"]["queued"] == queued
        assert after["processed"] == len(archive.call_ids)

    def test_it_needs_a_sign_in_but_not_admin(self, client, read_token) -> None:
        """Like every admin read: closed to guests, open to any ``read`` credential."""
        assert client.get("/api/admin/audio").status_code == 401
        assert client.get("/api/admin/audio", headers=auth(read_token)).status_code == 200


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestPreview:
    def test_the_preview_runs_the_chain_and_stores_nothing(
        self, client, admin_token, session, archive
    ) -> None:
        from app.audio import audio_path, processed_path_for
        from app.config import get_settings
        from app.models import Call

        call = session.get(Call, archive.call_ids[0])
        beside = processed_path_for(audio_path(call, get_settings()))

        response = client.post(
            "/api/admin/audio/preview", json={"call_id": call.id}, headers=auth(admin_token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["call_id"] == call.id
        assert body["filter"]
        assert body["meta"]["mean_db"] < 0
        assert body["raw_url"].endswith("?raw=1")

        # Nothing was written beside the call and the row is untouched.
        assert not beside.exists()
        session.expire(call)
        assert call.processed_path is None

        audio = client.get(body["processed_url"], headers=auth(admin_token))
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/mpeg"
        assert len(audio.content) > 0

    def test_a_reader_may_not_preview(self, client, read_token, archive) -> None:
        response = client.post(
            "/api/admin/audio/preview", json={"call_id": archive.call_ids[0]}, headers=auth(read_token)
        )
        assert response.status_code == 403

    def test_an_unknown_call_is_a_404(self, client, admin_token) -> None:
        response = client.post(
            "/api/admin/audio/preview", json={"call_id": 999_999_999}, headers=auth(admin_token)
        )
        assert response.status_code == 404


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestWorkerEndToEnd:
    def test_one_pass_writes_the_copy_and_the_row(self, session, settings, archive) -> None:
        from app.audio import audio_path, processed_path_for
        from app.audioproc.chain import PIPELINE_VERSION
        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.config import get_settings
        from app.models import AudioJob, Call

        clear_jobs(session)
        call = session.get(Call, archive.call_ids[0])
        beside = processed_path_for(audio_path(call, get_settings()))
        assert enqueue(session, call, get_settings()) is not None
        session.commit()

        try:
            worked = AudioProcessWorker(
                settings=get_settings(), session_factory=lambda: session
            ).cycle()
            assert worked is True

            session.expire_all()
            call = session.get(Call, archive.call_ids[0])
            assert call.processed_path
            assert call.audio_meta["version"] == PIPELINE_VERSION
            assert beside.is_file()
            job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
            assert job.status == "done"
        finally:
            beside.unlink(missing_ok=True)

    def test_a_purged_recording_parks_the_job_rather_than_retrying(
        self, session, settings, archive
    ) -> None:
        """A file that is gone will not be back in thirty seconds; three tries only delay."""
        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.config import get_settings
        from app.models import AudioJob, Call

        clear_jobs(session)
        call = session.get(Call, archive.call_ids[1])
        assert enqueue(session, call, get_settings()) is not None
        call.audio_state = "purged"
        session.commit()

        assert AudioProcessWorker(settings=get_settings(), session_factory=lambda: session).cycle()
        session.expire_all()
        job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
        assert job.status == "failed"
        assert job.attempts == 0  # parked on the first pass, not retried into the ground
        assert "no audio" in (job.last_error or "")

    def test_an_empty_recording_finishes_done_and_marks_the_call_empty(
        self, session, settings, archive  # noqa: ANN001, ARG002
    ) -> None:
        """A 288-byte LAME header is not a fault — nothing was recorded.

        trunkd sent about forty a day.  Every one used to burn three attempts and park as a
        failed job on "could not find codec parameters", for a call nobody can repair.
        """
        from app.audio import audio_path
        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.config import get_settings
        from app.models import AudioJob, Call, TranscriptionJob
        from tests.test_audioproc import write_empty_mp3

        clear_jobs(session)
        call = session.get(Call, archive.call_ids[2])
        source = audio_path(call, get_settings())
        original = source.read_bytes()
        call.transcript_status = "pending"
        session.add(TranscriptionJob(call_id=call.id, status="queued"))
        assert enqueue(session, call, get_settings()) is not None
        session.commit()

        try:
            write_empty_mp3(source)
            assert AudioProcessWorker(
                settings=get_settings(), session_factory=lambda: session
            ).cycle()

            session.expire_all()
            job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
            assert job.status == "done"
            assert job.last_error is None
            done = session.get(Call, call.id)
            assert done.audio_meta == {"empty": True, "reason": "no frames"}
            assert done.transcript_status == "empty"
            assert done.processed_path is None
            assert done.tone_detected is False
            queued = session.scalars(
                select(TranscriptionJob).where(TranscriptionJob.call_id == call.id)
            ).one()
            assert queued.status == "cancelled"
        finally:
            source.write_bytes(original)

    def test_a_recording_that_really_is_broken_still_fails(
        self, session, settings, archive  # noqa: ANN001, ARG002
    ) -> None:
        """Over the size floor and undecodable: a fault, and it must still read as one."""
        from app.audio import audio_path
        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.config import get_settings
        from app.models import AudioJob, Call

        clear_jobs(session)
        call = session.get(Call, archive.call_ids[3])
        source = audio_path(call, get_settings())
        original = source.read_bytes()
        assert enqueue(session, call, get_settings()) is not None
        session.commit()

        try:
            source.write_bytes(b"\x00" * 64_000)
            assert AudioProcessWorker(
                settings=get_settings(), session_factory=lambda: session
            ).cycle()
            session.expire_all()
            job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
            assert job.status in {"queued", "failed"}
            assert job.last_error
            assert session.get(Call, call.id).transcript_status != "empty"
        finally:
            source.write_bytes(original)

    def test_retry_failed_puts_the_parked_jobs_back_on_the_queue(
        self, session, settings, archive  # noqa: ANN001, ARG002
    ) -> None:
        """``keyup process-audio --retry-failed`` — the one-shot over the 38 parked jobs."""
        from typer.testing import CliRunner

        from app.cli import app as cli
        from app.models import AudioJob

        clear_jobs(session)
        parked = AudioJob(
            call_id=archive.call_ids[0],
            status="failed",
            attempts=3,
            last_error="ffmpeg could not decode x.mp3: could not find codec parameters",
        )
        session.add(parked)
        session.commit()

        result = CliRunner().invoke(cli, ["process-audio", "--retry-failed"])
        assert result.exit_code == 0, result.output
        assert '"requeued": 1' in result.output

        session.expire_all()
        again = session.get(AudioJob, parked.id)
        assert again.status == "queued"
        assert again.attempts == 0
        assert again.last_error is None

    def test_retry_failed_requeues_one_job_per_call(
        self, session, settings, archive  # noqa: ANN001, ARG002
    ) -> None:
        """Two parked rows for one call must not both come back — the open-job index
        allows one, and the second would have aborted the whole retry."""
        from typer.testing import CliRunner

        from app.cli import app as cli
        from app.models import AudioJob

        clear_jobs(session)
        older = AudioJob(call_id=archive.call_ids[0], status="failed", attempts=3, last_error="a")
        newer = AudioJob(call_id=archive.call_ids[0], status="failed", attempts=3, last_error="b")
        session.add_all([older, newer])
        session.commit()

        result = CliRunner().invoke(cli, ["process-audio", "--retry-failed"])
        assert result.exit_code == 0, result.output
        assert '"requeued": 1' in result.output

        session.expire_all()
        assert session.get(AudioJob, newer.id).status == "queued"
        assert session.get(AudioJob, older.id).status == "failed"

    def test_deleting_the_call_takes_the_processed_copy_with_it(
        self, client, admin_token, session, settings, archive
    ) -> None:
        from app.audio import audio_path, processed_path_for
        from app.config import get_settings
        from app.models import Call

        call = session.get(Call, archive.call_ids[2])
        source = audio_path(call, get_settings())
        beside = processed_path_for(source)
        beside.write_bytes(b"not really an mp3")
        call.processed_path = stored_path(beside)
        session.commit()
        original = source.read_bytes()

        try:
            response = client.delete(
                f"/api/calls/{call.id}?delete_audio=1", headers=auth(admin_token)
            )
            assert response.status_code == 204
            assert not beside.exists()
        finally:
            beside.unlink(missing_ok=True)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(original)


class TestSettings:
    """The Audio group's two enums, and the one setting that has a prerequisite."""

    def test_the_enums_carry_their_options(self, client, read_token) -> None:
        rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=auth(read_token)).json()}
        assert rows["audio_noise_reduction"]["kind"] == "enum"
        assert rows["audio_noise_reduction"]["options"] == ["off", "afftdn", "arnndn", "anlmdn"]
        assert rows["audio_tone_handling"]["options"] == ["keep", "attenuate", "remove"]

    def test_a_value_outside_the_enum_is_refused(self, client, admin_token) -> None:
        """422 rather than a stored override — pydantic would reject the whole merged set."""
        response = client.put(
            "/api/admin/settings", json={"audio_tone_handling": "obliterate"}, headers=auth(admin_token)
        )
        assert response.status_code == 422
        assert "keep, attenuate, remove" in response.json()["detail"]

    def test_a_good_value_saves(self, client, admin_token) -> None:
        response = client.put(
            "/api/admin/settings", json={"audio_tone_handling": "remove"}, headers=auth(admin_token)
        )
        assert response.status_code == 200
        rows = {row["key"]: row for row in response.json()}
        assert rows["audio_tone_handling"]["value"] == "remove"
        assert rows["audio_tone_handling"]["is_default"] is False

    def test_arnndn_without_a_model_is_refused_at_save_time(self, client, admin_token) -> None:
        """Otherwise the page would show a denoiser that is not the one running."""
        response = client.put(
            "/api/admin/settings", json={"audio_noise_reduction": "arnndn"}, headers=auth(admin_token)
        )
        assert response.status_code == 422
        assert "model file" in response.json()["detail"]

    def test_arnndn_with_a_model_saves(self, client, admin_token, tmp_path) -> None:
        model = tmp_path / "bd.rnnn"
        model.write_bytes(b"not really a model, but it is a file")
        response = client.put(
            "/api/admin/settings",
            json={"audio_noise_reduction": "arnndn", "audio_rnnoise_model": str(model)},
            headers=auth(admin_token),
        )
        assert response.status_code == 200

    def test_afftdn_needs_no_model(self, client, admin_token) -> None:
        assert (
            client.put(
                "/api/admin/settings", json={"audio_noise_reduction": "afftdn"}, headers=auth(admin_token)
            ).status_code
            == 200
        )
