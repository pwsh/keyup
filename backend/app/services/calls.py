"""Reading calls: the paged list, the facets and the full detail document (§4.1)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, String, func, literal, literal_column, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api.filters import (
    CALL_COLUMNS,
    DEFAULT_SORT,
    CallFilters,
    PageParams,
    apply_call_filters,
    base_call_select,
    capped_count,
    count_call_select,
    cursor_condition,
    encode_cursor,
    order_by,
    resolve_filter_sets,
    search_query,
    with_call_joins,
)
from app.config import get_settings
from app.ingest.reconcile import duplicates_of
from app.models import (
    Agency,
    Call,
    CallSource,
    Conversation,
    ExternalLink,
    Patch,
    Person,
    Radio,
    RadioAssignment,
    Site,
    System,
    Talkgroup,
    Transcript,
    Unit,
)
from app.schemas import (
    CallCopyOut,
    CallDetail,
    CallOut,
    CallSourceOut,
    ConversationOut,
    ExternalLinkOut,
    FacetBucket,
    FacetsOut,
    PatchOut,
    RadioOut,
    RawMetadata,
    SiteOut,
    SystemOut,
    TalkgroupOut,
    TranscriptOut,
)
from app.services import counts as counts_service

log = logging.getLogger("app.services.calls")

#: ``ts_headline`` options: only ``<mark>``, one fragment, short enough for a table cell.
HEADLINE_OPTIONS = "StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MaxWords=18, MinWords=5"


def audio_url(call_id: int) -> str:
    return f"/api/calls/{call_id}/audio"


def row_to_call(row: Row[Any] | Any, *, highlight: str | None = None) -> CallOut:
    """Turn one denormalized row (see ``CALL_COLUMNS``) into a :class:`CallOut`."""
    data = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    data["duration_s"] = float(data.get("duration_s") or 0)
    data["notes"] = data.get("notes") or ""
    data["tags"] = list(data.get("tags") or ())
    data["talkgroup_ids"] = list(data.get("talkgroup_ids") or ())
    data["patch_member_tgids"] = list(data.get("patch_member_tgids") or ())
    if data.get("patch_id") is None:
        data["patch_label"] = None
    data["audio_url"] = audio_url(data["id"])
    # A path, not a flag, is what the column holds; the API says only whether there *is*
    # one, because where a file lives on the server is not a client's business.
    data["processed_audio"] = bool(data.pop("processed_path", None))
    if highlight is not None:
        data["transcript_highlight"] = highlight
    return CallOut.model_validate(data)


@dataclass(frozen=True)
class CallPage:
    """One page of calls, plus what is known about how many there are in all."""

    items: list[CallOut]
    #: ``None`` while cursor paging: an infinite scroll never shows a total, and counting
    #: for it was a full scan per page.
    total: int | None
    #: True when the count stopped at :data:`app.api.filters.COUNT_CAP` — "at least that".
    total_capped: bool
    next_cursor: str | None


def list_calls(session: Session, f: CallFilters, paging: PageParams) -> CallPage:
    """One page of calls, the total for the filter, and the cursor that continues it.

    Both pagination styles are supported at once: ``cursor`` wins when present (keyset,
    stable while calls are arriving), otherwise ``page``/``page_size`` offsets.

    The total is counted only for offset paging, and only up to
    :data:`~app.api.filters.COUNT_CAP` rows.
    """
    f = resolve_filter_sets(session, f)
    total: int | None = None
    capped = False
    if not paging.cursor:
        total, capped = capped_count(session, f)
    if paging.cursor:
        stmt = base_call_select(f).where(cursor_condition(f, paging.cursor))
        stmt = stmt.order_by(*order_by(f)).limit(paging.page_size)
    elif paging.offset:
        stmt = _deferred_page(f, paging)
    else:
        stmt = base_call_select(f).order_by(*order_by(f)).limit(paging.page_size)
    rows = list(session.execute(stmt))
    items = [row_to_call(r) for r in rows]
    next_cursor = None
    if items and len(items) == paging.page_size:
        last = rows[-1]._mapping
        next_cursor = encode_cursor(last["started_at"], last["id"])
    return CallPage(items=items, total=total, total_capped=capped, next_cursor=next_cursor)


def _deferred_page(f: CallFilters, paging: PageParams) -> Any:
    """An offset page as a deferred join: pick the page's ids first, then build its rows.

    ``OFFSET k`` over the denormalized SELECT made PostgreSQL produce — and throw away — k
    fully joined, ~55-column rows: six directory lookups each, and a sort of the wide rows
    when the plan was a sort (45 MB across four workers for ``offset=50000`` on a busy
    talkgroup).  Here the skipped rows are only ever ``calls.id`` plus the sort key: the
    inner query carries the same joins as every other filtered query, but nothing reads
    their columns, so the planner removes them.  The page's own ``page_size`` rows are then
    joined back by primary key and re-sorted, which is ``page_size`` rows, not ``k``.

    The skipped rows are still *visited*: an offset cannot know where row k is without
    counting to it.  That walk is the floor, and the cursor is the way under it.
    """
    ids = (
        apply_call_filters(with_call_joins(select(Call.id).select_from(Call)), f)
        .order_by(*order_by(f))
        .limit(paging.page_size)
        .offset(paging.offset)
        .subquery("page_ids")
    )
    stmt = select(*CALL_COLUMNS).select_from(ids).join(Call, Call.id == ids.c.id)
    return with_call_joins(stmt).order_by(*order_by(f))


def _bucket(rows: list[Any], label_of: Any = None) -> list[FacetBucket]:
    out = []
    for row in rows:
        key = row[0]
        if key is None:
            continue
        label = row[1] if len(row) > 2 else None
        count = row[-1]
        out.append(FacetBucket(key=str(key), label=str(label if label is not None else key), count=count))
    return out


#: How long a facet query may run before it is cancelled.  The sidebar is a *hint* — nine
#: counts a reader glances at — and it shares the API's connection pool with the recorders'
#: pushes.  The incident this bound exists for had five facet queries at 14-24 s each
#: starving ``POST /api/recorder/spectrum`` until it timed out at 30 s with a 500: the
#: archive stopped ingesting because somebody ticked seven categories.  Fifteen seconds is
#: comfortably more than any facet needs once the talkgroup sets are resolved (the worst of
#: them measured 123 ms on production) and far less than a pool slot is worth.
FACET_TIMEOUT = "15s"

#: How long a computed facet set is reused.  A reader clicking through the category picker
#: fires one request per click, each one nine aggregate queries over the same table; a
#: minute of staleness costs them nothing — the numbers are approximate the moment they are
#: drawn, because calls keep landing — and it collapses a burst into one round of work.
FACET_TTL_S = 60.0

#: Single-flight locks for :func:`facets`, taken by ``hash(key) % len``.  A fixed pool
#: rather than one lock per key so the table cannot grow without bound; two unrelated
#: filter sets that collide simply take their turn, which is the behaviour we want anyway.
_FACET_LOCKS = tuple(threading.Lock() for _ in range(16))


def _facet_key(f: CallFilters, limit: int) -> str:
    """Cache key: the filter as it was applied, normalised, plus the per-dimension cap.

    ``sort`` is normalised away — it changes the order of the rows, not which rows they
    are, and a facet count does not depend on it.  What is *resolved* on the filter is not
    part of the key either: :meth:`CallFilters.echo` skips the private fields, and it must,
    because the resolved sets are an answer derived from the filter rather than part of it.
    """
    normalised = f.replace(sort=DEFAULT_SORT)
    return "facets:" + json.dumps({"f": normalised.echo(), "limit": limit}, sort_keys=True, default=str)


def _is_timeout(exc: BaseException) -> bool:
    """True for PostgreSQL's ``57014 query_canceled`` — our own statement_timeout firing."""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "sqlstate", None) == "57014" or getattr(orig, "pgcode", None) == "57014":
        return True
    return "statement timeout" in str(exc).lower()


