"""Calls: list, facets, detail, audio, waveform, transcripts, edits and exports (§4.1)."""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import exports as export_service
from app import waveform as waveform_service
from app.api.filters import (
    Filters,
    Paging,
    base_call_select,
    call_hidden_from_guests,
    filters_from_mapping,
    order_by,
    resolve_filter_sets,
)
from app.api.search_load import SEARCH_LOAD
from app.audio import (
    AudioUnavailable,
    audio_path,
    processed_path,
    prune_empty_dirs,
    resolve_playback,
)
from app.auth import AdminPrincipal, OptionalPrincipal, Principal, WritePrincipal
from app.config import get_settings
from app.db import get_session, lock_calls, notify
from app.ingest.reconcile import make_primary
from app.ingest.service import enqueue_transcription
from app.models import Call, ExportJob, Talkgroup, Transcript, TranscriptionJob
from app.runtime_settings import effective_settings
from app.schemas import (
    CallDetail,
    CallOut,
    CallPatch,
    ExportJobOut,
    FacetsOut,
    Page,
    TranscribeAccepted,
    TranscribeRequest,
    TranscriptOut,
    WaveformOut,
)
from app.services import calls as call_service

router = APIRouter(prefix="/calls", tags=["calls"])
exports_router = APIRouter(prefix="/exports", tags=["calls"])

DbSession = Annotated[Session, Depends(get_session)]

#: ``bytes=0-1023`` / ``bytes=1024-`` / ``bytes=-512``.  One range only: media elements
#: never ask for more, and a multipart response would need a different content type.
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

#: Audio never changes once written, so it may be cached hard — but privately: an export
#: of somebody's radio traffic should not sit in a shared proxy.
AUDIO_CACHE_CONTROL = "private, max-age=31536000, immutable"


#: Everything a filename may contain in a ``Content-Disposition``.  The recorder's own
#: names are already this shape; a header value that is not would let a stored filename
#: inject its own header parameters into the response.
_DISPOSITION_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def content_disposition(kind: str, filename: str | None) -> str:
    """``inline; filename="…"`` with the name sanitized (L3)."""
    name = _DISPOSITION_SAFE_RE.sub("_", Path(filename or "").name).strip() or "recording.mp3"
    return f'{kind}; filename="{name[:180]}"'


log = logging.getLogger("app.api.calls")

#: extension -> the type the audio route serves it as.  sdrtrunk writes MP3; trunk-recorder
#: writes WAV and/or M4A (§1.7), and a `<audio>` element that is told the wrong type either
#: refuses to play the file or, worse, plays it only in the browser that guesses well.
AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
}

DEFAULT_AUDIO_MEDIA_TYPE = "audio/mpeg"


def audio_media_type(path: Path) -> str:
    """The ``Content-Type`` for a stored recording, from its extension."""
    return AUDIO_MEDIA_TYPES.get(path.suffix.lower(), DEFAULT_AUDIO_MEDIA_TYPE)


# --------------------------------------------------------------------------------------
# list, facets
# --------------------------------------------------------------------------------------


@router.get(
    "",
    dependencies=SEARCH_LOAD,
    response_model=Page[CallOut],
    summary="List calls",
    description=(
        "Filtered, paginated list of recorded transmissions. Accepts the whole common filter "
        "set (see the parameters below — the same set works on `/calls/facets`, `/calls/export`, "
        "`/search` and the live feed) and both pagination styles: `page`/`page_size`, or `cursor` "
        "for a stable infinite scroll. `total` is counted only for `page`/`page_size` — it is "
        "null while cursor paging — and stops at 50 000, where `total_capped` becomes true. It is "
        "also returned in the `X-Total-Count` header whenever it was counted."
    ),
)
def list_calls(f: Filters, paging: Paging, db: DbSession, response: Response) -> Page[CallOut]:
    page = call_service.list_calls(db, f, paging)
    if page.total is not None:
        response.headers["X-Total-Count"] = str(page.total)
    return Page[CallOut](
        items=page.items,
        total=page.total,
        total_capped=page.total_capped,
        page=paging.page,
        page_size=paging.page_size,
        next_cursor=page.next_cursor,
    )


