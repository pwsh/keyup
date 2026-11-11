"""The roster: units, personnel and radio assignments (§4.2).

The directory (:mod:`app.api.directory`) answers "what did the radio network see"; this
answers "who was that".  A unit is the thing a callsign names — Engine 1, Ambulance 21,
Station 3 — a person is a person, and an assignment ties either of them to a radio for a
stretch of time, so a transmission from March resolves against March's assignment rather
than today's.

It is a separate router from the directory because it is a separate *kind* of data: units
and personnel are what an operator curates, while talkgroups and radios are what the
recorder discovers.  Nothing here is ever created by ingest.

Assignment writes go through :mod:`app.services.roster`, which owns the rule that keeps
``radios.unit_id`` — the denormalized label every call list reads — in step with the
current unit assignment.
"""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.uploads import read_import_payload
from app.auth import OptionalPrincipal, WritePrincipal, require_scope
from app.db import get_session
from app.ingest.roster_import import (
    PERSONNEL_CSV_HEADER,
    UNIT_CSV_HEADER,
    import_file,
)
from app.models import Agency, Person, Radio, RadioAssignment, Unit
from app.schemas import (
    AssignedRadioOut,
    ImportResult,
    Page,
    PersonBulkPatch,
    PersonBulkResult,
    PersonDetail,
    PersonIn,
    PersonOut,
    PersonPatch,
    RadioAssignmentIn,
    RadioAssignmentOut,
    UnitBulkPatch,
    UnitBulkResult,
    UnitDetail,
    UnitIn,
    UnitOut,
    UnitPatch,
)
from app.services import bulk
from app.services import roster as roster_service
from app.services.calls import like_contains

router = APIRouter(tags=["roster"])
DbSession = Annotated[Session, Depends(get_session)]

# The roster is not public.  Talkgroups, radios and calls are what the network broadcast;
# units and personnel are who the operator says those radios belong to — names, ranks and
# badge numbers that were never on the air.  The UI hides the pages from a guest, and
# these say the same thing where it counts, so a bare `curl` of the roster is a 401 rather
# than a download.  A radio's assignment history is not a 401 because its detail page is
# public and asks for it; a guest gets an empty list, since every row names one of them.
REQUIRE_READ = [Depends(require_scope("read"))]


def _check_agency(db: Session, code: str | None) -> None:
    """An agency code that is not in the table is a typo, not a new agency."""
    if code and db.get(Agency, code) is None:
        raise HTTPException(status_code=422, detail=f"unknown agency code {code!r}")


def _sorted(stmt, columns: dict[str, object], sort: str):  # noqa: ANN001, ANN202
    """Apply a whitelisted sort key, ``-`` for descending.  Unknown keys are a 422."""
    descending = sort.startswith("-")
    column = columns.get(sort[1:] if descending else sort)
    if column is None:
        raise HTTPException(status_code=422, detail=f"sort: unknown key {sort!r}")
    return stmt.order_by(column.desc().nullslast() if descending else column.asc().nullslast())


def _radio_counts(db: Session, column, ids: list[int]) -> dict[int, int]:
    """``{unit or person id: radios currently assigned}`` for one page of rows."""
    if not ids:
        return {}
    rows = db.execute(
        select(column, func.count())
        .where(column.in_(ids), RadioAssignment.effective_to.is_(None))
        .group_by(column)
    )
    return {int(key): int(count) for key, count in rows}


def _run_import(db: Session, raw: bytes, kind: str) -> ImportResult:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    try:
        summary = import_file(db, temp, kind=kind)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)
    return ImportResult(
        created=summary.created,
        updated=summary.updated,
        skipped=summary.skipped,
        source=summary.source,
        aliases=summary.rows,
    )


# --------------------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------------------


