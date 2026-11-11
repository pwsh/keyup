"""The roster: units, personnel, and which radio belongs to whom right now (§3.1).

Everything that *changes* an assignment goes through :func:`assign` and :func:`end_current`
rather than through the router, because an assignment change is never one write.  Assigning
a radio to a unit closes the previous unit assignment, opens a new one, and re-derives
``radios.unit_id`` — three statements that have to agree, and that two endpoints (the
radio's own assignment API and the roster importer) both need.

The denormalization rule, in one place so it cannot drift
--------------------------------------------------------
``radios.unit_id`` is the label the whole archive reads: the canonical name of a radio is
``coalesce(radios.unit_id, radios.alias, radios.radio_id, calls.radio_raw)``, computed on
every row of every call list.  Resolving that through ``radio_assignments`` instead would
put two joins on the hottest query in the application, so the column stays and is kept in
step from here:

* **assign a unit** → ``radios.unit_id = unit.callsign``, unconditionally.  The operator
  just said which unit this radio is; that is the best label available.
* **end a unit assignment** → ``radios.unit_id = NULL`` *only if it still equals the ended
  unit's callsign*.  If it does not, somebody typed something else in the meantime and
  meant it, and clearing their edit would be this code overruling a human.
* **person assignments never touch it.**  ``unit_id`` names apparatus, not people; a
  radio handed to J. Smith is still Engine 1's radio.

A direct ``PATCH /radios/{id}`` therefore still works, and the next assignment change wins
— which is what :class:`app.models.Radio`'s docstring promises.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Call, Person, Radio, RadioAssignment, Unit
from app.schemas import AssignedRadioOut, PersonRef, RadioAssignmentOut, UnitRef

__all__ = [
    "assign",
    "assigned_radios",
    "assignment_out",
    "current_identities",
    "end_current",
    "history",
    "roster_call_stats",
]


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------


def current_identities(
    session: Session, radio_pks: list[int] | set[int]
) -> dict[int, tuple[UnitRef | None, PersonRef | None]]:
    """``{radios.id: (unit, person)}`` for the radios asked about, current rows only.

    One query for a whole page rather than one per row: the radio list renders up to 500
    rows and this is the only place it needs the roster, so a per-row lookup would turn a
    single page into 500 round trips.
    """
    keys = list(radio_pks)
    if not keys:
        return {}
    out: dict[int, tuple[UnitRef | None, PersonRef | None]] = {}
    rows = session.execute(
        select(RadioAssignment, Unit, Person)
        .outerjoin(Unit, RadioAssignment.unit_id == Unit.id)
        .outerjoin(Person, RadioAssignment.person_id == Person.id)
        .where(RadioAssignment.radio_pk.in_(keys), RadioAssignment.effective_to.is_(None))
    )
    for assignment, unit, person in rows:
        unit_ref, person_ref = out.get(assignment.radio_pk, (None, None))
        if unit is not None:
            unit_ref = UnitRef(id=unit.id, callsign=unit.callsign, name=unit.name)
        if person is not None:
            person_ref = PersonRef(id=person.id, name=person.name)
        out[assignment.radio_pk] = (unit_ref, person_ref)
    return out


def assignment_out(
    assignment: RadioAssignment, unit: Unit | None, person: Person | None
) -> RadioAssignmentOut:
    return RadioAssignmentOut(
        id=assignment.id,
        radio_pk=assignment.radio_pk,
        kind="unit" if assignment.unit_id is not None else "person",
        unit=UnitRef(id=unit.id, callsign=unit.callsign, name=unit.name) if unit else None,
        person=PersonRef(id=person.id, name=person.name) if person else None,
        effective_from=assignment.effective_from,
        effective_to=assignment.effective_to,
        current=assignment.effective_to is None,
        note=assignment.note,
    )


def history(session: Session, radio_pk: int) -> list[RadioAssignmentOut]:
    """Every assignment this radio has had, current ones first, then newest first.

    Sorting current rows to the top rather than sorting on ``effective_from`` alone: an
    assignment can be back-dated, and "who has this radio now" must not slide down the page
    because somebody recorded a correction for last March.
    """
    rows = session.execute(
        select(RadioAssignment, Unit, Person)
        .outerjoin(Unit, RadioAssignment.unit_id == Unit.id)
        .outerjoin(Person, RadioAssignment.person_id == Person.id)
        .where(RadioAssignment.radio_pk == radio_pk)
        .order_by(
            RadioAssignment.effective_to.is_(None).desc(),
            RadioAssignment.effective_from.desc(),
            RadioAssignment.id.desc(),
        )
    )
    return [assignment_out(a, u, p) for a, u, p in rows]


def assigned_radios(
    session: Session, *, unit_id: int | None = None, person_id: int | None = None
) -> list[AssignedRadioOut]:
    """The radios currently assigned to one unit or one person, busiest first."""
    if (unit_id is None) == (person_id is None):
        raise ValueError("pass exactly one of unit_id or person_id")
    target = (
        RadioAssignment.unit_id == unit_id
        if unit_id is not None
        else RadioAssignment.person_id == person_id
    )
    rows = session.execute(
        select(RadioAssignment, Radio)
        .join(Radio, RadioAssignment.radio_pk == Radio.id)
        .where(target, RadioAssignment.effective_to.is_(None))
        .order_by(Radio.call_count.desc(), Radio.radio_id)
    )
    return [
        AssignedRadioOut(
            radio_pk=radio.id,
            radio_id=radio.radio_id,
            alias=radio.alias,
            unit_id=radio.unit_id,
            kind=radio.kind,
            call_count=radio.call_count,
            last_seen_at=radio.last_seen_at,
            assignment_id=assignment.id,
            effective_from=assignment.effective_from,
            note=assignment.note,
        )
        for assignment, radio in rows
    ]


def roster_call_stats(session: Session, radio_pks: list[int]) -> tuple[int, datetime | None]:
    """Calls and last activity across a set of radios — the detail page's aggregate.

    Computed from the radios' own maintained counters where possible: ``radios.call_count``
    is what ingest keeps up to date, so summing it costs nothing next to counting ``calls``.
    """
    if not radio_pks:
        return 0, None
    total = session.scalar(
        select(func.coalesce(func.sum(Radio.call_count), 0)).where(Radio.id.in_(radio_pks))
    )
    last = session.scalar(
        select(func.max(Call.started_at)).where(Call.radio_id.in_(radio_pks))
    )
    return int(total or 0), last


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def _current(session: Session, radio_pk: int, kind: str) -> RadioAssignment | None:
    column = RadioAssignment.unit_id if kind == "unit" else RadioAssignment.person_id
    return session.scalar(
        select(RadioAssignment)
        .where(
            RadioAssignment.radio_pk == radio_pk,
            RadioAssignment.effective_to.is_(None),
            column.isnot(None),
        )
        .order_by(RadioAssignment.effective_from.desc())
        .limit(1)
    )


def assign(
    session: Session,
    radio: Radio,
    *,
    unit: Unit | None = None,
    person: Person | None = None,
    note: str | None = None,
    at: datetime | None = None,
) -> RadioAssignment:
    """Give ``radio`` to a unit or a person, ending whatever it had of the same kind.

    The previous assignment is *closed*, never deleted: an archive that rewrote history
    every time a radio changed hands could not answer a question about last month.  The
    new row starts at the same instant the old one ends, so the two never overlap and a
    lookup at any instant finds exactly one of them.

    Does not commit — the caller owns the transaction, which is what lets the importer
    apply a whole roster atomically.
    """
    if (unit is None) == (person is None):
        raise ValueError("pass exactly one of unit or person")
    when = at or _now()
    kind = "unit" if unit is not None else "person"

    previous = _current(session, radio.id, kind)
    if previous is not None:
        if kind == "unit" and previous.unit_id == (unit.id if unit else None):
            # Already assigned there: leave the row alone rather than closing it and opening
            # an identical one, which would make the timeline lie about a change.  Its note
            # is history too — it records why *this* assignment was made, and a re-assert
            # (a repeated click, a replayed request) does not get to overwrite it.
            _sync_label(radio, unit=unit)
            return previous
        if kind == "person" and previous.person_id == (person.id if person else None):
            return previous
        previous.effective_to = when

    assignment = RadioAssignment(
        radio_pk=radio.id,
        unit_id=unit.id if unit else None,
        person_id=person.id if person else None,
        effective_from=when,
        note=note,
    )
    session.add(assignment)
    if unit is not None:
        _sync_label(radio, unit=unit)
    session.flush()
    return assignment


def end_current(
    session: Session, radio: Radio, kind: str, *, at: datetime | None = None
) -> RadioAssignment | None:
    """End the radio's current unit or person assignment.  Returns it, or ``None``."""
    assignment = _current(session, radio.id, kind)
    if assignment is None:
        return None
    assignment.effective_to = at or _now()
    if kind == "unit":
        unit = session.get(Unit, assignment.unit_id)
        _clear_label(radio, unit=unit)
    session.flush()
    return assignment


def _sync_label(radio: Radio, *, unit: Unit) -> None:
    """The current unit's callsign becomes the radio's label.  See the module docstring."""
    radio.unit_id = unit.callsign


def _clear_label(radio: Radio, *, unit: Unit | None) -> None:
    """Clear the label only when it is still the one this assignment put there."""
    if unit is not None and radio.unit_id == unit.callsign:
        radio.unit_id = None
