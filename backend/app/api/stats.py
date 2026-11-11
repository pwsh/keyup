"""Statistics for the dashboard and the Statistics page (§4.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.filters import CallFilters, Filters
from app.auth import OptionalPrincipal
from app.db import get_session
from app.schemas import (
    ActivityResponse,
    RadioStat,
    RecorderGapOut,
    StatsOverview,
    TalkgroupStat,
    TimelinePoint,
    TranscriptionStats,
)
from app.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])
DbSession = Annotated[Session, Depends(get_session)]


@router.get(
    "/overview",
    response_model=StatsOverview,
    summary="Headline numbers",
    description=(
        "Totals and today's volume, hours of audio, transcript coverage (calls with a transcript "
        "over calls eligible for one — the sub-second squelch tails the policy skips are excluded "
        "from the denominator), the transcription queue depth, the STT real-time factor measured "
        "from `transcripts.processing_ms`, the ingest lag (`now − max(recorded_at)`) and the "
        "recorder gaps detected in the last 24 hours."
    ),
)
def overview(db: DbSession) -> StatsOverview:
    return StatsOverview.model_validate(stats_service.overview(db))


@router.get(
    "/activity",
    response_model=ActivityResponse,
    summary="Calls per bucket per talkgroup",
    description=(
        "Heatmap-ready: `cells` is the raw `{talkgroup, bucket, calls, seconds}` series and "
        "`matrix`/`rows`/`buckets` is the same data shaped for a grid, with `group_by` deciding "
        "what a row is. Buckets are cut on the **local** clock, so a day is the day the "
        "dispatcher lived. Accepts the whole call filter set on top of `from`/`to`."
    ),
)
def activity(
    f: Filters,
    db: DbSession,
    from_: Annotated[
        str | None, Query(alias="from", description="Start (date or timestamp); default 7 days ago")
    ] = None,
    to: Annotated[str | None, Query(description="End (date or timestamp); default now")] = None,
    bucket: Annotated[Literal["hour", "day"], Query(description="Bucket width")] = "hour",
    group_by: Annotated[
        Literal["talkgroup", "day", "none"],
        Query(
            description=(
                "Rows of the matrix: one per talkgroup, one per **local** calendar day "
                "(pair with the default `bucket=hour` for a day x hour grid), or a single "
                "`all` row."
            )
        ),
    ] = "talkgroup",
    limit_talkgroups: Annotated[int, Query(ge=1, le=200, description="Rows kept in the matrix")] = 25,
) -> ActivityResponse:
    from app.api.filters import _as_datetime  # noqa: PLC0415 - one shared parser, kept private

    start = _as_datetime(from_, end_of_day=False) or (datetime.now(UTC) - timedelta(days=7))
    end = _as_datetime(to, end_of_day=True) or datetime.now(UTC)
    scoped = f.replace(date_from=f.date_from or start, date_to=f.date_to or end)
    return ActivityResponse.model_validate(
        stats_service.activity(
            db,
            scoped,
            bucket=bucket,
            group_by=group_by,
            limit_talkgroups=limit_talkgroups,
        )
    )


@router.get(
    "/talkgroups",
    response_model=list[TalkgroupStat],
    summary="Per-talkgroup volume",
    description="Calls, audio seconds, last heard and a 24-hour sparkline, busiest first.",
)
def talkgroups(
    f: Filters, db: DbSession, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[TalkgroupStat]:
    return [TalkgroupStat.model_validate(row) for row in stats_service.talkgroup_stats(db, f, limit)]


@router.get(
    "/radios",
    response_model=list[RadioStat],
    summary="Per-radio volume",
    description="The same shape as `/stats/talkgroups`, plus each radio's busiest channel.",
)
def radios(
    f: Filters, db: DbSession, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[RadioStat]:
    return [RadioStat.model_validate(row) for row in stats_service.radio_stats(db, f, limit)]


@router.get(
    "/transcription",
    response_model=TranscriptionStats,
    summary="Transcription throughput and quality",
    description=(
        "Queue depth and running jobs, the distribution of transcript statuses and quality labels, "
        "and per-backend counts with the real-time factor each one achieved. The queue figures "
        "are current; the distributions and the per-backend table are archive-wide scans and "
        "are recomputed at most every five minutes. `backends` is empty for an anonymous "
        "caller: which speech-to-text servers the operator runs is not public."
    ),
)
def transcription(db: DbSession, principal: OptionalPrincipal) -> TranscriptionStats:
    stats = stats_service.transcription_stats(db)
    # The cache holds the full measurement and the filter runs here, per request, so an
    # operator's table can never be served to a guest from it (nor a guest's empty one to
    # an operator).  "Signed in" is any credential, the same line the Stats page draws.
    if principal is None:
        stats = {**stats, "backends": []}
    return TranscriptionStats.model_validate(stats)


@router.get(
    "/gaps",
    response_model=list[RecorderGapOut],
    summary="Recorder outages",
    description=(
        "Periods longer than `GAP_MINUTES` with no traffic on a system that normally has some — "
        "the recorder-down detector. Newest first.\n\n"
        "`hours` keeps only gaps that *ended* within that window; `from`/`to` (a date or a "
        "timestamp, as everywhere else) select gaps that **overlap** the range, so an outage "
        "spanning the whole of it is returned rather than missed. `system_id` narrows to one "
        "system, and `limit`/`offset` page the result."
    ),
)
def gaps(
    db: DbSession,
    hours: Annotated[int | None, Query(ge=1, description="Only gaps that ended within this window")] = None,
    from_: Annotated[
        str | None, Query(alias="from", description="Range start (date or timestamp); gaps overlapping it")
    ] = None,
    to: Annotated[
        str | None, Query(description="Range end (date or timestamp); gaps overlapping it")
    ] = None,
    system_id: Annotated[int | None, Query(description="Only this system")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0, description="Rows to skip, newest first")] = 0,
) -> list[RecorderGapOut]:
    from app.api.filters import _as_datetime  # noqa: PLC0415 - one shared parser, kept private

    since = datetime.now(UTC) - timedelta(hours=hours) if hours else None
    return [
        RecorderGapOut.model_validate(row)
        for row in stats_service.gaps(
            db,
            since=since,
            start=_as_datetime(from_, end_of_day=False),
            end=_as_datetime(to, end_of_day=True),
            system_id=system_id,
            limit=limit,
            offset=offset,
        )
    ]


timeline_router = APIRouter(tags=["correlation"])


@timeline_router.get(
    "/timeline",
    response_model=list[TimelinePoint],
    summary="Compact per-minute activity for a window",
    description=(
        "The sparkline a consumer draws behind an incident: one point per minute (or hour, or day) "
        "with the call count and the seconds of audio. Accepts the full call filter set, so "
        "`/timeline?from=…&to=…&agency=EXAMPLEFD` is 'how busy was the fire ground'."
    ),
)
def timeline(
    f: Filters,
    db: DbSession,
    from_: Annotated[str | None, Query(alias="from", description="Start; default one hour ago")] = None,
    to: Annotated[str | None, Query(description="End; default now")] = None,
    bucket: Annotated[Literal["minute", "hour", "day"], Query()] = "minute",
) -> list[TimelinePoint]:
    from app.api.filters import _as_datetime  # noqa: PLC0415

    start = _as_datetime(from_, end_of_day=False) or (datetime.now(UTC) - timedelta(hours=1))
    end = _as_datetime(to, end_of_day=True) or datetime.now(UTC)
    scoped: CallFilters = f.replace(date_from=start, date_to=end)
    return [TimelinePoint.model_validate(p) for p in stats_service.timeline(db, scoped, bucket=bucket)]


__all__ = ["router", "timeline_router"]
