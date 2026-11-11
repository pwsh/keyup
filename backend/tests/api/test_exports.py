"""Streaming exports and the asynchronous bundle (§4.1)."""

from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from pathlib import Path

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db


def settle(job_id: str, timeout: float = 30.0) -> None:
    """Wait for the worker thread ``POST /api/exports`` already started for this job.

    Every test below posts the job — which spawns ``export.start_job`` — and *then* runs
    ``run_job`` inline so it can pass its own settings.  Those are two runs of one job,
    serialized by the build lock but in whichever order the scheduler picks, so without
    this the API's run can finish after the inline one and leave behind the very zip the
    inline run just cleaned up.  Joining first makes the inline run the last word.
    """
    for thread in threading.enumerate():
        if thread.name == f"export-{job_id}":
            thread.join(timeout)


def test_csv_export_streams_every_matching_row(client, archive) -> None:
    response = client.get("/api/calls/export", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == len(archive.call_ids)
    assert rows[0]["system"] == "Example System"
    assert rows[0]["audio_url"].startswith("/api/calls/")
    assert "started_at" in rows[0] and "started_local" in rows[0]


def test_export_respects_the_filter(client, archive) -> None:
    response = client.get("/api/calls/export", params={"format": "csv", "talkgroup": 1701})
    assert len(list(csv.DictReader(io.StringIO(response.text)))) == 3


def test_ndjson_export_is_one_object_per_line(client, archive) -> None:
    response = client.get("/api/calls/export", params={"format": "ndjson"})
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.strip().splitlines()]
    assert len(lines) == len(archive.call_ids)
    assert set(lines[0]) == set(csv.DictReader(io.StringIO(
        client.get("/api/calls/export", params={"format": "csv"}).text
    )).fieldnames)


def test_json_export_is_a_single_array(client, archive) -> None:
    body = client.get("/api/calls/export", params={"format": "json"}).json()
    assert isinstance(body, list)
    assert len(body) == len(archive.call_ids)


def test_export_filename_carries_the_date_range(client, archive) -> None:
    response = client.get(
        "/api/calls/export", params={"format": "csv", "date_from": "2026-08-29", "date_to": "2026-08-29"}
    )
    assert "keyup-calls-20260829-20260829.csv" in response.headers["content-disposition"]


def test_export_can_be_a_hand_picked_selection(client, archive) -> None:
    """"Export selected": the ids a reader ticked, and nothing else."""
    picked = archive.call_ids[:2]
    response = client.get(
        "/api/calls/export", params={"format": "csv", "ids": picked}
    )

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert sorted(int(row["id"]) for row in rows) == sorted(picked)
    # Named for what it is: the date range would describe where the picks came from.
    assert "keyup-calls-selected-2.csv" in response.headers["content-disposition"]


def test_a_selection_narrows_the_filters_rather_than_replacing_them(client, archive) -> None:
    """"These, out of those" — otherwise a stale tick could resurrect a filtered-out call."""
    on_1701 = archive.by_tgid[1701]
    elsewhere = [i for i in archive.call_ids if i not in on_1701]
    assert on_1701 and elsewhere

    rows = list(
        csv.DictReader(
            io.StringIO(
                client.get(
                    "/api/calls/export",
                    params={"format": "csv", "talkgroup": 1701, "ids": [*on_1701, *elsewhere]},
                ).text
            )
        )
    )
    assert sorted(int(row["id"]) for row in rows) == sorted(on_1701)


def test_a_selection_works_in_every_format_and_on_the_playlist(client, archive) -> None:
    picked = archive.call_ids[:2]
    assert len(client.get("/api/calls/export", params={"format": "json", "ids": picked}).json()) == 2
    ndjson = client.get("/api/calls/export", params={"format": "ndjson", "ids": picked}).text
    assert len(ndjson.strip().splitlines()) == 2
    m3u = client.get("/api/calls/playlist.m3u", params={"ids": picked}).text
    assert m3u.count("#EXTINF") == 2


def test_too_many_ids_is_refused_rather_than_truncated(client, archive) -> None:
    from app.api.filters import MAX_FILTER_IDS

    ok = client.get(
        "/api/calls/export", params={"format": "csv", "ids": list(range(1, MAX_FILTER_IDS + 1))}
    )
    assert ok.status_code == 200

    over = client.get(
        "/api/calls/export", params={"format": "csv", "ids": list(range(1, MAX_FILTER_IDS + 2))}
    )
    assert over.status_code == 422
    assert "at most" in over.text