def _arm_facet_timeout(session: Session) -> None:
    """``SET LOCAL statement_timeout`` for the facet queries, on PostgreSQL only.

    ``LOCAL`` means "until this transaction ends", so it cannot leak onto the next thing
    this session does — and because a cancelled statement aborts the transaction, it has to
    be re-armed after every rollback.
    """
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") != "postgresql":
        return  # SQLite has no statement_timeout; it also has no five-gigabyte tables
    session.execute(text(f"SET LOCAL statement_timeout = '{FACET_TIMEOUT}'"))


def facets(session: Session, f: CallFilters, *, limit: int = 50) -> FacetsOut:
    """Counts per dimension for the current filter — the Calls sidebar (§4.1).

    Every dimension is computed against the *same* filtered set, so clicking a value and
    re-running the query reproduces exactly the count that was shown.

    Three bounds, all of them scars (see :data:`FACET_TIMEOUT` for the incident):

    * the talkgroup sets are resolved to literal arrays first, so the planner can cost the
      ``talkgroup_ids &&`` predicate instead of guessing (:func:`resolve_filter_sets`);
    * every query runs under a 15 s ``statement_timeout``, and a dimension that hits it is
      dropped from the answer with ``incomplete=True`` rather than failing the request —
      eight counts and a caveat beat a 500;
    * the whole answer is cached for a minute under a single-flight lock, so seven clicks
      on the category picker cost one round of queries rather than seven.
    """
    key = _facet_key(f, limit)
    lock = _FACET_LOCKS[hash(key) % len(_FACET_LOCKS)]
    with lock:
        return counts_service.cached(key, FACET_TTL_S, lambda: _facets(session, f, limit=limit))


