"""``GET /api/status/activity`` — what the pipeline has been doing in the last few minutes.

``/api/status`` answers "is it up"; this answers the operator's other question, the one
that has had no answer at all until now: *are calls coming in, and what is happening to
them?*  One request returns a window's worth of counters — ingested, deduplicated,
processed, transcribed, exported, webhooked — each of them paired with the same counter
over the **previous** window of the same length, so the page can draw a trend arrow
without keeping history of its own.

Everything is keyed on the instant the work landed, not on when the call was recorded: a
call ingested at 10:00:03 counts in the 10:00 window even if the transmission happened an
hour earlier and the file was only just found.  That is the reading an operator watching
the screen wants — "is anything moving right now" — and it is the only one that makes the
recorder heartbeats mean anything.

Read access matches :mod:`app.api.status`: the counters are open, exactly like the rest of
the archive's reads.  The ``recent`` event list is **not** — those are log lines written for
an operator, and ``/api/activity`` has always been admin-only — so a signed-out caller gets
the tiles and an empty ticker.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, Text, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app import activity
from app.auth import OptionalPrincipal
from app.db import get_session
from app.models import (
    ActivityEvent,
    AudioJob,
    Call,
    CallExport,
    Recorder,
    TranscriptionJob,
    WebhookDelivery,
)

log = logging.getLogger("app.api.status_activity")
router = APIRouter(tags=["status"])
DbSession = Annotated[Session, Depends(get_session)]

#: Default window, in seconds: five minutes is long enough that a quiet channel still has
#: something in it and short enough that "now" still means now.
DEFAULT_WINDOW_S = 300
#: The longest window on offer.  Past an hour this stops being a live view and becomes
#: :mod:`app.api.stats`, which is the page that already answers it properly.
MAX_WINDOW_S = 3600

#: How far back the per-recorder "last call" look-back reaches.  A recorder that has been
#: silent for longer than this reports ``seconds_since_last_call: null`` rather than making
#: the query scan the whole ``calls`` table to date a heartbeat nobody is waiting on.
RECORDER_LOOKBACK_S = 86_400

#: ``audio_meta.content`` buckets reported as their own tiles
#: (:mod:`app.audioproc.process`: ``speech``/``page``/``none``/``static``).
CONTENT_TILES = {"speech": "speech", "page": "pages", "none": "no_speech", "static": "static"}

#: The ``kind`` prefixes that belong in an operational ticker.  ``upload``/``transcribe``
#: rows are the same work seen from the worker's side and would double every line, so the
#: ticker keeps the stage-level kinds an operator reads events by.
RECENT_KIND_PREFIXES = ("ingest", "export", "audio", "stt", "site", "recorder", "call")
#: How many ticker lines one response carries.
RECENT_LIMIT = 30

#: The ``call_exports.skip_reason`` prefixes, mapped to the counter they land in.  The
#: stored reason is the human sentence plus its detail ("no speech (0.04 s above -40 dB)"),
#: so the match is on the prefix and the order matters only in that every prefix here is
#: distinct.  A skip that matches none of them is counted as ``skipped_other`` rather than
#: dropped: an unexplained skip is exactly the thing an operator must be able to see.
SKIP_BUCKETS: tuple[tuple[str, str], ...] = (
    ("already exported as call", "skipped_duplicate"),
    ("no speech", "skipped_no_speech"),
    ("static", "skipped_static"),
    ("past the Broadcastify skew window", "skipped_late"),
    ("claimed by recorder", "claimed_by_recorder"),
)
#: Every key an export block carries, so a service with no traffic still renders a full
#: row of zeros instead of a ragged one.
EXPORT_KEYS = (
    "uploaded",
    "skipped_duplicate",
    "skipped_no_speech",
    "skipped_static",
    "skipped_late",
    "claimed_by_recorder",
    "skipped_other",
    "failed",
    "pending",
)


def _pair(column: Any, cur_start: datetime, prev_start: datetime) -> tuple[Any, Any]:
    """``(count over the current window, count over the one before it)`` for ``column``.

    Two filtered aggregates rather than two queries: PostgreSQL walks the rows once and the
    trend costs nothing beyond the counter it is a trend of.
    """
    return (
        func.count().filter(column >= cur_start),
        func.count().filter(column >= prev_start, column < cur_start),
    )


def _zero_pair() -> dict[str, int]:
    return {"current": 0, "previous": 0}


def _window(value: Any, previous: Any) -> dict[str, int]:
    return {"current": int(value or 0), "previous": int(previous or 0)}


def _seconds_since(at: datetime | None, now: datetime) -> float | None:
    if at is None:
        return None
    if at.tzinfo is None:  # pragma: no cover - defensive; every column is timestamptz
        at = at.replace(tzinfo=UTC)
    return round((now - at).total_seconds(), 1)


def _skip_bucket_case() -> Any:
    """A CASE that turns one export row into the counter name it belongs to."""
    branches = [
        (
            (CallExport.status == "skipped") & CallExport.skip_reason.like(f"{prefix}%"),
            bucket,
        )
        for prefix, bucket in SKIP_BUCKETS
    ]
    return func.coalesce(
        case(
            *branches,
            (CallExport.status == "skipped", "skipped_other"),
            else_=cast(CallExport.status, Text),
        ),
        "unknown",
    )


def _calls_block(
    db: Session, cur_start: datetime, prev_start: datetime
) -> dict[str, dict[str, int]]:
    """Ingest volume and the audio pass's verdict on it, both windows, in one pass."""
    columns: list[Any] = list(_pair(Call.ingested_at, cur_start, prev_start))
    names = ["ingested"]
    for content, key in CONTENT_TILES.items():
        names.append(key)
        expression = Call.audio_meta["content"].astext == content
        columns.append(func.count().filter(Call.ingested_at >= cur_start, expression))
        columns.append(
            func.count().filter(
                Call.ingested_at >= prev_start, Call.ingested_at < cur_start, expression
            )
        )
    row = db.execute(select(*columns).where(Call.ingested_at >= prev_start)).one()
    return {name: _window(row[index * 2], row[index * 2 + 1]) for index, name in enumerate(names)}


