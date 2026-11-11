"""Editing radio systems: the shared row builder, the delete guard, and the merge.

A system is the root of the directory — every site, talkgroup, radio, patch and call hangs
off one — and every one of those foreign keys is ``ON DELETE CASCADE``.  That makes an
unguarded ``DELETE`` on this table the single most destructive statement in the schema: it
would take a million recordings with it and report success.  :func:`system_blockers` is
therefore not a nicety, it is the guard, and it is read off the schema metadata
(:func:`app.repair.referrers`) rather than written out by hand so a table added later
cannot quietly fall outside it.

:func:`merge_systems` is the operator's version of the repair that fixed production.
:mod:`app.repair` already folds one system into another in three phases — classify and
re-parent, repoint in committed batches, then sum and delete — and the only thing about it
that was ever specific to the ``upload`` phantom was how the two systems got chosen.  So
this module chooses them (from an API request rather than from a name) and reuses every
phase; there is deliberately no second implementation of the folding here.

What that inherits, and what the endpoint's documentation promises:

* A child with a twin under the target — a site of the same name, a talkgroup with the
  same ``tgid``, a radio with the same ``(radio_id, wacn, home_system_id)`` — is folded
  into it: counters summed, blanks filled, the duplicate dropped.  One with no twin is
  simply re-parented, because it is a real row that only landed under the wrong system.
* **Calls are never deleted.**  ``calls`` has no unique key over any of its foreign keys,
  so a call can only ever be *moved*; the rows a merge does drop are the ones a unique key
  would otherwise be violated by, and every one of them says what its survivor says.
* The repointing runs in committed batches under ascending advisory locks, so a merge of a
  million calls does not hold the live feed off (:func:`app.repair._run_batch`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import repair
from app.models import Call, Site, System, Talkgroup
from app.schemas import SystemOut

log = logging.getLogger("app.services.systems")

__all__ = [
    "MergeRefused",
    "hold_calls_flag",
    "merge_systems",
    "system_blockers",
    "system_out",
    "system_outs",
    "trunked_ids",
]


class MergeRefused(ValueError):
    """The two systems cannot be merged — the same system twice."""


# --------------------------------------------------------------------------------------
# the row
# --------------------------------------------------------------------------------------


def system_out(
    db: Session,
    row: System,
    *,
    call_count: int | None = None,
    site_count: int | None = None,
    blocking: dict[str, int] | None = None,
) -> SystemOut:
    """One ``SystemOut``, with its counts and whether it could be deleted.

    The single place a ``SystemOut`` is built, for the same reason
    :func:`app.services.sites.site_out` is: ``deletable``/``blocking`` are answered from
    the counts the delete endpoint enforces, so the button the SPA enables and the check
    the API makes cannot drift apart.
    """
    if blocking is None:
        blocking = system_blockers(db, row.id)
    if call_count is None:
        call_count = int(
            db.scalar(select(func.count()).select_from(Call).where(Call.system_id == row.id)) or 0
        )
    if site_count is None:
        site_count = int(
            db.scalar(select(func.count()).select_from(Site).where(Site.system_id == row.id)) or 0
        )
    return SystemOut(
        id=row.id,
        name=row.name,
        wacn=row.wacn,
        system_id=row.system_id,
        protocol=row.protocol,
        trunked=row.trunked,
        is_trunked=row.is_trunked,
        description=row.description,
        rr_system_id=row.rr_system_id,
        rr_synced_at=row.rr_synced_at,
        call_count=call_count,
        site_count=site_count,
        deletable=not blocking,
        blocking=blocking,
    )


def system_outs(db: Session, rows: list[System]) -> list[SystemOut]:
    """Every row of the listing, with the counts gathered in one pass each.

    ``blocking`` still costs a count per referring table per system, which is a dozen
    cheap indexed counts over a handful of systems — an archive has two or three, not
    thousands — and it is what lets the Sites page disable a delete it knows would 409.
    """
    if not rows:
        return []
    calls = dict(db.execute(select(Call.system_id, func.count()).group_by(Call.system_id)).all())
    sites = dict(db.execute(select(Site.system_id, func.count()).group_by(Site.system_id)).all())
    return [
        system_out(
            db,
            row,
            call_count=int(calls.get(row.id, 0)),
            site_count=int(sites.get(row.id, 0)),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------------------
# what holds a system
# --------------------------------------------------------------------------------------


def system_blockers(db: Session, system_id: int) -> dict[str, int]:
    """What still references a system, as ``{table: rows}`` — empty when it can be deleted.

    Every foreign key in the schema that points at ``systems.id`` is ``ON DELETE CASCADE``,
    so Postgres would happily answer this delete by removing a million calls.  The listing
    is read off the metadata rather than enumerated here: a table added to the schema next
    year joins this guard the moment its foreign key does, which is the opposite of how a
    hand-written list ages.
    """
    counts: dict[str, int] = {}
    for table, column in repair.referrers("systems"):
        rows = db.scalar(select(func.count()).select_from(table).where(column == system_id)) or 0
        if rows:
            counts[table.name] = int(rows)
    return counts


# --------------------------------------------------------------------------------------
# the merge
# --------------------------------------------------------------------------------------


def merge_systems(
    db: Session,
    source: System,
    target: System,
    *,
    batch_size: int = repair.DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Fold ``source`` into ``target`` and delete it.  The three phases, on any two systems.

    Unlike :func:`app.services.sites.merge_sites` this one is *allowed* across the boundary
    it moves — that is the whole operation — and it is safe precisely because the phases
    below split the talkgroups and radios first: a call arrives under the target already
    pointing at the target's twin of its talkgroup, never at another system's.

    Returns the same shape the phantom repair reports, minus its dry-run scaffolding:
    ``rows_repointed``, ``duplicates_dropped`` and ``deleted``.
    """
    if source.id == target.id:
        raise MergeRefused("a system cannot be merged into itself")

    source_id, source_name = source.id, source.name
    fold = repair.map_children(db, source, target)
    result: dict[str, Any] = {
        "reparented": {
            table: len(rows) for table, rows in fold.reparented.items() if rows
        },
        "merged": {table: len(ids) for table, ids in fold.twins.items() if ids},
    }
    result.update(repair.repoint_phantom_rows(db, fold, batch_size=batch_size))
    result.update(repair.finish_phantom_merge(db, fold))
    log.info(
        "systems: merged %r (%s) into %r (%s); %d rows repointed",
        source_name,
        source_id,
        target.name,
        target.id,
        sum(result["rows_repointed"].values()),
    )
    return result


