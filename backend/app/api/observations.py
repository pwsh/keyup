"""``POST /api/recorder/observations`` — what the recorder sees of a site (§3.5, §4.2).

The archive knows what was *said*; only the recorder knows what the site *is*.  This is
the second thing trunkd posts (the first being the calls themselves, §1.6): one JSON
document per observation window carrying the live control channel, the secondary control
channels the site advertises, the band plan, every traffic channel granted in the window,
the neighbors announced, and the control frequencies the recorder itself is configured to
hunt.  :mod:`app.rr.observations` folds it into ``site_frequencies`` and ``adjacent_sites``;
:mod:`app.rr.audit` reports on the difference between that and RadioReference.

Authentication is the recorder's own: ``Authorization: Bearer kup_…`` on a token carrying
the ``upload`` scope — the same token the call push uses, so a recorder needs one
credential and not two.  Like ``/api/call-upload`` the route is listed in
``OPEN_MUTATION_PATHS``: it authenticates itself, and a recorder is not a browser, so
there is no CSRF proof to give.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import bearer_secret, verify_token
from app.config import Settings, get_settings
from app.db import get_session
from app.ingest.service import UnknownSystem
from app.models import ApiToken
from app.rr.audit import raise_new_findings
from app.rr.message_kinds import raise_message_kind_alerts
from app.rr.observations import record_observation
from app.schemas import RecorderObservationAccepted, RecorderObservationIn

log = logging.getLogger("app.api.observations")

router = APIRouter(tags=["upload"])
DbSession = Annotated[Session, Depends(get_session)]

#: the scopes an ``api_tokens`` row needs to post an observation (``admin`` implies both)
OBSERVATION_SCOPES = ("upload", "admin")


def require_recorder_token(request: Request, db: DbSession) -> ApiToken:
    """The recorder's token, or 401/403.  Mirrors the call-push key check."""
    raw = bearer_secret(request)
    if not raw:
        raise HTTPException(status_code=401, detail="This endpoint needs a recorder API token")
    token = verify_token(db, raw)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired API token")
    if not set(token.scopes or ()) & set(OBSERVATION_SCOPES):
        raise HTTPException(status_code=403, detail="This action needs the 'upload' scope")
    return token


RecorderToken = Annotated[ApiToken, Depends(require_recorder_token)]


@router.post(
    "/recorder/observations",
    response_model=RecorderObservationAccepted,
    summary="Post what a recorder sees of a site",
    description=(
        "One observation window for one site: the live control channel, the secondary control "
        "channels it advertises, the band plan, the traffic channels granted, the neighbors "
        "announced and the control frequencies the recorder is configured to hunt. The document "
        "is stored verbatim and folded into the site's channel inventory, where repeated windows "
        "accumulate counts rather than replacing each other. `instance` — the recorder's own "
        "`instance_id` — is stored beside the window, so a site two boxes both hear can be read "
        "one box at a time (`GET /api/sites/{site_id}/message-mix?instance=…`). Needs a Bearer "
        "token with the `upload` scope — the same one `/api/call-upload` uses."
    ),
)
def post_observation(
    body: RecorderObservationIn,
    db: DbSession,
    token: RecorderToken,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RecorderObservationAccepted:
    # ``mode="json"`` because the document is stored verbatim in a JSONB column;
    # every instant in it is therefore ISO-8601 text, which is what the archive
    # returns anyway.
    document = body.model_dump(mode="json")
    try:
        result = record_observation(db, document, recorder_default=token.label or "recorder")
    except UnknownSystem as exc:
        # The document named no system, and there is no single one to fall back on — the
        # recorder is misconfigured, not the archive.  Same answer the call push gives to
        # the same mistake (``/api/call-upload`` → 400 "incomplete call data: no system"),
        # so a recorder operator reads one diagnosis rather than two.
        db.rollback()
        log.warning("observation refused — the document names no system (%s) [%s]", exc, token.label)
        raise HTTPException(
            status_code=400, detail=f"incomplete observation: no system ({exc})"
        ) from exc
    alerts = 0
    if result.site_id is not None:
        alerts = len(raise_new_findings(db, result.site_id, settings=settings))
    # The message-census findings were computed (and logged) inside the fold; only their
    # delivery belongs out here, beside the channel audit's, where an HTTP call is allowed.
    alerts += len(
        raise_message_kind_alerts(db, result.message_kind_findings, settings=settings)
    )
    db.commit()
    return RecorderObservationAccepted(
        observation_id=result.observation_id,
        system_id=result.system_id,
        site_id=result.site_id,
        frequencies_upserted=result.frequencies_upserted,
        frequencies_new=result.frequencies_new,
        adjacent_upserted=result.adjacent_upserted,
        adjacent_new=result.adjacent_new,
        alerts=alerts,
        note=result.note,
    )


__all__ = ["OBSERVATION_SCOPES", "router"]
