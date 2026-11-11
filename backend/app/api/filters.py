"""The one shared call-filter dependency (docs/ARCHITECTURE.md §4.1).

``GET /calls``, ``/calls/facets``, ``/calls/export``, ``/calls/playlist.m3u``,
``POST /exports``, ``GET /search``, the statistics endpoints and the live feed all accept
exactly the same filter vocabulary, so it is implemented once: :func:`call_filters` parses
the query string into an immutable :class:`CallFilters`, :func:`call_conditions` turns that
into SQLAlchemy predicates, and :func:`base_call_select` builds the denormalized SELECT
every list endpoint reads.

Two things are specific to this archive and worth reading twice:

* **Patch awareness.** ``talkgroup=1101`` matches ``calls.talkgroup_ids``, the array of
  *every* talkgroup involved, so a patch call that carried 1101 as one of its members is
  found too (§1.4).  The value is the number **on the air** (``tgid``) and nothing else.
  It used to accept this archive's ``talkgroups.id`` as well, on the grounds that the SPA
  had the primary key to hand — but the two number spaces overlap (production has 43
  talkgroups whose ``id`` is some other talkgroup's ``tgid``), so ``talkgroup=1`` meant
  *both* readings at once and quietly returned another channel's traffic.  Use ``system``
  to disambiguate a tgid two systems both use.
* **Local time.** ``time_from``/``time_to``/``dow`` work on ``calls.started_local`` (naive
  local time, the ``TZ`` setting), so "weekday afternoons" means what a human means by it, while
  ``date_from``/``date_to`` accept either a date or a full timestamp and are resolved
  against the tz-aware ``started_at``.
"""

from __future__ import annotations

import base64
import binascii
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query
from sqlalchemy import (
    ColumnElement,
    Integer,
    Select,
    String,
    Text,
    Time,
    and_,
    cast,
    exists,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import UnaryExpression

from app.auth import Principal, current_principal
from app.config import get_settings
from app.db import get_session
from app.ingest.flow import FILTER_CLASSES as PRIORITY_CLASSES
from app.ingest.flow import priority_class_clause
from app.models import Agency, Call, Conversation, Patch, Radio, Site, System, Talkgroup
from app.services.counts import estimated_rows
from app.services.fuzzy import FuzzyExpansion, expand

# --------------------------------------------------------------------------------------
# sorting
# --------------------------------------------------------------------------------------

#: What ``sort=`` accepts.  Prefix with ``-`` for descending (``sort=-duration``).
SORTABLE: dict[str, Any] = {
    "started_at": Call.started_at,
    "started_local": Call.started_local,
    "recorded_at": Call.recorded_at,
    "ingested_at": Call.ingested_at,
    "duration": Call.duration_s,
    "duration_s": Call.duration_s,
    "talkgroup": Call.talkgroup_id,
    "radio": Call.radio_id,
    "transcript_status": Call.transcript_status,
    "quality": Call.transcript_quality,
    "id": Call.id,
}

DEFAULT_SORT = "-started_at"

#: How many explicit ids one request may name.  A ticked selection is something a person
#: made by hand, so a thousand is already generous; past it the URL stops being a sane
#: transport (browsers give up somewhere around 2 kB of query string, and a thousand ids is
#: most of that) and the caller wants a filter, not a list.  Over the cap is a 422, not a
#: silent truncation: quietly exporting the first thousand of someone's selection is worse
#: than telling them.
MAX_FILTER_IDS = 1000

#: The ranges of the columns the integer filters are compared with.  PostgreSQL refuses a
#: bind that does not fit (``talkgroup=3000000000`` against ``integer[]`` is "integer out of
#: range"), which used to surface as a 500 on REST and as an exception on *every* event of a
#: live socket whose filter frame had been acknowledged.  Checked once, at parse, instead.
INT32 = (-(2**31), 2**31 - 1)
INT64 = (-(2**63), 2**63 - 1)

#: Which bound each integer filter is held to: ``talkgroup`` and ``patch`` meet ``integer``
#: columns (``calls.talkgroup_ids``, ``patches.id``/``patch_id``); the rest are ``bigint``.
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "ids": INT64,
    "talkgroup": INT32,
    "radio": INT64,
    "patch": INT32,
    "conversation": INT64,
}


def _check_int_range(name: str, values: list[int] | None) -> None:
    low, high = _INT_BOUNDS[name]
    bad = [v for v in (values or ()) if not low <= v <= high]
    if bad:
        raise HTTPException(
            status_code=422, detail=f"{name}: {bad[0]} is out of range ({low}..{high})"
        )


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().tz)


