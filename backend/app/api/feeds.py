"""Syndication: a saved call filter published as RSS 2.0 and Atom 1.0 (docs/FEEDS.md).

A **feed** is one ``GET /calls`` filter object — the same shape a saved search stores and
the same one :func:`app.api.filters.filters_from_mapping` parses — plus a secret.  Fetching
it needs no account, because the things that read feeds (a reader, a podcast app, a Slack
integration, a cron job) have no way to sign in and no cookie jar worth the name.  So the
credential is the URL:

* the token is 43 url-safe characters of :mod:`secrets`, not the row id;
* an unknown, a disabled or a deleted token is a flat ``404`` — never a ``401`` — so a
  scanner learns nothing from the status code about which tokens exist;
* rotating is one request, and the old URL stops working the instant it returns.

The audio enclosure follows the same rule and adds one of its own.  ``?feed=<token>`` on
``GET /api/calls/{id}/audio`` is accepted **only for a call the feed's own filters match**
(the check is :func:`app.services.calls.call_if_matches`, so there is no second
implementation of the filter language and no way for a feed over one talkgroup to become a
key to the archive).

Every feed — a token feed and a built-in one alike — publishes a call only once the
archive has *finished* with it: the transcript has reached a terminal state and no audio
pass is still queued.  See :func:`ready_for_publication`.

Nothing here logs a token.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Annotated, Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.api.filters import (
    CallFilters,
    base_call_select,
    filters_from_mapping,
    order_by,
    resolve_filter_sets,
)
from app.auth import Principal, WritePrincipal, require_scope
from app.config import get_settings
from app.db import get_session
from app.models import (
    TALKGROUP_CATEGORIES,
    ApiToken,
    AudioJob,
    Call,
    Feed,
    Talkgroup,
    Transcript,
    User,
)
from app.runtime_settings import effective_settings
from app.schemas import FeedIn, FeedOut, FeedPatch, FeedPresetOut, FeedPresetTalkgroup
from app.services import counts as counts_service
from app.services.calls import row_to_call
from app.vocabulary import CATEGORY_DESCRIPTIONS, category_label

log = logging.getLogger("app.api.feeds")

router = APIRouter(tags=["feeds"])
DbSession = Annotated[Session, Depends(get_session)]

#: The largest number of items a feed will publish.  A reader polls every few minutes and
#: keeps its own history; a thousand-item document is a database scan per poll and a
#: megabyte per reader, for entries every subscriber already has.
MAX_ITEMS_CAP = 200

#: How long a cached copy stays fresh.  Calls land continuously, so a feed is never
#: "done" — but a minute of staleness costs a reader nothing and saves the archive a query
#: per impatient client.
FEED_CACHE_CONTROL = "max-age=60"

#: ``ttl`` in the RSS channel, in minutes — the same advice, in RSS's own vocabulary.
FEED_TTL_MINUTES = 5

TOKEN_BYTES = 32

RSS_MEDIA_TYPE = "application/rss+xml; charset=utf-8"
ATOM_MEDIA_TYPE = "application/atom+xml; charset=utf-8"

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/elements/1.1/"


def new_token() -> str:
    """43 url-safe characters — the whole credential for one feed."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def _base_url() -> str:
    return get_settings().public_base_url.rstrip("/")


# --------------------------------------------------------------------------------------
# built-in feeds
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PresetTalkgroup:
    """One busy channel inside a built-in feed — what the Feeds page prints to say what
    the feed actually carries, rather than only how many talkgroups are filed under it."""

    tgid: int
    alias: str | None
    calls_7d: int


@dataclass(frozen=True, slots=True)
class PresetFeed:
    """A feed nobody created: a fixed filter with a stable, guessable URL.

    The same renderer serves it as serves a saved one, so it duck-types :class:`Feed` over
    the handful of attributes the renderer touches — ``name``, ``query``, ``max_items``,
    ``include_audio`` — and differs in the two that matter:

    * ``token`` is ``None``.  There is no secret, because a built-in feed is not secret:
      it publishes a slice of the archive that the archive's own read policy already
      decides who may see.  ``/api/feeds/preset/fire.rss`` is therefore an ordinary read —
      open while ``AUTH_REQUIRE_READ`` is false, and behind a sign-in when it is not.
    * ``slug`` names it in the URL for ever, so a subscription outlives a rename.
    """

    slug: str
    name: str
    description: str
    query: dict[str, Any] = field(default_factory=dict)
    max_items: int = 50
    include_audio: bool = True
    talkgroups: int = 0
    #: calls matching the feed over :data:`PRESET_WINDOW_DAYS`, and the busiest channels in
    #: it — filled in only by the list endpoint, which is the one caller that prints them
    calls_7d: int = 0
    top_talkgroups: tuple[PresetTalkgroup, ...] = ()
    #: never a credential; the attribute exists so the renderer can ask without a branch
    token: str | None = None
    #: only ``_filters_of``'s warning uses it, and a preset's filter is written here
    id: str = "preset"


#: What the renderer accepts: a stored feed, or a built-in one wearing the same shape.
FeedLike = Feed | PresetFeed


# --------------------------------------------------------------------------------------
# describing a filter set in English
# --------------------------------------------------------------------------------------

#: ``CallFilters`` field -> how to say it.  Order is the order of the sentence.
_LIST_PHRASES: list[tuple[str, str]] = [
    ("talkgroup", "talkgroups"),
    ("agency", "agencies"),
    ("category", "categories"),
    ("system", "systems"),
    ("site", "sites"),
    ("radio", "radios"),
    ("unit", "units"),
    ("tag", "tags"),
    ("call_type", "call types"),
    ("kind", "addressed to"),
    ("transcript_status", "transcript status"),
    ("quality", "quality"),
    ("priority", "priority"),
    ("audio_type", "audio"),
]

