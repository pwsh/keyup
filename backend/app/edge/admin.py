"""Talking to the running edge over Caddy's admin API.

Caddy 2 keeps its whole configuration in memory and exposes it on an HTTP admin endpoint.
``POST /load`` with ``Content-Type: text/caddyfile`` hands it a Caddyfile, which it adapts
and swaps in **atomically**: either the new configuration is valid and takes effect with no
dropped connection, or the old one keeps running and the response says why.  That is what
makes "configure TLS from the Admin page" possible at all, and it is why the answer to
"can the SSL and DNS settings be edited in the UI?" is yes rather than "only by editing
`.env` and redeploying".

Two things about the exposure, both in ``deploy/edge/`` and ``docs/HTTPS.md``:

* The admin API is **unauthenticated**.  Anything that can reach it owns the front door.
  So it listens on ``0.0.0.0:2019`` *inside the container* and ``compose.yaml`` publishes
  no port for it: the only client that can reach ``edge:2019`` is another service on the
  compose network, which today means the ``api`` container.
* The Cloudflare token travels over that hop in the Caddyfile body.  That hop is a unix
  bridge inside one host, the same one the database password already crosses, and the
  token is never logged here — :func:`apply` logs the *hash* of what it pushed.

Certificate status is the awkward part: Caddy exposes its loaded configuration and its
*local* PKI, but nothing that says "here is the certificate currently being served for
this hostname".  So :func:`certificate_status` asks the only oracle that cannot be wrong —
it opens a TLS connection to ``edge:443`` with SNI set to the domain and reads the
certificate off the wire.
"""

from __future__ import annotations

import logging
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.edge.render import config_hash, normalize_domains, render_caddyfile, tls_is_effective

log = logging.getLogger("app.edge")

#: The edge's admin API on the compose network.  Overridable so a test — and the live
#: smoke test in the Makefile — can point at a scratch container.
DEFAULT_BASE_URL = "http://edge:2019"

#: Where a handshake probe connects.  The *edge* rather than the domain's own A record: the
#: point is to read what this deployment is serving, not what DNS happens to resolve to,
#: and inside the compose network the name is what resolves.
DEFAULT_PROBE_HOST = "edge"
DEFAULT_PROBE_PORT = 443

#: Every network call here is against a container one hop away.  Ten seconds is generous
#: for that and short enough that a wedged edge cannot hold an admin request open.
TIMEOUT_S = 10.0


@dataclass(slots=True)
class ApplyResult:
    """What :func:`apply` did, in a shape the API route can return unchanged."""

    ok: bool
    mode: str
    domains: list[str]
    #: sha256 of the Caddyfile that was pushed — never the Caddyfile, which holds the token.
    config_hash: str
    detail: str = ""


@dataclass(slots=True)
class CertificateStatus:
    """One domain's certificate as read off the wire."""

    domain: str
    ok: bool
    issuer: str | None = None
    not_after: str | None = None
    days_left: int | None = None
    #: False when the chain did not verify against the system trust store — the expected
    #: result while ``tls_acme_ca`` is ``staging``, whose root is deliberately untrusted.
    trusted: bool = False
    error: str | None = None


@dataclass(slots=True)
class EdgeStatus:
    """What the edge is actually running, as far as it can be observed."""

    reachable: bool
    #: ``tls`` | ``plain`` | ``unknown`` — what Caddy is serving, not what we want it to.
    mode: str = "unknown"
    #: The hostnames Caddy's loaded configuration matches on.
    caddy_domains: list[str] = field(default_factory=list)
    #: The hostnames the effective settings ask for.
    desired_domains: list[str] = field(default_factory=list)
    desired_mode: str = "plain"
    certificates: list[CertificateStatus] = field(default_factory=list)
    error: str | None = None


# ------------------------------------------------------------------------------- apply ---