# --------------------------------------------------------------------------------------
# parsed filter set
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallFilters:
    """One parsed filter set.  Frozen so it can be reused across sub-queries and echoed."""

    q: str | None = None
    #: Widen ``q`` to the spellings the archive's vocabulary holds
    #: (app/services/fuzzy.py).  Off by default: an exact search means what it always has.
    fuzzy: bool = False
    #: The reader is a guest and some talkgroups are hidden: leave those calls out.  Set by
    #: the server (:func:`guest_aware_filters`, the live socket), never parsed from a query.
    guest_view: bool = False
    #: Explicit call ids, when the caller has picked rows rather than described them.
    #: Intersects with every other filter rather than replacing them, which is what makes
    #: "the ones I ticked" mean the ones I ticked *out of what I was looking at*.
    ids: tuple[int, ...] = ()
    date_from: datetime | None = None
    date_to: datetime | None = None
    time_from: time | None = None
    time_to: time | None = None
    dow: tuple[int, ...] = ()
    system: tuple[str, ...] = ()
    site: tuple[str, ...] = ()
    talkgroup: tuple[int, ...] = ()
    radio: tuple[int, ...] = ()
    unit: tuple[str, ...] = ()
    agency: tuple[str, ...] = ()
    category: tuple[str, ...] = ()
    call_type: tuple[str, ...] = ()
    #: ``group`` / ``unit`` — who the traffic was addressed to (``calls.kind``), which is a
    #: different question from ``call_type``.
    kind: tuple[str, ...] = ()
    emergency: bool | None = None
    encrypted: bool | None = None
    #: A sustained tone was found in the audio (§5.5); ``alert`` narrows that to a *page*.
    #: Both are the audio-processing pass's opinion and are independent of ``emergency``,
    #: which is the radio's.
    tone_detected: bool | None = None
    alert: bool | None = None
    has_audio: bool | None = None
    audio_type: tuple[str, ...] = ()
    min_duration: float | None = None
    max_duration: float | None = None
    transcript_status: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    priority: tuple[str, ...] = ()
    tag: tuple[str, ...] = ()
    starred: bool | None = None
    patch: tuple[int, ...] = ()
    conversation: tuple[int, ...] = ()
    include_duplicates: bool = False
    #: Widen ``agency`` to the channels nobody owns.  Only ``agency`` reads it; on its own
    #: it changes nothing, because "shared" is not a set anyone asked to see by itself.
    include_shared: bool = False
    sort: str = DEFAULT_SORT
    _extra: dict[str, Any] = field(default_factory=dict, compare=False)
    #: Talkgroup sets already looked up in the directory, by the filter field that produced
    #: them — see :func:`resolve_filter_sets`.  Private, ``compare=False`` and skipped by
    #: :meth:`echo`: it is a cached answer, never part of what the caller asked for.
    _resolved: dict[str, tuple[int, ...]] = field(default_factory=dict, compare=False)
    #: ``q`` widened to its spelling variants, once :func:`resolve_filter_sets` has looked
    #: them up — cached the same way, and for the same reason, as ``_resolved``.
    _fuzzy: FuzzyExpansion | None = field(default=None, compare=False)

    def echo(self) -> dict[str, Any]:
        """JSON-safe echo of what was applied — stored on an export job, returned by /links."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if key.startswith("_") or value in (None, (), []):
                continue
            if key == "fuzzy" and not value:
                continue  # off is the default and every echo predates it
            # Kept in the echo when on: the live render cache and the facet cache key on it,
            # and a guest must not be answered from a signed-in reader's entry.
            if key == "guest_view" and not value:
                continue
            if isinstance(value, tuple):
                out[key] = list(value)
            elif isinstance(value, datetime | date | time):
                out[key] = value.isoformat()
            else:
                out[key] = value
        return out

    def replace(self, **kwargs: Any) -> CallFilters:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        data.update(kwargs)
        return CallFilters(**data)


def _as_datetime(raw: str | None, *, end_of_day: bool) -> datetime | None:
    """Parse ``date_from``/``date_to``: a date, or a timestamp, naive = local (``TZ``).

    A bare date on ``date_to`` means "through the end of that local day", which is what a
    reader typing 2026-08-29 in both boxes expects.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            moment = datetime.combine(day, time.max if end_of_day else time.min)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{text!r} is not an ISO date or timestamp"
        ) from exc
    return moment.replace(tzinfo=_tz()) if moment.tzinfo is None else moment