#: ``CallFilters`` boolean field -> what it means when true / when false.
_FLAG_PHRASES: dict[str, tuple[str, str]] = {
    "emergency": ("emergency only", "no emergency traffic"),
    "encrypted": ("encrypted only", "unencrypted only"),
    "tone_detected": ("with a tone", "without a tone"),
    "alert": ("pages only", "no pages"),
    "has_audio": ("with audio", "without audio"),
    "starred": ("starred only", "not starred"),
}

#: ``dow`` is PostgreSQL's ``extract('dow')`` — 0 is **Sunday**, not Monday.
_DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def _join(values: Sequence[Any], limit: int = 6) -> str:
    """``a, b, c`` — and ``+4 more`` rather than a paragraph of talkgroup ids."""
    shown = [str(v) for v in values[:limit]]
    extra = len(values) - len(shown)
    joined = ", ".join(shown)
    return f"{joined} +{extra} more" if extra > 0 else joined


def _names_for(db: Session, field_name: str, values: list[Any]) -> list[Any]:
    """Aliases in place of ids where the directory has them, so a summary reads
    ``talkgroups EC FIRE DISPATCH`` and not the row id the picker sent."""
    from app.models import Radio, Talkgroup  # noqa: PLC0415 - keep the module's import list short

    try:
        wanted = [int(v) for v in values]
    except (TypeError, ValueError):
        return values
    names: dict[int, str] = {}
    if field_name == "talkgroup":
        rows = db.execute(
            select(Talkgroup.id, Talkgroup.tgid, Talkgroup.alias).where(
                Talkgroup.id.in_(wanted) | Talkgroup.tgid.in_(wanted)
            )
        )
        for row_id, tgid, alias in rows:
            if alias:
                names.setdefault(int(row_id), alias)
                names.setdefault(int(tgid), alias)
    elif field_name == "radio":
        rows = db.execute(
            select(Radio.id, Radio.radio_id, Radio.alias).where(
                Radio.id.in_(wanted) | Radio.radio_id.in_(wanted)
            )
        )
        for row_id, radio_id, alias in rows:
            if alias:
                names.setdefault(int(row_id), alias)
                names.setdefault(int(radio_id), alias)
    else:
        return values
    return [names.get(v, v) for v in wanted]


def describe_filters(f: CallFilters, db: Session | None = None) -> str:
    """One sentence saying what a feed publishes, for somebody who did not build it.

    This is the feed's channel description, so it is written for a stranger's reader pane
    rather than for the person who ticked the boxes:
    ``matching "structure fire" · talkgroups 1101, 1001 · pages only``.  An empty filter
    set says so plainly — a feed over everything is a thing somebody may well have meant,
    and silence about it would read as a bug.
    """
    parts: list[str] = []
    if f.q:
        parts.append(f'matching "{f.q}"')
    for field_name, label in _LIST_PHRASES:
        values = getattr(f, field_name, ())
        if values:
            shown = _names_for(db, field_name, list(values)) if db is not None else list(values)
            parts.append(f"{label} {_join(shown)}")
    for field_name, (yes, no) in _FLAG_PHRASES.items():
        value = getattr(f, field_name, None)
        if value is not None:
            parts.append(yes if value else no)
    if f.date_from or f.date_to:
        start = f.date_from.date().isoformat() if f.date_from else "the beginning"
        end = f.date_to.date().isoformat() if f.date_to else "now"
        parts.append(f"{start} to {end}")
    if f.time_from and f.time_to:
        parts.append(f"between {f.time_from.strftime('%H:%M')} and {f.time_to.strftime('%H:%M')}")
    if f.dow:
        parts.append("on " + ", ".join(_DAYS[d] for d in sorted(f.dow) if 0 <= d < len(_DAYS)))
    if f.min_duration is not None:
        parts.append(f"at least {f.min_duration:g}s")
    if f.max_duration is not None:
        parts.append(f"at most {f.max_duration:g}s")
    if f.ids:
        parts.append(f"{len(f.ids)} picked calls")
    if not parts:
        return "Every call in the archive"
    return " · ".join(parts)


# --------------------------------------------------------------------------------------
# the items
# --------------------------------------------------------------------------------------


def _edited_texts(db: Session, call_ids: Sequence[int]) -> dict[int, str]:
    """``call_id -> edited_text`` for the primary transcripts a human has corrected.

    One extra query rather than a join on the hot list SELECT: a correction is rare, and
    the calls list is shared with every other endpoint.
    """
    if not call_ids:
        return {}
    rows = db.execute(
        select(Transcript.call_id, Transcript.edited_text).where(
            Transcript.call_id.in_(call_ids),
            Transcript.is_primary.is_(True),
            Transcript.edited_text.is_not(None),
        )
    )
    return {int(call_id): str(text) for call_id, text in rows if (text or "").strip()}


def _file_sizes(db: Session, call_ids: Sequence[int]) -> dict[int, int]:
    """``call_id -> bytes``.  RSS wants a ``length`` on an enclosure and a podcast client
    shows it as the download size, so it is worth one indexed lookup rather than a lie."""
    if not call_ids:
        return {}
    rows = db.execute(select(Call.id, Call.file_size).where(Call.id.in_(call_ids)))
    return {int(cid): int(size) for cid, size in rows if size}


