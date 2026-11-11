"""Exports: the streamed, uncapped download and the asynchronous audio bundle (§4.1).

Two shapes, because two different jobs:

* ``GET /api/calls/export?format=csv|json|ndjson`` streams *metadata* for the current
  filter with no row cap.  It opens its own session and iterates a server-side cursor, so
  a 8 000-row export costs a constant amount of memory and starts sending immediately.
  (A ``StreamingResponse`` body runs after FastAPI has torn down the request's
  dependencies, so borrowing the request session would be a use-after-close.)
* ``POST /api/exports`` queues a bundle: a background thread zips every matching MP3
  together with ``calls.csv`` and ``transcripts.json`` into ``EXPORT_DIR``.  Progress and
  the result live in ``export_jobs`` so the job survives the request that started it.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import threading
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.filters import (
    CallFilters,
    base_call_select,
    filters_from_mapping,
    order_by,
    resolve_filter_sets,
)
from app.audio import audio_path
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Call, ExportJob, Transcript
from app.runtime_settings import effective_settings

log = logging.getLogger("app.exports")

#: Column order of ``calls.csv`` and of the streamed CSV.  Stable: third parties diff it.
EXPORT_HEADER = [
    "id",
    "started_at",
    "started_local",
    "ended_at",
    "duration_s",
    "system",
    "site",
    "tgid",
    "talkgroup_alias",
    "talkgroup_ids",
    "category",
    "agency_code",
    "call_type",
    "patch_label",
    "radio_id",
    "radio_label",
    "unit_id",
    "radio_raw",
    "frequency_hz",
    "channel",
    "decoder",
    "emergency",
    "encrypted",
    "transcript_status",
    "transcript_quality",
    "transcript_text",
    "tags",
    "starred",
    "notes",
    "conversation_id",
    "audio_url",
    # Appended, never inserted: the order above is what a consumer's diff is against, so a
    # new column goes on the end where every existing position is untouched.  This one is
    # here because ``/calls/export`` is the bulk-sync path (docs/WEBHOOKS.md)
    # and a consumer copying the archive has to be able to tell a call recorded as it
    # happened from one an archive migration pushed months later.
    "ingest_priority",
    # Appended 2026-09-22 — the header is stable, so new columns go on the end.
    "radio_number",
    "talker_alias",
    "target_radio_number",
    "target_radio_label",
]

#: How many rows the server-side cursor materializes at a time.
STREAM_CHUNK = 500

#: How many call ids one ``IN`` list carries when the bundle reads transcripts and audio.
#: An unbounded ``IN (…)`` of every matching id was the other half of the memory problem.
ID_CHUNK = 1000

#: Bundles are built one at a time, process-wide.  Each one reads and compresses every
#: matching recording; two of them at once is how a 2 GB export becomes two (M5).
_BUILD_LOCK = threading.Lock()


def _value(mapping: Any, key: str) -> Any:
    value = mapping.get(key)
    if isinstance(value, list | tuple):
        return " ".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def export_row(mapping: Any) -> list[Any]:
    row = {
        "id": mapping["id"],
        "started_at": mapping["started_at"],
        "started_local": mapping["started_local"],
        "ended_at": mapping["ended_at"],
        "duration_s": float(mapping["duration_s"] or 0),
        "system": mapping.get("system_name"),
        "site": mapping.get("site_name"),
        "tgid": mapping.get("tgid"),
        "talkgroup_alias": mapping.get("talkgroup_alias") or mapping.get("talkgroup_alias_at_record"),
        "talkgroup_ids": mapping.get("talkgroup_ids"),
        "category": mapping.get("category"),
        "agency_code": mapping.get("agency_code"),
        "call_type": mapping.get("call_type"),
        "patch_label": mapping.get("patch_label") if mapping.get("patch_id") else None,
        "radio_id": mapping.get("radio_id"),
        "radio_label": mapping.get("radio_label"),
        "unit_id": mapping.get("unit_id"),
        "radio_raw": mapping.get("radio_raw"),
        "frequency_hz": mapping.get("frequency_hz"),
        "channel": mapping.get("channel"),
        "decoder": mapping.get("decoder"),
        "emergency": mapping.get("emergency"),
        "encrypted": mapping.get("encrypted"),
        "transcript_status": mapping.get("transcript_status"),
        "transcript_quality": mapping.get("transcript_quality"),
        "transcript_text": mapping.get("transcript_text"),
        "tags": mapping.get("tags"),
        "starred": mapping.get("starred"),
        "notes": mapping.get("notes"),
        "conversation_id": mapping.get("conversation_id"),
        "audio_url": f"/api/calls/{mapping['id']}/audio",
        "ingest_priority": mapping.get("ingest_priority"),
        "radio_number": mapping.get("radio_number"),
        "talker_alias": mapping.get("talker_alias"),
        "target_radio_number": mapping.get("target_radio_number"),
        "target_radio_label": mapping.get("target_radio_label"),
    }
    return [_value(row, key) for key in EXPORT_HEADER]


def export_object(mapping: Any) -> dict[str, Any]:
    return dict(zip(EXPORT_HEADER, export_row(mapping), strict=True))


def _iter_rows(session: Session, f: CallFilters, limit: int | None = None) -> Iterator[Any]:
    # An export reads the whole matching set, so the talkgroup sets are resolved to literal
    # arrays first — see app.api.filters.resolve_filter_sets.
    f = resolve_filter_sets(session, f)
    stmt = base_call_select(f).order_by(*order_by(f))
    if limit:
        stmt = stmt.limit(limit)
    result = session.execute(stmt.execution_options(yield_per=STREAM_CHUNK, stream_results=True))
    for row in result:
        yield row._mapping


def stream_csv(f: CallFilters, settings: Settings | None = None) -> Iterator[bytes]:
    """CSV with a header row, streamed.  Uncapped unless ``EXPORT_MAX_ROWS`` says otherwise."""
    session = SessionLocal()
    settings = effective_settings(session, base=settings)
    limit = settings.export_max_rows or None
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def flush() -> bytes:
        payload = buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)
        return payload

    try:
        writer.writerow(EXPORT_HEADER)
        yield flush()
        for mapping in _iter_rows(session, f, limit):
            writer.writerow(export_row(mapping))
            if buffer.tell() > 64_000:
                yield flush()
        remainder = flush()
        if remainder:
            yield remainder
    finally:
        session.close()


def stream_ndjson(f: CallFilters, settings: Settings | None = None) -> Iterator[bytes]:
    """One JSON object per line — the shape a pipeline wants."""
    session = SessionLocal()
    settings = effective_settings(session, base=settings)
    limit = settings.export_max_rows or None
    try:
        for mapping in _iter_rows(session, f, limit):
            yield orjson.dumps(export_object(mapping), default=str) + b"\n"
    finally:
        session.close()


def stream_json(f: CallFilters, settings: Settings | None = None) -> Iterator[bytes]:
    """A single JSON array, streamed element by element so nothing is buffered whole."""
    session = SessionLocal()
    settings = effective_settings(session, base=settings)
    limit = settings.export_max_rows or None
    try:
        yield b"["
        first = True
        for mapping in _iter_rows(session, f, limit):
            yield (b"" if first else b",") + orjson.dumps(export_object(mapping), default=str)
            first = False
        yield b"]"
    finally:
        session.close()


STREAMERS = {"csv": stream_csv, "ndjson": stream_ndjson, "json": stream_json}
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "ndjson": "application/x-ndjson",
    "json": "application/json",
}


def export_filename(f: CallFilters, suffix: str) -> str:
    """``keyup-calls-20260828-20260829.csv`` — dated when the filter is dated.

    A hand-picked selection is named ``keyup-calls-selected-12.csv`` instead: the dates
    would describe the window it was picked from, not the file, and somebody who exports
    two different selections out of one day's results needs two different filenames.
    """
    if f.ids:
        return f"keyup-calls-selected-{len(f.ids)}.{suffix}"
    parts = ["keyup-calls"]
    if f.date_from:
        parts.append(f.date_from.date().isoformat().replace("-", ""))
    if f.date_to:
        parts.append(f.date_to.date().isoformat().replace("-", ""))
    return "-".join(parts) + f".{suffix}"


# --------------------------------------------------------------------------------------
# the asynchronous bundle
# --------------------------------------------------------------------------------------


def export_dir(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).export_dir)


def new_job_id() -> str:
    return secrets.token_hex(8)


def create_job(session: Session, f: CallFilters, *, created_by: str | None = None) -> ExportJob:
    job = ExportJob(
        id=new_job_id(), filters=f.echo(), status="queued", created_by=created_by, call_count=0
    )
    session.add(job)
    session.commit()
    return job


def _chunks(values: list[int], size: int = ID_CHUNK) -> Iterator[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class ExportTooLarge(Exception):
    """The filter selects more than ``EXPORT_MAX_CALLS`` calls or ``EXPORT_MAX_BYTES``."""


def run_job(job_id: str, settings: Settings | None = None) -> None:
    """Build the zip for ``job_id``.  Runs in a thread with its own session.

    Serialized on :data:`_BUILD_LOCK` and capped twice — on the number of calls before any
    audio is read, and on the bytes of audio as they are added — because "every call that
    matches" is a filter an operator can write by accident (M5).
    """
    settings = settings or get_settings()
    session = SessionLocal()
    try:
        job = session.get(ExportJob, job_id)
        if job is None:
            return
        job.status = "running"
        session.commit()
        settings = effective_settings(session, base=settings)
        f = filters_from_mapping(dict(job.filters or {}))
        target_dir = export_dir(settings)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"keyup-export-{job_id}.zip"
        count = 0
        call_ids: list[int] = []

        max_calls = max(0, int(settings.export_max_calls))
        max_bytes = max(0, int(settings.export_max_bytes))
        audio_bytes = 0

        with _BUILD_LOCK, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer)
            writer.writerow(EXPORT_HEADER)
            for mapping in _iter_rows(session, f):
                writer.writerow(export_row(mapping))
                call_ids.append(mapping["id"])
                count += 1
                if max_calls and count > max_calls:
                    raise ExportTooLarge(
                        f"this filter selects more than {max_calls} calls — narrow it, or raise "
                        "EXPORT_MAX_CALLS"
                    )
            bundle.writestr("calls.csv", csv_buffer.getvalue())

            transcripts = [
                {
                    "call_id": t.call_id,
                    "engine": t.engine,
                    "backend_name": t.backend_name,
                    "model": t.model,
                    "language": t.language,
                    "text": t.edited_text if t.edited_by_user and t.edited_text else t.text_,
                    "segments": t.segments,
                    "quality": t.quality,
                    "is_primary": t.is_primary,
                    "edited_by_user": t.edited_by_user,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for chunk in _chunks(call_ids)
                for t in session.scalars(
                    select(Transcript).where(Transcript.call_id.in_(chunk)).order_by(Transcript.call_id)
                )
            ]
            bundle.writestr("transcripts.json", json.dumps(transcripts, indent=1, default=str))

            for chunk in _chunks(call_ids):
                for call in session.scalars(select(Call).where(Call.id.in_(chunk))):
                    source = audio_path(call, settings)
                    if source is None or not source.is_file():
                        continue
                    audio_bytes += source.stat().st_size
                    if max_bytes and audio_bytes > max_bytes:
                        raise ExportTooLarge(
                            f"this filter selects more than {max_bytes} bytes of audio — narrow "
                            "it, or raise EXPORT_MAX_BYTES"
                        )
                    bundle.write(source, arcname=f"audio/{call.id}-{Path(call.file_path).name}")
            bundle.writestr(
                "README.txt",
                "Keyup export\n"
                f"created: {datetime.now(UTC).isoformat()}\n"
                f"calls: {count}\n"
                f"filters: {json.dumps(job.filters or {}, default=str)}\n",
            )

        job.status = "done"
        job.file_path = str(target)
        job.size_bytes = target.stat().st_size
        job.call_count = count
        job.finished_at = datetime.now(UTC)
        session.commit()
        log.info("export.done id=%s calls=%d bytes=%s", job_id, count, job.size_bytes)
    except Exception as exc:  # noqa: BLE001 - the failure belongs on the job row
        if isinstance(exc, ExportTooLarge):
            log.warning("export.too_large id=%s: %s", job_id, exc)
        else:
            log.exception("export.failed id=%s", job_id)
        Path(export_dir(settings) / f"keyup-export-{job_id}.zip").unlink(missing_ok=True)
        session.rollback()
        job = session.get(ExportJob, job_id)
        if job is not None:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = datetime.now(UTC)
            session.commit()
    finally:
        session.close()


def start_job(job_id: str) -> threading.Thread:
    """Spawn the worker thread for ``job_id`` and return it (tests join on it)."""
    thread = threading.Thread(target=run_job, args=(job_id,), name=f"export-{job_id}", daemon=True)
    thread.start()
    return thread


def vacuum_bundles(session: Session, settings: Settings | None = None) -> int:
    """Delete finished bundles older than ``EXPORT_KEEP_DAYS``.  Returns how many went.

    A bundle is every matching MP3 in one zip; without this the export directory only ever
    grows, and it is the largest thing this deployment writes (M5).  The job row stays —
    it is the record that the export happened — with its ``file_path`` cleared.
    """
    settings = effective_settings(session, base=settings)
    days = max(0, int(settings.export_keep_days))
    if not days:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    for job in session.scalars(
        select(ExportJob).where(ExportJob.file_path.isnot(None), ExportJob.created_at < cutoff)
    ):
        path = Path(job.file_path)
        try:
            if path.is_file() and path.parent.resolve() == export_dir(settings).resolve():
                path.unlink()
        except OSError as exc:  # noqa: PERF203 - one stuck file must not stop the pass
            log.warning("export.vacuum: could not remove %s: %s", path, exc)
            continue
        # ``status`` stays ``done`` — the job *did* run, and the column is a Postgres
        # enum the wire contract shares; the cleared ``file_path`` is what says the bundle
        # is gone, and the download route answers 410 for it.
        job.file_path = None
        job.size_bytes = None
        removed += 1
    if removed:
        session.commit()
        log.info("export.vacuum: removed %d bundle(s) older than %d day(s)", removed, days)
    return removed


def job_out(job: ExportJob) -> dict[str, Any]:
    progress = {"queued": 0.0, "running": 0.5, "done": 1.0, "failed": 1.0, "cancelled": 1.0}
    return {
        "id": job.id,
        "status": job.status,
        "progress": progress.get(job.status, 0.0),
        "call_count": job.call_count,
        "bytes": job.size_bytes,
        "download_url": (
            f"/api/exports/{job.id}/download" if job.status == "done" and job.file_path else None
        ),
        "error": job.error,
        "filters": job.filters or {},
        "created_by": job.created_by,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


__all__ = [
    "EXPORT_HEADER",
    "ExportTooLarge",
    "MEDIA_TYPES",
    "STREAMERS",
    "create_job",
    "export_dir",
    "export_filename",
    "export_object",
    "export_row",
    "job_out",
    "run_job",
    "start_job",
    "stream_csv",
    "stream_json",
    "stream_ndjson",
    "vacuum_bundles",
]
