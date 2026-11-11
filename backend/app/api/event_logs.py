"""Archived recorder event logs: the recorder's upload, and the operator's list (§4.8).

trunkd writes **every** decoded control-channel message to a local ``<date>.jsonl.gz`` and
rolls one file per UTC day.  That firehose is deliberately never rows in this archive —
2.3 million messages a day on a busy site, of which ``radio_events`` keeps only the handful
that name a subscriber and ``recorder_observations`` keeps only the per-window histogram
(docs/NON-VOICE-DATA.md §5, "store the histogram, never the firehose").  But the recorder's
disk is not an archive: it fills, and the one artifact that could answer a question nobody
has thought to ask yet is exactly the one that gets rotated away.

So the day is *shipped whole* and kept as a file.  ``POST /recorder/event-log`` takes one
compressed day, verifies the digest the recorder claims for it, writes it under
``EVENT_LOG_ROOT/<instance>/<date>.jsonl.gz`` and records a receipt in
``event_log_archives``.  Postgres holds the receipt; the filesystem holds the bytes.

Three properties worth stating outright, because they are the contract trunkd implements:

* **The digest is verified, not trusted.**  The body is hashed as it is streamed to a
  temporary file, and the file is only moved into place if the hash and the byte count both
  match what the recorder said.  A truncated upload leaves nothing behind.
* **A repeat of the same day is a 409, not an overwrite** — when the digest matches.  The
  recorder succeeded and lost the response; saying so is cheaper than rewriting the file.
  A repeat with a *different* digest **replaces** it: that is a recorder that rebuilt a day
  it had only partly written, and refusing it would strand the archive on the worse copy.
* **The instance is a path segment, so it is validated like one.**  ``..`` and separators
  are refused outright rather than sanitized: a recorder that names itself something
  path-shaped is misconfigured, and quietly rewriting its identity would file its logs
  under a name it will never ask for them by.

Authentication is the recorder's own — a Bearer token carrying ``upload``, the same
:func:`app.api.observations.require_recorder_token` the call, observation and event pushes
use — so the route joins them in ``OPEN_MUTATION_PATHS`` and ``NO_CSRF_PATHS``.  The read
side is admin-gated like the rest of the recorder console.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from datetime import date as date_type
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.observations import RecorderToken
from app.auth import AdminPrincipal
from app.config import Settings, get_settings
from app.db import get_session
from app.models import EventLogArchive
from app.schemas import MAX_INSTANCE_ID_LEN, SHA256_RE, EventLogAccepted, EventLogArchiveOut

log = logging.getLogger("app.api.event_logs")

router = APIRouter(tags=["upload"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
DbSession = Annotated[Session, Depends(get_session)]

#: What an ``instance_id`` may contain when it is also going to be a directory name.  The
#: set is deliberately narrower than the one ``instance_id`` is stored with elsewhere: a
#: recorder may call itself anything it likes to the status endpoint, but a name that has to
#: survive a filesystem is checked against what a filesystem can hold.
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How much is hashed and written at a time.  1 MiB: large enough that a 100 MB day is a
#: hundred reads, small enough that a dozen concurrent uploads are not a memory event.
CHUNK = 1024 * 1024

#: The extension every archived day carries.  Named once because the upload path, the
#: duplicate check and the download filename all have to agree on it.
SUFFIX = ".jsonl.gz"


def _instance_dir(settings: Settings, instance: str) -> Path:
    """``EVENT_LOG_ROOT/<instance>``, with the instance checked as a path segment first."""
    if not INSTANCE_RE.match(instance):
        raise HTTPException(
            status_code=422,
            detail=(
                "instance: must be letters, digits, dot, dash or underscore, starting with a "
                "letter or digit"
            ),
        )
    return Path(settings.event_log_root) / instance


#: ``YYYY-MM-DD`` and nothing else.  ``date.fromisoformat`` also accepts the basic form
#: ``20260901`` and, on 3.11+, week and ordinal dates — all of which would name the same day
#: with a different filename, so the archive could hold two files for one date and neither
#: the uniqueness constraint nor an operator reading the directory would notice.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> date_type:
    """``YYYY-MM-DD`` or a 422.  The day is a filename, so the format is not negotiable."""
    text = value.strip()
    if not _DATE_RE.match(text):
        raise HTTPException(status_code=422, detail="date: must be YYYY-MM-DD")
    try:
        return date_type.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date: must be YYYY-MM-DD") from exc


@router.post(
    "/recorder/event-log",
    response_model=EventLogAccepted,
    summary="Archive one day of a recorder's raw event log",
    description=(
        "Uploads one compressed day of the recorder's own control-channel event log as "
        "multipart form data: `instance` (the recorder's instance_id), `date` (YYYY-MM-DD, the "
        "UTC day the log covers), `sha256` and `bytes` (what the recorder says the file is), and "
        "`file`, the `.jsonl.gz` itself. At most 100 MB.\n\n"
        "The digest and the length are **verified** while the body streams to disk; a mismatch "
        "is a 422 and leaves nothing behind. Re-uploading a day that is already archived with "
        "the **same** digest is a `409` — the recorder already succeeded and is retrying a lost "
        "response — while the same day with a **different** digest replaces the stored file, "
        "which is what a recorder that rebuilt a partial day is asking for. Needs a Bearer token "
        "with the `upload` scope, the same one `/api/call-upload` uses."
    ),
    responses={
        409: {"description": "That day is already archived with the same digest"},
        413: {"description": "The upload is over the 100 MB limit"},
        422: {"description": "Bad instance, date, digest or length"},
    },
)
def post_event_log(  # noqa: PLR0913 - a multipart form, one parameter per field
    db: DbSession,
    token: RecorderToken,
    settings: Annotated[Settings, Depends(get_settings)],
    instance: Annotated[str, Form(max_length=MAX_INSTANCE_ID_LEN, description="The recorder's instance_id")],
    date: Annotated[str, Form(description="The UTC day the log covers, YYYY-MM-DD")],
    sha256: Annotated[str, Form(description="Hex sha256 of the file")],
    bytes: Annotated[int, Form(ge=0, description="Length of the file")],  # noqa: A002 - the wire name
    file: Annotated[UploadFile, File(description="The .jsonl.gz for that day")],
) -> EventLogAccepted:
    directory = _instance_dir(settings, instance)
    day = _parse_date(date)
    digest = sha256.strip().lower()
    if not SHA256_RE.match(digest):
        raise HTTPException(status_code=422, detail="sha256: must be 64 hex characters")

    limit = settings.event_log_max_bytes
    if bytes > limit:
        raise HTTPException(
            status_code=413, detail=f"bytes: {bytes} is over the {limit} byte limit for one day"
        )

    # Existing receipt first: a retry of a day we already hold costs no disk at all.
    existing = db.scalar(
        select(EventLogArchive).where(
            EventLogArchive.instance == instance, EventLogArchive.date == day
        )
    )
    if existing is not None and existing.sha256 == digest:
        raise HTTPException(
            status_code=409,
            detail=f"{instance} {day.isoformat()} is already archived with that digest",
        )

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{day.isoformat()}{SUFFIX}"

    # Stream to a temporary file in the *destination* directory, so the rename below is
    # atomic (same filesystem) and a failed upload can never leave a half file where a
    # reader would find it.
    hasher = hashlib.sha256()
    written = 0
    descriptor, name = tempfile.mkstemp(dir=directory, suffix=".part")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := file.file.read(CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(
                        status_code=413, detail=f"the upload is over the {limit} byte limit"
                    )
                hasher.update(chunk)
                handle.write(chunk)
        if written != bytes:
            raise HTTPException(
                status_code=422, detail=f"bytes: the body is {written} bytes, not {bytes}"
            )
        actual = hasher.hexdigest()
        if actual != digest:
            raise HTTPException(status_code=422, detail=f"sha256: the body hashes to {actual}")
        # os.replace semantics: the reader either sees the old file or the new one.
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    if existing is not None:
        # Same day, different digest: the recorder rebuilt it.  The file has already been
        # replaced above; the receipt follows it.
        log.info(
            "event-log: %s %s replaced (%s -> %s)", instance, day, existing.sha256[:12], digest[:12]
        )
        existing.bytes = written
        existing.sha256 = digest
        existing.path = str(target)
    else:
        db.add(
            EventLogArchive(
                instance=instance,
                date=day,
                bytes=written,
                sha256=digest,
                path=str(target),
            )
        )
    db.commit()
    log.info("event-log: stored %s %s (%s bytes) [%s]", instance, day, written, token.label)
    return EventLogAccepted(stored=True, bytes=written)


@admin_router.get(
    "/event-logs",
    response_model=list[EventLogArchiveOut],
    summary="Archived recorder event logs (admin)",
    description=(
        "Every day of raw event log that has been shipped here, newest first, optionally for "
        "one `instance`. The bytes themselves come from "
        "`GET /admin/event-logs/{archive_id}/download`. Admin only."
    ),
)
def list_event_logs(
    db: DbSession,
    principal: AdminPrincipal,
    instance: Annotated[
        str | None, Query(description="Only this recorder instance", max_length=MAX_INSTANCE_ID_LEN)
    ] = None,
) -> list[EventLogArchiveOut]:
    stmt = select(EventLogArchive).order_by(
        EventLogArchive.date.desc(), EventLogArchive.instance
    )
    if instance:
        stmt = stmt.where(EventLogArchive.instance == instance)
    return [EventLogArchiveOut.model_validate(row) for row in db.scalars(stmt)]


@admin_router.get(
    "/event-logs/{archive_id}/download",
    response_class=FileResponse,
    summary="Download one archived event log (admin)",
    description=(
        "The `.jsonl.gz` exactly as the recorder shipped it. A 410 when the receipt survives but "
        "the file has been removed from disk — the row is the record that it *was* archived, and "
        "is not deleted just because the bytes are gone. Admin only."
    ),
    responses={410: {"description": "The file is no longer on disk"}},
)
def download_event_log(archive_id: int, db: DbSession, principal: AdminPrincipal) -> FileResponse:
    archive = db.get(EventLogArchive, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail=f"no event log archive {archive_id}")
    path = Path(archive.path)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="that event log is no longer on disk")
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=f"{archive.instance}-{archive.date.isoformat()}{SUFFIX}",
    )


__all__ = ["admin_router", "router"]