def apply(
    settings: Any,
    base_url: str = DEFAULT_BASE_URL,
    *,
    client: httpx.Client | None = None,
) -> ApplyResult:
    """Render the effective configuration and push it into the running edge.

    Never raises for a network or a Caddy-side failure: the caller is a startup hook, a
    settings save and a five-minute reconcile, and none of them may take the API down
    because the proxy is briefly unreachable.  The failure is in the result and in the log.
    """
    text = render_caddyfile(settings)
    digest = config_hash(text)
    tls_on = tls_is_effective(settings)
    mode = "tls" if tls_on else "plain"
    domains = normalize_domains(getattr(settings, "tls_domains", "")) if tls_on else []
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/load",
            content=text.encode(),
            headers={"Content-Type": "text/caddyfile"},
        )
    except httpx.HTTPError as exc:
        log.warning("edge: could not reach the admin API at %s: %s", base_url, exc)
        return ApplyResult(False, mode, domains, digest, f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            client.close()
    if response.status_code >= 400:
        detail = response.text.strip()[:1000]
        log.warning("edge: /load refused the configuration (%s): %s", response.status_code, detail)
        return ApplyResult(False, mode, domains, digest, detail or f"HTTP {response.status_code}")
    log.info("edge: loaded mode=%s domains=%s config=%s", mode, " ".join(domains), digest[:12])
    return ApplyResult(True, mode, domains, digest, response.text.strip()[:1000])


# ------------------------------------------------------------------------------ status ---


def _hosts(node: Any, out: set[str]) -> None:
    """Collect every ``host`` matcher value anywhere in Caddy's adapted JSON.

    A walk rather than a path lookup on purpose: the adapted shape
    (``apps.http.servers.<generated name>.routes[].match[].host[]``) is Caddy's output, not
    an interface, and the server names in it are generated.  All this needs is the set of
    names the running configuration answers for.
    """
    if isinstance(node, dict):
        hosts = node.get("host")
        if isinstance(hosts, list):
            out.update(str(h) for h in hosts)
        for value in node.values():
            _hosts(value, out)
    elif isinstance(node, list):
        for value in node:
            _hosts(value, out)


def certificate_status(
    domain: str,
    *,
    host: str = DEFAULT_PROBE_HOST,
    port: int = DEFAULT_PROBE_PORT,
    timeout: float = TIMEOUT_S,
) -> CertificateStatus:
    """Read the certificate the edge serves for ``domain``, by handshaking with it.

    Caddy's admin API has no "which certificate is loaded for this name" endpoint — the
    certificate cache is not part of the config it exposes — so this is the honest check.
    SNI is the domain, the connection goes to the edge, and hostname checking is off
    because the *address* is ``edge`` while the *name* being asked about is not.

    Verification is attempted against the system trust store first, so a real Let's Encrypt
    certificate reports ``trusted=True``.  A staging certificate fails that (its root is
    untrusted by design), and the second pass reads it anyway so the page can still show an
    issuer and an expiry during a rehearsal.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    try:
        cert = _peer_cert(context, host, port, domain, timeout)
        trusted = True
    except ssl.SSLCertVerificationError as exc:
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        try:
            cert = _peer_cert(unverified, host, port, domain, timeout, binary=True)
        except OSError as inner:
            return CertificateStatus(domain, ok=False, error=f"{type(inner).__name__}: {inner}")
        trusted = False
        if cert is None:
            return CertificateStatus(domain, ok=False, trusted=False, error=str(exc))
    except OSError as exc:
        return CertificateStatus(domain, ok=False, error=f"{type(exc).__name__}: {exc}")
    if not cert:
        return CertificateStatus(domain, ok=False, error="the peer offered no certificate")
    return _describe(domain, cert, trusted=trusted)


def _peer_cert(
    context: ssl.SSLContext,
    host: str,
    port: int,
    server_hostname: str,
    timeout: float,
    *,
    binary: bool = False,
) -> dict[str, Any] | None:
    with (
        socket.create_connection((host, port), timeout=timeout) as raw,
        context.wrap_socket(raw, server_hostname=server_hostname) as tls,
    ):
        if not binary:
            return tls.getpeercert()
        der = tls.getpeercert(binary_form=True)
    return _decode(der) if der else None


def _decode(der: bytes) -> dict[str, Any] | None:
    """Decode a DER certificate without verifying it.

    ``getpeercert()`` answers ``{}`` when the context does not verify, and the standard
    library ships no X.509 parser of its own.  ``ssl._ssl._test_decode_cert`` is OpenSSL's
    own decoder behind a private name; it has been there since Python 2 and returns exactly
    the dict ``getpeercert()`` does.  Wrapped, because "private" means it may go away — and
    if it does, the page loses the issuer line of a *staging* certificate and nothing else.
    """
    import tempfile

    try:
        from ssl import _ssl  # type: ignore[attr-defined]

        with tempfile.NamedTemporaryFile("w", suffix=".pem") as handle:
            handle.write(ssl.DER_cert_to_PEM_cert(der))
            handle.flush()
            return dict(_ssl._test_decode_cert(handle.name))
    except Exception:  # noqa: BLE001 - a best-effort read of an untrusted certificate
        log.debug("edge: could not decode an unverified certificate", exc_info=True)
        return None


def _describe(domain: str, cert: dict[str, Any], *, trusted: bool) -> CertificateStatus:
    issuer = None
    for rdn in cert.get("issuer") or ():
        for key, value in rdn:
            if key in {"organizationName", "commonName"} and issuer is None:
                issuer = str(value)
    not_after = cert.get("notAfter")
    days_left = None
    iso = None
    if not_after:
        try:
            expiry = datetime.strptime(str(not_after), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        except ValueError:
            iso = str(not_after)
        else:
            iso = expiry.isoformat()
            days_left = (expiry - datetime.now(UTC)).days
    return CertificateStatus(
        domain=domain,
        ok=True,
        issuer=issuer,
        not_after=iso,
        days_left=days_left,
        trusted=trusted,
    )


def status(
    settings: Any,
    base_url: str = DEFAULT_BASE_URL,
    *,
    client: httpx.Client | None = None,
    probe_host: str = DEFAULT_PROBE_HOST,
    probe_certificates: bool = True,
) -> EdgeStatus:
    """What the edge is running now, beside what the settings ask for."""
    desired_tls = tls_is_effective(settings)
    desired = normalize_domains(getattr(settings, "tls_domains", "")) if desired_tls else []
    out = EdgeStatus(
        reachable=False,
        desired_domains=desired,
        desired_mode="tls" if desired_tls else "plain",
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        response = client.get(f"{base_url.rstrip('/')}/config/")
        response.raise_for_status()
        config = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if owns_client:
            client.close()
    out.reachable = True
    hosts: set[str] = set()
    _hosts((config or {}).get("apps", {}).get("http", {}), hosts)
    out.caddy_domains = sorted(hosts)
    # A hostname matcher only ever appears in the configurations that serve TLS: the plain
    # variant has one address-less `:80` site and matches nothing by name.
    out.mode = "tls" if hosts else "plain"
    if probe_certificates and out.mode == "tls":
        out.certificates = [
            certificate_status(domain, host=probe_host) for domain in (desired or out.caddy_domains)
        ]
    return out


# --------------------------------------------------------------------- Cloudflare token ---

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


def verify_cloudflare_token(
    token: str,
    domains: list[str],
    *,
    client: httpx.Client | None = None,
    base_url: str = CLOUDFLARE_API,
) -> dict[str, Any]:
    """Prove the token before an issuance attempt spends a rate limit.

    Two questions, in the order that makes a failure legible:

    1. ``GET /user/tokens/verify`` — is this a live token at all?  A revoked or mistyped
       token fails here and nothing else needs saying.
    2. ``GET /zones?name=<zone>`` — can it *see* the zone the names live in?  A token
       scoped to the wrong zone verifies fine and then fails DNS-01 twenty seconds into an
       issuance, which is a much worse place to find out.

    The zone is guessed as the last two labels of each domain, which is right for
    ``example.com`` and wrong for a multi-label public suffix such as ``example.co.uk``; a
    zone that comes back empty says so rather than claiming the token is bad.

    The token is never logged, never echoed into the result and never put in a query
    string — it goes in the ``Authorization`` header, which is where Cloudflare wants it.
    """
    token = str(token or "").strip()
    if not token:
        return {"ok": False, "detail": "no Cloudflare API token is set", "zones": []}
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        verify = client.get(f"{base_url}/user/tokens/verify", headers=headers)
        payload = _json(verify)
        if not payload.get("success"):
            return {"ok": False, "detail": _errors(payload, verify), "zones": []}
        zones = []
        for zone_name in sorted({_zone_of(d) for d in domains if _zone_of(d)}):
            found = _json(client.get(f"{base_url}/zones", params={"name": zone_name}, headers=headers))
            results = found.get("result") or []
            zones.append(
                {
                    "zone": zone_name,
                    "found": bool(results),
                    "id": (results[0].get("id") if results else None),
                    "permissions": sorted(results[0].get("permissions") or []) if results else [],
                }
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "zones": []}
    finally:
        if owns_client:
            client.close()
    status_text = str((payload.get("result") or {}).get("status") or "active")
    missing = [z["zone"] for z in zones if not z["found"]]
    if not domains:
        return {"ok": True, "detail": f"token is {status_text}; no domains to check", "zones": []}
    if missing:
        return {
            "ok": False,
            "detail": (
                f"the token is {status_text} but cannot see "
                f"{', '.join(missing)} — check the token's Zone Resources"
            ),
            "zones": zones,
        }
    return {
        "ok": True,
        "detail": f"the token is {status_text} and can see {', '.join(z['zone'] for z in zones)}",
        "zones": zones,
    }


def _zone_of(domain: str) -> str:
    labels = str(domain or "").strip(".").split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ""


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {"success": False, "errors": [{"message": response.text.strip()[:200]}]}
    return value if isinstance(value, dict) else {"success": False, "errors": []}


def _errors(payload: dict[str, Any], response: httpx.Response) -> str:
    messages = [str(e.get("message") or e) for e in (payload.get("errors") or [])]
    return "; ".join(messages) or f"HTTP {response.status_code}"


__all__ = [
    "DEFAULT_BASE_URL",
    "ApplyResult",
    "CertificateStatus",
    "EdgeStatus",
    "apply",
    "certificate_status",
    "status",
    "verify_cloudflare_token",
]
