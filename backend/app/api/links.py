"""Persisted links between a call and a record in another system (§4.6).

An outside system — a CAD or calls-for-service application, an incident tracker, a person
with a case number — records here that a call (or a whole conversation) belongs to one of
its records, so the next reader sees a confirmed ``external_id`` against the call rather
than a guess.  Creating or deleting a link emits the ``link.created`` / ``link.deleted``
webhooks (docs/WEBHOOKS.md).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import webhooks
from app.auth import WritePrincipal
from app.db import get_session
from app.models import Call, ExternalLink
from app.schemas import ExternalLinkIn, ExternalLinkOut, Page
from app.services.calls import row_to_call

DbSession = Annotated[Session, Depends(get_session)]

links_router = APIRouter(prefix="/links", tags=["correlation"])


@links_router.get(
    "",
    response_model=Page[ExternalLinkOut],
    summary="Confirmed links to outside records",
    description="Filter by `source` (`cad`, `manual`, …), `external_id`, `call` or `conversation`.",
)
def list_links(
    db: DbSession,
    response: Response,
    source: Annotated[str | None, Query(description="Link source")] = None,
    external_id: Annotated[str | None, Query(description="The outside record's id")] = None,
    call: Annotated[int | None, Query(description="Call id")] = None,
    conversation: Annotated[int | None, Query(description="Conversation id")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Page[ExternalLinkOut]:
    conds = []
    if source:
        conds.append(ExternalLink.source == source)
    if external_id:
        conds.append(ExternalLink.external_id == external_id)
    if call:
        conds.append(ExternalLink.call_id == call)
    if conversation:
        conds.append(ExternalLink.conversation_id == conversation)
    total = int(db.scalar(select(func.count()).select_from(ExternalLink).where(*conds)) or 0)
    rows = db.scalars(
        select(ExternalLink)
        .where(*conds)
        .order_by(ExternalLink.created_at.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[ExternalLinkOut](
        items=[ExternalLinkOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@links_router.post(
    "",
    response_model=ExternalLinkOut,
    status_code=201,
    summary="Record a confirmed link",
    description=(
        "Persist that a call (or a whole conversation) belongs to an outside record — a CAD "
        "integration writes its incident number here. Re-posting the same triple updates the "
        "confidence and note rather than creating a duplicate.\n\n"
        "`source`, `link_confidence` and `link_method` default to `manual`/`strong`/`manual` — a "
        "person's confident assertion. **Those defaults are only for `source=manual`.** Send any "
        "other `source` and `link_confidence` and `link_method` are required: a 422 naming the "
        "missing fields, rather than an automated guess filed as a human's certainty."
    ),
)
def create_link(body: ExternalLinkIn, db: DbSession, principal: WritePrincipal) -> ExternalLinkOut:
    if body.call_id is None and body.conversation_id is None:
        raise HTTPException(status_code=422, detail="one of call_id or conversation_id is required")
    if body.call_id is not None and db.get(Call, body.call_id) is None:
        raise HTTPException(status_code=404, detail=f"no call {body.call_id}")
    existing = db.scalar(
        select(ExternalLink).where(
            ExternalLink.source == body.source,
            ExternalLink.external_id == body.external_id,
            ExternalLink.call_id.is_not_distinct_from(body.call_id),
            ExternalLink.conversation_id.is_not_distinct_from(body.conversation_id),
        )
    )
    row = existing or ExternalLink(
        call_id=body.call_id,
        conversation_id=body.conversation_id,
        source=body.source,
        external_id=body.external_id,
    )
    row.link_confidence = body.link_confidence
    row.link_method = body.link_method
    row.link_note = body.link_note
    row.created_by = principal.label
    if existing is None:
        db.add(row)
    db.flush()  # the delivery payload needs the id, and re-posting must not create two
    webhooks.enqueue_link_event(db, "link.created", row)
    db.commit()
    return ExternalLinkOut.model_validate(row)


@links_router.delete("/{link_id}", status_code=204, summary="Remove a link")
def delete_link(link_id: int, db: DbSession, principal: WritePrincipal) -> Response:
    row = db.get(ExternalLink, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no link {link_id}")
    # Enqueue *before* the delete: the payload is built from a row that is about to stop
    # existing, and a consumer that mirrors links has to be told which one went.
    webhooks.enqueue_link_event(db, "link.deleted", row)
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@links_router.get(
    "/{source}/{external_id}",
    summary="The calls linked to one outside record",
    description=(
        "What an outside system calls when it renders one of its records: every call linked to "
        "that `external_id`, in time order, with the link that records why."
    ),
    response_model=dict[str, Any],
)
def links_for(source: str, external_id: str, db: DbSession) -> dict[str, Any]:
    rows = list(
        db.scalars(
            select(ExternalLink)
            .where(ExternalLink.source == source, ExternalLink.external_id == external_id)
            .order_by(ExternalLink.created_at)
        )
    )
    call_ids = [r.call_id for r in rows if r.call_id is not None]
    conversation_ids = [r.conversation_id for r in rows if r.conversation_id is not None]
    from app.api.filters import CALL_COLUMNS, with_call_joins  # noqa: PLC0415

    calls = []
    if call_ids or conversation_ids:
        stmt = (
            with_call_joins(select(*CALL_COLUMNS).select_from(Call))
            .where(
                or_(
                    Call.id.in_(call_ids or [-1]),
                    Call.conversation_id.in_(conversation_ids or [-1]),
                )
            )
            .order_by(Call.started_at)
        )
        calls = [row_to_call(r) for r in db.execute(stmt)]
    return {
        "source": source,
        "external_id": external_id,
        "links": [ExternalLinkOut.model_validate(r).model_dump() for r in rows],
        "calls": [c.model_dump() for c in calls],
        "count": len(calls),
    }


__all__ = ["links_router"]