def _facets(session: Session, f: CallFilters, *, limit: int = 50) -> FacetsOut:
    """Compute the facet set — see :func:`facets`, which is the caching front door."""
    f = resolve_filter_sets(session, f)
    incomplete = False
    _arm_facet_timeout(session)

    def guarded[T](compute: Callable[[], T], empty: T, name: str) -> T:
        """Run one dimension; on our own timeout drop it and keep the rest.

        A cancelled statement leaves the transaction aborted, so the rollback is not
        optional — without it every later dimension fails with ``InFailedSqlTransaction``
        and the "partial answer" would be no answer at all.  The timeout is re-armed
        afterwards because ``SET LOCAL`` died with the transaction.
        """
        nonlocal incomplete
        try:
            return compute()
        except DBAPIError as exc:
            if not _is_timeout(exc):
                raise
            log.warning("facets: %s timed out after %s, dropped from the answer", name, FACET_TIMEOUT)
            incomplete = True
            session.rollback()
            _arm_facet_timeout(session)
            return empty

    base = with_call_joins(select().select_from(Call))

    def counts(
        name: str, *columns: Any, group: Any, order_desc: bool = True, cap: int | None = None
    ) -> list[Any]:
        def run() -> list[Any]:
            stmt = base_call_select(f, *columns, func.count().label("count")).group_by(*group)
            stmt = stmt.order_by(func.count().desc()) if order_desc else stmt.order_by(*group)
            return list(session.execute(stmt.limit(cap or limit)))

        return guarded(run, [], name)

    total = guarded(lambda: int(session.scalar(count_call_select(f)) or 0), 0, "total")
    # Keyed by the **on-air tgid**, because the bucket key is what the sidebar hands back to
    # ``?talkgroup=`` and that parameter means the tgid.  It used to be ``calls.talkgroup_id``
    # — the primary key — so clicking a bucket asked a question about a different channel
    # whenever some other talkgroup's tgid equalled that pk.  Grouping by tgid also merges
    # the same channel recorded under two systems, which is exactly what the filter does.
    talkgroup = counts(
        "talkgroup",
        Talkgroup.tgid,
        func.coalesce(Talkgroup.alias, func.cast(Talkgroup.tgid, String), "(unknown)"),
        group=(Talkgroup.tgid, Talkgroup.alias),
    )
    radio = counts(
        "radio",
        Call.radio_id,
        func.coalesce(Radio.unit_id, Radio.alias, func.cast(Radio.radio_id, String), "(unknown)"),
        group=(Call.radio_id, Radio.unit_id, Radio.alias, Radio.radio_id),
    )
    agency = counts("agency", Talkgroup.agency_code, Talkgroup.agency_code, group=(Talkgroup.agency_code,))
    category = counts("category", Talkgroup.category, Talkgroup.category, group=(Talkgroup.category,))
    status = counts(
        "transcript_status", Call.transcript_status, Call.transcript_status, group=(Call.transcript_status,)
    )
    quality = counts(
        "quality", Call.transcript_quality, Call.transcript_quality, group=(Call.transcript_quality,)
    )
    hour_expr = func.to_char(Call.started_local, "HH24")
    hours = counts("hour", hour_expr, hour_expr, group=(hour_expr,), order_desc=False, cap=24)
    dow_expr = func.cast(func.extract("dow", Call.started_local), String)
    dows = counts("dow", dow_expr, dow_expr, group=(dow_expr,), order_desc=False, cap=7)
    tag_expr = func.unnest(Call.tags).label("tag")
    tag_rows = guarded(
        lambda: list(
            session.execute(
                base_call_select(f, tag_expr, func.count().label("count"))
                .group_by(tag_expr)
                .order_by(func.count().desc())
                .limit(limit)
            )
        ),
        [],
        "tag",
    )
    del base  # only used to keep the join list honest above
    return FacetsOut(
        talkgroup=_bucket(talkgroup),
        radio=_bucket(radio),
        agency=_bucket(agency),
        category=_bucket(category),
        hour=_bucket(hours),
        dow=_bucket(dows),
        transcript_status=_bucket(status),
        quality=_bucket(quality),
        tag=[FacetBucket(key=str(r[0]), label=str(r[0]), count=r[1]) for r in tag_rows],
        total=total,
        incomplete=incomplete,
    )