# --------------------------------------------------------------------------------------
# trunked or not — the one thing Broadcastify Calls refuses to be talked out of
# --------------------------------------------------------------------------------------


def trunked_ids(db: Session, system_ids: Iterable[int]) -> set[int]:
    """Which of those ``systems.id`` are trunked, per :attr:`app.models.System.is_trunked`."""
    wanted = {int(value) for value in system_ids if value is not None}
    if not wanted:
        return set()
    rows = db.scalars(select(System).where(System.id.in_(sorted(wanted)))).all()
    return {row.id for row in rows if row.is_trunked}


def hold_calls_flag(db: Session, rows: Sequence[Talkgroup]) -> int:
    """Force ``export_bcfy_calls`` false on any talkgroup whose system is not trunked.

    Broadcastify **Calls** publishes trunked systems only, and the gate refuses such a call
    whatever the checkbox says.  Rather than leave a ticked box that cannot publish — which
    every screen showing it would then have to explain — the write paths hold the column at
    false, and the UI renders it disabled with the reason.  Returns how many rows it had to
    correct, which is what the copy endpoint reports as ``skipped_not_trunked``.
    """
    if not rows:
        return 0
    allowed = trunked_ids(db, {row.system_id for row in rows})
    held = 0
    for row in rows:
        if row.system_id not in allowed and row.export_bcfy_calls:
            row.export_bcfy_calls = False
            held += 1
    return held
