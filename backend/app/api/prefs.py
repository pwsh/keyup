"""Alerts, notifications, saved searches and favorites (§4.7).

These are the per-operator bits of the archive.  Saved searches and favorites are scoped
to the signed-in user; when reads are open and nobody is signed in they fall back to the
shared (``user_id IS NULL``) rows, so a single-operator deployment that never signs in
still gets working favorites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts import (
    REDACTED,
    SKIPPED_SEVERITY,
    redact_channel_config,
    scrub_secrets,
    unredact,
    validate_rule,
)
from app.auth import AdminPrincipal, CurrentUser, WritePrincipal, require_scope
from app.db import get_session
from app.models import AlertRule, Favorite, Notification, SavedSearch, Talkgroup
from app.schemas import (
    AlertRuleIn,
    AlertRuleOut,
    FavoriteIn,
    FavoriteOut,
    NotificationOut,
    Page,
    SavedSearchIn,
    SavedSearchOut,
)

router = APIRouter(tags=["alerts"])
DbSession = Annotated[Session, Depends(get_session)]


# --------------------------------------------------------------------------------------
# alert rules
# --------------------------------------------------------------------------------------


def _alert_out(db: Session, row: AlertRule) -> AlertRuleOut:
    """One rule, with every delivery secret masked (H1).

    Reading the archive needs no account, so a rule's webhook URLs and custom headers —
    which are credentials, not addresses — never appear here.  What is returned in their
    place is :data:`app.alerts.REDACTED`, and sending that value straight back on a PATCH
    leaves the stored secret alone, so the Alerts form still round-trips.
    """
    filters = dict(row.filters or {})
    config = redact_channel_config(row.channel_config)
    fired = int(
        db.scalar(select(func.count()).select_from(Notification).where(Notification.rule_id == row.id)) or 0
    )
    return AlertRuleOut(
        id=row.id,
        name=row.name,
        kind=row.kind,
        pattern=row.pattern,
        filters=filters,
        talkgroup_ids=[int(t) for t in filters.get("talkgroup_ids", [])],
        radio_ids=[int(r) for r in filters.get("radio_ids", [])],
        channels=list(row.channels or ()),
        channel_config=config,
        target=str(config.get("target") or ""),
        cooldown_s=row.cooldown_seconds,
        enabled=row.enabled,
        last_fired_at=row.last_fired_at,
        fire_count=fired,
        created_by=row.created_by,
        created_at=row.created_at,
    )


@router.get(
    "/alerts",
    response_model=list[AlertRuleOut],
    dependencies=[Depends(require_scope("read"))],
    summary="Alert rules",
    description=(
        "A rule watches for a talkgroup, a radio, a keyword or a regex in the transcript, and "
        "delivers to a webhook, Discord, ntfy or email with a cooldown so one busy incident does "
        "not become fifty messages. Reading a rule needs a credential — the rules are the "
        "operator's own configuration, not part of the public archive — and the "
        "delivery secrets — webhook URLs, custom headers — come back masked as `__redacted__`; "
        "sending that mask back unchanged keeps the stored value. Creating, editing and deleting "
        "a rule needs the `admin` scope, because a rule is an outbound request this server makes."
    ),
)
def list_alerts(db: DbSession) -> list[AlertRuleOut]:
    return [_alert_out(db, r) for r in db.scalars(select(AlertRule).order_by(AlertRule.name))]


def _apply_alert(row: AlertRule, body: AlertRuleIn) -> None:
    fields = body.model_dump(exclude_unset=True)
    row.name = fields.get("name", row.name)
    row.kind = fields.get("kind", row.kind)
    if "pattern" in fields:
        row.pattern = fields["pattern"]
    filters = dict(row.filters or {})
    if "filters" in fields and fields["filters"] is not None:
        filters.update(fields["filters"])
    if "talkgroup_ids" in fields and fields["talkgroup_ids"] is not None:
        filters["talkgroup_ids"] = list(fields["talkgroup_ids"])
    if "radio_ids" in fields and fields["radio_ids"] is not None:
        filters["radio_ids"] = list(fields["radio_ids"])
    row.filters = filters
    if "channels" in fields and fields["channels"] is not None:
        row.channels = list(fields["channels"])
    config = dict(row.channel_config or {})
    if "channel_config" in fields and fields["channel_config"] is not None:
        config = unredact(fields["channel_config"], config)
    if "target" in fields and fields["target"] is not None and fields["target"] != REDACTED:
        config["target"] = fields["target"]
    row.channel_config = config
    try:
        validate_rule(row.kind, row.pattern, row.channel_config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if fields.get("cooldown_s") is not None:
        row.cooldown_seconds = int(fields["cooldown_s"])
    if fields.get("enabled") is not None:
        row.enabled = bool(fields["enabled"])


@router.post("/alerts", response_model=AlertRuleOut, status_code=201, summary="Create an alert rule (admin)")
def create_alert(body: AlertRuleIn, db: DbSession, principal: AdminPrincipal) -> AlertRuleOut:
    row = AlertRule(name=body.name, kind=body.kind, created_by=principal.label)
    _apply_alert(row, body)
    db.add(row)
    db.commit()
    return _alert_out(db, row)


@router.patch("/alerts/{rule_id}", response_model=AlertRuleOut, summary="Edit an alert rule (admin)")
def patch_alert(
    rule_id: int, body: AlertRuleIn, db: DbSession, principal: AdminPrincipal
) -> AlertRuleOut:
    row = db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no alert rule {rule_id}")
    _apply_alert(row, body)
    db.commit()
    return _alert_out(db, row)


@router.delete("/alerts/{rule_id}", status_code=204, summary="Delete an alert rule (admin)")
def delete_alert(rule_id: int, db: DbSession, principal: AdminPrincipal) -> Response:
    row = db.get(AlertRule, rule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no alert rule {rule_id}")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


def _status(row: Notification) -> str:
    """``sent`` | ``failed`` | ``skipped`` — a rule the evaluator stepped over is not a failure."""
    if row.ok:
        return "sent"
    return "skipped" if row.severity == SKIPPED_SEVERITY else "failed"


@router.get(
    "/notifications",
    response_model=Page[NotificationOut],
    dependencies=[Depends(require_scope("read"))],
    summary="Delivery log",
    description="Every alert delivery attempt, newest first, with the error when one failed.",
)
def list_notifications(
    db: DbSession,
    response: Response,
    rule: Annotated[int | None, Query(description="Only this rule")] = None,
    hours: Annotated[int | None, Query(ge=1, description="Only the last N hours")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> Page[NotificationOut]:
    conds = []
    if rule:
        conds.append(Notification.rule_id == rule)
    if hours:
        conds.append(Notification.created_at >= datetime.now(UTC) - timedelta(hours=hours))
    total = int(db.scalar(select(func.count()).select_from(Notification).where(*conds)) or 0)
    rows = list(
        db.execute(
            select(Notification, AlertRule.name)
            .outerjoin(AlertRule, Notification.rule_id == AlertRule.id)
            .where(*conds)
            .order_by(Notification.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    )
    response.headers["X-Total-Count"] = str(total)
    return Page[NotificationOut](
        items=[
            NotificationOut(
                id=n.id,
                rule_id=n.rule_id,
                rule_name=name,
                call_id=n.call_id,
                channel=n.channel,
                status=_status(n),
                severity=n.severity,
                subject=n.subject,
                error=scrub_secrets(n.error),
                excerpt=(n.body or "")[:200] or None,
                created_at=n.created_at,
            )
            for n, name in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------------------
# saved searches and favorites
# --------------------------------------------------------------------------------------


def _saved_out(row: SavedSearch) -> SavedSearchOut:
    query = row.query
    if isinstance(query, dict):
        query = query.get("query", "")
    return SavedSearchOut(
        id=row.id,
        name=row.name,
        query=str(query or ""),
        shared=row.is_shared,
        created_at=row.created_at,
        created_by=None,
    )


@router.get(
    "/saved-searches",
    response_model=list[SavedSearchOut],
    summary="Saved searches",
    description="The signed-in user's saved filters plus everything anybody marked as shared.",
)
def list_saved_searches(db: DbSession, user: CurrentUser) -> list[SavedSearchOut]:
    stmt = select(SavedSearch).order_by(SavedSearch.name)
    if user is not None:
        stmt = stmt.where((SavedSearch.user_id == user.id) | SavedSearch.is_shared.is_(True))
    else:
        stmt = stmt.where(SavedSearch.user_id.is_(None) | SavedSearch.is_shared.is_(True))
    return [_saved_out(r) for r in db.scalars(stmt)]


@router.post("/saved-searches", response_model=SavedSearchOut, status_code=201, summary="Save a search")
def create_saved_search(
    body: SavedSearchIn, db: DbSession, principal: WritePrincipal
) -> SavedSearchOut:
    row = SavedSearch(
        user_id=principal.user.id if principal.user is not None else None,
        name=body.name,
        query={"query": body.query},
        is_shared=body.shared,
    )
    db.add(row)
    db.commit()
    return _saved_out(row)


@router.delete("/saved-searches/{search_id}", status_code=204, summary="Delete a saved search")
def delete_saved_search(search_id: int, db: DbSession, principal: WritePrincipal) -> Response:
    row = db.get(SavedSearch, search_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no saved search {search_id}")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


def _favorites(db: Session, user_id: int | None) -> list[FavoriteOut]:
    rows = db.execute(
        select(Favorite, Talkgroup)
        .join(Talkgroup, Favorite.talkgroup_id == Talkgroup.id)
        .where(Favorite.user_id == user_id if user_id is not None else Favorite.user_id.is_(None))
        .order_by(Favorite.sort_order, Talkgroup.alias)
    )
    return [
        FavoriteOut(talkgroup_id=tg.id, alias=tg.alias, tgid=tg.tgid, muted=fav.muted)
        for fav, tg in rows
    ]


@router.get(
    "/favorites",
    response_model=list[FavoriteOut],
    summary="Favorite talkgroups",
    description="The channels pinned to the top of the rail, with a mute flag for the live feed.",
)
def list_favorites(db: DbSession, user: CurrentUser) -> list[FavoriteOut]:
    return _favorites(db, user.id if user is not None else None)


@router.put(
    "/favorites",
    response_model=list[FavoriteOut],
    summary="Pin or mute a talkgroup",
    description="Idempotent: calling it twice with the same talkgroup updates the mute flag.",
)
def put_favorite(body: FavoriteIn, db: DbSession, principal: WritePrincipal) -> list[FavoriteOut]:
    user_id = principal.user.id if principal.user is not None else None
    if db.get(Talkgroup, body.talkgroup_id) is None:
        raise HTTPException(status_code=404, detail=f"no talkgroup {body.talkgroup_id}")
    row = db.scalar(
        select(Favorite).where(
            Favorite.talkgroup_id == body.talkgroup_id,
            Favorite.user_id == user_id if user_id is not None else Favorite.user_id.is_(None),
        )
    )
    if row is None:
        row = Favorite(user_id=user_id, talkgroup_id=body.talkgroup_id, muted=body.muted)
        db.add(row)
    else:
        row.muted = body.muted
    db.commit()
    return _favorites(db, user_id)


@router.delete("/favorites/{talkgroup_id}", status_code=204, summary="Unpin a talkgroup")
def delete_favorite(talkgroup_id: int, db: DbSession, principal: WritePrincipal) -> Response:
    user_id = principal.user.id if principal.user is not None else None
    row = db.scalar(
        select(Favorite).where(
            Favorite.talkgroup_id == talkgroup_id,
            Favorite.user_id == user_id if user_id is not None else Favorite.user_id.is_(None),
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"talkgroup {talkgroup_id} is not a favorite")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


__all__ = ["router"]