def call_if_matches(session: Session, f: CallFilters, call_id: int) -> CallOut | None:
    """Re-read one call and return it only when it satisfies ``f``.

    This is how the live feed filters: a ``pg_notify`` payload carries an id, and the same
    predicates that build ``GET /calls`` decide whether this subscriber wanted to hear about
    it.  One query, and no second implementation of the filter language.
    """
    f = resolve_filter_sets(session, f)
    row = session.execute(base_call_select(f).where(Call.id == call_id)).first()
    return row_to_call(row) if row is not None else None


def transcript_out(row: Transcript) -> TranscriptOut:
    """A transcript row, with the human correction preferred over the engine's text."""
    rtf = None
    if row.processing_ms and row.call is not None and float(row.call.duration_s or 0) > 0:
        rtf = round(float(row.call.duration_s) / (row.processing_ms / 1000.0), 3)
    return TranscriptOut(
        id=row.id,
        call_id=row.call_id,
        engine=row.engine,
        backend_name=row.backend_name,
        model=row.model,
        language=row.language,
        text=row.edited_text if row.edited_by_user and row.edited_text else row.text_,
        original_text=row.text_,
        segments=list(row.segments or ()),
        words=row.words,
        avg_logprob=row.avg_logprob,
        no_speech_prob=row.no_speech_prob,
        compression_ratio=row.compression_ratio,
        quality=row.quality,
        quality_reasons=list(row.quality_reasons or ()),
        prompt_used=row.prompt_used,
        vad_used=row.vad_used,
        processing_ms=row.processing_ms,
        rtf=rtf,
        created_at=row.created_at,
        is_primary=row.is_primary,
        edited_by_user=row.edited_by_user,
        edited_text=row.edited_text,
    )


def call_row(session: Session, call_id: int) -> Any | None:
    stmt = with_call_joins(select(*CALL_COLUMNS).select_from(Call)).where(Call.id == call_id)
    return session.execute(stmt).first()


def _id3_frames(call: Call) -> dict[str, str]:
    """The ID3 frame map on an sdrtrunk call, or nothing.

    ``raw_meta`` holds whichever recorder's metadata this call came with; only the sdrtrunk
    file dialect's is a flat map of ID3 frames, so only that one is offered as ``id3``.
    """
    if call.ingest_source != "file" or not isinstance(call.raw_meta, dict):
        return {}
    return {key: str(value) for key, value in call.raw_meta.items() if isinstance(value, str)}


def call_sources(session: Session, call_id: int) -> list[CallSourceOut]:
    """Every radio that keyed up inside the call, in order, with a label for each.

    The label is resolved the same way ``CallOut.radio_label`` is — unit id, then alias,
    then the bare number — with the recorder's own ``tag`` as the last resort, because a
    source whose radio was never added to the directory still has a name on the sidecar.
    """
    rows = session.execute(
        select(CallSource, Radio)
        .outerjoin(Radio, CallSource.radio_id == Radio.id)
        .where(CallSource.call_id == call_id)
        .order_by(CallSource.seq, CallSource.id)
    ).all()
    out: list[CallSourceOut] = []
    for source, radio in rows:
        label = None
        if radio is not None:
            label = radio.unit_id or radio.alias or (str(radio.radio_id) if radio.radio_id else None)
        label = label or source.tag or (str(source.src) if source.src is not None else None)
        out.append(
            CallSourceOut(
                id=source.id,
                radio_id=source.radio_id,
                radio_label=label,
                src=source.src,
                pos_s=float(source.pos_s or 0),
                len_s=float(source.len_s) if source.len_s is not None else None,
                emergency=bool(source.emergency),
                tag=source.tag,
                seq=source.seq,
            )
        )
    return out


