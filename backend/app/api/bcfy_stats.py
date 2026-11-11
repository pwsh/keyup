"""Broadcastify feed statistics: the status card, the charts, the hot hours, the archives.

Every route here is **admin-only**, which is stricter than the rest of the archive.  Reads
of Keyup are open by default because a recording of a public radio system is public; a
feed's listener numbers are the operator's own account data, and the same endpoints are the
place a misconfigured credential would surface, so the whole router asks for the ``admin``
scope rather than only its one mutating route.

The response models live in this module rather than in :mod:`app.schemas` because they are
one feature's shapes and nothing outside it consumes them; the generated OpenAPI document
carries them either way.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import runtime_settings
from app.api.uploads import MAX_BCFY_IMPORT_BYTES, TOO_LARGE_DETAIL
from app.auth import AdminPrincipal
from app.bcfy_stats import importer as bcfy_importer
from app.bcfy_stats import poller as bcfy_poller
from app.bcfy_stats import store
from app.bcfy_stats import summary as bcfy_summary
from app.bcfy_stats.client import BcfyOwnerError, extract
from app.bcfy_stats.importer import ImportFormatError
from app.config import get_settings
from app.db import get_session
from app.models import BcfyArchive

log = logging.getLogger("app.api.bcfy_stats")

router = APIRouter(prefix="/admin/bcfy-stats", tags=["bcfy stats"])
DbSession = Annotated[Session, Depends(get_session)]

#: The longest window the summary will compute.  Two years is longer than the rollup can
#: possibly hold on a new deployment and short enough that the two queries stay small.
MAX_DAYS = 730


# --------------------------------------------------------------------------------------
# response models
# --------------------------------------------------------------------------------------


class HotHour(BaseModel):
    """One cell of the hour-of-week grid, in a ranking."""

    weekday: int = Field(description="0 = Monday … 6 = Sunday, on the **local** clock")
    hour: int = Field(description="0–23, local clock")
    listeners_avg: float | None = Field(
        default=None,
        description=(
            "`null` when no poll has ever fallen in this cell — which is not the same fact "
            "as nobody listening, and on a long window is the usual case."
        ),
    )
    calls: int
    samples: int = Field(default=0, description="How many polls the cell's average is built from")


class QuietWindow(BaseModel):
    """A contiguous run of quiet hours — one suggested maintenance window."""

    weekday: int = Field(description="0 = Monday … 6 = Sunday, local clock")
    start_hour: int = Field(description="First hour of the window, inclusive")
    end_hour: int = Field(
        description=(
            "One past the last hour, modulo 24: a window that starts at 22 and runs three "
            "hours ends at 1. A window may wrap midnight; it never crosses into the next "
            "weekday."
        )
    )
    listeners_avg: float | None = Field(
        default=None, description="`null` when the window was chosen on call volume instead"
    )
    calls_avg: float


class DailyPoint(BaseModel):
    """One day of the dashboard's daily export: how much of it the feed was up, and its peak."""

    day: date = Field(description="The **UTC** day, as the export dates it")
    uptime_pct: float | None = Field(default=None, description="0–100, as Broadcastify computed it")
    listeners_max: int | None = Field(default=None, description="That day's highest listener count")


