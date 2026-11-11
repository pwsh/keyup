"""Archive repairs — the one-off fixes for damage a bug already did.

Unlike :mod:`app.retention` or :mod:`app.arbiter` nothing here runs on a schedule.  These
are operator commands, run by hand once, and every one of them is written to be **safe to
run twice**: a second pass over a repaired archive finds nothing to do and says so.

Today there is one, :func:`merge_phantom_system`, which cleans up after the push path's
old habit of inventing a ``System`` named after a placeholder (see
:func:`app.ingest.service.resolve_system_id` for the bug it repairs).

The repair runs in three phases, and the split is the whole point of this module's shape.
The phantom in production holds a million calls; doing the work in one transaction meant
holding a row lock on the *real* Central site and the *real* talkgroups from the first
second to the last, so every live upload's ``INSERT INTO sites … ON CONFLICT`` and every
transcription's ``UPDATE calls`` queued behind the repair.  Instead:

* **map** (:func:`map_phantom_children`) — one short transaction that touches only rows
  the phantom owns.  Every phantom child either has a twin under the real system, in which
  case it is recorded in a mapping, or it has none, in which case it is re-parented on the
  spot.  No target row is read for update, so nothing live waits on it.
* **repoint** (:func:`repoint_phantom_rows`) — the long phase, run in batches keyed by
  primary key with a ``COMMIT`` after each one.  A batch holds locks on at most
  ``batch_size`` phantom-owned rows for the fraction of a second it takes to write them,
  and between batches the live feed has the tables to itself.  Where a unique key covers
  the column being moved, the batch first deletes the rows that key would end up with two
  of; see :func:`_drop_losers` for how a key spanning *two* folded columns is read.
* **finish** (:func:`finish_phantom_merge`) — one short transaction that sums the
  counters, fills the real rows' blanks, and drops the emptied phantom.  This is the only
  moment a target row is locked, and it lasts milliseconds.

Every phase is resumable, because each one recognizes its own leftovers: a re-parented row
is no longer a phantom child, and a repointed row no longer matches the phantom ids.  Stop
the repair with Ctrl-C halfway through and run it again; it picks up where it stopped.

Nothing below :func:`_resolve` has ever cared *which* two systems it was handed — the
phantom is only a system whose name gave it away — so the three phases are also what
:func:`app.services.systems.merge_systems` runs when an operator merges two systems from
the Sites page.  That is why :func:`map_children` is split out of
:func:`map_phantom_children`, and why phase B takes the fold's conversation advisory locks
(:func:`_run_batch`): the operator's merge runs while the live feed is up.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    Table,
    UniqueConstraint,
    and_,
    case,
    delete,
    exists,
    func,
    select,
    update,
)
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Column

from app.models import Base, Patch, Radio, Site, System, Talkgroup
from app.refile import DEADLOCK_BACKOFF_S, DEADLOCK_RETRIES, is_deadlock, lock_talkgroups

log = logging.getLogger("app.repair")

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "PHANTOM_SYSTEM_NAME",
    "Fold",
    "RepairIncomplete",
    "classify",
    "finish_phantom_merge",
    "map_children",
    "map_phantom_children",
    "merge_phantom_system",
    "plan_phantom_merge",
    "referrers",
    "repoint_phantom_rows",
]

#: The name the old push path fell back to when a recorder sent no ``systemLabel``.
PHANTOM_SYSTEM_NAME = "upload"

#: Rows repointed per transaction.  Small enough that a batch's locks are invisible to the
#: live feed, large enough that a million calls is a few dozen round trips.
DEFAULT_BATCH_SIZE = 20_000

#: Identity columns copied from a discarded duplicate onto the row that survives it, but
#: only where the survivor has nothing.  What an operator (or a RadioReference sync) put on
#: the real row always wins; the duplicate is only allowed to fill in blanks.
_FILL_BLANKS: dict[str, tuple[str, ...]] = {
    "sites": ("site_number", "description", "nac", "rfss", "lra", "rr_site_id"),
    "talkgroups": ("alias", "group_name", "description", "category", "service_tag", "notes"),
    "radios": ("alias", "unit_id", "notes"),
    "patches": (),
}

#: Counters summed across a merge rather than picked from one side.
_SUM_COLUMNS: dict[str, tuple[str, ...]] = {
    "talkgroups": ("call_count",),
    "radios": ("call_count",),
    "patches": ("call_count",),
}

#: The children of a system, and the natural key that decides whether a phantom copy is a
#: duplicate of a real row or a real row that only landed under the wrong parent.  Each
#: identity is exactly its table's unique key minus ``system_id``, so a re-parented row can
#: never collide on the way across.
_CHILDREN: tuple[tuple[Any, str, tuple[str, ...]], ...] = (
    (Site, "sites", ("name",)),
    (Talkgroup, "talkgroups", ("tgid",)),
    (Radio, "radios", ("radio_id", "wacn", "home_system_id")),
    (Patch, "patches", ("patch_id", "member_tgids")),
)
_CHILD_TABLES = frozenset(table for _, table, _ in _CHILDREN)

#: Called once per committed batch with a progress event; see :func:`repoint_phantom_rows`.
Progress = Callable[[dict[str, Any]], None]


class RepairIncomplete(RuntimeError):
    """Phase C was asked to finish a merge whose repointing has not finished."""


@dataclass
class Fold:
    """What the merge decided: which phantom rows have twins, and which moved house.

    This is plain data, re-derived from the archive on every run rather than stored, which
    is what makes an interrupted repair resumable: the rows re-parented last time are no
    longer phantom children, and the twins that are left classify exactly as before.
    """

    phantom_system_id: int
    target_system_id: int
    target_name: str
    #: ``{table: {phantom row id: surviving row id}}`` for the duplicates
    twins: dict[str, dict[int, int]] = field(default_factory=dict)
    #: ``{table: [{identity…, "id": id}]}`` for the rows that only changed parent
    reparented: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: ``{table: {phantom row id: {identity…}}}`` — the human-readable side of ``twins``
    labels: dict[str, dict[int, dict[str, Any]]] = field(default_factory=dict)

    @property
    def id_maps(self) -> dict[str, dict[int, int]]:
        """Every ``{old id: new id}`` the repointing has to apply, keyed by parent table."""
        return {**self.twins, "systems": {self.phantom_system_id: self.target_system_id}}

    def merged_rows(self, table: str) -> list[dict[str, Any]]:
        """The report's ``<table>_merged`` list — stable between the plan and the run."""
        return [
            {**self.labels[table][old], "from_id": old, "into_id": new}
            for old, new in self.twins.get(table, {}).items()
        ]


