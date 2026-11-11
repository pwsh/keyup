"""``GET /api/status`` — the health and readiness probe (§4.7).

Deliberately more than a 200: the database ping is part of the check, because an API that
answers OK while PostgreSQL is unreachable is worse than one that fails loudly.  The
compose health check and the SPA's status badge both read this.

**The compose health check should target** ``GET /api/healthz`` **instead** — added below.
``/api/status`` costs one estimate, three small counts, an indexed queue count and the
ingest snapshot; ``/api/healthz`` is a single ``SELECT 1``.  A container liveness probe
running every ten seconds wants the second one, and the badge still wants the first.
"""

from __future__ import annotations

import logging
import shutil
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import disk_watch, health
from app.api.admin import ingest_snapshot
from app.auth import OptionalPrincipal
from app.config import get_settings
from app.db import get_probe_session, get_session
from app.ingest import flow
from app.models import Call, Patch, Radio, Talkgroup, TranscriptionJob
from app.runtime_settings import effective_settings
from app.schemas import DiskStatusOut, HealthConditionOut, IngestLagOut, SystemStatusOut
from app.services import counts as count_service

log = logging.getLogger("app.api.status")
router = APIRouter(tags=["status"])
DbSession = Annotated[Session, Depends(get_session)]
#: The liveness probe's session — one statement, no ``statement_timeout`` arming (app.db).
ProbeSession = Annotated[Session, Depends(get_probe_session)]

#: Process start, for the uptime the Admin page shows.
STARTED_AT = time.time()
VERSION = "0.1.0"

#: Cache key and TTL for the headline ``calls`` figure (app/services/counts.py).
CALLS_COUNT_KEY = "status.calls"
COUNT_TTL_S = count_service.DEFAULT_TTL_S


@router.get(
    "/status",
    response_model=SystemStatusOut,
    summary="Health, versions, queue and disk",
    description=(
        "Answers 200 with `status: \"degraded\"` rather than an error when the database is "
        "unreachable, so a monitor can tell 'the API is down' from 'the API is up and the database "
        "is not'. Includes the headline counts, the transcription queue depth, the upload flow-control "
        "gauge (`queue`), free space under "
        "`AUDIO_ROOT` and the ingest snapshot.\n\n"
        "`disk` is the audio root's filesystem with its alert level (`ok`/`warn`/`critical`, "
        "from the `disk_*` settings) and a days-to-full estimate fitted over the worker's last "
        "24 h of five-minute samples; `disk_volumes` lists every filesystem the worker "
        "watches.\n\n"
        "`health` lists the health conditions the worker holds raised right now (a hot recorder "
        "silent, no new calls, transcription stalled, the database backup overdue); empty when "
        "all is well.\n\n"
        "`counts.calls` is an estimate (`pg_class.reltuples`, within a couple of percent) "
        "refreshed at most once a minute, and `ingest` is a cached measurement that says how "
        "old it is in `as_of`/`cached_s`. Everything else is current. For a liveness probe "
        "use `/api/healthz`, which is one `SELECT 1`.\n\n"
        "**Guests** (no session, no token) get the same shape with the operator-only fields "
        "blanked: `version` and `audio_root` are empty, `uptime_s` is 0, `database.detail` and "
        "`ingest` are null, each disk's `path` is a neutral label (`audio storage`, "
        "`storage 2`, …) and a `backup_stale` condition carries no `message`."
    ),
)
def status(db: DbSession, principal: OptionalPrincipal) -> SystemStatusOut:
    settings = get_settings()
    db_ok = True
    detail: str | None = None
    counts: dict[str, int] = {}
    queue_depth = 0
    queue: dict[str, Any] = {}
    ingest = None
    conditions: list[HealthConditionOut] = []
    runtime = settings
    try:
        db.execute(text("SELECT 1"))
        runtime = effective_settings(db, base=settings)
        counts = {
            # ``calls`` is the only one of these that is expensive: three million rows and
            # five gigabytes, scanned in full by ``count(*)``, at ten-second intervals.  It
            # is an estimate from ``pg_class.reltuples``, cached for a minute (M-perf).  The
            # directory tables are all under eleven thousand rows, where the exact count is
            # an index-only scan of a few milliseconds and worth keeping exact.
            "calls": count_service.cached(
                CALLS_COUNT_KEY,
                COUNT_TTL_S,
                lambda: count_service.estimated_rows(db, Call),
            ),
            "talkgroups": int(db.scalar(select(func.count()).select_from(Talkgroup)) or 0),
            "radios": int(db.scalar(select(func.count()).select_from(Radio)) or 0),
            "patches": int(db.scalar(select(func.count()).select_from(Patch)) or 0),
            # Exact, and cheap: an index-only scan of the partial
            # ``ix_transcription_jobs_queued``, which holds only the queued rows.  A queue
            # depth that lagged a minute would be useless.
            "jobs_queued": int(
                db.scalar(
                    select(func.count())
                    .select_from(TranscriptionJob)
                    .where(TranscriptionJob.status == "queued")
                )
                or 0
            ),
        }
        queue_depth = counts["jobs_queued"]
        # The same gauge the upload path decides on — one cached query, so a monitor
        # polling /status sees exactly what a recorder is being told (docs/RECORDERS.md).
        queue = flow.gauge(db, effective_settings(db, base=settings)).as_dict()
        ingest = ingest_snapshot(db)
        # The worker's once-a-minute verdicts (app/health.py) — a handful of rows at most.
        conditions = [
            HealthConditionOut(
                key=row.key,
                check=row.check,
                raised_at=row.raised_at,
                checked_at=row.checked_at,
                value=row.value,
                message=row.message,
            )
            for row in health.active(db)
        ]
    except Exception as exc:  # noqa: BLE001 - reporting this *is* the health check
        db_ok = False
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("status: database unreachable: %s", detail)

    free = total = None
    try:
        usage = shutil.disk_usage(settings.audio_root)
        free, total = usage.free, usage.total
    except OSError:
        pass

    volumes: list[DiskStatusOut] = []
    try:
        volumes = [
            DiskStatusOut(**reading.as_dict())
            for reading in disk_watch.status(db if db_ok else None, runtime)
        ]
    except Exception:  # noqa: BLE001 - the probe answers whatever the disk says
        log.exception("status: disk readings failed")

    from datetime import UTC, datetime  # noqa: PLC0415 - one route, one import

    out = SystemStatusOut(
        status="ok" if db_ok else "degraded",
        ok=db_ok,
        version=VERSION,
        db_ok=db_ok,
        database={"ok": db_ok, "detail": detail},
        audio_root=str(settings.audio_root),
        tz=settings.tz,
        counts=counts,
        queue_depth=queue_depth,
        queue=queue,
        disk_free_bytes=free,
        disk_total_bytes=total,
        disk=volumes[0] if volumes and volumes[0].path == str(settings.audio_root) else None,
        disk_volumes=volumes,
        uptime_s=round(time.time() - STARTED_AT, 1),
        ingest=ingest,
        health=conditions,
        server_time=datetime.now(UTC),
    )
    return out if principal is not None else guest_view(out)