def _filters_of(feed: FeedLike) -> CallFilters:
    """The feed's stored filter object, parsed.  A stored filter that no longer parses
    publishes nothing rather than publishing everything (a feed silently widening to the
    whole archive is the one failure mode worth being paranoid about)."""
    try:
        return filters_from_mapping(dict(feed.query or {}))
    except (HTTPException, TypeError, ValueError):
        log.warning("feeds: feed %s has a filter set that no longer parses", feed.id)
        raise


#: ``calls.transcript_status`` values that mean the speech pipeline has not finished with
#: the row yet.  Everything else in :data:`app.models.TRANSCRIPT_STATUSES` — ``done``,
#: ``skipped``, ``failed``, ``empty`` — is terminal: the archive will not change the text
#: again on its own, so the record is as complete as it is ever going to be.  Written as
#: the *pending* set rather than the terminal one deliberately: a status added later is
#: assumed terminal and publishes, which is a late item rather than a missing one.
PENDING_TRANSCRIPT_STATUSES = ("pending", "queued", "running")

#: ``audio_jobs.status`` values that mean a processing pass is still to come.  These are
#: exactly the two the partial unique index ``ix_audio_jobs_open_call`` is built on, so
#: the ``NOT EXISTS`` below is an index probe and not a scan of the queue.
OPEN_AUDIO_JOB_STATUSES = ("queued", "running")


def ready_for_publication(max_wait_s: float | None = None) -> ColumnElement[bool]:
    """The predicate for "the archive has finished with this call".

    A call is written the moment its audio lands, and *then* it is transcribed and (when
    the deployment processes audio) normalised.  A reader that fetched in that window got
    a permanent entry — the ``guid`` is the call's permalink, so nothing re-publishes it —
    whose body said *Transcript pending* and whose enclosure might point at a file about
    to be rewritten.  The words arrived a minute later and no subscriber ever saw them.

    So a feed holds an item back until both queues have let go of the call:

    * ``transcript_status`` is terminal — not one of :data:`PENDING_TRANSCRIPT_STATUSES`.
      ``skipped`` and ``failed`` count: the archive has decided there will be no text,
      which is a finished record and not a pending one.
    * no open ``audio_jobs`` row, so the file behind the enclosure is the final one.

    The cost is latency, not completeness: the item still carries the call's own
    ``started_at`` as its ``pubDate``, so a reader that sorts by date files it where it
    belongs rather than at the moment the transcript happened to land.

    **The wait is capped** (``feed_ready_max_wait_s``, 15 min by default, measured from
    ``ingested_at``): past it a call publishes whatever its status.  Without the cap a
    stuck speech or audio worker held back every item of every feed until somebody noticed;
    an item with *Transcript pending* is a worse record than a finished one, but a much
    better one than none.  Ingest time rather than ``started_at`` so a backlog import of
    old recordings still gets its transcripts before it goes out.  ``max_wait_s`` overrides
    the setting (tests); 0 or less means no cap.
    """
    finished = and_(
        Call.transcript_status.notin_(PENDING_TRANSCRIPT_STATUSES),
        ~exists().where(
            AudioJob.call_id == Call.id, AudioJob.status.in_(OPEN_AUDIO_JOB_STATUSES)
        ),
    )
    wait = float(get_settings().feed_ready_max_wait_s if max_wait_s is None else max_wait_s)
    if wait <= 0:
        return finished
    return or_(finished, Call.ingested_at <= datetime.now(UTC) - timedelta(seconds=wait))


def feed_items(db: Session, feed: FeedLike) -> list[Any]:
    """The newest ``max_items`` **complete** calls the feed's filters match, newest first.

    The readiness gate is unconditional and has no per-feed setting: a feed is a permanent
    record handed to software that will never look again, so publishing a half-written one
    is not a trade-off anybody would opt into per feed.  Its one knob is the deployment-wide
    cap on how long it waits (:func:`ready_for_publication`).
    """
    f = resolve_filter_sets(db, _filters_of(feed))
    limit = max(1, min(int(feed.max_items or 50), MAX_ITEMS_CAP))
    stmt = (
        base_call_select(f)
        .where(ready_for_publication())
        .order_by(*order_by(f.replace(sort="-started_at")))
        .limit(limit)
    )
    return [row_to_call(row) for row in db.execute(stmt)]


def item_title(call: Any) -> str:
    """``EC EVENT 6 · 14:17:03 · 4.1s`` — or ``E1 → E2 · …`` for a unit call."""
    when = call.started_local.strftime("%H:%M:%S")
    duration = f"{float(call.duration_s or 0):.1f}s"
    if call.kind == "unit" or call.call_type == "private":
        left = call.radio_label or call.radio_raw or "unknown"
        right = call.target_radio_label or "unknown"
        return f"{left} → {right} · {when} · {duration}"
    channel = call.talkgroup_alias or call.talkgroup_alias_at_record
    if not channel:
        channel = str(call.tgid) if call.tgid is not None else "unknown talkgroup"
    return f"{channel} · {when} · {duration}"


#: What to say when there is no transcript, by ``transcript_status``.
_NO_TEXT = {
    "pending": "Transcript pending",
    "queued": "Transcript pending",
    "running": "Transcript pending",
    "skipped": "No transcript",
    "failed": "No transcript",
    "empty": "No transcript",
    "done": "No transcript",
}


