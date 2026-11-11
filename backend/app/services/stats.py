"""SQL-side aggregations behind ``/api/stats`` (§4.5).

Everything here is a ``GROUP BY`` in PostgreSQL rather than a loop in Python: the archive
is 8 000 calls a day and growing, and the dashboard asks for these numbers on a timer.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.api.filters import CallFilters, aggregate_select, resolve_filter_sets
from app.config import get_settings
from app.models import (
    Call,
    Radio,
    RecorderGap,
    System,
    Talkgroup,
    Transcript,
    TranscriptionJob,
)
from app.services import counts

log = logging.getLogger("app.services.stats")

#: Transcript statuses that mean "this call was never going to have a transcript", which
#: are excluded from the coverage denominator — otherwise a policy that skips 14 % of the
#: corpus (the sub-second squelch tails) would read as 86 % coverage forever.
NOT_ELIGIBLE = ("skipped",)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


def local_today_bounds() -> tuple[datetime, datetime]:
    tz = _tz()
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


#: Cache key and TTL for :func:`overview` (app/services/counts.py).
OVERVIEW_KEY = "stats.overview"
OVERVIEW_TTL_S = 60.0


#: The archive-wide totals inside :func:`overview` — every non-duplicate call counted and
#: its duration summed, the transcript-eligible and done counts, the transcript total and
#: the real-time factor over every transcript ever made — cost about ten seconds of
#: sequential scanning on a three-million-row archive, and they move by a rounding error
#: from one minute to the next.  They get their own, longer cache; the today/24 h figures
#: next to them are index-range counts and stay on the 60 s one.
LIFETIME_KEY = "stats.overview.lifetime"
LIFETIME_TTL_S = 900.0


def _lifetime_totals(session: Session) -> dict[str, Any]:
    live = Call.duplicate_of.is_(None)
    # Exact, and not as expensive as it looks: ``ix_calls_duplicate_of`` covers the
    # predicate, so this is a parallel index-only scan (295 ms on a three-million-row
    # production table) rather than the five-gigabyte heap scan a bare ``count(*)`` is.  It
    # cannot be an estimate either way — ``reltuples`` counts the duplicates, and they are
    # 17 % of the archive.
    calls_total = int(session.scalar(select(func.count()).select_from(Call).where(live)) or 0)
    audio_total = float(
        session.scalar(select(func.coalesce(func.sum(Call.duration_s), 0)).where(live)) or 0
    )

    eligible = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(live, Call.transcript_status.notin_(NOT_ELIGIBLE))
        )
        or 0
    )
    done = int(
        session.scalar(
            select(func.count()).select_from(Call).where(live, Call.transcript_status == "done")
        )
        or 0
    )
    transcripts_total = int(session.scalar(select(func.count()).select_from(Transcript)) or 0)

    # RTF: audio seconds per second of processing, over transcripts that recorded both.
    rtf_row = session.execute(
        select(
            func.coalesce(func.sum(Call.duration_s), 0),
            func.coalesce(func.sum(Transcript.processing_ms), 0),
        )
        .select_from(Transcript)
        .join(Call, Transcript.call_id == Call.id)
        .where(Transcript.processing_ms.isnot(None), Transcript.processing_ms > 0)
    ).one()
    stt_rtf = (
        round(float(rtf_row[0]) / (float(rtf_row[1]) / 1000.0), 3) if float(rtf_row[1] or 0) > 0 else None
    )
    return {
        "calls_total": calls_total,
        "audio_total": audio_total,
        "eligible": eligible,
        "done": done,
        "transcripts_total": transcripts_total,
        "stt_rtf": stt_rtf,
    }


def overview(session: Session, *, force: bool = False) -> dict[str, Any]:
    """The dashboard headline: volume, coverage, throughput, lag and recorder health.

    **Cached for :data:`OVERVIEW_TTL_S` seconds, process-locally.**  The Statistics page
    polls ``/stats/overview`` once a minute (the header's lag badge used to as well, from
    every open tab; it reads ``/status/lag`` now), and the dozen aggregates
    below include four ``count(*)``s over three million ``calls`` rows and a sum over every
    transcript joined to its call.  The frontend's own poll interval is 60 s, so a 60 s
    cache costs a reader nothing and turns *n* tabs into one query.  ``force=True`` for a
    caller that genuinely needs it fresh.

    Every figure here stays exact: they are all either windowed on ``started_at`` or
    answered by an index-only scan, so the cache alone was the fix.
    """
    return counts.cached(OVERVIEW_KEY, OVERVIEW_TTL_S, lambda: _overview(session, force=force), force=force)


def _overview(session: Session, *, force: bool = False) -> dict[str, Any]:
    """The uncached measurement behind :func:`overview`."""
    now = datetime.now(UTC)
    day_start, day_end = local_today_bounds()
    live = Call.duplicate_of.is_(None)

    calls_today = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(live, Call.started_at >= day_start, Call.started_at < day_end)
        )
        or 0
    )
    calls_24h = int(
        session.scalar(
            select(func.count()).select_from(Call).where(live, Call.started_at >= now - timedelta(hours=24))
        )
        or 0
    )
    # A subset of ``calls_24h``, not an addition to it: private calls are counted on their
    # own because they are the traffic an operator most wants to know is being recorded, and
    # in the total they are a rounding error nobody would notice appear or stop.
    unit_calls_24h = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(live, Call.kind == "unit", Call.started_at >= now - timedelta(hours=24))
        )
        or 0
    )
    audio_today = float(
        session.scalar(
            select(func.coalesce(func.sum(Call.duration_s), 0)).where(
                live, Call.started_at >= day_start, Call.started_at < day_end
            )
        )
        or 0
    )

    life = counts.cached(LIFETIME_KEY, LIFETIME_TTL_S, lambda: _lifetime_totals(session), force=force)
    calls_total = life["calls_total"]
    audio_total = life["audio_total"]
    eligible = life["eligible"]
    done = life["done"]
    transcripts_total = life["transcripts_total"]
    stt_rtf = life["stt_rtf"]

    queue_depth = int(
        session.scalar(
            select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == "queued")
        )
        or 0
    )
    running = int(
        session.scalar(
            select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == "running")
        )
        or 0
    )
    failed = int(
        session.scalar(
            select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == "failed")
        )
        or 0
    )

    last_recorded = session.scalar(select(func.max(Call.recorded_at)))
    last_call = session.scalar(select(func.max(Call.started_at)))
    lag = None
    if last_recorded is not None:
        moment = last_recorded if last_recorded.tzinfo else last_recorded.replace(tzinfo=UTC)
        lag = round((now - moment).total_seconds(), 1)

    gaps_24h = int(
        session.scalar(
            select(func.count())
            .select_from(RecorderGap)
            .where(RecorderGap.ended_at >= now - timedelta(hours=24))
        )
        or 0
    )
    talkgroups_active = int(
        session.scalar(
            select(func.count(func.distinct(Call.talkgroup_id))).where(
                live, Call.started_at >= now - timedelta(hours=24)
            )
        )
        or 0
    )
    radios_active = int(
        session.scalar(
            select(func.count(func.distinct(Call.radio_id))).where(
                live, Call.started_at >= now - timedelta(hours=24)
            )
        )
        or 0
    )

    # A subset of nothing: these calls still count everywhere else — the archive knows they
    # happened, and most of them have a transcript.  Only the recording is gone.  Counted on
    # ``ingested_at`` rather than ``started_at`` because it is the discard *pass* being
    # reported on, and a backlog import of old traffic is discarded today.
    audio_discarded_24h = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(
                Call.audio_state == "discarded",
                Call.ingested_at >= now - timedelta(hours=24),
            )
        )
        or 0
    )

    # Calls the audio pass found no speech in — a mic keyed and released with nothing said.
    # Counted on ``started_at`` beside the other 24-hour numbers, because unlike the discard
    # count above this is a fact about the traffic rather than about a pass we ran.
    no_speech_24h = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(
                live,
                Call.started_at >= now - timedelta(hours=24),
                Call.audio_meta["content"].astext == "none",
            )
        )
        or 0
    )

    # …and the ones that were not silence but noise: broadband static across the whole
    # vocoder band (``audio_meta.content = "static"``, §5.5).  Counted apart from
    # ``no_speech_24h`` although both are refused by the same gates, because a rising
    # static count is a radio or an RF problem and a rising key-up count is not.
    static_24h = int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(
                live,
                Call.started_at >= now - timedelta(hours=24),
                Call.audio_meta["content"].astext == "static",
            )
        )
        or 0
    )

    free = total = None
    try:
        usage = shutil.disk_usage(get_settings().audio_root)
        free, total = usage.free, usage.total
    except OSError:  # the archive is not mounted here (CI, a fresh container)
        log.debug("stats: audio_root is not readable for a disk-usage probe")

    return {
        "calls_total": calls_total,
        "calls_today": calls_today,
        "calls_24h": calls_24h,
        "unit_calls_24h": unit_calls_24h,
        "audio_hours_total": round(audio_total / 3600, 3),
        "audio_hours_today": round(audio_today / 3600, 3),
        "transcript_coverage": round(done / eligible, 4) if eligible else 0.0,
        "transcripts_total": transcripts_total,
        "stt_rtf": stt_rtf,
        "queue_depth": queue_depth,
        "running_jobs": running,
        "failed_jobs": failed,
        "ingest_lag_s": lag,
        "last_call_at": last_call,
        "recorder_gaps_24h": gaps_24h,
        "talkgroups_active_24h": talkgroups_active,
        "radios_active_24h": radios_active,
        "audio_discarded_24h": audio_discarded_24h,
        "no_speech_24h": no_speech_24h,
        "static_24h": static_24h,
        "disk_free_bytes": free,
        "disk_total_bytes": total,
    }


def activity(
    session: Session,
    f: CallFilters,
    *,
    bucket: str = "hour",
    group_by: str = "talkgroup",
    limit_talkgroups: int = 25,
) -> dict[str, Any]:
    """Calls and audio seconds per time bucket, shaped for the heatmap.

    Buckets are cut on the **local** clock (``started_local``), so a "day" is a day as the
    dispatcher lived it, not a UTC day that starts at 19:00.

    ``group_by`` picks what the matrix's *rows* are:

    ``talkgroup``
        one row per talkgroup, busiest ``limit_talkgroups`` kept.

    ``day``
        one row per local calendar day, columns being the time of day — the
        day × hour grid the Statistics page draws by default.  Pair it with the default
        ``bucket="hour"``; with ``bucket="day"`` every row has a single column, which is
        the same thing said twice.

    ``none``
        a single ``all`` row — the whole archive as one series.
    """
    f = resolve_filter_sets(session, f)
    bucket = bucket if bucket in ("hour", "day") else "hour"
    group_by = group_by if group_by in ("talkgroup", "day", "none") else "talkgroup"
    group_by_talkgroup = group_by == "talkgroup"
    bucket_expr = func.date_trunc(bucket, Call.started_local).label("bucket")
    columns = [bucket_expr, func.count().label("calls"), func.coalesce(func.sum(Call.duration_s), 0)]
    group: list[Any] = [bucket_expr]
    if group_by_talkgroup:
        columns = [
            Call.talkgroup_id.label("talkgroup"),
            Talkgroup.tgid.label("tgid"),
            Talkgroup.alias.label("alias"),
            *columns,
        ]
        group = [Call.talkgroup_id, Talkgroup.tgid, Talkgroup.alias, bucket_expr]

    rows = list(
        session.execute(aggregate_select(f, *columns).group_by(*group).order_by(bucket_expr))
    )

    cells = []
    for row in rows:
        mapping = row._mapping
        cells.append(
            {
                "talkgroup": mapping.get("talkgroup"),
                "tgid": mapping.get("tgid"),
                "alias": mapping.get("alias"),
                "bucket": mapping["bucket"],
                "calls": int(mapping["calls"]),
                "seconds": float(list(mapping.values())[-1] or 0),
            }
        )

    # Matrix form for the heatmap.  Rows are talkgroups, local days or a single "all";
    # columns are the buckets actually present, both in a stable order.  Grouping by day
    # is the one shape that splits the bucket in two — the date picks the row, the time of
    # day picks the column — which is what makes a day × hour grid out of an hourly series.
    #
    # Hour columns are labeled bare ("14", not "14:00"): the reader appends the minutes
    # itself, so a label that carried them rendered as "14:00:00".
    hour_label, day_label = "%H", "%Y-%m-%d"
    if group_by == "day":
        column_format = hour_label if bucket == "hour" else day_label

        def column_key(when: datetime) -> str:
            return when.strftime(column_format)

        def row_key(when: datetime) -> str:
            return when.strftime(day_label)

        for cell in cells:
            # The row is the only name a day cell has; the talkgroup dimension is not in
            # this query at all.
            cell["alias"] = row_key(cell["bucket"])
        columns_present = sorted({column_key(c["bucket"]) for c in cells})
        row_keys: list[Any] = sorted({row_key(c["bucket"]) for c in cells})
        row_labels = list(row_keys)
        bucket_labels = columns_present
        index = {b: i for i, b in enumerate(columns_present)}
        key_index = {k: i for i, k in enumerate(row_keys)}
        matrix = [[0] * len(columns_present) for _ in row_keys]
        for cell in cells:
            matrix[key_index[row_key(cell["bucket"])]][index[column_key(cell["bucket"])]] += cell[
                "calls"
            ]
        return {
            "bucket": bucket,
            "cells": cells,
            "buckets": bucket_labels,
            "rows": row_labels,
            "matrix": matrix,
            "max": max((max(r) for r in matrix), default=0),
        }

    buckets = sorted({c["bucket"] for c in cells})
    label = hour_label if bucket == "hour" else day_label
    bucket_labels = [b.strftime(label) for b in buckets]
    if group_by_talkgroup:
        totals: dict[Any, int] = {}
        for cell in cells:
            totals[cell["talkgroup"]] = totals.get(cell["talkgroup"], 0) + cell["calls"]
        top = [k for k, _ in sorted(totals.items(), key=lambda kv: -kv[1])][:limit_talkgroups]
        names = {
            cell["talkgroup"]: (cell["alias"] or str(cell["tgid"] or cell["talkgroup"] or "?"))
            for cell in cells
        }
        row_keys = top
        row_labels = [names.get(k, str(k)) for k in row_keys]
    else:
        row_keys = [None]
        row_labels = ["all"]

    index = {b: i for i, b in enumerate(buckets)}
    matrix = [[0] * len(buckets) for _ in row_keys]
    key_index = {k: i for i, k in enumerate(row_keys)}
    for cell in cells:
        key = cell["talkgroup"] if group_by_talkgroup else None
        if key in key_index:
            matrix[key_index[key]][index[cell["bucket"]]] += cell["calls"]
    return {
        "bucket": bucket,
        "cells": cells,
        "buckets": bucket_labels,
        "rows": row_labels,
        "matrix": matrix,
        "max": max((max(r) for r in matrix), default=0),
    }


def _spark_map(session: Session, column: Any, keys: list[Any]) -> dict[Any, list[int]]:
    """24 hourly counts per key in one query, oldest hour first."""
    if not keys:
        return {}
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=23)
    hour = func.date_trunc("hour", Call.started_at)
    rows = session.execute(
        select(column, hour, func.count())
        .where(column.in_(keys), Call.started_at >= start, Call.duplicate_of.is_(None))
        .group_by(column, hour)
    )
    out: dict[Any, list[int]] = {key: [0] * 24 for key in keys}
    for key, bucket, count in rows:
        offset = int((bucket - start).total_seconds() // 3600)
        if key in out and 0 <= offset < 24:
            out[key][offset] = int(count)
    return out


def talkgroup_stats(session: Session, f: CallFilters, limit: int = 100) -> list[dict[str, Any]]:
    f = resolve_filter_sets(session, f)
    rows = list(
        session.execute(
            aggregate_select(
                f,
                Call.talkgroup_id,
                Talkgroup.tgid,
                Talkgroup.alias,
                Talkgroup.category,
                Talkgroup.agency_code,
                func.count().label("calls"),
                func.coalesce(func.sum(Call.duration_s), 0).label("seconds"),
                func.max(Call.started_at).label("last_heard_at"),
            )
            .where(Call.talkgroup_id.isnot(None))
            .group_by(
                Call.talkgroup_id, Talkgroup.tgid, Talkgroup.alias, Talkgroup.category, Talkgroup.agency_code
            )
            .order_by(func.count().desc())
            .limit(limit)
        )
    )
    sparks = _spark_map(session, Call.talkgroup_id, [r[0] for r in rows])
    return [
        {
            "talkgroup_id": r[0],
            "tgid": r[1],
            "alias": r[2],
            "category": r[3],
            "agency_code": r[4],
            "call_count": int(r[5]),
            "audio_seconds": float(r[6] or 0),
            "last_heard_at": r[7],
            "spark": sparks.get(r[0], [0] * 24),
        }
        for r in rows
    ]


def radio_stats(session: Session, f: CallFilters, limit: int = 100) -> list[dict[str, Any]]:
    f = resolve_filter_sets(session, f)
    rows = list(
        session.execute(
            aggregate_select(
                f,
                Call.radio_id,
                Radio.radio_id.label("radio_id_raw"),
                Radio.alias,
                Radio.unit_id,
                Radio.kind,
                Radio.agency_code,
                func.count().label("calls"),
                func.coalesce(func.sum(Call.duration_s), 0).label("seconds"),
                func.max(Call.started_at).label("last_heard_at"),
            )
            .where(Call.radio_id.isnot(None))
            .group_by(
                Call.radio_id, Radio.radio_id, Radio.alias, Radio.unit_id, Radio.kind, Radio.agency_code
            )
            .order_by(func.count().desc())
            .limit(limit)
        )
    )
    ids = [r[0] for r in rows]
    sparks = _spark_map(session, Call.radio_id, ids)
    top_tg: dict[int, str] = {}
    if ids:
        ranked = (
            select(
                Call.radio_id,
                func.coalesce(Talkgroup.alias, cast(Talkgroup.tgid, String)).label("alias"),
                func.count().label("calls"),
                func.row_number()
                .over(partition_by=Call.radio_id, order_by=func.count().desc())
                .label("rank"),
            )
            .join(Talkgroup, Call.talkgroup_id == Talkgroup.id)
            .where(Call.radio_id.in_(ids))
            .group_by(Call.radio_id, Talkgroup.alias, Talkgroup.tgid)
            .subquery()
        )
        best = session.execute(
            select(ranked.c.radio_id, ranked.c.alias).where(ranked.c.rank == 1)
        )
        top_tg = {row[0]: row[1] for row in best}
    return [
        {
            "radio_id": r[0],
            "radio_id_raw": r[1],
            "alias": r[2],
            "unit_id": r[3],
            "kind": r[4] or "unknown",
            "agency_code": r[5],
            "call_count": int(r[6]),
            "audio_seconds": float(r[7] or 0),
            "last_heard_at": r[8],
            "top_talkgroup": top_tg.get(r[0]),
            "spark": sparks.get(r[0], [0] * 24),
        }
        for r in rows
    ]


#: Cache key and TTL for the archive-wide half of :func:`transcription_stats`.  The status
#: and quality distributions are GROUP BYs over every call, and the per-backend table joins
#: every transcript to its call — together about ten seconds on production, and the join is
#: the query that once exhausted shared memory.  They move by a handful of rows a minute.
TRANSCRIPTION_KEY = "stats.transcription"
TRANSCRIPTION_TTL_S = 300.0


def transcription_stats(session: Session, *, force: bool = False) -> dict[str, Any]:
    """Queue depth, the status and quality distributions, and per-backend throughput.

    The queue figures (queued, running, failed in the last 24 h) are index-range counts and
    always current.  The distributions and the per-backend table are archive-wide scans
    and come from a cache of their own, recomputed at most every
    :data:`TRANSCRIPTION_TTL_S` seconds.
    """
    now = datetime.now(UTC)

    def by_status(status: str) -> Any:
        return select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == status)

    # Two counts on the partial ``ix_transcription_jobs_queued`` / ``…_running_failed``
    # indexes rather than a GROUP BY over the whole job history (millions of rows, most
    # of them done).
    queued = int(session.scalar(by_status("queued")) or 0)
    running = int(session.scalar(by_status("running")) or 0)
    failed_24h = int(
        session.scalar(by_status("failed").where(TranscriptionJob.finished_at >= now - timedelta(hours=24)))
        or 0
    )
    archive = counts.cached(
        TRANSCRIPTION_KEY, TRANSCRIPTION_TTL_S, lambda: _transcription_archive(session), force=force
    )
    return {
        "queue_depth": queued,
        "running": running,
        "failed_24h": failed_24h,
        **archive,
    }


def _transcription_archive(session: Session) -> dict[str, Any]:
    """The uncached archive-wide measurement behind :func:`transcription_stats`."""
    status = [
        {"status": s, "count": int(c)}
        for s, c in session.execute(
            select(Call.transcript_status, func.count())
            .where(Call.duplicate_of.is_(None))
            .group_by(Call.transcript_status)
            .order_by(func.count().desc())
        )
    ]
    quality = [
        {"quality": q or "(none)", "count": int(c)}
        for q, c in session.execute(
            select(Call.transcript_quality, func.count())
            .where(Call.duplicate_of.is_(None), Call.transcript_quality.isnot(None))
            .group_by(Call.transcript_quality)
            .order_by(func.count().desc())
        )
    ]
    backends = [
        {
            "backend_name": name or "(unknown)",
            "engine": engine,
            "calls": int(calls),
            "audio_seconds": float(audio or 0),
            "processing_seconds": round(float(processing or 0) / 1000.0, 3),
            "rtf": round(float(audio or 0) / (float(processing) / 1000.0), 3)
            if float(processing or 0) > 0
            else None,
        }
        for name, engine, calls, audio, processing in session.execute(
            select(
                Transcript.backend_name,
                Transcript.engine,
                func.count(),
                func.coalesce(func.sum(Call.duration_s), 0),
                func.coalesce(func.sum(Transcript.processing_ms), 0),
            )
            .join(Call, Transcript.call_id == Call.id)
            .group_by(Transcript.backend_name, Transcript.engine)
            .order_by(func.count().desc())
        )
    ]
    return {"status": status, "quality": quality, "backends": backends}


def gaps(
    session: Session,
    *,
    since: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    system_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Detected recorder outages, newest first (§3.4).

    ``since`` is the ``hours=`` window and asks "did it end recently".  ``start``/``end``
    are the ``from``/``to`` range and ask the other question a consumer actually has —
    "was the recorder down at any point during my incident" — so they match on *overlap*:
    a gap that began before ``start`` and ended after ``end`` covers the whole range and is
    the most interesting answer there is, and a containment test would miss it.
    """
    stmt = (
        select(RecorderGap, System.name)
        .join(System, RecorderGap.system_id == System.id)
        .order_by(RecorderGap.started_at.desc())
        .limit(limit)
        .offset(max(0, offset))
    )
    if since is not None:
        stmt = stmt.where(RecorderGap.ended_at >= since)
    if start is not None:
        stmt = stmt.where(RecorderGap.ended_at >= start)
    if end is not None:
        stmt = stmt.where(RecorderGap.started_at <= end)
    if system_id is not None:
        stmt = stmt.where(RecorderGap.system_id == system_id)
    return [
        {
            "id": gap.id,
            "system_id": gap.system_id,
            "system_name": name,
            "started_at": gap.started_at,
            "ended_at": gap.ended_at,
            "duration_s": float(gap.duration_s or 0),
            "minutes": round(float(gap.duration_s or 0) / 60, 1),
            "note": gap.note,
            "detected_at": gap.detected_at,
        }
        for gap, name in session.execute(stmt)
    ]