def call_filters(  # noqa: PLR0913 - this *is* the public query contract
    q: Annotated[
        str | None,
        Query(
            description=(
                "Full-text search over the transcript, notes and the recorded talkgroup alias, "
                'using PostgreSQL websearch syntax: `"quoted phrase"`, `or`, `-negated`.'
            ),
            examples=["fire alarm"],
        ),
    ] = None,
    fuzzy: Annotated[
        bool,
        Query(
            description=(
                "Also match spelling variants of the words in `q` that the archive's "
                "transcripts contain (speech-to-text writes `Milrose` for Melrose). Each "
                "word of four or more letters, without digits and not negated, is widened "
                "to at most 8 vocabulary words within one edit (two for words over five "
                "letters). Default false: without it `q` matches exactly as it always has. "
                "No effect without `q`."
            )
        ),
    ] = False,
    ids: Annotated[
        list[int] | None,
        Query(
            description=(
                "Explicit `calls.id` values; repeat for OR. Narrows whatever the other filters "
                f"already match, so it means \"these, out of those\". At most {MAX_FILTER_IDS} "
                "per request (422 over) — past that, describe the set with filters instead."
            ),
            examples=[[41, 42]],
        ),
    ] = None,
    date_from: Annotated[
        str | None,
        Query(
            description="Inclusive start. A date (local day) or an ISO timestamp; naive = local time (TZ)."
        ),
    ] = None,
    date_to: Annotated[
        str | None, Query(description="Inclusive end. A bare date means the end of that local day.")
    ] = None,
    time_from: Annotated[
        time | None,
        Query(description="Start time of day (HH:MM) on the local clock; wraps midnight when after time_to"),
    ] = None,
    time_to: Annotated[time | None, Query(description="End time of day (HH:MM) on the local clock")] = None,
    dow: Annotated[
        list[int] | None, Query(description="Day of week on the local clock, 0 = Sunday; repeat for OR")
    ] = None,
    system: Annotated[list[str] | None, Query(description="System id or name; repeat for OR")] = None,
    site: Annotated[list[str] | None, Query(description="Site id or name; repeat for OR")] = None,
    talkgroup: Annotated[
        list[int] | None,
        Query(
            description=(
                "Talkgroup, as the **on-air `tgid`** — not this archive's `talkgroups.id`. "
                "Matches `calls.talkgroup_ids`, so patched traffic carrying the talkgroup is "
                "included. Add `system` to disambiguate a tgid two systems both use."
            ),
            examples=[[1101]],
        ),
    ] = None,
    radio: Annotated[
        list[int] | None, Query(description="Radio, as `radios.id` or the on-air `radio_id`")
    ] = None,
    unit: Annotated[
        list[str] | None, Query(description="Apparatus id on the radio (`radios.unit_id`)", examples=[["E1"]])
    ] = None,
    agency: Annotated[
        list[str] | None,
        Query(
            description="Agency code, short name or name; matched through `talkgroups.agency_code`",
            examples=[["EXAMPLEPD"]],
        ),
    ] = None,
    category: Annotated[
        list[str] | None, Query(description="Talkgroup category (law/fire/ems/pw/transit/interop/…)")
    ] = None,
    call_type: Annotated[
        list[str] | None, Query(description="`group`, `patch` or `individual`")
    ] = None,
    kind: Annotated[
        list[str] | None,
        Query(
            description=(
                "`group` for traffic on a talkgroup (the default for everything the archive "
                "held before private calls were recordable) or `unit` for a P25 "
                "unit-to-unit **private call** — one radio to one radio, on no talkgroup. "
                "Repeat for OR. Not the same axis as `call_type`, which says how the "
                "channel was granted."
            ),
            examples=[["unit"]],
        ),
    ] = None,
    emergency: Annotated[
        bool | None,
        Query(
            description=(
                "Only emergency calls (true) or only non-emergency (false). The flag comes "
                "from a trunk-recorder sidecar or a push; an sdrtrunk file cannot carry one, "
                "so those calls match neither value."
            )
        ),
    ] = None,
    encrypted: Annotated[
        bool | None,
        Query(
            description=(
                "Only encrypted calls (true) or only clear (false). An encrypted call is "
                "kept for its metadata and has no audio (§1.7)."
            )
        ),
    ] = None,
    tone_detected: Annotated[
        bool | None,
        Query(
            description=(
                "Only calls the audio-processing pass found a sustained tone in (true), or only "
                "those it did not (false). A call that has not been processed yet reads false "
                "(§5.5)."
            )
        ),
    ] = None,
    alert: Annotated[
        bool | None,
        Query(
            description=(
                "Only pages (true) — a two-tone or alert pattern in the first seconds of the "
                "call. Not the same as `emergency`, which is the flag the radio asserted."
            )
        ),
    ] = None,
    has_audio: Annotated[
        bool | None,
        Query(
            description=(
                "Only calls whose recording is on hand (true → `audio_state='present'`) or only the "
                "metadata-only rows (false → archived or purged, which is how an encrypted call is "
                "kept). Omit for both (§1.7, §5.3)."
            )
        ),
    ] = None,
    audio_type: Annotated[
        list[str] | None,
        Query(description="`analog`, `digital` or `digital tdma`; repeat for OR", examples=[["digital"]]),
    ] = None,
    min_duration: Annotated[float | None, Query(ge=0, description="Minimum duration in seconds")] = None,
    max_duration: Annotated[float | None, Query(ge=0, description="Maximum duration in seconds")] = None,
    transcript_status: Annotated[
        list[str] | None, Query(description="pending/queued/running/done/skipped/failed/empty")
    ] = None,
    quality: Annotated[list[str] | None, Query(description="good/suspect/noise")] = None,
    priority: Annotated[
        list[str] | None,
        Query(
            description=(
                "How the call reached the archive: `live` (recorded as it happened), `backlog` "
                "or `bulk` (pushed by an archive migration long afterwards). Repeat for OR. "
                "Reads `calls.ingest_priority`, the admission class the push path recorded; "
                "`urgent` reads back as `live` and a bare integer is judged against the same "
                "band edges the transcription queue uses. Calls the file watcher picked up off "
                "disk carry no class and match none of the three."
            ),
            examples=[["live"]],
        ),
    ] = None,
    tag: Annotated[list[str] | None, Query(description="User tag; repeat for OR")] = None,
    starred: Annotated[
        bool | None, Query(description="Only starred (true) or only unstarred (false)")
    ] = None,
    patch: Annotated[
        list[int] | None, Query(description="Patch, as `patches.id` or the on-air patch number")
    ] = None,
    conversation: Annotated[list[int] | None, Query(description="Conversation id")] = None,
    include_duplicates: Annotated[
        bool,
        Query(description="Include calls the dedupe marked as duplicates of another (default false)"),
    ] = False,
    include_shared: Annotated[
        bool,
        Query(
            description=(
                "Let `agency` also match talkgroups flagged `shared` — the county dispatch "
                "and interop channels no single department owns. Default false, because a "
                "shared channel is not that agency's traffic and counting it as such would "
                "silently change what every existing `?agency=` query means. Has no effect "
                "without `agency`."
            )
        ),
    ] = False,
    sort: Annotated[
        str,
        Query(
            description=(
                "Sort key, `-` prefixed for descending. One of: "
                + ", ".join(sorted(SORTABLE))
                + ". Default `-started_at`."
            )
        ),
    ] = DEFAULT_SORT,
) -> CallFilters:
    key = sort[1:] if sort.startswith("-") else sort
    if key not in SORTABLE:
        raise HTTPException(status_code=422, detail=f"sort: unknown key {key!r}")
    bad_dow = [d for d in (dow or ()) if d < 0 or d > 6]
    if bad_dow:
        raise HTTPException(status_code=422, detail="dow: expected 0..6 (0 = Sunday)")
    bad_priority = sorted({p.strip().lower() for p in (priority or ())} - set(PRIORITY_CLASSES))
    if bad_priority:
        raise HTTPException(
            status_code=422,
            detail=f"priority: expected {', '.join(PRIORITY_CLASSES)}, got {', '.join(bad_priority)}",
        )
    if ids and len(ids) > MAX_FILTER_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"ids: at most {MAX_FILTER_IDS} per request, got {len(ids)}",
        )
    for name, values in (
        ("ids", ids),
        ("talkgroup", talkgroup),
        ("radio", radio),
        ("patch", patch),
        ("conversation", conversation),
    ):
        _check_int_range(name, values)
    for name, bound in (("min_duration", min_duration), ("max_duration", max_duration)):
        # ``Query(ge=0)`` covers REST; a mapping (the live socket, a stored feed) reaches
        # here without FastAPI's validation, so the same rule is applied again.
        if bound is not None and not (bound >= 0 and math.isfinite(bound)):
            raise HTTPException(status_code=422, detail=f"{name}: expected a number >= 0")
    return CallFilters(
        q=(q or "").strip() or None,
        fuzzy=bool(fuzzy),
        ids=tuple(dict.fromkeys(ids or ())),
        date_from=_as_datetime(date_from, end_of_day=False),
        date_to=_as_datetime(date_to, end_of_day=True),
        time_from=time_from,
        time_to=time_to,
        dow=tuple(dow or ()),
        system=tuple(system or ()),
        site=tuple(site or ()),
        talkgroup=tuple(talkgroup or ()),
        radio=tuple(radio or ()),
        unit=tuple(unit or ()),
        agency=tuple(agency or ()),
        category=tuple(category or ()),
        call_type=tuple(call_type or ()),
        kind=tuple(kind or ()),
        emergency=emergency,
        encrypted=encrypted,
        tone_detected=tone_detected,
        alert=alert,
        has_audio=has_audio,
        audio_type=tuple(audio_type or ()),
        min_duration=min_duration,
        max_duration=max_duration,
        transcript_status=tuple(transcript_status or ()),
        quality=tuple(quality or ()),
        priority=tuple(dict.fromkeys(p.strip().lower() for p in (priority or ()))),
        tag=tuple(tag or ()),
        starred=starred,
        patch=tuple(patch or ()),
        conversation=tuple(conversation or ()),
        include_duplicates=include_duplicates,
        include_shared=include_shared,
        sort=sort,
    )


def any_hidden_talkgroups(session: Session) -> bool:
    return bool(session.scalar(select(exists().where(Talkgroup.is_hidden.is_(True)))))


