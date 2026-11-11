"""Recent transcripts: the first screen of the embeddable live view (docs/EMBED.md).

``/embed/transcripts`` is a page other sites put in an ``<iframe>``, so it has to be small
on the wire as well as in JavaScript.  ``GET /calls`` answers with the whole denormalized
row — some forty fields, raw metadata and all — for every call; this answers with the ten
the view prints, for the last few minutes of the filter, and hands back the filter itself
in the shape ``WS /api/live`` takes as its first frame so the page can tail it from there.

Nothing here is a second filter language.  The query goes through
:func:`~app.api.filters.call_filters` (every ``GET /calls`` parameter works),
:func:`~app.api.filters.resolve_filter_sets` and :func:`~app.api.filters.base_call_select`
— the same three steps the calls list and the live feed take — so a row is on this page
exactly when it would be on the Calls page under the same filter.  Duplicates are always
excluded.  The only thing added is the view's own rule about *which* calls are worth a
row: a finished transcript with text in it, not one the hallucination check threw away —
unless ``show_pending`` asks for every call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.feeds import COMPOSITE_PRESETS, PRESET_ALL, feed_presets
from app.api.filters import (
    CALL_COLUMNS,
    CallFilters,
    Filters,
    base_call_select,
    cursor_condition,
    encode_cursor,
    order_by,
    resolve_filter_sets,
)
from app.db import get_session
from app.models import TALKGROUP_CATEGORIES, Call
from app.schemas import EmbedPresetOut, RecentTranscriptOut, RecentTranscriptsOut

router = APIRouter(prefix="/transcripts", tags=["live"])
DbSession = Annotated[Session, Depends(get_session)]

#: The window the embed loads by default, and the most it may ask for.  Four hours is
#: already a long scroll of dispatch traffic; past it the reader wants the Calls page.
DEFAULT_MINUTES = 15
MAX_MINUTES = 240

#: Rows per answer.  The embed keeps 100 by default; 500 is the ceiling for a busy window.
DEFAULT_LIMIT = 200
MAX_LIMIT = 500

#: The columns a row prints, taken *from* the calls list's own column list by name, so a
#: talkgroup alias or a radio label here is by construction the one the Calls page shows.
ROW_COLUMNS = (
    "id",
    "started_at",
    "started_local",
    "duration_s",
    "tgid",
    "talkgroup_alias",
    "radio_label",
    "transcript_text",
    "transcript_status",
    "transcript_quality",
)


def _column(name: str) -> Any:
    for column in CALL_COLUMNS:
        if getattr(column, "key", None) == name:
            return column
    raise LookupError(f"CALL_COLUMNS has no column {name!r}")  # pragma: no cover - import-time bug


_SELECTED = tuple(_column(name) for name in ROW_COLUMNS)


def preset_categories(db: Session, slug: str) -> tuple[str, ...] | None:
    """The categories a built-in feed covers — ``None`` for ``all``; 404 for no such feed.

    Every vocabulary category counts as a feed here, in use or not (see below).

    A composite feed (``emergency``) contributes its whole category list, not only the
    categories the directory files a talkgroup under today: a category nobody uses matches
    no talkgroup, so the answer is the same, and a category somebody files a channel under
    tomorrow is in the embed from that moment without anyone editing a URL.
    """
    wanted = slug.strip().lower()
    if wanted == PRESET_ALL:
        return None
    for composite, _name, _description, categories in COMPOSITE_PRESETS:
        if composite == wanted:
            return tuple(categories)
    for preset in feed_presets(db, include_audio=False):
        if preset.slug == wanted:
            return tuple(preset.query.get("category") or ())
    # A category in the vocabulary that no talkgroup is filed under *today* has no feed,
    # but an embed pasted into somebody's page must not break because of it: it is an
    # empty list, and it fills the moment a channel is filed there.
    if wanted in TALKGROUP_CATEGORIES:
        return (wanted,)
    raise HTTPException(status_code=404, detail=f"no built-in feed {slug}")


def displayable() -> list[Any]:
    """The view's row rule: a finished transcript that says something.

    ``done`` is the status a transcript that kept its text ends in; ``empty`` is what the
    hallucination check (and silence) leaves, and ``noise`` is its verdict on the text.
    """
    return [
        Call.transcript_status == "done",
        Call.transcript_text.is_not(None),
        func.length(func.trim(Call.transcript_text)) > 0,
        Call.transcript_quality.is_distinct_from("noise"),
    ]


def live_filter(f: CallFilters) -> dict[str, Any]:
    """The filter as a ``WS /api/live`` first frame: no dates (the feed rolls), no sort."""
    frame = f.replace(date_from=None, date_to=None).echo()
    frame.pop("sort", None)
    return frame


@router.get(
    "/recent",
    response_model=RecentTranscriptsOut,
    summary="Recent transcripts for the embeddable live view",
    description=(
        "The last `minutes` (default 15, at most 240) of transcribed calls matching the filter, "
        "newest first, with only the fields `/embed/transcripts` prints. Every `GET /calls` "
        "filter parameter works and means the same thing; `preset=<slug>` starts from a "
        "built-in feed (`emergency`, `all`, or a category) and supplies `category` when the "
        "query does not name one. Duplicates are always excluded, and so are calls with no "
        "finished transcript unless `show_pending=true`. The answer carries `filter`, the "
        "resolved filter to send as the first frame of `WS /api/live` to keep the list live. "
        "`next_cursor` continues the list into older calls: pass it back as `cursor` and the "
        "answer is the next `limit` rows before it, with no `minutes` bound — how the view "
        "scrolls past its first screen. It is null once a cursor page comes back short."
    ),
)
def recent_transcripts(  # noqa: PLR0913 - the embed's URL parameters, one each
    f: Filters,
    db: DbSession,
    response: Response,
    preset: Annotated[
        str | None,
        Query(max_length=64, description="A built-in feed to start from: `emergency`, `all` or a category"),
    ] = None,
    minutes: Annotated[
        int, Query(ge=1, le=MAX_MINUTES, description="How far back to read, in minutes")
    ] = DEFAULT_MINUTES,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="At most this many rows")] = DEFAULT_LIMIT,
    show_pending: Annotated[
        bool, Query(description="Include calls whose transcript is missing, pending or rejected")
    ] = False,
    presets: Annotated[
        bool, Query(description="Also list the built-in feeds, for a filter bar")
    ] = False,
    cursor: Annotated[
        str | None,
        Query(
            max_length=200,
            description="`next_cursor` from a previous answer: the older rows, unbounded by `minutes`",
        ),
    ] = None,
) -> RecentTranscriptsOut:
    slug = preset.strip().lower() if preset and preset.strip() else None
    if slug is not None and not f.category:
        categories = preset_categories(db, slug)
        if categories is not None:
            f = f.replace(category=categories)
    elif slug is not None:
        preset_categories(db, slug)  # still a 404 for a slug that does not exist
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    # A cursor page reads on past the window: the first screen is the last few minutes, and
    # scrolling to its end asks for what came before, however far back that is.
    f = f.replace(
        date_from=None if cursor else since, date_to=None, include_duplicates=False, sort="-started_at"
    )
    resolved = resolve_filter_sets(db, f)
    stmt = base_call_select(resolved, *_SELECTED)
    if cursor:
        stmt = stmt.where(cursor_condition(resolved, cursor))
    if not show_pending:
        stmt = stmt.where(*displayable())
    rows = db.execute(stmt.order_by(*order_by(resolved)).limit(limit)).all()
    if rows:
        last = rows[-1]._mapping
        next_cursor: str | None = encode_cursor(last["started_at"], last["id"])
    else:
        # An empty first screen still has a past: continue from the window's start.
        next_cursor = None if cursor else encode_cursor(since, 0)
    if cursor and len(rows) < limit:
        next_cursor = None
    response.headers["Cache-Control"] = "no-store"
    return RecentTranscriptsOut(
        minutes=minutes,
        since=since,
        preset=slug,
        filter=live_filter(f),
        next_cursor=next_cursor,
        items=[RecentTranscriptOut.model_validate(dict(row._mapping)) for row in rows],
        presets=(
            [
                EmbedPresetOut(slug=p.slug, name=p.name.removeprefix("Keyup · "))
                for p in feed_presets(db, include_audio=False)
            ]
            if presets
            else None
        ),
    )


__all__ = ["DEFAULT_MINUTES", "MAX_MINUTES", "preset_categories", "router"]
