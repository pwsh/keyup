"""The directory: talkgroups, radios, systems, sites, patches, conversations, agencies (§4.2).

This is the "who is who" of the archive.  Everything here is small and slow-moving, so the
list endpoints default to a page big enough to hold the whole directory — the SPA's filter
rail wants every talkgroup at once — and the counters (``call_count``, ``last_seen_at``)
are the ones ingest maintains rather than aggregates computed per request.
"""

from __future__ import annotations

import csv
import io
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import String, cast, delete, false, func, or_, select
from sqlalchemy.orm import Session

from app import activity
from app.api.radio_events import event_rows
from app.api.uploads import read_import_payload
from app.auth import AdminPrincipal, OptionalPrincipal, WritePrincipal
from app.db import get_session
from app.ingest.playlist_import import import_file
from app.models import (
    RR_SERVICE_TAGS,
    Agency,
    AudioProfile,
    Call,
    Conversation,
    Patch,
    Radio,
    RadioAffiliation,
    RadioEvent,
    Site,
    SiteFrequency,
    System,
    Talkgroup,
)
from app.refile import site_blockers
from app.schemas import (
    AgencyOut,
    ConversationOut,
    ConversationThread,
    HourCount,
    ImportResult,
    LabelCount,
    Page,
    PatchOut,
    RadioAffiliationOut,
    RadioBulkPatch,
    RadioBulkResult,
    RadioDetail,
    RadioHistoryOut,
    RadioOut,
    RadioPatch,
    RadioPresence,
    RadioPresenceAffiliation,
    SiteFrequencyOut,
    SiteFrequencyPatch,
    SiteMergeResult,
    SiteOut,
    SitePatch,
    SiteProfileOut,
    SystemMergeResult,
    SystemOut,
    SystemPatch,
    TalkgroupBulkPatch,
    TalkgroupBulkResult,
    TalkgroupDetail,
    TalkgroupIn,
    TalkgroupOut,
    TalkgroupPatch,
    TalkgroupStatsOut,
    TopRadio,
    VocabularyOut,
)
from app.services import bulk, site_profile
from app.services import calls as call_service
from app.services import roster as roster_service
from app.services import sites as site_service
from app.services import systems as system_service
from app.services.calls import like_contains
from app.vocabulary import vocabulary

router = APIRouter(tags=["directory"])
DbSession = Annotated[Session, Depends(get_session)]

TALKGROUP_CSV_HEADER = [
    "tgid",
    "alias",
    "group_name",
    "category",
    "service_tag",
    "agency_code",
    "shared",
    "color",
    "priority",
    "record",
    "transcribe",
    "transcribe_min_seconds",
    "retention_days",
    "export_broadcastify",
    "export_bcfy_calls",
    "export_openmhz",
    "is_hidden",
    "call_count",
    "first_seen_at",
    "last_seen_at",
    "notes",
]


# --------------------------------------------------------------------------------------
# talkgroups
# --------------------------------------------------------------------------------------