def guest_aware_filters(
    f: Annotated[CallFilters, Depends(call_filters)],
    principal: Annotated[Principal | None, Depends(current_principal)],
    session: Annotated[Session, Depends(get_session)],
) -> CallFilters:
    """The parsed filters, restricted for a guest when any talkgroup is hidden.

    With nothing hidden the filter set is left exactly as parsed, so a guest's first page
    keeps the unfiltered fast path (:func:`capped_count`).
    """
    if principal is None and any_hidden_talkgroups(session):
        return f.replace(guest_view=True)
    return f


Filters = Annotated[CallFilters, Depends(guest_aware_filters)]


#: What a mapping may spell a boolean as: JSON's own, and the strings a query string or a
#: hand-written frame carries.  Anything else is a 422, not a truthy guess —
#: ``bool("false")`` is ``True``, which is how ``include_duplicates: "false"`` used to
#: include them.
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def filters_from_mapping(params: dict[str, Any]) -> CallFilters:
    """Build a filter set from a plain dict — the WebSocket filter frame and export jobs.

    Unknown keys are ignored so the live feed can be handed the SPA's whole filter object.
    Every value is coerced to the type the field needs *here*, and a value that will not
    coerce is an ``HTTPException(422)`` naming the field: on REST that is the response, and
    on the live socket it is an ``error`` frame.  What must never happen is a filter that
    parses into the wrong type and then throws on every event it is applied to — a string
    compared with a boolean column, a number too large for its column.
    """

    def bad(name: str, expected: str, value: Any) -> HTTPException:
        return HTTPException(status_code=422, detail=f"{name}: expected {expected}, got {value!r}")

    def as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        return list(value) if isinstance(value, list | tuple) else [value]

    def ints(name: str) -> list[int] | None:
        out: list[int] = []
        for value in as_list(params.get(name)):
            if isinstance(value, bool):
                raise bad(name, "an integer", value)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            try:
                out.append(value if isinstance(value, int) else int(str(value).strip()))
            except ValueError:
                raise bad(name, "an integer", value) from None
        return out or None

    def strs(name: str) -> list[str] | None:
        return [str(v) for v in as_list(params.get(name))] or None

    def text(name: str) -> str | None:
        value = params.get(name)
        if value is None:
            return None
        if isinstance(value, dict | list | tuple):
            raise bad(name, "a string", value)
        return str(value)

    def flag(name: str, default: bool | None = None) -> bool | None:
        value = params.get(name)
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
        raise bad(name, "true or false", value)

    def number(name: str) -> float | None:
        value = params.get(name)
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise bad(name, "a number", value)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise bad(name, "a number", value) from None

    def as_time(name: str) -> time | None:
        value = params.get(name)
        if not value:
            return None
        if isinstance(value, time):
            return value
        try:
            return time.fromisoformat(str(value))
        except ValueError:
            raise bad(name, "a time of day (HH:MM)", value) from None

    return call_filters(
        q=text("q"),
        fuzzy=bool(flag("fuzzy", False)),
        ids=ints("ids"),
        date_from=text("date_from"),
        date_to=text("date_to"),
        time_from=as_time("time_from"),
        time_to=as_time("time_to"),
        dow=ints("dow"),
        system=strs("system"),
        site=strs("site"),
        talkgroup=ints("talkgroup"),
        radio=ints("radio"),
        unit=strs("unit"),
        agency=strs("agency"),
        category=strs("category"),
        call_type=strs("call_type"),
        kind=strs("kind"),
        emergency=flag("emergency"),
        encrypted=flag("encrypted"),
        tone_detected=flag("tone_detected"),
        alert=flag("alert"),
        has_audio=flag("has_audio"),
        audio_type=strs("audio_type"),
        min_duration=number("min_duration"),
        max_duration=number("max_duration"),
        transcript_status=strs("transcript_status"),
        quality=strs("quality"),
        priority=strs("priority"),
        tag=strs("tag"),
        starred=flag("starred"),
        patch=ints("patch"),
        conversation=ints("conversation"),
        include_duplicates=bool(flag("include_duplicates", False)),
        include_shared=bool(flag("include_shared", False)),
        sort=text("sort") or DEFAULT_SORT,
    )


# --------------------------------------------------------------------------------------
# conditions
# --------------------------------------------------------------------------------------

_INT_ARRAY = ARRAY(Integer)


def tsquery(q: str) -> ColumnElement[Any]:
    """``websearch_to_tsquery('english', q)`` — the one place the FTS config is named.

    The configuration name is emitted as a literal rather than a bind parameter because
    PostgreSQL wants a ``regconfig`` there, and a parameter arrives as ``varchar``.
    """
    return func.websearch_to_tsquery(literal_column("'english'"), q)


def search_query(f: CallFilters) -> ColumnElement[Any]:
    """The ``tsquery`` ``f.q`` stands for: widened when ``fuzzy`` found variants, else exact.

    The widened form is ``tsquery`` text built from lexemes that are already normalised, so
    it is *cast*, not passed through ``to_tsquery`` — which would stem the stems again.
    """
    assert f.q is not None
    if f.fuzzy and f._fuzzy is not None and f._fuzzy.tsquery is not None:
        return cast(literal(f._fuzzy.tsquery), TSQUERY)
    return tsquery(f.q)


def _agency_codes(values: tuple[str, ...]) -> Select[Any]:
    """Resolve agency codes, short names and names to codes (the seeded agency list's vocabulary)."""
    wanted = [v.strip().upper() for v in values if v.strip()]
    return select(Agency.code).where(
        or_(
            func.upper(Agency.code).in_(wanted),
            func.upper(Agency.short_name).in_(wanted),
            func.upper(Agency.name).in_(wanted),
        )
    )


def _tgids_where(condition: ColumnElement[bool]) -> Any:
    """Scalar subquery: the tgids of every talkgroup matching ``condition``."""
    return (
        select(func.coalesce(func.array_agg(Talkgroup.tgid), func.cast(literal("{}"), _INT_ARRAY)))
        .where(condition)
        .scalar_subquery()
    )


def _on_hidden_talkgroup() -> ColumnElement[bool]:
    """True for a call on a hidden talkgroup, or a patch with a hidden member.

    Never NULL: a call with no talkgroup must count as not hidden, and NOT NULL would drop it.
    """
    hidden = Talkgroup.is_hidden.is_(True)
    return func.coalesce(
        or_(
            Call.talkgroup_id.in_(select(Talkgroup.id).where(hidden)),
            Call.talkgroup_ids.op("&&")(_tgids_where(hidden)),
        ),
        false(),
    )