def item_body(call: Any, edited: str | None) -> str:
    """The transcript — a human's correction first — then one line of metadata.

    A noise-flagged transcript is *not* printed: the archive's own judgment is that the
    words are an artefact of the model, and republishing them into somebody's reader would
    launder a known hallucination into a quotable line.
    """
    if edited:
        text = edited.strip()
    elif (call.transcript_quality or "") == "noise":
        text = "Rejected as hallucination"
    elif (call.transcript_text or "").strip():
        text = call.transcript_text.strip()
    else:
        text = _NO_TEXT.get(call.transcript_status or "pending", "No transcript")

    facts: list[str] = []
    if call.radio_label:
        facts.append(f"radio {call.radio_label}")
    if call.agency_code:
        facts.append(call.agency_code)
    if call.site_name:
        facts.append(call.site_name)
    if call.transcript_quality:
        facts.append(f"quality {call.transcript_quality}")
    if call.emergency:
        facts.append("emergency")
    if call.encrypted:
        facts.append("encrypted")
    return f"{text}\n\n{' · '.join(facts)}" if facts else text


def _audio_url(call: Any, feed: FeedLike) -> str:
    """The enclosure.  A token feed signs its own enclosures with its token; a built-in
    feed has no secret to sign with and links the plain audio route, which is open under
    exactly the same read policy that let the document itself be fetched."""
    url = f"{_base_url()}/api/calls/{call.id}/audio"
    return f"{url}?feed={feed.token}" if feed.token else url


def _self_url(feed: FeedLike, extension: str) -> str:
    """``rel="self"`` — the URL this document was fetched from."""
    slug = getattr(feed, "slug", None)
    if slug is not None:
        return f"{_base_url()}/api/feeds/preset/{slug}.{extension}"
    return f"{_base_url()}/api/feeds/{feed.token}.{extension}"


def _channel_description(feed: FeedLike, db: Session | None) -> str:
    """A built-in feed says what it is in the words the Help page uses; a saved one is
    described from its filters, because nobody wrote it a sentence."""
    written = getattr(feed, "description", "")
    return written or describe_filters(_filters_of(feed), db)


def _audio_type(call: Any) -> str:
    """The enclosure's MIME type, from what ingest recorded about the file."""
    stored = (call.audio_type or "").lower()
    if "m4a" in stored or "mp4" in stored or "aac" in stored:
        return "audio/mp4"
    if "wav" in stored:
        return "audio/wav"
    return "audio/mpeg"


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def _text(parent: ET.Element, tag: str, value: str, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, tag, {k: v for k, v in attrs.items()})
    node.text = value
    return node


def _serialize(root: ET.Element) -> bytes:
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + body


def render_rss(
    feed: FeedLike,
    calls: Sequence[Any],
    edited: dict[int, str],
    sizes: dict[int, int] | None = None,
    db: Session | None = None,
) -> bytes:
    """RSS 2.0.  ``pubDate`` is RFC 822, which is what RSS says and what readers parse."""
    base = _base_url()
    self_url = _self_url(feed, "rss")
    sizes = sizes or {}
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": ATOM_NS, "xmlns:dc": DC_NS})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", feed.name)
    _text(channel, "link", f"{base}/calls")
    _text(channel, "description", _channel_description(feed, db))
    _text(channel, "generator", "Keyup")
    _text(channel, "lastBuildDate", format_datetime(datetime.now(UTC)))
    _text(channel, "ttl", str(FEED_TTL_MINUTES))
    ET.SubElement(channel, "atom:link", {"href": self_url, "rel": "self", "type": "application/rss+xml"})

    for call in calls:
        item = ET.SubElement(channel, "item")
        link = f"{base}/calls/{call.id}"
        _text(item, "title", item_title(call))
        _text(item, "link", link)
        _text(item, "guid", link, isPermaLink="true")
        _text(item, "pubDate", format_datetime(call.started_at))
        _text(item, "description", item_body(call, edited.get(call.id)))
        for category in (call.talkgroup_alias, call.agency_code):
            if category:
                _text(item, "category", category)
        if call.radio_label:
            # ``dc:creator`` rather than ``author``: RSS says an ``author`` is an email
            # address, and a radio is not one.  Readers show ``dc:creator`` as the byline.
            _text(item, "dc:creator", call.radio_label)
        if feed.include_audio and call.audio_state == "present":
            ET.SubElement(
                item,
                "enclosure",
                {
                    "url": _audio_url(call, feed),
                    "type": _audio_type(call),
                    "length": str(sizes.get(call.id, 0)),
                },
            )
    return _serialize(rss)


def render_atom(
    feed: FeedLike,
    calls: Sequence[Any],
    edited: dict[int, str],
    sizes: dict[int, int] | None = None,
    db: Session | None = None,
) -> bytes:
    """Atom 1.0.  ``id`` is the same permalink as ``link``, which is a legal Atom id and
    spares a reader from treating a re-render as a new entry."""
    base = _base_url()
    self_url = _self_url(feed, "atom")
    sizes = sizes or {}
    root = ET.Element("feed", {"xmlns": ATOM_NS})
    _text(root, "title", feed.name)
    _text(root, "subtitle", _channel_description(feed, db))
    _text(root, "id", self_url)
    _text(root, "updated", (calls[0].started_at if calls else datetime.now(UTC)).isoformat())
    _text(root, "generator", "Keyup")
    ET.SubElement(root, "link", {"href": f"{base}/calls", "rel": "alternate"})
    ET.SubElement(root, "link", {"href": self_url, "rel": "self", "type": "application/atom+xml"})

    for call in calls:
        entry = ET.SubElement(root, "entry")
        link = f"{base}/calls/{call.id}"
        _text(entry, "title", item_title(call))
        _text(entry, "id", link)
        ET.SubElement(entry, "link", {"href": link, "rel": "alternate", "type": "text/html"})
        _text(entry, "updated", call.started_at.isoformat())
        _text(entry, "published", call.started_at.isoformat())
        _text(entry, "content", item_body(call, edited.get(call.id)), type="text")
        for category in (call.talkgroup_alias, call.agency_code):
            if category:
                ET.SubElement(entry, "category", {"term": category})
        if call.radio_label:
            _text(ET.SubElement(entry, "author"), "name", call.radio_label)
        if feed.include_audio and call.audio_state == "present":
            ET.SubElement(
                entry,
                "link",
                {
                    "href": _audio_url(call, feed),
                    "rel": "enclosure",
                    "type": _audio_type(call),
                    "length": str(sizes.get(call.id, 0)),
                },
            )
    return _serialize(root)


