"""Editing the deployment's own wiring from the GUI: STT backends and ingest sources (§4.7).

Two things were previously spelled only in the environment — ``STT_BACKENDS`` and
``AUDIO_ROOT`` — and therefore could only be changed by someone with shell access to the
host and permission to restart services.  This module is the CRUD that makes them rows, and
the two properties that make that worth doing:

* an edit **reaches the running processes** without a restart.  Each write bumps a
  generation counter in ``settings``; the transcription worker compares it on its
  ten-second publish tick and rebuilds its router (:mod:`app.stt.hotswap`), and the ingest
  watcher compares its own on a thirty-second tick and re-points its watchdog watches
  (:mod:`app.ingest.watcher`).  Neither is polled hard: the check is one indexed read of a
  single integer, and only a change costs anything.
* an edit **cannot destroy history**.  ``transcripts.backend_name`` names the engine behind
  every transcript in the archive, so deleting a backend that appears there marks the row
  deleted instead of dropping it, and the counters in ``stt_backend_stats`` are never
  touched at all.

``api_key`` is write-only throughout: accepted on POST and PUT, stored, and absent from
every response — the browser is told only whether one is set.  On a PUT, an omitted or
``null`` key means "leave it alone" and ``""`` means "clear it", so re-pointing a backend's
URL never requires re-pasting its credential.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import activity
from app.auth import AdminPrincipal
from app.config import get_settings
from app.db import get_session
from app.ingest import sources as source_store
from app.models import (
    ApiToken,
    Call,
    IngestFile,
    IngestSource,
    SttBackendConfig,
    SttBackendStat,
    Transcript,
)
from app.schemas import (
    IngestMigrationProgressIn,
    IngestSourceIn,
    IngestSourceOut,
    IngestSourcesOut,
    IngestSourceStatusOut,
    SttBackendConfigIn,
    SttBackendDeleteOut,
    SttBackendProbeOut,
    SttBackendPut,
    SttBackendStatOut,
)
from app.stt import config_store, stats_store

log = logging.getLogger("app.api.admin_config")
router = APIRouter(prefix="/admin", tags=["admin"])
DbSession = Annotated[Session, Depends(get_session)]

#: the benchmark clip a probe transcribes has to be short enough that the request returns
#: while the operator is still looking at the dialog, and long enough to contain speech
PROBE_CLIP_MIN_S = 1.0
PROBE_CLIP_MAX_S = 8.0

#: a probe must not hold an API worker for the router's full 300-second job timeout
PROBE_TIMEOUT_S = 45.0

#: how many probes may run at once across this process.  A probe is a *synchronous*
#: transcription on an API worker, so without a bound an operator holding the button on the
#: backends page — or two operators, or a browser retrying — can occupy every worker for the
#: better part of a minute each and take the API down with them.  Two is enough to compare
#: two backends side by side and few enough to leave the API answering.
PROBE_SLOTS = threading.BoundedSemaphore(2)

#: engine kinds that run the model *inside the process that asks*.  Probing one of these
#: from the API would load a multi-gigabyte model into the web worker — competing with the
#: transcription worker for the same GPU/NPU and the same RAM, and leaving it resident
#: afterwards — to time a device the API never transcribes on anyway.  The health check is
#: cheap and honest, so it still runs; the benchmark is the worker's to report.
LOCAL_ENGINE_KINDS = frozenset({"faster_whisper_local", "openvino_genai", "openvino_local"})


# --------------------------------------------------------------------------------------
# STT backends: the rows behind the page
# --------------------------------------------------------------------------------------


def configured_backends(db: Session) -> dict[str, dict[str, Any]]:
    """The backends this deployment runs on, keyed by name.

    Database first, environment only when the table is empty — the one precedence rule,
    read out of :func:`app.stt.config_store.entries` so that this process and the worker
    cannot disagree about what is configured.
    """
    return {
        (entry.get("name") or entry.get("kind")): entry for entry in config_store.entries(db)
    }


def backend_rows(db: Session, *, now: datetime | None = None) -> list[SttBackendStatOut]:
    """Every backend, its configuration and whatever the worker last published about it.

    Three populations, all of which the operator needs to be able to tell apart:

    * a configured backend with a fresh stats row — the normal case;
    * a configured backend with no row, or a stale one — "configured but never seen", which
      is not the same thing as "not configured" and must not look like an absence;
    * a stats row for a backend that has left the configuration — its history, kept because
      the counters record work that really happened.

    Configuration is authoritative for the descriptive columns even when the worker's row is
    stale: an operator who has just re-pointed a backend should see where it now points, not
    where it pointed before the worker died.
    """
    stamp = now or datetime.now(UTC)
    published = {row.name: row for row in db.scalars(select(SttBackendStat))}
    rows = {row.name: row for row in config_store.all_rows(db)}
    config = configured_backends(db)
    out: list[SttBackendStatOut] = []
    for name, entry in config.items():
        stat = published.get(name)
        config_row = rows.get(name)
        config_enabled = bool(entry.get("enabled", True))
        admin_enabled = stat.admin_enabled if stat is not None else None
        base = (
            SttBackendStatOut.model_validate(stat, from_attributes=True)
            if stat is not None
            else SttBackendStatOut(name=name)
        )
        base.kind = entry.get("kind") or base.kind
        base.model = entry.get("model") or base.model
        base.base_url = entry.get("base_url") or base.base_url
        base.priority = int(entry.get("priority", base.priority))
        base.concurrency = int(entry.get("concurrency", base.concurrency))
        base.config_enabled = config_enabled
        base.admin_enabled = admin_enabled
        base.enabled = admin_enabled if admin_enabled is not None else config_enabled
        base.stale = stats_store.is_stale(base.updated_at, now=stamp)
        _apply_config_row(base, config_row, entry)
        out.append(base)
    for name, stat in published.items():
        if name not in config:  # a backend that has left the configuration, with its history
            leftover = SttBackendStatOut.model_validate(stat, from_attributes=True)
            leftover.stale = True
            _apply_config_row(leftover, rows.get(name), None)
            out.append(leftover)
    out.sort(key=lambda item: (item.priority, item.name))
    return out


def _apply_config_row(
    out: SttBackendStatOut, row: SttBackendConfig | None, entry: dict[str, Any] | None
) -> None:
    """Fold the configuration half onto a response row, minus the key itself."""
    if row is not None:
        out.has_api_key = bool(row.api_key)
        out.config_source = row.source
        out.device = row.device
        out.compute_type = row.compute_type
        out.cpu_threads = row.cpu_threads
        out.cache_dir = row.cache_dir
        out.use_prompt = row.use_prompt
        out.use_hotwords = row.use_hotwords
        out.timeout = row.timeout
        out.notes = row.notes
        out.deleted_at = row.deleted_at
        # Only a row can be edited.  A backend that exists solely in ``STT_BACKENDS`` is
        # shown, and its switch works (the override is a stats-table column), but the edit
        # and delete buttons are off: there is nothing here to write to.
        out.editable = row.deleted_at is None
        return
    if entry is not None:
        out.has_api_key = bool(entry.get("api_key"))
        out.device = entry.get("device") or None
        out.compute_type = entry.get("compute_type") or None
        out.cpu_threads = entry.get("cpu_threads")
        out.cache_dir = entry.get("cache_dir") or None
        out.use_prompt = entry.get("use_prompt")
        out.use_hotwords = entry.get("use_hotwords")
        out.timeout = entry.get("timeout")


def _require_row(db: Session, name: str) -> SttBackendConfig:
    row = db.get(SttBackendConfig, name)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"no STT backend named {name!r}")
    return row


def _validate_kind(body: SttBackendConfigIn) -> None:
    """The per-kind requirements ``build_backends`` would otherwise raise at worker start.

    Checked here so the operator is told at the dialog rather than by a transcription queue
    that quietly stops moving, and so a bad row can never be the reason a worker's reload
    fails.
    """
    if not body.model.strip():
        raise HTTPException(status_code=422, detail="model is required")
    if body.kind in ("openai_compatible", "ovms") and not body.base_url.strip():
        raise HTTPException(status_code=422, detail=f"base_url is required for {body.kind}")


def _seed_if_empty(db: Session, principal: AdminPrincipal) -> None:
    """Materialize ``STT_BACKENDS`` before the first write.

    Without this, the first backend an operator adds to a deployment that has never been
    edited would become the *only* backend — the table stops being empty, so the environment
    stops being consulted, and the server that was transcribing a minute ago vanishes.  The
    seed makes the implicit configuration explicit at exactly the moment it starts to matter.
    """
    written = config_store.seed_from_env(db)
    if written:
        activity.record(
            db,
            source="stt",
            level="info",
            kind="stt.backends_seeded",
            message=f"seeded {written} backend(s) from STT_BACKENDS before the first edit",
            meta={"count": written, "by": principal.label},
        )


def _backend_response(db: Session, name: str) -> SttBackendStatOut:
    rows = {row.name: row for row in backend_rows(db)}
    if name not in rows:  # pragma: no cover - the row was just written
        raise HTTPException(status_code=404, detail=f"no STT backend named {name!r}")
    return rows[name]


@router.post(
    "/stt/backends",
    response_model=SttBackendStatOut,
    status_code=201,
    summary="Add a speech-to-text backend (admin)",
    description=(
        "Creates a row in `stt_backends` and bumps `stt_config_version`. The transcription "
        "worker rebuilds its router within its publish interval (about ten seconds) and "
        "starts routing to the new backend — no restart. `api_key` is write-only: it is "
        "stored and never returned. On a deployment that has never been edited, the "
        "backends from `STT_BACKENDS` are copied into the table first, so adding one does "
        "not silently remove the others."
    ),
)
def create_stt_backend(
    db: DbSession, principal: AdminPrincipal, body: SttBackendConfigIn
) -> SttBackendStatOut:
    _validate_kind(body)
    _seed_if_empty(db, principal)
    existing = db.get(SttBackendConfig, body.name)
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status_code=409, detail=f"a backend named {body.name!r} already exists")
    values = {field: getattr(body, field) for field in config_store.EDITABLE_FIELDS}
    values["api_key"] = body.api_key or ""
    if existing is not None:
        # Re-creating a soft-deleted name: the same identity comes back rather than being
        # refused, and its transcripts line up with it again.
        for field, value in values.items():
            setattr(existing, field, value)
        existing.deleted_at = None
        existing.source = "db"
        existing.updated_by = principal.label
        row = existing
    else:
        row = SttBackendConfig(name=body.name, source="db", updated_by=principal.label, **values)
        db.add(row)
    version = config_store.bump(db, by=principal.label)
    activity.record(
        db,
        source="stt",
        level="info",
        kind="stt.backend_created",
        message=f"backend {body.name} ({body.kind}) added by {principal.label}",
        meta={"backend": body.name, "kind": body.kind, "config_version": version},
    )
    db.commit()
    return _backend_response(db, body.name)


@router.put(
    "/stt/backends/{name}",
    response_model=SttBackendStatOut,
    summary="Edit a speech-to-text backend (admin)",
    description=(
        "**A full replace, not a patch**: every editable field is written from the body, so "
        "one the body omits is reset to its default — send the whole row back, or an edit "
        "to `model` alone will silently reset `concurrency` and `priority`. Bumps "
        "`stt_config_version`, which the worker "
        "picks up within about ten seconds; attempts already in flight finish on the old "
        "router. `api_key` is the one exception, being write-only — omit it (or send "
        "`null`) to keep the stored key, "
        "send `\"\"` to clear it. A backend that exists only in `STT_BACKENDS` cannot be "
        "edited here until the table has been seeded, which the first write does. The "
        "runtime on/off switch is `PUT /admin/stt/backends/{name}/enabled`."
    ),
)
def update_stt_backend(
    db: DbSession, principal: AdminPrincipal, name: str, body: SttBackendPut
) -> SttBackendStatOut:
    _seed_if_empty(db, principal)
    _validate_kind(body)
    row = _require_row(db, name)
    before = {"kind": row.kind, "base_url": row.base_url, "model": row.model, "enabled": row.enabled}
    for field in config_store.EDITABLE_FIELDS:
        if field == "api_key":
            continue
        setattr(row, field, getattr(body, field))
    if body.api_key is not None:
        # "" is a real instruction (clear it); None means the operator did not touch it.
        row.api_key = body.api_key
    row.source = "db"
    row.updated_by = principal.label
    version = config_store.bump(db, by=principal.label)
    activity.record(
        db,
        source="stt",
        level="info",
        kind="stt.backend_updated",
        message=f"backend {name} edited by {principal.label}",
        meta={"backend": name, "before": before, "config_version": version},
    )
    db.commit()
    return _backend_response(db, name)


@router.delete(
    "/stt/backends/{name}",
    response_model=SttBackendDeleteOut,
    summary="Remove a speech-to-text backend (admin)",
    description=(
        "Removes the backend from the routing configuration. If any transcript names it — "
        "`transcripts.backend_name` records which engine produced every transcript in the "
        "archive — the row is *retained*, marked deleted and disabled, so the history stays "
        "answerable; otherwise it is dropped. Either way the counters in `stt_backend_stats` "
        "are left alone: they record work that really happened."
    ),
)
def delete_stt_backend(
    db: DbSession, principal: AdminPrincipal, name: str
) -> SttBackendDeleteOut:
    _seed_if_empty(db, principal)
    row = _require_row(db, name)
    transcripts = int(
        db.scalar(
            select(func.count()).select_from(Transcript).where(Transcript.backend_name == name)
        )
        or 0
    )
    if transcripts:
        row.deleted_at = datetime.now(UTC)
        row.enabled = False
        row.updated_by = principal.label
    else:
        db.delete(row)
    version = config_store.bump(db, by=principal.label)
    activity.record(
        db,
        source="stt",
        level="info",
        kind="stt.backend_deleted",
        message=(
            f"backend {name} retained (disabled) by {principal.label}: {transcripts} transcript(s) name it"
            if transcripts
            else f"backend {name} removed by {principal.label}"
        ),
        meta={"backend": name, "transcripts": transcripts, "config_version": version},
    )
    db.commit()
    return SttBackendDeleteOut(
        name=name,
        removed=not transcripts,
        retained=bool(transcripts),
        transcripts=transcripts,
        config_version=version,
    )


# --------------------------------------------------------------------------------------
# probing one backend
# --------------------------------------------------------------------------------------


def _probe_clip(db: Session) -> tuple[Path, float, int] | None:
    """A short real recording to benchmark with, or ``None``.

    The archive's own audio rather than a bundled sample: a backend is about to be asked to
    transcribe *this deployment's* traffic, at its bitrate, with its noise floor, and a
    synthetic clip would answer a question nobody asked.
    """
    root = Path(get_settings().audio_root)
    rows = db.execute(
        select(Call.id, Call.file_path, Call.duration_s)
        .where(
            Call.file_path.is_not(None),
            Call.duration_s >= PROBE_CLIP_MIN_S,
            Call.duration_s <= PROBE_CLIP_MAX_S,
        )
        .order_by(Call.id.desc())
        .limit(20)
    ).all()
    for call_id, file_path, duration in rows:
        candidate = Path(file_path)
        full = candidate if candidate.is_absolute() else root / candidate
        if full.is_file():
            return full, float(duration or 0.0), int(call_id)
    return None


@router.post(
    "/stt/backends/{name}/probe",
    response_model=SttBackendProbeOut,
    summary="Check one speech-to-text backend, and time it on a real clip (admin)",
    description=(
        "Builds the backend from its saved settings *in this process* and asks it two "
        "questions: does it answer its health check (is the server up, does it have the "
        "model), and can it actually transcribe — timed against a short recording from this "
        "archive, reported as latency and real-time factor. The second question is the one "
        "that matters, because a server can answer `/health` and still not have the model "
        "loaded. Nothing is written: this does not touch the worker's router, the queue or "
        "any call. The probe runs synchronously and is capped at 45 seconds, and at most "
        "two run at once across the API process — a third gets `429`, because a probe "
        "occupies a web worker for as long as it takes. A backend whose engine runs "
        "in-process (`faster_whisper_local`, `openvino_genai`) is health-checked — which "
        "for those means the model loads — but **not** benchmarked: transcribing here would "
        "contend with the transcription worker for the same device and time a path the API "
        "never runs anyway, so the timing is the worker's to report."
    ),
)
def probe_stt_backend(
    db: DbSession, principal: AdminPrincipal, name: str
) -> SttBackendProbeOut:
    if not PROBE_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="two probes are already running; try again in a moment",
        )
    try:
        return _probe_stt_backend(db, principal, name)
    finally:
        PROBE_SLOTS.release()


def _probe_stt_backend(
    db: Session, principal: AdminPrincipal, name: str
) -> SttBackendProbeOut:
    from app.stt.router import build_backends  # noqa: PLC0415 - optional engine extras

    entry = configured_backends(db).get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no STT backend named {name!r}")
    now = datetime.now(UTC)
    if entry.get("kind") in LOCAL_ENGINE_KINDS:
        return _probe_health_only(db, principal, name, entry, now)
    try:
        built = build_backends([dict(entry)], include_disabled=True)
    except Exception as exc:  # noqa: BLE001 - a misconfigured or unavailable engine
        return SttBackendProbeOut(
            name=name,
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            kind=entry.get("kind"),
            model=entry.get("model"),
            checked_at=now,
            benchmark_skipped="the backend could not be constructed",
        )
    backend = built[0]
    started = time.monotonic()
    try:
        health = backend.health()
        ok, detail = bool(health.ok), health.detail
    except Exception as exc:  # noqa: BLE001 - an unreachable server is a result, not a 500
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    health_s = round(time.monotonic() - started, 3)
    out = SttBackendProbeOut(
        name=name,
        ok=ok,
        detail=detail,
        kind=entry.get("kind"),
        model=entry.get("model"),
        health_s=health_s,
        checked_at=now,
    )
    if not ok:
        out.benchmark_skipped = "the health check failed"
        _record_probe(db, principal, out)
        return out
    clip = _probe_clip(db)
    if clip is None:
        out.benchmark_skipped = "no recording between 1 and 8 seconds is available to time"
        _record_probe(db, principal, out)
        return out
    path, audio_s, call_id = clip
    from app.stt.types import TranscribeOptions  # noqa: PLC0415

    settings = get_settings()
    options = TranscribeOptions(
        language=settings.stt_language or None,
        vad=settings.stt_vad,
        # The backend's own timeout, capped: an operator waiting on a dialog must not be
        # held for the five minutes a batch job is allowed.
        timeout_s=min(PROBE_TIMEOUT_S, float(entry.get("timeout") or PROBE_TIMEOUT_S)),
    )
    started = time.monotonic()
    try:
        result = backend.transcribe(path, options)
    except Exception as exc:  # noqa: BLE001
        out.ok = False
        out.detail = f"{type(exc).__name__}: {exc}"
        out.clip_call_id = call_id
        out.audio_s = round(audio_s, 2)
        out.benchmark_skipped = "the transcription attempt failed"
        _record_probe(db, principal, out)
        return out
    latency = time.monotonic() - started
    out.clip_call_id = call_id
    out.audio_s = round(audio_s, 2)
    out.latency_s = round(latency, 3)
    out.rtf = round(audio_s / latency, 2) if latency > 0 else None
    out.model = getattr(result, "model", None) or out.model
    out.text = (getattr(result, "text", "") or "")[:500]
    _record_probe(db, principal, out)
    return out


def _probe_health_only(
    db: Session,
    principal: AdminPrincipal,
    name: str,
    entry: dict[str, Any],
    now: datetime,
) -> SttBackendProbeOut:
    """Health, and no benchmark, for an engine that would run *in this process*.

    A remote backend's probe measures a server the worker also talks to, so the number the
    dialog shows is the number the worker will see.  A local engine's would measure this
    API worker instead — a different process, contending for the same GPU/NPU as the real
    transcription worker, on hardware the API is never asked to transcribe on.  The figure
    would be misleading even when it is not actively harmful, so the honest answer is the
    half that transfers: can the engine be constructed and does its model load.
    """
    from app.stt.router import build_backends  # noqa: PLC0415 - optional engine extras

    out = SttBackendProbeOut(
        name=name,
        ok=False,
        kind=entry.get("kind"),
        model=entry.get("model"),
        checked_at=now,
        benchmark_skipped="local engines are probed by the worker",
    )
    started = time.monotonic()
    try:
        backend = build_backends([dict(entry)], include_disabled=True)[0]
        health = backend.health()
        out.ok, out.detail = bool(health.ok), health.detail
    except Exception as exc:  # noqa: BLE001 - a missing extra or an absent model is a result
        out.detail = f"{type(exc).__name__}: {exc}"
    out.health_s = round(time.monotonic() - started, 3)
    _record_probe(db, principal, out)
    return out


def _record_probe(db: Session, principal: AdminPrincipal, out: SttBackendProbeOut) -> None:
    activity.record(
        db,
        source="stt",
        level="info" if out.ok else "warn",
        kind="stt.backend_probed",
        message=(
            f"backend {out.name} probed by {principal.label}: "
            + ("ok" if out.ok else f"failed — {out.detail}")
            + (f", {out.rtf}× real time" if out.rtf else "")
        ),
        meta={
            "backend": out.name,
            "ok": out.ok,
            "rtf": out.rtf,
            "latency_s": out.latency_s,
            "by": principal.label,
        },
    )
    db.commit()


# --------------------------------------------------------------------------------------
# ingest sources
# --------------------------------------------------------------------------------------


def _source_out(row: IngestSource, *, token: ApiToken | None = None) -> IngestSourceOut:
    state, detail = (
        source_store.path_state(row.path) if row.kind == "watch_dir" else ("n/a", None)
    )
    active: bool | None = None
    if token is not None:
        now = datetime.now(UTC)
        expires = token.expires_at
        active = token.revoked_at is None and (expires is None or expires > now)
    return IngestSourceOut(
        id=row.id,
        kind=row.kind,
        label=row.label,
        enabled=row.enabled,
        path=row.path,
        recursive=row.recursive,
        tr_prefer_audio=row.tr_prefer_audio,
        tr_sidecar_wait_seconds=row.tr_sidecar_wait_seconds,
        token_id=row.token_id,
        token_label=token.label if token is not None else None,
        token_active=active,
        notes=row.notes,
        meta=row.meta,
        last_activity_at=row.last_activity_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        path_state=state,
        path_detail=detail,
    )


def _tokens_for(db: Session, rows: list[IngestSource]) -> dict[int, ApiToken]:
    ids = {row.token_id for row in rows if row.token_id is not None}
    if not ids:
        return {}
    return {t.id: t for t in db.scalars(select(ApiToken).where(ApiToken.id.in_(ids)))}


def _validate_source(db: Session, body: IngestSourceIn, *, source_id: int | None = None) -> None:
    """Reject a row the watcher could not act on, at the dialog rather than in a log.

    A bad path is *not* a rejection: an operator may well configure a spool before the mount
    exists, and refusing that would be less useful than saving it and showing it red. What is
    rejected is a row that is structurally wrong for its kind, or a duplicate.
    """
    if body.kind == "watch_dir":
        if not (body.path or "").strip():
            raise HTTPException(status_code=422, detail="path is required for a watch_dir source")
        clash = db.scalar(
            select(IngestSource.id).where(
                IngestSource.kind == "watch_dir",
                IngestSource.path == body.path,
                IngestSource.id != (source_id or -1),
            )
        )
        if clash is not None:
            raise HTTPException(status_code=409, detail=f"{body.path} is already a source")
    elif body.path:
        raise HTTPException(status_code=422, detail=f"a {body.kind} source has no path")
    if body.kind == "push_token":
        if body.token_id is None:
            raise HTTPException(
                status_code=422, detail="token_id is required for a push_token source"
            )
        if db.get(ApiToken, body.token_id) is None:
            raise HTTPException(status_code=404, detail=f"no token with id {body.token_id}")
    elif body.token_id is not None:
        raise HTTPException(status_code=422, detail=f"a {body.kind} source has no token")
    clash = db.scalar(
        select(IngestSource.id).where(
            IngestSource.label == body.label, IngestSource.id != (source_id or -1)
        )
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail=f"a source called {body.label!r} already exists")


@router.get(
    "/ingest/sources",
    response_model=IngestSourcesOut,
    summary="Where recordings come from (admin)",
    description=(
        "Every configured ingest source: the directories the watcher watches, the upload "
        "tokens recorders push through, and any bulk import that has reported progress. "
        "`path_state` is checked on every read rather than stored, because a directory that "
        "was readable when it was saved is exactly the thing that stops being readable at "
        "three in the morning. `config_version` is the generation the watcher polls for."
    ),
)
def list_ingest_sources(db: DbSession, principal: AdminPrincipal) -> IngestSourcesOut:
    rows = list(db.scalars(select(IngestSource).order_by(IngestSource.kind, IngestSource.id)))
    tokens = _tokens_for(db, rows)
    return IngestSourcesOut(
        sources=[
            _source_out(row, token=tokens.get(row.token_id) if row.token_id else None)
            for row in rows
        ],
        config_version=source_store.version(db),
    )


@router.post(
    "/ingest/sources",
    response_model=IngestSourceOut,
    status_code=201,
    summary="Add an ingest source (admin)",
    description=(
        "Adds a source and bumps `ingest_config_version`. A `watch_dir` starts being watched "
        "within about thirty seconds — the watcher re-reads the table on a timer and "
        "schedules a new watchdog watch — with no restart. A path that does not exist yet is "
        "saved anyway and shown as unreadable: configuring a spool before its mount comes up "
        "is a normal thing to do."
    ),
)
def create_ingest_source(
    db: DbSession, principal: AdminPrincipal, body: IngestSourceIn
) -> IngestSourceOut:
    _validate_source(db, body)
    row = IngestSource(
        kind=body.kind,
        label=body.label,
        enabled=body.enabled,
        path=body.path or None,
        recursive=body.recursive,
        tr_prefer_audio=body.tr_prefer_audio,
        tr_sidecar_wait_seconds=body.tr_sidecar_wait_seconds,
        token_id=body.token_id,
        notes=body.notes,
        updated_by=principal.label,
    )
    db.add(row)
    db.flush()
    version = source_store.bump(db, by=principal.label)
    state, _ = source_store.path_state(row.path) if row.kind == "watch_dir" else ("n/a", None)
    activity.record(
        db,
        source="ingest",
        level="info" if state in ("ok", "n/a") else "warn",
        kind="ingest.source_created",
        message=f"ingest source {row.label} ({row.kind}) added by {principal.label}",
        meta={"source": row.id, "kind": row.kind, "path": row.path, "path_state": state,
              "config_version": version},
    )
    db.commit()
    token = db.get(ApiToken, row.token_id) if row.token_id else None
    return _source_out(row, token=token)


@router.put(
    "/ingest/sources/{source_id}",
    response_model=IngestSourceOut,
    summary="Edit an ingest source (admin)",
    description=(
        "Replaces the source's settings and bumps `ingest_config_version`. Changing a "
        "`watch_dir`'s path or switching it off re-points the watcher's watches on its next "
        "reload; files already on its queue are still ingested."
    ),
)
def update_ingest_source(
    db: DbSession, principal: AdminPrincipal, source_id: int, body: IngestSourceIn
) -> IngestSourceOut:
    row = db.get(IngestSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no ingest source with id {source_id}")
    if body.kind != row.kind:
        raise HTTPException(status_code=422, detail="a source's kind cannot be changed")
    _validate_source(db, body, source_id=source_id)
    before = {"path": row.path, "enabled": row.enabled, "label": row.label}
    row.label = body.label
    row.enabled = body.enabled
    row.path = body.path or None
    row.recursive = body.recursive
    row.tr_prefer_audio = body.tr_prefer_audio
    row.tr_sidecar_wait_seconds = body.tr_sidecar_wait_seconds
    row.token_id = body.token_id
    row.notes = body.notes
    row.updated_by = principal.label
    if before["path"] != row.path:
        # The old path's failure is not the new path's problem.
        row.last_error = None
        row.last_error_at = None
    version = source_store.bump(db, by=principal.label)
    activity.record(
        db,
        source="ingest",
        level="info",
        kind="ingest.source_updated",
        message=f"ingest source {row.label} edited by {principal.label}",
        meta={"source": row.id, "before": before, "config_version": version},
    )
    db.commit()
    token = db.get(ApiToken, row.token_id) if row.token_id else None
    return _source_out(row, token=token)


@router.delete(
    "/ingest/sources/{source_id}",
    status_code=204,
    summary="Remove an ingest source (admin)",
    description=(
        "Deletes the row and bumps `ingest_config_version`; a `watch_dir` stops being watched "
        "on the watcher's next reload. Nothing already ingested is affected — the calls and "
        "the ledger rows belong to the archive, not to the source that found them."
    ),
)
def delete_ingest_source(db: DbSession, principal: AdminPrincipal, source_id: int) -> Response:
    row = db.get(IngestSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no ingest source with id {source_id}")
    label, kind, path = row.label, row.kind, row.path
    db.delete(row)
    version = source_store.bump(db, by=principal.label)
    activity.record(
        db,
        source="ingest",
        level="info",
        kind="ingest.source_deleted",
        message=f"ingest source {label} ({kind}) removed by {principal.label}",
        meta={"source": source_id, "kind": kind, "path": path, "config_version": version},
    )
    db.commit()
    return Response(status_code=204)


@router.get(
    "/ingest/sources/{source_id}/status",
    response_model=IngestSourceStatusOut,
    summary="What one ingest source has actually done (admin)",
    description=(
        "The per-source answer to 'is this thing on': the current readability of a watched "
        "directory, its ledger counts for the last 24 hours, and — for a push source — how "
        "many calls arrived on its token. Counted from `ingest_files` and `calls` rather "
        "than from anything the watcher remembers, so it survives a restart."
    ),
)
def ingest_source_status(
    db: DbSession, principal: AdminPrincipal, source_id: int
) -> IngestSourceStatusOut:
    row = db.get(IngestSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no ingest source with id {source_id}")
    since = datetime.now(UTC) - timedelta(hours=24)
    state, detail = (
        source_store.path_state(row.path) if row.kind == "watch_dir" else ("n/a", None)
    )
    out = IngestSourceStatusOut(
        id=row.id,
        kind=row.kind,
        label=row.label,
        enabled=row.enabled,
        path_state=state,
        path_detail=detail,
        last_activity_at=row.last_activity_at,
        last_error=row.last_error,
        last_error_at=row.last_error_at,
        meta=row.meta,
    )
    if row.kind == "watch_dir" and row.path:
        # The ledger stores paths relative to AUDIO_ROOT where it can, absolute otherwise;
        # match on either so a spool outside the archive root still counts.
        root = Path(get_settings().audio_root)
        prefixes = [f"{row.path.rstrip('/')}/"]
        with contextlib.suppress(ValueError):  # the source lies outside the archive root
            prefixes.append(f"{Path(row.path).resolve().relative_to(root.resolve())}/")
        clauses = [IngestFile.file_path.like(f"{prefix}%") for prefix in prefixes]
        window = IngestFile.first_seen_at >= since
        out.files_new_24h = int(
            db.scalar(
                select(func.count())
                .select_from(IngestFile)
                .where(or_(*clauses), window, IngestFile.status == "ingested")
            )
            or 0
        )
        out.files_failed_24h = int(
            db.scalar(
                select(func.count())
                .select_from(IngestFile)
                .where(or_(*clauses), window, IngestFile.status == "failed")
            )
            or 0
        )
    if row.kind == "push_token" and row.token_id is not None:
        # Two different facts, and it matters which is which.  ``calls_24h`` is every call
        # that arrived over the push path, *not* this token's share: a call row records how
        # it arrived (``ingest_source``) but not which token authenticated it, so a
        # per-token count would be a number the database cannot actually support.  What is
        # per-token is ``api_tokens.last_used_at``, which the upload path stamps on every
        # accepted push — so that, and not the count, is this source's own last activity.
        token = db.get(ApiToken, row.token_id)
        out.calls_24h = int(
            db.scalar(
                select(func.count())
                .select_from(Call)
                .where(Call.ingest_source != "file", Call.ingested_at >= since)
            )
            or 0
        )
        if token is not None and token.last_used_at is not None:
            out.last_activity_at = out.last_activity_at or token.last_used_at
    return out


@router.post(
    "/ingest/migration-progress",
    response_model=IngestSourceOut,
    summary="Report a bulk import's progress (admin)",
    description=(
        "The migration tool is a CLI somewhere else; this is how a long import gets a status "
        "card on the Ingest tab instead of being invisible. Posting creates the `migration` "
        "source named by `label` the first time and updates it thereafter. Deliberately "
        "additive — it never bumps `ingest_config_version`, because a progress report is not "
        "a configuration change and must not make the watcher reload."
    ),
)
def report_migration_progress(
    db: DbSession, principal: AdminPrincipal, body: IngestMigrationProgressIn
) -> IngestSourceOut:
    row = db.scalar(
        select(IngestSource).where(
            IngestSource.kind == "migration", IngestSource.label == body.label
        )
    )
    if row is None:
        row = IngestSource(
            kind="migration", label=body.label, enabled=True, updated_by=principal.label
        )
        db.add(row)
        db.flush()
    meta = dict(row.meta or {})
    for field in ("state", "files_total", "files_done", "files_failed", "message"):
        value = getattr(body, field)
        if value is not None:
            meta[field] = value
    meta["finished"] = body.finished
    meta["reported_at"] = datetime.now(UTC).isoformat()
    row.meta = meta
    row.last_activity_at = datetime.now(UTC)
    if body.files_failed:
        row.last_error = f"{body.files_failed} file(s) failed"
        row.last_error_at = datetime.now(UTC)
    db.commit()
    return _source_out(row)
