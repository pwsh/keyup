"""Operations: ingest, jobs, STT backends, settings, retention, tokens, users (§4.7).

Everything here is either read-only diagnostics — open like the rest of the archive — or an
action that changes the deployment, which needs the ``admin`` scope.  The distinction is
per route rather than per router because the Admin page shows the diagnostics to everyone
who can see the site and only refuses when a button is pressed.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import Float, case, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app import activity, runtime_settings
from app.api import admin_config
from app.auth import (
    COOKIE_NAME,
    AdminPrincipal,
    create_api_token,
    hash_password,
    normalize_scopes,
    require_scope,
    revoke_user_sessions,
)
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.ingest.flow import BACKLOG_BAND
from app.ingest.reconcile import last_pass_at
from app.ingest.service import INGEST_STATUS_KEY, ingest_status, ingest_tree
from app.models import (
    WEBHOOK_EVENTS,
    ApiToken,
    AudioProfile,
    Call,
    CallExport,
    IngestFile,
    IngestRun,
    Recorder,
    RetentionPolicy,
    Setting,
    Site,
    System,
    Talkgroup,
    TranscriptionJob,
    User,
    WebhookDelivery,
)
from app.schemas import (
    AdminIngestOut,
    ApiTokenCreated,
    ApiTokenIn,
    ApiTokenInfo,
    AudioPreviewIn,
    AudioPreviewOut,
    AudioProfileApplyIn,
    AudioProfileApplyOut,
    AudioProfileCatalogOut,
    AudioProfileIn,
    AudioProfileOut,
    AudioProfilePatch,
    AudioProfilePreviewIn,
    AudioRepairPrimariesIn,
    AudioRepairPrimariesOut,
    AudioReprocessIn,
    AudioReprocessOut,
    AudioStatusOut,
    BroadcastifyFeedStatus,
    ExportLedgerRow,
    ExportServiceStatus,
    ExportStatusOut,
    ExportTestRequest,
    ExportTestResult,
    IngestFileOut,
    IngestRunOut,
    IngestStatusOut,
    Page,
    RadioReferenceIdIn,
    RadioReferenceSyncOut,
    RadioReferenceSyncResult,
    RetentionPolicyIn,
    RetentionPolicyOut,
    RetentionRunResult,
    SettingDefinition,
    SiteOut,
    SttBackendEnabledIn,
    SttBackendOut,
    SttBackendsOut,
    SttBackendStatOut,
    SttQueueOut,
    SystemOut,
    TalkgroupExportFlagCopy,
    TalkgroupExportFlagCopyResult,
    TalkgroupOut,
    TranscriptionJobOut,
    TrunkedSystem,
    UserIn,
    UserOut,
    UserPatch,
    WebhookDeliveryOut,
    WebhookStatusOut,
)
from app.services import counts as count_service
from app.services import sites as site_service
from app.services import systems as system_service
from app.services.calls import like_contains
from app.stt import config_store, stats_store

log = logging.getLogger("app.api.admin")
#: the band /audio/repair-primaries queues at — the backlog band the general audio
#: backfill uses, because a repair is never more urgent than the traffic arriving while
#: it runs.
REPAIR_PRIMARIES_PRIORITY = 600
#: and the band /audio/profiles/{id}/apply queues at, for the same reason: re-rendering a
#: channel through a new filter is never more urgent than the call arriving while it runs.
PROFILE_APPLY_PRIORITY = 600

router = APIRouter(prefix="/admin", tags=["admin"])
#: ``/api/metrics`` (Prometheus).  It sits outside ``/api/admin`` but reports the same kind
#: of operator figures (job counts by status, ingest lag, disk under ``AUDIO_ROOT``), so it
#: needs any credential holding ``read``: a signed-in session, or a scraper's ``read``-scoped
#: API token sent as ``Authorization: Bearer`` — Prometheus's ``authorization`` block.
metrics_router = APIRouter(tags=["admin"], dependencies=[Depends(require_scope("read"))])
DbSession = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------


def ingest_snapshot(db: Session) -> IngestStatusOut:
    """The ingest picture: the ledger, the last run, the 24-hour counters and the lag."""
    settings = get_settings()
    raw = ingest_status(db)
    since = datetime.now(UTC) - timedelta(hours=24)
    runs_24h = db.execute(
        select(
            func.coalesce(func.sum(IngestRun.files_seen), 0),
            func.coalesce(func.sum(IngestRun.files_new), 0),
            func.coalesce(func.sum(IngestRun.files_duplicate), 0),
            func.coalesce(func.sum(IngestRun.files_failed), 0),
        ).where(IngestRun.started_at >= since)
    ).one()
    last_recorded = db.scalar(select(func.max(Call.recorded_at)))
    lag = None
    if last_recorded is not None:
        moment = last_recorded if last_recorded.tzinfo else last_recorded.replace(tzinfo=UTC)
        lag = round((datetime.now(UTC) - moment).total_seconds(), 1)
    last_run = raw.get("last_run") or {}
    ledger = {str(k): int(v) for k, v in (raw.get("ledger") or {}).items()}
    measured_at = count_service.cached_at(INGEST_STATUS_KEY)
    return IngestStatusOut(
        as_of=None if measured_at is None else datetime.fromtimestamp(measured_at, UTC),
        cached_s=None if measured_at is None else round(time.time() - measured_at, 1),
        watching=settings.ingest_watch,
        audio_root=str(settings.audio_root),
        last_run_at=last_run.get("started_at"),
        last_run_files_new=int(last_run.get("files_new") or 0),
        files_seen_24h=int(runs_24h[0]),
        files_new_24h=int(runs_24h[1]),
        files_duplicate_24h=int(runs_24h[2]),
        files_failed_24h=int(runs_24h[3]),
        lag_s=lag,
        pending_files=int(ledger.get("failed", 0)),
        calls=int(raw.get("calls") or 0),
        duplicates=int(raw.get("duplicates") or 0),
        ledger=ledger,
        jobs={str(k): int(v) for k, v in (raw.get("jobs") or {}).items()},
    )


@router.get(
    "/ingest",
    response_model=AdminIngestOut,
    summary="Ingest health and recent runs",
    description="The ledger, the last twenty scan/watch runs and the lag behind the recorder.",
)
def admin_ingest(db: DbSession) -> AdminIngestOut:
    runs = [
        IngestRunOut(
            id=r.id,
            kind=r.kind,
            root=r.root,
            started_at=r.started_at,
            finished_at=r.finished_at,
            files_seen=r.files_seen,
            files_new=r.files_new,
            files_duplicate=r.files_duplicate,
            files_skipped=r.files_skipped,
            files_failed=r.files_failed,
            errors=list(r.errors or ()),
        )
        for r in db.scalars(select(IngestRun).order_by(IngestRun.started_at.desc()).limit(20))
    ]
    return AdminIngestOut(status=ingest_snapshot(db), runs=runs)


_rescan_lock = threading.Lock()
_rescan_thread: threading.Thread | None = None


@router.post(
    "/ingest/rescan",
    summary="Rescan the recording tree (admin)",
    description=(
        "Walks `AUDIO_ROOT` in a background thread. Safe to run at any time: the ledger is the "
        "skip list, so files already ingested are not read again. One rescan at a time."
    ),
)
def rescan(
    principal: AdminPrincipal,
    root: Annotated[str | None, Query(description="Subtree to scan (default AUDIO_ROOT)")] = None,
    workers: Annotated[int, Query(ge=1, le=16)] = 4,
) -> dict[str, Any]:
    global _rescan_thread  # noqa: PLW0603 - one process-wide rescan is the point
    settings = get_settings()
    target = Path(root) if root else Path(settings.audio_root)
    with _rescan_lock:
        if _rescan_thread is not None and _rescan_thread.is_alive():
            return {"queued": False, "detail": "a rescan is already running", "root": str(target)}

        def work() -> None:
            try:
                summary = ingest_tree(SessionLocal, target, workers=workers, settings=settings)
                log.info("admin.rescan.done new=%d seen=%d", summary.files_new, summary.files_seen)
            except Exception:  # noqa: BLE001 - a background failure belongs in the log
                log.exception("admin.rescan.failed root=%s", target)

        _rescan_thread = threading.Thread(target=work, name="admin-rescan", daemon=True)
        _rescan_thread.start()
    return {"queued": True, "root": str(target), "workers": workers}


@router.get(
    "/ingest/files",
    response_model=Page[IngestFileOut],
    summary="The per-file ingest ledger",
    description=(
        "Every file the ingest has seen and what happened to it. `status=failed` is the useful one."
    ),
)
def ingest_files(
    db: DbSession,
    response: Response,
    status: Annotated[str | None, Query(description="ingested/duplicate/failed/ignored")] = None,
    q: Annotated[str | None, Query(description="Substring of the path")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[IngestFileOut]:
    conds = []
    if status:
        conds.append(IngestFile.status == status)
    if q:
        conds.append(IngestFile.file_path.ilike(like_contains(q), escape="\\"))
    total = int(db.scalar(select(func.count()).select_from(IngestFile).where(*conds)) or 0)
    rows = db.scalars(
        select(IngestFile)
        .where(*conds)
        .order_by(IngestFile.last_attempt_at.desc().nullslast())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[IngestFileOut](
        items=[
            IngestFileOut(
                id=r.id,
                path=r.file_path,
                status=r.status,
                error=r.error,
                call_id=r.call_id,
                attempts=r.attempts,
                first_seen_at=r.first_seen_at,
                last_attempt_at=r.last_attempt_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/ingest/files/{file_id}/retry",
    response_model=IngestFileOut,
    summary="Re-ingest one failed file (admin)",
)
def retry_ingest_file(file_id: int, db: DbSession, principal: AdminPrincipal) -> IngestFileOut:
    from app.ingest.service import ingest_file  # noqa: PLC0415 - heavy import, one route

    row = db.get(IngestFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no ingest file {file_id}")
    settings = get_settings()
    full = Path(settings.audio_root) / row.file_path
    if not full.is_file():
        raise HTTPException(status_code=410, detail=f"{row.file_path} is no longer on disk")
    ingest_file(db, full, settings=settings, audio_root=Path(settings.audio_root))
    db.commit()
    db.refresh(row)
    return IngestFileOut(
        id=row.id,
        path=row.file_path,
        status=row.status,
        error=row.error,
        call_id=row.call_id,
        attempts=row.attempts,
        first_seen_at=row.first_seen_at,
        last_attempt_at=row.last_attempt_at,
    )


# --------------------------------------------------------------------------------------
# transcription jobs
# --------------------------------------------------------------------------------------


@router.get(
    "/jobs",
    response_model=Page[TranscriptionJobOut],
    summary="Transcription jobs",
    description="The queue. Counts per status are in the `X-Job-Counts` header as JSON.",
)
def list_jobs(
    db: DbSession,
    response: Response,
    status: Annotated[
        list[str] | None, Query(description="queued/running/done/failed/cancelled")
    ] = None,
    call: Annotated[int | None, Query(description="Only jobs for this call")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[TranscriptionJobOut]:
    conds = []
    if status:
        conds.append(TranscriptionJob.status.in_([s.lower() for s in status]))
    if call:
        conds.append(TranscriptionJob.call_id == call)
    total = int(db.scalar(select(func.count()).select_from(TranscriptionJob).where(*conds)) or 0)
    rows = list(
        db.execute(
            select(TranscriptionJob, Talkgroup.alias)
            .outerjoin(Call, TranscriptionJob.call_id == Call.id)
            .outerjoin(Talkgroup, Call.talkgroup_id == Talkgroup.id)
            .where(*conds)
            .order_by(TranscriptionJob.scheduled_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    counts = dict(
        db.execute(select(TranscriptionJob.status, func.count()).group_by(TranscriptionJob.status)).all()
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Job-Counts"] = json.dumps({str(k): int(v) for k, v in counts.items()})
    return Page[TranscriptionJobOut](
        items=[
            TranscriptionJobOut(
                id=job.id,
                call_id=job.call_id,
                talkgroup_alias=alias,
                status=job.status,
                priority=job.priority,
                backend_name=job.backend_name,
                model=job.model,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                last_error=job.last_error,
                scheduled_at=job.scheduled_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                locked_by=job.locked_by,
            )
            for job, alias in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/jobs/requeue",
    summary="Requeue failed transcription jobs (admin)",
    description=(
        "Moves `failed` jobs back to `queued` and resets their attempt counter, so a backend that "
        "was down can be retried without touching the database. Pass `job_ids` for a subset. "
        "Requeues every failed job whatever parked it — the manual override over the automatic "
        "pass, which only ever moves the jobs an STT outage parked (§5.2)."
    ),
)
def requeue_jobs(
    db: DbSession,
    principal: AdminPrincipal,
    body: Annotated[dict[str, Any] | None, Body(description='{"job_ids": [1, 2]}')] = None,
) -> dict[str, int]:
    # The same function the automatic unpark pass calls, with the operator's arguments: no
    # reason filter (they asked for all of them), no batch limit, and the error cleared
    # because they have already read it.
    from app.stt.worker import requeue_failed  # noqa: PLC0415 - avoids an import cycle

    job_ids = (body or {}).get("job_ids") or []
    moved = requeue_failed(db, job_ids=job_ids or None)
    db.commit()
    return {"requeued": moved}


# --------------------------------------------------------------------------------------
# STT backends
# --------------------------------------------------------------------------------------


#: how the queue block's "lately" numbers are windowed
QUEUE_RECENT_MINUTES = 10


def _queue_block(db: Session, *, now: datetime | None = None) -> SttQueueOut:
    """The queue as ``transcription_jobs`` has it — not as any one process remembers it.

    Everything else on this page is one worker's memory, which is why it disappears when
    that worker restarts.  These four counts and the rate derived from them are the
    database's own answer, so they stay true across a restart and across two workers.
    """
    stamp = now or datetime.now(UTC)
    hour_ago = stamp - timedelta(hours=1)
    recent = stamp - timedelta(minutes=QUEUE_RECENT_MINUTES)
    live_band = case((TranscriptionJob.priority < BACKLOG_BAND, 1), else_=0)

    open_rows = db.execute(
        select(
            TranscriptionJob.status,
            live_band.label("live"),
            func.count().label("jobs"),
            func.coalesce(func.sum(Call.duration_s), 0).label("audio_s"),
        )
        .select_from(TranscriptionJob)
        .join(Call, Call.id == TranscriptionJob.call_id)
        .where(TranscriptionJob.status.in_(("queued", "running")))
        .group_by(TranscriptionJob.status, live_band)
    ).all()
    queued_live = queued_backlog = running = 0
    audio_queued = 0.0
    for status, is_live, jobs, audio_s in open_rows:
        audio_queued += float(audio_s or 0.0)
        if status == "running":
            running += int(jobs)
        elif is_live:
            queued_live += int(jobs)
        else:
            queued_backlog += int(jobs)

    # The last hour and the last ten minutes in one pass over the hour, which the partial
    # ``ix_transcription_jobs_finished`` (finished_at WHERE status IN ('done', 'failed'))
    # hands back as a short range scan; without it both were sequential scans of every
    # job ever run.
    finished = db.execute(
        select(
            TranscriptionJob.status,
            func.count().label("jobs"),
            func.avg(
                func.extract("epoch", TranscriptionJob.finished_at - TranscriptionJob.started_at)
            ).label("mean_s"),
            func.count().filter(TranscriptionJob.finished_at >= recent).label("recent"),
        )
        .where(
            TranscriptionJob.status.in_(("done", "failed")),
            TranscriptionJob.finished_at >= hour_ago,
        )
        .group_by(TranscriptionJob.status)
    ).all()
    completed_1h = failed_1h = done_recent = 0
    mean_processing_s: float | None = None
    for status, jobs, mean_s, in_recent in finished:
        if status == "done":
            completed_1h = int(jobs)
            mean_processing_s = float(mean_s) if mean_s is not None else None
            done_recent = int(in_recent or 0)
        else:
            failed_1h = int(jobs)
    per_min = done_recent / QUEUE_RECENT_MINUTES
    outstanding = queued_live + queued_backlog + running
    # Jobs per minute rather than audio seconds per wall second: it is the rate this
    # deployment has actually achieved, whatever mix of backends and audio lengths made it.
    eta = (outstanding / per_min) * 60.0 if per_min > 0 and outstanding else None
    return SttQueueOut(
        queued_live=queued_live,
        queued_backlog=queued_backlog,
        running=running,
        completed_1h=completed_1h,
        failed_1h=failed_1h,
        mean_processing_s=round(mean_processing_s, 3) if mean_processing_s is not None else None,
        throughput_per_min=round(per_min, 2),
        audio_seconds_queued=round(audio_queued, 1),
        eta_seconds=round(eta, 1) if eta is not None else None,
    )


#: Which backends exist, and how they read on the page, is now decided by the table the
#: admin CRUD writes — so both live next to that CRUD in :mod:`app.api.admin_config` and are
#: imported here rather than duplicated.  A second copy of the precedence rule is the one
#: way this could go quietly wrong.
_configured_backends = admin_config.configured_backends
_backend_rows = admin_config.backend_rows


@router.get(
    "/stt/backends",
    response_model=SttBackendsOut,
    summary="Speech-to-text backends, their counters and the queue (admin)",
    description=(
        "What the transcription worker is doing, read from `stt_backend_stats` — the table "
        "the worker publishes to every few seconds — plus the queue counted in SQL from "
        "`transcription_jobs`. Nothing here probes a backend or depends on this process "
        "having a router, which is why it reports the work that is actually happening. "
        "`stale` on a row (and `reporting: false` overall) means the worker has stopped "
        "publishing, not that the backend is idle."
    ),
)
def stt_backends(db: DbSession, principal: AdminPrincipal) -> SttBackendsOut:
    now = datetime.now(UTC)
    rows = _backend_rows(db, now=now)
    fresh = [row for row in rows if not row.stale]
    last_report = max((row.updated_at for row in rows if row.updated_at), default=None)
    reporter = fresh[0] if fresh else None
    # The two versions are the page's answer to "has my edit taken effect yet".  The
    # applied one is read from the freshest reporting row rather than from any of them: a
    # stale row's version describes a worker that is no longer running.
    return SttBackendsOut(
        backends=rows,
        queue=_queue_block(db, now=now),
        reporting=bool(fresh),
        worker_instance=reporter.worker_instance if reporter else None,
        worker_started_at=reporter.started_at if reporter else None,
        last_report_at=last_report,
        config_version=config_store.version(db),
        applied_config_version=reporter.config_version if reporter else None,
        from_environment=not any(row.editable for row in rows),
    )


@router.put(
    "/stt/backends/{name}/enabled",
    response_model=SttBackendStatOut,
    summary="Enable or disable one speech-to-text backend at runtime (admin)",
    description=(
        "Overrides the backend's configured `enabled` flag without editing it. `null` "
        "clears the override and goes back to following the configuration. The worker's "
        "router re-reads this within a few seconds — faster than a configuration change, "
        "which waits for the router to be rebuilt — and stops picking a backend that is "
        "off; an attempt already in flight finishes normally. Disabling every backend "
        "pauses transcription: jobs wait in the queue rather than failing. `PUT "
        "/admin/stt/backends/{name}` is the *configuration*; this is the switch on it."
    ),
)
def set_stt_backend_enabled(
    db: DbSession,
    principal: AdminPrincipal,
    name: str,
    body: SttBackendEnabledIn,
) -> SttBackendStatOut:
    if name not in _configured_backends(db):
        raise HTTPException(status_code=404, detail=f"no STT backend named {name!r}")
    stats_store.set_override(db, name, body.enabled, changed_by=principal.label)
    if body.enabled is None:
        kind, message = "stt.backend_override_cleared", f"backend {name} follows STT_BACKENDS again"
    elif body.enabled:
        kind, message = "stt.backend_enabled", f"backend {name} enabled by {principal.label}"
    else:
        kind, message = "stt.backend_disabled", f"backend {name} disabled by {principal.label}"
    activity.record(
        db,
        source="stt",
        level="info",
        kind=kind,
        message=message,
        meta={"backend": name, "enabled": body.enabled, "by": principal.label},
    )
    db.commit()
    rows = {row.name: row for row in _backend_rows(db)}
    return rows[name]


@router.get(
    "/backends",
    response_model=list[SttBackendOut],
    summary="Configured speech-to-text backends and their health (admin)",
    description=(
        "The compact, older view of the same rows `GET /admin/stt/backends` serves: one "
        "entry per configured backend with its health and rolling real-time factor as the "
        "worker last published them. It no longer probes anything from this process — the "
        "counters an API process builds for itself are structurally zero, which is what "
        "made this endpoint report an idle system while the worker was busy."
    ),
)
def backends(db: DbSession, principal: AdminPrincipal) -> list[SttBackendOut]:
    since = datetime.now(UTC) - timedelta(hours=24)
    from app.models import Transcript  # noqa: PLC0415

    calls_24h = dict(
        db.execute(
            select(Transcript.backend_name, func.count())
            .where(Transcript.created_at >= since)
            .group_by(Transcript.backend_name)
        ).all()
    )
    queued = int(
        db.scalar(
            select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == "queued")
        )
        or 0
    )
    return [
        SttBackendOut(
            name=row.name,
            engine=row.kind or "openai_compatible",
            model=row.model,
            base_url=row.base_url,
            enabled=row.enabled,
            priority=row.priority,
            concurrency=row.concurrency,
            # A stale row's health is old news, and reporting it as fact would keep a green
            # dot on a backend nobody has heard from since the worker died.
            healthy=None if row.stale else row.healthy,
            detail="worker not reporting" if row.stale else (row.health_detail or None),
            last_error=row.last_error,
            rtf=row.mean_rtf,
            queue_depth=queued,
            calls_24h=int(calls_24h.get(row.name, 0)),
        )
        for row in _backend_rows(db)
    ]


# --------------------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------------------

#: The whitelist of runtime settings the UI may change lives with the code that *reads*
#: them (``app.runtime_settings``), so a key can only appear here once something consumes
#: it: the database URL, the audio root and the secret key belong to the deployment, and a
#: knob that needed a restart to take effect would be worse than no knob at all.
SETTINGS_SPEC = runtime_settings.SETTINGS_SPEC


def _coerce(kind: str, raw: str, key: str = "") -> Any:
    """One string from the UI into the type the setting is stored as.

    ``enum`` is checked against :data:`app.runtime_settings.SETTINGS_OPTIONS` rather than
    taken on trust: an unknown value would be layered onto :class:`Settings` and then
    rejected by pydantic, which drops *the whole override set* — one typo would silently
    revert every other runtime setting at once.
    """
    if kind == "enum":
        options = runtime_settings.SETTINGS_OPTIONS.get(key, ())
        value = str(raw).strip()
        if options and value not in options:
            raise ValueError(f"not one of {', '.join(options)}")
        return value
    if kind == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def _to_str(kind: str, value: Any) -> str:
    """Serialize a stored value for the UI.

    Booleans in particular must go out as lowercase ``true``/``false`` — the value is a
    JSONB Python ``bool`` and a plain ``str(True)`` is ``"True"``, which the Switch (it
    compares against ``"true"``) reads as off, so a saved toggle looked like it cleared.

    A ``secret`` never goes out at all (:data:`app.runtime_settings.WRITE_ONLY_KINDS`).
    Reads of this archive are open by default and this endpoint is one of them, so a
    signing key that could be *read* back would be a signing key anybody who can see the
    site can forge with.  ``is_default`` still says whether one is set, which is the whole
    of what a reader needs; setting a new one overwrites it, and clearing the field
    removes it.
    """
    if kind in runtime_settings.WRITE_ONLY_KINDS:
        return ""
    if value is None:
        return ""
    if kind == "bool":
        return "true" if _coerce("bool", value) else "false"
    return str(value)


def _setting_rows(db: Session) -> list[SettingDefinition]:
    settings = get_settings()
    overrides = runtime_settings.read_overrides(db)
    out = []
    for key, kind, group, label, description in SETTINGS_SPEC:
        default = getattr(settings, key, None)
        override = overrides.get(key)
        out.append(
            SettingDefinition(
                key=key,
                value=_to_str(kind, override if override is not None else default),
                kind=kind,
                group=group,
                label=label,
                description=description,
                options=list(runtime_settings.SETTINGS_OPTIONS.get(key, ())) or None,
                default_value=_to_str(kind, default),
                is_default=override is None,
            )
        )
    return out


@router.get(
    "/settings",
    response_model=list[SettingDefinition],
    summary="Runtime settings",
    description=(
        "The whitelist of settings the UI may change, each with its environment default and "
        "whether a database override is currently in force. Deployment-level values — the database "
        "URL, the audio root, the secret key — are deliberately not exposed here."
    ),
)
def get_admin_settings(db: DbSession) -> list[SettingDefinition]:
    return _setting_rows(db)


def _check_rnnoise_model(db: Session, body: dict[str, Any]) -> None:
    """Refuse ``audio_noise_reduction=arnndn`` when there is no model file to load.

    ``arnndn`` needs an RNNoise model on disk; none ships in the image, because the ones
    that exist are not redistributable through pip and this deployment builds offline.
    Without this check the setting would save, the chain would silently fall back to
    ``afftdn``, and the Admin page would show a denoiser that is not the one running — so
    it is refused here with the reason, and the runtime fallback (which is still there,
    with a logged warning) only ever covers a model that vanished after it was saved.
    """
    wanted = str(body.get("audio_noise_reduction", "")).strip().lower()
    if wanted != "arnndn":
        return
    model = str(body.get("audio_rnnoise_model", "")).strip()
    if not model:
        settings = runtime_settings.effective_settings(db, base=get_settings())
        model = str(getattr(settings, "audio_rnnoise_model", "") or "").strip()
    if model and Path(model).is_file():
        return
    raise HTTPException(
        status_code=422,
        detail=(
            "audio_noise_reduction: arnndn needs a model file. Set audio_rnnoise_model to a "
            "readable RNNoise model inside the container, or choose afftdn."
            if model
            else "audio_noise_reduction: arnndn needs a model file and audio_rnnoise_model is "
            "not set. Set it to a readable RNNoise model inside the container, or choose afftdn."
        ),
    )


@router.put(
    "/settings",
    response_model=list[SettingDefinition],
    summary="Change runtime settings (admin)",
    description=(
        "Body is `{key: value}` with values as strings. Only whitelisted keys are accepted; an "
        "empty string removes the override and restores the environment default. Workers read the "
        "`settings` table on their next cycle."
    ),
)
def put_admin_settings(
    body: Annotated[dict[str, Any], Body(description='{"stt_language": "en"}')],
    db: DbSession,
    principal: AdminPrincipal,
) -> list[SettingDefinition]:
    known = {key: kind for key, kind, *_ in SETTINGS_SPEC}
    unknown = sorted(set(body) - set(known))
    if unknown:
        raise HTTPException(status_code=422, detail=f"not a settable key: {', '.join(unknown)}")
    _check_rnnoise_model(db, body)
    for key, raw in body.items():
        row = db.get(Setting, key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if row is not None:
                if key == "retention_archive_path":
                    # Cleared, not forgotten: calls archived under it must stay playable.
                    from app.retention import remember_archive_root  # noqa: PLC0415

                    remember_archive_root(db, str(row.value or ""))
                db.delete(row)
            continue
        try:
            value = _coerce(known[key], raw, key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{key}: {exc}") from exc
        choices = runtime_settings.SETTINGS_OPTIONS.get(key)
        if choices is not None and value not in choices:
            raise HTTPException(
                status_code=422, detail=f"{key}: must be one of {', '.join(choices)}"
            )
        low, high = runtime_settings.SETTINGS_BOUNDS.get(key, (None, None))
        if (low is not None and value < low) or (high is not None and value > high):
            raise HTTPException(status_code=422, detail=f"{key}: must be between {low} and {high}")
        if key == "retention_archive_path":
            # The same check a policy's archive path gets, and the same history: the old
            # value stays an allowed audio root, so what was archived under it still plays.
            from app.retention import remember_archive_root, validate_archive_path  # noqa: PLC0415

            try:
                value = validate_archive_path(str(value), get_settings())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{key}: {exc}") from exc
            previous = row.value if row is not None else get_settings().retention_archive_path
            remember_archive_root(db, str(previous or ""))
            remember_archive_root(db, value)
        if row is None:
            row = Setting(key=key, value=value)
            db.add(row)
        else:
            row.value = value
        row.updated_by = principal.label
    db.commit()
    # Every consumer reads through app.runtime_settings, which caches for a few seconds;
    # dropping it here makes the change take effect on the next request instead.
    runtime_settings.invalidate()
    # The HTTPS group is the one whose consumer is another container: it is rendered into a
    # Caddyfile and pushed into the running edge (docs/HTTPS.md).  Best effort — a proxy that
    # cannot be reached is reported by GET /admin/https/status, not by failing the save the
    # operator has just made.
    if any(key.startswith("tls_") for key in body):
        from app.edge import service as edge_service  # noqa: PLC0415

        edge_service.apply_now(db, reason=f"settings:{principal.label}")
    return _setting_rows(db)


# --------------------------------------------------------------------------------------
# upload flow control (docs/RECORDERS.md "Priority and flow control")
# --------------------------------------------------------------------------------------


def _set_pause_backlog(db: Session, paused: bool, principal: Any) -> dict[str, Any]:
    """Write ``upload_pause_backlog`` and hand back the gauge as it now reads.

    It goes in the ``settings`` table rather than a module flag because the API runs as more
    than one process: a switch only the process that received the request could see would be
    a switch that half the uploads ignore.
    """
    from app.ingest import flow  # noqa: PLC0415 - avoids an import cycle at module load

    row = db.get(Setting, "upload_pause_backlog")
    if row is None:
        row = Setting(key="upload_pause_backlog", value=paused)
        db.add(row)
    else:
        row.value = paused
    row.updated_by = principal.label
    db.commit()
    runtime_settings.invalidate()
    settings = runtime_settings.effective_settings(db, base=get_settings())
    view = flow.gauge(db, settings, force=True)
    log.info("upload: backlog ingestion %s by %s", "paused" if paused else "resumed", principal.label)
    return {"paused_backlog": paused, "queue": view.as_dict()}


@router.post(
    "/upload/pause-backlog",
    summary="Stop accepting backlog/bulk uploads (admin)",
    description=(
        "Refuses every `priority=backlog|bulk` push with 429 until resumed; `live` and `urgent` "
        "are unaffected, and calls already stored keep their queued transcriptions. The switch is "
        "the `upload_pause_backlog` runtime setting, so it survives a restart and every API "
        "process sees it."
    ),
)
def pause_backlog(db: DbSession, principal: AdminPrincipal) -> dict[str, Any]:
    return _set_pause_backlog(db, True, principal)


@router.post(
    "/upload/resume-backlog",
    summary="Accept backlog/bulk uploads again (admin)",
    description="Clears `upload_pause_backlog`; the queue caps and the ETA ceiling still apply.",
)
def resume_backlog(db: DbSession, principal: AdminPrincipal) -> dict[str, Any]:
    return _set_pause_backlog(db, False, principal)


# --------------------------------------------------------------------------------------
# audio processing (docs/ARCHITECTURE.md 5.5)
# --------------------------------------------------------------------------------------

#: Preview renders, by token.  In-process and deliberately small: a preview is something an
#: operator listens to once while tuning a setting, so it lives as long as the API process
#: and no longer, and a restart simply means pressing the button again.  Files go to the
#: system temp directory, never into the archive — nothing here may write beside a call.
_PREVIEWS: dict[str, Path] = {}
PREVIEW_KEEP = 12


def _audio_runtime(db: Session) -> Any:
    return runtime_settings.effective_settings(db, base=get_settings())


def _audio_on(settings: Any) -> bool:
    """Is the processing stage switched on — the check ``enqueue`` itself makes."""
    return bool(getattr(settings, "audio_process_enabled", True))


#: The answer a bulk audio queue route gives while the stage is off.  With the switch off
#: the worker cancels every ``queued`` job on its next idle cycle (``_cancel_queued`` in
#: app/audioproc/jobs.py), so queueing a backfill then is a table's worth of rows written
#: only to be cancelled — the shape of the 450,962-job repair on 2026-09-23.
AUDIO_OFF_DETAIL = "audio processing is switched off; turn it on before queueing a backfill"


@router.get(
    "/audio",
    response_model=AudioStatusOut,
    summary="Audio-processing status",
    description=(
        "The audio queue and what it has covered: jobs by status, how many calls have a "
        "current processed copy and how many are still owed one, today's tone and page "
        "counts, and the mean gain the pass has been applying. Read-only."
    ),
)
def audio_status(db: DbSession) -> AudioStatusOut:
    from app.audioproc.chain import PIPELINE_VERSION  # noqa: PLC0415 - imports numpy

    settings = _audio_runtime(db)
    figures = count_service.cached(
        AUDIO_STATUS_KEY, AUDIO_STATUS_TTL_S, lambda: _audio_figures(db, settings.tz)
    )
    jobs = figures["jobs"]
    return AudioStatusOut(
        enabled=bool(getattr(settings, "audio_process_enabled", True)),
        version=PIPELINE_VERSION,
        reprocess_version=int(getattr(settings, "audio_reprocess_version", PIPELINE_VERSION) or 1),
        jobs=jobs,
        processed=figures["processed"],
        pending=figures["pending"],
        failed=int(jobs.get("failed", 0)),
        tone_today=figures["tone_today"],
        alert_today=figures["alert_today"],
        average_gain_db=figures["average_gain_db"],
    )


#: Cache key and TTL for the counts behind ``GET /admin/audio`` (app/services/counts.py).
#: The two coverage counts are full scans of ``calls`` — ``audio_state IN ('present',
#: 'archived')`` has no index, the partial ``ix_calls_audio_state`` covers the other side —
#: and they took about four seconds on production.  The tab polls once a minute; the queue
#: routes below drop the entry so a backfill someone just queued shows at once.
AUDIO_STATUS_KEY = "admin.audio"
AUDIO_STATUS_TTL_S = 60.0


def _audio_figures(db: Session, tz: str) -> dict[str, Any]:
    """The uncached measurement behind :func:`audio_status`."""
    from app.audioproc.jobs import counts as audio_counts  # noqa: PLC0415

    jobs = audio_counts(db)
    have_audio = Call.audio_state.in_(("present", "archived"))
    processed = db.execute(
        select(func.count()).select_from(Call).where(have_audio, Call.processed_path.is_not(None))
    ).scalar_one()
    total = db.execute(select(func.count()).select_from(Call).where(have_audio)).scalar_one()
    midnight = datetime.now(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    tone_today, alert_today = db.execute(
        select(
            func.count().filter(Call.tone_detected),
            func.count().filter(Call.alert),
        ).where(Call.started_at >= midnight)
    ).one()
    # Averaged over the recent tail rather than the archive: the number is there to answer
    # "is the current setting lifting things sensibly", and a year of old passes would bury
    # the effect of a change made this morning.
    recent = select(Call.audio_meta["gain_db"].astext.cast(Float).label("gain")).where(
        Call.audio_meta.is_not(None)
    ).order_by(Call.started_at.desc()).limit(1000).subquery()
    average = db.execute(select(func.avg(recent.c.gain))).scalar_one_or_none()
    return {
        "jobs": {str(k): int(v) for k, v in jobs.items()},
        "processed": int(processed),
        "pending": max(0, int(total) - int(processed)),
        "tone_today": int(tone_today or 0),
        "alert_today": int(alert_today or 0),
        "average_gain_db": None if average is None else round(float(average), 2),
    }


@router.post(
    "/audio/reprocess",
    response_model=AudioReprocessOut,
    summary="Queue an audio-processing backfill (admin)",
    description=(
        "Queues calls for the processing pass at the backlog band, so a backfill over the "
        "archive never delays a call arriving now. Without `force`, only calls with no "
        "processed copy or one from an older pipeline are queued — which makes the call "
        "safe to repeat and makes 'raise `audio_reprocess_version`, then post this' the "
        "whole procedure for applying new settings to the archive. `only_tone_flagged` "
        "narrows it to calls the stored pass heard a tone in, which is the selection for "
        "a change to the tone detector. 409 while audio processing is switched off: the "
        "worker cancels queued jobs then, so nothing is queued."
    ),
    responses={409: {"description": "Audio processing is switched off"}},
)
def audio_reprocess(
    body: AudioReprocessIn, db: DbSession, principal: AdminPrincipal
) -> AudioReprocessOut:
    from app.audioproc.jobs import enqueue_many, select_for_reprocess  # noqa: PLC0415 - numpy

    settings = _audio_runtime(db)
    if not _audio_on(settings):
        raise HTTPException(409, AUDIO_OFF_DETAIL)
    moment = None
    if body.since:
        try:
            moment = datetime.fromisoformat(body.since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, f"{body.since!r} is not an ISO date") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(settings.tz))
    call_ids = select_for_reprocess(
        db,
        settings,
        since=moment,
        talkgroup=body.talkgroup,
        ids=body.ids,
        only_tone_flagged=body.only_tone_flagged,
        force=body.force,
        limit=body.limit,
    )
    queued = enqueue_many(
        db,
        call_ids,
        priority=body.priority,
        max_attempts=int(getattr(settings, "audio_process_max_attempts", 3) or 3),
    )
    db.commit()
    count_service.invalidate(AUDIO_STATUS_KEY)  # the tab shows the new queue at once
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.reprocess",
        message=f"{principal.label} queued {queued} call(s) for audio processing",
    )
    db.commit()
    log.info("audio: %s queued %d call(s) for reprocessing", principal.label, queued)
    return AudioReprocessOut(
        matched=len(call_ids),
        queued=queued,
        already_open=len(call_ids) - queued,
        priority=body.priority,
        force=body.force,
    )


@router.post(
    "/audio/repair-primaries",
    response_model=AudioRepairPrimariesOut,
    summary="Queue audio processing for primaries a reconciliation swap left behind (admin)",
    description=(
        "Sweeps for calls that are primary, have audio, have at least one copy, and have no "
        "processed file, and queues them at the backlog band (600). These are the rows a "
        "reconciliation swap created before the swap re-queued the pass: the processing job "
        "was queued for whichever copy was primary at ingest, so when the seat moved the row "
        "now being played was left unprocessed while its hidden copy holds a file nobody can "
        "reach. Safe to repeat — a call with an open job is skipped by the queue's own "
        "partial index — and a no-op once the archive is clean. 409 while audio "
        "processing is switched off."
    ),
    responses={409: {"description": "Audio processing is switched off"}},
)
def audio_repair_primaries(
    body: AudioRepairPrimariesIn, db: DbSession, principal: AdminPrincipal
) -> AudioRepairPrimariesOut:
    from app.audioproc.jobs import enqueue_many  # noqa: PLC0415 - imports numpy

    settings = _audio_runtime(db)
    if not _audio_on(settings):
        raise HTTPException(409, AUDIO_OFF_DETAIL)
    moment = None
    if body.since:
        try:
            moment = datetime.fromisoformat(body.since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, f"{body.since!r} is not an ISO date") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(settings.tz))

    # "Has a copy" is what separates this from the general backfill: a primary with no
    # processed file and no copies simply has not reached the queue yet, and sweeping those
    # in would make the count meaningless as a measure of the bug.
    has_copy = select(Call.duplicate_of).where(Call.duplicate_of.is_not(None)).distinct()
    stmt = select(Call.id).where(
        Call.duplicate_of.is_(None),
        Call.audio_state.in_(("present", "archived")),
        Call.processed_path.is_(None),
        Call.id.in_(has_copy),
    )
    if moment is not None:
        stmt = stmt.where(Call.started_at >= moment)
    call_ids = list(db.scalars(stmt.order_by(Call.started_at.desc()).limit(body.limit)).all())

    queued = enqueue_many(
        db,
        call_ids,
        priority=REPAIR_PRIMARIES_PRIORITY,
        max_attempts=int(getattr(settings, "audio_process_max_attempts", 3) or 3),
    )
    db.commit()
    count_service.invalidate(AUDIO_STATUS_KEY)  # the tab shows the new queue at once
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.repair_primaries",
        message=f"{principal.label} queued {queued} demoted-primary call(s) for audio processing",
    )
    db.commit()
    log.info(
        "audio: %s repaired %d primary/primaries missing processed audio (matched %d)",
        principal.label,
        queued,
        len(call_ids),
    )
    return AudioRepairPrimariesOut(
        matched=len(call_ids),
        queued=queued,
        already_open=len(call_ids) - queued,
        priority=REPAIR_PRIMARIES_PRIORITY,
        since=body.since,
    )


@router.post(
    "/audio/preview",
    response_model=AudioPreviewOut,
    summary="Run the chain on one call without storing it (admin)",
    description=(
        "Runs the whole processing chain over one call with the settings as they stand and "
        "hands back the measurements and a temporary URL to hear the result. Nothing is "
        "written beside the call and the row is not touched — this is the 'try it before "
        "saving it' path for the settings above. The preview expires when the API restarts."
    ),
    responses={404: {"description": "No such call"}, 503: {"description": "ffmpeg failed"}},
)
def audio_preview(
    body: AudioPreviewIn, db: DbSession, principal: AdminPrincipal  # noqa: ARG001 - scope only
) -> AudioPreviewOut:
    from app.audio import AudioUnavailable, resolve_audio  # noqa: PLC0415
    from app.audioproc.chain import AudioConfig  # noqa: PLC0415 - imports numpy
    from app.audioproc.process import AudioProcessError, process_file  # noqa: PLC0415

    call = db.get(Call, body.call_id)
    if call is None:
        raise HTTPException(404, f"no call {body.call_id}")
    settings = _audio_runtime(db)
    config = AudioConfig.from_settings(settings)
    token = uuid4().hex
    target = Path(tempfile.gettempdir()) / "keyup-audio-preview" / f"{token}.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = process_file(resolve_audio(call, settings), config, dst=target)
    except AudioUnavailable as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    except (AudioProcessError, OSError) as exc:
        raise HTTPException(503, f"could not process call {call.id}: {exc}") from exc
    _PREVIEWS[token] = target
    for stale in list(_PREVIEWS)[:-PREVIEW_KEEP]:
        with suppress(OSError):
            _PREVIEWS.pop(stale).unlink(missing_ok=True)
    return AudioPreviewOut(
        call_id=call.id,
        meta=result.meta,
        filter=str(result.meta.get("filter", "")),
        raw_url=f"/api/calls/{call.id}/audio?raw=1",
        processed_url=f"/api/admin/audio/preview/{token}",
    )


@router.get(
    "/audio/preview/{token}",
    summary="Hear a preview render (admin)",
    include_in_schema=False,
)
def audio_preview_audio(token: str, principal: AdminPrincipal) -> Response:  # noqa: ARG001 - scope only
    path = _PREVIEWS.get(token)
    if path is None or not path.is_file():
        raise HTTPException(404, "that preview has expired")
    return Response(path.read_bytes(), media_type="audio/mpeg")


# --------------------------------------------------------------------------------------
# audio profiles (docs/AUDIO.md)
# --------------------------------------------------------------------------------------


def _profile_out(db: Session, row: AudioProfile) -> AudioProfileOut:
    """One profile with its rendered chain and how many channels are on it.

    The render is done per response rather than stored, because it is derived: a stored
    string would be one more thing that can disagree with the steps beside it, and the
    whole reason the chain is held as data is that the ffmpeg string is generated.
    """
    from app.audioproc.profiles import ProfileError, render_chain  # noqa: PLC0415 - numpy-free but paired

    used = db.execute(
        select(func.count()).select_from(Talkgroup).where(Talkgroup.audio_profile_id == row.id)
    ).scalar_one()
    try:
        rendered = render_chain(row.chain or [])
    except ProfileError as exc:  # pragma: no cover - a stored chain is validated on write
        rendered = f"(will not render: {exc})"
    return AudioProfileOut(
        id=row.id,
        name=row.name,
        description=row.description,
        chain=row.chain or [],
        builtin=row.builtin,
        rendered=rendered,
        talkgroup_count=int(used),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _profile_chain(steps: Sequence[Any]) -> list[dict[str, Any]]:
    """Validate a submitted chain, or answer 422 with what is wrong with it.

    Every path that stores or renders operator-typed steps comes through here, so the
    whitelist in :mod:`app.audioproc.profiles` is the only thing that decides what may
    become an ffmpeg filter — there is no second, looser route into the renderer.
    """
    from app.audioproc.profiles import ProfileError, normalize_chain  # noqa: PLC0415

    try:
        return normalize_chain([step.model_dump() for step in steps])
    except ProfileError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/audio/profiles/steps",
    response_model=AudioProfileCatalogOut,
    summary="The filter steps a profile may hold",
    description=(
        "The whitelist: every step kind, every parameter it takes, and the range each one "
        "is confined to. The editor builds its form from this rather than repeating the "
        "ranges in TypeScript, and a chain holding anything not described here is refused "
        "before an ffmpeg filter graph exists. Read-only."
    ),
)
def audio_profile_steps() -> AudioProfileCatalogOut:
    from app.audioproc.profiles import MAX_STEPS, step_catalog  # noqa: PLC0415

    return AudioProfileCatalogOut(max_steps=MAX_STEPS, steps=step_catalog())


@router.get(
    "/audio/profiles",
    response_model=list[AudioProfileOut],
    summary="Audio profiles",
    description=(
        "The named filter chains talkgroups can be processed through (docs/AUDIO.md). A "
        "profile replaces the denoiser and gate stages of the global chain for the "
        "channels assigned to it; the trim, the tone envelope and the loudness tail are "
        "measured per call and are not a profile's to change. Read-only."
    ),
)
def list_audio_profiles(db: DbSession) -> list[AudioProfileOut]:
    rows = db.scalars(select(AudioProfile).order_by(AudioProfile.builtin.desc(), AudioProfile.name))
    return [_profile_out(db, row) for row in rows]


@router.post(
    "/audio/profiles",
    response_model=AudioProfileOut,
    status_code=201,
    summary="Create an audio profile (admin)",
    responses={409: {"description": "A profile of that name exists"}},
)
def create_audio_profile(
    body: AudioProfileIn, db: DbSession, principal: AdminPrincipal
) -> AudioProfileOut:
    name = body.name.strip()
    if db.scalar(select(AudioProfile).where(AudioProfile.name == name)) is not None:
        raise HTTPException(409, f"an audio profile named {name!r} already exists")
    row = AudioProfile(name=name, description=body.description, chain=_profile_chain(body.chain))
    db.add(row)
    db.commit()
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.profile_created",
        message=f"{principal.label} created audio profile {name!r}",
    )
    db.commit()
    return _profile_out(db, row)


@router.patch(
    "/audio/profiles/{profile_id}",
    response_model=AudioProfileOut,
    summary="Edit an audio profile (admin)",
    description=(
        "Only the keys present are written. The three shipped profiles are editable — an "
        "operator who wants a different noise floor on `Default` should have it — and the "
        "edit does **not** re-render anything on its own: press *Apply* (or run a "
        "reprocess) to bring the archive up to the new chain."
    ),
    responses={404: {"description": "No such profile"}, 409: {"description": "Name in use"}},
)
def patch_audio_profile(
    profile_id: int, body: AudioProfilePatch, db: DbSession, principal: AdminPrincipal
) -> AudioProfileOut:
    row = db.get(AudioProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"no audio profile {profile_id}")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        name = str(fields["name"]).strip()
        clash = db.scalar(
            select(AudioProfile).where(AudioProfile.name == name, AudioProfile.id != row.id)
        )
        if clash is not None:
            raise HTTPException(409, f"an audio profile named {name!r} already exists")
        row.name = name
    if "description" in fields:
        row.description = fields["description"]
    if "chain" in fields and body.chain is not None:
        row.chain = _profile_chain(body.chain)
    db.commit()
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.profile_edited",
        message=f"{principal.label} edited audio profile {row.name!r}",
    )
    db.commit()
    return _profile_out(db, row)


@router.delete(
    "/audio/profiles/{profile_id}",
    status_code=204,
    summary="Delete an audio profile (admin)",
    description=(
        "Refused for the shipped profiles, and refused with the count for a profile any "
        "talkgroup is still on: deleting it would quietly move those channels back to the "
        "global chain, which is a change to how their audio sounds and should be made by "
        "reassigning them, not as a side effect of tidying up a list."
    ),
    responses={
        404: {"description": "No such profile"},
        409: {"description": "Builtin, or still in use"},
    },
)
def delete_audio_profile(profile_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(AudioProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"no audio profile {profile_id}")
    if row.builtin:
        raise HTTPException(409, f"{row.name!r} is a built-in profile and cannot be deleted")
    used = db.execute(
        select(func.count()).select_from(Talkgroup).where(Talkgroup.audio_profile_id == row.id)
    ).scalar_one()
    if used:
        raise HTTPException(
            409,
            f"{row.name!r} is still assigned to {used} talkgroup(s); move them to another "
            "profile (or to none) first",
        )
    name = row.name
    db.delete(row)
    db.commit()
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.profile_deleted",
        message=f"{principal.label} deleted audio profile {name!r}",
    )
    db.commit()
    return Response(status_code=204)


@router.post(
    "/audio/profiles/preview",
    summary="Hear a chain on one call without saving it (admin)",
    description=(
        "Renders the chain in the body over the call's **original** audio and returns the "
        "MP3. Nothing is stored: no row is touched, no file is written beside the call, "
        "and the profile need not exist — this is how a chain is tuned before it is a "
        "profile at all. At most 60 seconds of audio is rendered. The chain is validated "
        "against the step whitelist first, so no operator text reaches ffmpeg."
    ),
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "The rendered audio"},
        404: {"description": "No such call"},
        422: {"description": "The chain is not valid"},
        503: {"description": "ffmpeg failed"},
    },
)
def audio_profile_preview(
    body: AudioProfilePreviewIn,
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001 - scope only
) -> Response:
    from app.audio import AudioUnavailable, resolve_audio  # noqa: PLC0415
    from app.audioproc.process import AudioProcessError, render_to_mp3  # noqa: PLC0415 - numpy
    from app.audioproc.profiles import render_chain  # noqa: PLC0415

    call = db.get(Call, body.call_id)
    if call is None:
        raise HTTPException(404, f"no call {body.call_id}")
    settings = _audio_runtime(db)
    steps = _profile_chain(body.chain)
    chain = render_chain(
        steps,
        target_lufs=float(getattr(settings, "audio_target_lufs", -16.0)),
        true_peak_db=float(getattr(settings, "audio_true_peak_db", -4.0)),
    )
    try:
        audio = render_to_mp3(resolve_audio(call, settings), chain, seconds=body.seconds)
    except AudioUnavailable as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    except (AudioProcessError, OSError) as exc:
        raise HTTPException(503, f"could not render call {call.id}: {exc}") from exc
    return Response(
        audio,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            # The chain is handed back in a header so the editor can show what it just
            # played without rendering the string a second time in TypeScript.
            "X-Keyup-Filter": chain,
        },
    )


@router.post(
    "/audio/profiles/{profile_id}/apply",
    response_model=AudioProfileApplyOut,
    summary="Assign a profile and requeue those channels' recent calls (admin)",
    description=(
        "The second half of tuning a filter: assign the profile to the talkgroups named in "
        "`talkgroup_ids` and queue their calls from the last `hours` (24 by default) to be "
        "re-rendered through it. `all_with_profile` skips the assignment and sweeps every "
        "channel already on the profile, which is what to press after *editing* one. The "
        "requeue is forced — the calls' processed copies are current by version, and it is "
        "the chain that changed — and runs at the backlog band, behind live traffic. While "
        "audio processing is switched off the assignment is made and nothing is queued."
    ),
    responses={404: {"description": "No such profile"}, 422: {"description": "No such talkgroup"}},
)
def apply_audio_profile(
    profile_id: int, body: AudioProfileApplyIn, db: DbSession, principal: AdminPrincipal
) -> AudioProfileApplyOut:
    from app.audioproc.jobs import enqueue_many, select_for_reprocess  # noqa: PLC0415 - numpy

    row = db.get(AudioProfile, profile_id)
    if row is None:
        raise HTTPException(404, f"no audio profile {profile_id}")
    settings = _audio_runtime(db)

    assigned = 0
    if body.talkgroup_ids:
        rows = list(db.scalars(select(Talkgroup).where(Talkgroup.id.in_(body.talkgroup_ids))))
        missing = sorted(set(body.talkgroup_ids) - {tg.id for tg in rows})
        if missing:
            raise HTTPException(422, f"no talkgroup {missing[0]}")
        for talkgroup in rows:
            if talkgroup.audio_profile_id != row.id:
                talkgroup.audio_profile_id = row.id
                assigned += 1
        db.commit()

    # Whatever was just assigned *plus* whatever was already on the profile: an operator
    # who assigns one channel and presses apply means "make this channel right", and one
    # who edits the chain and presses apply means "make every channel on it right".  The
    # union is both, and re-queueing a call twice is a no-op the queue's own index absorbs.
    tgids = list(
        db.scalars(
            select(Talkgroup.tgid).where(Talkgroup.audio_profile_id == row.id).distinct()
        )
    )
    matched: list[int] = []
    # The assignment above stands whatever the switch says; the re-render does not, since
    # the worker would cancel it on its next idle cycle.  ``queued`` 0 of ``matched``
    # 0 is the answer, and the tab says why.
    if tgids and _audio_on(settings):
        matched = select_for_reprocess(
            db,
            settings,
            since=datetime.now(UTC) - timedelta(hours=body.hours),
            talkgroups=tgids,
            force=True,
            limit=body.limit,
        )
    queued = enqueue_many(
        db,
        matched,
        priority=PROFILE_APPLY_PRIORITY,
        max_attempts=int(getattr(settings, "audio_process_max_attempts", 3) or 3),
    )
    db.commit()
    count_service.invalidate(AUDIO_STATUS_KEY)  # the tab shows the new queue at once
    activity.record(
        db,
        source="admin",
        level="info",
        kind="audio.profile_applied",
        message=(
            f"{principal.label} applied audio profile {row.name!r} to {len(tgids)} talkgroup(s) "
            f"and queued {queued} call(s)"
        ),
    )
    db.commit()
    log.info(
        "audio: %s applied profile %r (%d assigned, %d talkgroups, %d calls queued)",
        principal.label, row.name, assigned, len(tgids), queued,
    )  # fmt: skip
    return AudioProfileApplyOut(
        profile_id=row.id,
        profile_name=row.name,
        assigned=assigned,
        talkgroups=len(tgids),
        matched=len(matched),
        queued=queued,
        already_open=len(matched) - queued,
        hours=body.hours,
    )


# --------------------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------------------


def _policy_out(db: Session, row: RetentionPolicy) -> RetentionPolicyOut:
    label = "Everything"
    if row.scope == "system" and row.scope_id:
        system = db.get(System, row.scope_id)
        label = system.name if system else f"system {row.scope_id}"
    elif row.scope == "talkgroup" and row.scope_id:
        tg = db.get(Talkgroup, row.scope_id)
        label = (tg.alias or str(tg.tgid)) if tg else f"talkgroup {row.scope_id}"
    return RetentionPolicyOut(
        id=row.id,
        scope=row.scope,
        scope_id=row.scope_id,
        scope_label=label,
        keep_days=row.keep_days,
        keep_audio_days=row.keep_audio_days,
        keep_transcripts=row.keep_transcripts,
        archive_path=row.archive_path,
        enabled=row.enabled,
    )


@router.get(
    "/retention/policies",
    response_model=list[RetentionPolicyOut],
    summary="Retention policies",
    description=(
        "Per-scope retention tiers: how long to keep the row, how long to keep the audio, and "
        "whether to keep the transcript once the audio is gone. A talkgroup policy beats a system "
        "policy, which beats the global one."
    ),
)
def list_retention(db: DbSession) -> list[RetentionPolicyOut]:
    return [
        _policy_out(db, r)
        for r in db.scalars(select(RetentionPolicy).order_by(RetentionPolicy.scope, RetentionPolicy.id))
    ]


@router.post(
    "/retention/policies",
    response_model=list[RetentionPolicyOut],
    summary="Create or update a retention policy (admin)",
)
def save_retention(
    body: RetentionPolicyIn, db: DbSession, principal: AdminPrincipal
) -> list[RetentionPolicyOut]:
    from app.retention import remember_archive_root, validate_archive_path  # noqa: PLC0415

    if body.archive_path is not None and body.archive_path.strip():
        # Checked, not trusted: an archive path becomes a directory the audio route will
        # serve files from, and one that does not exist (an unmounted share) would be
        # created on the root filesystem by the first pass (review R1).
        try:
            body.archive_path = validate_archive_path(
                body.archive_path, runtime_settings.effective_settings(db, base=get_settings())
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"archive_path: {exc}") from exc
        remember_archive_root(db, body.archive_path)
    else:
        body.archive_path = None
    row = db.get(RetentionPolicy, body.id) if body.id else None
    if row is None:
        row = RetentionPolicy(scope=body.scope, scope_id=body.scope_id)
        db.add(row)
    for key in (
        "scope",
        "scope_id",
        "keep_days",
        "keep_audio_days",
        "keep_transcripts",
        "archive_path",
        "enabled",
    ):
        setattr(row, key, getattr(body, key))
    db.commit()
    return list_retention(db)


@router.delete("/retention/policies/{policy_id}", status_code=204, summary="Delete a policy (admin)")
def delete_retention(policy_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(RetentionPolicy, policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no retention policy {policy_id}")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


# The SPA calls these two shorter paths; they are the same handlers.
router.add_api_route(
    "/retention",
    list_retention,
    methods=["GET"],
    response_model=list[RetentionPolicyOut],
    summary="Retention policies",
)
router.add_api_route(
    "/retention",
    save_retention,
    methods=["POST"],
    response_model=list[RetentionPolicyOut],
    summary="Create or update a retention policy (admin)",
)


@router.get(
    "/retention/run",
    response_model=RetentionRunResult,
    summary="Preview what retention would remove (admin)",
    description=(
        "A dry run: the same full pass as `POST /admin/retention/run` — it walks every call "
        "past the shortest window — but nothing is deleted. As expensive as the real thing, "
        "so it needs the `admin` scope too."
    ),
)
def retention_preview(db: DbSession, principal: AdminPrincipal) -> RetentionRunResult:
    return _run_retention(db, dry_run=True)


@router.post(
    "/retention/run",
    response_model=RetentionRunResult,
    summary="Apply retention now (admin)",
    description=(
        "Runs the nightly retention pass immediately: purge or archive audio past its tier and "
        "delete rows past theirs. Answers 501 when this build has no retention module."
    ),
    responses={501: {"description": "Retention is not available in this build"}},
)
def retention_run(db: DbSession, principal: AdminPrincipal) -> RetentionRunResult:
    return _run_retention(db, dry_run=False)


def _run_retention(db: Session, *, dry_run: bool) -> RetentionRunResult:
    try:
        from app.retention import apply_retention  # noqa: PLC0415 - optional module
    except ImportError as exc:
        raise HTTPException(
            status_code=501, detail="retention is not available in this build"
        ) from exc
    result = apply_retention(db, dry_run=dry_run)
    # ``RetentionReport`` names the row count ``calls_deleted``; the wire has always called
    # it ``calls_purged``, and reading the wire name off the report quietly reported zero.
    payload = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    return RetentionRunResult(
        audio_deleted=int(payload.get("audio_deleted", 0)),
        audio_archived=int(payload.get("audio_archived", 0)),
        calls_purged=int(payload.get("calls_deleted", 0)),
        radio_events_purged=int(payload.get("radio_events_deleted", 0)),
        bytes_freed=int(payload.get("bytes_freed", 0)),
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------------------
# tokens and users
# --------------------------------------------------------------------------------------


def _token_out(row: ApiToken, *, used_by: list[str] | None = None) -> ApiTokenInfo:
    return ApiTokenInfo(
        id=row.id,
        label=row.label,
        scopes=list(row.scopes or ()),
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        prefix=row.token_prefix,
        suffix=(row.token_hash or "")[-4:],
        used_by_recorders=used_by or [],
    )


def _token_recorders(db: Session) -> dict[int, list[str]]:
    """``{token id: [recorder …]}`` — which recorder each token was first seen from.

    ``recorders.token_id`` is written on first contact and never enforced (any `upload`
    token may poll for any recorder), so this is a report, not a permission. It is what
    turns "revoking this token breaks something" from a guess into a sentence on the row.
    """
    bound: dict[int, list[str]] = {}
    for token_id, name, instance_id in db.execute(
        select(Recorder.token_id, Recorder.label, Recorder.instance_id)
        .where(Recorder.token_id.is_not(None))
        .order_by(Recorder.instance_id)
    ):
        bound.setdefault(int(token_id), []).append(name or instance_id)
    return bound


@router.get(
    "/reconcile/stats",
    summary="Cross-recorder reconciliation",
    description=(
        "How much of the archive is second copies and who is winning: pairs resolved, the "
        "deciding rule's tally, primaries and duplicates per recorder, and when the batch pass "
        "last ran (docs/RECONCILIATION.md)."
    ),
)
def reconcile_stats(db: DbSession) -> dict[str, Any]:
    settings = get_settings()
    duplicates = db.scalar(
        select(func.count()).select_from(Call).where(Call.duplicate_of.is_not(None))
    )
    reconciled = db.scalar(
        select(func.count()).select_from(Call).where(Call.reconciled_at.is_not(None))
    )
    total = db.scalar(select(func.count()).select_from(Call))

    # Which component decided, counted over the primaries — one row per pair, because both
    # sides carry the same ruling and only one of them is the primary.
    # One expression object, used in both the SELECT and the GROUP BY: two copies would
    # carry two different bind parameters and PostgreSQL would not match them.
    rule_column = Call.reconcile_reason["rule"].astext
    rules = {
        str(rule): int(count)
        for rule, count in db.execute(
            select(rule_column, func.count())
            .where(Call.reconciled_at.is_not(None), Call.duplicate_of.is_(None))
            .group_by(rule_column)
            .order_by(func.count().desc())
        )
    }
    by_source: dict[str, dict[str, int]] = {}
    for source, is_duplicate, count in db.execute(
        select(Call.ingest_source, Call.duplicate_of.is_not(None), func.count())
        .where(Call.reconciled_at.is_not(None))
        .group_by(Call.ingest_source, Call.duplicate_of.is_not(None))
    ):
        entry = by_source.setdefault(str(source), {"primary": 0, "duplicate": 0})
        entry["duplicate" if is_duplicate else "primary"] += int(count)

    return {
        "enabled": settings.reconcile_enabled,
        "settings": {
            "clock_skew_seconds": settings.reconcile_clock_skew_seconds,
            "min_overlap": settings.reconcile_min_overlap,
            "fragment_seconds": settings.reconcile_fragment_seconds,
            "merge_ratio": settings.reconcile_merge_ratio,
            "prefer_sources": list(settings.reconcile_prefer_sources),
            "minutes": settings.reconcile_minutes,
        },
        "calls": int(total or 0),
        "reconciled": int(reconciled or 0),
        "duplicates": int(duplicates or 0),
        "pairs": int((reconciled or 0) - (duplicates or 0)),
        "rules": rules,
        "by_source": by_source,
        "last_pass_at": (lambda when: when.isoformat() if when else None)(last_pass_at(db)),
    }


#: Which runtime settings each export service owns, as key prefixes — the same split the
#: Exports page renders its cards from.  A test may be handed the *unsaved* values of the
#: card it was pressed on and nothing else: a body that could carry any key at all would be
#: a second, unaudited settings API, and one that could carry another service's key would
#: let the OpenMHz card decide what Broadcastify was tested with.
EXPORT_SERVICE_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # service: (prefixes it owns, prefixes carved out of them)
    "broadcastify": (
        ("broadcastify_", "export_broadcastify_"),
        ("broadcastify_feed_", "export_broadcastify_feed_"),
    ),
    "openmhz": (("openmhz_", "export_openmhz_"), ()),
    "broadcastify_feed": (("broadcastify_feed_", "export_broadcastify_feed_"), ()),
}

#: The knobs that belong to every service at once (the rate and selection block), which any
#: card may legitimately send along with its own.
EXPORT_SHARED_KEYS = frozenset(
    {
        "export_include_encrypted",
        "export_min_duration_s",
        "export_ntp_server",
        "export_timeout_s",
    }
)


def _service_owns(service: str, key: str) -> bool:
    """May a test of ``service`` be handed a draft value for ``key``?"""
    if key in EXPORT_SHARED_KEYS:
        return True
    owned, carved = EXPORT_SERVICE_KEYS.get(service, ((), ()))
    if any(key.startswith(prefix) for prefix in carved):
        return False
    return any(key.startswith(prefix) for prefix in owned)


def _draft_settings(base: Any, service: str, overrides: dict[str, Any]) -> tuple[Any, list[str]]:
    """``base`` with one card's unsaved values layered on top — for this request only.

    Every key is checked against ``SETTINGS_SPEC`` and against the service that is being
    tested, then coerced by exactly the same :func:`_coerce` a save would use, so a draft
    test and the save that follows it cannot read a value differently.  Nothing is written:
    the merged :class:`~app.config.Settings` is a local object that is dropped when the
    response is.
    """
    if not overrides:
        return base, []
    known = {key: kind for key, kind, *_ in SETTINGS_SPEC}
    values: dict[str, Any] = {}
    applied: list[str] = []
    for key, raw in overrides.items():
        if key not in known:
            raise HTTPException(status_code=422, detail=f"not a settable key: {key}")
        if not _service_owns(service, key):
            raise HTTPException(
                status_code=422, detail=f"{key} does not belong to the {service} service"
            )
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # A blank field means "unchanged", exactly as it does on save — most of all for
            # a secret, where blank is what a write-only input shows when it is untouched.
            continue
        try:
            values[key] = _coerce(known[key], raw, key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{key}: {exc}") from exc
        applied.append(key)
    if not values:
        return base, []
    try:
        merged = type(base).model_validate({**base.model_dump(), **values})
    except Exception as exc:  # noqa: BLE001 - a bad draft is the operator's, not a 500
        raise HTTPException(status_code=422, detail=f"unusable draft values: {exc}") from exc
    return merged, sorted(applied)


@router.get(
    "/exports",
    response_model=ExportStatusOut,
    summary="Outbound export services",
    description=(
        "Broadcastify Calls and OpenMHz: whether each is switched on, whether its "
        "credentials are present, when it was enabled (the watermark before which nothing "
        "is ever uploaded), which talkgroups and systems it is selecting, the ledger "
        "tallies — total and over the last 24 hours — the queue depth, the last success "
        "and the last error, plus the most recent ledger rows (docs/EXPORTERS.md). Both "
        "ship disabled. The ledger figures and rows are cached for up to a minute (retrying "
        "a row clears the cache); the configuration fields are always current."
    ),
)
def export_status(db: DbSession) -> ExportStatusOut:
    from app.exporters import clock, gate  # noqa: PLC0415 - imports httpx via the clients
    from app.exporters.broadcastify import SERVICE as BROADCASTIFY_SERVICE  # noqa: PLC0415
    from app.exporters.broadcastify import (  # noqa: PLC0415
        encoding_for,
        environment_name,
        monitoring_url,
    )
    from app.exporters.openmhz import SERVICE as OPENMHZ_SERVICE  # noqa: PLC0415
    from app.exporters.worker import build_clients, enabled_for  # noqa: PLC0415

    settings = runtime_settings.effective_settings(db)
    # The ledger half of the page — every number read from ``call_exports`` — is cached;
    # the configuration half (switches, credentials, selection, watermarks, the feed's live
    # status) is read fresh on every poll, so a switch flipped on this page shows at once.
    figures = count_service.cached(EXPORTS_STATUS_KEY, EXPORTS_STATUS_TTL_S, lambda: _export_figures(db))
    tallies = figures["tallies"]
    recent_24h = figures["recent_24h"]
    duplicates, duplicates_24h, dupe_uploads_24h, late_24h, responses_24h = figures["responses"]
    by_recorder_24h, claimed_24h = figures["recorder_counts"]
    last_errors = figures["last_errors"]
    services = []
    for client in build_clients(settings):
        service = client.service
        mark = gate.watermark(db, service)
        counts = tallies.get(service, {})
        error, error_at = last_errors.get(service, (None, None))
        # Broadcastify Calls is the only service with an environment, an encoding choice
        # and a clock requirement; the other rows leave all of it null rather than
        # inventing a value that would read as a claim about OpenMHz.
        extra: dict[str, object] = {}
        if service == BROADCASTIFY_SERVICE:
            extra = {
                "environment": environment_name(settings),
                "status_url": monitoring_url(settings, "status") or None,
                "manage_url": monitoring_url(settings, "manage") or None,
                "encoding": encoding_for(settings),
                # Measured here rather than read from the worker's cache, because the
                # worker is a different *process*: its cache is not this one's, and a
                # status block that always said "unknown" would be worse than a UDP round
                # trip to a LAN server.  It is the same ten-minute cache, so the cost is
                # one datagram per ten minutes per API process, and the 2 s socket timeout
                # bounds the worst case when the NTP server is gone.
                **clock.sampled(settings).as_dict(),
            }
        elif service == OPENMHZ_SERVICE:
            # OpenMHz has no environments or clock rule, but it does have an account page
            # and a public system page worth a link from the card.
            short_name = str(getattr(settings, "openmhz_short_name", "") or "").strip().lower()
            extra = {
                "manage_url": "https://account.openmhz.com/",
                "status_url": f"https://openmhz.com/system/{short_name}" if short_name else None,
            }
        services.append(
            ExportServiceStatus(
                **extra,
                service=service,
                enabled=enabled_for(settings, service),
                configured=client.configured(),
                enabled_at=mark,
                talkgroup_mode=gate.selection_mode(settings, service),
                systems=sorted(gate.systems_for(settings, service)),
                counts=counts,
                counts_24h=recent_24h.get(service, {}),
                duplicates=duplicates.get(service, 0),
                duplicates_24h=duplicates_24h.get(service, 0),
                duplicate_uploads_24h=dupe_uploads_24h.get(service, 0),
                late_24h=late_24h.get(service, 0),
                # Who is doing the uploading, and the two numbers that say whether it is
                # working: how many of the last day's uploads a recorder sent, and how
                # many calls keyup stood down on because one had already claimed them.
                upload_at=(
                    str(getattr(settings, "broadcastify_upload_at", "keyup") or "keyup")
                    if service == BROADCASTIFY_SERVICE
                    else "keyup"
                ),
                by_recorder_24h=by_recorder_24h.get(service, 0),
                claimed_24h=claimed_24h.get(service, 0),
                responses_24h=responses_24h.get(service, {}),
                uploaded=int(counts.get("uploaded", 0)),
                pending=int(counts.get("pending", 0)),
                skipped=int(counts.get("skipped", 0)),
                failed=int(counts.get("failed", 0)),
                queue_depth=int(counts.get("pending", 0)),
                last_error=error,
                last_error_at=error_at,
            )
        )
    last_upload = figures["last_upload"]
    for entry in services:
        entry.last_upload_at = last_upload.get(entry.service)
    return ExportStatusOut(
        services=services,
        max_attempts=int(settings.export_max_attempts),
        recent=list(figures["recent"]),
        broadcastify_feed=_feed_status(db, settings),
        # What Calls *could* publish, whatever is selected: an empty list is the honest
        # answer to "why is the Calls switch disabled".
        trunked_systems=[
            TrunkedSystem(id=row.id, name=row.name) for row in gate.trunked_systems(db)
        ],
    )


#: Cache key and TTL for the ledger figures behind ``GET /admin/exports``
#: (app/services/counts.py).  Nine reads of ``call_exports`` — the all-time tallies and
#: duplicate count, six 24-hour windows, the last error and last upload per service, and
#: the recent rows — about 330 ms of database time a request on production's 823 k-row
#: ledger (2026-09-23), for a tab that polls every 15 s.  The windows and the recent rows
#: have ``ix_call_exports_updated_at`` since 554fe0551d12; the all-time figures are scans
#: whatever is indexed.  A minute old is fine for numbers the exporter worker moves once a
#: cycle; the retry route drops the entry so the row an operator just re-queued shows at
#: once.
EXPORTS_STATUS_KEY = "admin.exports"
EXPORTS_STATUS_TTL_S = 60.0


def _export_figures(db: Session) -> dict[str, Any]:
    """The uncached ledger reads behind :func:`export_status`."""
    from app.exporters import ledger  # noqa: PLC0415 - imports httpx via the clients

    last_errors = {
        str(service): (error, when)
        for service, error, when in db.execute(
            select(CallExport.service, CallExport.last_error, CallExport.updated_at)
            .where(CallExport.last_error.is_not(None))
            .order_by(CallExport.service, CallExport.updated_at.desc())
            .ext(distinct_on(CallExport.service))
        )
    }
    last = db.execute(
        select(CallExport.service, func.max(CallExport.uploaded_at))
        .where(CallExport.uploaded_at.is_not(None))
        .group_by(CallExport.service)
    ).all()
    return {
        "tallies": ledger.summary(db),
        "recent_24h": ledger.window_summary(db, hours=24),
        "responses": _export_responses(db),
        "recorder_counts": _export_recorder_counts(db),
        "last_errors": last_errors,
        "last_upload": {str(k): v for k, v in last if v is not None},
        # Built into response rows here, so a cached entry holds no ORM objects tied to
        # the session that read them.
        "recent": tuple(_export_rows(db, ledger.recent(db, limit=50))),
    }


def _export_recorder_counts(
    db: Session, *, hours: int = 24
) -> tuple[dict[str, int], dict[str, int]]:
    """Per service: uploads a **recorder** sent, and calls it claimed out from under us.

    Two halves of one question — "is recorder-direct upload actually running" — and they
    are counted separately because they can disagree in a way worth seeing: claims rising
    while recorder uploads do not means the recorders are winning the claim and then
    failing to upload, which is the failure mode this pair exists to make visible.

    ``uploaded_by`` is NULL on every row written before recorder upload existed, so the
    recorder count is a positive test (``uploaded_by`` is set and is not ``keyup``) rather
    than "not keyup", which would read the whole history as recorder uploads.
    """
    from app.exporters.claims import CLAIMED_REASON, KEYUP  # noqa: PLC0415

    since = datetime.now(UTC) - timedelta(hours=max(1, hours))
    by_recorder = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(
                CallExport.status == "uploaded",
                CallExport.uploaded_by.is_not(None),
                CallExport.uploaded_by != KEYUP,
                CallExport.updated_at >= since,
            )
            .group_by(CallExport.service)
        )
    }
    claimed = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(
                CallExport.skip_reason.like(f"{CLAIMED_REASON}%"), CallExport.updated_at >= since
            )
            .group_by(CallExport.service)
        )
    }
    return by_recorder, claimed


def _export_responses(
    db: Session, *, hours: int = 24
) -> tuple[
    dict[str, int], dict[str, int], dict[str, int], dict[str, int], dict[str, dict[str, int]]
]:
    """Per service: duplicates all time, then three windowed counts and the responses.

    The duplicate count reads ``skip_reason`` rather than ``response``, and deliberately:
    ``call_exports.response`` was added after several thousand rows had already been
    written, and the whole point of the number is that it is the biggest one on the page —
    3 485 duplicates against 63 uploads on this deployment.  Counting only rows that
    happen to postdate the column would report nearly none of them.  The prose is a
    constant (``app.exporters.broadcastify.DUPLICATE_REASON``), not operator input, so
    matching on it is matching on our own writer.

    ``responses_24h`` has no such history to honour and reads ``response`` directly,
    bucketed by :func:`~app.exporters.broadcastify.response_class` so the per-call tail —
    the upload id, the skew in seconds — does not give every row its own entry.  The window
    is by ``updated_at``, the same "how is it doing now" clock the 24-hour tallies use.
    """
    from app.exporters.broadcastify import (  # noqa: PLC0415
        DUPLICATE_REASON,
        LATE_REASON,
        response_class,
    )
    from app.exporters.ledger import DUPLICATE_UPLOAD_REASON  # noqa: PLC0415

    since = datetime.now(UTC) - timedelta(hours=max(1, hours))
    is_duplicate = CallExport.skip_reason.like(f"{DUPLICATE_REASON}%")
    duplicates = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(is_duplicate)
            .group_by(CallExport.service)
        )
    }
    duplicates_24h = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(is_duplicate, CallExport.updated_at >= since)
            .group_by(CallExport.service)
        )
    }
    # A different duplicate, and the label on the card says so: this one is *our* second
    # copy of one transmission, refused before anything was sent, because reconciliation
    # moved the primary after the first copy had already been published (docs/EXPORTERS.md,
    # "One upload per transmission").  No all-time twin, for the same reason as the late
    # count below: the reason is new, so an all-time number would say nothing about the
    # months before it existed.
    duplicate_uploads_24h = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(
                CallExport.skip_reason.like(f"{DUPLICATE_UPLOAD_REASON}%"),
                CallExport.updated_at >= since,
            )
            .group_by(CallExport.service)
        )
    }
    # The other half of the same story, and the reason it is worth a number of its own: a
    # duplicate is somebody else publishing first, while a late skip is *this* deployment
    # not getting there in time.  Both leave a `skipped` row and only the reason tells them
    # apart.  No all-time twin — the reason is new, so an all-time count would say nothing
    # about the months before it existed.
    late_24h = {
        str(service): int(count)
        for service, count in db.execute(
            select(CallExport.service, func.count())
            .where(CallExport.skip_reason.like(f"{LATE_REASON}%"), CallExport.updated_at >= since)
            .group_by(CallExport.service)
        )
    }
    responses: dict[str, dict[str, int]] = {}
    for service, response, count in db.execute(
        select(CallExport.service, CallExport.response, func.count())
        .where(CallExport.response.is_not(None), CallExport.updated_at >= since)
        .group_by(CallExport.service, CallExport.response)
    ):
        bucket = responses.setdefault(str(service), {})
        name = response_class(response)
        bucket[name] = bucket.get(name, 0) + int(count)
    return duplicates, duplicates_24h, duplicate_uploads_24h, late_24h, responses


def _feed_status(db: Session, settings) -> BroadcastifyFeedStatus:  # noqa: ANN001
    """The live feed's block: its configuration here, its live numbers from the worker.

    The password is deliberately absent — this endpoint is an admin read, but a mount
    password is a credential and the settings API already refuses to hand any of them
    back.
    """
    from app.exporters import gate  # noqa: PLC0415
    from app.exporters.broadcastify_feed import FEED_SERVICE, feed_config, read_status  # noqa: PLC0415

    config = feed_config(settings)
    live = read_status(db)

    def moment(value) -> datetime | None:  # noqa: ANN001
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:  # pragma: no cover - defensive
            return None

    return BroadcastifyFeedStatus(
        enabled=bool(getattr(settings, "export_broadcastify_feed_enabled", False)),
        configured=config.configured(),
        connected=bool(live.get("connected")),
        since=moment(live.get("since")),
        enabled_at=gate.watermark(db, FEED_SERVICE),
        server=config.server,
        port=int(config.port),
        mount=config.mount_path if config.mount else "",
        username=config.username,
        bitrate_kbps=int(config.bitrate_kbps),
        sample_rate=int(config.sample_rate),
        max_backlog_s=float(config.max_backlog_s),
        silence=bool(config.silence),
        bytes_sent=int(live.get("bytes_sent") or 0),
        calls_played=int(live.get("calls_played") or 0),
        dropped=int(live.get("dropped") or 0),
        queued_s=float(live.get("queued_s") or 0.0),
        last_error=live.get("last_error"),
        last_error_at=moment(live.get("last_error_at")),
    )


def _export_rows(db: Session, rows: Sequence[CallExport]) -> list[ExportLedgerRow]:
    """Ledger rows joined to just enough of the call to be readable in a table."""
    call_ids = [row.call_id for row in rows]
    calls = {
        call_id: (tgid, alias, started)
        for call_id, tgid, alias, started in db.execute(
            select(Call.id, Talkgroup.tgid, Talkgroup.alias, Call.started_at)
            .outerjoin(Talkgroup, Talkgroup.id == Call.talkgroup_id)
            .where(Call.id.in_(call_ids or [0]))
        )
    }
    out = []
    for row in rows:
        tgid, alias, started = calls.get(row.call_id, (None, None, None))
        out.append(
            ExportLedgerRow(
                id=int(row.id),
                call_id=int(row.call_id),
                service=str(row.service),
                status=str(row.status),
                attempts=int(row.attempts or 0),
                last_error=row.last_error,
                skip_reason=row.skip_reason,
                remote_id=row.remote_id,
                response=row.response,
                response_code=row.response_code,
                talkgroup=int(tgid) if tgid is not None else None,
                talkgroup_alias=alias,
                started_at=started,
                next_attempt_at=row.next_attempt_at,
                uploaded_at=row.uploaded_at,
                updated_at=row.updated_at,
            )
        )
    return out


@router.post(
    "/exports/broadcastify-feed/test",
    response_model=ExportTestResult,
    summary="Test the Broadcastify Feed mount (admin)",
    description=(
        "Opens the Broadcastify **Feed** mount as a source, writes about two seconds of "
        "**silence**, and closes it. Icecast has no dry run — a mount either accepts a "
        "source or it does not — so this is the only honest check of the Server, Port, "
        "Mount, Username and Password from the feed's *Feed Technical Details (Advanced)* "
        "panel. No call audio is published. It does briefly occupy the mount, so it fails "
        "while another broadcasting client holds it, which is usually the answer being "
        "looked for. Nothing is switched on and the feed's own enable flag is untouched."
    ),
)
def export_feed_test(
    db: DbSession,
    principal: AdminPrincipal,
    body: ExportTestRequest | None = None,
) -> ExportTestResult:
    from app.exporters.broadcastify_feed import (  # noqa: PLC0415
        FEED_SERVICE,
        feed_config,
        probe_mount,
        read_status,
    )

    settings = runtime_settings.effective_settings(db)
    settings, drafted = _draft_settings(settings, FEED_SERVICE, (body.overrides if body else {}))
    config = feed_config(settings)
    live = read_status(db)
    if live.get("connected"):
        # A mount takes one source, and Keyup's own worker is it: probing now would be
        # refused with "Mountpoint in use" and read as a failure of the very thing that
        # is working.  The worker's status *is* the test.
        result = {
            "ok": True,
            "checked": f"the worker's own connection to {config.mount_path} on {config.server}:{config.port}",
            "detail": (
                f"Keyup is streaming to it now (since {live.get('since') or 'unknown'}); "
                "the mount was not probed a second time because it takes one source"
            ),
        }
    else:
        result = probe_mount(config)
    log.info("export: broadcastify feed tested by %s -> %s", principal.label, result.get("ok"))
    return ExportTestResult(
        service=FEED_SERVICE,
        ok=bool(result.get("ok")),
        checked=str(result.get("checked", "")),
        detail=str(result.get("detail", "")),
        used_draft=bool(drafted),
        draft_keys=drafted,
    )


@router.post(
    "/exports/{service}/test",
    response_model=ExportTestResult,
    summary="Test one export service's configuration (admin)",
    description=(
        "Checks as much of a service's configuration as can be checked **without "
        "publishing a call**, and says in `checked` exactly what that was. Broadcastify "
        "Calls is tested with the documented `test=1` parameter alongside `apiKey` and "
        "`systemId` — a real check of the credentials that registers no call and returns "
        "no upload URL — against whichever endpoint `broadcastify_environment` selects, "
        "with the credentials belonging to that same environment. OpenMHz has no such "
        "check: its public `/{shortName}/calls` read resolves the short name, but its API "
        "key is only ever validated by a real upload. The Broadcastify result also carries "
        "the host clock offset (`clock_offset_s`, `clock_ok`, `clock_source`), which is "
        "step 1 of their upload workflow. Nothing is uploaded here and no switch is changed."
    ),
)
def export_test(
    service: str,
    db: DbSession,
    principal: AdminPrincipal,
    body: ExportTestRequest | None = None,
) -> ExportTestResult:
    from app.exporters.worker import build_clients  # noqa: PLC0415

    settings = runtime_settings.effective_settings(db)
    settings, drafted = _draft_settings(settings, service, (body.overrides if body else {}))
    for client in build_clients(settings):
        if client.service == service:
            result = client.test_credentials()
            log.info("export: %s tested by %s -> %s", service, principal.label, result.get("ok"))
            offset = result.get("clock_offset_s")
            clock_ok = result.get("clock_ok")
            return ExportTestResult(
                service=service,
                ok=bool(result.get("ok")),
                checked=str(result.get("checked", "")),
                # ``detail`` is the raw first line of the service's reply.  The client has
                # already run it through ``redact()``; nothing here re-adds a credential.
                detail=str(result.get("detail", "")),
                environment=str(result["environment"]) if result.get("environment") else None,
                clock_offset_s=float(offset) if isinstance(offset, (int, float)) else None,
                clock_ok=bool(clock_ok) if isinstance(clock_ok, bool) else None,
                clock_source=str(result["clock_source"]) if result.get("clock_source") else None,
                used_draft=bool(drafted),
                draft_keys=drafted,
            )
    raise HTTPException(status_code=404, detail=f"no export service {service!r}")


@router.post(
    "/exports/{service}/ledger/{export_id}/retry",
    response_model=ExportLedgerRow,
    summary="Retry one parked export (admin)",
    description=(
        "Puts a `failed` or `skipped` ledger row back in the queue, due now, with its "
        "attempt count reset. An `uploaded` row is refused: that row **is** the guarantee "
        "the call was not published twice, and re-sending it would publish it twice. The "
        "worker picks the row up on its next cycle, and only if the service is still "
        "switched on and the call still passes the live-band and watermark guards."
    ),
)
def export_retry(
    service: str, export_id: int, db: DbSession, principal: AdminPrincipal
) -> ExportLedgerRow:
    from app.exporters import ledger  # noqa: PLC0415

    row = db.get(CallExport, export_id)
    if row is None or row.service != service:
        raise HTTPException(status_code=404, detail=f"no {service} export {export_id}")
    if ledger.requeue(db, export_id) is None:
        raise HTTPException(
            status_code=409, detail=f"an export in status {row.status!r} cannot be retried"
        )
    db.commit()
    count_service.invalidate(EXPORTS_STATUS_KEY)  # the tab shows the row back in the queue
    log.info("export: %s ledger row %s requeued by %s", service, export_id, principal.label)
    db.refresh(row)
    return _export_rows(db, [row])[0]


@router.post(
    "/talkgroups/export-flags/copy",
    response_model=TalkgroupExportFlagCopyResult,
    summary="Copy one per-talkgroup export flag onto another (admin)",
    description=(
        "Writes every selected talkgroup's `source` checkbox into its `destination` "
        "checkbox — `export_broadcastify` (the live **Feed**), `export_bcfy_calls` "
        "(Broadcastify **Calls**) and `export_openmhz`, in any pair. It exists for the "
        "day Broadcastify's shared checkbox became two: \"make Calls what Broadcastify "
        "already was\" is one request rather than several hundred clicks.\n\n"
        "Send `ids` for a selection or `all: true` for the whole directory; one or the "
        "other, never both. A talkgroup whose system is **not trunked** is never given a "
        "ticked Calls flag — Calls publishes trunked systems only — and is counted in "
        "`skipped_not_trunked` instead."
    ),
)
def copy_talkgroup_export_flags(
    body: TalkgroupExportFlagCopy, db: DbSession, principal: AdminPrincipal
) -> TalkgroupExportFlagCopyResult:
    from app.models import Talkgroup  # noqa: PLC0415 - kept local, as the ledger imports are

    source, destination = body.source, body.destination
    if body.all:
        rows = list(db.scalars(select(Talkgroup).order_by(Talkgroup.id)))
    else:
        wanted = list(dict.fromkeys(body.ids or []))
        rows = list(
            db.scalars(select(Talkgroup).where(Talkgroup.id.in_(wanted)).order_by(Talkgroup.id))
        )
        missing = sorted(set(wanted) - {row.id for row in rows})
        if missing:
            listed = ", ".join(str(i) for i in missing[:10])
            more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
            raise HTTPException(status_code=404, detail=f"no talkgroup {listed}{more}")

    trunked = system_service.trunked_ids(db, {row.system_id for row in rows})
    updated = unchanged = skipped = 0
    for row in rows:
        value = bool(getattr(row, source))
        if destination == "export_bcfy_calls" and row.system_id not in trunked:
            # Held false rather than copied: the gate would refuse the call anyway, and a
            # ticked box that cannot publish is a promise the page would have to retract.
            skipped += 1
            if getattr(row, destination):
                setattr(row, destination, False)
                updated += 1
            continue
        if bool(getattr(row, destination)) == value:
            unchanged += 1
            continue
        setattr(row, destination, value)
        updated += 1
    activity.record(
        db,
        source="system",
        level="info",
        kind="talkgroups_bulk_edited",
        message=f"Copied {source} to {destination} on {len(rows)} talkgroups",
        meta={
            "count": len(rows),
            "fields": [destination],
            "source": source,
            "destination": destination,
            "updated": updated,
            "skipped_not_trunked": skipped,
            "by": principal.label,
        },
    )
    db.commit()
    log.info(
        "export: %s copied %s -> %s on %s talkgroups (%s changed, %s held non-trunked)",
        principal.label,
        source,
        destination,
        len(rows),
        updated,
        skipped,
    )
    return TalkgroupExportFlagCopyResult(
        source=source,
        destination=destination,
        considered=len(rows),
        updated=updated,
        unchanged=unchanged,
        skipped_not_trunked=skipped,
        items=[TalkgroupOut.model_validate(row) for row in rows],
    )


# --------------------------------------------------------------------------------------
# outbound webhooks (docs/WEBHOOKS.md)
# --------------------------------------------------------------------------------------


@router.get(
    "/webhooks",
    response_model=WebhookStatusOut,
    summary="Outbound webhook queue",
    description=(
        "Whether webhooks are switched on, which events are subscribed, whether a signing "
        "secret is set (never the secret itself), the queue tallies and the most recent "
        "deliveries with the last error each one hit. Errors have every URL scrubbed out: a "
        "delivery URL can carry a token in its path and this endpoint is an open read. The "
        "feature ships off — a blank `webhook_url` means nothing is ever queued."
    ),
)
def webhook_status(
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200, description="Deliveries returned")] = 50,
    status: Annotated[
        str | None, Query(description="pending, delivered or failed")
    ] = None,
    event: Annotated[str | None, Query(description="One of the subscribable event names")] = None,
) -> WebhookStatusOut:
    from app import webhooks  # noqa: PLC0415 - imports httpx

    settings = runtime_settings.effective_settings(db)
    subscribed = webhooks.subscribed_events(settings)
    return WebhookStatusOut(
        enabled=bool(subscribed),
        url_set=bool(str(settings.webhook_url or "").strip()),
        secret_set=bool(str(settings.webhook_secret or "").strip()),
        events=sorted(subscribed),
        known_events=list(WEBHOOK_EVENTS),
        max_attempts=int(settings.webhook_max_attempts),
        counts=webhooks.summary(db),
        deliveries=[
            WebhookDeliveryOut.model_validate(row)
            for row in webhooks.recent(db, limit=limit, status=status, event=event)
        ],
    )


@router.post(
    "/webhooks/deliveries/{delivery_id}/retry",
    response_model=WebhookDeliveryOut,
    summary="Retry one webhook delivery (admin)",
    description=(
        "Put a parked (or still-pending) delivery back at the front of the queue with its "
        "attempt count reset — a retry an operator asked for is a fresh start, not the sixth "
        "of five. The body that goes out is the one frozen when the event happened, not a "
        "re-read of the call as it is now. A delivery that already succeeded is a 409: "
        "sending it twice is the receiver's problem, not a repair."
    ),
)
def retry_webhook_delivery(
    delivery_id: int, db: DbSession, principal: AdminPrincipal
) -> WebhookDeliveryOut:
    from app import webhooks  # noqa: PLC0415

    row = db.get(WebhookDelivery, delivery_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no webhook delivery {delivery_id}")
    if row.status == "delivered":
        raise HTTPException(
            status_code=409, detail=f"webhook delivery {delivery_id} was already delivered"
        )
    webhooks.requeue(db, delivery_id)
    db.commit()
    db.refresh(row)
    return WebhookDeliveryOut.model_validate(row)


@router.get("/tokens", response_model=list[ApiTokenInfo], summary="API tokens (admin)")
def list_tokens(db: DbSession, principal: AdminPrincipal) -> list[ApiTokenInfo]:
    bound = _token_recorders(db)
    return [
        _token_out(row, used_by=bound.get(row.id))
        for row in db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc()))
    ]


@router.post(
    "/tokens",
    response_model=ApiTokenCreated,
    status_code=201,
    summary="Mint an API token (admin)",
    description=(
        "Returns the raw `kup_…` secret **once**; only its sha256 is stored, so a lost token is "
        "replaced rather than recovered. Scopes are `read`, `write`, `admin`, `stream`, `upload`; "
        "the recorder push endpoint needs `upload`."
    ),
)
def create_token(body: ApiTokenIn, db: DbSession, principal: AdminPrincipal) -> ApiTokenCreated:
    row, raw = create_api_token(db, body.label, list(body.scopes), created_by=principal.label)
    if body.expires_at is not None:
        row.expires_at = body.expires_at
        db.commit()
    return ApiTokenCreated(**_token_out(row).model_dump(), token=raw)


@router.post(
    "/tokens/{token_id}/revoke",
    status_code=204,
    summary="Revoke an API token (admin)",
    description=(
        "The row is kept with a `revoked_at` so the audit trail survives; the token stops "
        "working. Once revoked, `DELETE /admin/tokens/{token_id}` can remove the row for good."
    ),
)
def revoke_token(token_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no token {token_id}")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        activity.record(
            db,
            source="system",
            level="info",
            kind="token.revoked",
            message=f"API token {row.label!r} revoked by {principal.label}",
            meta={"token_id": row.id, "label": row.label, "by": principal.label},
        )
    db.commit()
    return Response(status_code=204)


@router.delete(
    "/tokens/{token_id}",
    status_code=204,
    summary="Delete a revoked API token (admin)",
    description=(
        "Permanently removes the row of a token that has already been revoked, so an admin "
        "list is not a graveyard of dead keys. A token still in service is refused with 409 — "
        "revoke it first (`POST /admin/tokens/{token_id}/revoke`), which keeps the audit trail "
        "and the chance to notice what breaks before the row is gone. Any recorder bound to "
        "the token has its `recorders.token_id` cleared and re-binds on its next contact; the "
        "recorder itself is left alone."
    ),
)
def delete_token(token_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(ApiToken, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no token {token_id}")
    if row.revoked_at is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"token {token_id} ({row.label!r}) is still active; revoke it first with "
                f"POST /admin/tokens/{token_id}/revoke"
            ),
        )
    # Belt and braces: the FK is ON DELETE SET NULL, but nulling it here keeps the ORM's
    # loaded Recorder rows honest and names the recorders in the log line.
    unbound: list[str] = []
    for recorder in db.scalars(select(Recorder).where(Recorder.token_id == token_id)):
        unbound.append(recorder.label or recorder.instance_id)
        recorder.token_id = None
    label = row.label
    db.delete(row)
    activity.record(
        db,
        source="system",
        level="info",
        kind="token.deleted",
        message=f"revoked API token {label!r} deleted by {principal.label}",
        meta={"token_id": token_id, "label": label, "by": principal.label, "unbound": unbound},
    )
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# RadioReference (docs/CHANNEL_AUDIT.md)
# --------------------------------------------------------------------------------------


@router.post(
    "/radioreference/sync",
    response_model=RadioReferenceSyncOut,
    summary="Sync RadioReference now (admin)",
    description=(
        "Pull the documented site frequencies for every mapped system from RadioReference. "
        "Needs `RR_APP_KEY`, `RR_USERNAME` and `RR_PASSWORD` (a **Premium** subscription and the "
        "operator's own app key) and a system mapped either through `RR_SYSTEMS` or with "
        "`PUT /admin/systems/{id}/radioreference`. Without `force` a system synced within "
        "`RR_SYNC_HOURS` is left alone — RR is somebody else's server."
    ),
)
def radioreference_sync(
    db: DbSession,
    principal: AdminPrincipal,
    force: Annotated[bool, Query(description="Sync even a system synced within RR_SYNC_HOURS")] = False,
) -> RadioReferenceSyncOut:
    from app.rr.client import credentials_present  # noqa: PLC0415 - keeps zeep off the import path
    from app.rr.sync import sync_due  # noqa: PLC0415

    settings = get_settings()
    if not credentials_present(settings):
        return RadioReferenceSyncOut(
            enabled=False,
            detail="RR_APP_KEY, RR_USERNAME and RR_PASSWORD are not all set; sync is disabled",
        )
    summaries = sync_due(db, settings=settings, force=force)
    db.commit()
    return RadioReferenceSyncOut(
        enabled=True,
        synced=[RadioReferenceSyncResult(**summary.as_dict()) for summary in summaries],
        detail=None if summaries else "no system was due; pass force=true to sync anyway",
    )


@router.put(
    "/systems/{system_id}/radioreference",
    response_model=SystemOut,
    summary="Map a system to RadioReference (admin)",
    description=(
        "Set `systems.rr_system_id` — RadioReference's `sid` for this system, the number in its "
        "database URL. Null clears it and stops the sync touching the system. Takes precedence "
        "over the `RR_SYSTEMS` environment mapping."
    ),
)
def set_system_rr_id(
    system_id: int, body: RadioReferenceIdIn, db: DbSession, principal: AdminPrincipal
) -> SystemOut:
    row = db.get(System, system_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no system {system_id}")
    row.rr_system_id = body.rr_id
    db.commit()
    # Through the shared builder rather than ``model_validate``, for the same reason the
    # site mapping below is: a ``SystemOut`` without its counts and ``blocking`` is a row
    # the SPA would render with a delete button it cannot know is wrong.
    return system_service.system_out(db, row)


@router.put(
    "/sites/{site_id}/radioreference",
    response_model=SiteOut,
    summary="Map a site to RadioReference (admin)",
    description=(
        "Set `sites.rr_site_id`. The sync matches sites by site number, NAC and description on "
        "its own; this is the escape hatch for a site those heuristics get wrong, and it always "
        "wins."
    ),
)
def set_site_rr_id(
    site_id: int, body: RadioReferenceIdIn, db: DbSession, principal: AdminPrincipal
) -> SiteOut:
    row = db.get(Site, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    row.rr_site_id = body.rr_id
    db.commit()
    # Through the shared builder rather than ``model_validate``: a ``SiteOut`` without its
    # ``system_name`` is a site name nobody can place, and without ``blocking`` the SPA
    # would see a row claiming it is not deletable when it is.
    return site_service.site_out(db, row)


def _user_out(row: User) -> UserOut:
    scopes = list(row.scopes or ())
    return UserOut(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        scopes=scopes,
        is_admin="admin" in scopes,
        is_active=row.is_active,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
    )


@router.get("/users", response_model=list[UserOut], summary="Users (admin)")
def list_users(db: DbSession, principal: AdminPrincipal) -> list[UserOut]:
    return [_user_out(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    summary="Create a user (admin)",
    description=(
        "`is_admin: true` is shorthand for the `admin` scope, which implies every other scope."
    ),
)
def create_user(body: UserIn, db: DbSession, principal: AdminPrincipal) -> UserOut:
    if not body.password:
        raise HTTPException(status_code=422, detail="password is required")
    if db.scalar(select(User).where(func.lower(User.username) == body.username.strip().lower())):
        raise HTTPException(status_code=409, detail=f"user {body.username!r} already exists")
    scopes = normalize_scopes(list(body.scopes) if body.scopes else ["read", "write"])
    if body.is_admin and "admin" not in scopes:
        scopes = normalize_scopes([*scopes, "admin"])
    row = User(
        username=body.username.strip(),
        display_name=body.display_name,
        email=body.email,
        password_hash=hash_password(body.password),
        scopes=scopes,
        is_active=body.is_active,
    )
    db.add(row)
    db.commit()
    return _user_out(row)


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Edit a user (admin)",
    description=(
        "Only the fields you send change. Setting `password` signs the user out everywhere: "
        "every session they hold is ended, except the one making this request when an admin "
        "changes their own password."
    ),
)
def patch_user(
    user_id: int, body: UserPatch, request: Request, db: DbSession, principal: AdminPrincipal
) -> UserOut:
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no user {user_id}")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("password"):
        row.password_hash = hash_password(fields["password"])
        # A session stolen before the reset must not survive it.
        # The admin's own browser is spared when the password is their own.
        own = principal.kind == "session" and principal.user is not None and principal.user.id == row.id
        ended = revoke_user_sessions(db, row.id, keep=request.cookies.get(COOKIE_NAME) if own else None)
        log.info(
            "users: password of %s changed by %s, %d session(s) ended", row.username, principal.label, ended
        )
    for key in ("display_name", "email", "is_active"):
        if key in fields:
            setattr(row, key, fields[key])
    scopes = list(row.scopes or ())
    if "scopes" in fields and fields["scopes"] is not None:
        scopes = normalize_scopes(list(fields["scopes"]))
    if "is_admin" in fields and fields["is_admin"] is not None:
        scopes = [s for s in scopes if s != "admin"] + (["admin"] if fields["is_admin"] else [])
        scopes = normalize_scopes(scopes)
    row.scopes = scopes
    db.commit()
    return _user_out(row)


# --------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------

_REQUEST_COUNTS: dict[str, int] = {}


def count_request(method: str, status: int) -> None:
    """Called by the request-logging middleware in ``app.main``."""
    key = f"{method.upper()}:{status // 100}xx"
    _REQUEST_COUNTS[key] = _REQUEST_COUNTS.get(key, 0) + 1


#: Cache key and TTL for the database half of ``/metrics`` (app/services/counts.py).  An
#: exact ``count(*)`` over ``calls`` and another over its duplicates are two full scans, and
#: a Prometheus scrape every 15 s would run them four times a minute; the lag, the
#: subscriber count, free disk and the request counters stay live.
METRICS_KEY = "admin.metrics"
METRICS_TTL_S = 60.0


def _metrics_figures(db: Session) -> dict[str, Any]:
    """The uncached database counts behind :func:`metrics`."""
    return {
        # An estimate on the archive-sized table, exact below ESTIMATE_MIN_ROWS (tests).
        "calls_total": count_service.estimated_rows(db, Call),
        # ``ix_calls_duplicate_of`` answers this with an index-only scan.
        "duplicates": int(
            db.scalar(select(func.count()).select_from(Call).where(Call.duplicate_of.isnot(None))) or 0
        ),
        "talkgroups": int(db.scalar(select(func.count()).select_from(Talkgroup)) or 0),
        "jobs": {
            str(status): int(count)
            for status, count in db.execute(
                select(TranscriptionJob.status, func.count()).group_by(TranscriptionJob.status)
            ).all()
        },
    }


@metrics_router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Prometheus metrics",
    description=(
        "Text exposition format: call and talkgroup totals, transcription jobs by status, the "
        "ingest lag in seconds, live-feed subscribers, free disk under `AUDIO_ROOT` and an API "
        "request counter by method and status class. The call, duplicate, talkgroup and job "
        "counts are recomputed at most once a minute, and `keyup_calls_total` is the "
        "`pg_class` estimate on an archive-sized table; everything else is live."
    ),
)
def metrics(db: DbSession) -> PlainTextResponse:
    from app.live import bus  # noqa: PLC0415 - avoids importing the bus into every module

    settings = get_settings()
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, Any]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{labels} {value}")

    figures = count_service.cached(METRICS_KEY, METRICS_TTL_S, lambda: _metrics_figures(db))
    metric(
        "keyup_calls_total",
        "gauge",
        "Calls in the archive (pg_class estimate, refreshed at most once a minute)",
        [("", figures["calls_total"])],
    )
    metric(
        "keyup_duplicate_calls_total",
        "gauge",
        "Calls marked as duplicates (refreshed at most once a minute)",
        [("", figures["duplicates"])],
    )
    metric("keyup_talkgroups_total", "gauge", "Talkgroups in the directory", [("", figures["talkgroups"])])
    job_counts = figures["jobs"]
    metric(
        "keyup_transcription_jobs",
        "gauge",
        "Transcription jobs by status (refreshed at most once a minute)",
        [(f'{{status="{status}"}}', int(count)) for status, count in sorted(job_counts.items())]
        or [('{status="queued"}', 0)],
    )
    last_recorded = db.scalar(select(func.max(Call.recorded_at)))
    lag = 0.0
    if last_recorded is not None:
        moment = last_recorded if last_recorded.tzinfo else last_recorded.replace(tzinfo=UTC)
        lag = round((datetime.now(UTC) - moment).total_seconds(), 1)
    metric("keyup_ingest_lag_seconds", "gauge", "Seconds since the newest recording", [("", lag)])
    metric(
        "keyup_live_subscribers",
        "gauge",
        "Connected live-feed clients",
        [("", bus.subscriber_count)],
    )
    try:
        usage = shutil.disk_usage(settings.audio_root)
        metric(
            "keyup_audio_disk_free_bytes", "gauge", "Free bytes under AUDIO_ROOT", [("", usage.free)]
        )
    except OSError:
        pass
    metric(
        "keyup_api_requests_total",
        "counter",
        "API requests by method and status class",
        [
            (f'{{method="{key.split(":")[0]}",status="{key.split(":")[1]}"}}', value)
            for key, value in sorted(_REQUEST_COUNTS.items())
        ]
        or [('{method="GET",status="2xx"}', 0)],
    )
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


__all__ = ["count_request", "ingest_snapshot", "metrics_router", "router"]