def _dedupe_block(db: Session, cur_start: datetime, prev_start: datetime) -> dict[str, int]:
    """Copies reconciliation ruled against, dated by the ruling rather than by the call."""
    current, previous = _pair(Call.reconciled_at, cur_start, prev_start)
    row = db.execute(
        select(current, previous).where(
            Call.reconciled_at >= prev_start, Call.duplicate_of.is_not(None)
        )
    ).one()
    return _window(row[0], row[1])


def _discard_block(db: Session, cur_start: datetime, prev_start: datetime) -> dict[str, int]:
    """Calls whose audio the discard pass (§5.6) threw away, dated by when it went."""
    current, previous = _pair(Call.audio_archived_at, cur_start, prev_start)
    row = db.execute(
        select(current, previous).where(
            Call.audio_archived_at >= prev_start, Call.audio_state == "discarded"
        )
    ).one()
    return _window(row[0], row[1])


def _jobs_block(db: Session, model: Any, cur_start: datetime, prev_start: datetime) -> dict[str, Any]:
    """``done`` in each window plus the depth of the queue right now, for one job table.

    Two statements rather than one ``FILTER``-ed pass over the table: that pass had no
    ``WHERE`` and so read every job ever run, twice a poll (~120 ms each on production,
    2026-09-23).  The windows are a range scan of the partial ``ix_*_jobs_finished``
    (``finished_at WHERE status IN ('done', 'failed')``) and the depth an index-only count
    over the partial ``ix_*_jobs_queued``.
    """
    row = db.execute(
        select(
            func.count().filter(model.finished_at >= cur_start),
            func.count().filter(model.finished_at < cur_start),
        ).where(model.status == "done", model.finished_at >= prev_start)
    ).one()
    queued = db.scalar(select(func.count()).select_from(model).where(model.status == "queued"))
    return {"done": _window(row[0], row[1]), "queued": int(queued or 0)}


def _activity_kinds_block(
    db: Session, kinds: tuple[str, ...], cur_start: datetime, prev_start: datetime
) -> dict[str, dict[str, int]]:
    current, previous = _pair(ActivityEvent.at, cur_start, prev_start)
    rows = db.execute(
        select(ActivityEvent.kind, current, previous)
        .where(ActivityEvent.at >= prev_start, ActivityEvent.kind.in_(kinds))
        .group_by(ActivityEvent.kind)
    ).all()
    found = {row[0]: _window(row[1], row[2]) for row in rows}
    return {kind: found.get(kind, _zero_pair()) for kind in kinds}