#: Health checks whose ``message`` names a filesystem path (the backup directory).
_PATH_BEARING_CHECKS = frozenset({"backup_stale"})


def guest_view(full: SystemStatusOut) -> SystemStatusOut:
    """``full`` with what only an operator should see removed, for an anonymous caller.

    ``/api/status`` stays open — the header badges, the disk and health banners on the
    public Stats page and any outside monitor read it without an account — but the build,
    the server's paths, its uptime and the raw database error are not a visitor's business.
    The shape is unchanged so the SPA's types hold: strings go empty, the rest go null/0.
    The disk figures and levels stay (the public banner warns on them); only the mount
    path is replaced with a label.
    """
    audio_root = full.audio_root
    volumes = [
        volume.model_copy(
            update={"path": "audio storage" if volume.path == audio_root else f"storage {index}"}
        )
        for index, volume in enumerate(full.disk_volumes, start=1)
    ]
    disk = None
    if full.disk is not None:
        disk = full.disk.model_copy(update={"path": "audio storage"})
    health = [
        condition.model_copy(update={"message": None})
        if condition.check in _PATH_BEARING_CHECKS
        else condition
        for condition in full.health
    ]
    return full.model_copy(
        update={
            "version": "",
            "audio_root": "",
            "uptime_s": 0.0,
            "database": {"ok": full.database.get("ok", full.db_ok), "detail": None},
            "ingest": None,
            "disk": disk,
            "disk_volumes": volumes,
            "health": health,
        }
    )


@router.get(
    "/status/lag",
    response_model=IngestLagOut,
    summary="Ingest lag: seconds since the newest recording",
    description=(
        "`now − max(recorded_at)` over the archive, answered from the `ix_calls_recorded_at` "
        "index — one row read, no counts and no cache. This is what the header's lag badge "
        "polls on every page; `/api/stats/overview` carries the same figure among a dozen "
        "aggregates and is meant for the Statistics page."
    ),
)
def ingest_lag(db: DbSession) -> IngestLagOut:
    from datetime import UTC, datetime  # noqa: PLC0415 - as in status() above

    now = datetime.now(UTC)
    last = db.scalar(select(func.max(Call.recorded_at)))
    lag = None
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=UTC)
        lag = round((now - last).total_seconds(), 1)
    return IngestLagOut(ingest_lag_s=lag, last_recorded_at=last, server_time=now)


@router.get(
    "/healthz",
    summary="Liveness: the API is up and the database answers",
    description=(
        "A single `SELECT 1` and `{\"ok\": true}` — nothing counted, nothing cached, no "
        "disk stat. This is what a container health check or a load balancer should poll; "
        "`/api/status` is the operator-facing picture and is far more expensive. Answers "
        "503 when the database is unreachable, so an orchestrator can act on it."
    ),
    responses={503: {"description": "The database did not answer"}},
)
def healthz(db: ProbeSession) -> dict[str, Any]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - reporting this *is* the health check
        log.warning("healthz: database unreachable: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=503, content={"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        )
    return {"ok": True}


def legacy_status_dict(db: Session) -> dict[str, Any]:  # pragma: no cover - kept for the CLI
    return status(db).model_dump()


__all__ = ["VERSION", "healthz", "ingest_lag", "router"]