def call_hidden_from_guests(session: Session, call_id: int) -> bool:
    return bool(
        session.scalar(select(exists().where(Call.id == call_id, _on_hidden_talkgroup())))
    )


def _tgid_set_conditions(f: CallFilters) -> dict[str, ColumnElement[bool]]:
    """The ``talkgroups`` predicate behind each filter that means "a set of talkgroups".

    Two filters are really directory lookups — ``agency`` and ``category`` — and both end
    as ``calls.talkgroup_ids && <set>``.  Naming the predicates in one place is what lets
    :func:`resolve_filter_sets` run them ahead of time and :func:`call_conditions` fall
    back to the subquery form unchanged.  ``talkgroup`` is *not* here: its values are
    already tgids, so it needs no lookup at all (it used to resolve ``talkgroups.id`` as
    well, which is the bug this parameter no longer has).
    """
    out: dict[str, ColumnElement[bool]] = {}
    if f.agency:
        # Agency → talkgroups → tgids, so a patch call on one of the agency's channels matches.
        owned = Talkgroup.agency_code.in_(_agency_codes(f.agency))
        # ``include_shared`` widens that to the channels nobody owns: on this system the
        # dispatch and interop channels carry no ``agency_code``, so an agency's own
        # channels are exactly the traffic that is *not* on the channel it was dispatched on.
        out["agency"] = or_(owned, Talkgroup.shared.is_(True)) if f.include_shared else owned
    if f.category:
        wanted = [c.strip().lower() for c in f.category if c.strip()]
        out["category"] = func.lower(Talkgroup.category).in_(wanted)
    return out


def resolve_filter_sets(session: Any, f: CallFilters) -> CallFilters:
    """Look the talkgroup sets up **now** and carry them on the filter set as literals.

    ``?category=law`` reads as ``calls.talkgroup_ids && (SELECT array_agg(tgid) FROM
    talkgroups WHERE lower(category) = 'law')``.  That subquery is an InitPlan: it runs once
    and it is correct, but the planner cannot see how *big* the array is at plan time, so it
    falls back to a fixed guess — about 1.5 % of the table — for the selectivity of the
    ``&&``.  When the set is small the guess is roughly right and the GIN index on
    ``talkgroup_ids`` is the right answer.  When it is not, the guess is catastrophic: the
    Calls page with seven categories ticked resolves to 1 555 of 2 447 talkgroups, the
    planner still expects 283 rows, and it BitmapAnds a GIN scan that reads **2.9 million**
    index entries — the whole archive — against the date range it should have led with.
    Measured on production, one facet query out of the five the sidebar runs: 13.8 s.

    With the array resolved here and emitted as a literal, the planner can use
    ``most_common_elems`` on ``talkgroup_ids``, estimates 13 860 rows against an actual
    11 913, leads with ``started_at`` and filters — the same query, 123 ms.

    The lookups are two small indexed reads against the directory (2 ms for a sequential
    scan of 2 447 talkgroups), so this is worth doing on any path that then reads ``calls``.
    It is idempotent and safe to call twice; a filter set that names none of the three
    fields is returned unchanged.  :meth:`CallFilters.replace` deliberately **drops** what
    was resolved, because changing ``category`` changes the answer.

    Why the set is not simply dropped when it covers nearly every talkgroup: it is not a
    no-op even at 100 % coverage.  ``talkgroup_ids && anything`` is false for a row whose
    array is empty, and production has 588 such calls (private calls, and rows whose
    talkgroup was never resolved).  Dropping the predicate would silently add them to the
    answer, so the literal array is passed however large it is — 1 555 integers is 12 kB of
    SQL text and 0.1 ms of extra planning against the 13.7 s it saves.
    """
    if f.fuzzy and f.q and f._fuzzy is None:
        # ``fuzzy`` is looked up here for the same reason: one read of the vocabulary, and
        # every hot path that already resolves gets the widened query for free.
        f = replace(f, _fuzzy=expand(session, f.q))
    conditions = _tgid_set_conditions(f)
    missing = {name: cond for name, cond in conditions.items() if name not in f._resolved}
    # ``talkgroup`` needs no query — the values *are* tgids — but it is still resolved here
    # so the literal-array form of ``&&`` is what every hot path emits for it too.
    wants_talkgroup = bool(f.talkgroup) and "talkgroup" not in f._resolved
    if not missing and not wants_talkgroup:
        return f
    resolved = dict(f._resolved)
    if wants_talkgroup:
        resolved["talkgroup"] = tuple(sorted({int(t) for t in f.talkgroup}))
    for name, cond in missing.items():
        tgids = {int(t) for t in session.scalars(select(Talkgroup.tgid).where(cond)) if t is not None}
        resolved[name] = tuple(sorted(tgids))
    return replace(f, _resolved=resolved)


def _tgid_overlap(f: CallFilters, name: str, fallback: ColumnElement[bool]) -> ColumnElement[bool]:
    """``calls.talkgroup_ids && <set>``, as a literal array when the set has been resolved."""
    if name not in f._resolved:
        return fallback
    tgids = f._resolved[name]
    if not tgids:
        # The old form emitted ``talkgroup_ids && '{}'``, which no row satisfies.  Saying so
        # outright lets the planner stop rather than scan for an answer that cannot exist.
        return literal(False)
    # Rendered into the SQL text, not sent as a bind parameter, and that is the whole point
    # of resolving the set: the planner's estimate for ``&&`` comes from ``most_common_elems``
    # on ``talkgroup_ids``, which it can only consult if it can see the array while planning.
    # A parameter gets a generic plan's fixed guess — the 1.5 % that cost 13.8 s — as soon as
    # PostgreSQL decides to cache the plan.  The values are integers this function produced,
    # so the interpolation is not a hole.
    array_sql = "'{" + ",".join(str(int(t)) for t in tgids) + "}'"
    return Call.talkgroup_ids.op("&&")(func.cast(literal_column(array_sql), _INT_ARRAY))


def _int_or_name(values: tuple[str, ...]) -> tuple[list[int], list[str]]:
    ids, names = [], []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        # ``systems.id``/``sites.id`` are ``integer``: a longer run of digits cannot be an
        # id, and binding it would be "integer out of range" rather than "no such system".
        if text.isdigit() and int(text) <= INT32[1]:
            ids.append(int(text))
        else:
            names.append(text.upper())
    return ids, names


