"""Editing tower sites: the shared row builder, and the merge (§4.2).

A site is the one directory row whose name does not identify it.  ``sites`` is unique on
``(system_id, name)``, so two systems can both have an ``Eastside`` — and real archives
have exactly that, which is how a school district's traffic ended up filed under the county
network in the first place (:mod:`app.refile`).  :func:`site_out` is therefore the *only*
place a ``SiteOut`` is built: it carries ``system_name`` on every row so no client is ever
handed a bare site name to render, and it answers ``deletable``/``blocking`` from the same
counts the delete endpoint enforces, so the button the SPA enables and the check the API
makes cannot drift apart.

:func:`merge_sites` is the other half of the Sites page's cleanup story.  Two rows for one
tower — a rename that landed as a second site, a recorder that spelled it differently —
are folded into one: every row that names the source is repointed at the target, the
target's blanks are filled from the source, and the emptied source is deleted.  Calls are
repointed in committed batches for the same reason :mod:`app.repair` does it, and refusing
a merge across systems is not a limitation but the point — moving calls *between* systems
is a re-filing, and re-filing has its own command with its own talkgroup and radio twins.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.orm import Session, aliased

from app.models import (
    AdjacentSite,
    Call,
    RadioEvent,
    RecorderGap,
    RecorderObservation,
    Site,
    SiteFrequency,
    System,
)
from app.refile import site_blockers
from app.schemas import SiteOut
from app.services import site_profile

log = logging.getLogger("app.services.sites")

__all__ = [
    "COUNT_CACHE_TTL_S",
    "MergeRefused",
    "call_counts",
    "forget_call_counts",
    "merge_sites",
    "site_out",
    "site_outs",
]

#: Calls repointed per committed transaction.  A merge is an interactive request, so this
#: is smaller than a repair's: a site with a million calls is folded in many short
#: transactions rather than one long one holding the live feed off.
MERGE_BATCH_SIZE = 5_000

#: Filled onto the target from the source, and only where the target has nothing.  Same
#: rule as :data:`app.repair._FILL_BLANKS`: what an operator or an RR sync put on the
#: surviving row always wins, and the row being deleted may only fill in a blank.
_FILL_BLANKS = ("site_number", "description", "notes", "nac", "rfss", "lra", "rr_site_id")


class MergeRefused(ValueError):
    """The two sites cannot be merged — different systems, or the same site twice."""


# --------------------------------------------------------------------------------------
# the per-site call counts
# --------------------------------------------------------------------------------------

#: How long the whole ``{site_id: calls}`` map is reused for.  The Sites page shows a
#: count beside a tower name; nobody is watching it tick, and the archive grows by a few
#: rows a second, so an answer up to a minute old is the same answer.  What this buys is
#: that a page which refetches — on a mutation, on a live frame, on a remount — costs one
#: grouped index scan a minute rather than one per request.  The exact count is still
#: available per site from :func:`site_out` when nothing is passed in, which is the path
#: ``GET /sites/{id}`` takes.
COUNT_CACHE_TTL_S = 60.0

_counts_lock = threading.Lock()
_counts_cache: tuple[float, dict[int, int]] | None = None


def forget_call_counts() -> None:
    """Drop the cached counts, so the next listing asks the database again.

    Called where a write has just moved a large number of calls between sites — a merge, a
    delete — because those are the moments an operator is looking straight at the number
    and would read a minute-old one as the edit having failed.
    """
    global _counts_cache
    with _counts_lock:
        _counts_cache = None


def call_counts(db: Session, *, ttl: float | None = None) -> dict[int, int]:
    """``{site_id: calls}`` for every site, from cache when it is fresh enough.

    One grouped count for the whole archive rather than one per site: with
    ``ix_calls_site_recorded`` this is an index-only scan, and it is asked at most once
    every :data:`COUNT_CACHE_TTL_S` seconds however many listings arrive in between.
    """
    global _counts_cache
    window = COUNT_CACHE_TTL_S if ttl is None else ttl
    now = time.monotonic()
    with _counts_lock:
        cached = _counts_cache
    if cached is not None and now - cached[0] < window:
        return cached[1]
    counts = {
        int(site_id): int(rows)
        for site_id, rows in db.execute(
            select(Call.site_id, func.count())
            .where(Call.site_id.is_not(None))
            .group_by(Call.site_id)
        )
    }
    with _counts_lock:
        _counts_cache = (time.monotonic(), counts)
    return counts


# --------------------------------------------------------------------------------------
# the row
# --------------------------------------------------------------------------------------


#: "the caller did not pass one", which has to be told apart from "the caller passed one
#: and this site has no services".  A plain ``None`` default cannot say both.
_UNASKED: Any = object()


def site_out(
    db: Session,
    row: Site,
    *,
    call_count: int | None = None,
    system_name: str | None = None,
    blocking: dict[str, int] | None = None,
    services_available: Any = _UNASKED,
) -> SiteOut:
    """One ``SiteOut``, with the system's name and whether it could be deleted.

    ``call_count``, ``system_name``, ``blocking`` and ``services_available`` may be passed
    in by a list endpoint that already has them grouped, so listing N sites stays a handful
    of queries rather than 3N.  Left out, each is fetched for this row alone.
    """
    if services_available is _UNASKED:
        services_available = site_profile.latest_services(db, [row.id]).get(row.id)
    if blocking is None:
        blocking = site_blockers(db, row.id)
    if system_name is None:
        system_name = db.scalar(select(System.name).where(System.id == row.system_id))
    if call_count is None:
        call_count = int(
            db.scalar(select(func.count()).select_from(Call).where(Call.site_id == row.id)) or 0
        )
    return SiteOut(
        id=row.id,
        system_id=row.system_id,
        system_name=system_name,
        name=row.name,
        site_number=row.site_number,
        description=row.description,
        notes=row.notes,
        call_count=call_count,
        nac=row.nac,
        rfss=row.rfss,
        lra=row.lra,
        rr_site_id=row.rr_site_id,
        rr_synced_at=row.rr_synced_at,
        services_available=services_available,
        deletable=not blocking,
        blocking=blocking,
    )


#: The tuner blocker, asked of every site at once.  ``tuners.desired``/``reported`` are
#: JSONB documents rather than columns (:func:`app.refile.site_blockers` explains why the
#: check has to look there at all), so the site id comes back as text and is cast; a
#: document that names no site, or names something that is not a number, contributes
#: nothing.  Both halves are unioned and the tuners counted ``DISTINCT``, which is exactly
#: the ``desired = $1 OR reported = $1`` the per-site check asks: a tuner told to focus a
#: site *or* reporting it blocks that site, and one doing both still blocks it once.
_TUNER_BLOCKERS = text(
    "SELECT named.site_id::bigint AS site_id, count(DISTINCT named.id) AS rows_found FROM ("
    "  SELECT id, desired ->> 'site_id' AS site_id FROM tuners"
    "  UNION ALL"
    "  SELECT id, reported ->> 'site_id' AS site_id FROM tuners"
    ") named WHERE named.site_id ~ '^[0-9]+$' GROUP BY 1"
)


def site_outs(db: Session, rows: list[Site]) -> list[SiteOut]:
    """Every row of a listing, with the per-site counts gathered in one pass each.

    Six grouped counts and one system lookup for the whole page — never a query *per site*.
    That distinction is the whole point of this function and it was quietly lost: the
    tuner blocker used to be asked one site at a time through
    :func:`~app.refile.site_blockers`, which answers *all five* blockers, so listing N
    sites ran N sequential scans of ``calls`` on top of the grouped one.  On the production
    archive (1.1 M calls, no index on ``site_id``) that was the 60-78 second ``GET /sites``.
    The tuner count is now one grouped query like the rest, and the call counts come from
    :func:`call_counts`, which is cached for a minute.
    """
    ids = [row.id for row in rows]
    if not ids:
        return []
    wanted = set(ids)
    systems = dict(db.execute(select(System.id, System.name)).all())
    calls = call_counts(db)
    blocking: dict[int, dict[str, int]] = {site_id: {} for site_id in ids}
    for label, column in (
        ("recorder_observations", RecorderObservation.site_id),
        ("radio_events", RadioEvent.site_id),
        ("site_frequencies", SiteFrequency.site_id),
        ("adjacent_sites", AdjacentSite.site_id),
    ):
        for site_id, rows_found in db.execute(
            select(column, func.count()).where(column.in_(ids)).group_by(column)
        ):
            if rows_found:
                blocking[site_id][label] = int(rows_found)
    for site_id, rows_found in db.execute(_TUNER_BLOCKERS):
        if rows_found and site_id in wanted:
            blocking[site_id]["tuners"] = int(rows_found)
    # One DISTINCT ON for the page: the services column is what the Sites card shows
    # without expanding a row, and asking per site would undo the whole point of this
    # function.
    services = site_profile.latest_services(db, ids)
    return [
        site_out(
            db,
            row,
            call_count=int(calls.get(row.id, 0)),
            system_name=systems.get(row.system_id),
            services_available=services.get(row.id),
            # ``calls`` blocks a delete like anything else, and it is the same number the
            # row already shows — no second count for it.
            blocking=(
                {"calls": calls[row.id], **blocking[row.id]}
                if calls.get(row.id)
                else blocking[row.id]
            ),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------------------
# the merge
# --------------------------------------------------------------------------------------


def _repoint_calls(db: Session, source_id: int, target_id: int, batch_size: int) -> tuple[int, int]:
    """Move every call off the source site, a committed batch at a time.

    Batched by primary key exactly as :func:`app.repair._repoint_table` batches: a moved
    call drops out of the next batch's filter, so an interrupted merge has nothing to
    resume from and nothing to lose — the calls already moved are already on the target.
    ``calls`` has no unique key over ``site_id``, so nothing can collide on the way across
    and no call is ever dropped.
    """
    moved = batches = 0
    while True:
        ids = list(
            db.scalars(
                select(Call.id).where(Call.site_id == source_id).order_by(Call.id).limit(batch_size)
            )
        )
        if not ids:
            break
        moved += int(
            db.execute(update(Call).where(Call.id.in_(ids)).values(site_id=target_id)).rowcount
        )
        db.commit()
        batches += 1
    return moved, batches


def merge_sites(
    db: Session, source: Site, target: Site, *, batch_size: int = MERGE_BATCH_SIZE
) -> dict[str, Any]:
    """Fold ``source`` into ``target`` and delete it.  Both must be on the same system.

    Refuses across systems: a site's calls carry that system's talkgroups and radios, and
    moving them to another system's site would leave every one of them pointing at a
    talkgroup on a system the call no longer belongs to.  That operation exists — it is
    ``keyup refile-calls``, which splits the talkgroups and radios off onto twins first —
    and it is not something a two-click merge should do by accident.

    Everything that names the source is repointed rather than deleted, except where a
    unique key forbids two: ``site_frequencies`` is unique on
    ``(site_id, frequency_hz, source)`` and ``adjacent_sites`` on its identity, so a row
    the target already has is dropped instead of moved.  The target's blanks are filled
    from the source (never the other way — the survivor's own values win), the source is
    deleted, and the counts are returned.
    """
    if source.id == target.id:
        raise MergeRefused("a site cannot be merged into itself")
    if source.system_id != target.system_id:
        raise MergeRefused(
            f"site {source.id} is on system {source.system_id} and site {target.id} is on "
            f"system {target.system_id}; merging across systems would leave every call "
            "pointing at another system's talkgroups — use `keyup refile-calls` instead"
        )

    source_id, target_id = source.id, target.id
    moved: dict[str, int] = {}
    calls, batches = _repoint_calls(db, source_id, target_id, batch_size)
    if calls:
        moved["calls"] = calls

    # The two tables with a unique key that spans site_id: a row the target already holds
    # says the same thing as the one coming across, so the duplicate is dropped rather than
    # moved.  The whole key is compared — the frequency *and* the source that reported it,
    # the neighbor's whole identity — because half a key matching means nothing.
    for label, model, rival, key in (
        ("site_frequencies", SiteFrequency, aliased(SiteFrequency), ("frequency_hz", "source")),
        (
            "adjacent_sites",
            AdjacentSite,
            aliased(AdjacentSite),
            ("system_id_p25", "rfss", "site_number"),
        ),
    ):
        same_key = [
            getattr(rival, name).is_not_distinct_from(getattr(model, name)) for name in key
        ]
        dropped = int(
            db.execute(
                delete(model).where(
                    model.site_id == source_id,
                    exists(select(1).where(rival.site_id == target_id, *same_key)),
                )
            ).rowcount
        )
        if dropped:
            moved[f"{label}_dropped"] = dropped

    for label, model, column in (
        ("site_frequencies", SiteFrequency, SiteFrequency.site_id),
        ("adjacent_sites", AdjacentSite, AdjacentSite.site_id),
        ("recorder_observations", RecorderObservation, RecorderObservation.site_id),
        ("radio_events", RadioEvent, RadioEvent.site_id),
    ):
        rows = int(
            db.execute(
                update(model)
                .where(column == source_id)
                .values({column.key: target_id})
                .execution_options(synchronize_session=False)
            ).rowcount
        )
        if rows:
            moved[label] = rows

    # Recorder gaps are per system, not per site, so nothing there names the source; the
    # column is read here only so a schema that grows one is not silently missed.
    _ = RecorderGap

    for name in _FILL_BLANKS:
        if getattr(target, name, None) is None and getattr(source, name, None) is not None:
            setattr(target, name, getattr(source, name))

    db.flush()
    db.execute(delete(Site).where(Site.id == source_id))
    db.commit()
    # A merge is exactly when somebody is watching the count: it just moved every one of
    # the source's calls onto the target, and a cached map would show the old split.
    forget_call_counts()
    log.info(
        "sites: merged %r (%s) into %r (%s); %s",
        source.name,
        source_id,
        target.name,
        target_id,
        ", ".join(f"{rows} {label}" for label, rows in sorted(moved.items())) or "nothing to move",
    )
    return {"moved": moved, "batches": batches}
