"""Re-filing calls that were archived under the wrong system.

``keyup refile-system`` is the command this is run with — ``--match`` a literal substring
of the filename, and the move happening unless ``--dry-run`` says otherwise.
``keyup refile-calls`` is the same operation with the raw ``LIKE`` pattern and an explicit
``--apply``, kept for a slice a substring cannot name.

A sibling of :mod:`app.repair`, and deliberately the *opposite* operation.  ``repair``
folds a system that should never have existed into the one it duplicated.  This one takes
a slice of calls out of a system that is perfectly real and puts it under the system those
calls always belonged to.

The damage it undoes is a recorder mislabeling: a county network (``Example System``) grew a site
called ``Eastside`` holding 3,803 calls whose filenames all say
``EXAMPLE_SCHOOL_DISTRICT``.  They are a school district's own P25
system, recorded and filed under the county network beside it.  Because the ingest keys a
talkgroup on ``(system_id, tgid)``, the school's talkgroup 101 was *merged into* the county's
talkgroup 101 — a different channel that happens to carry the same number — and its radios
merged into the county's radios the same way.  Nothing is duplicated here and nothing can simply
be re-parented: the county's own calls sit on those exact rows.  The slice has to be **split
off** onto twins.

So the operation is:

* **map** (:func:`map_refile`) — one short transaction that reads the slice and, for every
  talkgroup and radio it touches, ensures a *twin* exists under the target system with the
  same natural key (``tgid``; ``radio_id/wacn/home_system_id``).  A twin that already
  exists is used as it is; a twin this run creates is seeded from the source row's
  identity columns.  Seeding is **create-only**, not fill-blanks-on-an-existing-row: a
  talkgroup already under the target system is the target system's own row, curated there,
  and a mislabeled source row has no standing to edit it.
* **repoint** (:func:`repoint_refiled_calls`) — the long phase, batched by call id with a
  ``COMMIT`` per batch.  Each batch rewrites ``calls.system_id/site_id/talkgroup_id/
  radio_id`` and the ``call_sources.radio_id`` of those calls, holding locks on nothing but
  the calls it is moving.
* **finish** (:func:`finish_refile`) — moves the derived per-pair rows that are
  *exclusively* about the moved calls (see :func:`_move_exclusive`), recomputes
  ``call_count`` on every talkgroup and radio on both sides, and reports whether the source
  site is now empty.

**A call is never deleted.**  Where the phantom merge has to drop a duplicate that a unique
key will not let across, this one has no such case: ``calls`` has no unique key over any of
its foreign keys, so every matching call is moved.

Resumability comes from the filter rather than from a cursor: "still to do" is *matches
the pattern and is not yet under the target system*, which a moved call stops satisfying
the moment its batch commits.  Interrupt it and run it again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import (
    AdjacentSite,
    Call,
    CallSource,
    Conversation,
    Radio,
    RadioAffiliation,
    RadioEvent,
    RecorderObservation,
    Site,
    SiteFrequency,
    System,
    Talkgroup,
    Tuner,
)

log = logging.getLogger("app.refile")

__all__ = [
    "DEADLOCK_BACKOFF_S",
    "DEADLOCK_RETRIES",
    "DEFAULT_BATCH_SIZE",
    "RefileIncomplete",
    "RefileMap",
    "RefileTargetMissing",
    "finish_refile",
    "is_deadlock",
    "lock_talkgroups",
    "map_refile",
    "plan_refile",
    "refile_calls",
    "repoint_refiled_calls",
    "site_blockers",
]

#: Calls repointed per committed transaction.  Smaller than the phantom merge's 20,000:
#: every batch takes its talkgroups' conversation advisory locks and holds them until it
#: commits (:func:`lock_talkgroups`), and a lock a live call waits on is a lock to hold
#: briefly.
DEFAULT_BATCH_SIZE = 5_000

#: Retries for a batch Postgres chose as a deadlock victim, and the pause before each.
DEADLOCK_RETRIES = 5
DEADLOCK_BACKOFF_S = 0.25

#: Identity copied onto a twin **this run creates**, and never onto one that already
#: existed.  ``call_count`` is deliberately not among them: phase C recomputes it from the
#: calls themselves, which is the only number that cannot drift.
_TALKGROUP_SEED = ("alias", "group_name", "description", "category", "service_tag", "agency_code")
_RADIO_SEED = ("alias", "unit_id", "agency_code", "kind")


class RefileTargetMissing(LookupError):
    """The named target system or site does not exist.  Nothing is guessed and nothing created."""


class RefileIncomplete(RuntimeError):
    """Phase C was asked to finish a refile whose calls have not all moved."""


@dataclass
class RefileMap:
    """The twins the move will use, and the sites the slice is coming out of.

    Re-derived from the archive on every run rather than stored, which is what makes an
    interrupted refile resumable: the twins created last time are *found* rather than
    created, and the calls already moved are no longer in the slice.
    """

    match: str
    target_system_id: int
    target_system_name: str
    target_site_id: int
    target_site_name: str
    #: ``{source talkgroup id: target talkgroup id}``
    talkgroups: dict[int, int] = field(default_factory=dict)
    #: ``{source radio id: target radio id}``
    radios: dict[int, int] = field(default_factory=dict)
    #: the twins this run created (or, in a dry run, would create)
    talkgroups_created: list[dict[str, Any]] = field(default_factory=list)
    radios_created: list[dict[str, Any]] = field(default_factory=list)
    #: the ``(system, site)`` pairs the outstanding calls sit under, with their counts
    source_sites: list[dict[str, Any]] = field(default_factory=list)


#: Called once per committed batch with a progress event; see :func:`repoint_refiled_calls`.
Progress = Callable[[dict[str, Any]], None]


# --------------------------------------------------------------------------------------
# the slice
# --------------------------------------------------------------------------------------


def _pending(match: str, target_system_id: int) -> tuple[Any, ...]:
    """The calls still to move: matching the pattern and not yet under the target system.

    This is the *only* definition of outstanding work in the module — the plan counts it,
    each batch re-reads it, and phase C refuses while it is non-empty.  A stored cursor
    could disagree with the archive; this cannot.
    """
    return (Call.file_name.like(match), Call.system_id != target_system_id)


def _resolve(session: Session, *, to_system: str, to_site: str) -> tuple[System, Site]:
    """The target system and the site under it, both of which must already exist.

    Refusing to create them is the point: an operator who mistypes a system name would
    otherwise get a brand new system holding several thousand calls, which is the very
    damage this command exists to undo.
    """
    system = session.scalars(select(System).where(System.name == to_system)).first()
    if system is None:
        raise RefileTargetMissing(f"no system named {to_system!r}; create it before re-filing")
    site = session.scalars(
        select(Site).where(Site.system_id == system.id, Site.name == to_site)
    ).first()
    if site is None:
        raise RefileTargetMissing(
            f"system {to_system!r} has no site named {to_site!r}; create it before re-filing"
        )
    return system, site


def _slice_talkgroup_ids(session: Session, match: str, target_system_id: int) -> list[int]:
    """Every talkgroup the outstanding calls are filed under, in id order."""
    return list(
        session.scalars(
            select(Call.talkgroup_id)
            .where(*_pending(match, target_system_id), Call.talkgroup_id.is_not(None))
            .distinct()
            .order_by(Call.talkgroup_id)
        )
    )


def _slice_radio_ids(session: Session, match: str, target_system_id: int) -> list[int]:
    """Every radio the slice names — the call's own, and every keying radio in its sources.

    ``call_sources`` matters as much as ``calls.radio_id``: a trunk-recorder call carries
    the whole ``srcList``, and a refile that moved only the first talker would leave the
    rest of the conversation pointing at the wrong system's radios.
    """
    own = select(Call.radio_id).where(*_pending(match, target_system_id), Call.radio_id.is_not(None))
    sourced = (
        select(CallSource.radio_id)
        .join(Call, CallSource.call_id == Call.id)
        .where(*_pending(match, target_system_id), CallSource.radio_id.is_not(None))
    )
    return sorted({row for row in session.scalars(own.union(sourced))})


# --------------------------------------------------------------------------------------
# phase A — map
# --------------------------------------------------------------------------------------


def _talkgroups_the_move_empties(
    session: Session, match: str, target_system_id: int, talkgroup_ids: list[int]
) -> set[int]:
    """Of the slice's source talkgroups, the ones that will hold no call afterwards.

    This is what decides whether a twin is seeded with the source row's curation or created
    bare.  A source talkgroup the move *empties* only ever carried the mislabeled traffic —
    it is the school's own channel, wearing the county's system id by accident, and its
    label and service tag are the school's to keep.  One that keeps calls is the county's
    own channel that happens to carry the same number, and the county's label for it
    ("EC LAW 1") says nothing true about the school's channel 101.  That twin gets the
    tgid and nothing else, for a human to name.
    """
    if not talkgroup_ids:
        return set()
    staying = set(
        session.scalars(
            select(Call.talkgroup_id)
            .where(
                Call.talkgroup_id.in_(talkgroup_ids),
                or_(
                    Call.file_name.is_(None),
                    Call.file_name.not_like(match),
                ),
            )
            .distinct()
        )
    )
    # A call already under the target is one this run moved: it does not keep the source row.
    return {tgid for tgid in talkgroup_ids if tgid not in staying}



def _find_twin_talkgroup(session: Session, source: Talkgroup, system_id: int) -> Talkgroup | None:
    return session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == system_id, Talkgroup.tgid == source.tgid)
    ).first()


def _find_twin_radio(session: Session, source: Radio, system_id: int) -> Radio | None:
    return session.scalars(
        select(Radio).where(
            Radio.system_id == system_id,
            Radio.radio_id == source.radio_id,
            Radio.wacn == source.wacn,
            Radio.home_system_id == source.home_system_id,
        )
    ).first()


def _build_map(session: Session, *, match: str, system: System, site: Site, create: bool) -> RefileMap:
    """Classify the slice's talkgroups and radios, creating the missing twins when asked to.

    With ``create=False`` nothing is written — the plan reports which twins *would* be
    created and maps the rest — which is what makes the dry run a pure read.
    """
    plan = RefileMap(
        match=match,
        target_system_id=system.id,
        target_system_name=system.name,
        target_site_id=site.id,
        target_site_name=site.name,
    )
    slice_talkgroups = _slice_talkgroup_ids(session, match, system.id)
    emptied = _talkgroups_the_move_empties(session, match, system.id, slice_talkgroups)
    for talkgroup_id in slice_talkgroups:
        source = session.get(Talkgroup, talkgroup_id)
        if source is None:  # pragma: no cover - a foreign key makes this impossible
            continue
        twin = _find_twin_talkgroup(session, source, system.id)
        if twin is not None:
            plan.talkgroups[talkgroup_id] = twin.id
            continue
        # Seeded only from a source row the move leaves empty — see
        # :func:`_talkgroups_the_move_empties`.
        seed = {name: getattr(source, name) for name in _TALKGROUP_SEED}
        if talkgroup_id not in emptied:
            seed = dict.fromkeys(_TALKGROUP_SEED)
        label: dict[str, Any] = {
            "tgid": source.tgid,
            "alias": seed["alias"],
            "seeded": talkgroup_id in emptied,
            "id": None,
        }
        if create:
            twin = Talkgroup(
                system_id=system.id,
                tgid=source.tgid,
                first_seen_at=source.first_seen_at,
                last_seen_at=source.last_seen_at,
                **seed,
            )
            session.add(twin)
            session.flush()
            plan.talkgroups[talkgroup_id] = twin.id
            label["id"] = twin.id
        plan.talkgroups_created.append(label)

    for radio_id in _slice_radio_ids(session, match, system.id):
        source_radio = session.get(Radio, radio_id)
        if source_radio is None:  # pragma: no cover
            continue
        twin_radio = _find_twin_radio(session, source_radio, system.id)
        if twin_radio is not None:
            plan.radios[radio_id] = twin_radio.id
            continue
        label = {"radio_id": source_radio.radio_id, "alias": source_radio.alias, "id": None}
        if create:
            twin_radio = Radio(
                system_id=system.id,
                radio_id=source_radio.radio_id,
                wacn=source_radio.wacn,
                home_system_id=source_radio.home_system_id,
                first_seen_at=source_radio.first_seen_at,
                last_seen_at=source_radio.last_seen_at,
                **{name: getattr(source_radio, name) for name in _RADIO_SEED},
            )
            session.add(twin_radio)
            session.flush()
            plan.radios[radio_id] = twin_radio.id
            label["id"] = twin_radio.id
        plan.radios_created.append(label)

    plan.source_sites = [
        {
            "system_id": sys_id,
            "system": sys_name,
            "site_id": site_id,
            "site": site_name,
            "calls": int(rows),
        }
        for sys_id, sys_name, site_id, site_name, rows in session.execute(
            select(System.id, System.name, Site.id, Site.name, func.count())
            .select_from(Call)
            .join(System, Call.system_id == System.id)
            .outerjoin(Site, Call.site_id == Site.id)
            .where(*_pending(match, system.id))
            .group_by(System.id, System.name, Site.id, Site.name)
            .order_by(func.count().desc())
        )
    ]
    return plan


def map_refile(session: Session, *, match: str, to_system: str, to_site: str) -> RefileMap:
    """Phase A: ensure a twin talkgroup and radio under the target for everything the slice names.

    One short transaction.  Every row it writes is a brand new row under the target system,
    so there is nothing for a live upload to wait on: an ingest that races it either finds
    the twin and uses it, or creates it first and this run finds it.
    """
    system, site = _resolve(session, to_system=to_system, to_site=to_site)
    plan = _build_map(session, match=match, system=system, site=site, create=True)
    session.commit()
    if plan.talkgroups_created or plan.radios_created:
        log.info(
            "refile: created %d talkgroup and %d radio twins under %r",
            len(plan.talkgroups_created),
            len(plan.radios_created),
            system.name,
        )
    return plan


# --------------------------------------------------------------------------------------
# phase B — repoint
# --------------------------------------------------------------------------------------


def lock_talkgroups(session: Session, talkgroup_ids: list[int]) -> None:
    """Take the conversation lock for every talkgroup this batch touches, in id order.

    Live ingest ends each call with ``pg_advisory_xact_lock(4242, talkgroup_id)`` and
    *then* writes the conversation row (:func:`app.ingest.service.assign_conversation`), so
    anything that may write a conversation row for the same talkgroup must take that lock
    first or the two deadlock ABBA — the lesson
    :func:`app.repair_timestamps._lock_talkgroup` was written from.  Phase C re-parents
    conversations, and it is a committed batch here that makes a conversation eligible for
    that, so the lock is taken from the first batch onwards.

    **Sorted**, because a batch takes several at once: two refiles, or a refile and a live
    ingest already holding one, can only deadlock by asking for the same pair in opposite
    orders.  Ascending id on both sides means they cannot.  The locks are
    transaction-scoped, so the batch's commit releases them — which is why
    :data:`DEFAULT_BATCH_SIZE` is a quarter of the phantom merge's.
    """
    from app.ingest.service import CONVERSATION_LOCK_CLASS  # noqa: PLC0415 - import cycle

    for talkgroup_id in sorted(talkgroup_ids):
        session.execute(
            text("SELECT pg_advisory_xact_lock(:cls, :key)"),
            {"cls": CONVERSATION_LOCK_CLASS, "key": talkgroup_id},
        )


def is_deadlock(error: DBAPIError) -> bool:
    """``40P01`` — Postgres picked this transaction as the victim, so a retry can win."""
    orig = error.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return str(code) == "40P01"


def _remap(column: Any, id_map: dict[int, int]) -> Any:
    """``column`` read through ``{old: new}``; every other value passes through unchanged."""
    if not id_map:
        return column
    return case(*[(column == old, new) for old, new in id_map.items()], else_=column)


def _move_batch(session: Session, plan: RefileMap, call_ids: list[int]) -> int:
    """Rewrite one batch of calls and their sources.  One transaction, committed here."""
    lock_talkgroups(session, [*plan.talkgroups, *plan.talkgroups.values()])
    moved = session.execute(
        update(Call)
        .where(Call.id.in_(call_ids))
        .values(
            system_id=plan.target_system_id,
            site_id=plan.target_site_id,
            talkgroup_id=_remap(Call.talkgroup_id, plan.talkgroups),
            radio_id=_remap(Call.radio_id, plan.radios),
        )
    ).rowcount
    if plan.radios:
        session.execute(
            update(CallSource)
            .where(CallSource.call_id.in_(call_ids), CallSource.radio_id.in_(list(plan.radios)))
            .values(radio_id=_remap(CallSource.radio_id, plan.radios))
        )
    session.commit()
    return int(moved)


def repoint_refiled_calls(
    session: Session,
    plan: RefileMap,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_batch: Progress | None = None,
) -> dict[str, Any]:
    """Phase B: move the slice onto the target system, a committed batch at a time.

    Each batch is the next ``batch_size`` outstanding call ids, so a batch that commits
    removes itself from the next batch's query and an interrupted run resumes without a
    cursor to keep.  A batch Postgres picks as a deadlock victim is rolled back and retried
    up to :data:`DEADLOCK_RETRIES` times — the sorted advisory locks make that rare, and a
    retry is free because a rolled-back batch moved nothing.

    ``on_batch`` is called after each committed batch with a progress event.  Raising from
    it is how a test interrupts the phase; the batches already committed stay committed.
    """
    total = _outstanding(session, plan)
    if not total:
        return {"calls_moved": 0, "batches": 0, "deadlock_retries": 0}

    moved_total = batches = deadlocks = 0
    log.info("refile: moving %d calls onto %r", total, plan.target_system_name)
    while True:
        call_ids = list(
            session.scalars(
                select(Call.id)
                .where(*_pending(plan.match, plan.target_system_id))
                .order_by(Call.id)
                .limit(batch_size)
            )
        )
        if not call_ids:
            break
        moved = 0
        for attempt in range(DEADLOCK_RETRIES + 1):
            try:
                moved = _move_batch(session, plan, call_ids)
            except DBAPIError as error:
                session.rollback()
                if not is_deadlock(error) or attempt == DEADLOCK_RETRIES:
                    raise
                deadlocks += 1
                log.warning(
                    "refile: batch %d was a deadlock victim (attempt %d); retrying",
                    batches + 1,
                    attempt + 1,
                )
                time.sleep(DEADLOCK_BACKOFF_S * (attempt + 1))
            else:
                break
        if not moved:  # pragma: no cover - a batch that matches but writes nothing
            raise RuntimeError(f"refile: made no progress on {len(call_ids)} calls")
        moved_total += moved
        batches += 1
        log.info("refile: %d/%d calls moved (batch %d)", moved_total, total, batches)
        if on_batch is not None:
            on_batch(
                {
                    "table": "calls",
                    "batch": batches,
                    "rows_done": moved_total,
                    "rows_total": total,
                }
            )
    return {"calls_moved": moved_total, "batches": batches, "deadlock_retries": deadlocks}


# --------------------------------------------------------------------------------------
# phase C — finish
# --------------------------------------------------------------------------------------


def _outstanding(session: Session, plan: RefileMap) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(*_pending(plan.match, plan.target_system_id))
        )
        or 0
    )


def _move_exclusive(
    session: Session, plan: RefileMap, *, model: Any, radio_column: Any, talkgroup_column: Any
) -> dict[str, int]:
    """Repoint the per-pair rows that are *only* about the moved calls, and leave the rest.

    ``radio_affiliations`` and ``radio_events`` both say "radio R was heard on talkgroup
    T".  After the split, R and T each exist twice — once on the source system, once on the
    target — and one row cannot be in both places.  Moving it unconditionally would be a
    lie whenever the source system's *own* calls also put R on T, which for a merged tgid is
    exactly the case this repair exists for.

    So a row moves only when both ends are ends the refile mapped **and** no surviving call
    on the source side still makes the same claim.  Otherwise it stays, describing the
    traffic that stayed with it.  A genuinely shared pair ends up described on the source
    side only: an understatement, never a fabrication, and ingest re-derives the target
    side from the next call it hears.
    """
    if not plan.talkgroups or not plan.radios:
        return {"moved": 0, "kept": 0}
    rows = list(
        session.scalars(
            select(model).where(
                radio_column.in_(list(plan.radios)), talkgroup_column.in_(list(plan.talkgroups))
            )
        )
    )
    moved = kept = 0
    for row in rows:
        source_radio = getattr(row, radio_column.key)
        source_talkgroup = getattr(row, talkgroup_column.key)
        still_claimed = session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.radio_id == source_radio, Call.talkgroup_id == source_talkgroup)
        )
        if still_claimed:
            kept += 1
            continue
        setattr(row, radio_column.key, plan.radios[source_radio])
        setattr(row, talkgroup_column.key, plan.talkgroups[source_talkgroup])
        moved += 1
    session.flush()
    return {"moved": moved, "kept": kept}


def _move_exclusive_conversations(session: Session, plan: RefileMap) -> dict[str, int]:
    """Re-parent the threads whose every call moved; split nothing.

    A :class:`~app.models.Conversation` is "consecutive calls on one talkgroup".  Once its
    calls sit under the target system a thread still pointing at the source talkgroup is a
    thread the UI would file under the wrong system.  A thread that also holds calls that
    did *not* move is genuinely mixed, and the honest thing is to leave it: rebuilding
    threads is ingest's job (``keyup fix-upload-timestamps --regroup`` does it wholesale),
    not a re-filing's.
    """
    if not plan.talkgroups:
        return {"moved": 0, "kept": 0}
    moved = kept = 0
    for conversation in session.scalars(
        select(Conversation).where(Conversation.talkgroup_id.in_(list(plan.talkgroups)))
    ):
        outside = session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.conversation_id == conversation.id, Call.system_id != plan.target_system_id)
        )
        inside = session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.conversation_id == conversation.id, Call.system_id == plan.target_system_id)
        )
        if outside or not inside:
            kept += 1
            continue
        conversation.talkgroup_id = plan.talkgroups[conversation.talkgroup_id]
        moved += 1
    session.flush()
    return {"moved": moved, "kept": kept}


def _recount(session: Session, *, model: Any, call_column: Any, ids: set[int]) -> int:
    """Set ``call_count`` from the calls themselves, on both sides of the move.

    Counted the way ingest counts it — ``duplicate_of IS NULL``, because a second copy of a
    transmission is not a second transmission — so a re-filed archive's counters read
    exactly as they would have if it had been filed correctly from the start.  Returns how
    many rows the recount actually changed.
    """
    if not ids:
        return 0
    counts = dict(
        session.execute(
            select(call_column, func.count())
            .where(call_column.in_(sorted(ids)), Call.duplicate_of.is_(None))
            .group_by(call_column)
        ).all()
    )
    changed = 0
    for row_id in sorted(ids):
        row = session.get(model, row_id)
        if row is None:  # pragma: no cover
            continue
        wanted = int(counts.get(row_id, 0))
        if row.call_count != wanted:
            row.call_count = wanted
            changed += 1
    session.flush()
    return changed


def finish_refile(session: Session, plan: RefileMap) -> dict[str, Any]:
    """Phase C: move the exclusive derived rows, fix the counters, report the empty source.

    Refuses while phase B has calls left, because every exclusivity test below asks "does
    the source side still claim this pair?" and a half-moved slice answers yes for pairs
    that are about to stop being claimed at all.
    """
    left = _outstanding(session, plan)
    if left:
        raise RefileIncomplete(
            f"{left} calls have not moved yet; run the refile again to resume"
        )

    affiliations = _move_exclusive(
        session,
        plan,
        model=RadioAffiliation,
        radio_column=RadioAffiliation.radio_id_fk,
        talkgroup_column=RadioAffiliation.talkgroup_id_fk,
    )
    events = _move_exclusive(
        session,
        plan,
        model=RadioEvent,
        radio_column=RadioEvent.radio_pk,
        talkgroup_column=RadioEvent.talkgroup_pk,
    )
    # A radio event names the site it was heard at as well as the pair.  Where the pair
    # moved, the site has to move with it or the row points across two systems.
    if plan.radios:
        session.execute(
            update(RadioEvent)
            .where(
                RadioEvent.radio_pk.in_(list(plan.radios.values())),
                RadioEvent.system_id.is_not(None),
                RadioEvent.system_id != plan.target_system_id,
            )
            .values(system_id=plan.target_system_id, site_id=plan.target_site_id)
        )
    conversations = _move_exclusive_conversations(session, plan)

    recounted = {
        "talkgroups": _recount(
            session,
            model=Talkgroup,
            call_column=Call.talkgroup_id,
            ids={*plan.talkgroups, *plan.talkgroups.values()},
        ),
        "radios": _recount(
            session,
            model=Radio,
            call_column=Call.radio_id,
            ids={*plan.radios, *plan.radios.values()},
        ),
    }
    session.commit()
    # The Sites page reads its per-site call counts from a minute-long process cache
    # (:func:`app.services.sites.call_counts`).  A refile is precisely the moment an
    # operator is looking straight at those numbers, so a stale one would read as the move
    # having failed.
    from app.services.sites import forget_call_counts  # noqa: PLC0415 - avoids a cycle

    forget_call_counts()
    return {
        "affiliations": affiliations,
        "radio_events": events,
        "conversations": conversations,
        "recounted": recounted,
    }


# --------------------------------------------------------------------------------------
# what still holds a site, and the empty source
# --------------------------------------------------------------------------------------


def site_blockers(session: Session, site_id: int) -> dict[str, int]:
    """What still references a site, as ``{table: rows}`` — empty when it can be deleted.

    Shared with the API's ``DELETE /sites/{id}`` so the CLI and the GUI cannot disagree
    about what "deletable" means.  ``calls`` is listed like anything else even though its
    foreign key is ``ON DELETE SET NULL``: silently orphaning thousands of recordings from
    the site they were heard at is not a delete anyone asked for.

    Tuners are asked of their JSONB rather than of a column: ``tuners.desired``/``reported``
    are documents the recorder owns, and a tuner told to focus this site keeps working
    after the row goes — on a site nobody can name any more.
    """
    counts: dict[str, int] = {}
    for label, model, column in (
        ("calls", Call, Call.site_id),
        ("recorder_observations", RecorderObservation, RecorderObservation.site_id),
        ("radio_events", RadioEvent, RadioEvent.site_id),
        ("site_frequencies", SiteFrequency, SiteFrequency.site_id),
        ("adjacent_sites", AdjacentSite, AdjacentSite.site_id),
    ):
        rows = session.scalar(select(func.count()).select_from(model).where(column == site_id)) or 0
        if rows:
            counts[label] = int(rows)
    tuners = (
        session.scalar(
            select(func.count())
            .select_from(Tuner)
            .where(
                text(
                    "(tuners.desired ->> 'site_id') = :sid OR (tuners.reported ->> 'site_id') = :sid"
                ).bindparams(sid=str(site_id))
            )
        )
        or 0
    )
    if tuners:
        counts["tuners"] = int(tuners)
    return counts


def _emptied_sites(session: Session, plan: RefileMap) -> list[dict[str, Any]]:
    """The sites the slice came out of that now hold no calls at all.

    Reported rather than deleted: a site with no calls may still be a site the recorder is
    configured to hunt, so removing it is a decision, not a side effect of a move.
    ``--drop-empty-source-site`` is that decision, typed.
    """
    empty: list[dict[str, Any]] = []
    for source in plan.source_sites:
        site_id = source.get("site_id")
        if site_id is None or site_id == plan.target_site_id:
            continue
        remaining = session.scalar(
            select(func.count()).select_from(Call).where(Call.site_id == site_id)
        )
        if not remaining:
            empty.append({"id": site_id, "name": source["site"], "system": source["system"]})
    return empty


def _drop_sites(session: Session, sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Delete the emptied sites, unless something other than calls still references one."""
    dropped: list[dict[str, Any]] = []
    for site in sites:
        blocking = site_blockers(session, site["id"])
        if blocking:
            site["blocked_by"] = blocking
            continue
        session.execute(delete(Site).where(Site.id == site["id"]))
        dropped.append({"id": site["id"], "name": site["name"], "system": site["system"]})
    if dropped:
        session.commit()
    return dropped


