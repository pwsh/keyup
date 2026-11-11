"""``GET /api/directory/{sites,systems}/{id}/channel-audit`` — the report (§4.2, §3.5).

Computed on every request from ``site_frequencies`` and ``adjacent_sites``: the report is
a *comparison*, and a stored comparison is one that is wrong the moment either side moves.
The scheduled job in :mod:`app.rr.audit` logs the same numbers hourly and raises the
alerts; it caches nothing for this route to read.

Reads follow the archive's rule (``AUTH_REQUIRE_READ``): open by default, exactly like
``GET /sites``, so the web UI's Channel audit panel works for a reader without an account.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.rr.audit import audit_site, audit_system
from app.schemas import SiteChannelAudit, SystemChannelAudit

router = APIRouter(prefix="/directory", tags=["directory"])
DbSession = Annotated[Session, Depends(get_session)]


def report_clock() -> datetime:
    """The instant the report is computed at — "missing" means not heard since this minus
    ``CHANNEL_AUDIT_STALE_DAYS``.  A dependency, not an inline ``datetime.now``, so a test can
    pin it (``app.dependency_overrides[report_clock]``) the way it passes ``now=`` to
    :func:`app.rr.audit.raise_new_findings`; otherwise a frozen fixture drifts with the date.
    """
    return datetime.now(UTC)


Now = Annotated[datetime, Depends(report_clock)]


@router.get(
    "/sites/{site_id}/channel-audit",
    response_model=SiteChannelAudit,
    summary="Channel audit for one site",
    description=(
        "What the site is on the air, what RadioReference documents and what the recorder is "
        "configured to hunt, and every difference between the three: **undocumented** channels "
        "(heard, not in RR), **missing** channels (in RR, not heard within "
        "`CHANNEL_AUDIT_STALE_DAYS`), **control drift** (RR and the air disagree about which "
        "frequency is the control channel), the **recorder config** gap with a ready-made "
        "control-frequency list, and the **adjacent sites** the control channel announces."
    ),
)
def site_channel_audit(site_id: int, db: DbSession, now: Now) -> SiteChannelAudit:
    report = audit_site(db, site_id, now=now)
    if report is None:
        raise HTTPException(status_code=404, detail="No such site")
    return SiteChannelAudit.model_validate(report)


@router.get(
    "/systems/{system_id}/channel-audit",
    response_model=SystemChannelAudit,
    summary="Channel audit summary for a system",
    description=(
        "One row per site — undocumented, missing, drifted and unconfigured counts, plus the "
        "unknown neighbors — so a system with a dozen towers says which one to open."
    ),
)
def system_channel_audit(system_id: int, db: DbSession, now: Now) -> SystemChannelAudit:
    report = audit_system(db, system_id, now=now)
    if report is None:
        raise HTTPException(status_code=404, detail="No such system")
    return SystemChannelAudit.model_validate(report)


__all__ = ["report_clock", "router"]