# --------------------------------------------------------------------------------------
# reading the schema
# --------------------------------------------------------------------------------------


def referrers(table_name: str) -> list[tuple[Table, Column[Any]]]:
    """Every ``(table, column)`` in the schema whose foreign key points at ``table_name``.

    Read off the metadata rather than written out by hand: a repair that silently misses a
    table added after it was written would orphan rows, and the whole point of this module
    is that it leaves nothing behind.
    """
    found: list[tuple[Table, Column[Any]]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == table_name and fk.column.name == "id":
                    found.append((table, column))
    return found


def _primary_key(table: Table) -> Column[Any]:
    """The single integer key every referring table is batched by."""
    return next(iter(table.primary_key.columns))


def _unique_keys(table: Table, column: Column[Any]) -> list[tuple[str, ...]]:
    """The unique keys that cover ``column`` — the ones a repoint can violate.

    Both halves of the schema count: a named ``UniqueConstraint`` and a plain
    ``Index(..., unique=True)`` constrain the archive identically, and a repair that only
    read the first would walk into the second.  Expression indexes
    (``uq_radio_events_identity``, keyed on ``COALESCE(site_id, 0)``) and partial ones
    (``uq_radio_assignments_current_unit``, only over the current assignments) are skipped
    here rather than half-understood: their keys cannot be compared by column name alone.
    :func:`_repoint_table` catches what they raise instead.
    """
    keys: list[tuple[str, ...]] = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and column.name in constraint.columns:
            keys.append(tuple(c.name for c in constraint.columns))
    for index in table.indexes:
        names = tuple(c.name for c in index.columns)
        if not index.unique or column.name not in names:
            continue
        if len(index.expressions) != len(names):  # keyed on an expression, not columns
            continue
        if index.dialect_kwargs.get("postgresql_where") is not None:  # partial
            continue
        keys.append(names)
    return keys


def _work(fold: Fold) -> Iterator[tuple[Table, Column[Any], dict[int, int]]]:
    """``(table, column, {old id: new id})`` for every referrer the repointing must visit.

    The four child tables' own ``system_id`` is skipped: phase A already re-parented the
    rows that move, and the ones left pointing at the phantom are the twins phase C
    deletes.  Repointing them here would only collide with the real row they duplicate.
    """
    maps = fold.id_maps
    for parent in ("sites", "talkgroups", "radios", "patches", "systems"):
        ids = maps.get(parent)
        if not ids:
            continue
        for table, column in referrers(parent):
            if parent == "systems" and table.name in _CHILD_TABLES and column.name == "system_id":
                continue
            yield table, column, ids


def _column_maps(fold: Fold) -> dict[str, dict[str, dict[int, int]]]:
    """``{table: {column: {old id: new id}}}`` — every rewrite the whole fold will make.

    A batch has to know about the columns *other* passes will move, not just its own: a
    unique key that spans two foreign keys is only safe once both of them are read the way
    the finished repair will leave them.  Keyed off :func:`_work` and nothing else, so the
    keys a batch computes and the writes the repair performs cannot drift apart.
    """
    maps: dict[str, dict[str, dict[int, int]]] = {}
    for table, column, ids in _work(fold):
        maps.setdefault(table.name, {})[column.name] = ids
    return maps


def _count(session: Session, table: Table, column: Column[Any], ids: dict[int, int]) -> int:
    """How many rows of ``table`` still point at one of ``ids``."""
    return session.scalar(
        select(func.count()).select_from(table).where(column.in_(list(ids)))
    ) or 0


# --------------------------------------------------------------------------------------
# choosing the two systems
# --------------------------------------------------------------------------------------


def _resolve(session: Session, name: str, report: dict[str, Any]) -> tuple[System, System] | None:
    """The phantom and the one system it can be folded into, or ``None`` with a reason.

    It **never** guesses: with no phantom, no other system, or more than one other system,
    it changes nothing and says why.
    """
    phantom = session.scalars(select(System).where(System.name == name)).first()
    if phantom is None:
        report["detail"] = f"no system named {name!r} — nothing to do"
        return None

    others = session.scalars(select(System).where(System.id != phantom.id).order_by(System.id)).all()
    if len(others) != 1:
        report["detail"] = (
            f"{len(others)} other systems exist; merging needs exactly one obvious target"
        )
        report["candidates"] = [{"id": s.id, "name": s.name} for s in others]
        return None

    report["system_id"] = phantom.id
    report["target"] = {"id": others[0].id, "name": others[0].name}
    return phantom, others[0]


def classify(session: Session, phantom: System, target: System) -> Fold:
    """Sort the phantom's children into duplicates and displaced originals.  Read only.

    A row whose identity already exists under the target system is a *twin* — the real
    Central site keeps its id, and everything hanging off the duplicate will be moved onto
    it.  A row with no counterpart is a *re-parent*: Eastside is a genuine neighbor site that
    only ever landed under the wrong parent, and deleting it would throw away real
    observations.
    """
    fold = Fold(phantom_system_id=phantom.id, target_system_id=target.id, target_name=target.name)
    for model, table_name, identity in _CHILDREN:
        twins: dict[int, int] = {}
        labels: dict[int, dict[str, Any]] = {}
        reparented: list[dict[str, Any]] = []
        rows = session.scalars(
            select(model).where(model.system_id == phantom.id).order_by(model.id)
        ).all()
        for row in rows:
            label = {name: getattr(row, name) for name in identity}
            where = [model.system_id == target.id]
            where += [
                getattr(model, name).is_not_distinct_from(getattr(row, name)) for name in identity
            ]
            twin = session.scalars(select(model).where(*where).order_by(model.id)).first()
            if twin is None:
                reparented.append({**label, "id": row.id})
            else:
                twins[row.id] = twin.id
                labels[row.id] = label
        fold.twins[table_name] = twins
        fold.labels[table_name] = labels
        fold.reparented[table_name] = reparented
    return fold


# --------------------------------------------------------------------------------------
# phase A — map
# --------------------------------------------------------------------------------------


def map_phantom_children(session: Session, *, name: str = PHANTOM_SYSTEM_NAME) -> Fold:
    """Phase A: classify the phantom's children and re-parent the ones with no twin.

    One short transaction.  Every row it writes is a row the phantom owns, addressed by
    primary key, so the only lock a live upload could contend for is on a row it has no
    reason to touch.  Raises :class:`LookupError` when the archive is not in a shape this
    repair understands; :func:`merge_phantom_system` reports that rather than raising.
    """
    report: dict[str, Any] = {}
    pair = _resolve(session, name, report)
    if pair is None:
        raise LookupError(report["detail"])
    return map_children(session, *pair)


def map_children(session: Session, source: System, target: System) -> Fold:
    """Phase A for any two systems: classify ``source``'s children, re-parent the singletons.

    Split out of :func:`map_phantom_children` so the operator-facing system merge
    (:mod:`app.services.systems`) is the same three phases as the phantom repair rather
    than a second implementation of them.  The phantom is only a system whose name gave it
    away; nothing below this line has ever cared which two systems it was handed.
    """
    fold = classify(session, source, target)
    for model, table_name, _ in _CHILDREN:
        moving = [row["id"] for row in fold.reparented[table_name]]
        if not moving:
            continue
        session.execute(update(model).where(model.id.in_(moving)).values(system_id=target.id))
        log.info("repair: re-parented %d %s onto %r", len(moving), table_name, target.name)
    session.commit()
    return fold


# --------------------------------------------------------------------------------------
# phase B — repoint
# --------------------------------------------------------------------------------------


def _mapped(selectable: Any, name: str, maps: dict[str, dict[int, int]]) -> Any:
    """``selectable.name`` as the finished fold will leave it — the column's *mapped* value.

    A column this fold repoints reads through its ``{old: new}`` map; every other column
    reads as itself.  Whether that column's own pass has already run or is still to come
    makes no difference to the answer, which is the entire point.
    """
    column = selectable.c[name]
    id_map = maps.get(name)
    if not id_map:
        return column
    return case(*[(column == old, new) for old, new in id_map.items()], else_=column)


def _same_key(
    table: Table,
    other: Any,
    names: tuple[str, ...],
    maps: dict[str, dict[int, int]],
) -> Any:
    """``other`` and ``table`` will hold the same key once the fold has finished.

    ``IS NOT DISTINCT FROM`` rather than ``=`` so that two rows agreeing on a NULL count as
    the same row, which is what a unique key itself does.
    """
    return and_(
        *[_mapped(other, name, maps).is_not_distinct_from(_mapped(table, name, maps)) for name in names]
    )


def _drop_losers(
    session: Session,
    table: Table,
    column: Column[Any],
    *,
    keys: list[Any],
    old_ids: list[int],
    maps: dict[str, dict[int, int]],
) -> int:
    """Delete the rows a *finished* fold would leave holding the same unique key.

    The comparison is between **fully mapped** keys: every column of the key that this
    fold repoints is read through its map, whether that column's pass has already run, is
    running now, or is still to come.  ``uq_radio_affiliations_radio_talkgroup`` is why —
    it spans ``radio_id_fk`` *and* ``talkgroup_id_fk``, and the fold moves both.  Read one
    column at a time against a row's *current* other columns, the archive looks different
    in every pass: a rival whose other column has not been repointed yet is invisible, the
    same rival is a duplicate once it has, and which of ``(A', X')``, ``(A', X)`` and
    ``(A, X')`` survives depends on which pass reaches them first.  That is the crack
    production fell through — the talkgroup pass left behind a pair the radio pass could
    only collide with.  Mapping the whole key sees one group of three from the first batch
    onwards, and the pass order stops mattering.

    Losers are decided by lowest primary key, which is deterministic and therefore
    re-runnable: an interrupted repair picks the same survivor the second time.  A rival is
    any row in the table, batch or not — a phantom radio's affiliation with a *real*
    talkgroup is nowhere in the talkgroup pass's batch and still collides with it — and a
    loser is deleted wherever it sits, as long as its key group reaches into this batch.

    Where the losing row is deleted rather than moved, the survivor already says the same
    thing.  ``calls`` has no unique key over any of its foreign keys, so a call is only
    ever *moved* — the repair must never lose a recording to a coincidence.
    """
    dropped = 0
    pk = _primary_key(table)
    for names in _unique_keys(table, column):
        rival = table.alias("rival")
        batched = table.alias("batched")
        # `keys` are the lowest primary keys still pointing at a phantom id, so the batch is
        # exactly the rows in that span that still do — a range the index can walk, rather
        # than twenty thousand bind parameters re-checked once per candidate row.
        in_batch = and_(
            batched.c[pk.name].between(keys[0], keys[-1]),
            batched.c[column.name].in_(old_ids),
        )
        dropped += session.execute(
            delete(table).where(
                exists(select(1).where(rival.c[pk.name] < pk, _same_key(table, rival, names, maps))),
                exists(select(1).where(in_batch, _same_key(table, batched, names, maps))),
            )
        ).rowcount
    return dropped


def _reason(error: IntegrityError) -> str:
    """The constraint an ``IntegrityError`` names, for the log line that reports it."""
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None) or str(error.orig)


