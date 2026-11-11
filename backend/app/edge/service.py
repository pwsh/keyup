"""Applying the HTTPS settings to the running edge, and remembering that it happened.

Three callers, one function:

* **API start-up** (:func:`app.main.lifespan`) — the edge may have just been restarted by a
  deploy, in which case it came back on whatever ``entrypoint.sh`` chose from the
  environment.  Pushing the effective configuration at start-up is what makes the settings
  table, not ``.env``, the thing that decides.
* **the settings save** (``PUT /api/admin/settings``) — a change to any ``tls_*`` key.
* **the worker's five-minute reconcile** — the edge can restart on its own at any time
  (``restart: unless-stopped``, an OOM, an image pull) and nothing tells the API when it
  does.  :func:`reconcile` notices and re-applies.

None of the three may raise: an unreachable proxy is a thing to report on the Admin page,
not a reason for the API to fail to start or for a settings save to 500.  The outcome is
recorded in three ``settings`` rows — ``https_last_applied_at``, ``https_last_error`` and
``https_last_applied_hash`` — which are deliberately *not* in
:data:`app.runtime_settings.SETTINGS_SPEC`: they are bookkeeping this module writes and
reads, not knobs an operator sets, and ``read_overrides`` ignores anything not in the spec.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.edge import admin as edge_admin
from app.edge.render import config_hash, normalize_domains, render_caddyfile, tls_is_effective
from app.models import Setting
from app.runtime_settings import effective_settings

log = logging.getLogger("app.edge.service")

#: The bookkeeping rows.  ``https_`` rather than ``tls_`` so they cannot be confused with
#: the settable group, and so a glance at the table separates the two.
APPLIED_AT_KEY = "https_last_applied_at"
LAST_ERROR_KEY = "https_last_error"
APPLIED_HASH_KEY = "https_last_applied_hash"


def https_settings(session: Session) -> Settings:
    """The effective HTTPS configuration: the settings table over the environment."""
    return effective_settings(session, base=get_settings())


def _note(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if value is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        session.add(Setting(key=key, value=value, updated_by="edge"))
    else:
        row.value = value
        row.updated_by = "edge"


def read_note(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    return None if row is None else row.value


def apply_now(
    session: Session,
    *,
    reason: str,
    settings: Settings | None = None,
    base_url: str | None = None,
) -> edge_admin.ApplyResult:
    """Render the effective configuration, push it, and write down what happened."""
    settings = settings or https_settings(session)
    result = edge_admin.apply(settings, base_url or settings.edge_admin_url)
    _note(session, APPLIED_AT_KEY, datetime.now(UTC).isoformat())
    _note(session, LAST_ERROR_KEY, None if result.ok else result.detail[:2000])
    _note(session, APPLIED_HASH_KEY, result.config_hash if result.ok else None)
    session.commit()
    log.log(
        logging.INFO if result.ok else logging.WARNING,
        "edge.apply reason=%s ok=%s mode=%s domains=%s%s",
        reason,
        result.ok,
        result.mode,
        " ".join(result.domains),
        "" if result.ok else f" error={result.detail[:200]}",
    )
    return result


def reconcile(
    session: Session, *, base_url: str = edge_admin.DEFAULT_BASE_URL
) -> edge_admin.ApplyResult | None:
    """Re-apply if the edge is not running what we last pushed.  ``None`` when it is.

    Comparing Caddy's ``GET /config/`` JSON against our own rendered JSON would be the
    obvious check and is the wrong one: that document is *adapted* output, with generated
    server names and defaults filled in, and it changes shape between Caddy releases — a
    diff over it would either be permanently unequal (re-applying every five minutes
    forever, which costs an ACME lookup each time it is wrong about TLS) or so lenient it
    proves nothing.

    So the comparison is between two cheaper facts:

    * the hash of the Caddyfile we last successfully pushed, kept in ``settings``;
    * whether the edge, right now, answers for the hostnames the settings ask for.

    A restarted edge that came back on the environment's configuration fails the second
    test whenever the settings table says something the environment does not — which is
    exactly the case this exists for.  A hash we never wrote (a fresh database, or the last
    apply failed) also re-applies, because "we have never successfully configured it" is
    the same situation as "it is not configured".
    """
    settings = https_settings(session)
    wanted_tls = tls_is_effective(settings)
    wanted = set(normalize_domains(getattr(settings, "tls_domains", ""))) if wanted_tls else set()
    expected_hash = config_hash(render_caddyfile(settings))

    base_url = base_url or settings.edge_admin_url
    state = edge_admin.status(settings, base_url, probe_certificates=False)
    if not state.reachable:
        log.debug("edge.reconcile: the admin API is unreachable (%s)", state.error)
        return None
    if read_note(session, APPLIED_HASH_KEY) == expected_hash:
        serving = set(state.caddy_domains)
        if wanted_tls and wanted <= serving:
            return None
        if not wanted_tls and not serving:
            return None
    return apply_now(session, reason="reconcile", settings=settings, base_url=base_url)


__all__ = [
    "APPLIED_AT_KEY",
    "APPLIED_HASH_KEY",
    "LAST_ERROR_KEY",
    "apply_now",
    "https_settings",
    "read_note",
    "reconcile",
]