def call_conditions(f: CallFilters) -> list[ColumnElement[bool]]:
    """Translate a parsed filter set into predicates over ``calls`` and its joins.

    ``talkgroup``/``agency``/``category`` are emitted as a literal array when the caller has
    been through :func:`resolve_filter_sets` and as the correlated subquery otherwise, so
    every existing caller keeps working and the hot ones get a plan the planner can cost.
    """
    conds: list[ColumnElement[bool]] = []
    if f.guest_view:
        conds.append(~_on_hidden_talkgroup())
    tgid_sets = _tgid_set_conditions(f)

    if f.q:
        conds.append(Call.search_tsv.op("@@")(search_query(f)))
    if f.ids:
        conds.append(Call.id.in_(list(f.ids)))
    if f.date_from is not None:
        conds.append(Call.started_at >= f.date_from)
    if f.date_to is not None:
        conds.append(Call.started_at <= f.date_to)

    # time-of-day filters work on the naive local clock, which is what a human means
    local_time = cast(Call.started_local, Time)
    if f.time_from is not None and f.time_to is not None:
        if f.time_from <= f.time_to:
            conds.append(and_(local_time >= f.time_from, local_time <= f.time_to))
        else:  # a window that wraps midnight, e.g. 22:00 → 02:00
            conds.append(or_(local_time >= f.time_from, local_time <= f.time_to))
    elif f.time_from is not None:
        conds.append(local_time >= f.time_from)
    elif f.time_to is not None:
        conds.append(local_time <= f.time_to)

    if f.dow:
        conds.append(func.extract("dow", Call.started_local).in_([int(d) for d in f.dow]))

    if f.system:
        ids, names = _int_or_name(f.system)
        clauses = []
        if ids:
            clauses.append(Call.system_id.in_(ids))
        if names:
            clauses.append(Call.system_id.in_(select(System.id).where(func.upper(System.name).in_(names))))
        conds.append(or_(*clauses) if clauses else literal(False))
    if f.site:
        ids, names = _int_or_name(f.site)
        clauses = []
        if ids:
            clauses.append(Call.site_id.in_(ids))
        if names:
            clauses.append(Call.site_id.in_(select(Site.id).where(func.upper(Site.name).in_(names))))
        conds.append(or_(*clauses) if clauses else literal(False))

    if f.talkgroup:
        wanted = [int(t) for t in f.talkgroup]
        # The value is an on-air tgid carried by the call, patch members included.  Nothing
        # is looked up in the directory: a ``talkgroups.id`` reading used to be OR'd in here
        # and it silently matched a *different* channel whose tgid happened to equal the pk.
        conds.append(
            _tgid_overlap(
                f,
                "talkgroup",
                Call.talkgroup_ids.op("&&")(func.cast(literal(wanted), _INT_ARRAY)),
            )
        )
    if f.radio:
        wanted = [int(r) for r in f.radio]
        # Both ends.  "Calls involving this radio" has meant "calls it keyed up on" for as
        # long as a call had one radio; a private call has two, and the radio at the far end
        # took part in it just as much — it is on its detail page and in its timeline, and a
        # filter that showed only the half it started would be lying by omission.
        matching = select(Radio.id).where(or_(Radio.id.in_(wanted), Radio.radio_id.in_(wanted)))
        conds.append(
            or_(Call.radio_id.in_(matching), Call.target_radio_id.in_(matching))
        )
    if f.unit:
        wanted = [u.strip().upper() for u in f.unit if u.strip()]
        conds.append(Call.radio_id.in_(select(Radio.id).where(func.upper(Radio.unit_id).in_(wanted))))
    if f.agency:
        # The agency → talkgroups → tgids lookup itself lives in ``_tgid_set_conditions``.
        conds.append(
            _tgid_overlap(
                f, "agency", Call.talkgroup_ids.op("&&")(_tgids_where(tgid_sets["agency"]))
            )
        )
    if f.category:
        conds.append(
            _tgid_overlap(
                f, "category", Call.talkgroup_ids.op("&&")(_tgids_where(tgid_sets["category"]))
            )
        )
    if f.call_type:
        conds.append(Call.call_type.in_([c.strip().lower() for c in f.call_type]))
    if f.kind:
        conds.append(Call.kind.in_([k.strip().lower() for k in f.kind]))
    # ``is_`` rather than ``==``: both columns are nullable, and "not an emergency" must
    # not quietly include the calls whose recorder never said either way.
    if f.emergency is not None:
        conds.append(Call.emergency.is_(f.emergency))
    if f.encrypted is not None:
        conds.append(Call.encrypted.is_(f.encrypted))
    # NOT NULL columns, so a plain equality is right here — unlike `emergency`, there is no
    # "the recorder never said" state to preserve: a call that has not been processed yet
    # reads false, which is the honest answer to "do we know of a tone in this".
    if f.tone_detected is not None:
        conds.append(Call.tone_detected == f.tone_detected)
    if f.alert is not None:
        conds.append(Call.alert == f.alert)
    # ``present`` is the only state whose recording can actually be served; ``archived`` and
    # ``purged`` are metadata-only rows (an encrypted call, or one aged past retention).
    if f.has_audio is not None:
        conds.append(
            Call.audio_state == "present" if f.has_audio else Call.audio_state != "present"
        )
    if f.audio_type:
        conds.append(func.lower(Call.audio_type).in_([a.strip().lower() for a in f.audio_type]))
    if f.min_duration is not None:
        conds.append(Call.duration_s >= f.min_duration)
    if f.max_duration is not None:
        conds.append(Call.duration_s <= f.max_duration)
    if f.transcript_status:
        conds.append(Call.transcript_status.in_([s.strip().lower() for s in f.transcript_status]))
    if f.quality:
        conds.append(Call.transcript_quality.in_([s.strip().lower() for s in f.quality]))
    if f.priority:
        # OR across the requested classes; each one is the band test the queue itself uses
        # (app/ingest/flow.py), so "live" here means exactly what "live" meant on the way in.
        conds.append(or_(*(priority_class_clause(p) for p in f.priority)))
    if f.tag:
        # ``text[] && varchar[]`` has no operator, so the literal is cast to text[].
        conds.append(Call.tags.op("&&")(func.cast(literal(list(f.tag)), ARRAY(Text))))
    if f.starred is not None:
        conds.append(Call.starred.is_(f.starred))
    if f.patch:
        wanted = [int(p) for p in f.patch]
        conds.append(
            Call.patch_id.in_(select(Patch.id).where(or_(Patch.id.in_(wanted), Patch.patch_id.in_(wanted))))
        )
    if f.conversation:
        conds.append(Call.conversation_id.in_([int(c) for c in f.conversation]))
    if not f.include_duplicates:
        conds.append(Call.duplicate_of.is_(None))
    return conds