@router.get(
    "/talkgroups",
    response_model=Page[TalkgroupOut],
    summary="List talkgroups",
    description=(
        "The channel directory. `q` matches the alias, group, description or the tgid itself. "
        "The default page holds the whole directory (500) because the Calls filter rail needs it "
        "in one request.\n\n"
        "Every filter is ANDed with the rest, and the policy and export filters are "
        "three-valued: absent means "
        "'either', `true` and `false` each keep only the channels in that state. Because the "
        "page is a page, filtering here rather than in the client is the difference between "
        "'every channel that is not transcribed' and 'the ones on this screen that are not'."
    ),
)
def list_talkgroups(  # noqa: PLR0913 - one filter per column, by design
    db: DbSession,
    response: Response,
    principal: OptionalPrincipal,
    q: Annotated[str | None, Query(description="Alias, group, description or tgid")] = None,
    system: Annotated[int | None, Query(description="System id")] = None,
    system_id: Annotated[
        int | None,
        Query(
            description=(
                "The same thing as `system`, under the name the rest of the API spells a "
                "system reference with. Give one or the other."
            )
        ),
    ] = None,
    category: Annotated[list[str] | None, Query(description="Category; repeat for OR")] = None,
    service_tag: Annotated[
        list[str] | None,
        Query(description="RadioReference service tag, RR's own spelling; repeat for OR"),
    ] = None,
    agency: Annotated[list[str] | None, Query(description="Agency code; repeat for OR")] = None,
    include_shared: Annotated[
        bool,
        Query(
            description=(
                "Let `agency` also match channels flagged `shared` — the county dispatch and "
                "interop channels no single department owns. Default false. No effect without "
                "`agency`."
            )
        ),
    ] = False,
    include_hidden: Annotated[bool, Query(description="Include talkgroups marked hidden")] = True,
    shared: Annotated[
        bool | None,
        Query(description="Only the shared county channels (`true`), or only the owned ones"),
    ] = None,
    record: Annotated[bool | None, Query(description="Only channels whose audio is kept, or not")] = None,
    transcribe: Annotated[
        bool | None, Query(description="Only channels that are transcribed, or not")
    ] = None,
    export_broadcastify: Annotated[
        bool | None, Query(description="Only channels ticked for the Broadcastify Feed, or not")
    ] = None,
    export_bcfy_calls: Annotated[
        bool | None, Query(description="Only channels ticked for Broadcastify Calls, or not")
    ] = None,
    export_openmhz: Annotated[
        bool | None, Query(description="Only channels ticked for OpenMHz, or not")
    ] = None,
    sort: Annotated[
        str,
        Query(
            description=(
                "Any column the directory table has a header for, `-` for descending: "
                "`tgid`, `alias`, `description`, `category`, `service_tag`, `agency_code`, "
                "`priority`, `calls_24h`, `call_count`, `first_seen_at`, `last_seen_at`, "
                "`record`, `transcribe`, `is_hidden`, `shared`, `export_broadcastify`, "
                "`export_bcfy_calls`, `export_openmhz`. Empty values sort last either way."
            )
        ),
    ] = "-call_count",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 500,
) -> Page[TalkgroupOut]:
    stmt = select(Talkgroup)
    count_stmt = select(func.count()).select_from(Talkgroup)
    conds = []
    if q:
        # `%` and `_` are ILIKE wildcards: typed into the search box they asked for
        # every row, once per keystroke (L5).
        pattern = like_contains(q)
        conds.append(
            or_(
                Talkgroup.alias.ilike(pattern, escape="\\"),
                Talkgroup.group_name.ilike(pattern, escape="\\"),
                Talkgroup.description.ilike(pattern, escape="\\"),
                cast(Talkgroup.tgid, String).like(pattern, escape="\\"),
            )
        )
    wanted_system = system or system_id
    if wanted_system:
        conds.append(Talkgroup.system_id == wanted_system)
    if category:
        conds.append(func.lower(Talkgroup.category).in_([c.lower() for c in category]))
    if service_tag:
        # Validated, unlike `category`: RR's tags are a closed list with fussy spelling
        # ("Law Dispatch", "Fire-Talk"), so a near miss typed into a query string would
        # otherwise come back as a confident empty directory.
        known = {tag.lower(): tag for tag in RR_SERVICE_TAGS}
        unknown = [tag for tag in service_tag if tag.lower() not in known]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"service_tag: unknown tag {unknown[0]!r}; see GET /directory/vocabulary",
            )
        conds.append(func.lower(Talkgroup.service_tag).in_([t.lower() for t in service_tag]))
    if agency:
        owned = func.upper(Talkgroup.agency_code).in_([a.upper() for a in agency])
        conds.append(or_(owned, Talkgroup.shared.is_(True)) if include_shared else owned)
    if not include_hidden or principal is None:
        conds.append(Talkgroup.is_hidden.is_(False))
    # The three-valued ones. `is None` rather than a falsy test: `False` is a filter
    # ("show me what is *not* transcribed"), not the absence of one.
    for flag, wanted in (
        (Talkgroup.shared, shared),
        (Talkgroup.record, record),
        (Talkgroup.transcribe, transcribe),
        (Talkgroup.export_broadcastify, export_broadcastify),
        (Talkgroup.export_bcfy_calls, export_bcfy_calls),
        (Talkgroup.export_openmhz, export_openmhz),
    ):
        if wanted is not None:
            conds.append(flag.is_(wanted))
    if conds:
        stmt = stmt.where(*conds)
        count_stmt = count_stmt.where(*conds)

    columns = {
        "call_count": Talkgroup.call_count,
        "tgid": Talkgroup.tgid,
        "alias": Talkgroup.alias,
        "description": Talkgroup.description,
        "category": Talkgroup.category,
        "service_tag": Talkgroup.service_tag,
        "agency_code": Talkgroup.agency_code,
        "first_seen_at": Talkgroup.first_seen_at,
        "last_seen_at": Talkgroup.last_seen_at,
        "priority": Talkgroup.priority,
        "record": Talkgroup.record,
        "transcribe": Talkgroup.transcribe,
        "is_hidden": Talkgroup.is_hidden,
        "shared": Talkgroup.shared,
        "export_broadcastify": Talkgroup.export_broadcastify,
        "export_bcfy_calls": Talkgroup.export_bcfy_calls,
        "export_openmhz": Talkgroup.export_openmhz,
    }
    descending = sort.startswith("-")
    key = sort[1:] if descending else sort
    if key == "calls_24h":
        # The one sort key that is not a column: the "Last 24h" header. Joined only when
        # it is asked for, so the ordinary listing keeps costing one table scan, and
        # `coalesce` so the channels with no traffic sort as 0 rather than as unknown —
        # "no calls yesterday" is a fact about them, not a missing value.
        recent = (
            select(Call.talkgroup_id.label("talkgroup_id"), func.count().label("calls"))
            .where(
                Call.talkgroup_id.isnot(None),
                Call.duplicate_of.is_(None),
                Call.started_at >= datetime.now(UTC) - timedelta(hours=24),
            )
            .group_by(Call.talkgroup_id)
            .subquery()
        )
        stmt = stmt.outerjoin(recent, recent.c.talkgroup_id == Talkgroup.id)
        column: Any = func.coalesce(recent.c.calls, 0)
    else:
        column = columns.get(key)
        if column is None:
            raise HTTPException(status_code=422, detail=f"sort: unknown key {sort!r}")
    # The tgid is the tie-break on every sort, not decoration: a page of talkgroups
    # ordered by a boolean or a category has hundreds of ties, and without a stable
    # second key the same row can appear on page 1 and page 2 of one reading.
    stmt = stmt.order_by(
        column.desc().nullslast() if descending else column.asc().nullslast(), Talkgroup.tgid.asc()
    )

    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(stmt.limit(page_size).offset((page - 1) * page_size))
    response.headers["X-Total-Count"] = str(total)
    return Page[TalkgroupOut](
        items=[TalkgroupOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/talkgroups/export",
    response_class=PlainTextResponse,
    summary="Export the talkgroup directory as CSV",
    description="The same columns `POST /talkgroups/import` accepts, so an export can be re-imported.",
)
def export_talkgroups(db: DbSession, principal: OptionalPrincipal) -> PlainTextResponse:
    visible = select(Talkgroup) if principal else select(Talkgroup).where(Talkgroup.is_hidden.is_(False))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TALKGROUP_CSV_HEADER)
    for tg in db.scalars(visible.order_by(Talkgroup.tgid)):
        writer.writerow(
            [
                tg.tgid,
                tg.alias or "",
                tg.group_name or "",
                tg.category or "",
                tg.service_tag or "",
                tg.agency_code or "",
                tg.shared,
                tg.color or "",
                tg.priority,
                tg.record,
                tg.transcribe,
                tg.transcribe_min_seconds if tg.transcribe_min_seconds is not None else "",
                tg.retention_days if tg.retention_days is not None else "",
                tg.export_broadcastify,
                tg.export_bcfy_calls,
                tg.export_openmhz,
                tg.is_hidden,
                tg.call_count,
                tg.first_seen_at.isoformat() if tg.first_seen_at else "",
                tg.last_seen_at.isoformat() if tg.last_seen_at else "",
                (tg.notes or "").replace("\n", " "),
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="keyup-talkgroups.csv"'},
    )


def _run_import(db: Session, raw: bytes, suffix: str, system: str | None) -> ImportResult:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    try:
        summary = import_file(db, temp, system=system)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)
    return ImportResult(
        created=summary.talkgroups_created + summary.radios_created,
        updated=summary.talkgroups_updated + summary.radios_updated,
        skipped=summary.skipped_non_apco25,
        source=summary.source,
        aliases=summary.aliases,
        skipped_service_tags=summary.skipped_service_tags,
        skipped_agency_codes=summary.skipped_agency_codes,
    )


@router.post(
    "/talkgroups/import",
    response_model=ImportResult,
    summary="Import an sdrtrunk playlist or a CSV",
    description=(
        "Reads an sdrtrunk `default.xml` (`<alias name group color><id type=\"talkgroup\" value/></alias>`) "
        "or a CSV and writes aliases, groups, categories, RadioReference service tags, agency codes "
        "and colors onto `talkgroups` "
        "and `radios`. Unlike ingest, the importer **overwrites** aliases: importing a playlist is an "
        "explicit act, and the playlist is the recorder operator's own naming. Send the file as "
        "multipart `file=@default.xml` or a JSON body `{\"content\": \"…\", \"format\": \"xml\"}`."
    ),
)
async def import_talkgroups(
    request: Request,
    db: DbSession,
    principal: WritePrincipal,
    file: UploadFile | None = None,
    system: Annotated[str | None, Query(description="System name (default: the only one)")] = None,
) -> ImportResult:
    raw, suffix = await read_import_payload(request, file)
    return _run_import(db, raw, suffix, system)