@router.get(
    "/facets",
    dependencies=SEARCH_LOAD,
    response_model=FacetsOut,
    summary="Facet counts for the current filter",
    description=(
        "Counts per talkgroup, radio, agency, category, hour of the local day, day of week, "
        "transcript status, quality and tag for the filtered set — the Calls sidebar. Every "
        "dimension is counted over the same filtered set, so clicking a value reproduces its count."
    ),
)
def call_facets(
    f: Filters,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=500, description="Values per dimension")] = 50,
) -> FacetsOut:
    return call_service.facets(db, f, limit=limit)


# --------------------------------------------------------------------------------------
# exports (declared before /{call_id} so the literal paths win)
# --------------------------------------------------------------------------------------


@router.get(
    "/export",
    summary="Stream the filtered calls as CSV, JSON or NDJSON",
    description=(
        "Uncapped and streamed: the response starts before the query has finished and costs a "
        "constant amount of memory, so exporting the whole archive is fine. `format=csv` (default) "
        "returns the stable column set documented in `app/exports.py`; `ndjson` is one object per line. "
        "Pass `ids` to export a hand-picked selection instead of everything the filters match."
    ),
    responses={200: {"content": {"text/csv": {}, "application/json": {}, "application/x-ndjson": {}}}},
)
def export_calls(
    f: Filters,
    fmt: Annotated[
        Literal["csv", "json", "ndjson"], Query(alias="format", description="Output format")
    ] = "csv",
) -> StreamingResponse:
    streamer = export_service.STREAMERS[fmt]
    filename = export_service.export_filename(f, "csv" if fmt == "csv" else fmt)
    return StreamingResponse(
        streamer(f),
        media_type=export_service.MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/playlist.m3u",
    response_class=PlainTextResponse,
    summary="M3U playlist of the filtered calls",
    description=(
        "An extended M3U an external player can stream from: one `#EXTINF` per call carrying "
        "the duration and a `talkgroup · local time · transcript` title. Capped at `limit` entries "
        "because players load the whole list into memory."
    ),
)
def playlist(
    f: Filters,
    request: Request,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=5000, description="Maximum entries")] = 500,
) -> PlainTextResponse:
    base = str(request.base_url).rstrip("/")
    stmt = base_call_select(resolve_filter_sets(db, f)).order_by(*order_by(f)).limit(limit)
    lines = ["#EXTM3U"]
    for row in db.execute(stmt):
        m = row._mapping
        label = m["talkgroup_alias"] or m["talkgroup_alias_at_record"] or str(m["tgid"] or "")
        # started_local is nullable — a row imported before the column existed, or one
        # whose recorder clock could not be resolved — and one such row must not 500 the
        # whole playlist (L9).
        local = m["started_local"] or m["started_at"]
        when = local.strftime("%Y-%m-%d %H:%M:%S") if local is not None else "unknown time"
        text = (m["transcript_text"] or "").replace("\n", " ").strip()
        title = f"{label} · {when}" + (f" · {text[:80]}" if text else "")
        lines.append(f"#EXTINF:{int(float(m['duration_s'] or 0))},{title}")
        lines.append(f"{base}/api/calls/{m['id']}/audio")
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="keyup.m3u"'},
    )


@exports_router.post(
    "",
    response_model=ExportJobOut,
    status_code=202,
    summary="Queue an audio + metadata bundle",
    description=(
        "Starts a background job that zips every matching MP3 together with `calls.csv` and "
        "`transcripts.json` into `EXPORT_DIR`. Poll `GET /exports/{id}` and download from "
        "`GET /exports/{id}/download` once the status is `done`."
    ),
)
def create_export(f: Filters, db: DbSession, principal: WritePrincipal) -> ExportJobOut:
    job = export_service.create_job(db, f, created_by=principal.label)
    export_service.start_job(job.id)
    return ExportJobOut.model_validate(export_service.job_out(job))


