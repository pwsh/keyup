"""Radio events: the recorder's batch push, and the three ways to read it back (§3.6, §4.2).

``POST /api/recorder/events`` is the third thing a recorder posts, after the calls (§1.6)
and the observation windows (§3.5).  It carries what the control channel says about
*subscribers* — who registered, who affiliated to which talkgroup, who asked for a data
channel — which is the one class of non-voice traffic worth a table of its own
(docs/NON-VOICE-DATA.md §5, docs/RADIO_EVENTS.md).

It authenticates exactly as the observation push does: a Bearer token carrying the
``upload`` scope, checked by ``require_recorder_token``, which is imported from
:mod:`app.api.observations` rather than reimplemented so a recorder needs one credential
and the two endpoints can never drift apart.  Like the other two recorder routes it is in
``OPEN_MUTATION_PATHS`` and ``NO_CSRF_PATHS``: it authenticates itself, and a daemon is not
a browser.

The read side:

``GET /api/radios/{id}/events``
    one radio's timeline, filterable and paged.
``GET /api/sites/{id}/radio-events/summary``
    is the site busy, with what, and with whom — counts by kind and result, the loudest
    radios, and registrations per hour, which is the number that should spike at shift
    change.
``GET /api/sites/{id}/message-mix``
    the *histogram* of every control-channel message, aggregated out of the observation
    payloads.  Not a radio_events reader at all: the firehose is deliberately never stored
    as rows (2.3 M/day), only counted per window, and this is how those counts are read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.observations import RecorderToken
from app.auth import AdminPrincipal
from app.db import get_session
from app.ingest.events import record_events
from app.models import Radio, RadioEvent, RecorderObservation, Site, SiteMessageKind, Talkgroup
from app.schemas import (
    MAX_INSTANCE_ID_LEN,
    LabelCount,
    MessageMixEntry,
    Page,
    RadioEventOut,
    RadioEventsHourPoint,
    RadioEventsSummary,
    RadioEventsSummaryKind,
    RadioEventsSummaryRadio,
    RecorderEventsAccepted,
    RecorderEventsIn,
    SiteMessageKindOut,
    SiteMessageKindPatch,
    SiteMessageMix,
)

log = logging.getLogger("app.api.radio_events")

#: The one sentence every ``instance=`` filter below is documented with.  Written once
#: because it is the same fact each time, and a filter documented two ways is a filter a
#: reader has to compare.
INSTANCE_FILTER_DOC = (
    "Only what this recorder *instance* decoded (its `instance_id`). Two boxes hearing one "
    "site write into the same rows, and this is what separates them; rows from a recorder "
    "that sends no instance match nothing."
)

router = APIRouter(tags=["radio events"])
DbSession = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------------------


@router.post(
    "/recorder/events",
    response_model=RecorderEventsAccepted,
    summary="Post a batch of radio events",
    description=(
        "Up to 1 000 control-channel events about subscribers — unit registration and "
        "de-registration, group affiliation and affiliation queries, location registration, "
        "SNDCP grants and pages, and the ack/deny/queued responses to all of them. Needs a "
        "Bearer token with the `upload` scope, the same one `/api/call-upload` and "
        "`/api/recorder/observations` use. The batch's `instance` — the recorder's own "
        "`instance_id` — is stamped on every row it stores, and is what "
        "`GET /api/radios/{radio_id}/events?instance=…` filters on.\n\n"
        "The batch is **idempotent** on `(site, at, kind, opcode, radio_id, talkgroup)`: a "
        "recorder that spooled a batch through a network failure may resend it, and the "
        "resend costs nothing — note that the key does *not* include `instance`, so when two "
        "recorders hear one message the row keeps whichever instance arrived first. A `kind` "
        "the archive does not know is stored as `other` with "
        "the opcode kept, and **one bad event never costs the batch** — the response counts "
        "`stored`, `duplicates` and `rejected` instead."
    ),
)
def post_events(body: RecorderEventsIn, db: DbSession, token: RecorderToken) -> RecorderEventsAccepted:
    # ``mode="json"`` for the same reason the observation path uses it: the fold reads plain
    # JSON, so a document replayed out of a log behaves identically to one off the wire.
    document = body.model_dump(mode="json")
    result = record_events(db, document)
    db.commit()
    log.info(
        "recorder/events: %s stored, %s duplicate, %s rejected from %s (%s)",
        result.stored,
        result.duplicates,
        result.rejected,
        body.recorder,
        token.label,
    )
    return RecorderEventsAccepted(
        stored=result.stored,
        duplicates=result.duplicates,
        rejected=result.rejected,
        system_id=result.system_id,
        site_id=result.site_id,
        radios_touched=result.radios_touched,
        affiliations_touched=result.affiliations_touched,
        errors=result.errors,
    )


# --------------------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------------------


def event_rows(db: Session, events: list[RadioEvent]) -> list[RadioEventOut]:
    """Decorate a list of events with the site and talkgroup names, in two queries.

    The alternative — a relationship loaded per row — is what turns a 50-row timeline into
    101 statements, and the timeline is on the radio detail page.
    """
    site_ids = {e.site_id for e in events if e.site_id is not None}
    tg_ids = {e.talkgroup_pk for e in events if e.talkgroup_pk is not None}
    sites = (
        dict(db.execute(select(Site.id, Site.name).where(Site.id.in_(site_ids))).all())
        if site_ids
        else {}
    )
    talkgroups = (
        dict(db.execute(select(Talkgroup.id, Talkgroup.alias).where(Talkgroup.id.in_(tg_ids))).all())
        if tg_ids
        else {}
    )
    return [
        RadioEventOut(
            **{
                column.name: getattr(event, column.name)
                for column in RadioEvent.__table__.columns
                if column.name in RadioEventOut.model_fields
            },
            site_name=sites.get(event.site_id),
            talkgroup_alias=talkgroups.get(event.talkgroup_pk),
        )
        for event in events
    ]


@router.get(
    "/radios/{radio_id}/events",
    response_model=Page[RadioEventOut],
    summary="One radio's control-channel timeline",
    description=(
        "Every registration, affiliation and data event the recorder decoded about this "
        "radio, newest first. `from`/`to` bound the window, `kind` filters (repeat for OR), "
        "`instance` narrows it to one recorder instance. "
        "Rows are purged after `RADIO_EVENTS_KEEP_DAYS` (90 by default) — this is presence "
        "data about identifiable people (docs/RADIO_EVENTS.md)."
    ),
)
def radio_events(  # noqa: PLR0913 - a filter set, one per query parameter
    radio_id: int,
    db: DbSession,
    response: Response,
    from_: Annotated[datetime | None, Query(alias="from", description="Inclusive lower bound")] = None,
    to: Annotated[datetime | None, Query(description="Exclusive upper bound")] = None,
    kind: Annotated[list[str] | None, Query(description="Event kind; repeat for OR")] = None,
    instance: Annotated[
        str | None, Query(description=INSTANCE_FILTER_DOC, max_length=MAX_INSTANCE_ID_LEN)
    ] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[RadioEventOut]:
    if db.get(Radio, radio_id) is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    conds = [RadioEvent.radio_pk == radio_id]
    if from_ is not None:
        conds.append(RadioEvent.at >= from_)
    if to is not None:
        conds.append(RadioEvent.at < to)
    if kind:
        conds.append(RadioEvent.kind.in_([k.strip().lower() for k in kind]))
    if instance:
        conds.append(RadioEvent.instance == instance)

    total = int(db.scalar(select(func.count()).select_from(RadioEvent).where(*conds)) or 0)
    rows = list(
        db.scalars(
            select(RadioEvent)
            .where(*conds)
            .order_by(RadioEvent.at.desc(), RadioEvent.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[RadioEventOut](
        items=event_rows(db, rows), total=total, page=page, page_size=page_size
    )


def _summary(
    db: Session, site_id: int, hours: int, instance: str | None = None
) -> RadioEventsSummary:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    since = datetime.now(UTC) - timedelta(hours=hours)
    where: tuple[Any, ...] = (RadioEvent.site_id == site_id, RadioEvent.at >= since)
    if instance:
        where = (*where, RadioEvent.instance == instance)

    total = int(db.scalar(select(func.count()).select_from(RadioEvent).where(*where)) or 0)
    by_kind = [
        RadioEventsSummaryKind(kind=k, count=int(c))
        for k, c in db.execute(
            select(RadioEvent.kind, func.count())
            .where(*where)
            .group_by(RadioEvent.kind)
            .order_by(func.count().desc())
        )
    ]
    by_result = [
        LabelCount(key=r or "none", label=r, count=int(c))
        for r, c in db.execute(
            select(RadioEvent.result, func.count())
            .where(*where)
            .group_by(RadioEvent.result)
            .order_by(func.count().desc())
        )
    ]
    top = [
        RadioEventsSummaryRadio(
            radio_pk=pk, radio_id=rid, alias=alias, unit_id=unit, count=int(count)
        )
        for pk, rid, alias, unit, count in db.execute(
            select(RadioEvent.radio_pk, RadioEvent.radio_id, Radio.alias, Radio.unit_id, func.count())
            .join(Radio, RadioEvent.radio_pk == Radio.id, isouter=True)
            .where(*where, RadioEvent.radio_id.isnot(None))
            .group_by(RadioEvent.radio_pk, RadioEvent.radio_id, Radio.alias, Radio.unit_id)
            .order_by(func.count().desc())
            .limit(20)
        )
    ]
    bucket = func.date_trunc("hour", RadioEvent.at)
    per_hour = [
        RadioEventsHourPoint(at=at, count=int(count))
        for at, count in db.execute(
            select(bucket, func.count())
            .where(*where, RadioEvent.kind == "unit_registration")
            .group_by(bucket)
            .order_by(bucket)
        )
    ]
    return RadioEventsSummary(
        site_id=site_id,
        site_name=site.name,
        instance=instance,
        hours=hours,
        since=since,
        total=total,
        by_kind=by_kind,
        by_result=by_result,
        top_radios=top,
        registrations_per_hour=per_hour,
    )


@router.get(
    "/sites/{site_id}/radio-events/summary",
    response_model=RadioEventsSummary,
    summary="Radio-event activity at one site",
    description=(
        "Counts by kind and by result over the last `hours`, the twenty busiest radios, and "
        "`unit_registration` per hour — the series that should spike at shift change. "
        "`instance` narrows every count to one recorder instance."
    ),
)
def site_radio_event_summary(
    site_id: int,
    db: DbSession,
    hours: Annotated[int, Query(ge=1, le=24 * 30, description="Window, hours")] = 24,
    instance: Annotated[
        str | None, Query(description=INSTANCE_FILTER_DOC, max_length=MAX_INSTANCE_ID_LEN)
    ] = None,
) -> RadioEventsSummary:
    return _summary(db, site_id, hours, instance)


def _mix_key(raw: str) -> tuple[int, int] | None:
    """``"164:15"`` / ``"164/15"`` / ``"15"`` → ``(mfid, opcode)``.

    trunkd writes ``mfid:opcode``; the histogram in docs/NON-VOICE-DATA.md §3.1 was
    generated with a slash.  Both are accepted, and a bare number is a standard (mfid 0)
    opcode, because the one thing that must not happen is a census key being dropped for
    punctuation.
    """
    text = str(raw).strip()
    for separator in (":", "/", "-"):
        if separator in text:
            left, _, right = text.partition(separator)
            try:
                return int(left), int(right)
            except ValueError:
                return None
    try:
        return 0, int(text)
    except ValueError:
        return None


def _aggregate_mix(
    windows: list[tuple[datetime, dict[str, Any]]],
    field: str,
    exemplars: dict[tuple[int, int], str],
) -> list[MessageMixEntry]:
    """Fold one census field across every window into ``mfid:opcode`` totals."""
    totals: dict[tuple[int, int], MessageMixEntry] = {}
    for observed_at, payload in windows:
        mix = payload.get(field)
        if not isinstance(mix, dict):
            continue
        for raw_key, raw_count in mix.items():
            key = _mix_key(raw_key)
            if key is None:
                continue
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            entry = totals.get(key)
            if entry is None:
                totals[key] = MessageMixEntry(
                    key=f"{key[0]}:{key[1]}",
                    mfid=key[0],
                    opcode=key[1],
                    count=count,
                    windows=1,
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                    exemplar=exemplars.get(key),
                )
                continue
            entry.count += count
            entry.windows += 1
            if entry.first_seen_at is None or observed_at < entry.first_seen_at:
                entry.first_seen_at = observed_at
            if entry.last_seen_at is None or observed_at > entry.last_seen_at:
                entry.last_seen_at = observed_at
    return sorted(totals.values(), key=lambda e: (-e.count, e.mfid, e.opcode))


@router.get(
    "/sites/{site_id}/message-mix",
    response_model=SiteMessageMix,
    summary="Control-channel message census for one site",
    description=(
        "Every `mfid:opcode` the recorder counted over the last `days`, with totals, the "
        "windows it appeared in, first and last sighting, and — for a message the decoder "
        "could not name — one hex exemplar. Aggregated out of the stored observation "
        "payloads: the raw TSBK stream is 2.3 M messages a day and is deliberately never "
        "stored as rows (docs/NON-VOICE-DATA.md §5). This is how a new opcode appearing on "
        "the air becomes visible. `instance` folds only the windows one recorder instance "
        "posted, which is how two boxes on one site are compared rather than summed."
    ),
)
def site_message_mix(
    site_id: int,
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365, description="Window, days")] = 7,
    instance: Annotated[
        str | None,
        Query(
            description=(
                "Only the observation windows this recorder *instance* posted. Windows from a "
                "recorder that sends no instance match nothing."
            ),
            max_length=MAX_INSTANCE_ID_LEN,
        ),
    ] = None,
) -> SiteMessageMix:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    since = datetime.now(UTC) - timedelta(days=days)
    rows = list(
        db.execute(
            select(RecorderObservation.observed_at, RecorderObservation.payload)
            .where(
                RecorderObservation.site_id == site_id,
                RecorderObservation.observed_at >= since,
                *(
                    (RecorderObservation.instance == instance,)
                    if instance
                    else ()
                ),
            )
            .order_by(RecorderObservation.observed_at)
        ).all()
    )
    windows = [(at, payload) for at, payload in rows if isinstance(payload, dict)]

    # The exemplars are a flat list per window; the newest one wins, so the hex shown is
    # what the site is transmitting *now* and a change is visible against the first_seen.
    exemplars: dict[tuple[int, int], str] = {}
    for _at, payload in windows:
        for entry in payload.get("unknown_exemplars") or ():
            if not isinstance(entry, dict) or not entry.get("exemplar"):
                continue
            try:
                key = (int(entry.get("mfid") or 0), int(entry.get("opcode")))
            except (TypeError, ValueError):
                continue
            exemplars[key] = str(entry["exemplar"])

    messages = _aggregate_mix(windows, "message_mix", exemplars)
    return SiteMessageMix(
        site_id=site_id,
        site_name=site.name,
        instance=instance,
        days=days,
        since=since,
        windows=len(windows),
        total=sum(entry.count for entry in messages),
        messages=messages,
        link_control=_aggregate_mix(windows, "link_control_mix", exemplars),
    )


# --------------------------------------------------------------------------------------
# the message-kind registry (docs/UNKNOWN-DATA-PLAN.md phase 5)
#
# ``/message-mix`` above is a rolling histogram recomputed on every read; these two routes
# read and edit the *memory* underneath it — the row per (site, mfid, opcode) that makes a
# first sighting an event.  The census stays as it is: it answers a different question.
# --------------------------------------------------------------------------------------


def _kind_out(row: SiteMessageKind) -> SiteMessageKindOut:
    return SiteMessageKindOut(
        site_id=row.site_id,
        key=f"{row.mfid}:{row.opcode}",
        mfid=row.mfid,
        opcode=row.opcode,
        count=int(row.count or 0),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        exemplar=row.exemplar,
        exemplar_previous=row.exemplar_previous,
        exemplar_changed_at=row.exemplar_changed_at,
        known=bool(row.known),
        label=row.label,
        alerted_at=row.alerted_at,
    )


@router.get(
    "/sites/{site_id}/message-kinds",
    response_model=list[SiteMessageKindOut],
    summary="Every control-channel message kind this site has ever transmitted",
    description=(
        "One row per `mfid:opcode`, newest first — when it was first and last heard, how many "
        "times, and the newest hex exemplar with the one it replaced when the recorder cannot "
        "decode it. Unlike `/message-mix`, which is a rolling census folded out of the last "
        "few days of observation windows, this is the archive's memory: it is what makes a "
        "kind appearing for the first time an event (`site.unknown_new` in the activity log, "
        "and a `new_kind` alert). `unknown_only=true` hides the kinds an operator has already "
        "named."
    ),
)
def list_site_message_kinds(
    site_id: int,
    db: DbSession,
    unknown_only: Annotated[
        bool, Query(description="Only kinds nobody has named yet (`known=false`)")
    ] = False,
) -> list[SiteMessageKindOut]:
    if db.get(Site, site_id) is None:
        raise HTTPException(status_code=404, detail=f"no site {site_id}")
    stmt = select(SiteMessageKind).where(SiteMessageKind.site_id == site_id)
    if unknown_only:
        stmt = stmt.where(SiteMessageKind.known.is_(False))
    stmt = stmt.order_by(
        SiteMessageKind.first_seen_at.desc(), SiteMessageKind.mfid, SiteMessageKind.opcode
    )
    return [_kind_out(row) for row in db.scalars(stmt)]


@router.patch(
    "/admin/sites/{site_id}/message-kinds/{mfid}/{opcode}",
    response_model=SiteMessageKindOut,
    summary="Name a control-channel message kind",
    description=(
        "Mark a `mfid:opcode` as known and give it a label. Naming a kind is what stops it "
        "being news: the registry in docs/IDENTIFIERS.md is the durable version of the same "
        "act, and this is the one an operator performs from the Sites page. Only the keys "
        "present are written. Admin only."
    ),
)
def patch_site_message_kind(
    site_id: int,
    mfid: int,
    opcode: int,
    body: SiteMessageKindPatch,
    db: DbSession,
    principal: AdminPrincipal,
) -> SiteMessageKindOut:
    row = db.scalars(
        select(SiteMessageKind).where(
            SiteMessageKind.site_id == site_id,
            SiteMessageKind.mfid == mfid,
            SiteMessageKind.opcode == opcode,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"site {site_id} has never transmitted {mfid}:{opcode}")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return _kind_out(row)


# The paths the analysis notes name (`/api/directory/sites/…`).  The directory router has
# never carried a `/directory` prefix — its own routes are `/radios`, `/talkgroups` — so
# the canonical spelling above matches `/api/sites/{id}/channel-audit`, and these two are
# unlisted aliases so a client written against either spelling works.
router.add_api_route(
    "/directory/sites/{site_id}/radio-events/summary",
    site_radio_event_summary,
    methods=["GET"],
    response_model=RadioEventsSummary,
    include_in_schema=False,
)
router.add_api_route(
    "/directory/sites/{site_id}/message-mix",
    site_message_mix,
    methods=["GET"],
    response_model=SiteMessageMix,
    include_in_schema=False,
)

__all__ = ["event_rows", "router"]