class FeedSummary(BaseModel):
    """``GET /admin/bcfy-stats/summary`` — the whole Feed stats page in one object."""

    feed_id: str
    days: int
    from_: datetime = Field(alias="from")
    to: datetime
    timezone: str = Field(description="The clock both matrices are cut on")
    current_listeners: int | None = None
    online: bool | None = None
    last_poll_at: datetime | None = None
    peak_listeners: int | None = None
    peak_at: datetime | None = None
    peak_source: Literal["samples", "hourly", "daily"] | None = Field(
        default=None,
        description=(
            "Which history the peak was read from, and therefore how precise `peak_at` is. "
            "`samples` is one poll and names the minute; `hourly` is an hour row (imported "
            "or page-derived included) and names the hour; `daily` is a day of the dashboard "
            "export and names only the day, reported at that day's UTC midnight. Equal counts "
            "prefer the sharper source."
        ),
    )
    hours_sampled: int = 0
    listeners_hours_covered: int = Field(
        default=0,
        description=(
            "How many hours of the window carry a listener figure — polled hours plus any "
            "reconstructed from the public feed page. The owner API returns one instant's "
            "count and no history, so this begins at this deployment's first poll and, "
            "before that, reaches back at most the 24 hours the public page's sparkline "
            "shows. Compare it with `window_hours` before reading a long window's grid."
        ),
    )
    hours_from_page: int = Field(
        default=0,
        description=(
            "How many of those hours came from the **public feed page**'s 24-hour sparkline "
            "rather than from this deployment's own polls. Those hours are real listener "
            "figures and are in every grid, but they are Broadcastify's rounded hourly "
            "numbers for hours nobody here was watching — a poll always replaces one."
        ),
    )
    hours_from_import: int = Field(
        default=0,
        description=(
            "How many of those hours came from the **feed dashboard's hourly CSV export** "
            "rather than from this deployment's own polls. That export is Broadcastify's own "
            "authoritative figure for the hour and it carries about a week, so it outranks a "
            "page-derived hour and is still replaced by the first real poll that lands in it."
        ),
    )
    daily: list[DailyPoint] = Field(
        default_factory=list,
        description=(
            "One row per **UTC day** of the window from the dashboard's daily CSV export: "
            "the day's uptime percentage and its peak listener count. About 180 days of it "
            "exist where the hourly history has about seven, so this is usually the only "
            "thing in the summary that covers a long window. Empty until an import; days the "
            "export never covered are absent rather than zero."
        ),
    )
    daily_days_covered: int = Field(
        default=0, description="How many days of the window `daily` actually carries"
    )
    first_sample_at: datetime | None = Field(
        default=None, description="The first hour ever polled: where the listener history starts"
    )
    history_from: date | None = Field(
        default=None,
        description=(
            "The earliest day this feed has **any** listener figure for — hourly or daily. "
            "After an import of the daily export this is months before `first_sample_at`, "
            "which is why they are two fields: one is where the history starts, the other is "
            "where the *hourly* history starts."
        ),
    )
    calls_hours_covered: int = Field(
        default=0, description="How many hours of the window contain calls this feed carries"
    )
    window_hours: int = Field(default=0, description="Hours between `from` and `to`")
    listeners_hour_of_week: list[list[float | None]] = Field(
        default_factory=list,
        description=(
            "7x24, Monday first, average listeners per cell. A cell is `null` — not `0` — "
            "when no poll fell in it, so an unmeasured hour never reads as an empty one."
        ),
    )
    listeners_max_hour_of_week: list[list[int | None]] = Field(
        default_factory=list,
        description="The same grid's highest single reading per cell; `null` where unmeasured",
    )
    listeners_samples_hour_of_week: list[list[int]] = Field(
        default_factory=list,
        description="How many polls each cell of the listener grid is built from",
    )
    calls_hour_of_week: list[list[float]] = Field(
        default_factory=list,
        description=(
            "The same grid for local call volume — the talkgroups the `export_broadcastify` "
            "selection covers, so it is the traffic this feed actually carries."
        ),
    )
    hottest_hours: list[HotHour] = Field(default_factory=list)
    quietest_hours: list[HotHour] = Field(
        default_factory=list,
        description=(
            "The ten *least* busy cells — the maintenance question. Ranked by average "
            "listeners among cells that have listener samples, tie-broken by call volume."
        ),
    )
    quietest_basis: Literal["listeners", "calls"] = Field(
        default="listeners",
        description=(
            "What `quietest_hours` and `quiet_windows` were computed from. `calls` means no "
            "listener sample exists yet and the ranking fell back to radio traffic, which is "
            "a weaker claim: it says the radio is quiet, not that nobody is listening."
        ),
    )
    quiet_windows: list[QuietWindow] = Field(
        default_factory=list,
        description=(
            "The three quietest non-overlapping runs of at least two hours — long enough to "
            "restart a feed in, which the single quietest hour is not."
        ),
    )
    calls_total: int = 0
    correlation: float | None = Field(
        default=None,
        description=(
            "Pearson's r between hourly call volume and hourly average listeners. `null` "
            "when fewer than 24 paired hours exist or either series never varies — an r "
            "over three hours would be a number with no information in it."
        ),
    )
    correlation_hours: int = 0

    model_config = {"populate_by_name": True}


