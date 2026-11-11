"""Full-text search over the archive (§4.3).

The same filter set as ``GET /calls``, but ranked: results come back in ``ts_rank_cd``
order with a ``ts_headline`` excerpt marking the terms that matched.  The index is
``calls.search_tsv``, a stored generated column over the transcript, the user's notes and
the talkgroup alias recorded on the file — so a call that has not been transcribed yet is
still findable by the channel it was on, and the excerpt says so.

**Fuzzy search.**  ``fuzzy=true`` (a filter, so ``/calls``, facets, feeds and saved
searches honour it too) widens each word of ``q`` to the spellings the archive's own
vocabulary holds — see app/services/fuzzy.py.  ``fallback=true``, which the Search page
sends, does that *only* when the exact query matched nothing, and says so in ``fuzzy``.
Only then, and not "when there are few results": a threshold above zero would change what
an exact search returns, and a search that already found three calls of Melrose must not
quietly start returning fourteen.  When some exact results exist, the page offers
widening as a click instead.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.filters import CallFilters, Filters, Paging, resolve_filter_sets
from app.api.search_load import SEARCH_LOAD
from app.db import get_session
from app.schemas import FacetsOut, FuzzyOut, FuzzyTermOut, SearchHit, SearchResponse, Suggestion
from app.services import calls as call_service

router = APIRouter(prefix="/search", tags=["search"])
DbSession = Annotated[Session, Depends(get_session)]


@router.get(
    "",
    dependencies=SEARCH_LOAD,
    response_model=SearchResponse,
    summary="Ranked full-text search",
    description=(
        "Accepts every `GET /calls` filter plus `q`, and returns hits ordered by `ts_rank_cd` with "
        "a `<mark>`-highlighted excerpt. websearch syntax works: `\"engine 1\" fire -test`. "
        "Without `q` this degrades to a plain filtered list in time order, so the Search page can "
        "show something before the reader types. `fuzzy=true` also matches spelling variants "
        "of the words in `q`; `fallback=true` does that only when the exact query matched "
        "nothing. Either way `fuzzy` in the response lists the variants that were used."
    ),
)
def search(
    f: Filters,
    paging: Paging,
    db: DbSession,
    response: Response,
    facets: Annotated[bool, Query(description="Include facet counts for the same filter")] = True,
    fallback: Annotated[
        bool,
        Query(
            description=(
                "When the exact `q` matches nothing, run it again with `fuzzy=true` and set "
                "`fuzzy.automatic`. A query with any exact match is returned exactly. "
                "Default false."
            )
        ),
    ] = False,
) -> SearchResponse:
    f = resolve_filter_sets(db, f)
    hits, total, capped = call_service.search_calls(db, f, paging)
    automatic = False
    if fallback and f.q and not f.fuzzy and total == 0:
        widened = resolve_filter_sets(db, f.replace(fuzzy=True))
        automatic = True
        if widened._fuzzy is not None and widened._fuzzy.widened:
            f = widened
            hits, total, capped = call_service.search_calls(db, f, paging)
        else:
            f = widened  # nothing close either; report the words that were looked up
    response.headers["X-Total-Count"] = str(total)
    return SearchResponse(
        items=[SearchHit(call=call, highlight=highlight, rank=rank) for call, highlight, rank in hits],
        total=total,
        total_capped=capped,
        page=paging.page,
        page_size=paging.page_size,
        facets=call_service.facets(db, f) if facets else FacetsOut(),
        fuzzy=_fuzzy_out(f, automatic=automatic),
    )


def _fuzzy_out(f: CallFilters, *, automatic: bool) -> FuzzyOut | None:
    if not (f.q and f.fuzzy and f._fuzzy is not None):
        return None
    return FuzzyOut(
        automatic=automatic,
        widened=f._fuzzy.widened,
        terms=[FuzzyTermOut(term=t.term, variants=list(t.variants)) for t in f._fuzzy.terms],
    )


@router.get(
    "/suggest",
    response_model=list[Suggestion],
    summary="Prefix suggestions for the search box",
    description=(
        "Talkgroup aliases, roster units and personnel, radio unit ids and aliases, and user tags "
        "matching `q`, busiest first. Each carries its `kind`, so the search box can say what it is "
        "offering. With `fuzzy=true`, any room left under `limit` is filled with aliases, "
        "callsigns and names *spelled like* `q` (pg_trgm word similarity), marked `fuzzy`."
    ),
)
def suggest(
    db: DbSession,
    q: Annotated[str, Query(min_length=1, description="What the reader has typed so far")],
    limit: Annotated[int, Query(ge=1, le=50)] = 12,
    fuzzy: Annotated[
        bool,
        Query(
            description=(
                "After every substring match, add near spellings (at least 4 characters typed). "
                "Default false."
            )
        ),
    ] = False,
) -> list[Suggestion]:
    return [Suggestion(**row) for row in call_service.suggestions(db, q, limit, fuzzy=fuzzy)]


__all__ = ["router"]