# --------------------------------------------------------------------------------------
# the plan, and the whole command
# --------------------------------------------------------------------------------------


def _blank_report(match: str, to_system: str, to_site: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "match": match,
        "to_system": to_system,
        "to_site": to_site,
        "dry_run": dry_run,
        "changed": False,
        "detail": None,
    }


def plan_refile(session: Session, *, match: str, to_system: str, to_site: str) -> dict[str, Any]:
    """What ``--apply`` would do, worked out without writing or locking anything.

    Every statement is a ``SELECT``.  There is deliberately no rehearsal transaction: the
    phantom merge learned that lesson (:func:`app.repair.plan_phantom_merge`) — a dry run
    that does the work and rolls it back takes the same locks for the same minutes as the
    real thing, for a preview nobody keeps.
    """
    report = _blank_report(match, to_system, to_site, dry_run=True)
    system, site = _resolve(session, to_system=to_system, to_site=to_site)
    plan = _build_map(session, match=match, system=system, site=site, create=False)
    calls = int(
        session.scalar(select(func.count()).select_from(Call).where(*_pending(match, system.id))) or 0
    )
    report.update(
        {
            "calls_to_move": calls,
            "from": plan.source_sites,
            "talkgroups_created": plan.talkgroups_created,
            "radios_created": plan.radios_created,
            "talkgroups_reused": len(plan.talkgroups) - len(plan.talkgroups_created),
            "radios_reused": len(plan.radios) - len(plan.radios_created),
            "changed": bool(calls or plan.talkgroups_created or plan.radios_created),
            "detail": (
                f"dry run: {calls} calls would move to {system.name!r} / {site.name!r}; "
                f"{len(plan.talkgroups_created)} talkgroup and {len(plan.radios_created)} radio "
                "twins would be created; nothing was written and nothing was read for update"
            ),
        }
    )
    return report