def test_bundle_job_zips_audio_and_metadata(client, archive, write_token, tmp_path, monkeypatch) -> None:
    from app import exports
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(exports, "export_dir", lambda s=None: tmp_path)

    created = client.post("/api/exports", params={"talkgroup": 1701}, headers=bearer(write_token))
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert created.json()["filters"]["talkgroup"] == [1701]

    settle(job_id)
    exports.run_job(job_id, settings)  # the inline run is the one whose settings are under test

    status = client.get(f"/api/exports/{job_id}").json()
    assert status["status"] == "done"
    assert status["call_count"] == 3
    assert status["download_url"] == f"/api/exports/{job_id}/download"
    assert status["created_by"].startswith("token:")

    download = client.get(f"/api/exports/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"

    bundle = zipfile.ZipFile(io.BytesIO(download.content))
    names = bundle.namelist()
    assert "calls.csv" in names
    assert "transcripts.json" in names
    assert "README.txt" in names
    assert sum(1 for n in names if n.startswith("audio/")) == 3
    assert len(list(csv.DictReader(io.StringIO(bundle.read("calls.csv").decode())))) == 3

    audio = next(n for n in names if n.startswith("audio/"))
    assert bundle.read(audio).startswith(b"ID3") or len(bundle.read(audio)) > 1000


def test_bundle_status_404s_and_download_409s_while_queued(client, archive, write_token) -> None:
    assert client.get("/api/exports/nope").status_code == 404
    created = client.post("/api/exports", params={"talkgroup": 1701}, headers=bearer(write_token))
    job_id = created.json()["id"]
    # The worker thread may already have finished; only assert the queued case when it has not.
    status = client.get(f"/api/exports/{job_id}").json()["status"]
    if status != "done":
        assert client.get(f"/api/exports/{job_id}/download").status_code == 409


def test_bundle_records_a_failure_on_the_job(client, archive, write_token, monkeypatch) -> None:
    from app import exports

    monkeypatch.setattr(exports, "export_dir", lambda s=None: Path("/proc/not-writable"))
    created = client.post("/api/exports", headers=bearer(write_token))
    job_id = created.json()["id"]
    settle(job_id)
    exports.run_job(job_id)
    body = client.get(f"/api/exports/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"]


def test_creating_a_bundle_needs_a_credential(client, archive) -> None:
    assert client.post("/api/exports", headers={"X-Requested-With": "keyup", "Origin": "http://testserver"}).status_code == 401


def test_a_bundle_is_capped_on_the_number_of_calls(client, archive, write_token, tmp_path, monkeypatch) -> None:
    """M5: "every call that matches" is a filter an operator can write by accident."""
    from app import exports
    from app.config import get_settings

    monkeypatch.setattr(exports, "export_dir", lambda s=None: tmp_path)
    settings = get_settings().model_copy(update={"export_max_calls": 2})

    job_id = client.post("/api/exports", headers=bearer(write_token)).json()["id"]
    settle(job_id)
    exports.run_job(job_id, settings)

    body = client.get(f"/api/exports/{job_id}").json()
    assert body["status"] == "failed"
    assert "more than 2 calls" in body["error"]
    assert not list(tmp_path.glob("*.zip"))  # the half-built zip is not left behind


def test_a_bundle_is_capped_on_the_bytes_of_audio(client, archive, write_token, tmp_path, monkeypatch) -> None:
    from app import exports
    from app.config import get_settings

    monkeypatch.setattr(exports, "export_dir", lambda s=None: tmp_path)
    settings = get_settings().model_copy(update={"export_max_bytes": 1024})

    job_id = client.post("/api/exports", headers=bearer(write_token)).json()["id"]
    settle(job_id)
    exports.run_job(job_id, settings)

    body = client.get(f"/api/exports/{job_id}").json()
    assert body["status"] == "failed"
    assert "bytes of audio" in body["error"]


def test_the_nightly_vacuum_removes_old_bundles(client, archive, session, write_token, tmp_path, monkeypatch) -> None:
    """M5: the export directory is the largest thing this deployment writes."""
    from datetime import UTC, datetime, timedelta

    from app import exports
    from app.config import get_settings
    from app.models import ExportJob

    monkeypatch.setattr(exports, "export_dir", lambda s=None: tmp_path)
    settings = get_settings()

    job_id = client.post("/api/exports", headers=bearer(write_token)).json()["id"]
    settle(job_id)
    exports.run_job(job_id, settings)
    bundle = tmp_path / f"keyup-export-{job_id}.zip"
    assert bundle.is_file()

    # Nothing is old enough yet.
    assert exports.vacuum_bundles(session, settings) == 0
    assert bundle.is_file()

    session.expire_all()
    row = session.get(ExportJob, job_id)
    row.created_at = datetime.now(UTC) - timedelta(days=settings.export_keep_days + 1)
    session.commit()

    assert exports.vacuum_bundles(session, settings) == 1
    assert not bundle.exists()

    body = client.get(f"/api/exports/{job_id}").json()
    assert body["status"] == "done" and body["download_url"] is None
    assert client.get(f"/api/exports/{job_id}/download").status_code == 410