class SamplePoint(BaseModel):
    bucket: datetime
    listeners_avg: float | None = None
    listeners_max: int | None = None
    samples: int = 0


class ArchiveOut(BaseModel):
    day: date
    start_at: datetime
    end_at: datetime | None = None
    url: str | None = None
    size_bytes: int | None = None


class ProbeResult(BaseModel):
    """What one live ``a=feed`` request returned, raw and as the mapping reads it."""

    ok: bool
    feed_id: str
    error: str | None = None
    raw: Any | None = Field(
        default=None, description="The response exactly as it arrived — this is the point"
    )
    extracted: dict[str, Any] | None = Field(
        default=None, description="What `FIELD_ALIASES` could find in it"
    )
    unmapped_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Top-level keys of the response the mapping did not claim. This is the list to "
            "read when a value is missing: the name it is really called is in here."
        ),
    )


class ImportReport(BaseModel):
    """What one dashboard CSV import parsed, wrote and refused."""

    format: Literal["hourly", "daily"] = Field(
        description="Which of the two exports this was, decided by its header"
    )
    rows_parsed: int = Field(description="Rows the parser could read")
    rows_written: int = Field(
        description=(
            "Rows that actually reached the database. Lower than `rows_parsed` exactly to the "
            "extent this deployment had already **polled** those hours: an import never "
            "overwrites a poll. (The daily export has nothing to compete with, so the two "
            "numbers match there.)"
        )
    )
    rows_skipped: int = Field(default=0, description="Rows the parser could not read")
    first: datetime | date | None = Field(default=None, description="Oldest row in the file")
    last: datetime | date | None = Field(default=None, description="Newest row in the file")
    skipped_reasons: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Why rows were skipped, counted per reason rather than listed per row — a file "
            "that is entirely the wrong thing says so in one line."
        ),
    )


class PublicPageResult(BaseModel):
    """What one fetch of the **public** feed page said, and what it wrote."""

    ok: bool
    feed_id: str
    error: str | None = None
    bars: list[int] = Field(
        default_factory=list,
        description=(
            "The page's hourly listener sparkline, **oldest first** — 24 bars as the page "
            "renders it today, though the count is reported rather than assumed. Empty means "
            "the page loaded and carried no sparkline this parser recognized, which is what "
            "a redesign on Broadcastify's side looks like."
        ),
    )
    hours_written: int = Field(
        default=0,
        description=(
            "How many hourly rows the bars actually wrote. Lower than `len(bars)` whenever "
            "this deployment already polled some of those hours: a page value never "
            "overwrites a polled one."
        ),
    )
    online: bool | None = Field(default=None, description="The page's status badge")
    since: datetime | None = Field(
        default=None, description="When that status began, per the page's `data-since`"
    )
    listeners: int | None = Field(default=None, description="The page's current listener count")


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _settings(db: Session) -> Any:
    return runtime_settings.effective_settings(db, base=get_settings())


def _feed_id(settings: Any) -> str:
    feed_id = str(getattr(settings, "bcfy_feed_id", "") or "").strip()
    if not feed_id:
        raise HTTPException(
            status_code=409,
            detail="No feed is configured. Set bcfy_feed_id (Admin → Settings → Feed stats).",
        )
    return feed_id


def _as_datetime(value: str | None, *, default: datetime) -> datetime:
    if not value:
        return default
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"not a date or timestamp: {value}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