def refile_calls(
    session: Session,
    *,
    match: str,
    to_system: str,
    to_site: str,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    drop_empty_source_site: bool = False,
    on_batch: Progress | None = None,
) -> dict[str, Any]:
    """Move every call whose ``file_name`` matches ``match`` onto another system's site.

    ``match`` is a SQL ``LIKE`` pattern applied to ``calls.file_name`` — the filename is
    the only place the truth survived, because every column the ingest derived was derived
    from the wrong system label.

    ``dry_run`` (the default) reports the plan and writes nothing.  Otherwise the three
    phases run in order, committing as they go, and it is safe to interrupt and re-run: a
    finished refile finds nothing outstanding and a half-finished one resumes.  Nothing is
    ever deleted except, with ``drop_empty_source_site``, a source site the move itself
    emptied and nothing else references.
    """
    if dry_run:
        return plan_refile(session, match=match, to_system=to_system, to_site=to_site)

    report = _blank_report(match, to_system, to_site, dry_run=False)
    plan = map_refile(session, match=match, to_system=to_system, to_site=to_site)
    report["from"] = plan.source_sites
    report["talkgroups_created"] = plan.talkgroups_created
    report["radios_created"] = plan.radios_created
    report.update(repoint_refiled_calls(session, plan, batch_size=batch_size, on_batch=on_batch))
    report.update(finish_refile(session, plan))

    empty = _emptied_sites(session, plan)
    report["emptied_source_sites"] = empty
    if empty and drop_empty_source_site:
        report["dropped_source_sites"] = _drop_sites(session, empty)
    report["changed"] = bool(
        report.get("calls_moved") or plan.talkgroups_created or plan.radios_created
    )
    detail = (
        f"{report.get('calls_moved', 0)} calls moved to "
        f"{plan.target_system_name!r} / {plan.target_site_name!r}"
    )
    if empty:
        names = ", ".join(f"{row['system']} / {row['name']}" for row in empty)
        if report.get("dropped_source_sites"):
            detail += f"; source site {names} was emptied by the move and deleted"
        else:
            detail += (
                f"; source site {names} now holds no calls — delete it from the Sites page, "
                "or re-run with --drop-empty-source-site"
            )
    report["detail"] = detail
    log.info("refile: %s", detail)
    return report