def timeline(
    session: Session, f: CallFilters, *, bucket: str = "minute", limit: int = 20000
) -> list[dict[str, Any]]:
    """Compact per-bucket activity — the sparkline behind an outside incident (§4.6)."""
    f = resolve_filter_sets(session, f)
    bucket = bucket if bucket in ("minute", "hour", "day") else "minute"
    expr = func.date_trunc(bucket, Call.started_at).label("bucket")
    rows = session.execute(
        aggregate_select(f, expr, func.count(), func.coalesce(func.sum(Call.duration_s), 0))
        .group_by(expr)
        .order_by(expr)
        .limit(limit)
    )
    return [{"minute": b, "count": int(c), "seconds": float(s or 0)} for b, c, s in rows]


def daily_counts(session: Session, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    expr = func.date_trunc("day", Call.started_local)
    return [
        {"day": d.date() if isinstance(d, datetime | date) else d, "count": int(c)}
        for d, c in session.execute(
            select(expr, func.count())
            .where(Call.started_at >= since, Call.duplicate_of.is_(None))
            .group_by(expr)
            .order_by(expr)
        )
    ]


__all__ = [
    "activity",
    "daily_counts",
    "gaps",
    "overview",
    "radio_stats",
    "talkgroup_stats",
    "timeline",
    "transcription_stats",
]