@router.get(
    "/summary",
    response_model=FeedSummary,
    summary="Feed statistics and hot times (admin)",
    description=(
        "Current listeners, the window's peak and when it happened, a 7x24 average-listeners "
        "matrix by hour of the **local** week, the same matrix for local call volume on the "
        "talkgroups this feed carries, the ten hottest and ten quietest cells, three suggested "
        "maintenance windows, and Pearson's r between the two series over hourly buckets. "
        "Everything is read from the hourly rollup and one aggregate over calls, so a year "
        "costs two index range scans and a week costs the same two.\n\n"
        "The two halves cover different histories: calls go back as far as this archive does, "
        "listeners only to the first poll (`first_sample_at`), because the owner API carries "
        "no listener history to backfill. Unmeasured listener cells are `null`, never `0`."
    ),
)
def get_summary(
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001 - the dependency *is* the authorization
    days: Annotated[int, Query(ge=1, le=MAX_DAYS, description="Window, in days")] = 30,
) -> FeedSummary:
    settings = _settings(db)
    feed_id = _feed_id(settings)
    return FeedSummary.model_validate(
        bcfy_summary.summary(db, feed_id=feed_id, settings=settings, days=days)
    )


@router.get(
    "/samples",
    response_model=list[SamplePoint],
    summary="Listeners over time (admin)",
    description=(
        "One point per hour or per day over `from`..`to` (default: the last 7 days), each with "
        "the average and peak listener count and how many polls it is built from. A day point "
        "is the sample-weighted mean of its hours, not the mean of 24 means."
    ),
)
def get_samples(
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001
    from_: Annotated[
        str | None, Query(alias="from", description="Start (date or timestamp); default 7 days ago")
    ] = None,
    to: Annotated[str | None, Query(description="End; default now")] = None,
    step: Annotated[Literal["hour", "day"], Query(description="Bucket width")] = "hour",
) -> list[SamplePoint]:
    settings = _settings(db)
    feed_id = _feed_id(settings)
    now = datetime.now(UTC)
    start = _as_datetime(from_, default=now - timedelta(days=7))
    end = _as_datetime(to, default=now)
    if end <= start:
        raise HTTPException(status_code=422, detail="`to` must be after `from`")
    return [
        SamplePoint.model_validate(point)
        for point in bcfy_summary.series(db, feed_id=feed_id, start=start, end=end, step=step)
    ]


@router.get(
    "/archives",
    response_model=list[ArchiveOut],
    summary="Feed archive listings (admin)",
    description=(
        "The archived blocks the sync has stored, newest first — one day's when `day` is given, "
        "otherwise the most recent listings across every day. Download links come from "
        "Broadcastify and are time-limited: a link that 404s means the listing is stale, and the "
        "next sync replaces it."
    ),
)
def get_archives(
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001
    day: Annotated[date | None, Query(description="Archive day (YYYY-MM-DD)")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> list[ArchiveOut]:
    settings = _settings(db)
    feed_id = _feed_id(settings)
    query = (
        select(BcfyArchive)
        .where(BcfyArchive.feed_id == feed_id)
        .order_by(BcfyArchive.start_at.desc())
        .limit(limit)
    )
    if day is not None:
        query = query.where(BcfyArchive.day == day)
    return [ArchiveOut.model_validate(row, from_attributes=True) for row in db.scalars(query)]


@router.post(
    "/probe",
    response_model=ProbeResult,
    summary="Fetch the feed response once and show it (admin)",
    description=(
        "Makes one live `a=feed` request with the stored credentials and returns the response "
        "**verbatim**, beside what the field mapping made of it and the top-level keys it did "
        "not recognize. The owner API's response fields are undocumented, so this is how the "
        "guesses in `app.bcfy_stats.client.FIELD_ALIASES` get replaced with observations. "
        "Nothing is stored and the password never appears in the result."
    ),
)
def probe(
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001
) -> ProbeResult:
    settings = _settings(db)
    feed_id = _feed_id(settings)
    client = bcfy_poller.make_client(settings)
    if client is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The feed credentials are incomplete. Set bcfy_owner_username, "
                "bcfy_owner_password and bcfy_feed_id."
            ),
        )
    try:
        document = client.feed()
    except BcfyOwnerError as exc:
        return ProbeResult(ok=False, feed_id=feed_id, error=str(exc))
    extracted = extract(document)
    return ProbeResult(
        ok=True,
        feed_id=feed_id,
        raw=document,
        extracted=extracted or None,
        unmapped_fields=unmapped(document, extracted),
    )