def _exports_block(
    db: Session, cur_start: datetime, prev_start: datetime
) -> tuple[dict[str, dict[str, dict[str, int]]], int]:
    """Per-service export outcomes for both windows, and the pending backlog overall."""
    bucket = _skip_bucket_case()
    current, previous = _pair(CallExport.updated_at, cur_start, prev_start)
    rows = db.execute(
        select(CallExport.service, bucket.label("bucket"), current, previous)
        .where(CallExport.updated_at >= prev_start)
        .group_by(CallExport.service, bucket)
    ).all()

    services: dict[str, dict[str, dict[str, int]]] = {}
    for service, name, in_window, before in rows:
        block = services.setdefault(service, {key: _zero_pair() for key in EXPORT_KEYS})
        if name not in block:  # a status we have no tile for, e.g. 'running'
            block[name] = _zero_pair()
        block[name] = _window(in_window, before)

    pending_rows = db.execute(
        select(CallExport.service, func.count())
        .where(CallExport.status == "pending")
        .group_by(CallExport.service)
    ).all()
    pending_total = 0
    for service, count in pending_rows:
        block = services.setdefault(service, {key: _zero_pair() for key in EXPORT_KEYS})
        block["pending"] = {"current": int(count or 0), "previous": int(count or 0)}
        pending_total += int(count or 0)
    return services, pending_total


def _webhooks_block(db: Session, cur_start: datetime, prev_start: datetime) -> dict[str, Any]:
    current, previous = _pair(WebhookDelivery.updated_at, cur_start, prev_start)
    rows = db.execute(
        select(WebhookDelivery.status, current, previous)
        .where(WebhookDelivery.updated_at >= prev_start)
        .group_by(WebhookDelivery.status)
    ).all()
    by_status = {row[0]: _window(row[1], row[2]) for row in rows}
    pending = int(
        db.scalar(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.status == "pending")
        )
        or 0
    )
    return {
        "delivered": by_status.get("delivered", _zero_pair()),
        "failed": by_status.get("failed", _zero_pair()),
        "pending": pending,
    }


#: Hostnames that mean "this machine" without needing to be parsed as an address.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_local_url(url: str | None) -> bool:
    """True when ``url`` points at the loopback — a link that only works *on the recorder*.

    The recorder's monitor binds to 127.0.0.1 unless its operator says otherwise, so the URL
    it reports is usually one that works in a browser on that box and nowhere else.  The
    page has to say so, and it is decided here rather than in the browser because the
    browser would have to re-implement the address parsing to do it — and get IPv6, which
    arrives bracketed, wrong on the first try.

    Anything unparseable is *not* called local: an address keyup cannot read is not one it
    should be making claims about, and the muted "(on that machine)" note is a promise, not
    a guess.
    """
    if not url:
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _monitor_block(reported: Any) -> dict[str, Any] | None:
    """The recorder's live-monitor report as the panel needs it, or ``None`` for no panel.

    Five keys out of the six the recorder sends: the listener counts are the monitor's own
    business and change every few seconds, which is exactly the sort of number a status row
    should not be re-rendering.  What the row needs is whether there is something to link
    to, where, and the two warnings that decide how the link reads.
    """
    if not isinstance(reported, dict):
        return None
    url = reported.get("url")
    return {
        "enabled": bool(reported.get("enabled")),
        "url": url if isinstance(url, str) else None,
        "stream_url": (
            reported["stream_url"] if isinstance(reported.get("stream_url"), str) else None
        ),
        "token_required": bool(reported.get("token_required")),
        "local_only": _is_local_url(url if isinstance(url, str) else None),
    }


def _recorders_block(
    db: Session, now: datetime, cur_start: datetime
) -> tuple[list[dict[str, Any]], datetime | None]:
    """One row per recorder instance: registration, volume in the window, and its heartbeat.

    Instances are the union of the ``recorders`` table and whatever ``calls.source_instance``
    actually carries, because a recorder that pushes before it registers — or one whose row
    was deleted while its calls remain — is precisely the anomaly this panel exists to show.
    """
    lookback = now - timedelta(seconds=RECORDER_LOOKBACK_S)
    call_rows = db.execute(
        select(
            Call.source_instance,
            func.count().filter(Call.ingested_at >= cur_start),
            func.max(Call.ingested_at),
        )
        .where(Call.ingested_at >= lookback)
        .group_by(Call.source_instance)
    ).all()
    by_instance = {
        (row[0] or ""): {"calls_in_window": int(row[1] or 0), "last_call_at": row[2]}
        for row in call_rows
    }

    registered = db.execute(
        select(
            Recorder.instance_id, Recorder.label, Recorder.last_seen_at, Recorder.monitor
        ).order_by(Recorder.instance_id)
    ).all()

    names: list[str] = [row[0] for row in registered]
    labels = {row[0]: row[1] for row in registered}
    seen = {row[0]: row[2] for row in registered}
    monitors = {row[0]: _monitor_block(row[3]) for row in registered}
    for instance in sorted(by_instance):
        if instance and instance not in labels:
            names.append(instance)

    out: list[dict[str, Any]] = []
    for instance in names:
        stats = by_instance.get(instance, {})
        last_call = stats.get("last_call_at")
        out.append(
            {
                "instance": instance,
                "label": labels.get(instance),
                "registered": instance in labels,
                "last_seen_at": seen[instance].isoformat() if seen.get(instance) else None,
                "seconds_since_last_seen": _seconds_since(seen.get(instance), now),
                "calls_in_window": int(stats.get("calls_in_window", 0)),
                "last_call_at": last_call.isoformat() if last_call else None,
                "seconds_since_last_call": _seconds_since(last_call, now),
                # None for an unregistered instance as much as for a recorder with no panel:
                # there is no row to have reported one.
                "monitor": monitors.get(instance),
            }
        )

    newest = max(
        (value["last_call_at"] for value in by_instance.values() if value["last_call_at"]),
        default=None,
    )
    return out, newest