# --------------------------------------------------------------------------------------
# the public routes — token in the path, no cookie, no CSRF
# --------------------------------------------------------------------------------------

#: No ``min_length``: a short token must answer 404 like every other wrong one, and a 422
#: would tell a prober that the shape of the guess was what failed.
FeedToken = Annotated[str, Path(max_length=128, description="The feed's secret")]


def feed_by_token(db: Session, token: str) -> Feed:
    """The enabled feed with this token, or a 404 that says nothing else.

    Disabled and unknown answer identically on purpose: a feed that has been turned off is
    a URL somebody may have leaked, and confirming that it *used* to be real is exactly the
    fact the operator was trying to take back.
    """
    feed = db.scalar(select(Feed).where(Feed.token == token, Feed.enabled.is_(True)))
    if feed is None:
        raise HTTPException(status_code=404, detail="No such feed")
    return feed


def _note_fetch(db: Session, feed_id: int) -> None:
    """One unconditional UPDATE, committed on its own — a counter must never make a reader
    wait on another reader, and a lost increment costs nothing worth a lock."""
    try:
        db.execute(
            update(Feed)
            .where(Feed.id == feed_id)
            .values(last_fetched_at=datetime.now(UTC), fetch_count=Feed.fetch_count + 1)
        )
        db.commit()
    except Exception:  # pragma: no cover - a counter is never worth failing a fetch
        db.rollback()
        log.warning("feeds: could not record the fetch of feed %s", feed_id)


def _rendered(db: Session, feed: Feed, media_type: str, body: bytes) -> Response:
    _note_fetch(db, feed.id)
    return Response(
        content=body,
        media_type=media_type,
        headers={"Cache-Control": FEED_CACHE_CONTROL, "X-Robots-Tag": "noindex"},
    )


@router.get(
    "/feeds/{token}.rss",
    summary="A feed as RSS 2.0",
    response_class=Response,
    responses={200: {"content": {"application/rss+xml": {}}}, 404: {"description": "Unknown or disabled"}},
    description=(
        "The newest calls matching the feed's saved filters, as RSS 2.0. The token in the "
        "path is the whole credential — anybody holding this URL can read what the filters "
        "match — so treat it as a password, and rotate it if it gets out. An unknown or "
        "disabled token is a 404."
    ),
)
def feed_rss(token: FeedToken, db: DbSession) -> Response:
    feed = feed_by_token(db, token)
    calls = feed_items(db, feed)
    ids = [c.id for c in calls]
    edited = _edited_texts(db, ids)
    sizes = _file_sizes(db, ids) if feed.include_audio else {}
    return _rendered(db, feed, RSS_MEDIA_TYPE, render_rss(feed, calls, edited, sizes, db))


@router.get(
    "/feeds/{token}.atom",
    summary="A feed as Atom 1.0",
    response_class=Response,
    responses={200: {"content": {"application/atom+xml": {}}}, 404: {"description": "Unknown or disabled"}},
    description="The same items as the RSS twin, as Atom 1.0. The token is the credential.",
)
def feed_atom(token: FeedToken, db: DbSession) -> Response:
    feed = feed_by_token(db, token)
    calls = feed_items(db, feed)
    ids = [c.id for c in calls]
    edited = _edited_texts(db, ids)
    sizes = _file_sizes(db, ids) if feed.include_audio else {}
    return _rendered(db, feed, ATOM_MEDIA_TYPE, render_atom(feed, calls, edited, sizes, db))


# --------------------------------------------------------------------------------------
# the built-in routes — no token, the archive's own read policy
# --------------------------------------------------------------------------------------

#: The one preset that is not a talkgroup category.
PRESET_ALL = "all"

PRESET_ALL_DESCRIPTION = (
    "Every call in the archive, newest first — the whole thing, as it is transcribed."
)

#: No ``pattern``: an unknown slug must be a 404 like every other one, and a 422 would
#: make the shape of a guess distinguishable from its content.
PresetSlug = Annotated[str, Path(max_length=64, description="``all`` or a talkgroup category")]


def _category_counts(db: Session) -> dict[str, int]:
    """``category -> how many talkgroups are in it``, keyed by the lower-case slug.

    Derived at request time rather than from :data:`TALKGROUP_CATEGORIES`, because the
    point of the list is the categories this deployment's directory actually uses: a feed
    for a category no talkgroup is filed under would publish nothing for ever.
    """
    slug = func.lower(func.trim(Talkgroup.category))
    rows = db.execute(
        select(slug, func.count())
        .where(Talkgroup.category.is_not(None), func.trim(Talkgroup.category) != "")
        .group_by(slug)
    )
    return {str(key): int(count) for key, count in rows}


def _preset_order(counts: dict[str, int]) -> list[str]:
    """The vocabulary's own order first — law, fire, EMS — then anything the directory
    holds that the vocabulary has not heard of, alphabetically rather than at random."""
    known = [key for key in TALKGROUP_CATEGORIES if key in counts]
    return known + sorted(set(counts) - set(TALKGROUP_CATEGORIES))


