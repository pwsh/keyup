"""The agency table, as the Admin page edits it (§4.7).

``GET /api/agencies`` (in :mod:`app.api.directory`) answers "what is on this agency's air"
and is open like the rest of the archive.  These four routes answer "which rows exist and
may I change them", which is an administrative question about reference data every
talkgroup, radio, unit and person can point at — so they take the ``admin`` scope.

The table used to be a mirror of the seeded agency list, refreshed wholesale by
``keyup seed-agencies``.  It is not any more: plenty of what this receiver hears belongs to
an agency that list has no code for — a neighbouring county's sheriff, a railroad, a school
district, a federal unit — and those rows are added here.  ``agencies.source`` keeps the
two populations apart, and the whole point of it is that neither side can quietly destroy
the other: the seed never writes a ``local`` row (see :func:`app.seed.seed_agencies`), and
``DELETE`` here refuses a ``seed`` one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import activity
from app.auth import AdminPrincipal
from app.db import get_session
from app.models import AGENCY_KINDS, Agency, Person, Radio, Talkgroup, Unit
from app.schemas import AdminAgencyOut, AgencyIn, AgencyPatchIn
from app.seed import mark_agency_edited

router = APIRouter(prefix="/admin", tags=["admin"])
DbSession = Annotated[Session, Depends(get_session)]

#: Every table with an ``agency_code``, and the field the counts are reported under.  It is
#: written once because the delete guard and the list endpoint have to agree exactly: a row
#: the list says has nothing pointing at it must be a row the delete accepts.
REFERENCING = (
    ("talkgroup_count", Talkgroup),
    ("radio_count", Radio),
    ("unit_count", Unit),
    ("person_count", Person),
)


def _reference_counts(db: Session) -> dict[str, dict[str, int]]:
    """``{agency code: {field: n}}`` for every table that points at an agency."""
    counts: dict[str, dict[str, int]] = {}
    for field, model in REFERENCING:
        rows = db.execute(
            select(model.agency_code, func.count())
            .where(model.agency_code.isnot(None))
            .group_by(model.agency_code)
        ).all()
        for code, total in rows:
            counts.setdefault(code, {})[field] = int(total)
    return counts


def _out(agency: Agency, counts: dict[str, int]) -> AdminAgencyOut:
    return AdminAgencyOut(
        code=agency.code,
        name=agency.name,
        short_name=agency.short_name,
        kind=agency.kind,
        source=agency.source,
        active=agency.active,
        edited_at=agency.edited_at,
        **{field: counts.get(field, 0) for field, _ in REFERENCING},
    )


def _check_kind(kind: str) -> str:
    if kind not in AGENCY_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"{kind!r} is not an agency kind; use one of {', '.join(AGENCY_KINDS)}",
        )
    return kind


def _get(db: Session, code: str) -> Agency:
    agency = db.get(Agency, code)
    if agency is None:
        raise HTTPException(status_code=404, detail=f"no agency {code!r}")
    return agency


@router.get(
    "/agencies",
    response_model=list[AdminAgencyOut],
    summary="Every agency, with what points at it",
    description=(
        "The whole table — inactive rows included, because this is the page that "
        "reactivates one — ordered by kind and then name, the same order the pickers use. "
        "The four counts are what `DELETE` checks, so a row showing zeroes is a row that "
        "will delete."
    ),
)
def list_admin_agencies(db: DbSession, principal: AdminPrincipal) -> list[AdminAgencyOut]:  # noqa: ARG001 - scope only
    counts = _reference_counts(db)
    rows = db.scalars(select(Agency).order_by(Agency.kind, Agency.name))
    return [_out(agency, counts.get(agency.code, {})) for agency in rows]


@router.post(
    "/agencies",
    response_model=AdminAgencyOut,
    status_code=201,
    summary="Add an agency the seeded list does not have",
    description=(
        "The new row is `source: local`, which is what keeps `keyup seed-agencies` off it "
        "for good. The code is the primary key every talkgroup, radio, unit and person "
        "points at, so it cannot be changed afterwards — a wrong one is deleted and "
        "retyped while nothing references it yet."
    ),
)
def create_agency(body: AgencyIn, db: DbSession, principal: AdminPrincipal) -> AdminAgencyOut:
    code = body.code.strip().upper()
    if db.get(Agency, code) is not None:
        raise HTTPException(status_code=409, detail=f"agency {code!r} already exists")
    agency = Agency(
        code=code,
        name=body.name.strip(),
        # The code is a serviceable abbreviation and a required column; asking for a short
        # name twice for `EXAMPLE_RR` would be a form nobody finishes.
        short_name=(body.short_name or "").strip() or code,
        kind=_check_kind(body.kind),
        source="local",
        active=body.active,
    )
    mark_agency_edited(agency)
    db.add(agency)
    activity.record(
        db,
        source="system",
        level="info",
        kind="agency_created",
        message=f"Added agency {code} ({agency.name})",
        meta={"code": code, "kind": agency.kind, "by": principal.label},
    )
    db.commit()
    return _out(agency, {})


@router.patch(
    "/agencies/{code}",
    response_model=AdminAgencyOut,
    summary="Rename an agency, change its kind, or retire it",
    description=(
        "Works on a seeded row as well as a local one — a `seed` agency's name is often "
        "the one a CAD system shouts in all caps, and this is how it is made readable. Editing "
        "a seeded row stamps `edited_at`, after which `keyup seed-agencies` leaves the name "
        "alone; `keyup seed-agencies --force` is the way back to the shared spelling.\n\n"
        "`active: false` is the retirement that is not a delete: the agency drops out of "
        "the pickers while every call, talkgroup and radio still mapped to it stays exactly "
        "as it is."
    ),
)
def patch_agency(code: str, body: AgencyPatchIn, db: DbSession, principal: AdminPrincipal) -> AdminAgencyOut:
    agency = _get(db, code)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        agency.name = fields["name"].strip()
    if "short_name" in fields:
        agency.short_name = (fields["short_name"] or "").strip() or agency.code
    if "kind" in fields and fields["kind"] is not None:
        agency.kind = _check_kind(fields["kind"])
    if "active" in fields and fields["active"] is not None:
        agency.active = fields["active"]
    mark_agency_edited(agency)
    activity.record(
        db,
        source="system",
        level="info",
        kind="agency_updated",
        message=f"Edited agency {agency.code} ({', '.join(sorted(fields)) or 'nothing'})",
        meta={"code": agency.code, "fields": sorted(fields), "by": principal.label},
    )
    db.commit()
    return _out(agency, _reference_counts(db).get(agency.code, {}))


@router.delete(
    "/agencies/{code}",
    status_code=204,
    summary="Delete a locally added agency nothing points at",
    description=(
        "Two guards, both 409s. A `seed` row is never deleted — it belongs to the seeded list "
        "and the next seed would put it back anyway; deactivate it instead. A row something "
        "still points at is not deleted either: the response carries the four counts so the "
        "page can say what to reassign, and `PATCH {active: false}` is the answer when the "
        "history is worth keeping, which it usually is."
    ),
)
def delete_agency(code: str, db: DbSession, principal: AdminPrincipal) -> Response:
    agency = _get(db, code)
    if agency.source != "local":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{agency.code} came from the seeded agency list and cannot be deleted; "
                "set active to false to take it out of the pickers"
            ),
        )
    counts = _reference_counts(db).get(agency.code, {})
    if any(counts.values()):
        # Not an `HTTPException`: the app's handler flattens a non-string `detail` with
        # `str()`, and the page needs the four numbers as numbers to say what to reassign.
        # So the body is the usual `{"detail": "…"}` plus a `counts` object beside it.
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"{agency.code} is still mapped to "
                    + ", ".join(
                        f"{n} {field.removesuffix('_count')}{'s' if n != 1 else ''}"
                        for field, n in sorted(counts.items())
                        if n
                    )
                    + "; reassign them or set active to false instead"
                ),
                "counts": {field: counts.get(field, 0) for field, _ in REFERENCING},
            },
        )
    activity.record(
        db,
        source="system",
        level="warn",
        kind="agency_deleted",
        message=f"Deleted agency {agency.code} ({agency.name})",
        meta={"code": agency.code, "by": principal.label},
    )
    db.delete(agency)
    db.commit()
    return Response(status_code=204)


__all__ = ["router"]