def _repoint_rows(
    session: Session,
    table: Table,
    column: Column[Any],
    *,
    keys: list[Any],
    old_ids: list[int],
    whens: list[tuple[Any, int]],
) -> int:
    """Move every row of this batch that still points at a phantom id.  One statement."""
    return session.execute(
        update(table)
        .where(_primary_key(table).in_(keys), column.in_(old_ids))
        .values({column.name: case(*whens, else_=column)})
    ).rowcount


def _repoint_one_by_one(
    session: Session,
    table: Table,
    column: Column[Any],
    *,
    keys: list[Any],
    old_ids: list[int],
    whens: list[tuple[Any, int]],
    label: str,
) -> tuple[int, int]:
    """Belt and braces: retry a batch a row at a time, dropping the ones that will not go.

    :func:`_drop_losers` reads the unique keys it can read by column name, which leaves the
    expression and partial unique indexes — ``uq_radio_events_identity`` on
    ``COALESCE(site_id, 0)``, ``uq_radio_assignments_current_unit`` on the *current*
    assignments — able to surprise the batch update.  A surprise like that used to abort
    the whole repair mid-phase; now the batch is retried under a savepoint per row and the
    handful of rows that genuinely collide are deleted, loudly, like any other loser.
    """
    key = _primary_key(table)
    moved = dropped = 0
    for pk_value in keys:
        try:
            with session.begin_nested():
                moved += _repoint_rows(
                    session, table, column, keys=[pk_value], old_ids=old_ids, whens=whens
                )
        except IntegrityError as error:
            log.warning(
                "repair: %s — row %s still collides (%s); dropping it",
                label,
                pk_value,
                _reason(error),
            )
            with session.begin_nested():
                dropped += session.execute(delete(table).where(key == pk_value)).rowcount
    return moved, dropped