def unmapped(document: Any, extracted: dict[str, Any]) -> list[str]:
    """Top-level response keys no alias claimed — the list to read when a value is missing."""
    from app.bcfy_stats.client import FIELD_ALIASES, feed_payload  # noqa: PLC0415

    payload = feed_payload(document)
    if not isinstance(payload, dict):
        return []
    claimed = {
        name.lower().replace("_", "").replace("-", "")
        for concept in extracted
        for name in FIELD_ALIASES.get(concept, ())
    }
    return sorted(
        key for key in payload if key.lower().replace("_", "").replace("-", "") not in claimed
    )


@router.post(
    "/sync-archives",
    summary="Sync the archive listings now (admin)",
    description=(
        "Runs the archive sync immediately, ignoring both its enable switch and its interval, "
        "and returns how many blocks were written, how much of the backlog it caught up on and "
        "which days failed. The scheduled sync is unaffected.\n\n"
        "Every run refreshes the last week (download links expire) and then backfills days the "
        "API lists but this database has never stored, oldest first, up to "
        "`bcfy_archives_backfill_days_per_sync`. `full=true` raises that budget to a year for "
        "this one run — enough to catch up a feed's whole history in a pass or two, and still "
        "bounded so the request cannot hang on somebody else's API for a quarter of an hour."
    ),
)
def sync_now(
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001
    full: Annotated[
        bool, Query(description="Catch the whole archive backlog up, not just this pass's budget")
    ] = False,
) -> dict[str, Any]:
    settings = _settings(db)
    _feed_id(settings)
    report = bcfy_poller.sync_archives(db, settings, force=True, full=full)
    db.commit()
    return report


@router.post(
    "/poll",
    response_model=ProbeResult,
    summary="Poll once and store the sample (admin)",
    description=(
        "Unlike `probe`, this one **stores** what it gets: a sample row and its hourly rollup, "
        "exactly as the scheduled poller would. Useful to seed a chart without waiting for the "
        "switch, and to confirm the write path end to end."
    ),
)
def poll_now(db: DbSession, principal: AdminPrincipal) -> ProbeResult:  # noqa: ARG001
    settings = _settings(db)
    feed_id = _feed_id(settings)
    client = bcfy_poller.make_client(settings)
    if client is None:
        raise HTTPException(status_code=409, detail="The feed credentials are incomplete.")
    try:
        document = client.feed()
    except BcfyOwnerError as exc:
        return ProbeResult(ok=False, feed_id=feed_id, error=str(exc))
    store.record_sample(db, feed_id=feed_id, document=document)
    db.commit()
    extracted = extract(document)
    return ProbeResult(
        ok=True,
        feed_id=feed_id,
        raw=document,
        extracted=extracted or None,
        unmapped_fields=unmapped(document, extracted),
    )


@router.post(
    "/poll-page",
    response_model=PublicPageResult,
    summary="Backfill from the public feed page (admin)",
    description=(
        "Fetches the feed's **public** listen page once — the login-free one any visitor "
        "gets — and backfills the hours nobody polled from the 24-hour listener sparkline it "
        "server-renders. This is the only listener *history* Broadcastify publishes anywhere: "
        "the owner API answers with one instant's count and nothing else, so without this a "
        "deployment's listener grid begins at its first poll.\n\n"
        "The merge rule is one way round: a page value fills an hour that has no poll, and "
        "refreshes an hour a previous page fetch filled, but it **never** overwrites an hour "
        "this deployment polled — those are its own measurements. `hours_written` is therefore "
        "usually smaller than `len(bars)` once the poller has been running.\n\n"
        "No credentials are used or needed, only `bcfy_feed_id`. This route ignores both the "
        "poller switch and the fetch interval; the scheduled fetch is unaffected. The page is "
        "undocumented HTML and may change without notice, in which case `bars` comes back "
        "empty and nothing is written."
    ),
)
def poll_page(db: DbSession, principal: AdminPrincipal) -> PublicPageResult:  # noqa: ARG001
    settings = _settings(db)
    feed_id = _feed_id(settings)
    report = bcfy_poller.sync_public_page(db, settings, force=True)
    db.commit()
    error = report.get("error")
    return PublicPageResult(
        ok=not error,
        feed_id=str(report.get("feed_id") or feed_id),
        error=error,
        bars=list(report.get("bars") or []),
        hours_written=int(report.get("hours_written") or 0),
        online=report.get("online"),
        since=report.get("since"),
        listeners=report.get("listeners"),
    )