# --------------------------------------------------------------------------------------
# selects
# --------------------------------------------------------------------------------------

#: The ``radios`` row at the *far* end of a unit call, aliased because ``Call`` joins the
#: same table twice — once for the radio that keyed up, once for the one it called.
TargetRadio = aliased(Radio, name="target_radio")

#: Alias joins are LEFT because a call may have no talkgroup (never seen), no radio (the
#: 0.6 % where sdrtrunk never resolved FROM), no site, no patch and — unless it is a
#: private call — no far end.
CALL_COLUMNS: list[Any] = [
    Call.id,
    Call.system_id,
    System.name.label("system_name"),
    Call.site_id,
    Site.name.label("site_name"),
    Call.talkgroup_id,
    Talkgroup.alias.label("talkgroup_alias"),
    Call.talkgroup_alias_at_record,
    Talkgroup.tgid.label("tgid"),
    Call.talkgroup_ids,
    Talkgroup.category.label("category"),
    # The Talkgroup join is already here for the alias, so RR's service tag is one more
    # selected column and no extra query work: the calls list is the hot path and must
    # not grow a join for a label.
    Talkgroup.service_tag.label("service_tag"),
    Talkgroup.agency_code.label("agency_code"),
    Talkgroup.color.label("color"),
    Call.patch_id,
    ("PATCH " + func.cast(Patch.patch_id, String)).label("patch_label"),
    func.coalesce(Patch.member_tgids, func.cast(literal("{}"), _INT_ARRAY)).label("patch_member_tgids"),
    Call.kind,
    Call.target_radio_id,
    # The far end of a private call, labeled exactly as the near end is.  One more LEFT JOIN
    # on the hot list path, but it is on a primary key and NULL for every group call, so it
    # costs a lookup only for the rows that have one — and without it the calls table cannot
    # render a unit call at all: there is no talkgroup to put in its place.
    func.coalesce(
        TargetRadio.unit_id, TargetRadio.alias, func.cast(TargetRadio.radio_id, String)
    ).label("target_radio_label"),
    Call.radio_id,
    func.coalesce(
        Radio.unit_id, Radio.alias, func.cast(Radio.radio_id, String), Call.radio_raw
    ).label("radio_label"),
    Call.radio_raw,
    Radio.unit_id.label("unit_id"),
    # The on-air numbers themselves, beside the labels: an export that hands a row to
    # somebody's spreadsheet needs the number the radio keyed up as, not this archive's
    # row id for it.
    Radio.radio_id.label("radio_number"),
    TargetRadio.radio_id.label("target_radio_number"),
    Call.talker_alias,
    Call.radio_resolved,
    Call.ingest_source,
    # The admission class the push path recorded.  Selected on the hot list path because
    # it is the one column that says whether a row is today's traffic or a decade of
    # archive that arrived last Tuesday — which the table has to be able to mark, and
    # which an outside consumer has to be able to see on every call it reads.
    Call.ingest_priority,
    Call.recorded_at,
    Call.started_at,
    Call.started_local,
    Call.ended_at,
    Call.time_source,
    Call.duration_s,
    Call.frequency_hz,
    Call.channel,
    Call.decoder,
    Call.call_type,
    Call.emergency,
    Call.encrypted,
    Call.audio_type,
    Call.audio_state,
    Call.tone_detected,
    Call.alert,
    Call.processed_path,
    Call.audio_meta,
    Call.duplicate_of,
    Call.conversation_id,
    Call.transcript_status,
    Call.transcript_text,
    Call.transcript_quality,
    Call.notes,
    Call.tags,
    Call.starred,
    Call.ingested_at,
    Call.updated_at,
]


def with_call_joins(stmt: Select[Any]) -> Select[Any]:
    """LEFT JOIN the directory tables every denormalized call row reads."""
    return (
        stmt.outerjoin(System, Call.system_id == System.id)
        .outerjoin(Site, Call.site_id == Site.id)
        .outerjoin(Talkgroup, Call.talkgroup_id == Talkgroup.id)
        .outerjoin(Radio, Call.radio_id == Radio.id)
        .outerjoin(TargetRadio, Call.target_radio_id == TargetRadio.id)
        .outerjoin(Patch, Call.patch_id == Patch.id)
    )


def apply_call_filters(stmt: Select[Any], f: CallFilters) -> Select[Any]:
    conds = call_conditions(f)
    return stmt.where(*conds) if conds else stmt


def base_call_select(f: CallFilters, *columns: Any) -> Select[Any]:
    """A filtered, joined SELECT over calls; defaults to the denormalized row."""
    stmt = select(*(columns or CALL_COLUMNS)).select_from(Call)
    return apply_call_filters(with_call_joins(stmt), f)


def count_call_select(f: CallFilters) -> Select[Any]:
    """``SELECT count(*)`` over the same filtered set, with the joins the filters need."""
    return apply_call_filters(with_call_joins(select(func.count()).select_from(Call)), f)


#: The largest total a list endpoint will count to.  An unfiltered ``count(*)`` over a
#: growing archive is a full scan on every page of every table, and nobody reads the exact
#: number past a few thousand: past this the answer is "at least 50,000".
COUNT_CAP = 50_000

#: The filter set that means "no filter at all" — the request every tab makes on load, and
#: the one the archiver clients make once an hour.  ``sort`` is normalised away because it
#: changes the order of the rows and not which rows they are.
_UNFILTERED = CallFilters()


def _is_unfiltered(f: CallFilters) -> bool:
    return replace(f, sort=DEFAULT_SORT) == _UNFILTERED