def call_detail(session: Session, call_id: int) -> CallDetail | None:
    """The full detail document: the row, the directory objects, the primary transcript,
    the adjacent calls on the same talkgroup, the conversation and the external links."""
    row = call_row(session, call_id)
    if row is None:
        return None
    call = session.get(Call, call_id)
    if call is None:  # pragma: no cover - defensive, the row above proves it exists
        return None
    base = row_to_call(row).model_dump()

    transcripts = list(
        session.scalars(
            select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.created_at.desc())
        )
    )
    primary = next((t for t in transcripts if t.is_primary), transcripts[0] if transcripts else None)

    prev_id = next_id = None
    # "The call before this one" means "on the same channel", and for a private call the
    # channel is the pair of radios: stepping through a unit call's neighbours by talkgroup
    # is impossible (it has none) and by frequency would walk into whatever the site granted
    # that traffic channel to next, which is somebody else's conversation.
    neighbour = (
        (Call.kind == "unit")
        & (Call.radio_id == call.radio_id)
        & (Call.target_radio_id == call.target_radio_id)
        if call.kind == "unit"
        else (Call.kind == "group") & (Call.talkgroup_id == call.talkgroup_id)
    )
    if call.kind == "unit" or call.talkgroup_id is not None:
        prev_id = session.scalar(
            select(Call.id)
            .where(
                neighbour,
                Call.duplicate_of.is_(None),
                or_(
                    Call.started_at < call.started_at,
                    (Call.started_at == call.started_at) & (Call.id < call.id),
                ),
            )
            .order_by(Call.started_at.desc(), Call.id.desc())
            .limit(1)
        )
        next_id = session.scalar(
            select(Call.id)
            .where(
                neighbour,
                Call.duplicate_of.is_(None),
                or_(
                    Call.started_at > call.started_at,
                    (Call.started_at == call.started_at) & (Call.id > call.id),
                ),
            )
            .order_by(Call.started_at.asc(), Call.id.asc())
            .limit(1)
        )

    conversation = None
    conversation_ids: list[int] = []
    if call.conversation_id is not None:
        row_conv = session.get(Conversation, call.conversation_id)
        if row_conv is not None:
            conversation = conversation_out(session, row_conv)
            conversation_ids = list(
                session.scalars(
                    select(Call.id)
                    .where(Call.conversation_id == row_conv.id)
                    .order_by(Call.started_at.asc(), Call.id.asc())
                )
            )

    links = [
        ExternalLinkOut.model_validate(link)
        for link in session.scalars(
            select(ExternalLink)
            .where(
                or_(
                    ExternalLink.call_id == call_id,
                    (
                        ExternalLink.conversation_id == call.conversation_id
                        if call.conversation_id is not None
                        else literal(False)
                    ),
                )
            )
            .order_by(ExternalLink.created_at.desc())
        )
    ]

    system = session.get(System, call.system_id) if call.system_id else None
    site = session.get(Site, call.site_id) if call.site_id else None
    talkgroup = session.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    radio = session.get(Radio, call.radio_id) if call.radio_id else None
    target_radio = session.get(Radio, call.target_radio_id) if call.target_radio_id else None
    patch = session.get(Patch, call.patch_id) if call.patch_id else None

    return CallDetail(
        **base,
        system=SystemOut.model_validate(system) if system else None,
        site=SiteOut.model_validate(site) if site else None,
        talkgroup=TalkgroupOut.model_validate(talkgroup) if talkgroup else None,
        radio=RadioOut.model_validate(radio) if radio else None,
        target_radio=RadioOut.model_validate(target_radio) if target_radio else None,
        patch=PatchOut.model_validate(patch) if patch else None,
        sources=call_sources(session, call_id),
        raw=RawMetadata(
            file_path=call.file_path,
            file_name=call.file_name,
            file_size=call.file_size,
            sha256=call.sha256,
            bitrate=call.bitrate,
            sample_rate=call.sample_rate,
            channels=call.channels,
            recorder_version=call.recorder_version,
            file_mtime=call.file_mtime,
            # For an sdrtrunk file ``raw_meta`` *is* the ID3 frame map, so the long-standing
            # empty ``id3`` finally has its contents; for any other dialect there are no ID3
            # frames and the panel reads ``raw_meta`` instead.
            id3=_id3_frames(call),
            raw_meta=call.raw_meta or {},
        ),
        transcript=transcript_out(primary) if primary is not None else None,
        transcript_count=len(transcripts),
        prev_call_id=prev_id,
        next_call_id=next_id,
        conversation=conversation,
        conversation_call_ids=conversation_ids,
        links=links,
        # The other recorders' copies of this transmission, with the score each was given
        # when the primary was chosen (§5.1 step 4, docs/RECONCILIATION.md).
        duplicates=[CallCopyOut(**copy) for copy in duplicates_of(session, call, get_settings())],
        reconcile_reason=call.reconcile_reason,
        reconciled_at=call.reconciled_at,
        # Which receiver's recording this actually is, and what it said about hearing it.
        # Detail-only on purpose: the list is the hot path and nothing in a table cell wants
        # a frame count, but on one call "why is this rec-02's copy" deserves an answer.
        source_instance=call.source_instance,
        source_quality=call.source_quality,
    )