#: Built-in feeds that span several categories.  ``(slug, name, description, categories)``;
#: a category the directory does not use is simply absent from that feed's count.
COMPOSITE_PRESETS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "emergency",
        "Keyup · Emergency dispatch",
        "The majority of emergency traffic in one feed: law enforcement, fire, EMS, the "
        "multi-service event channels and the interoperability channels the agencies "
        "share on an incident.",
        ("law", "fire", "ems", "multi", "interop"),
    ),
)


def feed_presets(db: Session, *, include_audio: bool = True) -> list[PresetFeed]:
    """Every built-in feed, in the order the list endpoint and the UI show them."""
    counts = _category_counts(db)
    total = int(db.scalar(select(func.count()).select_from(Talkgroup)) or 0)
    built = [
        PresetFeed(
            slug=PRESET_ALL,
            name="Keyup · All calls",
            description=PRESET_ALL_DESCRIPTION,
            query={},
            include_audio=include_audio,
            talkgroups=total,
        )
    ]
    built += [
        PresetFeed(
            slug=slug,
            name=name,
            description=description,
            query={"category": [c for c in categories if c in counts]},
            include_audio=include_audio,
            talkgroups=sum(counts.get(c, 0) for c in categories),
        )
        for slug, name, description, categories in COMPOSITE_PRESETS
        if any(c in counts for c in categories)
    ]
    built += [
        PresetFeed(
            slug=key,
            name=f"Keyup · {category_label(key)}",
            description=CATEGORY_DESCRIPTIONS.get(key, f"Every {category_label(key)} channel."),
            query={"category": [key]},
            include_audio=include_audio,
            talkgroups=counts[key],
        )
        for key in _preset_order(counts)
    ]
    return built


def _preset_or_404(db: Session, slug: str, *, include_audio: bool = True) -> PresetFeed:
    wanted = slug.strip().lower()
    for preset in feed_presets(db, include_audio=include_audio):
        if preset.slug == wanted:
            return preset
    raise HTTPException(status_code=404, detail=f"no built-in feed {slug}")


def _preset_audio_allowed(db: Session) -> bool:
    """Enclosures only while the archive is readable without an account.

    A built-in feed has no token, so its enclosure would be a plain ``/audio`` URL — and
    with ``AUTH_REQUIRE_READ`` on that is a 401 in a podcast app, which reads as a broken
    feed rather than as a closed archive.  Minting a signed per-item key instead would put
    a credential back into a URL that exists precisely because it holds none, so the
    document simply carries no enclosure while reads are closed: the items, the links and
    the transcripts are all still there for the reader that signed in to fetch it.
    """
    return not effective_settings(db).auth_require_read


def _preset_rendered(media_type: str, body: bytes) -> Response:
    """Like :func:`_rendered`, without the fetch counter — there is no row to count on."""
    return Response(
        content=body,
        media_type=media_type,
        headers={"Cache-Control": FEED_CACHE_CONTROL, "X-Robots-Tag": "noindex"},
    )


#: The window the built-in feed list reports volume over.  A week rather than a day so a
#: category that only runs on weekdays does not read as empty on a Sunday, and rather than
#: a month so "busiest channels" means what it says now and not what it meant in the spring.
PRESET_WINDOW_DAYS = 7

#: How many channels a built-in feed names.  The point is to let somebody recognise the
#: feed — "oh, that is the county fire dispatch one" — not to reproduce the directory.
PRESET_TOP_TALKGROUPS = 5

#: How long the whole built-in list is reused.  It costs one grouped query per preset (a
#: production deployment has sixteen) and its numbers are a week's volume, which does not
#: move in ten minutes.  ``app.services.counts.cached`` is process-local, and the test
#: suite clears it between tests along with every other cached count.
PRESETS_CACHE_TTL_S = 600.0

PRESETS_CACHE_KEY = "feeds:presets"


def _preset_volume(db: Session, preset: PresetFeed) -> tuple[int, tuple[PresetTalkgroup, ...]]:
    """``(calls in the window, the busiest channels in it)`` for one built-in feed.

    One grouped query over the feed's own filter set — the same
    :func:`resolve_filter_sets` the documents themselves go through, so the number
    describes what the feed publishes and is not a second, subtly different definition of
    "in this category".  The readiness gate is deliberately *not* applied: this is a
    description of the slice, and holding a minute of it back would make the figure wrong
    rather than careful.
    """
    since = datetime.now(UTC) - timedelta(days=PRESET_WINDOW_DAYS)
    f = resolve_filter_sets(db, _filters_of(preset).replace(date_from=since))
    total = func.count().label("calls")
    rows = db.execute(
        base_call_select(f, Talkgroup.tgid, Talkgroup.alias, total)
        .group_by(Talkgroup.tgid, Talkgroup.alias)
        .order_by(total.desc())
    ).all()
    calls = sum(int(row[2]) for row in rows)
    top = tuple(
        PresetTalkgroup(tgid=int(tgid), alias=alias, calls_7d=int(count))
        for tgid, alias, count in rows[: PRESET_TOP_TALKGROUPS + 1]
        if tgid is not None
    )[:PRESET_TOP_TALKGROUPS]
    return calls, top


def _presets_payload(db: Session) -> list[FeedPresetOut]:
    """The list endpoint's whole answer — computed behind :data:`PRESETS_CACHE_TTL_S`."""
    base = _base_url()
    out: list[FeedPresetOut] = []
    for preset in feed_presets(db):
        calls_7d, top = _preset_volume(db, preset)
        out.append(
            FeedPresetOut(
                slug=preset.slug,
                name=preset.name,
                description=preset.description,
                query=dict(preset.query),
                talkgroups=preset.talkgroups,
                calls_7d=calls_7d,
                top_talkgroups=[
                    FeedPresetTalkgroup(tgid=tg.tgid, alias=tg.alias, calls_7d=tg.calls_7d)
                    for tg in top
                ],
                rss_url=f"{base}/api/feeds/preset/{preset.slug}.rss",
                atom_url=f"{base}/api/feeds/preset/{preset.slug}.atom",
            )
        )
    return out