@router.post(
    "/radios/import",
    response_model=ImportResult,
    summary="Import radio aliases from a playlist or CSV",
    description="The same importer as `/talkgroups/import`; radio ids in the playlist become `radios` rows.",
)
async def import_radios(
    request: Request,
    db: DbSession,
    principal: WritePrincipal,
    file: UploadFile | None = None,
    system: Annotated[str | None, Query(description="System name (default: the only one)")] = None,
) -> ImportResult:
    raw, suffix = await read_import_payload(request, file)
    return _run_import(db, raw, suffix, system)


@router.post(
    "/talkgroups",
    response_model=TalkgroupOut,
    status_code=201,
    summary="Create a talkgroup",
    description=(
        "For channels that have not been heard yet, so they can be named and policed before their "
        "first call. `system_id` defaults to the only system when there is exactly one."
    ),
)
def create_talkgroup(body: TalkgroupIn, db: DbSession, principal: WritePrincipal) -> TalkgroupOut:
    system_id = body.system_id
    if system_id is None:
        ids = list(db.scalars(select(System.id).order_by(System.id)))
        if len(ids) != 1:
            raise HTTPException(status_code=422, detail="system_id is required (several systems exist)")
        system_id = ids[0]
    existing = db.scalar(
        select(Talkgroup).where(Talkgroup.system_id == system_id, Talkgroup.tgid == body.tgid)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"talkgroup {body.tgid} already exists")
    row = Talkgroup(
        **{k: v for k, v in body.model_dump().items() if k != "system_id"}, system_id=system_id
    )
    db.add(row)
    db.flush()
    # Broadcastify Calls takes trunked systems only, so the flag is held false on a
    # conventional system rather than stored ticked and refused later.
    system_service.hold_calls_flag(db, [row])
    db.commit()
    return TalkgroupOut.model_validate(row)


@router.get("/talkgroups/{talkgroup_id}", response_model=TalkgroupDetail, summary="One talkgroup")
def get_talkgroup(talkgroup_id: int, db: DbSession, principal: OptionalPrincipal) -> TalkgroupDetail:
    row = db.get(Talkgroup, talkgroup_id)
    if row is None or (principal is None and row.is_hidden):
        raise HTTPException(status_code=404, detail=f"no talkgroup {talkgroup_id}")
    system = db.get(System, row.system_id)
    agency = db.get(Agency, row.agency_code) if row.agency_code else None
    audio = db.scalar(
        select(func.coalesce(func.sum(Call.duration_s), 0)).where(Call.talkgroup_id == talkgroup_id)
    )
    radios = db.scalar(
        select(func.count(func.distinct(Call.radio_id))).where(Call.talkgroup_id == talkgroup_id)
    )
    last_call = db.scalar(
        select(Call.id)
        .where(Call.talkgroup_id == talkgroup_id)
        .order_by(Call.started_at.desc())
        .limit(1)
    )
    profile = db.get(AudioProfile, row.audio_profile_id) if row.audio_profile_id else None
    return TalkgroupDetail(
        **TalkgroupOut.model_validate(row).model_dump(),
        system_name=system.name if system else None,
        agency_name=agency.name if agency else None,
        audio_profile_name=profile.name if profile else None,
        audio_seconds=float(audio or 0),
        radio_count=int(radios or 0),
        last_call_id=last_call,
    )


def _check_audio_profile(db: Session, fields: dict[str, Any]) -> None:
    """Refuse an ``audio_profile_id`` that names no profile.

    The column is ``ON DELETE SET NULL`` and the FK would refuse a bad id anyway, but as an
    IntegrityError at commit — a 500 with a Postgres constraint name in it. One lookup buys
    the operator "no audio profile 7" instead, and answers it for a whole bulk edit at once
    because the id is the same id for every row in the batch.
    """
    profile_id = fields.get("audio_profile_id")
    if profile_id is not None and db.get(AudioProfile, profile_id) is None:
        raise HTTPException(status_code=422, detail=f"no audio profile {profile_id}")