def _run_batch(
    session: Session,
    table: Table,
    column: Column[Any],
    *,
    keys: list[Any],
    old_ids: list[int],
    whens: list[tuple[Any, int]],
    maps: dict[str, dict[int, int]],
    locks: list[int],
    label: str,
) -> tuple[int, int]:
    """One committed batch: take the conversation locks, drop the losers, move the rest.

    The locks are the fold's talkgroups, both sides of every twin, taken in ascending id
    order by :func:`app.refile.lock_talkgroups`.  Live ingest holds exactly that lock while
    it writes a conversation row, and a batch here can make a call eligible to change
    conversation, so the two must ask for the same locks in the same order or they deadlock
    ABBA — the lesson :mod:`app.refile` was written from, applied to the merge that runs
    while the feed is up rather than the one an operator runs at 3am.
    """
    if locks:
        lock_talkgroups(session, locks)
    dropped = _drop_losers(session, table, column, keys=keys, old_ids=old_ids, maps=maps)
    try:
        with session.begin_nested():
            moved = _repoint_rows(session, table, column, keys=keys, old_ids=old_ids, whens=whens)
    except IntegrityError as error:
        log.warning(
            "repair: %s — batch of %d rows hit %s; retrying it row by row",
            label,
            len(keys),
            _reason(error),
        )
        moved, extra = _repoint_one_by_one(
            session, table, column, keys=keys, old_ids=old_ids, whens=whens, label=label
        )
        dropped += extra
    session.commit()
    return moved, dropped