@router.get(
    "/feeds/presets",
    response_model=list[FeedPresetOut],
    summary="The built-in feeds",
    description=(
        "Feeds nobody had to create: one over everything, and one per talkgroup category "
        "the directory actually uses. The URLs are stable and hold no secret, so they can "
        "be handed out — they are readable by whoever may read the archive, which is "
        "everybody while `AUTH_REQUIRE_READ` is off. Each entry also says what is in it: "
        "how many talkgroups are filed under it, how many calls it matched over the last "
        "seven days, and the five busiest channels in that week. Those figures are "
        "recomputed at most every ten minutes."
    ),
)
def list_feed_presets(db: DbSession) -> list[FeedPresetOut]:
    return counts_service.cached(
        PRESETS_CACHE_KEY, PRESETS_CACHE_TTL_S, lambda: _presets_payload(db)
    )


def _preset_document(db: Session, slug: str, *, atom: bool) -> Response:
    preset = _preset_or_404(db, slug, include_audio=_preset_audio_allowed(db))
    calls = feed_items(db, preset)
    ids = [c.id for c in calls]
    edited = _edited_texts(db, ids)
    sizes = _file_sizes(db, ids) if preset.include_audio else {}
    render = render_atom if atom else render_rss
    media_type = ATOM_MEDIA_TYPE if atom else RSS_MEDIA_TYPE
    return _preset_rendered(media_type, render(preset, calls, edited, sizes, db))


@router.get(
    "/feeds/preset/{slug}.rss",
    summary="A built-in feed as RSS 2.0",
    response_class=Response,
    responses={200: {"content": {"application/rss+xml": {}}}, 404: {"description": "No such preset"}},
    description=(
        "`all`, or a talkgroup category (`law`, `fire`, `ems`, …) — the same slugs "
        "`GET /api/feeds/presets` lists. No token: this is an ordinary read of the "
        "archive and follows the archive's own policy."
    ),
)
def feed_preset_rss(slug: PresetSlug, db: DbSession) -> Response:
    return _preset_document(db, slug, atom=False)


@router.get(
    "/feeds/preset/{slug}.atom",
    summary="A built-in feed as Atom 1.0",
    response_class=Response,
    responses={200: {"content": {"application/atom+xml": {}}}, 404: {"description": "No such preset"}},
    description="The same items as the RSS twin, as Atom 1.0.",
)
def feed_preset_atom(slug: PresetSlug, db: DbSession) -> Response:
    return _preset_document(db, slug, atom=True)


# --------------------------------------------------------------------------------------
# management
# --------------------------------------------------------------------------------------


def _owner(principal: Principal) -> tuple[int | None, int | None]:
    """``(user_id, token_id)`` a feed made by this principal is stored under.

    A signed-in user owns by ``user_id``; a token principal has no user, so it owns by its
    own ``token_id``.  Exactly one of the two is set — a principal that has neither (which
    :func:`app.auth.authenticate` never produces) owns nothing.
    """
    if principal.user is not None:
        return principal.user.id, None
    if principal.token is not None:
        return None, principal.token.id
    return None, None


def _owned_by(principal: Principal) -> ColumnElement[bool] | None:
    """The ``WHERE`` clause for "feeds this principal owns", or ``None`` for nothing.

    Compared on the matching owner column only, never ``IS NULL``: a feed with neither
    owner (its token was deleted, or it was made by a token before ``token_id`` existed)
    belongs to nobody but the admins.
    """
    user_id, token_id = _owner(principal)
    if user_id is not None:
        return Feed.user_id == user_id
    if token_id is not None:
        return Feed.token_id == token_id
    return None


def _may_touch(principal: Principal, feed: Feed) -> bool:
    if principal.is_admin:
        return True
    user_id, token_id = _owner(principal)
    if user_id is not None:
        return feed.user_id == user_id
    if token_id is not None:
        return feed.token_id == token_id
    return False


def _feed_out(feed: Feed, *, owner: str | None = None, db: Session | None = None) -> FeedOut:
    base = _base_url()
    query = dict(feed.query or {})
    try:
        summary = describe_filters(filters_from_mapping(query), db)
    except (HTTPException, TypeError, ValueError):  # pragma: no cover - stored by this module
        summary = "Unreadable filter set"
    return FeedOut(
        id=feed.id,
        name=feed.name,
        query=query,
        summary=summary,
        enabled=feed.enabled,
        include_audio=feed.include_audio,
        max_items=feed.max_items,
        rss_url=f"{base}/api/feeds/{feed.token}.rss",
        atom_url=f"{base}/api/feeds/{feed.token}.atom",
        owner=owner,
        created_at=feed.created_at,
        updated_at=feed.updated_at,
        last_fetched_at=feed.last_fetched_at,
        fetch_count=feed.fetch_count,
    )


def _normalized_query(raw: dict[str, Any] | None) -> tuple[dict[str, Any], CallFilters]:
    """Parse what was sent and store *that*, so what the UI shows is what will apply.

    A filter object arrives from the SPA's address bar and carries whatever else lives
    there — ``page``, ``view``, a column list.  ``filters_from_mapping`` ignores unknown
    keys, and echoing the parsed set back means none of that noise is stored as though it
    were part of the feed.
    """
    parsed = filters_from_mapping(dict(raw or {}))
    echoed = parsed.echo()
    echoed.pop("sort", None)  # a feed is always newest-first; sort is not the feed's to keep
    return echoed, parsed