@router.post(
    "/import",
    response_model=ImportReport,
    summary="Import a feed dashboard CSV export (admin)",
    description=(
        "Reads one of the two CSVs the feed-owner dashboard offers — Broadcastify → Manage → "
        "your feed → Statistics → export **hourly listeners** or **uptime** — and stores it. "
        "This is the only listener backfill that reaches further than a day: the owner API "
        "carries no history at all and the public page's sparkline carries 24 hours, while "
        "the hourly export carries about a week and the daily one about 180 days.\n\n"
        "The format is decided by the file's header, not by a parameter. "
        "`Hour,Online Checks,Online Reports,Max Listeners` becomes hourly rows beside the "
        "polled ones; `Day,Uptime %,Max Listeners` becomes rows of a table nothing else "
        "writes. Its timestamps are **UTC** (docs/BCFY_STATS.md records how that was "
        "established).\n\n"
        "Precedence is polled > import > page: an imported hour replaces one reconstructed "
        "from the public page, refreshes an earlier import, and never touches an hour this "
        "deployment polled — a later poll replaces it in turn. Re-importing the same file is "
        "therefore safe and idempotent.\n\n"
        "Send it as multipart `file=@hourly_listeners.csv` or as a JSON body "
        '`{"content": "…"}`. At most 2 MB. Rows that cannot be read are counted with a '
        "reason and skipped rather than failing the import."
    ),
)
async def import_export(
    request: Request,
    db: DbSession,
    principal: AdminPrincipal,  # noqa: ARG001 - the dependency *is* the authorization
    file: UploadFile | None = None,
) -> ImportReport:
    settings = _settings(db)
    feed_id = _feed_id(settings)
    text = await _import_text(request, file)
    try:
        report = bcfy_importer.import_csv(db, feed_id, text)
    except ImportFormatError as exc:
        # 422 and not 400: the request was well formed and the file is simply not one of
        # the two exports, which is a thing the operator can see and fix from the message.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return ImportReport.model_validate(report)


async def _import_text(request: Request, file: UploadFile | None) -> str:
    """The uploaded CSV as text, from either a multipart part or a JSON ``content`` field.

    Both ways in, for the same reason the playlist importers accept both: the SPA already
    holds the file's text and posting it avoids a second read as a `Blob`, while `curl -F`
    and anything scripted send the file itself.

    The size cap is applied here **again**, to the bytes, even though the body-size
    middleware has already checked `Content-Length` — the header is a claim, and this is
    the measurement.

    Decoding is UTF-8 with the BOM stripped, falling back to latin-1, which cannot fail: a
    CSV that went through a spreadsheet on Windows may carry a stray byte in a column this
    parser does not read, and refusing 180 days of history over it would be the wrong trade.
    """
    if file is not None:
        raw = await file.read(MAX_BCFY_IMPORT_BYTES + 1)
        if len(raw) > MAX_BCFY_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - a body that is neither a file nor JSON
        raise HTTPException(
            status_code=422, detail="send the CSV as a file, or as JSON with a 'content' field"
        ) from exc
    content = (body or {}).get("content") if isinstance(body, dict) else None
    if not content:
        raise HTTPException(
            status_code=422, detail="send the CSV as a file, or as JSON with a 'content' field"
        )
    if len(content) > MAX_BCFY_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
    return str(content)