def _repoint_table(
    session: Session,
    table: Table,
    column: Column[Any],
    id_map: dict[int, int],
    *,
    maps: dict[str, dict[int, int]],
    batch_size: int,
    on_batch: Progress | None,
    locks: list[int] | None = None,
) -> tuple[int, int]:
    """Walk one ``(table, column)`` across in primary-key order, committing every batch.

    The batch is the next ``batch_size`` primary keys that still point at a phantom id.
    Rows already moved drop out of that filter, so the next batch begins where this one
    stopped without a cursor to keep — and a re-run after an interruption resumes for
    exactly the same reason.
    """
    key = _primary_key(table)
    old_ids = list(id_map)
    whens = [(column == old, new) for old, new in id_map.items()]
    label = f"{table.name}.{column.name}"
    total = _count(session, table, column, id_map)
    if not total:
        return 0, 0

    moved_total = dropped_total = batches = 0
    log.info("repair: repointing %s — %d rows to move", label, total)
    while True:
        keys = list(
            session.scalars(select(key).where(column.in_(old_ids)).order_by(key).limit(batch_size))
        )
        if not keys:
            break
        # A batch Postgres picks as a deadlock victim is retried rather than lost: the rows
        # it was moving are still where they were, so re-running it is re-running the same
        # work.  Ordered locks make this rare; the retry is what makes it harmless.
        for attempt in range(DEADLOCK_RETRIES + 1):
            try:
                moved, dropped = _run_batch(
                    session,
                    table,
                    column,
                    keys=keys,
                    old_ids=old_ids,
                    whens=whens,
                    maps=maps,
                    locks=locks or [],
                    label=label,
                )
                break
            except DBAPIError as error:
                if not is_deadlock(error) or attempt == DEADLOCK_RETRIES:
                    raise
                session.rollback()
                log.warning("repair: %s — batch deadlocked, retry %d", label, attempt + 1)
                time.sleep(DEADLOCK_BACKOFF_S)
        if not moved and not dropped:  # pragma: no cover - a batch that matches but writes nothing
            raise RuntimeError(f"repair: {label} made no progress on {len(keys)} rows")
        moved_total += moved
        dropped_total += dropped
        batches += 1
        log.info(
            "repair: %s — %d/%d moved%s (batch %d)",
            label,
            moved_total,
            total,
            f", {dropped_total} duplicates dropped" if dropped_total else "",
            batches,
        )
        if on_batch is not None:
            on_batch(
                {
                    "table": table.name,
                    "column": column.name,
                    "batch": batches,
                    "rows_done": moved_total,
                    "rows_total": total,
                    "duplicates_dropped": dropped_total,
                }
            )
    return moved_total, dropped_total


