"""Which calls an exporter is allowed to touch — the safety half of :mod:`app.exporters`.

This module exists because of one specific hazard.  A 400 000-call sdrtrunk archive is
being migrated into this deployment through ``POST /api/call-upload`` at ``priority=backlog``
while the live recorder keeps pushing at ``priority=live``.  Both land in ``calls``.  An
exporter that simply walked ``calls`` newest-first would happily re-publish a decade of
2016 traffic to Broadcastify as though it were happening now.

So nothing is exported unless it passes **both** of these, independently:

:func:`is_live_band`
    the call arrived in the *live* admission band.  ``calls.ingest_priority`` is the
    admission class the push path recorded (``app/ingest/flow.py``): ``live``/``urgent``
    pass, ``backlog``/``bulk`` never do, a bare integer is judged against
    :data:`~app.ingest.flow.BACKLOG_BAND`, and **NULL fails**.  NULL means the row came
    from the file watcher or from a client that sent no priority at all — which is exactly
    what a bulk import looks like — and "we cannot prove this is live" has to mean "do not
    publish it".  trunkd's uploader sends ``priority=live`` on every call, so live traffic
    is never caught by this.

:func:`after_watermark`
    the call started *after* the moment the service was switched on.  The watermark is
    written the first time the exporter worker sees the runtime switch turned on and
    deleted when it is turned off, so enabling an exporter publishes the traffic from here
    forward and never a backfill of whatever happens to be in the archive.

On top of those two sits the operator's *selection* — which talkgroups, which systems,
which recordings (:func:`excluded_reason`).  That half is configuration rather than safety:
it decides what an operator wants published, and every one of its knobs is a runtime
setting.  It can only ever narrow what the two guards already allowed.

The two guards overlap on purpose, because each covers the other's blind spot.  The band
check alone would republish every live call already sitting in the archive the instant a
switch was flipped.  The watermark alone would pass a *backlog* row whose ``started_at``
happens to be current — a migrated recording with a bad timestamp, or a bulk push of
traffic recorded this morning — because the watermark only knows when the call started, not
where it came from.  Neither is trusted to be sufficient, and :func:`exportable` requires
both.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, cast, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session
from sqlalchemy.types import Integer

from app.ingest.flow import BACKLOG_BAND
from app.models import (
    CONVENTIONAL_PROTOCOL_REGEX,
    TRUNKED_PROTOCOL_REGEX,
    Call,
    RadioAffiliation,
    Setting,
    SiteFrequency,
    System,
    Talkgroup,
)

log = logging.getLogger("app.exporters.gate")

__all__ = [
    "FLAG_COLUMNS",
    "LIVE_CLASSES",
    "NOT_TRUNKED",
    "NO_UNIT_CALL_SERVICES",
    "SELECTION_MODES",
    "SETTINGS_SERVICE",
    "TRUNKED_ONLY_SERVICES",
    "UNIT_CALL",
    "settings_service",
    "trunked_clause",
    "trunked_ok",
    "trunked_systems",
    "after_watermark",
    "content_ok",
    "content_clause",
    "excluded_reason",
    "export_flag_column",
    "selected",
    "selection_clause",
    "selection_mode",
    "systems_clause",
    "systems_for",
    "system_ok",
    "clear_watermark",
    "exportable",
    "is_live_band",
    "is_unit_call",
    "live_band_clause",
    "set_watermark",
    "sync_watermark",
    "unit_calls_clause",
    "unit_calls_ok",
    "export_normalize",
    "watermark",
    "watermark_key",
]

#: The admission classes that count as live traffic.  ``backlog`` and ``bulk`` are the
#: migration client's; anything else has to be an integer below the backlog band.
LIVE_CLASSES = frozenset({"live", "urgent"})


def is_live_band(call: Any) -> bool:
    """Did this call arrive in the live admission band?  ``None`` → ``False``, always.

    Mirrors :class:`app.ingest.flow.PriorityChoice`: the class names ``live`` and ``urgent``
    resolve below :data:`~app.ingest.flow.BACKLOG_BAND` for every talkgroup, ``backlog`` and
    ``bulk`` never do, and a bare integer is judged on its value.  An unrecognized string
    fails closed.
    """
    raw = getattr(call, "ingest_priority", None)
    if raw is None:
        return False
    text = str(raw).strip().lower()
    if not text:
        return False
    if text in LIVE_CLASSES:
        return True
    try:
        return int(text) < BACKLOG_BAND
    except ValueError:
        return False


def live_band_clause() -> ColumnElement[bool]:
    """The SQL half of :func:`is_live_band`, for the discovery query.

    ``ingest_priority`` is free text, so the integer branch is guarded by a regex test
    before the cast — a bad value must make the row *fail* the filter, not the query.
    """
    numeric = Call.ingest_priority.op("~")("^[0-9]+$")
    return and_(
        Call.ingest_priority.is_not(None),
        or_(
            Call.ingest_priority.in_(sorted(LIVE_CLASSES)),
            and_(numeric, cast(Call.ingest_priority, Integer) < BACKLOG_BAND),
        ),
    )


# --------------------------------------------------------------------------------------
# which talkgroups, which systems, which recordings
# --------------------------------------------------------------------------------------
#
# THE SELECTION RULE, in one sentence per mode.  ``export_<service>_talkgroup_mode``:
#
# ``all``    everything.  The per-talkgroup checkbox is not looked at.
# ``allow``  only the talkgroups whose checkbox is **ticked** are published.
# ``deny``   everything except the talkgroups whose checkbox is **cleared**.
#
# ``allow`` and ``deny`` read the *same* column (``talkgroups.export_<service>``, default
# true); they differ only in what an untouched, default-true row means.  With every row at
# its default the two behave identically — publish everything — so the mode is a statement
# about how the operator intends to curate the list: ``allow`` starts from "nothing but
# what I tick" (uncheck the box on the ones you do not want *after* ticking the ones you
# do), ``deny`` from "everything but what I untick".
#
# A call with no talkgroup is conventional traffic keyed by the frequency it was heard on,
# and it is judged by ``site_frequencies.export_<service>`` under exactly the same rule.

#: Which service's ``export_<service>_talkgroup_mode`` / ``export_<service>_systems``
#: settings a service reads.  Only the live **Feed** is not its own: it shares the
#: Broadcastify Calls *mode and system list* (one Broadcastify account, one selection),
#: while keeping its own per-talkgroup checkbox — see :data:`FLAG_COLUMNS`.
SETTINGS_SERVICE = {"broadcastify_feed": "broadcastify"}

#: Which column each service's per-talkgroup / per-frequency checkbox lives in, in
#: preference order — the first one the model actually has, wins.
#:
#: Broadcastify Calls has a column of its own (``export_bcfy_calls``) because the two
#: Broadcastify products are chosen separately: Calls is a permanent, searchable archive on
#: somebody else's site and the Feed is audio that scrolls past, and only Calls is refused
#: for a non-trunked system.  The Feed kept ``export_broadcastify``, the column the two used
#: to share, so an existing selection keeps meaning what it meant.  ``site_frequencies`` has
#: no Calls column and falls through to ``export_broadcastify``: a conventional call keyed
#: by frequency is never published to Calls anyway, because its system is not trunked.
FLAG_COLUMNS = {
    "broadcastify": ("export_bcfy_calls", "export_broadcastify"),
    "broadcastify_feed": ("export_broadcastify",),
    "openmhz": ("export_openmhz",),
}

#: The services that publish **trunked systems only**.  Broadcastify Calls is one: its
#: archive is organized by trunked system and a conventional one has no place in it, so a
#: call from one is refused here whatever the operator selected.  The Feed (audio, with no
#: system identity attached) and OpenMHz are not gated on this.
TRUNKED_ONLY_SERVICES = frozenset({"broadcastify"})

#: The ledger's word for that refusal.  Distinct from every other skip reason so that
#: "why is nothing uploading" has one obvious answer in the ledger.
NOT_TRUNKED = "not_trunked"

#: The services that never publish a **unit-to-unit (private) call**.  Both archives are
#: keyed on a talkgroup — Broadcastify Calls on ``tg``, OpenMHz on ``talkgroup_num`` — and a
#: private call has none, so there is no honest value to send.  Sending it as a
#: *conventional* slot (the other way a call reaches those services without a talkgroup,
#: keyed by its frequency) would be worse than dropping it: the frequency is a traffic
#: channel the site hands to whatever it granted next, so one radio's private conversation
#: would be filed under a channel that means nothing and belongs to nobody.
#:
#: The live **Feed** is not on this list.  It publishes audio with no identity attached, so
#: there is nothing to file wrongly, and whether private traffic belongs on a feed the
#: public listens to is a judgement rather than a fact — hence a setting
#: (``broadcastify_feed_include_unit_calls``, on) rather than a rule.
NO_UNIT_CALL_SERVICES = frozenset({"broadcastify", "openmhz"})

#: The ledger's word for that one.  Its own token, like :data:`NOT_TRUNKED`, so that a
#: reader asking "where did the private calls go" greps the ledger once and is done.
UNIT_CALL = "unit_call"


def is_unit_call(call: Any) -> bool:
    """Is this a unit-to-unit (private) call?  Anything that does not say, is not."""
    return (getattr(call, "kind", None) or "group") == "unit"


def export_normalize(settings: Any) -> bool:
    """Whether an unprocessed original is run through the transcription normaliser on export.

    ``stt_normalize`` used to decide this too, so turning the exports back to the recording
    as made also took the engine's evened-out copy away.  Two switches now: the engine's
    stays where it was, and this one is the exporters'.
    """
    return bool(getattr(settings, "export_normalize", True))


def unit_calls_ok(call: Any, settings: Any, service: str) -> bool:
    """May *this service* publish this call, as far as its being private goes?"""
    if not is_unit_call(call):
        return True
    if service in NO_UNIT_CALL_SERVICES:
        return False
    return bool(getattr(settings, "broadcastify_feed_include_unit_calls", True))


def unit_calls_clause(settings: Any, service: str) -> ColumnElement[bool] | None:
    """The SQL half of :func:`unit_calls_ok`, or ``None`` when nothing is excluded.

    Filtered out of discovery rather than written as a ``skipped`` ledger row per call, for
    the same reason the operator's talkgroup selection is: these are calls the service was
    never asked about.  A call that reaches the ledger anyway — a re-file, a claimed row —
    still gets :data:`UNIT_CALL` from :func:`excluded_reason`.
    """
    if service in NO_UNIT_CALL_SERVICES or not bool(
        getattr(settings, "broadcastify_feed_include_unit_calls", True)
    ):
        return Call.kind != "unit"
    return None


#: The three values ``export_<service>_talkgroup_mode`` accepts.  Anything else is read as
#: ``all`` — an unreadable mode must not silently stop publishing.
SELECTION_MODES = ("all", "allow", "deny")


def settings_service(service: str) -> str:
    """Whose ``export_<service>_*`` settings this service reads (:data:`SETTINGS_SERVICE`)."""
    return SETTINGS_SERVICE.get(service, service)


def selection_mode(settings: Any, service: str) -> str:
    """The configured mode for one service, normalized; unknown values read as ``all``."""
    key = f"export_{settings_service(service)}_talkgroup_mode"
    raw = str(getattr(settings, key, "all") or "all").strip().lower()
    return raw if raw in SELECTION_MODES else "all"


def export_flag_column(model: Any, service: str):  # noqa: ANN201 - a mapped Column
    """This service's checkbox column on ``model``, or ``None`` when it has none.

    :data:`FLAG_COLUMNS` names the candidates in preference order, so a model that lacks
    the service's own column (``site_frequencies`` has no ``export_bcfy_calls``) falls
    through to the shared one rather than silently selecting nothing.
    """
    for name in FLAG_COLUMNS.get(service, (f"export_{service}",)):
        column = getattr(model, name, None)
        if column is not None:
            return column
    return None


def systems_for(settings: Any, service: str) -> set[int]:
    """The keyup ``systems.id`` values one service may publish; empty means every system."""
    raw = str(getattr(settings, f"export_{settings_service(service)}_systems", "") or "")
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("export: %s system selection %r is not a system id; ignored", service, part)
    return out


def system_ok(call: Any, settings: Any, service: str) -> bool:
    """Is this call's system in the service's system selection?  Empty selection → yes."""
    wanted = systems_for(settings, service)
    if not wanted:
        return True
    return int(getattr(call, "system_id", 0) or 0) in wanted


def systems_clause(settings: Any, service: str) -> ColumnElement[bool] | None:
    """The SQL half of :func:`system_ok`, or ``None`` when every system is allowed."""
    wanted = systems_for(settings, service)
    if not wanted:
        return None
    return Call.system_id.in_(sorted(wanted))


# --------------------------------------------------------------------------------------
# trunked systems only (Broadcastify Calls)
# --------------------------------------------------------------------------------------
#
# Broadcastify Calls is an archive of *trunked* systems: a call is filed under a system id
# issued for one, and their own guidance is that conventional traffic belongs on a feed
# rather than in Calls.  Keyup archives both — a county's trunked network and the handful
# of conventional fire channels beside it — so "which system is this call from" is a real
# question here, and publishing the wrong answer is not something an operator should have
# to notice.  Hence a rule rather than a setting: ``TRUNKED_ONLY_SERVICES`` cannot be
# widened from the Admin page, and every layer below refuses the same call.


def system_trunked(session: Session | None, system_id: Any) -> bool:
    """Is that ``systems.id`` a trunked system?  Unknown or missing → ``False``.

    Fails closed on purpose: an id that names no row is not a system we can say anything
    about, and Calls is the one service where "we are not sure" has to mean "no".
    """
    if session is None or system_id is None:
        return False
    row = session.get(System, int(system_id))
    return bool(row is not None and row.is_trunked)


def trunked_ok(session: Session | None, call: Any, service: str) -> bool:
    """Does this call's system satisfy the service's trunked rule?

    Every service but Broadcastify Calls answers ``True`` unconditionally.  Without a
    session there is nothing to look up, and the answer is deferred to the SQL half rather
    than guessed — the worker always has one, so this only affects direct callers.
    """
    if service not in TRUNKED_ONLY_SERVICES:
        return True
    if session is None:  # pragma: no cover - defensive; the worker always has one
        return True
    return system_trunked(session, getattr(call, "system_id", None))


def trunked_system_clause():  # noqa: ANN201 - a SQL boolean over ``systems``
    """``systems.trunked`` when it is set, the protocol guess when it is not, as SQL.

    The Python half is :meth:`app.models.System.is_trunked`; the two are kept in step by
    ``tests/test_exporters.py``, which asserts the same systems come back either way.
    """
    return or_(
        System.trunked.is_(True),
        and_(
            System.trunked.is_(None),
            System.protocol.is_not(None),
            System.protocol.op("~*", is_comparison=True)(TRUNKED_PROTOCOL_REGEX),
            ~System.protocol.op("~*", is_comparison=True)(CONVENTIONAL_PROTOCOL_REGEX),
        ),
    )


def trunked_clause(service: str) -> ColumnElement[bool] | None:
    """The discovery filter for :func:`trunked_ok`, or ``None`` for an ungated service.

    Filtering rather than writing a ``skipped`` row per call, for the same reason the
    talkgroup selection is filtered: a conventional system was never a candidate, and a
    ledger full of rows saying so would bury the calls we did decide against.  The
    *claimed* row still gets :data:`NOT_TRUNKED` from :func:`excluded_reason` — that path
    catches the rows discovered before a system's protocol was corrected.
    """
    if service not in TRUNKED_ONLY_SERVICES:
        return None
    return select(System.id).where(System.id == Call.system_id, trunked_system_clause()).exists()


def trunked_systems(session: Session) -> list[System]:
    """Every trunked system, by name — what the Admin page offers Calls a choice of."""
    rows = session.scalars(select(System).order_by(System.name)).all()
    return [row for row in rows if row.is_trunked]


def content_ok(call: Any, settings: Any) -> bool:
    """Is the *recording itself* worth publishing — long enough, and not encrypted?"""
    if not bool(getattr(settings, "export_include_encrypted", False)) and (
        getattr(call, "encrypted", None) is True
    ):
        return False
    floor = float(getattr(settings, "export_min_duration_s", 0.0) or 0.0)
    if floor > 0:
        duration = getattr(call, "duration_s", None)
        if duration is None or float(duration) < floor:
            return False
    return True


def content_clause(settings: Any) -> list[ColumnElement[bool]]:
    """The duration half of :func:`content_ok`, as clauses for the discovery query.

    Encryption is deliberately **not** here.  An encrypted call gets a ``skipped`` ledger
    row saying so (:func:`app.exporters.ledger.skip_reason_for`), because "we saw it and
    decided against it" is worth recording and worth not re-examining every cycle.  A call
    below the duration floor is filtered instead: the floor is a threshold an operator
    moves, and a row written under the old one would outlive the change.
    """
    out: list[ColumnElement[bool]] = []
    floor = float(getattr(settings, "export_min_duration_s", 0.0) or 0.0)
    if floor > 0:
        out.append(Call.duration_s >= floor)
    return out


def _unit_call_selected(session: Session, call: Any, service: str) -> bool:
    """A private call follows the selection of the talkgroup its radios belong to.

    It has no talkgroup of its own, so the checkbox an operator un-ticked for a channel
    used to have no purchase on it: with every transit talkgroup deselected for the
    Broadcastify feed, a transit base's private calls still went out over it.  The radios
    do tell us where they live — their most recent group affiliation — so the source radio
    and, when it is in the directory, the target radio are each resolved to that talkgroup
    and its flag for this service is what decides.  Either side deselected keeps the call
    off the service; a radio with no affiliation on record says nothing, and a call that
    resolves nowhere falls back to the plain include-unit-calls switch, which is where
    :func:`unit_calls_ok` already left it.
    """
    column = export_flag_column(Talkgroup, service)
    if column is None:  # a service with no talkgroup checkbox has nothing to deselect
        return True
    radio_pks: set[int] = set()
    source_pk = getattr(call, "radio_id", None)
    if source_pk is not None:
        radio_pks.add(int(source_pk))
    # ``target_radio_id`` is the directory row (``radios.id``), like ``radio_id``.
    target_pk = getattr(call, "target_radio_id", None)
    if target_pk is not None:
        radio_pks.add(int(target_pk))
    if not radio_pks:
        return True
    # One statement for both radios (review O21): each radio's latest affiliation —
    # ``DISTINCT ON`` over the affiliations alone, so a radio's newest row is the one that
    # counts even if its talkgroup were gone — then that talkgroup's flag.  A radio with no
    # affiliation contributes no row, which is the "says nothing" case.
    latest = (
        select(RadioAffiliation.radio_id_fk, RadioAffiliation.talkgroup_id_fk)
        .where(RadioAffiliation.radio_id_fk.in_(sorted(radio_pks)))
        .ext(distinct_on(RadioAffiliation.radio_id_fk))
        .order_by(RadioAffiliation.radio_id_fk, RadioAffiliation.last_heard_at.desc().nulls_last())
        .subquery()
    )
    flags = session.scalars(
        select(column).select_from(latest).join(Talkgroup, Talkgroup.id == latest.c.talkgroup_id_fk)
    ).all()
    return all(bool(flag) for flag in flags)


def selected(session: Session | None, call: Any, settings: Any, service: str) -> bool:
    """Does the talkgroup (or, conventional, the frequency) pass the selection rule?

    ``all`` never looks at a flag, so it needs no session and no query.  ``allow`` and
    ``deny`` both come down to "is the flag true", and both fail closed on a row we cannot
    read: a talkgroup that is gone, or a frequency this deployment has never catalogued,
    is not something to publish on the strength of a default.
    """
    mode = selection_mode(settings, service)
    if mode == "all":
        return True
    if session is None:  # pragma: no cover - defensive; the worker always has one
        return True
    # A private call is on no talkgroup and is not conventional traffic either, so neither
    # checkbox has anything to say about it: its switch is
    # ``broadcastify_feed_include_unit_calls``, checked by :func:`unit_calls_ok`.  Falling
    # through to the frequency branch below would judge it by whoever else was granted that
    # traffic channel, which is not a selection anybody made.
    if is_unit_call(call):
        return _unit_call_selected(session, call, service)
    talkgroup_id = getattr(call, "talkgroup_id", None)
    if talkgroup_id is not None:
        row = session.get(Talkgroup, talkgroup_id)
        if row is None:
            return False
        column = export_flag_column(Talkgroup, service)
        name = column.key if column is not None else f"export_{service}"
        return bool(getattr(row, name, True))
    # Conventional: no talkgroup, so the channel *is* the frequency.  Any catalogued row
    # for that frequency saying "no" is enough to stop it.
    frequency = getattr(call, "frequency_hz", None)
    if frequency is None:
        return False
    column = export_flag_column(SiteFrequency, service)
    if column is None:  # pragma: no cover - defensive
        return True
    flags = session.scalars(
        select(column).where(SiteFrequency.frequency_hz == int(frequency))
    ).all()
    if not flags:
        return False
    return all(bool(flag) for flag in flags)


def selection_clause(settings: Any, service: str) -> ColumnElement[bool] | None:
    """The SQL half of :func:`selected`, or ``None`` in ``all`` mode (no filtering).

    Written as EXISTS subqueries rather than joins so the discovery query keeps its shape:
    ``calls`` is the driving table and this is one more predicate on it.
    """
    if selection_mode(settings, service) == "all":
        return None
    tg_flag = export_flag_column(Talkgroup, service)
    freq_flag = export_flag_column(SiteFrequency, service)
    if tg_flag is None or freq_flag is None:  # pragma: no cover - defensive
        return None
    on_talkgroup = and_(
        Call.talkgroup_id.is_not(None),
        select(Talkgroup.id)
        .where(Talkgroup.id == Call.talkgroup_id, tg_flag.is_(True))
        .exists(),
    )
    unit = Call.kind == "unit"
    conventional = and_(
        Call.kind != "unit",
        Call.talkgroup_id.is_(None),
        Call.frequency_hz.is_not(None),
        select(SiteFrequency.id)
        .where(SiteFrequency.frequency_hz == Call.frequency_hz)
        .exists(),
        ~select(SiteFrequency.id)
        .where(SiteFrequency.frequency_hz == Call.frequency_hz, freq_flag.is_(False))
        .exists(),
    )
    return or_(on_talkgroup, conventional, unit)


def excluded_reason(
    session: Session | None, call: Any, settings: Any, service: str
) -> str | None:
    """Why this call is not for *this service*, or ``None`` when it may go.

    Separate from :func:`app.exporters.ledger.skip_reason_for`, which answers the same
    question about the recording rather than about the configuration, and separate from
    :func:`exportable`, which is the safety gate and is never softened by a setting.
    """
    if not unit_calls_ok(call, settings, service):
        # Before the content and selection rules, and worded as one token for the same
        # reason :data:`NOT_TRUNKED` is: for two of the three services this is not something
        # an operator can turn on from the Exports page, and it must be greppable.
        return UNIT_CALL
    if not trunked_ok(session, call, service):
        # First, and worded as one token rather than a sentence: this is the one exclusion
        # an operator cannot undo from the Exports page, so it is the one worth being able
        # to grep the ledger for.
        return NOT_TRUNKED
    if not content_ok(call, settings):
        if getattr(call, "encrypted", None) is True:
            return "encrypted"
        return "shorter than the minimum export duration"
    if not system_ok(call, settings, service):
        return "system not selected for export"
    if not selected(session, call, settings, service):
        return "talkgroup not selected for export"
    return None


# --------------------------------------------------------------------------------------
# the enable watermark
# --------------------------------------------------------------------------------------


def watermark_key(service: str) -> str:
    """The ``settings`` row that holds one service's enable moment.

    Deliberately *not* in :data:`app.runtime_settings.SETTINGS_SPEC`: it is bookkeeping the
    worker owns, not a knob an operator should be able to edit in the Admin page, and
    ``effective_settings`` drops any key the spec does not list, so it never reaches
    :class:`~app.config.Settings`.
    """
    return f"export_{service}_enabled_at"


def watermark(session: Session, service: str) -> datetime | None:
    """When this service was switched on, or ``None`` when it is off / never was."""
    row = session.get(Setting, watermark_key(service))
    if row is None or row.value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(row.value))
    except ValueError:
        log.warning("export: %s has an unreadable enable watermark %r", service, row.value)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def set_watermark(session: Session, service: str, when: datetime | None = None) -> datetime:
    """Stamp the enable moment (idempotent: an existing watermark is never moved forward).

    Never advancing an existing watermark matters on restart — the worker re-observes
    "enabled" every cycle, and a watermark that moved each time would be a rolling window
    rather than a fixed floor.
    """
    existing = watermark(session, service)
    if existing is not None:
        return existing
    moment = (when or datetime.now(UTC)).astimezone(UTC)
    key = watermark_key(service)
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, description=f"When the {service} exporter was enabled.")
        session.add(row)
    row.value = moment.isoformat()
    session.flush()
    return moment


def clear_watermark(session: Session, service: str) -> None:
    """Forget the enable moment — turning a service off restarts its clock."""
    row = session.get(Setting, watermark_key(service))
    if row is not None:
        session.delete(row)
        session.flush()


def sync_watermark(session: Session, service: str, *, enabled: bool) -> datetime | None:
    """Reconcile the watermark with the switch, and return the watermark now in force.

    Called at the top of every worker cycle: turning a service on for the first time stamps
    *now*, turning it off drops the stamp, and leaving it on is a no-op.  ``None`` back
    means "this service exports nothing", which is the state both services ship in.
    """
    if not enabled:
        clear_watermark(session, service)
        return None
    return set_watermark(session, service)


def after_watermark(call: Any, mark: datetime | None) -> bool:
    """Did this call start after the service was enabled?  No watermark → ``False``."""
    if mark is None:
        return False
    started = getattr(call, "started_at", None)
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started > mark


def exportable(
    call: Any,
    mark: datetime | None,
    *,
    settings: Any | None = None,
    service: str | None = None,
    session: Session | None = None,
) -> bool:
    """**Both** safety guards, plus the operator's selection when one is supplied.

    The two guards are unconditional and no setting can turn either off.  ``settings`` and
    ``service`` add the *configured* half — the talkgroup/frequency selection, the system
    list, the encrypted and minimum-duration rules — which is a matter of taste rather than
    safety, so callers that only want the safety answer (the tests, anything auditing the
    band) may leave them out.
    """
    if not (is_live_band(call) and after_watermark(call, mark)):
        return False
    if settings is None or service is None:
        return True
    return excluded_reason(session, call, settings, service) is None