def conversation_out(session: Session, row: Conversation) -> ConversationOut:
    talkgroup = session.get(Talkgroup, row.talkgroup_id) if row.talkgroup_id else None
    started_local = session.scalar(
        select(Call.started_local).where(Call.conversation_id == row.id).order_by(Call.started_at).limit(1)
    )
    return ConversationOut(
        id=row.id,
        talkgroup_id=row.talkgroup_id,
        talkgroup_alias=talkgroup.alias if talkgroup else None,
        tgid=talkgroup.tgid if talkgroup else None,
        started_at=row.started_at,
        started_local=started_local,
        ended_at=row.ended_at,
        call_count=row.call_count,
        first_call_id=row.first_call_id,
        duration_s=max(0.0, (row.ended_at - row.started_at).total_seconds()),
    )


def search_calls(
    session: Session, f: CallFilters, paging: PageParams
) -> tuple[list[tuple[CallOut, str, float]], int, bool]:
    """Ranked full-text search: ``ts_rank_cd`` order plus ``ts_headline`` highlights (§4.3).

    The headline is taken over the transcript when there is one and the recorded talkgroup
    alias otherwise, so an untranscribed call still shows *why* it matched.

    Returns the hits, the match count and whether that count stopped at
    :data:`~app.api.filters.COUNT_CAP` — "at least this many".
    """
    if not f.q:
        page = list_calls(session, f, paging)
        return [(c, "", 0.0) for c in page.items], page.total or 0, page.total_capped

    f = resolve_filter_sets(session, f)
    # Widened to its spelling variants when ``f.fuzzy`` found any, so the rank and the
    # highlight both see the words that actually matched.
    query = search_query(f)
    rank = func.ts_rank_cd(Call.search_tsv, query).label("rank")
    document = func.concat_ws(
        " — ", func.coalesce(Call.transcript_text, ""), func.coalesce(Call.talkgroup_alias_at_record, "")
    )
    highlight = func.ts_headline(
        literal_column("'english'"), document, query, literal(HEADLINE_OPTIONS)
    ).label("highlight")
    total, capped = capped_count(session, f)
    stmt = (
        base_call_select(f, *CALL_COLUMNS, rank, highlight)
        .order_by(rank.desc(), Call.started_at.desc(), Call.id.desc())
        .limit(paging.page_size)
        .offset(paging.offset)
    )
    hits = []
    for row in session.execute(stmt):
        mapping = row._mapping
        hits.append((row_to_call(row), mapping["highlight"] or "", float(mapping["rank"] or 0)))
    return hits, total, capped


#: ``%``, ``_`` and the escape character itself are wildcards to ``ILIKE``; typing one in
#: the search box asked for "everything", once per keystroke, over four tables (L5).
_LIKE_SPECIALS = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def like_contains(value: str) -> str:
    """``%value%`` with the caller's wildcards escaped, for use with ``ILIKE``."""
    return f"%{value.strip().translate(_LIKE_SPECIALS)}%"


#: Shortest input that also gets near-spelling suggestions: below this every alias shares a
#: trigram with it and "near" means nothing.
FUZZY_SUGGEST_MIN_LENGTH = 4