def repoint_phantom_rows(
    session: Session,
    fold: Fold,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_batch: Progress | None = None,
) -> dict[str, Any]:
    """Phase B: move every reference off the phantom ids, a committed batch at a time.

    Nothing here reads or writes a target row for its own sake — the only rows it touches
    are rows still pointing at something the phantom owns, which by definition nothing live
    is writing.  A failure part-way leaves the batches before it committed and the archive
    consistent; running the repair again finishes the job.

    ``on_batch`` is called after each committed batch with a progress event.  Raising from
    it is how a test interrupts the phase; the batches already committed stay committed.
    """
    moved: dict[str, int] = {}
    dropped: dict[str, int] = {}
    column_maps = _column_maps(fold)
    talkgroups = fold.twins.get("talkgroups", {})
    locks = sorted({*talkgroups, *talkgroups.values()})
    for table, column, id_map in _work(fold):
        rows, losers = _repoint_table(
            session,
            table,
            column,
            id_map,
            maps=column_maps[table.name],
            batch_size=batch_size,
            on_batch=on_batch,
            locks=locks,
        )
        label = f"{table.name}.{column.name}"
        if rows:
            moved[label] = rows
        if losers:
            dropped[label] = losers
    return {"rows_repointed": moved, "duplicates_dropped": dropped}


