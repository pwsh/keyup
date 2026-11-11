"""The HTTPS edge: one Caddyfile template, and a client for the running proxy's admin API.

* :mod:`app.edge.render` is pure — settings in, Caddyfile text out — and is also the source
  the checked-in ``deploy/edge/Caddyfile.*`` artifacts are generated from.
* :mod:`app.edge.admin` pushes that text into a running Caddy over its admin API, reads
  back what it is serving, and verifies a Cloudflare token before an issuance is attempted.

Everything the Admin page's HTTPS tab does goes through these two (docs/HTTPS.md).
"""

from __future__ import annotations

from app.edge.admin import (
    DEFAULT_BASE_URL,
    ApplyResult,
    CertificateStatus,
    EdgeStatus,
    apply,
    certificate_status,
    status,
    verify_cloudflare_token,
)
from app.edge.render import (
    ACME_DIRECTORIES,
    ADMIN_LISTEN,
    acme_directory,
    checked_in_artifacts,
    config_hash,
    missing_requirements,
    normalize_domains,
    render_caddyfile,
    tls_challenge,
    tls_is_effective,
    write_artifacts,
)

__all__ = [
    "ACME_DIRECTORIES",
    "ADMIN_LISTEN",
    "DEFAULT_BASE_URL",
    "ApplyResult",
    "CertificateStatus",
    "EdgeStatus",
    "acme_directory",
    "apply",
    "certificate_status",
    "checked_in_artifacts",
    "config_hash",
    "missing_requirements",
    "normalize_domains",
    "render_caddyfile",
    "status",
    "tls_challenge",
    "tls_is_effective",
    "verify_cloudflare_token",
    "write_artifacts",
]