@router.patch(
    "/talkgroups/{talkgroup_id}",
    response_model=TalkgroupOut,
    summary="Edit a talkgroup",
    description=(
        "Only the keys present are written. `agency_code` is what makes a channel findable by "
        "agency (`GET /calls?agency=…`); `record` is whether the audio is kept at all, and "
        "`transcribe` and `transcribe_min_seconds` are the "
        "per-channel transcription policy the ingest applies."
    ),
)
def patch_talkgroup(
    talkgroup_id: int, body: TalkgroupPatch, db: DbSession, principal: WritePrincipal
) -> TalkgroupOut:
    row = db.get(Talkgroup, talkgroup_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no talkgroup {talkgroup_id}")
    fields = body.model_dump(exclude_unset=True)
    wanted_agency = fields.get("agency_code")
    if wanted_agency and db.get(Agency, wanted_agency) is None:
        raise HTTPException(status_code=422, detail=f"unknown agency code {wanted_agency!r}")
    _check_audio_profile(db, fields)
    for key, value in fields.items():
        setattr(row, key, value)
    system_service.hold_calls_flag(db, [row])
    db.commit()
    return TalkgroupOut.model_validate(row)


@router.post(
    "/talkgroups/bulk",
    response_model=TalkgroupBulkResult,
    summary="Edit many talkgroups at once",
    description=(
        "The same body as `PATCH /talkgroups/{id}` — only the keys present in `changes` are "
        "written, and a key present with `null` clears the column — applied to every id in one "
        "transaction. Retagging a shift's worth of channels is one request and one activity line "
        "rather than two hundred of each. `alias` is accepted but rarely wanted: it would give "
        "every selected channel the same name."
    ),
)
def bulk_patch_talkgroups(
    body: TalkgroupBulkPatch, db: DbSession, principal: WritePrincipal
) -> TalkgroupBulkResult:
    fields = body.changes.model_dump(exclude_unset=True)
    bulk.require_changes(fields)
    # Validated once for the whole batch rather than per row: the code is the same code for
    # all of them, so one lookup answers it.
    if fields.get("agency_code") and db.get(Agency, fields["agency_code"]) is None:
        raise HTTPException(status_code=422, detail=f"unknown agency code {fields['agency_code']!r}")
    _check_audio_profile(db, fields)
    rows = bulk.resolve(db, Talkgroup, body.ids, noun="talkgroup")
    bulk.apply_fields(rows, fields)
    system_service.hold_calls_flag(db, rows)
    bulk.record(
        db,
        kind="talkgroups_bulk_edited",
        noun="talkgroups",
        ids=[row.id for row in rows],
        fields=fields,
        by=principal.label,
    )
    db.commit()
    return TalkgroupBulkResult(
        updated=len(rows), items=[TalkgroupOut.model_validate(row) for row in rows]
    )


@router.get(
    "/talkgroups/{talkgroup_id}/stats",
    response_model=TalkgroupStatsOut,
    summary="Activity of one talkgroup",
    description="Calls per local day, busiest hours of the day, a 24-hour sparkline and the top radios.",
)
def talkgroup_stats(
    talkgroup_id: int,
    db: DbSession,
    principal: OptionalPrincipal,
    days: Annotated[int, Query(ge=1, le=365, description="Days of history for the daily series")] = 30,
) -> TalkgroupStatsOut:
    row = db.get(Talkgroup, talkgroup_id)
    if row is None or (principal is None and row.is_hidden):
        raise HTTPException(status_code=404, detail=f"no talkgroup {talkgroup_id}")
    where = (Call.talkgroup_id == talkgroup_id, Call.duplicate_of.is_(None))
    count = db.scalar(select(func.count()).select_from(Call).where(*where)) or 0
    audio = db.scalar(select(func.coalesce(func.sum(Call.duration_s), 0)).where(*where)) or 0
    hour_expr = func.extract("hour", Call.started_local)
    hourly = {
        int(h): int(c)
        for h, c in db.execute(
            select(hour_expr, func.count()).where(*where).group_by(hour_expr).order_by(hour_expr)
        )
    }
    day_expr = func.date_trunc("day", Call.started_local)
    since = datetime.now(UTC) - timedelta(days=days)
    daily = [
        LabelCount(key=d.date().isoformat(), label=d.date().isoformat(), count=int(c))
        for d, c in db.execute(
            select(day_expr, func.count())
            .where(*where, Call.started_at >= since)
            .group_by(day_expr)
            .order_by(day_expr)
        )
    ]
    top = [
        TopRadio(radio_id=rid, label=label or str(rid), count=int(c))
        for rid, label, c in db.execute(
            select(
                Call.radio_id,
                func.coalesce(Radio.unit_id, Radio.alias, cast(Radio.radio_id, String)),
                func.count(),
            )
            .select_from(Call)
            .join(Radio, Call.radio_id == Radio.id)
            .where(*where)
            .group_by(Call.radio_id, Radio.unit_id, Radio.alias, Radio.radio_id)
            .order_by(func.count().desc())
            .limit(10)
        )
    ]
    return TalkgroupStatsOut(
        talkgroup_id=talkgroup_id,
        call_count=int(count),
        audio_seconds=float(audio),
        hourly=[HourCount(hour=h, count=hourly.get(h, 0)) for h in range(24)],
        spark=call_service.spark_24h(db, Call.talkgroup_id, talkgroup_id),
        daily=daily,
        top_radios=top,
    )


# --------------------------------------------------------------------------------------
# radios
# --------------------------------------------------------------------------------------


@router.get(
    "/radios",
    response_model=Page[RadioOut],
    summary="List radios and units",
    description="`q` matches the alias, the unit id or the radio id. Sorted by traffic by default.",
)
def list_radios(
    db: DbSession,
    response: Response,
    principal: OptionalPrincipal,
    q: Annotated[str | None, Query(description="Alias, unit id or radio id")] = None,
    system: Annotated[int | None, Query()] = None,
    agency: Annotated[list[str] | None, Query(description="Agency code; repeat for OR")] = None,
    kind: Annotated[list[str] | None, Query(description="console/portable/mobile/unknown")] = None,
    has_unit: Annotated[bool | None, Query(description="Only radios with (or without) a unit id")] = None,
    activity: Annotated[
        Literal["all", "transmitted", "seen"] | None,
        Query(
            description="`transmitted` = has keyed up (call_count > 0); `seen` = present on the "
            "control channel but silent (call_count == 0); default/`all` = both"
        ),
    ] = None,
    sort: Annotated[
        str, Query(description="`-call_count`, `radio_id`, `unit_id`, `last_seen_at`")
    ] = "-call_count",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Page[RadioOut]:
    conds = []
    if q:
        pattern = like_contains(q)
        conds.append(
            or_(
                Radio.alias.ilike(pattern, escape="\\"),
                Radio.unit_id.ilike(pattern, escape="\\"),
                cast(Radio.radio_id, String).like(pattern, escape="\\"),
            )
        )
    if system:
        conds.append(Radio.system_id == system)
    if agency:
        conds.append(func.upper(Radio.agency_code).in_([a.upper() for a in agency]))
    if kind:
        conds.append(Radio.kind.in_([k.lower() for k in kind]))
    if has_unit is not None:
        conds.append(Radio.unit_id.isnot(None) if has_unit else Radio.unit_id.is_(None))
    if activity == "transmitted":
        conds.append(Radio.call_count > 0)
    elif activity == "seen":
        conds.append(Radio.call_count == 0)

    columns = {
        "call_count": Radio.call_count,
        "radio_id": Radio.radio_id,
        "unit_id": Radio.unit_id,
        "alias": Radio.alias,
        "last_seen_at": Radio.last_seen_at,
    }
    descending = sort.startswith("-")
    column = columns.get(sort[1:] if descending else sort)
    if column is None:
        raise HTTPException(status_code=422, detail=f"sort: unknown key {sort!r}")

    total = int(db.scalar(select(func.count()).select_from(Radio).where(*conds)) or 0)
    rows = list(
        db.scalars(
            select(Radio)
            .where(*conds)
            .order_by(column.desc().nullslast() if descending else column.asc().nullslast())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    # The roster identity for the whole page in one query rather than one per row: this
    # list renders up to 500 radios and the assignment lookup is the only thing on it that
    # is not already on the ``radios`` row (§3.1).
    items = [RadioOut.model_validate(r) for r in rows]
    # The roster is closed to guests, so its link on a radio is too.
    if principal is not None:
        identities = roster_service.current_identities(db, [item.id for item in items])
        for item in items:
            item.current_unit, item.current_person = identities.get(item.id, (None, None))
    response.headers["X-Total-Count"] = str(total)
    return Page[RadioOut](items=items, total=total, page=page, page_size=page_size)


@router.get("/radios/{radio_id}", response_model=RadioDetail, summary="One radio")
def get_radio(radio_id: int, db: DbSession, principal: OptionalPrincipal) -> RadioDetail:
    row = db.get(Radio, radio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    system = db.get(System, row.system_id)
    agency = db.get(Agency, row.agency_code) if row.agency_code else None
    audio = db.scalar(select(func.coalesce(func.sum(Call.duration_s), 0)).where(Call.radio_id == radio_id))
    talkgroups = db.scalar(
        select(func.count(func.distinct(Call.talkgroup_id))).where(Call.radio_id == radio_id)
    )
    last_call = db.scalar(
        select(Call.id).where(Call.radio_id == radio_id).order_by(Call.started_at.desc()).limit(1)
    )
    # Presence (docs/RADIO_EVENTS.md) — the control channel's answer to "where is this
    # radio", which the call archive cannot give for a unit that never keys up.  The last
    # fifty events ride along so the detail page opens in one request; the paged endpoint
    # is ``GET /radios/{id}/events``.
    events = list(
        db.scalars(
            select(RadioEvent)
            .where(RadioEvent.radio_pk == radio_id)
            .order_by(RadioEvent.at.desc(), RadioEvent.id.desc())
            .limit(50)
        )
    )
    event_count = int(
        db.scalar(select(func.count()).select_from(RadioEvent).where(RadioEvent.radio_pk == radio_id))
        or 0
    )
    unit_ref, person_ref = (
        roster_service.current_identities(db, [radio_id]).get(radio_id, (None, None))
        if principal is not None
        else (None, None)
    )
    out = RadioOut.model_validate(row)
    out.current_unit, out.current_person = unit_ref, person_ref
    return RadioDetail(
        **out.model_dump(),
        system_name=system.name if system else None,
        agency_name=agency.name if agency else None,
        audio_seconds=float(audio or 0),
        talkgroup_count=int(talkgroups or 0),
        last_call_id=last_call,
        presence=RadioPresence(
            last_seen_at=row.last_seen_at,
            last_registered_at=row.last_registered_at,
            last_deregistered_at=row.last_deregistered_at,
            affiliations=_presence_affiliations(db, radio_id),
        ),
        events=event_rows(db, events),
        event_count=event_count,
    )


def _presence_affiliations(db: Session, radio_id: int) -> list[RadioPresenceAffiliation]:
    """The radio's talkgroups, newest evidence first, whichever kind of evidence it is.

    Ordered on ``greatest(last_affiliated_at, last_heard_at)`` rather than ``last_heard_at``
    because a row written by an affiliation event has no ``last_heard_at`` at all — nobody
    spoke — and would otherwise sort as if it were the oldest thing on the page.
    """
    recency = func.greatest(
        func.coalesce(RadioAffiliation.last_affiliated_at, RadioAffiliation.last_heard_at),
        func.coalesce(RadioAffiliation.last_heard_at, RadioAffiliation.last_affiliated_at),
    )
    rows = db.execute(
        select(RadioAffiliation, Talkgroup)
        .join(Talkgroup, RadioAffiliation.talkgroup_id_fk == Talkgroup.id)
        .where(RadioAffiliation.radio_id_fk == radio_id)
        .order_by(recency.desc().nullslast())
    )
    return [
        RadioPresenceAffiliation(
            talkgroup_id=tg.id,
            tgid=tg.tgid,
            talkgroup_alias=tg.alias,
            source=affiliation.source,
            last_heard_at=affiliation.last_heard_at,
            last_affiliated_at=affiliation.last_affiliated_at,
            call_count=affiliation.call_count,
        )
        for affiliation, tg in rows
    ]


@router.patch(
    "/radios/{radio_id}",
    response_model=RadioOut,
    summary="Edit a radio",
    description=(
        "`unit_id` is the field that matters: it is the apparatus id (`E1`, `A21`) that lets "
        "`GET /calls?unit=E1` find this radio's traffic from the unit a CAD system names."
    ),
)
def patch_radio(radio_id: int, body: RadioPatch, db: DbSession, principal: WritePrincipal) -> RadioOut:
    row = db.get(Radio, radio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("agency_code") and db.get(Agency, fields["agency_code"]) is None:
        raise HTTPException(status_code=422, detail=f"unknown agency code {fields['agency_code']!r}")
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    return RadioOut.model_validate(row)


@router.post(
    "/radios/bulk",
    response_model=RadioBulkResult,
    summary="Edit many radios at once",
    description=(
        "`agency_code`, `kind` and `notes` written onto every id in one transaction. `alias` and "
        "`unit_id` are refused with a 422: they name one particular radio, and writing one of "
        "them across a selection would erase the distinctions the directory exists to keep."
    ),
)
def bulk_patch_radios(body: RadioBulkPatch, db: DbSession, principal: WritePrincipal) -> RadioBulkResult:
    fields = body.changes.model_dump(exclude_unset=True)
    bulk.require_changes(fields)
    if fields.get("agency_code") and db.get(Agency, fields["agency_code"]) is None:
        raise HTTPException(status_code=422, detail=f"unknown agency code {fields['agency_code']!r}")
    rows = bulk.resolve(db, Radio, body.ids, noun="radio")
    bulk.apply_fields(rows, fields)
    bulk.record(
        db,
        kind="radios_bulk_edited",
        noun="radios",
        ids=[row.id for row in rows],
        fields=fields,
        by=principal.label,
    )
    db.commit()
    return RadioBulkResult(updated=len(rows), items=[RadioOut.model_validate(row) for row in rows])


@router.get(
    "/radios/{radio_id}/affiliations",
    response_model=list[RadioAffiliationOut],
    summary="Talkgroups this radio has been heard on",
    description="The rolling affiliation view ingest maintains — the unit's channel history.",
)
def radio_affiliations(radio_id: int, db: DbSession) -> list[RadioAffiliationOut]:
    radio = db.get(Radio, radio_id)
    if radio is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    # ``greatest`` for the reason in :func:`_presence_affiliations`: a row an affiliation
    # event wrote has no ``last_heard_at``.
    recency = func.greatest(
        func.coalesce(RadioAffiliation.last_affiliated_at, RadioAffiliation.last_heard_at),
        func.coalesce(RadioAffiliation.last_heard_at, RadioAffiliation.last_affiliated_at),
    )
    rows = db.execute(
        select(RadioAffiliation, Talkgroup)
        .join(Talkgroup, RadioAffiliation.talkgroup_id_fk == Talkgroup.id)
        .where(RadioAffiliation.radio_id_fk == radio_id)
        .order_by(recency.desc().nullslast())
    )
    return [
        RadioAffiliationOut(
            radio_id=radio_id,
            radio_alias=radio.alias,
            unit_id=radio.unit_id,
            talkgroup_id=tg.id,
            talkgroup_alias=tg.alias,
            tgid=tg.tgid,
            first_heard_at=aff.first_heard_at,
            last_heard_at=aff.last_heard_at,
            call_count=aff.call_count,
            source=aff.source,
            last_affiliated_at=aff.last_affiliated_at,
        )
        for aff, tg in rows
    ]


@router.get(
    "/radios/{radio_id}/history",
    response_model=RadioHistoryOut,
    summary="Activity of one radio",
    description="A 24-hour sparkline, the hours of the day it is heard, and which channels it uses.",
)
def radio_history(radio_id: int, db: DbSession) -> RadioHistoryOut:
    if db.get(Radio, radio_id) is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    where = (Call.radio_id == radio_id, Call.duplicate_of.is_(None))
    count = db.scalar(select(func.count()).select_from(Call).where(*where)) or 0
    hour_expr = func.extract("hour", Call.started_local)
    hourly = {
        int(h): int(c)
        for h, c in db.execute(select(hour_expr, func.count()).where(*where).group_by(hour_expr))
    }
    talkgroups = [
        LabelCount(key=str(tgid), label=alias or str(tgid), count=int(c))
        for tgid, alias, c in db.execute(
            select(Talkgroup.tgid, Talkgroup.alias, func.count())
            .select_from(Call)
            .join(Talkgroup, Call.talkgroup_id == Talkgroup.id)
            .where(*where)
            .group_by(Talkgroup.tgid, Talkgroup.alias)
            .order_by(func.count().desc())
            .limit(20)
        )
    ]
    return RadioHistoryOut(
        radio_id=radio_id,
        call_count=int(count),
        spark=call_service.spark_24h(db, Call.radio_id, radio_id),
        hourly=[HourCount(hour=h, count=hourly.get(h, 0)) for h in range(24)],
        talkgroups=talkgroups,
    )


# --------------------------------------------------------------------------------------
# systems, sites, patches, conversations, agencies
# --------------------------------------------------------------------------------------


@router.get(
    "/systems",
    response_model=list[SystemOut],
    summary="Radio systems",
    description=(
        "Every system in the archive with its counts, its RadioReference mapping and "
        "`deletable`/`blocking` — what `DELETE /systems/{id}` would answer. Every foreign key "
        "onto `systems` cascades, so the UI disables that button from this rather than "
        "discovering it."
    ),
)
def list_systems(db: DbSession) -> list[SystemOut]:
    return system_service.system_outs(db, list(db.scalars(select(System).order_by(System.name))))


@router.patch(
    "/systems/{system_id}",
    response_model=SystemOut,
    summary="Edit a system",
    description=(
        "Only the keys present are written. `name` and `description` are the two fields that "
        "are ours to choose; `wacn`, `system_id`, `nac` and `protocol` are what the control "
        "channel announced about itself and are filled from what the recorder heard. "
        "`systems.name` is unique across the archive, so a clash is a 409 naming the system "
        "that already has it."
    ),
)
def patch_system(
    system_id: int, body: SystemPatch, db: DbSession, principal: WritePrincipal
) -> SystemOut:
    row = db.get(System, system_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no system {system_id}")
    fields = body.model_dump(exclude_unset=True)
    name = fields.get("name")
    if name is not None and name != row.name:
        # Caught here rather than left to the unique index: the message names the system
        # holding the name, which is the one an operator most likely meant to merge into.
        clash = db.scalars(select(System).where(System.name == name, System.id != row.id)).first()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"a system named {name!r} already exists (system {clash.id})",
            )
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return system_service.system_out(db, row)


@router.delete(
    "/systems/{system_id}",
    status_code=204,
    summary="Delete a system (admin)",
    description=(
        "Allowed only when nothing belongs to the system. A 409 lists what does — the same "
        "`blocking` map `GET /systems` puts on the row. Every foreign key onto `systems` is "
        "`ON DELETE CASCADE`, so without this check the database would answer the delete by "
        "removing every site, talkgroup, radio and call underneath it and reporting success. "
        "To empty a system rather than lose it, merge it into another one."
    ),
)
def delete_system(system_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(System, system_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no system {system_id}")
    blocking = system_service.system_blockers(db, system_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                f"system {system_id} still owns "
                + ", ".join(f"{rows} {label}" for label, rows in sorted(blocking.items()))
                + "; merge it into another system, or move those first"
            ),
        )
    activity.record(
        db,
        source="system",
        level="warn",
        kind="system_deleted",
        message=f"Deleted system {row.name}",
        meta={"system_id": system_id, "name": row.name, "by": principal.label},
    )
    db.execute(delete(System).where(System.id == system_id))
    db.commit()
    return Response(status_code=204)


@router.post(
    "/systems/{system_id}/merge-into/{target_id}",
    response_model=SystemMergeResult,
    summary="Merge one system into another (admin)",
    description=(
        "Folds every site, talkgroup, radio, patch, call, observation and event of the source "
        "onto the target and deletes the source. A child with a twin under the target — a site "
        "of the same name, a talkgroup with the same `tgid`, a radio with the same identity — "
        "is folded into it, counters summed and blanks filled; one with no twin is re-parented "
        "as it is. **No call is ever deleted**: `calls` has no unique key over its foreign "
        "keys, so a call can only be moved. The repointing runs in committed batches under "
        "ascending advisory locks, so merging a million calls does not hold the live feed off."
    ),
)
def merge_system(
    system_id: int, target_id: int, db: DbSession, principal: AdminPrincipal
) -> SystemMergeResult:
    source = db.get(System, system_id)
    target = db.get(System, target_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no system {system_id}")
    if target is None:
        raise HTTPException(status_code=404, detail=f"no system {target_id}")
    source_label = {"id": source.id, "name": source.name}
    target_id_kept, target_name = target.id, target.name
    try:
        result = system_service.merge_systems(db, source, target)
    except system_service.MergeRefused as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # The merge commits as it goes, so re-read the survivor rather than trust an ORM
    # identity from a session that has been through several transactions since.
    surviving = db.get(System, target_id_kept)
    if surviving is None:  # pragma: no cover - it was just merged into; it exists
        raise HTTPException(status_code=500, detail="the merge target disappeared")
    activity.record(
        db,
        source="system",
        level="warn",
        kind="system_merged",
        message=f"Merged system {source_label['name']} into {target_name}",
        meta={
            "source": source_label,
            "target_id": target_id_kept,
            "rows_repointed": result.get("rows_repointed", {}),
            "by": principal.label,
        },
    )
    db.commit()
    return SystemMergeResult(
        source=source_label,
        target=system_service.system_out(db, surviving),
        reparented=result.get("reparented", {}),
        merged=result.get("merged", {}),
        rows_repointed=result.get("rows_repointed", {}),
        duplicates_dropped=result.get("duplicates_dropped", {}),
        deleted=result.get("deleted", {}),
    )


@router.get(
    "/sites",
    response_model=list[SiteOut],
    summary="Tower sites",
    description=(
        "Every row carries `system_name` as well as `name`, because a site name is unique only "
        "within its system — two systems can both have an `Eastside` — and `deletable`/`blocking`, "
        "which is what `DELETE /sites/{id}` would answer."
    ),
)
def list_sites(
    db: DbSession, response: Response, system: Annotated[int | None, Query()] = None
) -> list[SiteOut]:
    stmt = select(Site).join(System, Site.system_id == System.id).order_by(System.name, Site.name)
    if system:
        stmt = stmt.where(Site.system_id == system)
    rows = site_service.site_outs(db, list(db.scalars(stmt)))
    response.headers["X-Total-Count"] = str(len(rows))
    return rows


@router.get(
    "/sites/{site_id}",
    response_model=SiteOut,
    summary="One tower site",
    description="The same row `GET /sites` returns, including `deletable` and what is blocking it.",
)
def get_site(site_id: int, db: DbSession) -> SiteOut:
    row = db.get(Site, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    return site_service.site_out(db, row)


@router.patch(
    "/sites/{site_id}",
    response_model=SiteOut,
    summary="Edit a site",
    description=(
        "Only the keys present are written. `name`, `notes` and `rr_site_id` are the three fields "
        "that are ours to choose; `site_number`, `nac`, `rfss` and `lra` are what the control "
        "channel announced about itself and are filled from observations, not by hand."
    ),
)
def patch_site(site_id: int, body: SitePatch, db: DbSession, principal: WritePrincipal) -> SiteOut:
    row = db.get(Site, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    fields = body.model_dump(exclude_unset=True)
    name = fields.get("name")
    if name is not None and name != row.name:
        # `sites` is unique on (system_id, name); catching it here makes the message say
        # which system already has the name rather than leaking a constraint violation.
        clash = db.scalars(
            select(Site).where(Site.system_id == row.system_id, Site.name == name, Site.id != row.id)
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"this system already has a site named {name!r} (site {clash.id})",
            )
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return site_service.site_out(db, row)


@router.get(
    "/sites/{site_id}/profile",
    response_model=SiteProfileOut,
    summary="Everything known about one tower site",
    description=(
        "The whole site in one document: the identity it keys (WACN, P25 system id, NAC, "
        "RFSS, site number, LRA), its control channel and the backups it announces, the "
        "SYS_SVC_BCAST services it offers and merely supports, the IDEN band plan, the "
        "traffic channels it granted, the neighbours it names — resolved to a Keyup site "
        "where one matches — its packet-data activity and how many of its message kinds "
        "have ever been named.\n\n"
        "Folded out of the **latest stored observation window per recorder instance**, "
        "newest window winning field by field, so a second box posting a thinner document "
        "does not blank what the first one saw. `unknowns` lists, in plain sentences, what "
        "about this site is *not* identified — which is as much the point of the document "
        "as the rest of it (docs/UNKNOWN-DATA-PLAN.md)."
    ),
)
def get_site_profile(site_id: int, db: DbSession) -> SiteProfileOut:
    row = db.get(Site, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    return site_profile.build(db, row)


@router.get(
    "/sites/{site_id}/frequencies",
    response_model=list[SiteFrequencyOut],
    summary="Frequencies catalogued at one site",
    description=(
        "Every `(frequency, source)` row this site has — what the air announced, what "
        "RadioReference documents, what the recorder is configured to hunt — with the two "
        "export checkboxes that decide whether *conventional* calls on that frequency (calls "
        "with no talkgroup) may be published. Ordered by frequency, then source."
    ),
)
def list_site_frequencies(site_id: int, db: DbSession) -> list[SiteFrequencyOut]:
    if db.get(Site, site_id) is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    rows = db.scalars(
        select(SiteFrequency)
        .where(SiteFrequency.site_id == site_id)
        .order_by(SiteFrequency.frequency_hz, SiteFrequency.source)
    )
    return [SiteFrequencyOut.model_validate(row) for row in rows]


@router.patch(
    "/site-frequencies/{frequency_id}",
    response_model=SiteFrequencyOut,
    summary="Edit a site frequency's export flags",
    description=(
        "Only `export_broadcastify` and `export_openmhz` are editable. The rest of the row "
        "is observed — the frequency, its role and its counters come from the air, from "
        "RadioReference or from the recorder's own configuration — and editing those by hand "
        "would only make the channel audit disagree with reality."
    ),
)
def patch_site_frequency(
    frequency_id: int, body: SiteFrequencyPatch, db: DbSession, principal: WritePrincipal
) -> SiteFrequencyOut:
    row = db.get(SiteFrequency, frequency_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site frequency {frequency_id}")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return SiteFrequencyOut.model_validate(row)


@router.delete(
    "/sites/{site_id}",
    status_code=204,
    summary="Delete a site (admin)",
    description=(
        "Allowed only when nothing references the site. A 409 lists what does — the same "
        "`blocking` map `GET /sites` puts on the row, so the UI can disable the button rather "
        "than let the operator find out here."
    ),
)
def delete_site(site_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(Site, site_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    blocking = site_blockers(db, site_id)
    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                f"site {site_id} still has "
                + ", ".join(f"{rows} {label}" for label, rows in sorted(blocking.items()))
                + "; move or delete those first"
            ),
        )
    system_name = db.scalar(select(System.name).where(System.id == row.system_id))
    label = f"{system_name} / {row.name}"
    activity.record(
        db,
        source="system",
        level="warn",
        kind="site_deleted",
        message=f"Deleted site {label}",
        meta={"site_id": site_id, "system_id": row.system_id, "by": principal.label},
    )
    db.execute(delete(Site).where(Site.id == site_id))
    db.commit()
    site_service.forget_call_counts()
    return Response(status_code=204)


@router.post(
    "/sites/{site_id}/merge-into/{target_id}",
    response_model=SiteMergeResult,
    summary="Merge one site into another (admin)",
    description=(
        "Repoints every call, frequency, neighbor, observation and radio event from the source "
        "onto the target, fills the target's blank fields from the source, and deletes the "
        "source. Both sites must be on the same system: moving calls between systems is a "
        "re-filing (`keyup refile-calls`), because their talkgroups and radios have to be split "
        "off onto twins first, and a 409 says so."
    ),
)
def merge_site(
    site_id: int, target_id: int, db: DbSession, principal: AdminPrincipal
) -> SiteMergeResult:
    source = db.get(Site, site_id)
    target = db.get(Site, target_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    if target is None:
        raise HTTPException(status_code=404, detail=f"no site {target_id}")
    source_label = {"id": source.id, "name": source.name, "system_id": source.system_id}
    try:
        result = site_service.merge_sites(db, source, target)
    except site_service.MergeRefused as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    system_name = db.scalar(select(System.name).where(System.id == target.system_id))
    activity.record(
        db,
        source="system",
        level="warn",
        kind="site_merged",
        message=(
            f"Merged site {system_name} / {source_label['name']} into "
            f"{system_name} / {target.name}"
        ),
        meta={
            "source": source_label,
            "target_id": target.id,
            "moved": result["moved"],
            "by": principal.label,
        },
    )
    db.commit()
    return SiteMergeResult(
        source=source_label,
        target=site_service.site_out(db, target),
        moved=result["moved"],
        batches=result["batches"],
    )


@router.get(
    "/patches",
    response_model=Page[PatchOut],
    summary="Patches seen on the air",
    description=(
        "A patch is a temporary supergroup dispatch creates; its id is regenerated each time, so "
        "patches are identified by their member set. Every call carries all member tgids, which is "
        "why `talkgroup=` finds patched traffic."
    ),
)
def list_patches(
    db: DbSession,
    response: Response,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Page[PatchOut]:
    total = int(db.scalar(select(func.count()).select_from(Patch)) or 0)
    rows = list(
        db.scalars(
            select(Patch)
            .order_by(Patch.last_seen_at.desc().nullslast())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    aliases = dict(db.execute(select(Talkgroup.tgid, Talkgroup.alias)).all())
    response.headers["X-Total-Count"] = str(total)
    return Page[PatchOut](
        items=[
            PatchOut(
                id=p.id,
                system_id=p.system_id,
                patch_id=p.patch_id,
                member_tgids=list(p.member_tgids or ()),
                member_aliases=[aliases.get(t) or str(t) for t in (p.member_tgids or ())],
                first_seen_at=p.first_seen_at,
                last_seen_at=p.last_seen_at,
                call_count=p.call_count,
            )
            for p in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/conversations",
    response_model=Page[ConversationOut],
    summary="Conversation threads",
    description=(
        "Consecutive calls on one talkgroup separated by less than `CONVERSATION_GAP_SECONDS` are "
        "grouped into a conversation, so a consumer can pull the whole exchange rather than one "
        "three-second key-up. `talkgroup` is the **on-air `tgid`**, as on `GET /calls`."
    ),
)
def list_conversations(
    db: DbSession,
    response: Response,
    principal: OptionalPrincipal,
    talkgroup: Annotated[
        int | None,
        Query(
            ge=-(2**31),
            le=2**31 - 1,
            description=(
                "Talkgroup, as the **on-air `tgid`** — not this archive's `talkgroups.id`, "
                "the same reading `GET /calls?talkgroup=` uses. Add `system` to disambiguate "
                "a tgid two systems both use."
            ),
            examples=[1101],
        ),
    ] = None,
    system: Annotated[list[str] | None, Query(description="System id or name; repeat for OR")] = None,
    min_calls: Annotated[int, Query(ge=1, description="Only threads with at least this many calls")] = 1,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Page[ConversationOut]:
    from app.api.filters import _int_or_name  # noqa: PLC0415 - avoids an import cycle

    conds = [Conversation.call_count >= min_calls]
    if principal is None:
        conds.append(
            or_(
                Conversation.talkgroup_id.is_(None),
                Conversation.talkgroup_id.in_(select(Talkgroup.id).where(Talkgroup.is_hidden.is_(False))),
            )
        )
    # The tgid, never the row id: the two number spaces overlap (production has talkgroups
    # whose ``id`` is another talkgroup's ``tgid``), which is why the calls filters dropped
    # the row-id reading (app/api/filters.py).  A tgid can name one channel on each of
    # several systems, so it resolves to a set of rows, narrowed by ``system`` when given.
    channels = None
    if talkgroup is not None:
        channels = select(Talkgroup.id).where(Talkgroup.tgid == talkgroup)
    if system:
        ids, names = _int_or_name(tuple(system))
        on_system = []
        if ids:
            on_system.append(Talkgroup.system_id.in_(ids))
        if names:
            on_system.append(
                Talkgroup.system_id.in_(select(System.id).where(func.upper(System.name).in_(names)))
            )
        channels = (channels if channels is not None else select(Talkgroup.id)).where(
            or_(*on_system) if on_system else false()
        )
    if channels is not None:
        conds.append(Conversation.talkgroup_id.in_(channels))
    total = int(db.scalar(select(func.count()).select_from(Conversation).where(*conds)) or 0)
    rows = db.scalars(
        select(Conversation)
        .where(*conds)
        .order_by(Conversation.started_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[ConversationOut](
        items=[call_service.conversation_out(db, r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/conversations/{conversation_id}",
    summary="One conversation and its calls",
    response_model=ConversationThread,
)
def get_conversation(
    conversation_id: int, db: DbSession, principal: OptionalPrincipal
) -> ConversationThread:
    row = db.get(Conversation, conversation_id)
    talkgroup = db.get(Talkgroup, row.talkgroup_id) if row and row.talkgroup_id is not None else None
    if row is None or (principal is None and talkgroup is not None and talkgroup.is_hidden):
        raise HTTPException(status_code=404, detail=f"no conversation {conversation_id}")
    from app.api.filters import CallFilters, PageParams  # noqa: PLC0415 - avoids an import cycle

    page = call_service.list_calls(
        db,
        CallFilters(conversation=(conversation_id,), sort="started_at", guest_view=principal is None),
        PageParams(page=1, page_size=500),
    )
    return ConversationThread(
        conversation=call_service.conversation_out(db, row),
        calls=list(page.items),
        total=page.total or 0,
    )


@router.get(
    "/agencies",
    response_model=list[AgencyOut],
    summary="Agency directory",
    description=(
        "The agency directory (seeded from the deployment's agency list, plus any added here), "
        "each with the talkgroups and radios mapped to it. `GET /calls?agency=EXAMPLEPD` resolves "
        "through this map, so "
        "an agency with no talkgroups here simply matches nothing.\n\n"
        "`talkgroup_ids` are **tgids** (the numbers on the air); `talkgroups` are this archive's "
        "**row primary keys**. `shared_talkgroup_ids` is the county dispatch and interop channels "
        "no single department owns, and is the same list on every agency — it is there so a "
        "consumer can discover that set rather than hardcode it, and `?include_shared=true` is how "
        "you ask for it alongside an agency's own traffic."
    ),
)
def list_agencies(db: DbSession) -> list[AgencyOut]:
    return [AgencyOut.model_validate(a) for a in call_service.agencies(db)]


@router.get(
    "/directory/vocabulary",
    response_model=VocabularyOut,
    summary="What every category, service tag and agency kind means",
    description=(
        "The controlled vocabularies a talkgroup and an agency are edited with, each value "
        "with a sentence on what belongs in it. It is static — no database is read — and it "
        "exists so the Help page and `docs/TALKGROUPS.md` render the same words the "
        "validators enforce instead of keeping a second copy that drifts.\n\n"
        "`service_tags` are RadioReference's own vocabulary in RR's spelling, each mapped to "
        "the coarser local `category` the UI colours and filters by; `categories` carries the "
        "reverse of that mapping so either table can be read on its own."
    ),
)
def directory_vocabulary() -> VocabularyOut:
    return VocabularyOut.model_validate(vocabulary())


__all__ = ["router"]
