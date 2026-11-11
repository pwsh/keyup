"""``/api/admin/https`` — configuring the archive's own TLS from the Admin page.

Admin-only, three routes:

``GET /status``
    what the running edge is serving, what the settings ask for, and the certificate on the
    wire for each domain.
``POST /apply``
    render the effective settings into a Caddyfile and push it into the running proxy.
``POST /test-dns``
    prove the Cloudflare token — that it is live, and that it can see the zone the domains
    live in — before an issuance attempt spends a Let's Encrypt rate limit on finding out.

The response models live here rather than in :mod:`app.schemas` because nothing outside
this router uses them; the generated TypeScript picks them up from ``openapi.json`` either
way.

Why this can be a UI at all, which was the open question: Caddy's admin API takes a whole
new configuration over HTTP and swaps it in atomically, so "change the SSL and DNS
settings" does not have to mean "edit `.env` and redeploy".  What it costs is an
unauthenticated control port on the compose network — see :mod:`app.edge.admin`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import AdminPrincipal
from app.db import get_session
from app.edge import admin as edge_admin
from app.edge import service as edge_service
from app.edge.render import acme_directory, missing_requirements, normalize_domains, tls_is_effective

router = APIRouter(prefix="/admin/https", tags=["admin"])
DbSession = Annotated[Session, Depends(get_session)]


class CertificateOut(BaseModel):
    """One domain's certificate, read off the wire by handshaking with the edge."""

    domain: str
    ok: bool
    issuer: str | None = None
    not_after: str | None = None
    days_left: int | None = None
    #: False while ``tls_acme_ca`` is ``staging`` — its root is untrusted by design.
    trusted: bool = False
    error: str | None = None


class HttpsStatusOut(BaseModel):
    """Everything the HTTPS tab's status card shows."""

    #: What the settings ask for: ``tls`` or ``plain``.
    desired_mode: str
    #: What the edge is actually serving: ``tls``, ``plain`` or ``unknown`` when unreachable.
    mode: str
    #: True when the edge's admin API answered at all.
    reachable: bool
    domains: list[str]
    caddy_domains: list[str]
    redirect_http: bool
    acme_ca: str
    acme_directory: str
    acme_email: str
    #: Whether a Cloudflare token is stored.  The token itself is never returned.
    has_token: bool
    #: Which of domains / email / token are still blank, when the switch is on.
    missing: list[str]
    certificates: list[CertificateOut] = Field(default_factory=list)
    last_applied_at: str | None = None
    last_error: str | None = None
    #: Set when the edge is unreachable, so the page can say "unreachable" rather than "off".
    error: str | None = None


class ApplyOut(BaseModel):
    ok: bool
    mode: str
    domains: list[str]
    detail: str = ""


class TestDnsZone(BaseModel):
    zone: str
    found: bool
    id: str | None = None
    permissions: list[str] = Field(default_factory=list)


class TestDnsOut(BaseModel):
    ok: bool
    detail: str
    zones: list[TestDnsZone] = Field(default_factory=list)


@router.get(
    "/status",
    response_model=HttpsStatusOut,
    summary="HTTPS status (admin)",
    description=(
        "What the edge is serving beside what the settings ask for, plus the certificate on "
        "the wire for each domain — issuer, expiry and days left — read by opening a TLS "
        "connection to the edge with SNI set to the name. The Cloudflare token is never "
        "returned; `has_token` says only whether one is stored."
    ),
)
def get_https_status(db: DbSession, principal: AdminPrincipal) -> HttpsStatusOut:
    settings = edge_service.https_settings(db)
    state = edge_admin.status(settings, settings.edge_admin_url)
    desired_tls = tls_is_effective(settings)
    return HttpsStatusOut(
        desired_mode="tls" if desired_tls else "plain",
        mode=state.mode if state.reachable else "unknown",
        reachable=state.reachable,
        domains=normalize_domains(settings.tls_domains),
        caddy_domains=state.caddy_domains,
        redirect_http=bool(settings.tls_redirect_http),
        acme_ca=str(settings.tls_acme_ca),
        acme_directory=acme_directory(settings.tls_acme_ca),
        acme_email=str(settings.tls_acme_email),
        has_token=bool(str(settings.tls_cloudflare_api_token or "").strip()),
        missing=missing_requirements(settings) if settings.tls_enabled else [],
        certificates=[CertificateOut(**asdict(c)) for c in state.certificates],
        last_applied_at=edge_service.read_note(db, edge_service.APPLIED_AT_KEY),
        last_error=edge_service.read_note(db, edge_service.LAST_ERROR_KEY),
        error=state.error,
    )


@router.post(
    "/apply",
    response_model=ApplyOut,
    summary="Push the HTTPS configuration to the edge (admin)",
    description=(
        "Renders the effective settings into a Caddyfile and loads it into the running proxy. "
        "Caddy swaps configurations atomically: a rejected one leaves the old one serving. "
        "This runs automatically on API start-up and whenever an HTTPS setting is saved, so "
        "the button is for retrying after a failure. A half-filled configuration — the switch "
        "on with a blank domain, address or token — applies the **plain HTTP** configuration "
        "rather than failing, which is the same fallback the edge's own entrypoint makes."
    ),
)
def post_https_apply(db: DbSession, principal: AdminPrincipal) -> ApplyOut:
    result = edge_service.apply_now(db, reason=f"admin:{principal.label}")
    return ApplyOut(ok=result.ok, mode=result.mode, domains=result.domains, detail=result.detail)


@router.post(
    "/test-dns",
    response_model=TestDnsOut,
    summary="Check the Cloudflare API token (admin)",
    description=(
        "Asks Cloudflare two questions with the stored token: `GET /user/tokens/verify` (is it "
        "live?) and `GET /zones?name=…` for each domain's zone (can it see the zone?). A token "
        "scoped to the wrong zone passes the first and fails the second, which is exactly the "
        "mistake that would otherwise surface twenty seconds into a failed issuance. Writes "
        "nothing, issues nothing, and never logs or returns the token."
    ),
)
def post_https_test_dns(db: DbSession, principal: AdminPrincipal) -> TestDnsOut:
    settings = edge_service.https_settings(db)
    result = edge_admin.verify_cloudflare_token(
        str(settings.tls_cloudflare_api_token or ""),
        normalize_domains(settings.tls_domains),
    )
    return TestDnsOut(
        ok=bool(result["ok"]),
        detail=str(result["detail"]),
        zones=[TestDnsZone(**zone) for zone in result.get("zones", [])],
    )


__all__ = ["router"]