def suggestions(
    session: Session, q: str, limit: int = 12, *, fuzzy: bool = False
) -> list[dict[str, Any]]:
    """Suggestions from talkgroup aliases, the roster, radio labels and user tags (§4.3).

    The roster (``units``, ``personnel``) is offered *before* the radio-derived unit ids and
    aliases: a curated callsign is a better thing to hand somebody than a label ingest
    happened to record, and when both exist they are usually the same string, in which case
    the roster's is the one with a name behind it.  Suggestions are deduplicated on
    ``(kind, value)`` for exactly that reason.
    """
    pattern = like_contains(q)
    out: list[dict[str, Any]] = []
    # How many radios a unit or a person currently holds — the roster's answer to "busiest
    # first", since neither table has a call counter of its own.
    unit_radios = (
        select(RadioAssignment.unit_id, func.count().label("radios"))
        .where(RadioAssignment.effective_to.is_(None), RadioAssignment.unit_id.isnot(None))
        .group_by(RadioAssignment.unit_id)
        .subquery()
    )
    for callsign, name, count in session.execute(
        select(Unit.callsign, Unit.name, func.coalesce(unit_radios.c.radios, 0))
        .outerjoin(unit_radios, unit_radios.c.unit_id == Unit.id)
        .where(
            or_(
                Unit.callsign.ilike(pattern, escape="\\"),
                Unit.name.ilike(pattern, escape="\\"),
            )
        )
        .order_by(func.coalesce(unit_radios.c.radios, 0).desc(), Unit.callsign)
        .limit(limit)
    ):
        out.append({"value": callsign, "kind": "unit", "count": int(count or 0), "label": name})
    person_radios = (
        select(RadioAssignment.person_id, func.count().label("radios"))
        .where(RadioAssignment.effective_to.is_(None), RadioAssignment.person_id.isnot(None))
        .group_by(RadioAssignment.person_id)
        .subquery()
    )
    for name, rank, count in session.execute(
        select(Person.name, Person.rank, func.coalesce(person_radios.c.radios, 0))
        .outerjoin(person_radios, person_radios.c.person_id == Person.id)
        .where(Person.name.ilike(pattern, escape="\\"))
        .order_by(func.coalesce(person_radios.c.radios, 0).desc(), Person.name)
        .limit(limit)
    ):
        out.append({"value": name, "kind": "person", "count": int(count or 0), "label": rank})
    for alias, count in session.execute(
        select(Talkgroup.alias, Talkgroup.call_count)
        .where(Talkgroup.alias.ilike(pattern, escape="\\"))
        .order_by(Talkgroup.call_count.desc())
        .limit(limit)
    ):
        out.append({"value": alias, "kind": "talkgroup", "count": int(count or 0)})
    for unit, count in session.execute(
        select(Radio.unit_id, Radio.call_count)
        .where(Radio.unit_id.isnot(None), Radio.unit_id.ilike(pattern, escape="\\"))
        .order_by(Radio.call_count.desc())
        .limit(limit)
    ):
        out.append({"value": unit, "kind": "unit", "count": int(count or 0)})
    for alias, count in session.execute(
        select(Radio.alias, Radio.call_count)
        .where(Radio.alias.isnot(None), Radio.alias.ilike(pattern, escape="\\"))
        .order_by(Radio.call_count.desc())
        .limit(limit)
    ):
        out.append({"value": alias, "kind": "radio", "count": int(count or 0)})
    # `unnest` is a set-returning function and cannot appear in HAVING, so the tags are
    # expanded in a subquery and filtered like an ordinary column.
    tags = select(func.unnest(Call.tags).label("tag")).subquery()
    for tag, count in session.execute(
        select(tags.c.tag, func.count())
        .where(tags.c.tag.ilike(pattern, escape="\\"))
        .group_by(tags.c.tag)
        .order_by(func.count().desc())
        .limit(limit)
    ):
        out.append({"value": tag, "kind": "tag", "count": int(count or 0)})
    # A roster callsign and the radio label derived from it are the same suggestion twice.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in out:
        key = (str(row["kind"]), str(row["value"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    if fuzzy and len(unique) < limit and len(q.strip()) >= FUZZY_SUGGEST_MIN_LENGTH:
        for row in _near_suggestions(session, q.strip(), limit):
            key = (str(row["kind"]), str(row["value"]))
            if key not in seen:
                seen.add(key)
                unique.append(row)
    return unique[:limit]


def _near_suggestions(session: Session, q: str, limit: int) -> list[dict[str, Any]]:
    """Aliases and callsigns *spelled like* ``q`` — ``Rivertn`` finds ``RIVERTON PD``.

    ``q <% column`` is pg_trgm's word similarity (≥ ``pg_trgm.word_similarity_threshold``,
    0.6 by default): how well ``q`` matches the best-matching stretch of the alias, which is
    what a half-remembered channel name is.  Each column has a trigram GIN index, so
    this is index probes, not scans.  Best match first across all four, then busiest.
    """
    needle = literal(q)
    found: list[tuple[float, dict[str, Any]]] = []
    sources: list[tuple[str, Any, Any, Any]] = [
        ("talkgroup", Talkgroup.alias, Talkgroup.call_count, None),
        ("radio", Radio.alias, Radio.call_count, None),
        ("unit", Unit.callsign, literal(0), Unit.name),
        ("person", Person.name, literal(0), Person.rank),
    ]
    for kind, column, count, label in sources:
        score = func.word_similarity(needle, column).label("score")
        columns = [column, count, score] + ([label] if label is not None else [])
        stmt = (
            select(*columns)
            .where(column.isnot(None), needle.op("<%")(column))
            .order_by(score.desc(), count.desc() if kind in ("talkgroup", "radio") else column)
            .limit(limit)
        )
        for row in session.execute(stmt):
            entry: dict[str, Any] = {
                "value": row[0],
                "kind": kind,
                "count": int(row[1] or 0),
                "fuzzy": True,
            }
            if label is not None:
                entry["label"] = row[3]
            found.append((float(row[2] or 0), entry))
    found.sort(key=lambda item: (-item[0], -item[1]["count"]))
    return [entry for _, entry in found]


def agencies(session: Session) -> list[dict[str, Any]]:
    """Every agency code with the talkgroups and radios mapped to it (§4.2)."""
    tg_rows = list(
        session.execute(
            select(
                Talkgroup.agency_code,
                func.array_agg(Talkgroup.tgid),
                func.array_agg(Talkgroup.id),
                func.coalesce(func.sum(Talkgroup.call_count), 0),
            )
            .where(Talkgroup.agency_code.isnot(None))
            .group_by(Talkgroup.agency_code)
        )
    )
    by_code = {code: (tgids, ids, int(calls)) for code, tgids, ids, calls in tg_rows}
    # The shared channels nobody owns, repeated on every agency.  It is not that each of
    # them owns the set — it is that a consumer asking about one agency is exactly the
    # caller who needs to know which channels are everybody's, and the alternative is for
    # every consumer to hardcode the tgid list in its own config.
    shared_tgids = sorted(
        t
        for t in session.scalars(select(Talkgroup.tgid).where(Talkgroup.shared.is_(True)))
        if t is not None
    )
    radio_counts = dict(
        session.execute(
            select(Radio.agency_code, func.count())
            .where(Radio.agency_code.isnot(None))
            .group_by(Radio.agency_code)
        ).all()
    )
    out = []
    for agency in session.scalars(select(Agency).order_by(Agency.kind, Agency.name)):
        tgids, ids, calls = by_code.get(agency.code, ([], [], 0))
        out.append(
            {
                "code": agency.code,
                "name": agency.name,
                "short_name": agency.short_name,
                "kind": agency.kind,
                "talkgroup_ids": sorted(t for t in tgids if t is not None),
                "talkgroups": sorted(i for i in ids if i is not None),
                "shared_talkgroup_ids": shared_tgids,
                "radio_count": int(radio_counts.get(agency.code, 0)),
                "call_count": calls,
                # Provenance and the active flag ride along on the public list so the
                # pickers can hide a disbanded agency without a second request; nothing
                # is filtered out here, because a consumer asking for the directory wants
                # the row a historical call still points at.
                "source": agency.source,
                "active": agency.active,
            }
        )
    return out


def spark_24h(session: Session, column: Any, value: Any) -> list[int]:
    """24 hourly call counts ending at the current hour, oldest first."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=23)
    # One expression object, used in both clauses: building it twice would emit two bind
    # parameters and PostgreSQL would no longer see the GROUP BY term as the same expression.
    bucket = func.date_trunc("hour", Call.started_at).label("bucket")
    rows = dict(
        session.execute(
            select(bucket, func.count())
            .where(column == value, Call.started_at >= start, Call.duplicate_of.is_(None))
            .group_by(bucket)
        ).all()
    )
    return [int(rows.get(start + timedelta(hours=i), 0)) for i in range(24)]


__all__ = [
    "CallPage",
    "agencies",
    "audio_url",
    "call_detail",
    "call_if_matches",
    "call_row",
    "conversation_out",
    "facets",
    "like_contains",
    "list_calls",
    "row_to_call",
    "search_calls",
    "spark_24h",
    "suggestions",
    "transcript_out",
]
