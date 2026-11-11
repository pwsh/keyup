"""Body-size limits for the three routes that accept an upload (H4).

``POST /api/call-upload`` authenticates itself from a form field, which means the
credential is *inside* the body: the server has to read the request before it can decide
whether it wanted it.  ``POST /api/talkgroups/import`` and ``/api/radios/import`` do check
a credential first, but both then buffer whatever arrived.  Either way an unauthenticated
peer could make this process spool an arbitrary number of bytes to disk.

Two defenses, in order:

1. :func:`enforce_body_limit`, installed as **middleware** in ``app.main``.  It has to be
   middleware rather than a route dependency: Starlette parses ``multipart/form-data``
   before FastAPI solves a route's dependencies, so by the time a dependency runs the
   bytes are already spooled.  A request with no ``Content-Length`` is refused with 411
   (this API has no chunked uploader) and one that declares too much with 413, both before
   a single byte of body is read.
2. :func:`stream_to_path`, which copies the audio part to its destination a megabyte at a
   time and aborts the moment the running total passes the cap — because ``Content-Length``
   is a claim, not a fact.  It is a *sync* function: by the time a route runs, the body has
   already been spooled by Starlette, so this is a file copy and belongs in the threadpool
   with the rest of the upload's blocking work.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, UploadFile
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

log = logging.getLogger("app.api.uploads")

#: 45 s of 32 kbps mono is ~180 kB; 50 MiB is four orders of magnitude of headroom and
#: still bounds what one request can make us write.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: An sdrtrunk ``default.xml`` for this system is ~200 kB.
MAX_IMPORT_BYTES = 8 * 1024 * 1024

#: The Broadcastify feed dashboard's CSV exports are 180 rows of about 30 bytes — some 6 kB
#: at their largest.  2 MiB is three hundred times that, which is generous for a file whose
#: length Broadcastify chooses, and small enough that the cap says plainly that this route
#: takes a CSV and not an archive.
MAX_BCFY_IMPORT_BYTES = 2 * 1024 * 1024

#: How much is copied per read when streaming a part to disk.
CHUNK_BYTES = 1024 * 1024

#: path -> the most bytes that path will accept.  Anything not listed is unlimited here
#: and bounded by whatever is in front of this process.
BODY_LIMITS: dict[str, int] = {
    "/api/call-upload": MAX_UPLOAD_BYTES,
    "/api/talkgroups/import": MAX_IMPORT_BYTES,
    "/api/radios/import": MAX_IMPORT_BYTES,
    "/api/units/import": MAX_IMPORT_BYTES,
    "/api/personnel/import": MAX_IMPORT_BYTES,
    "/api/admin/bcfy-stats/import": MAX_BCFY_IMPORT_BYTES,
}

LENGTH_REQUIRED_DETAIL = "Content-Length is required on an upload"
TOO_LARGE_DETAIL = "the upload is larger than this endpoint accepts"


class UploadTooLarge(Exception):
    """The bytes on the wire outran the cap despite what ``Content-Length`` claimed."""


def limit_for(path: str) -> int | None:
    return BODY_LIMITS.get(path)


def check_content_length(headers: dict[str, str] | Any, limit: int) -> None:  # noqa: ANN401
    """Raise 411/413 from the declared length alone, before the body is touched."""
    raw = headers.get("content-length")
    if raw is None:
        raise HTTPException(status_code=411, detail=LENGTH_REQUIRED_DETAIL)
    try:
        declared = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Content-Length is not a number") from exc
    if declared > limit:
        raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)


def enforce_body_limit(request: HTTPConnection) -> None:
    """Apply :data:`BODY_LIMITS` to this request, or do nothing."""
    limit = limit_for(request.scope.get("path", ""))
    if limit is None:
        return
    check_content_length(request.headers, limit)


async def body_size_guard(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
    """The middleware itself — ``app.main`` installs it, and the upload tests reuse it.

    It has to be middleware and not a route dependency: Starlette parses the multipart
    body before FastAPI solves a route's dependencies, so a dependency would run after the
    bytes had already been spooled to disk.
    """
    try:
        enforce_body_limit(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


def stream_to_path(upload: UploadFile, target: Path, limit: int) -> int:
    """Copy ``upload`` to ``target`` in :data:`CHUNK_BYTES` blocks; return the byte count.

    Raises :class:`UploadTooLarge` — and removes the partial file — as soon as the running
    total passes ``limit``, so a lying ``Content-Length`` costs one chunk more than the cap
    rather than the whole disk.

    **Deliberately synchronous.**  There is no network streaming left to do here: Starlette
    has already parsed the multipart body and spooled the audio part to a
    ``SpooledTemporaryFile`` (in memory up to a megabyte, on disk past it) *before* the
    route function is entered, so this is a file-to-file copy however it is spelled.  The
    async spelling only ever wrapped ``file.read`` in ``run_in_threadpool`` one chunk at a
    time while ``handle.write`` stayed on the event loop; the caller now runs the whole
    request in the threadpool instead (see :mod:`app.api.call_upload`), which is both the
    honest description and the one that cannot block the loop.  Nothing is buffered beyond
    one chunk either way.
    """
    written = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = upload.file.read(CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise UploadTooLarge(f"more than {limit} bytes")
                handle.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return written


async def read_import_payload(
    request: Request, file: UploadFile | None, *, default_suffix: str = ".xml"
) -> tuple[bytes, str]:
    """Accept either a multipart upload or a JSON ``{content, format}`` body.

    The SPA reads the file in the browser and posts its text; ``curl -F`` and the CLI send
    the file itself.  Both end up as bytes plus a suffix for the parser to dispatch on.

    ``Content-Length`` was already checked by :func:`body_size_guard` before the body was
    parsed; the cap is applied again to the bytes themselves, because the header is a
    claim (H4).

    Shared by every importer — playlists, and the unit and personnel rosters — so there is
    one place where the two ways of sending a file are reconciled and one place where the
    size cap is enforced a second time.
    """
    if file is not None:
        raw = await file.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
        suffix = Path(file.filename or "").suffix.lower() or default_suffix
        return raw, suffix
    body = await request.json()
    content = body.get("content")
    if not content:
        raise HTTPException(status_code=422, detail="send a file, or JSON with a 'content' field")
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
    fmt = (body.get("format") or "").lower()
    if fmt not in {"xml", "csv"}:
        fmt = "xml" if content.lstrip().startswith("<") else "csv"
    return content.encode("utf-8"), f".{fmt}"


__all__ = [
    "BODY_LIMITS",
    "CHUNK_BYTES",
    "LENGTH_REQUIRED_DETAIL",
    "MAX_BCFY_IMPORT_BYTES",
    "MAX_IMPORT_BYTES",
    "MAX_UPLOAD_BYTES",
    "TOO_LARGE_DETAIL",
    "UploadTooLarge",
    "body_size_guard",
    "check_content_length",
    "enforce_body_limit",
    "limit_for",
    "read_import_payload",
    "stream_to_path",
]
