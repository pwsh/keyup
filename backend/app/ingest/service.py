"""Turn a recording on disk into rows — docs/ARCHITECTURE.md 5.1 steps 2-6.

``ingest_file`` is the unit of work and owns its transaction: it commits on success and,
on a failure, rolls back and records the failure in the ``ingest_files`` ledger in a
fresh transaction, so a bad file is visible and retryable instead of silently lost.  It
is idempotent on ``calls.file_path``, which is what makes a rescan free.

``ingest_tree`` is the batch wrapper: it opens one session per worker thread, skips paths
the ledger already accounted for, and records an ``ingest_runs`` row.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import activity
from app.config import Settings, get_settings
from app.db import lock_calls, notify
from app.ingest.parser import (
    AUDIO_SUFFIXES,
    CallRecord,
    CallSourceRef,
    ParseError,
    build_call_record,
    is_keyup_artifact,
)
from app.ingest.reconcile import (
    ReconcileConfig,
    find_match_for_record,
    lock_reconcile,
    reconcile_pair,
)
from app.ingest.tr_parser import (
    SIDECAR_SUFFIX,
    audio_for_sidecar,
    is_trunk_recorder_call,
    is_trunk_recorder_name,
    sidecar_for,
    stem_of,
)
from app.models import (
    Call,
    CallSource,
    Conversation,
    IngestFile,
    IngestRun,
    Patch,
    Radio,
    RadioAffiliation,
    Site,
    System,
    Talkgroup,
    TranscriptionJob,
)
from app.runtime_settings import effective_settings
from app.services import counts as count_service

log = logging.getLogger("app.ingest")

#: how many per-file errors an ``ingest_runs`` row keeps; the ledger has them all
MAX_RUN_ERRORS = 100

#: advisory-lock class for conversation assignment (see :func:`assign_conversation`)
CONVERSATION_LOCK_CLASS = 4242


@dataclass
class IngestResult:
    """What happened to one file."""

    #: 'ingested' | 'duplicate' | 'skipped' | 'failed' — the ledger vocabulary plus
    #: 'skipped', which means "already accounted for, nothing to do"
    status: str
    file_path: str
    call_id: int | None = None
    duplicate_of: int | None = None
    job_id: int | None = None
    error: str | None = None


@dataclass
class IngestRunSummary:
    """Aggregate of one scan, mirroring the ``ingest_runs`` row."""

    run_id: int | None
    root: str
    files_seen: int = 0
    files_new: int = 0
    files_duplicate: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    elapsed_s: float = 0.0
    errors: list[dict[str, str]] = field(default_factory=list)
    #: what enumerating the tree cost, for the log line; ``None`` until the walk has run
    walk: WalkCost | None = None


# --------------------------------------------------------------------------------------
# directory upserts
#
# Every one of these is an ``INSERT … ON CONFLICT`` rather than a select-then-insert,
# because ``ingest_tree`` runs several worker threads against the same new talkgroup.
#
# **An ON CONFLICT SET on these tables must never assign a unique-key column.**  This is
# not style; it decides which row lock the upsert takes, and getting it wrong serializes
# the whole live feed behind any open transaction that merely *references* the row.
#
# Postgres picks the tuple lock from the columns in the UPDATE's target list, not from
# the values:
#
#   * a SET of only non-key columns takes ``FOR NO KEY UPDATE``
#   * a SET that names any column of any unique index/constraint on the table takes the
#     full ``FOR UPDATE`` — even when the value assigned is the one already there
#
# Meanwhile every foreign-key check takes ``FOR KEY SHARE`` on the *parent* row: inserting
# or updating a call locks its system, its site, its talkgroup and its radio that way, and
# holds them until that transaction commits.  ``FOR KEY SHARE`` does not conflict with
# ``FOR NO KEY UPDATE``, and does conflict with ``FOR UPDATE``.
#
# So ``… ON CONFLICT ON CONSTRAINT uq_sites_system_name DO UPDATE SET name = :name``
# (``name`` is half that constraint, and the value never even changed) waited on every STT
# worker mid-transaction, on each reconcile batch and on a repair pass — 79 s of blocked
# call uploads in production, in clusters that tracked the reconcile schedule.  Setting a
# non-key column instead, or ``DO NOTHING`` plus a ``SELECT`` when there is genuinely
# nothing to set, costs nothing and takes the weaker lock.
#
# ``DO NOTHING`` + ``SELECT`` stays race-safe: ``DO NOTHING`` waits out a concurrent
# inserter, and the unique constraint guarantees the row is there for the following
# statement's fresh READ COMMITTED snapshot.
# --------------------------------------------------------------------------------------


def upsert_system(
    session: Session,
    name: str,
    *,
    protocol: str | None,
    wacn: int | None,
    fill_protocol: bool = True,
) -> int:
    """The id of the system called ``name``, creating or completing the row if need be.

    **Read first.**  The obvious shape — ``INSERT … ON CONFLICT DO UPDATE … RETURNING id``
    on every upload — takes a row lock on the one ``systems`` row for the *rest of the
    upload transaction*: reconcile, the audio pass, the export rows.  A deployment watching
    one system therefore serialised every concurrent upload behind whichever one got there
    first, and the statement showed up in the slow log at over two seconds.  It was also a
    write nobody needed: ``protocol`` and ``wacn`` are ``coalesce(existing, new)``, so once
    the row is complete — which it is within the first call after a system appears — the
    UPDATE sets both columns to the values they already hold, forever.

    So: one indexed single-row SELECT (``systems.name`` is unique), and the upsert only when
    the row is absent or one of the two columns is still NULL and this call could fill it.
    The upsert stays exactly as it was for that case, because the read-then-write is not
    atomic and two uploads racing to create the same system must not both insert.

    No cache in front of the read: it is a unique-index lookup on a table with six rows in
    it, which is cheaper than the bookkeeping a cache would need to stay correct across the
    processes that write to ``systems`` (the roster importer, the RR sync, the admin API).

    ``fill_protocol=False`` makes ``protocol`` a value for a **new** row only: an existing
    row's protocol is left exactly as it is, NULL included.  The push path passes it when
    the protocol is only the recorder's ``decoder`` form field (trunkd's ``NBFM``), which
    describes one channel and must not re-label a system that already exists.
    """
    row = session.execute(
        select(System.id, System.protocol, System.wacn).where(System.name == name)
    ).first()
    # Nothing to fill in: the row already has everything this call could have supplied.
    if (
        row is not None
        and (protocol is None or row.protocol is not None or not fill_protocol)
        and (wacn is None or row.wacn is not None)
    ):
        return int(row.id)
    stmt = (
        pg_insert(System)
        .values(name=name, protocol=protocol, wacn=wacn)
        .on_conflict_do_update(
            index_elements=[System.name],
            set_={
                "protocol": (
                    func.coalesce(System.protocol, text("excluded.protocol"))
                    if fill_protocol
                    else System.protocol
                ),
                "wacn": func.coalesce(System.wacn, text("excluded.wacn")),
            },
        )
        .returning(System.id)
    )
    return session.execute(stmt).scalar_one()


#: A legacy sysite block resolved against the archive: system, site, channel name.
LegacySysite = tuple[str, str | None, str | None]


def resolve_legacy_sysite(session: Session, sysite: str) -> LegacySysite | None:
    """Split a pre-2025 ``System_Site_Name`` block against the systems that exist.

    The pre-2025 sdrtrunk dialect renders every space as ``_``, so the block is a run of
    tokens with no marker saying where the system ends and the site begins, and the
    parser can only count from the right (:func:`app.ingest.parser._split_sysite`).  Three
    of the four shapes in the production archive survive that; the fourth does not —
    ``EXAMPLE_SCHOOL_DISTRICT_Eastside_District_Schools`` has a two-token
    channel, so counting from the right invents a system called
    "… DISTRICT Eastside" beside the real one.  That is the phantom-system damage this
    branch exists to clean up, so the ingest layer does not repeat it.

    The archive itself is the missing marker: the **longest** token prefix that names a
    system already in ``systems`` is the system, and then the longest prefix of what is
    left that names one of *that system's* sites is the site, with the remainder the
    channel.  Longest-first matters both times — "Example System" must beat "Example" — and a
    block whose head names no known system returns ``None``, which leaves the parser's
    guess in place rather than attaching the call to a system it invented.  So this can
    only ever move a call to a system that already exists.
    """
    tokens = [t for t in sysite.split("_") if t]
    if len(tokens) < 2:  # noqa: PLR2004 - a system alone tells us nothing to re-split
        return None
    names = {
        name.casefold(): name
        for name in session.scalars(select(System.name)).all()
        if name
    }
    for cut in range(len(tokens) - 1, 0, -1):
        system = names.get(" ".join(tokens[:cut]).casefold())
        if system is None:
            continue
        rest = tokens[cut:]
        sites = {
            name.casefold(): name
            for name in session.scalars(
                select(Site.name).join(System, Site.system_id == System.id).where(
                    System.name == system
                )
            ).all()
            if name
        }
        for edge in range(len(rest) - 1, 0, -1):
            site = sites.get(" ".join(rest[:edge]).casefold())
            if site is not None:
                return system, site, " ".join(rest[edge:]) or None
        # No known site under it: the first token is the site, as the era always wrote it.
        return system, rest[0], " ".join(rest[1:]) or None
    return None


class UnknownSystem(Exception):
    """A write named no system and there is no single obvious one to attach it to."""


def resolve_system_id(
    session: Session,
    name: str | None,
    *,
    protocol: str | None = None,
    wacn: int | None = None,
    fill_protocol: bool = True,
) -> int:
    """The system a write belongs to — never one invented from a fallback label.

    A named system is upserted as always.  An **unnamed** one is not: it attaches to the
    only system there is, and raises when there is a choice to be made or nothing to
    choose from.  This is the same rule ``playlist_import.resolve_system`` has always
    applied to an import without ``--system``, and the reason it now applies to every
    ingest path is a production archive that grew a second, phantom ``System("upload")``
    with its own duplicate ``Central`` and ``Eastside`` sites: the push path used to fall
    back to the literal string ``"upload"`` as a system *name* whenever the recorder did
    not send one, and a name is exactly what ``upsert_system`` creates rows from.

    Guessing between several systems would silently file traffic under the wrong one, and
    inventing a name files it under a system that does not exist — so the only safe
    answers are "the one" and "I cannot".
    """
    if name and name.strip():
        return upsert_system(
            session, name.strip(), protocol=protocol, wacn=wacn, fill_protocol=fill_protocol
        )
    ids = session.execute(select(System.id).order_by(System.id)).scalars().all()
    if len(ids) == 1:
        return ids[0]
    detail = (
        "no systems exist yet"
        if not ids
        else f"{len(ids)} systems exist and the write names none of them"
    )
    log.warning("ingest: refusing to invent a system — %s", detail)
    raise UnknownSystem(detail)


def upsert_site(session: Session, system_id: int, name: str | None) -> int | None:
    """The ``sites`` row for one (system, name), creating it the first time.

    ``sites`` has nothing to update — the row *is* its key — so this is ``DO NOTHING``
    and a read, not ``DO UPDATE SET name = name``.  See the lock note above: that SET
    named half of ``uq_sites_system_name`` and took ``FOR UPDATE``, which is what put the
    live call-upload path behind every open transaction holding a call's FK lock.
    """
    if not name:
        return None
    stmt = (
        pg_insert(Site)
        .values(system_id=system_id, name=name)
        .on_conflict_do_nothing(constraint="uq_sites_system_name")
        .returning(Site.id)
    )
    site_id = session.execute(stmt).scalar_one_or_none()
    if site_id is not None:
        return site_id
    return session.execute(
        select(Site.id).where(Site.system_id == system_id, Site.name == name)
    ).scalar_one()


def upsert_talkgroup(
    session: Session,
    system_id: int,
    tgid: int,
    *,
    alias: str | None,
    seen_at: datetime,
    count: bool = True,
    settings: Settings | None = None,
) -> Talkgroup:
    """Create or touch one talkgroup and return it.

    The alias is only *filled in*, never overwritten: it is user-editable in the UI, and
    the alias the recorder printed is kept on the call itself
    (``calls.talkgroup_alias_at_record``) so a rename never rewrites history.  The
    playlist importer is the one thing that does replace aliases.
    """
    settings = settings or get_settings()
    stmt = (
        pg_insert(Talkgroup)
        .values(
            system_id=system_id,
            tgid=tgid,
            alias=alias,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            call_count=1 if count else 0,
            transcribe=settings.transcribe_default,
        )
        .on_conflict_do_update(
            constraint="uq_talkgroups_system_tgid",
            set_={
                "alias": func.coalesce(Talkgroup.alias, text("excluded.alias")),
                "first_seen_at": func.least(
                    func.coalesce(Talkgroup.first_seen_at, text("excluded.first_seen_at")),
                    text("excluded.first_seen_at"),
                ),
                "last_seen_at": func.greatest(
                    func.coalesce(Talkgroup.last_seen_at, text("excluded.last_seen_at")),
                    text("excluded.last_seen_at"),
                ),
                "call_count": Talkgroup.call_count + (1 if count else 0),
            },
        )
        .returning(Talkgroup.id)
    )
    tg_id = session.execute(stmt).scalar_one()
    return session.get(Talkgroup, tg_id)


def upsert_radio(
    session: Session,
    system_id: int,
    *,
    radio_id: int,
    wacn: int,
    home_system: int,
    seen_at: datetime,
    count: bool = True,
    alias: str | None = None,
) -> int:
    """Create or touch one radio.

    ``alias`` is the recorder's own name for the unit — trunk-recorder puts one on every
    ``srcList`` entry (§1.7).  Like a talkgroup's, it is only ever *filled in*: the column
    is user-editable, so a recorder rename must not overwrite what somebody typed.
    """
    stmt = (
        pg_insert(Radio)
        .values(
            system_id=system_id,
            radio_id=radio_id,
            wacn=wacn,
            home_system_id=home_system,
            alias=alias,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            call_count=1 if count else 0,
            # The recorder gives no hint whether a unit is a console, a portable or a
            # mobile, so ingest never guesses; the UI and the importer set it.
            kind="unknown",
        )
        .on_conflict_do_update(
            constraint="uq_radios_system_radio_wacn_home",
            set_={
                "alias": func.coalesce(Radio.alias, text("excluded.alias")),
                "first_seen_at": func.least(
                    func.coalesce(Radio.first_seen_at, text("excluded.first_seen_at")),
                    text("excluded.first_seen_at"),
                ),
                "last_seen_at": func.greatest(
                    func.coalesce(Radio.last_seen_at, text("excluded.last_seen_at")),
                    text("excluded.last_seen_at"),
                ),
                "call_count": Radio.call_count + (1 if count else 0),
            },
        )
        .returning(Radio.id)
    )
    return session.execute(stmt).scalar_one()


def fill_radio_home(session: Session, radio_pk: int, wacn: int | None, home_system: int | None) -> bool:
    """Fill a radio's ``wacn``/``home_system_id`` from a fully qualified sighting.

    These two columns are part of ``uq_radios_system_radio_wacn_home``, so this is not a
    cosmetic update: it changes the row's identity.  It is therefore done only when the row
    still carries the 0 sentinel (the state every row is in today, because no recorder has
    ever sent the source-ID extension), and only when no *other* row already occupies the
    fully qualified key — merging two radios is a decision this function has no business
    taking.  Returns whether anything moved.
    """
    if not wacn and not home_system:
        return False
    radio = session.get(Radio, radio_pk)
    if radio is None or radio.wacn or radio.home_system_id:
        return False
    target_wacn, target_home = int(wacn or 0), int(home_system or 0)
    clash = session.scalar(
        select(Radio.id).where(
            Radio.system_id == radio.system_id,
            Radio.radio_id == radio.radio_id,
            Radio.wacn == target_wacn,
            Radio.home_system_id == target_home,
            Radio.id != radio.id,
        )
    )
    if clash is not None:
        log.debug("radio %s already exists fully qualified as %s; leaving both", radio.id, clash)
        return False
    radio.wacn = target_wacn
    radio.home_system_id = target_home
    # ``SessionLocal`` runs with ``autoflush=False``, so without this the caller's next
    # ``ON CONFLICT`` upsert would look for a key this row does not carry *yet*, insert a
    # second row, and only then hit the unique violation at commit.
    session.flush()
    return True


def pick_radio_row(
    session: Session,
    system_id: int,
    *,
    radio_id: int,
    wacn: int | None,
    home_system: int | None,
) -> int | None:
    """Which ``radios`` row this sighting *is*, without writing anything.

    The selection half of :func:`resolve_radio`, split out because a second caller needs
    exactly the same answer and must not write: reconciliation resolves a record's two
    radios **before** the call is stored, and until it shared this function it asked a much
    looser question — "any row in this system with this ``radio_id``" — which one unit can
    answer five different ways.  One radio in a real archive had five rows (bare, and four
    home-system spellings); the loose lookup returned whichever the planner listed last, so
    a unit call's ``(source, target)`` pair rarely named the rows the stored calls actually
    carry, no candidate came back, and every private call reached the screen twice until
    the half-hourly batch pass (which compares stored rows, not a record) cleaned up.

    ``None`` means no row fits and :func:`resolve_radio` would mint one — which is also the
    right answer for a matcher: a radio the archive has never seen has no earlier calls.
    """
    target_wacn, target_home = int(wacn or 0), int(home_system or 0)
    rows = {
        (row.wacn, row.home_system_id): row.id
        for row in session.execute(
            select(Radio.id, Radio.wacn, Radio.home_system_id)
            .where(Radio.system_id == system_id, Radio.radio_id == radio_id)
            # Deterministic: with several spellings of one unit on file, "the first row"
            # has to mean the same row on every call, and the oldest is the one the rest
            # were split off from.
            .order_by(Radio.id)
        )
    }
    if not rows:
        return None
    exact = rows.get((target_wacn, target_home))
    if exact is not None:
        return exact
    if target_wacn == 0 and target_home == 0 and len(rows) == 1:
        # A bare sighting joins the one qualified row on file; more than one means two
        # genuinely different radios roaming in, and choosing between them is not ours.
        return next(iter(rows.values()))
    if (target_wacn or target_home) and (0, 0) in rows:
        return rows[(0, 0)]
    return None


def resolve_radio(
    session: Session,
    system_id: int,
    *,
    radio_id: int,
    wacn: int | None,
    home_system: int | None,
    seen_at: datetime,
    count: bool = True,
    alias: str | None = None,
) -> int:
    """The ``radios`` row for a sighting that may or may not be fully qualified.

    ``upsert_radio`` keys on ``(system, radio_id, wacn, home_system_id)``, which is right
    when the *recorder* always names a unit the same way — and it never does once the
    source-ID extension exists.  P25 LC opcode 9 rides some transmissions and not others,
    and a control-channel registration carries the WACN while the affiliation two seconds
    later does not, so the same physical radio arrives bare and qualified by turns.
    Upserting each spelling on its own key would file one unit as two rows and split its
    history down the middle.

    So: prefer the exact key; otherwise, adopt the row already there.  A qualified sighting
    fills a bare row in place (:func:`fill_radio_home`); a bare sighting joins the one
    qualified row if there is exactly one and no bare row.  "Exactly one" is the whole
    caution: two qualified rows for one id means two genuinely different radios roaming
    from two systems, and choosing between them is not this function's business.
    """
    target_wacn, target_home = int(wacn or 0), int(home_system or 0)
    existing = pick_radio_row(
        session, system_id, radio_id=radio_id, wacn=target_wacn, home_system=target_home
    )
    if existing is not None and (target_wacn or target_home):
        # The adopted row may be the *bare* one, which a qualified sighting fills in place.
        adopted = session.get(Radio, existing)
        if adopted is not None and adopted.wacn == 0 and adopted.home_system_id == 0:
            fill_radio_home(session, existing, target_wacn, target_home)

    if existing is not None:
        # Route the touch through the same upsert, on the key the row actually has now, so
        # first/last seen and the alias fill follow one rule and not two.
        radio = session.get(Radio, existing)
        return upsert_radio(
            session,
            system_id,
            radio_id=radio_id,
            wacn=radio.wacn,
            home_system=radio.home_system_id,
            seen_at=seen_at,
            count=count,
            alias=alias,
        )

    return upsert_radio(
        session,
        system_id,
        radio_id=radio_id,
        wacn=target_wacn,
        home_system=target_home,
        seen_at=seen_at,
        count=count,
        alias=alias,
    )


def upsert_patch(
    session: Session,
    system_id: int,
    *,
    patch_id: int,
    member_tgids: list[int],
    seen_at: datetime,
    count: bool = True,
) -> int:
    """A patch is identified by ``(patch_id, member set)``: dispatch regenerates the id
    every time the patch is re-created, so the members are the stable half."""
    stmt = (
        pg_insert(Patch)
        .values(
            system_id=system_id,
            patch_id=patch_id,
            member_tgids=member_tgids,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            call_count=1 if count else 0,
        )
        .on_conflict_do_update(
            constraint="uq_patches_system_id_members",
            set_={
                "first_seen_at": func.least(
                    func.coalesce(Patch.first_seen_at, text("excluded.first_seen_at")),
                    text("excluded.first_seen_at"),
                ),
                "last_seen_at": func.greatest(
                    func.coalesce(Patch.last_seen_at, text("excluded.last_seen_at")),
                    text("excluded.last_seen_at"),
                ),
                "call_count": Patch.call_count + (1 if count else 0),
            },
        )
        .returning(Patch.id)
    )
    return session.execute(stmt).scalar_one()


@dataclass
class CallRadios:
    """Every ``radios`` row one call needs, already upserted in ascending ``radio_id``.

    ``sources`` maps a ``srcList`` id to the row it resolved to, including the ids that
    simply reuse ``primary`` — :func:`write_call_sources` reads it instead of upserting.
    """

    primary: int | None = None
    target: int | None = None
    sources: dict[int, int] = field(default_factory=dict)


def upsert_call_radios(
    session: Session,
    system_id: int,
    record: CallRecord,
    *,
    seen_at: datetime,
    count: bool,
    primary_wacn: int = 0,
    primary_home: int = 0,
    primary_alias: str | None = None,
    target_radio_id: int | None = None,
    resolve_primary: bool = False,
) -> CallRadios:
    """Take every ``radios`` row this call touches, once, in ascending ``radio_id``.

    §5.2b: the call paths used to upsert the source radio, then the target, then the
    ``srcList`` radios in the order the recorder printed them — three unrelated orders,
    so two uploads sharing two units could take the two rows the opposite way round and
    deadlock.  Here the ids are collected first and the upserts are issued in one sorted
    pass; who gets which flavour of upsert is unchanged.

    ``resolve_primary`` is the push path's adopt-or-create :func:`resolve_radio` (the
    source-ID extension arrives beside a bare ``source``); the file path has no extension
    to disagree with and keeps the plain :func:`upsert_radio`.
    """
    radios = CallRadios()
    primary_radio_id = record.radio.radio_id if record.radio is not None else None

    # Which ``srcList`` entry owns the directory row for its id.  The *first* entry
    # printed with that id decides — its ``tag`` is the alias hint that lands — except
    # that a seq-0 entry reuses the record's own radio rather than re-keying a bare id,
    # so it owns nothing.  Both are exactly what the one-pass loop below still does.
    owners: dict[int, CallSourceRef] = {}
    reuses_primary: set[int] = set()
    for source in record.sources:
        if source.src is None or source.src in owners or source.src in reuses_primary:
            continue
        if source.seq == 0 and primary_radio_id is not None:
            reuses_primary.add(source.src)
        else:
            owners[source.src] = source

    def _primary() -> None:
        if resolve_primary:
            radios.primary = resolve_radio(
                session,
                system_id,
                radio_id=primary_radio_id,
                wacn=primary_wacn,
                home_system=primary_home,
                seen_at=seen_at,
                count=count,
                alias=primary_alias,
            )
        else:
            radios.primary = upsert_radio(
                session,
                system_id,
                radio_id=primary_radio_id,
                wacn=primary_wacn,
                home_system=primary_home,
                seen_at=seen_at,
                count=count,
                alias=primary_alias,
            )

    def _target() -> None:
        # ``count=False`` always: the target did not key up, and counting the call against
        # its ``call_count`` would make a radio that never transmitted look like one that did.
        radios.target = resolve_radio(
            session,
            system_id,
            radio_id=target_radio_id,
            wacn=0,
            home_system=0,
            seen_at=seen_at,
            count=False,
        )

    def _source(src: int, owner: CallSourceRef) -> Callable[[], None]:
        def run() -> None:
            radios.sources[src] = upsert_radio(
                session,
                system_id,
                radio_id=src,
                wacn=0,
                home_system=0,
                seen_at=record.started_at,
                count=count,
                alias=owner.tag,
            )

        return run

    # (radio_id, rank) — rank only breaks a tie, so a unit call from a radio to itself
    # still takes its row once for the source and once for the target, in that order.
    jobs: list[tuple[tuple[int, int], Callable[[], None]]] = []
    if primary_radio_id is not None:
        jobs.append(((primary_radio_id, 0), _primary))
    if target_radio_id is not None:
        jobs.append(((target_radio_id, 1), _target))
    for src, owner in owners.items():
        jobs.append(((src, 2), _source(src, owner)))
    for _, run in sorted(jobs, key=lambda job: job[0]):
        run()
    for src in reuses_primary:
        # The record's radio carries the WACN and home system a fully-qualified sdrtrunk
        # id has and a bare ``src`` does not, so reuse rather than re-key.
        if radios.primary is not None:
            radios.sources[src] = radios.primary
    return radios


def write_call_sources(
    session: Session,
    call: Call,
    record: CallRecord,
    system_id: int,
    *,
    primary_radio_pk: int | None,
    primary_talkgroup_pk: int | None,
    count: bool,
    radios: dict[int, int] | None = None,
) -> int | None:
    """Write one ``call_sources`` row per keying radio, and the directory rows behind them.

    A trunked call is a short conversation: trunk-recorder's ``srcList`` names every radio
    that keyed up and where in the audio it did (§1.7).  sdrtrunk can only see the first,
    so its record carries exactly one source at offset 0 — the two dialects come through
    here identically, which is the point.

    Every distinct radio gets a directory row (with the recorder's ``tag`` as an alias hint)
    and an affiliation touch, because "which units were on this channel" should count the
    third unit on a call as much as the first.  Returns the primary radio's pk — the first
    source's — so the caller can denormalize it onto ``calls.radio_id``.

    The first source is assumed to be the radio the caller already upserted (they are built
    from the same fact), so it is not counted a second time.

    ``radios`` is :attr:`CallRadios.sources` — the directory rows, already taken in
    ascending ``radio_id`` by :func:`upsert_call_radios` (§5.2b).  Without it this falls
    back to upserting them here, in ``srcList`` order, which is what the callers did
    before the lock order was pinned down.
    """
    if not record.sources:
        return primary_radio_pk

    resolved = primary_radio_pk
    # One radio may key up several times in one call; it is one unit in the directory and
    # one affiliation, but every keying is its own row.
    provided = radios or {}
    seen: dict[int, int] = {}
    affiliate: list[int] = []
    for source in record.sources:
        radio_pk: int | None = None
        if source.src is not None:
            if source.seq == 0 and primary_radio_pk is not None:
                # The record's radio carries the WACN and home system a fully-qualified
                # sdrtrunk id has and a bare ``src`` does not, so reuse rather than re-key.
                radio_pk = primary_radio_pk
            elif source.src in provided:
                radio_pk = provided[source.src]
            elif source.src in seen:
                radio_pk = seen[source.src]
            else:
                radio_pk = upsert_radio(
                    session,
                    system_id,
                    radio_id=source.src,
                    wacn=0,
                    home_system=0,
                    seen_at=record.started_at,
                    count=count,
                    alias=source.tag,
                )
            seen[source.src] = radio_pk
            if count and primary_talkgroup_pk is not None:
                affiliate.append(radio_pk)
        if source.seq == 0 and radio_pk is not None:
            resolved = radio_pk
        session.add(
            CallSource(
                call_id=call.id,
                radio_id=radio_pk,
                src=source.src,
                pos_s=Decimal(str(round(source.pos_s, 3))),
                len_s=None if source.len_s is None else Decimal(str(round(source.len_s, 3))),
                emergency=source.emergency,
                tag=source.tag,
                seq=source.seq,
            )
        )
    # Affiliations last and in ascending radio order (§5.2b).  One row per keying, as
    # before — a unit that keys three times counts three times on the pair.
    for radio_pk in sorted(affiliate):
        touch_affiliation(session, radio_pk, primary_talkgroup_pk, record.started_at)
    return resolved


def touch_affiliation(session: Session, radio_pk: int, talkgroup_pk: int, seen_at: datetime) -> None:
    """Update the rolling "this radio was last heard on that talkgroup" row."""
    stmt = pg_insert(RadioAffiliation).values(
        radio_id_fk=radio_pk,
        talkgroup_id_fk=talkgroup_pk,
        first_heard_at=seen_at,
        last_heard_at=seen_at,
        call_count=1,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_radio_affiliations_radio_talkgroup",
            set_={
                "first_heard_at": func.least(
                    func.coalesce(RadioAffiliation.first_heard_at, text("excluded.first_heard_at")),
                    text("excluded.first_heard_at"),
                ),
                "last_heard_at": func.greatest(
                    func.coalesce(RadioAffiliation.last_heard_at, text("excluded.last_heard_at")),
                    text("excluded.last_heard_at"),
                ),
                "call_count": RadioAffiliation.call_count + 1,
            },
        )
    )


# --------------------------------------------------------------------------------------
# dedupe / reconciliation (5.1 step 4)
#
# ``RECONCILE_ENABLED`` (the default) replaces the two functions below with
# :mod:`app.ingest.reconcile`: interval matching instead of start equality, a score instead
# of arrival order, and the losing copy stored rather than dropped.  ``lock_dedupe`` and
# ``find_duplicate`` are what the flag falls back to, and they are still the whole story
# for a deployment with one recorder.
# --------------------------------------------------------------------------------------


#: ``pg_advisory_xact_lock`` class ids.  4242 serializes conversation assignment; 4243
#: serializes the dedupe check-then-insert.  They are separate namespaces so the two locks
#: cannot collide on the same key.
DEDUPE_LOCK_CLASS = 4243


def lock_dedupe(session: Session, tgid: int | None, started_at: datetime) -> None:
    """Serialize the duplicate check and the insert that follows it (M4).

    :func:`find_duplicate` is a SELECT and the write is a separate INSERT, so two ingest
    workers handed the same recording by two paths — the watcher and the recorder's push,
    or two threads on a ``_V2`` pair — could each look, each see nothing, and each write.
    The lock is transaction-scoped and keyed on the thing that makes two calls duplicates:
    the primary talkgroup and the second the call started in.  Unrelated calls never wait
    on each other.
    """
    key = ((tgid or 0) * 1_000_003 + int(started_at.timestamp())) % 2_147_483_647
    session.execute(
        text("SELECT pg_advisory_xact_lock(:cls, :key)"),
        {"cls": DEDUPE_LOCK_CLASS, "key": key},
    )


def find_duplicate(session: Session, record: CallRecord, settings: Settings) -> int | None:
    """Return the id of the call this recording duplicates, or None.

    Two rungs, in order:

    1. **exact** — identical ``sha256``.  The same bytes under two paths is the same call,
       whatever the metadata says.
    2. **fuzzy** — the talkgroup sets overlap, the starts are within
       ``DEDUPE_WINDOW_SECONDS`` and the durations within ``DEDUPE_DURATION_TOLERANCE``.
       That is the sdrtrunk ``_V2`` case and the two-recorders-one-system case.
    """
    # A metadata-only call (an encrypted P25 call pushed without audio) has no
    # bytes to hash: ``record.sha256`` is None, and ``Call.sha256 == None``
    # would render as ``IS NULL`` and match every other audio-less call.
    exact = None
    if record.sha256:
        exact = session.execute(
            select(Call.id)
            .where(Call.sha256 == record.sha256, Call.file_path != record.file_path)
            .order_by(Call.id)
            .limit(1)
        ).scalar_one_or_none()
    if exact is not None:
        return exact

    if not record.talkgroup_ids:
        return None

    window = timedelta(seconds=settings.dedupe_window_seconds)
    duration = Decimal(str(round(record.duration_s, 3)))
    tolerance = Decimal(str(settings.dedupe_duration_tolerance))
    # |a - b| <= tol * max(a, b): a relative tolerance that stays symmetric.
    span = func.greatest(Call.duration_s, duration) * tolerance

    fuzzy = session.execute(
        select(Call.id)
        .where(
            Call.file_path != record.file_path,
            Call.duplicate_of.is_(None),
            Call.talkgroup_ids.overlap(list(record.talkgroup_ids)),
            Call.started_at >= record.started_at - window,
            Call.started_at <= record.started_at + window,
            func.abs(Call.duration_s - duration) <= span,
        )
        .order_by(Call.id)
        .limit(1)
    ).scalar_one_or_none()
    return fuzzy


# --------------------------------------------------------------------------------------
# conversations (3.2)
# --------------------------------------------------------------------------------------


def assign_conversation(session: Session, call: Call, gap_seconds: int) -> int | None:
    """Attach the call to the open conversation on its talkgroup, or start one.

    Serialized per talkgroup with a transaction-scoped advisory lock so two ingest workers
    handling adjacent calls cannot each start a conversation for the same thread.
    """
    if call.talkgroup_id is None:
        return None

    session.execute(
        text("SELECT pg_advisory_xact_lock(:cls, :key)"),
        {"cls": CONVERSATION_LOCK_CLASS, "key": call.talkgroup_id},
    )
    gap = timedelta(seconds=gap_seconds)
    conv = session.execute(
        select(Conversation)
        .where(
            Conversation.talkgroup_id == call.talkgroup_id,
            Conversation.ended_at >= call.started_at - gap,
            Conversation.started_at <= call.ended_at + gap,
        )
        .order_by(Conversation.ended_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if conv is None:
        conv = Conversation(
            talkgroup_id=call.talkgroup_id,
            started_at=call.started_at,
            ended_at=call.ended_at,
            call_count=1,
            first_call_id=call.id,
        )
        session.add(conv)
        session.flush()
    else:
        conv.started_at = min(conv.started_at, call.started_at)
        conv.ended_at = max(conv.ended_at, call.ended_at)
        conv.call_count += 1
        if conv.first_call_id is None:
            conv.first_call_id = call.id
    call.conversation_id = conv.id
    return conv.id


# --------------------------------------------------------------------------------------
# transcription queue (5.1 step 5)
# --------------------------------------------------------------------------------------


def audio_policy_for(talkgroup: Talkgroup | None) -> str:
    """``keep`` or ``discard`` — what the ingest writes onto ``calls.audio_policy``.

    Read off the talkgroup's ``record`` toggle, exactly the way ``transcript_status`` is
    read off its ``transcribe`` toggle a few lines below, and for the same reason: the
    decision belongs to the moment the call landed.  Nothing re-derives it afterwards, so
    turning Record back on does not make yesterday's calls keepers and turning it off does
    not condemn a year of archive.

    A call with no talkgroup — conventional traffic, a private call — has no toggle to
    read and is always ``keep``.
    """
    return "discard" if talkgroup is not None and not talkgroup.record else "keep"


def enqueue_audio_process(
    session: Session,
    call: Call,
    settings: Settings,
    *,
    priority: int | None = None,
) -> int | None:
    """Queue the call for the audio-processing pass (§5.5), ahead of transcription.

    Ahead, but never *blocking*: the two queues are independent, and a transcription job
    that reaches a worker before the processed copy exists simply transcribes the original.
    That is the pre-§5.5 behavior, so the worst case of the ordering being lost is the
    behavior we already had.

    Unlike :func:`enqueue_transcription` this ignores the talkgroup's policy: a channel
    nobody transcribes is still a channel somebody *listens* to, and the whole point of the
    processed copy is that playback is even across the archive.
    """
    from app.audioproc.jobs import enqueue as enqueue_audio  # noqa: PLC0415 - imports numpy

    try:
        # A SAVEPOINT, because swallowing a failed statement is not enough on PostgreSQL:
        # it aborts the caller's transaction, and the ingest's COMMIT then rolls the call
        # back without raising.  Only the audio job may be lost here.
        with session.begin_nested():
            return enqueue_audio(session, call, settings, priority=priority)
    except Exception:  # noqa: BLE001 - audio processing must never block an ingest
        log.exception("audioproc: could not queue call %s", call.id)
        return None


def enqueue_transcription(
    session: Session,
    call: Call,
    talkgroup: Talkgroup | None,
    settings: Settings,
    *,
    priority: int | None = None,
) -> int | None:
    """Queue the call when the talkgroup's policy allows it and it is long enough.

    A shorter recording is a squelch tail (14 % of the corpus is under a second) that
    Whisper reliably hallucinates over, so it is marked ``skipped`` rather than queued.

    ``priority`` overrides the talkgroup's own: the push path resolves the upload's
    ``priority`` class through :meth:`app.ingest.flow.PriorityChoice.resolve` and passes the
    result here, which is how a migrated recording queues behind live traffic
    (``backlog`` = 500 + the talkgroup's priority) without the policy leaking into ingest.
    The policy checks above still apply — a backlog upload for a talkgroup nobody
    transcribes is still skipped, not queued at 600.
    """
    if talkgroup is not None and not talkgroup.transcribe:
        call.transcript_status = "skipped"
        return None

    minimum = settings.transcribe_min_seconds
    if talkgroup is not None and talkgroup.transcribe_min_seconds is not None:
        minimum = float(talkgroup.transcribe_min_seconds)
    if float(call.duration_s) < minimum:
        call.transcript_status = "skipped"
        return None

    # ``calls`` before ``transcription_jobs`` (§5.2): the call row is locked — and the
    # pending ORM writes on it flushed — before a job row is written for it, so this path
    # cannot meet the workers head-on.
    lock_calls(session, call.id)
    stmt = (
        pg_insert(TranscriptionJob)
        .values(
            call_id=call.id,
            status="queued",
            priority=(
                priority
                if priority is not None
                else (talkgroup.priority if talkgroup is not None else 100)
            ),
            max_attempts=settings.transcribe_max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=["call_id"],
            index_where=text("status IN ('queued', 'running')"),
        )
        .returning(TranscriptionJob.id)
    )
    job_id = session.execute(stmt).scalar_one_or_none()
    call.transcript_status = "queued"
    return job_id


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


def record_ledger(
    session: Session,
    file_path: str,
    status: str,
    *,
    call_id: int | None = None,
    run_id: int | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    stmt = pg_insert(IngestFile).values(
        file_path=file_path,
        status=status,
        call_id=call_id,
        run_id=run_id,
        error=error,
        attempts=1,
        first_seen_at=now,
        last_attempt_at=now,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[IngestFile.file_path],
            set_={
                "status": status,
                "call_id": call_id,
                "run_id": run_id,
                "error": error,
                "attempts": IngestFile.attempts + 1,
                "last_attempt_at": now,
            },
        )
    )


# --------------------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------------------


def _relative(path: Path, audio_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(audio_root.resolve()))
    except ValueError:
        return str(path)


def ingest_file(
    session: Session,
    path: str | Path,
    *,
    settings: Settings | None = None,
    run_id: int | None = None,
    audio_root: str | Path | None = None,
) -> IngestResult:
    """Ingest one recording.  Commits on success; on failure rolls back and files the
    error in the ledger.  Calling it twice on the same path is a no-op the second time."""
    # The Admin page's ingest knobs (the dedupe window, the conversation gap, the
    # transcription policy) live in the settings table; layer them over whatever the
    # caller started with so a change takes effect without restarting the worker.
    settings = effective_settings(session, base=settings)
    root = Path(audio_root) if audio_root is not None else Path(settings.audio_root)
    path = Path(path)
    if is_trunk_recorder_call(path):
        # A trunk-recorder call is up to three files sharing a stem, and any of them may be
        # what the caller was handed.  Resolve to the one the call is *stored* as before
        # anything else, so ``rel`` — the ledger's key, the idempotence check and the row's
        # ``file_path`` — is the same string whichever file arrived (§1.7).  This does not
        # wait for a missing sidecar; the watcher does that before it gets here.
        path = await_trunk_recorder_call(
            path, prefer_audio=settings.tr_prefer_audio, wait_seconds=0.0
        )
    rel = _relative(path, root)

    existing = session.execute(select(Call.id).where(Call.file_path == rel)).scalar_one_or_none()
    if existing is not None:
        session.rollback()
        return IngestResult(status="skipped", file_path=rel, call_id=existing)

    try:
        result = _ingest_new(session, path, rel, root, settings, run_id)
        session.commit()
        return result
    except IntegrityError:
        # Another worker inserted this exact ``file_path`` between the check above and this
        # commit.  That is not a failure: the row exists, which is all the caller wanted
        # (M4).  It is filed as ``skipped``, the same as any path the ledger already had.
        session.rollback()
        existing = session.execute(select(Call.id).where(Call.file_path == rel)).scalar_one_or_none()
        log.info("ingest: %s was written by another worker; skipping", rel)
        try:
            # ``ingest_file_status`` has no 'skipped' member — 'ignored' is its name for
            # "already accounted for, nothing to do", which is exactly this.
            record_ledger(
                session,
                rel,
                "ignored",
                run_id=run_id,
                call_id=existing,
                error="another worker ingested this path first",
            )
            session.commit()
        except Exception:  # pragma: no cover - the ledger write itself failed
            session.rollback()
        return IngestResult(status="skipped", file_path=rel, call_id=existing)
    except Exception as exc:  # noqa: BLE001 - every failure belongs in the ledger
        session.rollback()
        message = f"{type(exc).__name__}: {exc}"
        log.warning("ingest failed for %s: %s", rel, message)
        try:
            record_ledger(session, rel, "failed", run_id=run_id, error=message[:2000])
            session.commit()
        except Exception:  # pragma: no cover - the ledger write itself failed
            session.rollback()
        return IngestResult(status="failed", file_path=rel, error=message)


def _ingest_new(
    session: Session,
    path: Path,
    rel: str,
    root: Path,
    settings: Settings,
    run_id: int | None,
) -> IngestResult:
    record = build_call_record(path, root, tz=settings.tz, prefer_audio=settings.tr_prefer_audio)
    if not record.talkgroup_ids and record.tgid is None:
        raise ParseError(f"no talkgroup in ID3 or filename: {record.file_name}")

    cfg = ReconcileConfig.of(settings)
    wacn = record.radio.wacn if record.radio and record.radio.wacn else None

    # Step 4 runs in two halves.  This one — before anything is written — answers "is this
    # a second copy of a transmission the archive already has?", because that is what says
    # whether the directory's counters may move.  The second half runs after the insert,
    # where there are two rows to compare and one of them can be handed the primary seat.
    match_id: int | None = None
    match = None
    if cfg.enabled:
        lock_reconcile(
            session,
            talkgroup_ids=record.talkgroup_ids,
            tgid=record.tgid,
            frequency_hz=record.frequency_hz,
        )
        system_id = upsert_system(session, record.system, protocol=record.decoder, wacn=wacn)
        matched, match = find_match_for_record(session, record, system_id, cfg)
        match_id = matched.id if matched is not None else None
        duplicate_of = match_id
    else:
        lock_dedupe(session, record.tgid, record.started_at)
        duplicate_of = find_duplicate(session, record, settings)
        system_id = upsert_system(session, record.system, protocol=record.decoder, wacn=wacn)
    is_duplicate = duplicate_of is not None
    # A second copy is kept but must not inflate the directory's counters or be transcribed
    # a second time, so every "touch" below is told not to count it.  That holds even when
    # the newcomer goes on to win the primary seat: the transmission was counted once
    # already, by the copy it is about to displace.
    count = not is_duplicate

    site_id = upsert_site(session, system_id, record.site)

    # Every talkgroup involved (all patch members, or the single group) gets a row and a
    # count; the primary one also becomes calls.talkgroup_id.
    tgids = list(record.talkgroup_ids) or ([record.tgid] if record.tgid is not None else [])
    primary_tg: Talkgroup | None = None
    # Ascending ``tgid``, and talkgroups before radios: the §5.2b lock order.  ``tgids``
    # itself keeps the recorder's order — it is stored on ``calls.talkgroup_ids``.
    for tgid in sorted(tgids):
        alias = record.member_aliases.get(tgid) or (
            record.talkgroup_alias if tgid == record.tgid else None
        )
        tg = upsert_talkgroup(
            session, system_id, tgid, alias=alias, seen_at=record.started_at, count=count,
            settings=settings,
        )
        if tgid == record.tgid:
            primary_tg = tg
            # A trunk-recorder sidecar names the talkgroup's group and description; fill
            # them where the directory has nothing and never overwrite a human's edit.
            if tg.group_name is None and record.talkgroup_group:
                tg.group_name = record.talkgroup_group
            if tg.description is None and record.talkgroup_description:
                tg.description = record.talkgroup_description

    patch_pk = None
    if record.patch_id is not None and record.member_tgids:
        patch_pk = upsert_patch(
            session,
            system_id,
            patch_id=record.patch_id,
            member_tgids=list(record.member_tgids),
            seen_at=record.started_at,
            count=count,
        )

    # Every radio the call touches — the keying unit and every ``srcList`` entry — in one
    # ascending pass, after the talkgroups and before the affiliations (§5.2b).
    radios = upsert_call_radios(
        session,
        system_id,
        record,
        seen_at=record.started_at,
        count=count,
        primary_wacn=record.radio.wacn if record.radio is not None else 0,
        primary_home=record.radio.home_system if record.radio is not None else 0,
        primary_alias=record.radio_alias,
    )
    radio_pk = radios.primary

    call = Call(
        system_id=system_id,
        site_id=site_id,
        talkgroup_id=primary_tg.id if primary_tg is not None else None,
        talkgroup_ids=tgids,
        patch_id=patch_pk,
        radio_id=radio_pk,
        radio_raw=record.radio.raw if record.radio else None,
        ingest_source=record.ingest_source,
        radio_resolved=record.radio_resolved,
        recorded_at=record.recorded_at,
        started_at=record.started_at,
        started_local=record.started_local,
        ended_at=record.ended_at,
        time_source=record.time_source,
        duration_s=Decimal(str(round(record.duration_s, 3))),
        frequency_hz=record.frequency_hz,
        channel=record.channel,
        decoder=record.decoder,
        call_type=record.call_type,
        emergency=record.emergency,
        encrypted=record.encrypted,
        audio_type=record.audio_type,
        signal_db=record.signal_db,
        noise_db=record.noise_db,
        freq_error=record.freq_error,
        recorder_call_num=record.recorder_call_num,
        raw_meta=record.raw_meta or None,
        talkgroup_alias_at_record=record.talkgroup_alias,
        # An encrypted trunk-recorder call has a sidecar and no sample.  The row is kept —
        # it proves an agency held the talkgroup — with the retention vocabulary for
        # "the metadata is right and the bytes are not there" (§1.7).
        audio_state=record.audio_state,
        file_path=rel,
        file_name=record.file_name,
        file_size=record.file_size,
        sha256=record.sha256,
        bitrate=record.bitrate,
        sample_rate=record.sample_rate,
        channels=record.channels,
        recorder_version=record.recorder_version,
        file_mtime=record.file_mtime,
        duplicate_of=duplicate_of,
        transcript_status="pending",
        # The Record toggle, decided here and never again (§5.6).  The audio is still
        # stored: transcription and the exporters both need the file, and the discard pass
        # is what deletes it once they are finished with it.
        audio_policy=audio_policy_for(primary_tg),
    )
    session.add(call)
    session.flush()

    radio_pk = write_call_sources(
        session,
        call,
        record,
        system_id,
        primary_radio_pk=radio_pk,
        primary_talkgroup_pk=primary_tg.id if primary_tg is not None else None,
        count=count and not is_duplicate,
        radios=radios.sources,
    )
    if radio_pk != call.radio_id:
        call.radio_id = radio_pk

    job_id = None
    if match_id is not None:
        # The second half of step 4: score the two copies, merge the loser's metadata into
        # the winner and, when the newcomer is the better one, swap the seats.
        call.transcript_status = "skipped"
        outcome = reconcile_pair(
            session, call, session.get(Call, match_id), settings, cfg=cfg, match=match
        )
        duplicate_of = call.duplicate_of
        is_duplicate = duplicate_of is not None
        job_id = outcome.job_id
    elif is_duplicate:
        call.transcript_status = "skipped"
    elif record.audio_state != "present":
        # Nothing to transcribe: the encrypted-call row has no audio by construction.
        call.transcript_status = "skipped"
        assign_conversation(session, call, settings.conversation_gap_seconds)
    else:
        if radio_pk is not None and primary_tg is not None and not record.sources:
            touch_affiliation(session, radio_pk, primary_tg.id, record.started_at)
        assign_conversation(session, call, settings.conversation_gap_seconds)
        enqueue_audio_process(session, call, settings)
        job_id = enqueue_transcription(session, call, primary_tg, settings)

    status = "duplicate" if is_duplicate else "ingested"
    record_ledger(session, rel, status, call_id=call.id, run_id=run_id)

    notify(
        session,
        "call.new",
        {
            "id": call.id,
            "kind": call.kind,
            "target_radio_id": call.target_radio_id,
            "talkgroup_id": call.talkgroup_id,
            "talkgroup_ids": tgids,
            "started_at": call.started_at.isoformat(),
            "duration_s": float(call.duration_s),
            "duplicate": is_duplicate,
            # Read by the worker's LISTEN thread (app/wake.py): backlog does not wake it.
            "ingest_priority": call.ingest_priority,
        },
    )
    if not is_duplicate:
        # In this transaction, beside the bus event, so a rolled-back ingest leaves no
        # promise to deliver.  The losing copy of a reconciled pair is deliberately not
        # announced: a consumer wants the transmission once, not once per receiver.
        from app import webhooks  # noqa: PLC0415 - avoids an import cycle at module load

        webhooks.enqueue_call_event(session, "call.stored", call.id)
    had_audio = record.audio_state == "present"
    tg_label = (
        (primary_tg.alias or str(primary_tg.tgid))
        if primary_tg is not None
        else (str(record.tgid) if record.tgid is not None else "?")
    )
    priority = primary_tg.priority if primary_tg is not None else None
    if record.encrypted:
        detail = "encrypted, no audio"
    elif not had_audio:
        detail = "no audio"
    else:
        detail = f"{float(call.duration_s):.1f}s"
    activity.record(
        session,
        source="ingest",
        level="info",
        kind="call.duplicate" if is_duplicate else "call.accepted",
        message=(
            f"{record.system} {tg_label} "
            + ("duplicate" if is_duplicate else "accepted")
            + f" (p{priority}, {detail})"
        ),
        call_id=call.id,
        meta={
            "system": record.system,
            "talkgroup": tg_label,
            "priority": priority,
            "had_audio": had_audio,
            "duplicate": is_duplicate,
            "duration_s": float(call.duration_s),
        },
    )
    return IngestResult(
        status=status,
        file_path=rel,
        call_id=call.id,
        duplicate_of=duplicate_of,
        job_id=job_id,
    )


# --------------------------------------------------------------------------------------
# a tree
# --------------------------------------------------------------------------------------


#: directories the walk never descends into, as paths relative to the scan root.
#:
#: ``uploads`` is the one that matters.  Pushed calls are written *and their rows created*
#: by ``POST /api/call-upload``, which is also what writes their ledger entry, so the only
#: thing the walk could ever learn about that subtree is that every file in it is already
#: settled — for which it was enumerating nearly a million files in one directory, and will be
#: enumerating five million across the archive, on every ``INGEST_SCAN_MINUTES`` tick.  It
#: is the cost of the answer that changed, not the answer (docs/ARCHITECTURE.md, "Don't
#: enumerate the tree").
DEFAULT_SKIP_DIRS = frozenset({"uploads"})


def scan_skip_dirs(settings: Settings) -> frozenset[str]:
    """``INGEST_SCAN_SKIP`` as a set, falling back to :data:`DEFAULT_SKIP_DIRS`.

    An operator who empties the setting gets the unpruned walk back, which is the right
    escape hatch for a one-off ``keyup ingest scan`` over a tree that really does need it.
    """
    raw = (getattr(settings, "ingest_scan_skip", None) or "").strip()
    if not raw:
        return frozenset()
    return frozenset(part.strip().strip("/") for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class WalkCost:
    """What one :func:`walk_recordings` pass cost.  Logged; never used for a decision."""

    dirs: int = 0
    entries: int = 0
    candidates: int = 0
    pruned: int = 0
    elapsed_s: float = 0.0
    #: ``max_files`` stopped the walk before the tree ran out — a scan that did not finish
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "dirs": self.dirs,
            "entries": self.entries,
            "candidates": self.candidates,
            "pruned": self.pruned,
            "elapsed_s": round(self.elapsed_s, 3),
            "truncated": self.truncated,
        }


def walk_recordings(
    root: str | Path,
    *,
    prefer_audio: str = "m4a",
    skip_dirs: Iterable[str] = DEFAULT_SKIP_DIRS,
    max_files: int = 0,
) -> tuple[list[Path], WalkCost]:
    """:func:`iter_recordings`, plus what the walk cost — see there for the semantics."""
    root = Path(root)
    if root.is_file():
        return [root], WalkCost(entries=1, candidates=1)

    preferred = f".{prefer_audio.strip().lower().lstrip('.')}"
    #: preference order within one stem, lowest wins.  ``TR_PREFER_AUDIO`` goes to the
    #: front of the audio extensions; the sidecar is last, so it is only chosen when it is
    #: the only file with that stem.
    ranked = [preferred, *(s for s in AUDIO_SUFFIXES if s != preferred), SIDECAR_SUFFIX]
    order = {suffix: rank for rank, suffix in enumerate(ranked)}
    unranked = len(ranked)
    skip = {s.strip("/") for s in skip_dirs if s.strip("/")}

    best: dict[tuple[Path, str], Path] = {}
    dirs = entries = pruned = 0
    truncated = False
    started = time.monotonic()
    # ``os.scandir`` rather than ``rglob`` for one reason: it can be told not to descend.
    # ``rglob`` yields the whole tree and leaves filtering to the consumer, which is the
    # same as walking it.
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack and not truncated:
        directory, rel = stack.pop()
        dirs += 1
        try:
            scan = os.scandir(directory)
        except OSError as exc:  # pragma: no cover - a directory that went away or is ours not to read
            log.warning("scan: cannot read %s: %s", directory, exc)
            continue
        with scan:
            for entry in scan:
                entries += 1
                child = f"{rel}/{entry.name}" if rel else entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:  # pragma: no cover - it went away mid-walk
                    continue
                if is_dir:
                    # Hidden directories are never a recording tree: ``uploads/.incoming``
                    # holds bodies that are still being received and have no row yet.
                    if entry.name.startswith(".") or child in skip:
                        pruned += 1
                        continue
                    stack.append((Path(entry.path), child))
                    continue

                path = Path(entry.path)
                suffix = path.suffix.lower()
                name = path.name.lower()
                if name.endswith(".raw.wav") or is_keyup_artifact(name):
                    continue
                if suffix == SIDECAR_SUFFIX:
                    # Only a trunk-recorder sidecar is a candidate; some other .json under
                    # the tree (a playlist, an export manifest) is not a recording.
                    if not is_trunk_recorder_name(path) and not _has_sibling_audio(path):
                        continue
                elif suffix not in AUDIO_SUFFIXES:
                    continue

                key = (path.parent, stem_of(path))
                current = best.get(key)
                if current is None or order.get(suffix, unranked) < order.get(
                    current.suffix.lower(), unranked
                ):
                    best[key] = path
                if max_files and len(best) >= max_files:
                    truncated = True
                    break

    cost = WalkCost(
        dirs=dirs,
        entries=entries,
        candidates=len(best),
        pruned=pruned,
        elapsed_s=time.monotonic() - started,
        truncated=truncated,
    )
    if truncated:
        log.warning(
            "scan: stopped at INGEST_SCAN_MAX_FILES=%d under %s — this scan did not finish; "
            "%s",
            max_files,
            root,
            cost.as_dict(),
        )
    else:
        log.info("scan: walked %s — %s", root, cost.as_dict())
    return sorted(best.values()), cost


def iter_recordings(
    root: str | Path,
    *,
    prefer_audio: str = "m4a",
    skip_dirs: Iterable[str] = DEFAULT_SKIP_DIRS,
    max_files: int = 0,
) -> list[Path]:
    """Every call under ``root`` — exactly one path per call — sorted, so a scan repeats.

    The walk is recursive, so every layout in use works: sdrtrunk's one flat folder, this
    archive's ``YYYYMMDD/<alias>/`` sort (§1.1), and trunk-recorder's
    ``<shortName>/<Y>/<M>/<D>/`` (§1.7).

    **It is not a whole-archive enumeration.**  ``skip_dirs`` prunes subtrees the walk has
    no business in — :data:`DEFAULT_SKIP_DIRS`, i.e. ``uploads``, whose files are written
    and recorded by the push endpoint itself — and hidden directories are never descended
    into.  Pruning happens *before* the descent, which is why this is ``os.scandir`` and no
    longer ``rglob``.  ``max_files`` is a guard-rail on top of that: it stops the walk and
    says so, loudly, because a truncated scan is a scan that did not finish.

    "One path per call" is the whole job here, because a trunk-recorder call is up to three
    files sharing a stem:

    * a ``.wav`` **and** an ``.m4a`` are the same audio kept twice — ``TR_PREFER_AUDIO``
      picks which one becomes the call, and the other is not a second call;
    * the ``.json`` sidecar is metadata for the audio beside it, and is only yielded on its
      own when there is no audio at all — the encrypted-call case, which is still a call;
    * ``.raw.wav`` is trunk-recorder's undecoded baseband debugging capture and never is;
    * ``<stem>.proc.mp3`` (and its ``.proc-*.mp3`` temp) is Keyup's own processed copy,
      written beside the original, and never is either.
    """
    found, _cost = walk_recordings(
        root, prefer_audio=prefer_audio, skip_dirs=skip_dirs, max_files=max_files
    )
    return found


def _has_sibling_audio(sidecar: Path) -> bool:
    stem = stem_of(sidecar)
    return any(sidecar.with_name(stem + suffix).is_file() for suffix in AUDIO_SUFFIXES)


def _sidecar_still_coming(path: Path, now: float, wait_seconds: float) -> bool:
    """True for a freshly written trunk-recorder audio file with no sidecar yet."""
    if path.suffix.lower() == SIDECAR_SUFFIX or not is_trunk_recorder_call(path):
        return False
    if sidecar_for(path).is_file():
        return False
    try:
        age = now - path.stat().st_mtime
    except OSError:  # pragma: no cover - the file went away between the walk and here
        return False
    return age < wait_seconds


def ingest_tree(
    session_factory: Callable[[], Session],
    root: str | Path,
    *,
    workers: int = 4,
    settings: Settings | None = None,
    audio_root: str | Path | None = None,
    kind: str = "scan",
) -> IngestRunSummary:
    """Scan a tree.  Safe to re-run: paths the ledger already settled are skipped.

    Files are handed to ``workers`` threads, each with its own session.  The work is
    dominated by hashing and MP3 parsing, which release the GIL, so threads are the right
    tool and psycopg's connection-per-thread rule is respected.
    """
    settings = settings or get_settings()
    audio_root = Path(audio_root) if audio_root is not None else Path(settings.audio_root)
    root = Path(root)
    started = time.monotonic()

    bootstrap = session_factory()
    try:
        run = IngestRun(kind=kind, root=str(root))
        bootstrap.add(run)
        bootstrap.commit()
        run_id = run.id
        # One query rather than one per file: the ledger is the skip list.
        done = set(
            bootstrap.execute(
                select(IngestFile.file_path).where(IngestFile.status.in_(("ingested", "duplicate")))
            )
            .scalars()
            .all()
        )
    finally:
        bootstrap.close()

    # Bounded, and the bound is logged: this used to be an unqualified walk of the whole
    # audio root, which on this archive is millions of files every scan tick.
    files, cost = walk_recordings(
        root,
        prefer_audio=settings.tr_prefer_audio,
        skip_dirs=scan_skip_dirs(settings),
        max_files=max(0, int(settings.ingest_scan_max_files or 0)),
    )
    summary = IngestRunSummary(run_id=run_id, root=str(root), files_seen=len(files))
    summary.walk = cost

    pending = []
    now = time.time()
    for path in files:
        if _relative(path, audio_root) in done:
            summary.files_skipped += 1
        elif _sidecar_still_coming(path, now, settings.tr_sidecar_wait_seconds):
            # A scan can run while trunk-recorder is mid-call: the audio is on disk and the
            # sidecar is seconds behind it.  Ingesting now would fix an approximate time on
            # a call whose exact one is about to be written, so it waits for the next scan.
            log.debug("sidecar may still be coming, deferring: %s", path)
            summary.files_skipped += 1
        else:
            pending.append(path)

    def work(paths: list[Path]) -> list[IngestResult]:
        session = session_factory()
        out = []
        try:
            for path in paths:
                out.append(
                    ingest_file(
                        session, path, settings=settings, run_id=run_id, audio_root=audio_root
                    )
                )
        finally:
            session.close()
        return out

    workers = max(1, workers)
    if pending:
        # Contiguous slices, not round-robin: consecutive calls on one talkgroup stay on
        # one worker, so the conversation grouping rarely has to wait on the advisory lock.
        chunk = (len(pending) + workers - 1) // workers
        batches = [pending[i : i + chunk] for i in range(0, len(pending), chunk)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for results in pool.map(work, batches):
                for result in results:
                    if result.status == "ingested":
                        summary.files_new += 1
                    elif result.status == "duplicate":
                        summary.files_duplicate += 1
                    elif result.status == "skipped":
                        summary.files_skipped += 1
                    else:
                        summary.files_failed += 1
                        if len(summary.errors) < MAX_RUN_ERRORS:
                            summary.errors.append(
                                {"path": result.file_path, "error": result.error or ""}
                            )

    summary.elapsed_s = time.monotonic() - started

    closer = session_factory()
    try:
        closer.execute(
            update(IngestRun)
            .where(IngestRun.id == run_id)
            .values(
                finished_at=datetime.now(UTC),
                files_seen=summary.files_seen,
                files_new=summary.files_new,
                files_duplicate=summary.files_duplicate,
                files_skipped=summary.files_skipped,
                files_failed=summary.files_failed,
                errors=summary.errors or None,
            )
        )
        closer.commit()
    finally:
        closer.close()

    return summary


def retry_failed(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    limit: int = 1000,
) -> IngestRunSummary:
    """Re-ingest everything the ledger marks ``failed``."""
    settings = settings or get_settings()
    root = Path(settings.audio_root)
    session = session_factory()
    try:
        paths = (
            session.execute(
                select(IngestFile.file_path).where(IngestFile.status == "failed").limit(limit)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()

    summary = IngestRunSummary(run_id=None, root=str(root), files_seen=len(paths))
    started = time.monotonic()
    session = session_factory()
    try:
        for rel in paths:
            full = root / rel
            if not full.exists():
                record_ledger(session, rel, "ignored", error="file no longer exists")
                session.commit()
                summary.files_skipped += 1
                continue
            result = ingest_file(session, full, settings=settings, audio_root=root)
            if result.status == "ingested":
                summary.files_new += 1
            elif result.status == "duplicate":
                summary.files_duplicate += 1
            elif result.status == "skipped":
                summary.files_skipped += 1
            else:
                summary.files_failed += 1
                if len(summary.errors) < MAX_RUN_ERRORS:
                    summary.errors.append({"path": rel, "error": result.error or ""})
    finally:
        session.close()
    summary.elapsed_s = time.monotonic() - started
    return summary


def await_trunk_recorder_call(
    path: str | Path,
    *,
    prefer_audio: str = "m4a",
    wait_seconds: float = 30.0,
    poll_seconds: float = 0.25,
) -> Path:
    """Resolve one filesystem event on a trunk-recorder call to the path that *is* the call.

    trunk-recorder writes the audio first and the ``.json`` beside it a moment later, and
    may write both a WAV and an M4A of the same call.  So a single call can fire three
    events, and ingesting the first one to settle would mean a call recorded without its
    sidecar (and possibly a second call for the second copy of the same audio).

    This waits — up to ``TR_SIDECAR_WAIT_SECONDS`` — for the sidecar, and only then picks
    the file the call is stored as: ``TR_PREFER_AUDIO`` when there is audio, the sidecar
    itself when there is not (the encrypted case, §1.7).  Whichever of the three files
    triggered it, every one of them resolves to the same path, so the ledger's idempotence
    on ``file_path`` collapses the duplicate events into one ingest.

    When the sidecar never arrives the audio is ingested anyway, from its filename and its
    header, with ``time_source='file_write'`` — an approximate call beats no call.
    """
    path = Path(path)
    sidecar = sidecar_for(path)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while not sidecar.is_file():
        if time.monotonic() >= deadline:
            log.info("no sidecar after %.0fs, ingesting %s on its own", wait_seconds, path.name)
            break
        time.sleep(poll_seconds)

    audio = audio_for_sidecar(sidecar, prefer=prefer_audio)
    if audio is not None:
        return audio
    return sidecar if sidecar.is_file() else path


def wait_until_settled(path: str | Path, *, settle_seconds: float, timeout: float = 60.0) -> bool:
    """True once the file's size has not changed for ``settle_seconds``.

    sdrtrunk names a recording when the call starts and closes it when the call ends, so a
    file seen by inotify is very often still being written (5.1 step 1).
    """
    path = Path(path)
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_since = 0.0
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        now = time.monotonic()
        if size != last_size or size == 0:
            last_size = size
            stable_since = now
        elif now - stable_since >= settle_seconds:
            return True
        time.sleep(min(0.25, settle_seconds))
    return False


#: Cache key and TTL for the whole :func:`ingest_status` dict (app/services/counts.py).
INGEST_STATUS_KEY = "ingest.status"
INGEST_STATUS_TTL_S = 60.0


def ingest_status(session: Session, *, force: bool = False) -> dict[str, object]:
    """Counts for ``keyup ingest status``, ``GET /admin/ingest`` and ``GET /api/status``.

    **Cached for :data:`INGEST_STATUS_TTL_S` seconds, process-locally.**  Every number in
    here is a full scan: a ``GROUP BY status`` over three million ``ingest_files`` rows, the
    same over 1.6 million ``transcription_jobs``, ``count(*)`` over three million ``calls``
    and again for the duplicates among them.  ``/api/status`` is polled every ten seconds by
    the container health check and by every open tab, and the live feed sends this dict as a
    heartbeat frame to every websocket subscriber — uncached, that is a scan of the whole
    archive several times a minute for numbers that move by single digits.

    One cache entry covers the whole dict, so the figures in it are always consistent with
    each other.  ``force=True`` recomputes: the CLI (``keyup ingest status``) uses it,
    because a person who typed the command asked for the number *now*.  The Admin → Ingest
    page and the status endpoint accept the staleness; the endpoint reports it in
    ``ingest.as_of`` / ``ingest.cached_s``.
    """
    return count_service.cached(
        INGEST_STATUS_KEY, INGEST_STATUS_TTL_S, lambda: _ingest_status(session), force=force
    )


def _ingest_status(session: Session) -> dict[str, object]:
    """The uncached measurement behind :func:`ingest_status`."""
    ledger = dict(
        session.execute(
            select(IngestFile.status, func.count()).group_by(IngestFile.status)
        ).all()
    )
    jobs = dict(
        session.execute(
            select(TranscriptionJob.status, func.count()).group_by(TranscriptionJob.status)
        ).all()
    )
    last_run = session.execute(
        select(IngestRun).order_by(IngestRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        # Estimated on a large archive, exact on a small one (app/services/counts.py); the
        # duplicates are a partial index scan on ``ix_calls_duplicate_of``, not a heap scan.
        "calls": count_service.estimated_rows(session, Call),
        "duplicates": session.execute(
            select(func.count()).select_from(Call).where(Call.duplicate_of.isnot(None))
        ).scalar_one(),
        "talkgroups": session.execute(select(func.count()).select_from(Talkgroup)).scalar_one(),
        "radios": session.execute(select(func.count()).select_from(Radio)).scalar_one(),
        "patches": session.execute(select(func.count()).select_from(Patch)).scalar_one(),
        "conversations": session.execute(select(func.count()).select_from(Conversation)).scalar_one(),
        "ledger": ledger,
        "jobs": jobs,
        "last_run": None
        if last_run is None
        else {
            "id": last_run.id,
            "kind": last_run.kind,
            "started_at": last_run.started_at,
            "finished_at": last_run.finished_at,
            "files_seen": last_run.files_seen,
            "files_new": last_run.files_new,
            "files_duplicate": last_run.files_duplicate,
            "files_skipped": last_run.files_skipped,
            "files_failed": last_run.files_failed,
        },
    }


__all__ = [
    "DEFAULT_SKIP_DIRS",
    "IngestResult",
    "IngestRunSummary",
    "UnknownSystem",
    "WalkCost",
    "scan_skip_dirs",
    "walk_recordings",
    "assign_conversation",
    "enqueue_audio_process",
    "enqueue_transcription",
    "find_duplicate",
    "lock_dedupe",
    "ingest_file",
    "INGEST_STATUS_KEY",
    "ingest_status",
    "ingest_tree",
    "iter_recordings",
    "resolve_system_id",
    "retry_failed",
    "wait_until_settled",
]