@router.get(
    "/units",
    dependencies=REQUIRE_READ,
    response_model=Page[UnitOut],
    summary="List units",
    description=(
        "Apparatus, stations and crews. `q` matches the callsign, the name or the notes. "
        "`radio_count` is how many radios are assigned to the unit right now."
    ),
)
def list_units(
    db: DbSession,
    response: Response,
    q: Annotated[str | None, Query(description="Callsign, name or notes")] = None,
    agency: Annotated[list[str] | None, Query(description="Agency code; repeat for OR")] = None,
    kind: Annotated[list[str] | None, Query(description="vehicle/station/team/other")] = None,
    sort: Annotated[str, Query(description="`callsign`, `name`, `kind`, `created_at`")] = "callsign",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Page[UnitOut]:
    conds = []
    if q:
        # `%` and `_` are ILIKE wildcards; typed into a search box they ask for every row.
        pattern = like_contains(q)
        conds.append(
            or_(
                Unit.callsign.ilike(pattern, escape="\\"),
                Unit.name.ilike(pattern, escape="\\"),
                Unit.notes.ilike(pattern, escape="\\"),
            )
        )
    if agency:
        conds.append(func.upper(Unit.agency_code).in_([a.upper() for a in agency]))
    if kind:
        conds.append(Unit.kind.in_([k.lower() for k in kind]))

    columns = {
        "callsign": Unit.callsign,
        "name": Unit.name,
        "kind": Unit.kind,
        "created_at": Unit.created_at,
    }
    stmt = _sorted(select(Unit).where(*conds), columns, sort)
    total = int(db.scalar(select(func.count()).select_from(Unit).where(*conds)) or 0)
    rows = list(db.scalars(stmt.limit(page_size).offset((page - 1) * page_size)))
    counts = _radio_counts(db, RadioAssignment.unit_id, [r.id for r in rows])
    response.headers["X-Total-Count"] = str(total)
    return Page[UnitOut](
        items=[
            UnitOut(
                **UnitOut.model_validate(r).model_dump(exclude={"radio_count"}),
                radio_count=counts.get(r.id, 0),
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/units/export",
    dependencies=REQUIRE_READ,
    response_class=PlainTextResponse,
    summary="Export the unit roster as CSV",
    description="The same columns `POST /units/import` accepts, so an export can be re-imported.",
)
def export_units(db: DbSession) -> PlainTextResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(UNIT_CSV_HEADER)
    for unit in db.scalars(select(Unit).order_by(Unit.callsign)):
        writer.writerow(
            [
                unit.callsign,
                unit.name or "",
                unit.agency_code or "",
                unit.kind,
                (unit.notes or "").replace("\n", " "),
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="keyup-units.csv"'},
    )


@router.post(
    "/units/import",
    response_model=ImportResult,
    summary="Import a unit roster",
    description=(
        "A CSV of `callsign,name,agency_code,kind,notes`. Units are matched on "
        "`(agency_code, callsign)` and **overwritten**: importing a roster is an explicit act "
        "and the file is the operator's own naming. A row naming an unknown agency code is "
        "skipped rather than imported without it. Send multipart `file=@units.csv` or JSON "
        '`{\"content\": \"…\", \"format\": \"csv\"}`.'
    ),
)
async def import_units_csv(
    request: Request, db: DbSession, principal: WritePrincipal, file: UploadFile | None = None
) -> ImportResult:
    raw, _suffix = await read_import_payload(request, file, default_suffix=".csv")
    return _run_import(db, raw, "units")


@router.post(
    "/units",
    response_model=UnitOut,
    status_code=201,
    summary="Create a unit",
    description="`callsign` plus, ideally, the agency it belongs to — `E1` means a different engine to each.",
)
def create_unit(body: UnitIn, db: DbSession, principal: WritePrincipal) -> UnitOut:
    _check_agency(db, body.agency_code)
    if _callsign_taken(db, body.callsign, body.agency_code):
        raise HTTPException(status_code=409, detail=f"unit {body.callsign} already exists")
    row = Unit(**body.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # The check above is the friendly answer; this is the same answer for the operator
        # who lost a race to `uq_units_agency_callsign`, which only the database can judge.
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"unit {body.callsign} already exists"
        ) from None
    return UnitOut.model_validate(row)


def _callsign_taken(
    db: Session, callsign: str, agency_code: str | None, *, excluding: int | None = None
) -> bool:
    """Is ``(agency_code, callsign)`` already somebody else's?

    One callsign per agency plus one more in the agency-less namespace — the two halves of
    the unique key — asked before the write so the operator gets a sentence and not an
    ``IntegrityError``.  It cannot be the *only* guard: see the callers' ``except``.
    """
    stmt = select(Unit.id).where(
        Unit.callsign == callsign,
        Unit.agency_code == agency_code if agency_code else Unit.agency_code.is_(None),
    )
    if excluding is not None:
        stmt = stmt.where(Unit.id != excluding)
    return db.scalar(stmt) is not None


@router.get(
    "/units/{unit_id}",
    dependencies=REQUIRE_READ,
    response_model=UnitDetail,
    summary="One unit",
    description=(
        "The unit, the radios assigned to it right now, and their combined traffic. The call "
        "count is the sum over the *currently* assigned radios, so it is only as true as the "
        "assignments are — it is not a history of the callsign."
    ),
)
def get_unit(unit_id: int, db: DbSession) -> UnitDetail:
    row = db.get(Unit, unit_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no unit {unit_id}")
    agency = db.get(Agency, row.agency_code) if row.agency_code else None
    radios = roster_service.assigned_radios(db, unit_id=unit_id)
    call_count, last_call_at = roster_service.roster_call_stats(db, [r.radio_pk for r in radios])
    return UnitDetail(
        **UnitOut.model_validate(row).model_dump(exclude={"radio_count"}),
        radio_count=len(radios),
        agency_name=agency.name if agency else None,
        radios=radios,
        call_count=call_count,
        last_call_at=last_call_at,
    )


@router.patch("/units/{unit_id}", response_model=UnitOut, summary="Edit a unit")
def patch_unit(unit_id: int, body: UnitPatch, db: DbSession, principal: WritePrincipal) -> UnitOut:
    row = db.get(Unit, unit_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no unit {unit_id}")
    fields = body.model_dump(exclude_unset=True)
    if "agency_code" in fields:
        _check_agency(db, fields["agency_code"])
    # Renaming onto a callsign the agency already uses is the operator's mistake, not a
    # 500: `uq_units_agency_callsign` would refuse the UPDATE at commit time.
    callsign = fields.get("callsign") or row.callsign
    agency_code = fields.get("agency_code", row.agency_code)
    if (callsign, agency_code) != (row.callsign, row.agency_code) and _callsign_taken(
        db, callsign, agency_code, excluding=unit_id
    ):
        raise HTTPException(status_code=409, detail=f"unit {callsign} already exists")
    for key, value in fields.items():
        setattr(row, key, value)
    # A renamed callsign has to reach the radios wearing it, or the archive keeps showing
    # the old label for a unit that no longer has that name anywhere else.
    if "callsign" in fields:
        for radio in _current_unit_radios(db, unit_id):
            radio.unit_id = row.callsign
    try:
        db.commit()
    except IntegrityError:  # the check above lost a race with another rename
        db.rollback()
        raise HTTPException(status_code=409, detail=f"unit {callsign} already exists") from None
    return UnitOut.model_validate(row)


@router.post(
    "/units/bulk",
    response_model=UnitBulkResult,
    summary="Edit many units at once",
    description=(
        "`name`, `kind`, `agency_code` and `notes` written onto every id in one transaction. "
        "`callsign` is refused with a 422: it is the unique key this roster is looked up by, so "
        "the same one on two units is a collision, not an edit. Moving a batch between agencies "
        "can still collide with a callsign the destination already uses — that comes back as a 409 "
        "and nothing is written."
    ),
)
def bulk_patch_units(body: UnitBulkPatch, db: DbSession, principal: WritePrincipal) -> UnitBulkResult:
    fields = body.changes.model_dump(exclude_unset=True)
    bulk.require_changes(fields)
    if "agency_code" in fields:
        _check_agency(db, fields["agency_code"])
    rows = bulk.resolve(db, Unit, body.ids, noun="unit")
    bulk.apply_fields(rows, fields)
    # Flushed *before* the activity line, so the constraint speaks while the session can
    # still answer for itself: `activity.record` flushes too, and it swallows what it
    # catches, so a collision discovered in there would surface as an unexplained 500.
    try:
        db.flush()
    except IntegrityError:
        # `uq_units_agency_callsign`: two of the selected callsigns are the same word, and
        # moving both into one agency has just made them the same unit.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="two of those units share a callsign in the agency you moved them to",
        ) from None
    bulk.record(
        db,
        kind="units_bulk_edited",
        noun="units",
        ids=[row.id for row in rows],
        fields=fields,
        by=principal.label,
    )
    db.commit()
    return UnitBulkResult(updated=len(rows), items=[UnitOut.model_validate(row) for row in rows])


def _current_unit_radios(db: Session, unit_id: int) -> list[Radio]:
    return list(
        db.scalars(
            select(Radio)
            .join(RadioAssignment, RadioAssignment.radio_pk == Radio.id)
            .where(RadioAssignment.unit_id == unit_id, RadioAssignment.effective_to.is_(None))
        )
    )


@router.get(
    "/units/{unit_id}/radios",
    dependencies=REQUIRE_READ,
    response_model=list[AssignedRadioOut],
    summary="Radios assigned to this unit",
    description="Current assignments only; the history of one radio is `GET /radios/{id}/assignments`.",
)
def unit_radios(unit_id: int, db: DbSession) -> list[AssignedRadioOut]:
    if db.get(Unit, unit_id) is None:
        raise HTTPException(status_code=404, detail=f"no unit {unit_id}")
    return roster_service.assigned_radios(db, unit_id=unit_id)


# --------------------------------------------------------------------------------------
# personnel
# --------------------------------------------------------------------------------------


@router.get(
    "/personnel",
    dependencies=REQUIRE_READ,
    response_model=Page[PersonOut],
    summary="List personnel",
    description=(
        "`q` matches the name, rank, badge or notes. Inactive people are included unless `active=true`."
    ),
)
def list_personnel(
    db: DbSession,
    response: Response,
    q: Annotated[str | None, Query(description="Name, rank, badge or notes")] = None,
    agency: Annotated[list[str] | None, Query(description="Agency code; repeat for OR")] = None,
    active: Annotated[bool | None, Query(description="Only people on (or off) the roster")] = None,
    sort: Annotated[str, Query(description="`name`, `rank`, `badge`, `created_at`")] = "name",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 200,
) -> Page[PersonOut]:
    conds = []
    if q:
        pattern = like_contains(q)
        conds.append(
            or_(
                Person.name.ilike(pattern, escape="\\"),
                Person.rank.ilike(pattern, escape="\\"),
                Person.badge.ilike(pattern, escape="\\"),
                Person.notes.ilike(pattern, escape="\\"),
            )
        )
    if agency:
        conds.append(func.upper(Person.agency_code).in_([a.upper() for a in agency]))
    if active is not None:
        conds.append(Person.active.is_(active))

    columns = {
        "name": Person.name,
        "rank": Person.rank,
        "badge": Person.badge,
        "created_at": Person.created_at,
    }
    stmt = _sorted(select(Person).where(*conds), columns, sort)
    total = int(db.scalar(select(func.count()).select_from(Person).where(*conds)) or 0)
    rows = list(db.scalars(stmt.limit(page_size).offset((page - 1) * page_size)))
    counts = _radio_counts(db, RadioAssignment.person_id, [r.id for r in rows])
    response.headers["X-Total-Count"] = str(total)
    return Page[PersonOut](
        items=[
            PersonOut(
                **PersonOut.model_validate(r).model_dump(exclude={"radio_count"}),
                radio_count=counts.get(r.id, 0),
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/personnel/export",
    dependencies=REQUIRE_READ,
    response_class=PlainTextResponse,
    summary="Export the personnel roster as CSV",
    description="The same columns `POST /personnel/import` accepts, so an export can be re-imported.",
)
def export_personnel(db: DbSession) -> PlainTextResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(PERSONNEL_CSV_HEADER)
    for person in db.scalars(select(Person).order_by(Person.name)):
        writer.writerow(
            [
                person.name,
                person.rank or "",
                person.badge or "",
                person.agency_code or "",
                (person.notes or "").replace("\n", " "),
                person.active,
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="keyup-personnel.csv"'},
    )


@router.post(
    "/personnel/import",
    response_model=ImportResult,
    summary="Import a personnel roster",
    description=(
        "A CSV of `name,rank,badge,agency_code,notes` and an optional `active`. People are "
        "matched on `(agency_code, name)` — case-insensitively — because `personnel` has no "
        "unique key of its own and re-importing a shift roster must update rather than "
        "duplicate. A row naming an unknown agency code is skipped."
    ),
)
async def import_personnel_csv(
    request: Request, db: DbSession, principal: WritePrincipal, file: UploadFile | None = None
) -> ImportResult:
    raw, _suffix = await read_import_payload(request, file, default_suffix=".csv")
    return _run_import(db, raw, "personnel")


@router.post(
    "/personnel",
    response_model=PersonOut,
    status_code=201,
    summary="Add a person",
    description=(
        "Two people may share a name — this archive cannot tell them apart — so a duplicate name "
        "is accepted rather than refused; the roster importer is the thing that matches on it."
    ),
)
def create_person(body: PersonIn, db: DbSession, principal: WritePrincipal) -> PersonOut:
    _check_agency(db, body.agency_code)
    row = Person(**body.model_dump())
    db.add(row)
    db.commit()
    return PersonOut.model_validate(row)


@router.get(
    "/personnel/{person_id}",
    dependencies=REQUIRE_READ,
    response_model=PersonDetail,
    summary="One person",
    description="Their record, the radios they are carrying right now, and the traffic from those radios.",
)
def get_person(person_id: int, db: DbSession) -> PersonDetail:
    row = db.get(Person, person_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no person {person_id}")
    agency = db.get(Agency, row.agency_code) if row.agency_code else None
    radios = roster_service.assigned_radios(db, person_id=person_id)
    call_count, last_call_at = roster_service.roster_call_stats(db, [r.radio_pk for r in radios])
    return PersonDetail(
        **PersonOut.model_validate(row).model_dump(exclude={"radio_count"}),
        radio_count=len(radios),
        agency_name=agency.name if agency else None,
        radios=radios,
        call_count=call_count,
        last_call_at=last_call_at,
    )


@router.patch("/personnel/{person_id}", response_model=PersonOut, summary="Edit a person")
def patch_person(
    person_id: int, body: PersonPatch, db: DbSession, principal: WritePrincipal
) -> PersonOut:
    row = db.get(Person, person_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no person {person_id}")
    fields = body.model_dump(exclude_unset=True)
    if "agency_code" in fields:
        _check_agency(db, fields["agency_code"])
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
    return PersonOut.model_validate(row)


@router.post(
    "/personnel/bulk",
    response_model=PersonBulkResult,
    summary="Edit many people at once",
    description=(
        "`rank`, `agency_code`, `active` and `notes` written onto every id in one transaction — "
        "how a shift change or a department transfer is actually entered. `name` and `badge` are "
        "accepted by the schema but name one person; the UI does not offer them."
    ),
)
def bulk_patch_personnel(
    body: PersonBulkPatch, db: DbSession, principal: WritePrincipal
) -> PersonBulkResult:
    fields = body.changes.model_dump(exclude_unset=True)
    bulk.require_changes(fields)
    if "agency_code" in fields:
        _check_agency(db, fields["agency_code"])
    rows = bulk.resolve(db, Person, body.ids, noun="person")
    bulk.apply_fields(rows, fields)
    bulk.record(
        db,
        kind="personnel_bulk_edited",
        noun="people",
        ids=[row.id for row in rows],
        fields=fields,
        by=principal.label,
    )
    db.commit()
    return PersonBulkResult(updated=len(rows), items=[PersonOut.model_validate(row) for row in rows])


@router.get(
    "/personnel/{person_id}/radios",
    dependencies=REQUIRE_READ,
    response_model=list[AssignedRadioOut],
    summary="Radios this person is carrying",
)
def person_radios(person_id: int, db: DbSession) -> list[AssignedRadioOut]:
    if db.get(Person, person_id) is None:
        raise HTTPException(status_code=404, detail=f"no person {person_id}")
    return roster_service.assigned_radios(db, person_id=person_id)


# --------------------------------------------------------------------------------------
# assignments
# --------------------------------------------------------------------------------------


@router.get(
    "/radios/{radio_id}/assignments",
    response_model=list[RadioAssignmentOut],
    summary="Who this radio has belonged to",
    description=(
        "Current assignments first, then newest first. A radio may hold one current *unit* "
        "assignment and one current *person* assignment at once — they are independent facts."
    ),
)
def radio_assignments(radio_id: int, db: DbSession, principal: OptionalPrincipal) -> list[RadioAssignmentOut]:
    if db.get(Radio, radio_id) is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    return roster_service.history(db, radio_id) if principal is not None else []


@router.post(
    "/radios/{radio_id}/assignments",
    response_model=RadioAssignmentOut,
    status_code=201,
    summary="Assign this radio to a unit or a person",
    description=(
        "Send exactly one of `unit_id` or `person_id`. Any current assignment *of the same kind* "
        "is closed at the same instant the new one opens, so the history never overlaps and never "
        "loses a row. Assigning a unit also writes that unit's callsign to `radios.unit_id`, the "
        "label the call lists render."
    ),
)
def create_assignment(
    radio_id: int, body: RadioAssignmentIn, db: DbSession, principal: WritePrincipal
) -> RadioAssignmentOut:
    radio = db.get(Radio, radio_id)
    if radio is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    unit = person = None
    if body.unit_id is not None:
        unit = db.get(Unit, body.unit_id)
        if unit is None:
            raise HTTPException(status_code=422, detail=f"no unit {body.unit_id}")
    else:
        person = db.get(Person, body.person_id)
        if person is None:
            raise HTTPException(status_code=422, detail=f"no person {body.person_id}")

    try:
        assignment = roster_service.assign(db, radio, unit=unit, person=person, note=body.note)
        db.commit()
    except IntegrityError:
        # Two operators assigning the same radio at once read the same current row, and
        # `uq_radio_assignments_current_unit` lets exactly one of the inserts through.  The
        # loser is told to look again rather than being handed a 500: by the time it reads
        # back, the winner's assignment is the truth it has to reassign from.
        db.rollback()
        raise HTTPException(
            status_code=409, detail="assignment changed concurrently; retry"
        ) from None
    db.refresh(assignment)
    return roster_service.assignment_out(assignment, unit, person)


@router.delete(
    "/radios/{radio_id}/assignments/current",
    response_model=RadioAssignmentOut,
    summary="End this radio's current assignment",
    description=(
        "Closes the current `unit` or `person` assignment — the row stays as history. Ending a "
        "unit assignment clears `radios.unit_id` only when it still holds that unit's callsign, "
        "so an operator's own edit is never overruled."
    ),
)
def end_assignment(
    radio_id: int,
    db: DbSession,
    principal: WritePrincipal,
    kind: Annotated[Literal["unit", "person"], Query(description="Which assignment to end")],
) -> RadioAssignmentOut:
    radio = db.get(Radio, radio_id)
    if radio is None:
        raise HTTPException(status_code=404, detail=f"no radio {radio_id}")
    assignment = roster_service.end_current(db, radio, kind)
    if assignment is None:
        raise HTTPException(status_code=404, detail=f"radio {radio_id} has no current {kind} assignment")
    unit = db.get(Unit, assignment.unit_id) if assignment.unit_id else None
    person = db.get(Person, assignment.person_id) if assignment.person_id else None
    db.commit()
    db.refresh(assignment)
    return roster_service.assignment_out(assignment, unit, person)


__all__ = ["router"]