# --------------------------------------------------------------------------------------
# phase C — finish
# --------------------------------------------------------------------------------------


def _outstanding(session: Session, fold: Fold) -> dict[str, int]:
    """Rows that still point at a phantom id — phase B's unfinished business, if any."""
    left: dict[str, int] = {}
    for table, column, id_map in _work(fold):
        count = _count(session, table, column, id_map)
        if count:
            left[f"{table.name}.{column.name}"] = count
    for model, table_name, _ in _CHILDREN:
        keep = list(fold.twins.get(table_name, {})) or [0]
        stragglers = session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.system_id == fold.phantom_system_id, model.id.not_in(keep))
        )
        if stragglers:
            left[f"{table_name}.system_id"] = stragglers
    return left


def finish_phantom_merge(session: Session, fold: Fold) -> dict[str, Any]:
    """Phase C: sum the counters, fill the real rows' blanks, drop what is now empty.

    One short transaction, and the only one that locks a row of the real system — long
    enough to add two integers and fill in a NAC.  It refuses outright if anything still
    points at a phantom id, because the deletes below would otherwise cascade real rows
    away or fail on a foreign key.
    """
    left = _outstanding(session, fold)
    if left:
        raise RepairIncomplete(
            "repointing is not finished — "
            + ", ".join(f"{name}: {rows} rows" for name, rows in sorted(left.items()))
            + "; run the repair again to resume"
        )

    deleted: dict[str, int] = {}
    for model, table_name, _ in _CHILDREN:
        twins = fold.twins.get(table_name, {})
        if not twins:
            continue
        for phantom_id, target_id in twins.items():
            loser = session.get(model, phantom_id)
            winner = session.get(model, target_id)
            if loser is None or winner is None:  # already folded by an earlier run
                continue
            for name in _FILL_BLANKS.get(table_name, ()):
                if getattr(winner, name, None) is None and getattr(loser, name, None) is not None:
                    setattr(winner, name, getattr(loser, name))
            for name in _SUM_COLUMNS.get(table_name, ()):
                setattr(winner, name, (getattr(winner, name) or 0) + (getattr(loser, name) or 0))
        session.flush()
        gone = session.execute(delete(model).where(model.id.in_(list(twins)))).rowcount
        if gone:
            deleted[table_name] = gone

    session.execute(delete(System).where(System.id == fold.phantom_system_id))
    session.commit()
    deleted["systems"] = 1
    return {"deleted": deleted}