def capped_count(session: Any, f: CallFilters, cap: int = COUNT_CAP) -> tuple[int, bool]:
    """``(total, capped)`` — counts at most ``cap + 1`` rows, so the query stops early.

    The default (unfiltered) list is answered from ``pg_class.reltuples`` instead: on an
    archive with comfortably more than ``2 × cap`` rows in it, the answer is ``(cap, True)``
    whatever the duplicates do, and the catalog lookup knows that without reading fifty
    thousand heap tuples (13 000 buffers, ~48 ms) on every unfiltered page.  Any actual
    filter — and any archive small enough for the margin to matter — still counts for real.
    """
    if _is_unfiltered(f) and estimated_rows(session, Call) >= cap * 2:
        return (cap, True)
    inner = apply_call_filters(
        with_call_joins(select(Call.id).select_from(Call)), f
    ).limit(cap + 1).subquery()
    counted = int(session.scalar(select(func.count()).select_from(inner)) or 0)
    return (cap, True) if counted > cap else (counted, False)


def aggregate_select(f: CallFilters, *columns: Any) -> Select[Any]:
    return apply_call_filters(with_call_joins(select(*columns).select_from(Call)), f)


def order_by(f: CallFilters) -> list[UnaryExpression[Any]]:
    """Stable ORDER BY: the requested key, then ``calls.id`` so paging cannot repeat a row.

    ``NULLS LAST`` is applied **only to a nullable column**, and that is a performance fix,
    not a tidy-up.  PostgreSQL's ``DESC`` btree index is ``DESC NULLS FIRST``; asking for
    ``DESC NULLS LAST`` is a different ordering as far as the planner is concerned, so it
    cannot use ``ix_calls_started_at`` and falls back to scanning and sorting the whole
    table.  On production that was the difference between 1.4 ms and 2.9 s for the default
    ``GET /api/calls`` — a parallel sequential scan over three million rows, six hash joins
    and a top-N sort, to return fifty rows.  ``calls.started_at`` is ``NOT NULL``, so the
    clause was never doing anything except hiding the index.

    With the clause gone the plan is an index scan on the sort column feeding an incremental
    sort for the ``calls.id`` tiebreak (``Presorted Key: started_at``), then nested-loop
    lookups for the fifty rows' directory joins.  Nullable sort keys (``talkgroup``,
    ``radio``, ``quality``) keep ``NULLS LAST``: there the ordering genuinely differs, and
    those sorts are not the hot path.
    """
    descending = f.sort.startswith("-")
    column = SORTABLE[f.sort[1:] if descending else f.sort]
    primary = column.desc() if descending else column.asc()
    if getattr(column.expression, "nullable", True):
        primary = primary.nullslast()
    return [primary, Call.id.desc() if descending else Call.id.asc()]


# --------------------------------------------------------------------------------------
# pagination — offset and keyset
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PageParams:
    page: int
    page_size: int
    cursor: str | None = None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: Annotated[int, Query(ge=1, description="1-based page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=500, description="Rows per page (max 500)")] = 50,
    cursor: Annotated[
        str | None,
        Query(
            description=(
                "Keyset cursor from a previous response's `next_cursor`. Continues exactly "
                "where that page ended and cannot skip or repeat a row while new calls arrive; "
                "only valid while sorting by `started_at`. Overrides `page`."
            )
        ),
    ] = None,
) -> PageParams:
    return PageParams(page=page, page_size=page_size, cursor=cursor)


Paging = Annotated[PageParams, Depends(page_params)]


def encode_cursor(started_at: datetime, call_id: int) -> str:
    """Base64 of ``<iso started_at>,<id>`` — the keyset of the last row on a page."""
    raw = f"{started_at.isoformat()},{call_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        started_raw, _, id_raw = base64.urlsafe_b64decode(padded).decode().rpartition(",")
        return datetime.fromisoformat(started_raw), int(id_raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="cursor: not a cursor from this API") from exc


#: Sort keys a cursor may be used with.  The cursor encodes ``(started_at, id)`` and the
#: keyset predicate compares ``started_at``, so it is only a valid continuation for a sort
#: that orders by that column.  ``started_local`` is the same instant in the recorder's
#: zone and orders identically (bar the one ambiguous DST hour a year, documented in the
#: review as accepted); ``recorded_at`` is the call *end* and used to be accepted here,
#: which silently paged in one order while filtering in another (M8).
CURSOR_SORTS = frozenset({"started_at", "started_local"})


def cursor_condition(f: CallFilters, cursor: str) -> ColumnElement[bool]:
    """``(started_at, id) < (…)`` for a descending sort, ``>`` for ascending.

    The keyset comparison is spelled out as ``started_at < X OR (started_at = X AND id < Y)``
    and then ANDed with the redundant ``started_at <= X``.  The redundant half is what
    makes it fast: an OR is not an index range, so without it the planner walked
    ``ix_calls_started_at`` from the newest row and *filtered* its way down to the cursor.
    On production a page continuing from mid-2024 read 3.9 M buffers and discarded 4 M rows
    to return 500 (5.2 s); with the bound it becomes an ``Index Cond`` and the scan starts at
    the cursor, so page N costs what page 1 does.  A row-value ``(started_at, id) < (X, Y)``
    would need a ``(started_at, id)`` index to be a range; the bound needs none.
    """
    key = f.sort[1:] if f.sort.startswith("-") else f.sort
    if key not in CURSOR_SORTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"cursor pagination cannot continue a sort by {key!r} — it is only available "
                "while sorting by started_at"
            ),
        )
    started_at, call_id = decode_cursor(cursor)
    if f.sort.startswith("-"):
        return and_(
            Call.started_at <= started_at,
            or_(Call.started_at < started_at, and_(Call.started_at == started_at, Call.id < call_id)),
        )
    return and_(
        Call.started_at >= started_at,
        or_(Call.started_at > started_at, and_(Call.started_at == started_at, Call.id > call_id)),
    )


def conversation_join(stmt: Select[Any]) -> Select[Any]:
    return stmt.outerjoin(Conversation, Call.conversation_id == Conversation.id)


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC instants a local calendar day spans."""
    tz = _tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


__all__ = [
    "CALL_COLUMNS",
    "COUNT_CAP",
    "CURSOR_SORTS",
    "DEFAULT_SORT",
    "PRIORITY_CLASSES",
    "SORTABLE",
    "CallFilters",
    "Filters",
    "PageParams",
    "Paging",
    "aggregate_select",
    "apply_call_filters",
    "base_call_select",
    "call_conditions",
    "call_filters",
    "call_hidden_from_guests",
    "guest_aware_filters",
    "capped_count",
    "count_call_select",
    "cursor_condition",
    "decode_cursor",
    "encode_cursor",
    "filters_from_mapping",
    "local_day_bounds",
    "order_by",
    "page_params",
    "resolve_filter_sets",
    "search_query",
    "tsquery",
    "with_call_joins",
]
