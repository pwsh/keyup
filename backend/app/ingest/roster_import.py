"""Import a roster of units or personnel from a CSV (§3.1).

The roster is a list somebody already keeps somewhere else — an apparatus sheet, a shift
roster, an export from the agency's own system — so the way this arrives is a CSV, and the
way it is applied is the way :mod:`app.ingest.playlist_import` applies a playlist: it
**overwrites**.  Importing a roster is an explicit operator act, and the file is the
operator's own naming; refusing to update a row that already exists would mean the second
import of a corrected sheet did nothing.

Shapes, header row required::

    units:      callsign,name,agency_code,kind,notes
    personnel:  name,rank,badge,agency_code,notes[,active]

Matching, which is what decides *created* from *updated*:

* a unit is ``(agency_code, callsign)`` — the table's own key, so ``E1`` can exist once per
  agency and once with no agency at all;
* a person is ``(agency_code, lower(name))``.  ``personnel`` has no unique constraint,
  because two people really can share a name and this archive has no way to tell them
  apart; matching on the name anyway is what stops re-importing a shift roster from
  creating a second J. Smith every time, and the operator can still create a deliberate
  duplicate through the API.

A row naming an ``agency_code`` that is not in the agency table is **skipped**, not
imported with the agency dropped: the agency is half of a unit's identity, so silently
clearing it would file the row against the wrong unit entirely.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import UNIT_KINDS, Agency, Person, Unit

log = logging.getLogger("app.ingest.roster")

UNIT_CSV_HEADER = ["callsign", "name", "agency_code", "kind", "notes"]
PERSONNEL_CSV_HEADER = ["name", "rank", "badge", "agency_code", "notes", "active"]

#: parsed from the CSV's ``active`` column; anything else is "yes", because a roster that
#: says nothing about a person is a roster of people who are on it
_FALSEY = {"0", "false", "f", "no", "n", "off"}


@dataclass
class UnitEntry:
    callsign: str
    name: str | None = None
    agency_code: str | None = None
    kind: str = "other"
    notes: str | None = None


@dataclass
class PersonEntry:
    name: str
    rank: str | None = None
    badge: str | None = None
    agency_code: str | None = None
    active: bool = True
    notes: str | None = None


@dataclass
class ImportSummary:
    source: str
    rows: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0


def _cell(row: dict[str, str], key: str) -> str | None:
    return (row.get(key) or "").strip() or None


def parse_units_csv(path: str | Path) -> list[UnitEntry]:
    """Read ``callsign,name,agency_code,kind,notes``.  A row with no callsign is nothing."""
    entries: list[UnitEntry] = []
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            callsign = _cell(row, "callsign")
            if not callsign:
                continue
            kind = (_cell(row, "kind") or "other").lower()
            entries.append(
                UnitEntry(
                    callsign=callsign,
                    name=_cell(row, "name"),
                    agency_code=_cell(row, "agency_code"),
                    # An unknown kind is normalized rather than rejected: the vocabulary is
                    # ours, not the source system's, and "other" is the honest default.
                    kind=kind if kind in UNIT_KINDS else "other",
                    notes=_cell(row, "notes"),
                )
            )
    return entries


def parse_personnel_csv(path: str | Path) -> list[PersonEntry]:
    """Read ``name,rank,badge,agency_code,notes`` plus an optional ``active``."""
    entries: list[PersonEntry] = []
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            name = _cell(row, "name")
            if not name:
                continue
            active = _cell(row, "active")
            entries.append(
                PersonEntry(
                    name=name,
                    rank=_cell(row, "rank"),
                    badge=_cell(row, "badge"),
                    agency_code=_cell(row, "agency_code"),
                    active=active is None or active.lower() not in _FALSEY,
                    notes=_cell(row, "notes"),
                )
            )
    return entries


def _known_agencies(session: Session) -> set[str]:
    return set(session.scalars(select(Agency.code)))


def import_units(session: Session, entries: list[UnitEntry], *, source: str = "csv") -> ImportSummary:
    """Apply unit entries, overwriting the columns the file carries."""
    summary = ImportSummary(source=source, rows=len(entries))
    agencies = _known_agencies(session)

    for entry in entries:
        if entry.agency_code and entry.agency_code not in agencies:
            log.info("roster: skipping unit %s — unknown agency %s", entry.callsign, entry.agency_code)
            summary.skipped += 1
            continue
        existing = session.scalar(
            select(Unit).where(
                Unit.callsign == entry.callsign,
                Unit.agency_code == entry.agency_code
                if entry.agency_code
                else Unit.agency_code.is_(None),
            )
        )
        if existing is None:
            session.add(
                Unit(
                    callsign=entry.callsign,
                    name=entry.name,
                    agency_code=entry.agency_code,
                    kind=entry.kind,
                    notes=entry.notes,
                )
            )
            summary.created += 1
            continue
        existing.name = entry.name
        existing.kind = entry.kind
        existing.notes = entry.notes
        summary.updated += 1

    session.flush()
    return summary


def import_personnel(
    session: Session, entries: list[PersonEntry], *, source: str = "csv"
) -> ImportSummary:
    """Apply personnel entries, matched on ``(agency_code, lower(name))``."""
    summary = ImportSummary(source=source, rows=len(entries))
    agencies = _known_agencies(session)

    for entry in entries:
        if entry.agency_code and entry.agency_code not in agencies:
            log.info("roster: skipping person %s — unknown agency %s", entry.name, entry.agency_code)
            summary.skipped += 1
            continue
        existing = session.scalar(
            select(Person).where(
                func.lower(Person.name) == entry.name.lower(),
                Person.agency_code == entry.agency_code
                if entry.agency_code
                else Person.agency_code.is_(None),
            )
        )
        if existing is None:
            session.add(
                Person(
                    name=entry.name,
                    rank=entry.rank,
                    badge=entry.badge,
                    agency_code=entry.agency_code,
                    active=entry.active,
                    notes=entry.notes,
                )
            )
            summary.created += 1
            continue
        existing.name = entry.name  # the file's capitalization wins
        existing.rank = entry.rank
        existing.badge = entry.badge
        existing.active = entry.active
        existing.notes = entry.notes
        summary.updated += 1

    session.flush()
    return summary


def import_file(session: Session, path: str | Path, *, kind: str) -> ImportSummary:
    """Import ``units`` or ``personnel`` from a CSV at ``path``."""
    path = Path(path)
    if kind == "units":
        return import_units(session, parse_units_csv(path), source=str(path))
    if kind == "personnel":
        return import_personnel(session, parse_personnel_csv(path), source=str(path))
    raise ValueError(f"unknown roster kind {kind!r}")


__all__ = [
    "PERSONNEL_CSV_HEADER",
    "UNIT_CSV_HEADER",
    "ImportSummary",
    "PersonEntry",
    "UnitEntry",
    "import_file",
    "import_personnel",
    "import_units",
    "parse_personnel_csv",
    "parse_units_csv",
]