# --------------------------------------------------------------------------------------
# the plan, and the whole repair
# --------------------------------------------------------------------------------------


def _blank_report(name: str, dry_run: bool) -> dict[str, Any]:
    return {"system": name, "dry_run": dry_run, "changed": False, "detail": None}


def _describe(report: dict[str, Any], fold: Fold) -> None:
    for _, table_name, _ in _CHILDREN:
        report[f"{table_name}_merged"] = fold.merged_rows(table_name)
        report[f"{table_name}_reparented"] = fold.reparented[table_name]


def plan_phantom_merge(session: Session, *, name: str = PHANTOM_SYSTEM_NAME) -> dict[str, Any]:
    """What ``--apply`` would do, worked out without writing or locking anything.

    Every statement here is a ``SELECT``: the classification, one ``COUNT`` per referrer,
    and the tally of rows phase C would delete.  There is deliberately no rehearsal
    transaction — the old dry run did the entire merge and rolled it back, which took the
    same locks for the same six minutes as the real thing and stalled the live feed for a
    preview nobody kept.
    """
    report = _blank_report(name, dry_run=True)
    pair = _resolve(session, name, report)
    if pair is None:
        return report
    phantom, target = pair

    fold = classify(session, phantom, target)
    _describe(report, fold)
    to_repoint: dict[str, int] = {}
    for table, column, id_map in _work(fold):
        count = _count(session, table, column, id_map)
        if count:
            to_repoint[f"{table.name}.{column.name}"] = count
    report["rows_to_repoint"] = to_repoint
    report["rows_to_repoint_total"] = sum(to_repoint.values())
    report["would_delete"] = {
        **{table: len(ids) for table, ids in fold.twins.items() if ids},
        "systems": 1,
    }
    report["changed"] = True
    report["detail"] = (
        f"dry run: {report['rows_to_repoint_total']} rows would be repointed onto "
        f"{target.name!r}; nothing was written and nothing was read for update"
    )
    return report


def merge_phantom_system(
    session: Session,
    *,
    name: str = PHANTOM_SYSTEM_NAME,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_batch: Progress | None = None,
) -> dict[str, Any]:
    """Fold a system that a fallback label invented back into the real one.

    Production grew a ``System`` literally named ``upload`` (WACN NULL), holding second
    copies of the ``Central`` and ``Eastside`` sites while the real ``Example System`` held the
    configured Central.  ``sites`` is unique on ``(system_id, name)``, so the phantom's
    Central never collided with the real one — it just sat beside it, collecting a million
    calls nobody would ever look at.

    ``dry_run`` (the default) reports the plan and writes nothing; see
    :func:`plan_phantom_merge` for why it no longer rehearses the merge.  Otherwise the
    three phases run in order, committing as they go, and it is safe to interrupt and
    re-run: a repaired archive has no phantom to find, and a half-repaired one resumes.
    """
    if dry_run:
        return plan_phantom_merge(session, name=name)

    report = _blank_report(name, dry_run=False)
    if _resolve(session, name, report) is None:
        return report

    fold = map_phantom_children(session, name=name)
    _describe(report, fold)
    report.update(repoint_phantom_rows(session, fold, batch_size=batch_size, on_batch=on_batch))
    report.update(finish_phantom_merge(session, fold))
    report["deleted_system"] = {"id": fold.phantom_system_id, "name": name}
    report["changed"] = True
    log.info(
        "repair: folded system %r (%s) into %r (%s); %d rows repointed",
        name,
        fold.phantom_system_id,
        fold.target_name,
        fold.target_system_id,
        sum(report["rows_repointed"].values()),
    )
    return report