@exports_router.get("/{export_id}", response_model=ExportJobOut, summary="Export job status")
def export_status(export_id: str, db: DbSession) -> ExportJobOut:
    job = db.get(ExportJob, export_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no export job {export_id!r}")
    return ExportJobOut.model_validate(export_service.job_out(job))


@exports_router.get(
    "/{export_id}/download",
    summary="Download a finished export bundle",
    description=(
        "The zip, once the job is `done`. Bundles are deleted by a nightly vacuum after "
        "`EXPORT_KEEP_DAYS`, after which this answers 410. Downloading needs a credential "
        "exactly when reading the archive does — the bundle holds the same recordings the "
        "rest of the API serves, no more."
    ),
    response_class=FileResponse,
    responses={
        409: {"description": "The bundle is not finished"},
        410: {"description": "The bundle has been vacuumed or removed"},
    },
)
def export_download(export_id: str, db: DbSession) -> FileResponse:
    job = db.get(ExportJob, export_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no export job {export_id!r}")
    if job.status != "done":
        raise HTTPException(status_code=409, detail=f"export {export_id} is {job.status}")
    # A finished job with no file has been through the nightly vacuum (M5).
    path = Path(job.file_path) if job.file_path else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=410, detail="the export bundle has been removed from disk")
    return FileResponse(
        path, media_type="application/zip", filename=f"keyup-export-{export_id}.zip"
    )


@exports_router.get("", response_model=Page[ExportJobOut], summary="Recent export jobs")
def list_exports(
    db: DbSession,
    response: Response,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[ExportJobOut]:
    total = int(db.scalar(select(func.count()).select_from(ExportJob)) or 0)
    rows = db.scalars(
        select(ExportJob)
        .order_by(ExportJob.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[ExportJobOut](
        items=[ExportJobOut.model_validate(export_service.job_out(j)) for j in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------------------
# one call
# --------------------------------------------------------------------------------------

CallId = Annotated[int, PathParam(description="Call id", examples=[1])]


@router.get(
    "/{call_id}",
    response_model=CallDetail,
    summary="One call, in full",
    description=(
        "The call row plus the directory objects behind it, the primary transcript with its "
        "segments, the previous and next call on the same talkgroup, the conversation it belongs "
        "to and every external link recorded against it — everything the detail page needs in one trip."
    ),
    responses={404: {"description": "No such call"}},
)
def get_call(call_id: CallId, db: DbSession, principal: OptionalPrincipal) -> CallDetail:
    _withhold_hidden(db, call_id, principal)
    detail = call_service.call_detail(db, call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    if principal is None and detail.transcript is not None:
        detail = detail.model_copy(update={"transcript": _for_guest(detail.transcript)})
    return detail


def _withhold_hidden(db: Session, call_id: int, principal: Principal | None) -> None:
    """A guest gets the same 404 as for a call that does not exist."""
    if principal is None and call_hidden_from_guests(db, call_id):
        raise HTTPException(status_code=404, detail=f"no call {call_id}")


def _for_guest(transcript: TranscriptOut) -> TranscriptOut:
    """A transcript without the speech-to-text server that produced it.

    Which servers the operator runs is not public (the same line ``GET /stats/transcription``
    and the Stats page draw); the engine and model stay, they describe the words.
    """
    return transcript.model_copy(update={"backend_name": None})


def _audio_path(db: Session, call_id: int, *, raw: bool = False) -> tuple[Call, Path, bool]:
    """The call, the file to serve and whether it is the processed copy (§5.5).

    Path resolution and the containment check live in :mod:`app.audio`, which every reader
    of an audio file goes through — this route, the waveform, the export bundle, the
    transcription worker and retention.  ``raw`` asks for the recorder's own bytes; without
    it the processed copy wins whenever one exists, which is what makes one call as loud as
    the next without anybody choosing per call.
    """
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    try:
        # The runtime settings, not the environment's: the archive paths an operator set in
        # Admin — and the history of past ones — are allowed roots too (review R1).
        path, processed = resolve_playback(call, effective_settings(db, base=get_settings()), raw=raw)
    except AudioUnavailable as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return call, path, processed


def _check_feed_grant(db: Session, call_id: int, token: str) -> None:
    """``?feed=<token>`` may fetch **this** call's audio, or it may not exist.

    A feed token is a URL handed to software, so it has to be able to fetch enclosures; it
    is emphatically not a read credential for the archive.  The grant is therefore
    re-derived per request from exactly what the feed document would publish: the feed's
    own filters **and** the readiness gate (:func:`app.api.feeds.ready_for_publication`),
    and only for a feed that publishes audio at all — a transcript-only feed carries no
    enclosure, so its token opens no recording.  Every way of failing — a token that is not
    a feed, a feed that is switched off or has no audio, a filter set that no longer
    parses, a call outside it or not yet published — answers with the *same* 404 as a call
    that does not exist, so the response says nothing about which of those it was.
    Nothing about the token reaches the log.
    """
    from app.api.feeds import feed_by_token, ready_for_publication  # noqa: PLC0415 - import cycle

    missing = HTTPException(status_code=404, detail=f"no call {call_id}")
    try:
        feed = feed_by_token(db, token)
        filters = filters_from_mapping(dict(feed.query or {}))
    except (HTTPException, TypeError, ValueError) as exc:
        raise missing from exc
    if not feed.include_audio:
        raise missing
    f = resolve_filter_sets(db, filters)
    granted = db.execute(
        base_call_select(f).where(Call.id == call_id).where(ready_for_publication()).limit(1)
    ).first()
    if granted is None:
        raise missing


def _etag(call: Call, path: Path, *, processed: bool = False) -> str:
    """The file's sha256 when ingest recorded one, else a cheap size+mtime tag.

    The processed copy never uses the row's ``sha256`` — that is the hash of the *original*,
    and handing the same strong ETag out for two different bodies would let a client that
    fetched ``?raw=1`` keep serving those bytes for the processed URL for a year.
    """
    if call.sha256 and not processed:
        return f'"{call.sha256}"'
    stat = path.stat()
    seed = f"{stat.st_size}-{stat.st_mtime_ns}{'-proc' if processed else ''}"
    return f'"{hashlib.sha256(seed.encode()).hexdigest()}"'


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """``(start, end)`` inclusive for a single byte range, or ``None`` when unsatisfiable."""
    match = RANGE_RE.match(header.strip())
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # suffix range: the final N bytes
        length = int(last)
        if length <= 0:
            return None
        return max(0, size - length), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


@router.get(
    "/{call_id}/audio",
    summary="Stream the recording",
    description=(
        "The recording as stored — MP3 from sdrtrunk, M4A or WAV from trunk-recorder, served "
        "with the `Content-Type` its extension calls for — with HTTP `Range` support (a media "
        "element seeks with it), a strong "
        "`ETag` — the file's sha256, which ingest already computed — and a long private "
        "`Cache-Control`, because a recording never changes. `?download=1` adds a "
        "`Content-Disposition: attachment` with the recorder's own filename."
    ),
    responses={
        200: {"content": {"audio/mpeg": {}, "audio/mp4": {}, "audio/wav": {}}},
        206: {"description": "Partial content for a `Range` request"},
        304: {"description": "The client's `If-None-Match` still matches"},
        416: {"description": "The requested range is not satisfiable"},
        410: {"description": "The row exists but the audio has been purged or archived"},
    },
)
def call_audio(
    call_id: CallId,
    request: Request,
    db: DbSession,
    principal: OptionalPrincipal,
    download: Annotated[bool, Query(description="Send as an attachment")] = False,
    raw: Annotated[bool, Query(description="Serve the recorder's own file, unprocessed")] = False,
    feed: Annotated[
        str | None,
        Query(
            description=(
                "A syndication feed token (docs/FEEDS.md). Lets a podcast app fetch the "
                "enclosure without an account — but only for a call that feed's own "
                "filters match."
            )
        ),
    ] = None,
) -> Response:
    if feed is not None:
        _check_feed_grant(db, call_id, feed)
    else:  # a feed token is its own grant, for the calls its owner's filters match
        _withhold_hidden(db, call_id, principal)
    call, path, processed = _audio_path(db, call_id, raw=raw)
    size = path.stat().st_size
    etag = _etag(call, path, processed=processed)
    media_type = audio_media_type(path)
    disposition = content_disposition("attachment" if download else "inline", call.file_name)
    headers = {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Cache-Control": AUDIO_CACHE_CONTROL,
        "Content-Disposition": disposition,
        # So a caller (and a log) can tell the two bodies apart without parsing the URL.
        # `?raw=1` is already part of the cache key, so no `Vary` is needed for it.
        "X-Keyup-Audio": "processed" if processed else "raw",
    }

    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)

    if request.method == "HEAD":
        # Players and link checkers probe with HEAD before committing to a download.
        return Response(
            status_code=200,
            media_type=media_type,
            headers={**headers, "Content-Length": str(size)},
        )

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=headers)

    span = _parse_range(range_header, size)
    if span is None:
        return Response(
            status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
        )
    start, end = span
    length = end - start + 1

    def chunks(chunk_size: int = 64 * 1024):  # noqa: ANN202 - a local generator
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(chunk_size, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(
        chunks(),
        status_code=206,
        media_type=media_type,
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )



@router.head("/{call_id}/audio", include_in_schema=False)
def call_audio_head(
    call_id: CallId,
    request: Request,
    db: DbSession,
    principal: OptionalPrincipal,
    download: Annotated[bool, Query()] = False,
    raw: Annotated[bool, Query()] = False,
    feed: Annotated[str | None, Query()] = None,
) -> Response:
    """HEAD twin of ``call_audio`` — same headers, no body; kept out of the schema so the
    generated operation ids stay deterministic (a two-method route names them from a set)."""
    return call_audio(call_id, request, db, principal, download, raw, feed)

@router.get(
    "/{call_id}/waveform",
    response_model=WaveformOut,
    summary="Peak envelope for the player",
    description=(
        "200 normalized peaks decoded with ffmpeg and cached on disk, so opening a call a second "
        "time costs one small file read. Peaks are normalized per call: these recordings are quiet "
        "and unevenly leveled, and an absolute scale would draw most of them flat. "
        "`refresh=1` discards the cache and runs ffmpeg again, which is the one expensive way to "
        "call this, so it needs the `write` scope."
    ),
)
def call_waveform(
    call_id: CallId,
    db: DbSession,
    principal: OptionalPrincipal,
    buckets: Annotated[int | None, Query(ge=16, le=2000, description="Peak buckets")] = None,
    refresh: Annotated[bool, Query(description="Ignore the cached file and recompute")] = False,
) -> WaveformOut:
    _withhold_hidden(db, call_id, principal)
    if refresh:
        # Reading a cached envelope is cheap; recomputing one is an ffmpeg decode, and an
        # anonymous caller could ask for it in a loop over every call in the archive.
        if principal is None:
            raise HTTPException(status_code=401, detail="Recomputing a waveform needs a credential")
        if "write" not in principal.scopes:
            raise HTTPException(status_code=403, detail="This action needs the 'write' scope")
    # The envelope is drawn under the player, and the player is playing the processed copy:
    # peaks from the original would put the waveform's silence where the audio has none.
    _, path, _processed = _audio_path(db, call_id)
    try:
        payload = waveform_service.waveform(call_id, path, buckets=buckets, refresh=refresh)
    except waveform_service.WaveformError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return WaveformOut.model_validate(payload)


@router.post(
    "/{call_id}/reprocess-audio",
    summary="Reprocess one call's audio (admin)",
    description=(
        "Queues this call for the audio-processing pass (§5.5) even if it already has a "
        "current processed copy — which is what makes it useful: it is how an operator "
        "re-renders one call after changing a setting, without touching the archive. "
        "Answers 409 when a pass is already queued or running for it, and 410 when there "
        "is no recording to process."
    ),
    responses={
        404: {"description": "No such call"},
        409: {"description": "A pass is already queued or running"},
        410: {"description": "The recording has been purged"},
    },
)
def reprocess_audio(call_id: CallId, db: DbSession, principal: AdminPrincipal) -> dict[str, Any]:
    from app.audioproc.jobs import enqueue as enqueue_audio  # noqa: PLC0415 - imports numpy

    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    if (call.audio_state or "present") not in ("present", "archived"):
        raise HTTPException(status_code=410, detail="the recording has been purged")
    # ``force`` on the enqueue is what lets this run while the feature is switched off: an
    # operator testing a setting on one call should not have to enable the whole stage
    # first.  It cannot bypass the open-job index, which is the 409 below.
    job_id = enqueue_audio(db, call, get_settings(), priority=1, force=True)
    if job_id is None:
        raise HTTPException(status_code=409, detail="a pass is already queued or running")
    db.commit()
    log.info("audio: %s queued call %s for reprocessing", principal.label, call_id)
    return {"call_id": call_id, "job_id": job_id, "status": "queued"}


@router.get(
    "/{call_id}/transcripts",
    response_model=list[TranscriptOut],
    summary="Every transcript of one call",
    description=(
        "Newest first. A call can carry several — different engines, different models, re-runs "
        "and a human correction — and exactly one is `is_primary`, which is the one denormalized "
        "onto the call and indexed for search."
    ),
)
def call_transcripts(call_id: CallId, db: DbSession, principal: OptionalPrincipal) -> list[TranscriptOut]:
    _withhold_hidden(db, call_id, principal)
    if db.get(Call, call_id) is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    rows = db.scalars(
        select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.created_at.desc())
    )
    out = [call_service.transcript_out(row) for row in rows]
    return out if principal is not None else [_for_guest(item) for item in out]


@router.post(
    "/{call_id}/transcribe",
    response_model=TranscribeAccepted,
    summary="Queue this call for transcription",
    description=(
        "Enqueues a transcription job, honouring the talkgroup's policy and the minimum duration "
        "unless `force` is set. `backend` pins an `STT_BACKENDS` entry; without it the router picks "
        "by priority, health and queue depth."
    ),
)
def transcribe_call(
    call_id: CallId, body: TranscribeRequest, db: DbSession, principal: WritePrincipal
) -> TranscribeAccepted:
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    talkgroup = db.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    settings = get_settings()

    if body.force:
        # Bypass the policy checks entirely: an operator asking again has already decided.
        # A partial unique index allows only one open job per call, so any job still queued
        # or running is canceled first — this request replaces it.
        #
        # The call row is written and locked before any of that (§5.2): ``calls`` first,
        # then ``transcription_jobs``, in every transaction that touches both.
        call.transcript_status = "queued"
        lock_calls(db, call.id)
        for open_job in db.scalars(
            select(TranscriptionJob).where(
                TranscriptionJob.call_id == call.id,
                TranscriptionJob.status.in_(("queued", "running")),
            )
        ):
            open_job.status = "cancelled"
            open_job.finished_at = datetime.now(UTC)
        db.flush()
        job = TranscriptionJob(
            call_id=call.id,
            status="queued",
            priority=body.priority if body.priority is not None else 1,
            backend_name=body.backend,
            model=body.model,
            max_attempts=settings.transcribe_max_attempts,
        )
        db.add(job)
        db.commit()
        notify(db, "call.updated", {"id": call.id})
        db.commit()
        return TranscribeAccepted(call_id=call.id, job_id=job.id, status="queued", detail="forced")

    job_id = enqueue_transcription(db, call, talkgroup, settings)
    if job_id is not None and (body.backend or body.model or body.priority is not None):
        job = db.get(TranscriptionJob, job_id)
        if job is not None:
            job.backend_name = body.backend or job.backend_name
            job.model = body.model or job.model
            job.priority = body.priority if body.priority is not None else job.priority
    db.commit()
    detail = None
    if job_id is None:
        detail = (
            "not queued: the talkgroup's policy or the minimum duration excludes it — retry with force"
            if call.transcript_status == "skipped"
            else "a job for this call is already queued or running"
        )
    return TranscribeAccepted(
        call_id=call.id, job_id=job_id, status=call.transcript_status, detail=detail
    )


@router.patch(
    "/{call_id}",
    response_model=CallDetail,
    summary="Edit notes, tags, the star, or correct the transcript",
    description=(
        "Only the keys present in the body are written. A `transcript_text` correction is stored "
        "on the **primary transcript** as `edited_text` with `edited_by_user=true` and denormalized "
        "onto the call, so the engine's own output is preserved and the correction is searchable."
    ),
)
def patch_call(
    call_id: CallId, body: CallPatch, db: DbSession, principal: WritePrincipal
) -> CallDetail:
    # The call row first (§5.2, :func:`app.db.lock_calls`): a correction races the
    # transcription worker's complete(), which decides under this same lock whether the
    # call already carries text a person stands behind.
    if not lock_calls(db, call_id):
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    call = db.get(Call, call_id)
    if call is None:  # pragma: no cover - locked a moment ago
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    fields = body.model_dump(exclude_unset=True)
    if "notes" in fields:
        call.notes = fields["notes"]
    if "tags" in fields:
        call.tags = sorted({t.strip() for t in (fields["tags"] or []) if t and t.strip()})
    if "starred" in fields:
        call.starred = bool(fields["starred"])
    if "transcript_text" in fields:
        corrected = fields["transcript_text"]
        primary = db.scalar(
            select(Transcript)
            .where(Transcript.call_id == call_id, Transcript.is_primary.is_(True))
            .limit(1)
        )
        if primary is None:
            primary = db.scalar(
                select(Transcript)
                .where(Transcript.call_id == call_id)
                .order_by(Transcript.created_at.desc())
                .limit(1)
            )
            if primary is not None:
                # No row was primary, so the one corrected becomes it: the worker protects
                # only a *primary* edited transcript, and would otherwise put the next
                # engine result over this correction.
                primary.is_primary = True
        captioned = False
        if primary is not None:
            primary.edited_text = corrected
            primary.edited_by_user = True
        elif corrected:
            # Nothing transcribed yet: the caption *is* the transcript.  A primary row of
            # its own, edited by a person, is what the worker's complete() keeps primary —
            # the engine's text is stored beside it, not over it.
            db.add(
                Transcript(
                    call_id=call.id,
                    engine="manual",
                    text_=corrected,
                    edited_text=corrected,
                    edited_by_user=True,
                    is_primary=True,
                )
            )
            captioned = True
        # Denormalized onto the call either way, so a call that was never transcribed can
        # still be captioned by hand and found by search.
        call.transcript_text = corrected
        if corrected and (
            captioned or call.transcript_status in ("pending", "skipped", "failed", "empty")
        ):
            call.transcript_status = "done"
    call.updated_at = datetime.now(UTC)
    db.commit()
    notify(db, "call.updated", {"id": call.id})
    db.commit()
    detail = call_service.call_detail(db, call_id)
    assert detail is not None  # noqa: S101 - the row was just written
    return detail


@router.post(
    "/{call_id}/make-primary",
    response_model=CallDetail,
    summary="Make this copy the primary one",
    description=(
        "Forces a swap: this call becomes the primary row of its pair, the copy it pointed at "
        "becomes the duplicate, everything that pointed at that copy is repointed here, and the "
        "conversation membership follows. The operator overruling the score — `reconcile_reason` "
        "records the ruling as `manual`. Answers 409 when the call is already the primary."
    ),
    responses={404: {"description": "No such call"}, 409: {"description": "Already the primary"}},
)
def make_primary_route(call_id: CallId, db: DbSession, principal: WritePrincipal) -> CallDetail:
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    try:
        make_primary(db, call, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    notify(db, "call.updated", {"id": call.id})
    db.commit()
    detail = call_service.call_detail(db, call_id)
    assert detail is not None  # noqa: S101 - the row was just written
    return detail


@router.delete(
    "/{call_id}",
    status_code=204,
    summary="Delete a call (admin)",
    description=(
        "Removes the row, its transcripts and its jobs. The MP3 on disk (and its processed "
        "copy) is left alone unless `delete_audio=1`: the ingest ledger would otherwise "
        "re-import it on the next scan. "
        "Answers 409 when the row went but the file could not — a read-only audio mount, "
        "which is how the API container is deployed."
    ),
)
def delete_call(
    call_id: CallId,
    db: DbSession,
    principal: AdminPrincipal,
    delete_audio: Annotated[bool, Query(description="Also unlink the MP3")] = False,
) -> Response:
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail=f"no call {call_id}")
    settings = effective_settings(db, base=get_settings())
    path = audio_path(call, settings) if delete_audio else None
    # The processed copy (§5.5) is derived from the recording and goes with it: leaving a
    # `.proc.mp3` behind would leave playable audio for a call the operator just deleted.
    derived = processed_path(call, settings) if delete_audio else None
    db.delete(call)
    db.commit()
    if delete_audio and derived is not None:
        with suppress(OSError):
            derived.unlink(missing_ok=True)
    if delete_audio and path is not None:
        try:
            path.unlink(missing_ok=True)
            # The partition this call sat in may now be empty — deleting the last 2022 call
            # of a bucket should not leave the bucket.  Never the root, and never a
            # directory that still holds a recording.
            prune_empty_dirs(path.parent, settings)
        except OSError as exc:
            # The API mounts the archive read-only in compose, so this is the normal
            # answer there rather than a surprise; the row is already gone either way.
            raise HTTPException(
                status_code=409,
                detail=f"call {call_id} was deleted but its recording could not be: {exc.strerror}",
            ) from exc
    return Response(status_code=204)


__all__ = ["exports_router", "router"]