def _recent(db: Session, limit: int) -> list[dict[str, Any]]:
    prefixes = [ActivityEvent.kind.like(f"{prefix}.%") for prefix in RECENT_KIND_PREFIXES]
    stmt: Select[Any] = (
        select(ActivityEvent)
        .where(or_(*prefixes))
        .order_by(ActivityEvent.id.desc())
        .limit(limit)
    )
    return [activity.to_dict(row) for row in db.scalars(stmt)]


@router.get(
    "/status/activity",
    summary="What the pipeline has done in the last few minutes",
    description=(
        "A live snapshot of the ingest → audio → transcription → export pipeline. Every "
        "counter is reported for the last `window` seconds **and** for the window before it, "
        "so a client can show a trend without keeping history. Also carries the queue depths, "
        "one heartbeat row per recorder instance — each with the `monitor` block the recorder "
        "reported, if it serves a live audio panel of its own, carrying `local_only` for a "
        "panel on a loopback address that only opens from that machine — and, for an "
        "administrator, the last "
        "operational log lines as a ticker. Counters are open like the rest of `/status`; "
        "`recent` is empty for anyone who is not an administrator."
    ),
)
def status_activity(
    db: DbSession,
    principal: OptionalPrincipal,
    window: Annotated[
        int,
        Query(ge=10, le=MAX_WINDOW_S, description="how many seconds back the counters reach"),
    ] = DEFAULT_WINDOW_S,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    cur_start = now - timedelta(seconds=window)
    prev_start = now - timedelta(seconds=2 * window)

    calls = _calls_block(db, cur_start, prev_start)
    audio = _jobs_block(db, AudioJob, cur_start, prev_start)
    transcription = _jobs_block(db, TranscriptionJob, cur_start, prev_start)
    kinds = _activity_kinds_block(db, ("stt.hallucination",), cur_start, prev_start)
    exports, exports_pending = _exports_block(db, cur_start, prev_start)
    recorders, last_call_at = _recorders_block(db, now, cur_start)

    counters: dict[str, dict[str, int]] = {
        "ingested": calls["ingested"],
        "duplicates_merged": _dedupe_block(db, cur_start, prev_start),
        "speech": calls["speech"],
        "pages": calls["pages"],
        "no_speech": calls["no_speech"],
        "static": calls["static"],
        "audio_processed": audio["done"],
        "audio_discarded": _discard_block(db, cur_start, prev_start),
        "transcribed": transcription["done"],
        "transcripts_rejected": kinds["stt.hallucination"],
    }

    is_admin = principal is not None and principal.is_admin
    return {
        "window_s": window,
        "server_time": now.isoformat(),
        "window_start": cur_start.isoformat(),
        "previous_window_start": prev_start.isoformat(),
        "counters": counters,
        "exports": exports,
        "webhooks": _webhooks_block(db, cur_start, prev_start),
        "queues": {
            "audio_queued": audio["queued"],
            "transcription_queued": transcription["queued"],
            "exports_pending": exports_pending,
        },
        "recorders": recorders,
        "last_call_at": last_call_at.isoformat() if last_call_at else None,
        "seconds_since_last_call": _seconds_since(last_call_at, now),
        "recent": _recent(db, RECENT_LIMIT) if is_admin else [],
        "recent_visible": is_admin,
    }


__all__ = ["DEFAULT_WINDOW_S", "MAX_WINDOW_S", "router"]