@router.get(
    "/feeds",
    response_model=list[FeedOut],
    summary="My feeds",
    description=(
        "The feeds you own, each with its RSS and Atom URLs. Those URLs contain the token "
        "and are therefore credentials. An admin sees every feed, with its owner."
    ),
)
def list_feeds(
    db: DbSession, principal: Annotated[Principal, Depends(require_scope("read"))]
) -> list[FeedOut]:
    stmt = select(Feed).order_by(Feed.name)
    if not principal.is_admin:
        mine = _owned_by(principal)
        if mine is None:
            return []
        stmt = stmt.where(mine)
    feeds = list(db.scalars(stmt))
    if not principal.is_admin:
        return [_feed_out(f, db=db) for f in feeds]
    users: dict[int, str] = {}
    tokens: dict[int, str] = {}
    user_ids = {f.user_id for f in feeds if f.user_id is not None}
    token_ids = {f.token_id for f in feeds if f.token_id is not None}
    if user_ids:
        users = {
            int(uid): str(username)
            for uid, username in db.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        }
    if token_ids:
        tokens = {
            int(tid): f"token:{label}"
            for tid, label in db.execute(
                select(ApiToken.id, ApiToken.label).where(ApiToken.id.in_(token_ids))
            )
        }

    def owner_of(feed: Feed) -> str | None:
        if feed.user_id is not None:
            return users.get(feed.user_id)
        if feed.token_id is not None:
            return tokens.get(feed.token_id)
        return None

    return [_feed_out(f, owner=owner_of(f), db=db) for f in feeds]


@router.post(
    "/feeds",
    response_model=FeedOut,
    status_code=201,
    summary="Publish a filter as a feed",
    description=(
        "Turns the current filters into an RSS/Atom feed and mints its token. The `query` "
        "you send is parsed and the **parsed** form is stored and returned, so the feed "
        "publishes what the filter language actually understood. `max_items` is capped at "
        "200."
    ),
)
def create_feed(body: FeedIn, db: DbSession, principal: WritePrincipal) -> FeedOut:
    query, _ = _normalized_query(body.query)
    user_id, token_id = _owner(principal)
    feed = Feed(
        user_id=user_id,
        token_id=token_id,
        name=body.name.strip(),
        token=new_token(),
        query=query,
        enabled=body.enabled,
        include_audio=body.include_audio,
        max_items=min(int(body.max_items), MAX_ITEMS_CAP),
    )
    db.add(feed)
    db.commit()
    log.info("feeds: %s created feed %s (%s)", principal.label, feed.id, feed.name)
    return _feed_out(feed, db=db)


def _owned(db: Session, feed_id: int, principal: Principal) -> Feed:
    feed = db.get(Feed, feed_id)
    if feed is None or not _may_touch(principal, feed):
        # Somebody else's feed is not yours to know about, so it is missing rather than
        # forbidden: a 403 confirms the id exists.
        raise HTTPException(status_code=404, detail=f"no feed {feed_id}")
    return feed


@router.patch(
    "/feeds/{feed_id}",
    response_model=FeedOut,
    summary="Edit a feed",
    description="Only the fields you send are changed. Editing never changes the token.",
)
def update_feed(feed_id: int, body: FeedPatch, db: DbSession, principal: WritePrincipal) -> FeedOut:
    feed = _owned(db, feed_id, principal)
    fields = body.model_dump(exclude_unset=True)
    if fields.get("name"):
        feed.name = str(fields["name"]).strip()
    if "query" in fields and fields["query"] is not None:
        feed.query, _ = _normalized_query(fields["query"])
    if fields.get("enabled") is not None:
        feed.enabled = bool(fields["enabled"])
    if fields.get("include_audio") is not None:
        feed.include_audio = bool(fields["include_audio"])
    if fields.get("max_items") is not None:
        feed.max_items = min(int(fields["max_items"]), MAX_ITEMS_CAP)
    db.commit()
    return _feed_out(feed, db=db)


@router.post(
    "/feeds/{feed_id}/rotate",
    response_model=FeedOut,
    summary="Rotate a feed's token",
    description=(
        "Mints a new token and returns the new URLs. Every copy of the old URL stops "
        "working immediately — which is the point, and also means every subscriber has to "
        "be given the new one."
    ),
)
def rotate_feed(feed_id: int, db: DbSession, principal: WritePrincipal) -> FeedOut:
    feed = _owned(db, feed_id, principal)
    feed.token = new_token()
    db.commit()
    log.info("feeds: %s rotated the token of feed %s", principal.label, feed.id)
    return _feed_out(feed, db=db)


@router.delete("/feeds/{feed_id}", status_code=204, summary="Delete a feed")
def delete_feed(feed_id: int, db: DbSession, principal: WritePrincipal) -> Response:
    feed = _owned(db, feed_id, principal)
    db.delete(feed)
    db.commit()
    return Response(status_code=204)


__all__ = [
    "MAX_ITEMS_CAP",
    "PRESETS_CACHE_KEY",
    "PRESET_TOP_TALKGROUPS",
    "PRESET_WINDOW_DAYS",
    "PresetFeed",
    "PresetTalkgroup",
    "describe_filters",
    "feed_by_token",
    "feed_items",
    "feed_presets",
    "item_body",
    "item_title",
    "new_token",
    "ready_for_publication",
    "render_atom",
    "render_rss",
    "router",
]
