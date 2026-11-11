"""The edge's Caddyfile, from one template.

There are three configurations the edge can run — TLS off, TLS on with the plain ``:80``
listener redirecting browsers to https, and TLS on with ``:80`` serving the whole site in
the clear beside https — and until this module existed they were hand-written files in
``deploy/edge/``.  That was fine while the environment was the only thing that chose
between them.  It stopped being fine when the Admin page gained the ability to push a new
configuration into a *running* Caddy over its admin API (:mod:`app.edge.admin`): the API
container has no copy of those files, and a second hand-written template inside the
backend would be two sources of truth for one proxy.

TLS on comes in two challenge flavours — Cloudflare DNS-01 and plain HTTP-01 — so each TLS
configuration exists once per challenge (:func:`tls_challenge`).

So the template lives here and the files in ``deploy/edge/`` are **generated artifacts**.
They are still checked in — the container must be able to start with no API to ask, which
is what makes a redeploy survivable — and ``tests/test_edge_render.py`` fails if they
drift from what this module renders.  Regenerate them with::

    cd backend && .venv/bin/python -m app.edge

Two rendering modes, one template:

``env_placeholders=True``
    The domains, the ACME directory, the contact address and the Cloudflare token come out
    as Caddy ``{$VAR}`` placeholders, resolved by the edge container from its own
    environment at adapt time.  This is what the checked-in files hold, and it is why the
    token never appears in the repository.
``env_placeholders=False`` (the default)
    Literal values from a :class:`~app.config.Settings`.  This is what is POSTed to the
    admin API, where there is no edge environment to expand a placeholder against — the
    values are resolved on this side, out of the ``settings`` table.
"""

from __future__ import annotations

import hashlib
import ipaddress
from pathlib import Path
from typing import Any, Protocol

#: Where the generated artifacts live, relative to the repository root.
ARTIFACT_DIR = Path("deploy/edge")

#: The ACME directories the ``tls_acme_ca`` enum selects between.  Staging has no rate
#: limits and issues an untrusted certificate; it is the rehearsal target (docs/HTTPS.md).
ACME_DIRECTORIES = {
    "production": "https://acme-v02.api.letsencrypt.org/directory",
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
}

#: How a certificate is obtained.  ``dns`` answers the ACME DNS-01 challenge through
#: Cloudflare (needs a token; the names may resolve to a LAN address).  ``http`` is Caddy's
#: default HTTP-01 / TLS-ALPN-01 (needs the names to resolve publicly to this box, with
#: inbound 80 and 443 reachable; no credential).  ``auto`` is DNS-01 when a token is set and
#: HTTP-01 otherwise, so a deployment that already has a token keeps doing what it did.
TLS_CHALLENGES = ("auto", "dns", "http")

#: The admin API's listen address *inside* the container.  Deliberately not published to
#: the host in ``compose.yaml``: the only intended client is the ``api`` service, which
#: reaches it over the compose network as ``edge:2019``.  Caddy's admin API is
#: unauthenticated and can rewrite the whole proxy, so putting it on the LAN would hand the
#: archive's front door to anyone who can reach the box.
ADMIN_LISTEN = "0.0.0.0:2019"

#: Where the edge sends every request: nginx, by a compose network alias (compose.yaml) and
#: never by the bare service name.  While ``web`` is being recreated Docker's DNS does not
#: know ``web`` and forwards it to the LAN resolver - and ``.web`` is a delegated TLD whose
#: apex answers 127.0.53.53 (ICANN's name-collision address), which is loopback *inside the
#: edge*: Caddy proxied to itself, about 560 hops a second, each holding a connection and a
#: growing ``Via`` header, until ``web`` came back (measured 2,800 hops, ~600 MiB and
#: one 37 KB error line from a single request in 5 s).  ``keyup-web.`` cannot leak that
#: way: an ASCII TLD may not contain a hyphen, so the public tree always answers NXDOMAIN,
#: and the trailing dot makes it absolute - no ``search`` domain (``example.org`` and its
#: future wildcards) is ever appended.  A stopped ``web`` is then one 502 and one log line.
UPSTREAM = "keyup-web.:80"

#: ``trusted_proxies`` when no connector is named: it needs an argument, and nothing but the
#: container itself can be 127.0.0.1.
LOOPBACK_ONLY = "127.0.0.1/32"

#: The proxy body every site block shares.  ``flush_interval -1`` disables response
#: buffering, which the live WebSocket, the SSE feed, ranged audio and streamed exports all
#: need; there is deliberately no ``request_body max_size``.
_PROXY = f"""\
	reverse_proxy {UPSTREAM} {{
		# -1 disables response buffering entirely.  Required for the live WebSocket and
		# the SSE feed (a buffered proxy holds events until the buffer fills), and it is
		# what keeps ranged audio and streamed export bundles flowing.  Caddy upgrades
		# WebSockets natively; there is no `proxy_http_version`/`Upgrade` dance to do.
		flush_interval -1
		# One address, whichever path the request took: TRUSTED_PROXY_HOPS=2 counts this
		# entry plus nginx's own, and a tunnel's extra hop would otherwise shift the count.
		header_up X-Forwarded-For {{client_ip}}
	}}"""

#: Who may name the visitor.  Only a listed connector's ``Cf-Connecting-Ip`` is believed; a
#: LAN client or a recorder is its own address, and sending the header changes nothing.
_SERVERS = """\
	servers {
		trusted_proxies static @@TRUSTED_PROXIES@@
		client_ip_headers Cf-Connecting-Ip
	}
"""


#: The mask for URL-borne credentials: ``/api/feeds/<token>.rss|atom`` and ``feed=``,
#: ``key=``, ``token=`` in a query.  The same two patterns as ``app/log_redact.py`` (and the
#: ``map``s in frontend/nginx.conf), joined into one RE2 alternation because Caddy's filter
#: encoder takes one filter per field; an unmatched group expands to nothing, so each branch
#: rebuilds only its own prefix/suffix.  ``tests/test_edge_render.py`` runs this against the
#: same cases ``tests/test_log_redact.py`` pins.
LOG_URI_REGEXP = r"(/api/feeds/)[^/?.]+(\.(?:rss|atom))|([?&](?:feed|key|token)=)[^&#]*"
LOG_URI_REPLACEMENT = "${1}${3}REDACTED${2}"

#: The default logger, re-declared only to put a filter in front of its JSON encoder.  This
#: is the logger ``http.log.error`` writes to: with no error route, a failed proxy (a backend
#: restarting) logs the whole request, and ``request>uri`` carries the query string - an
#: upload token (``/api/upload/flow?key=kup_...``) in the clear in ``docker compose logs
#: edge``.  A site-level ``log`` directive would not help: it only turns on an
#: access log for its own logger and leaves the error entries on this one.  ``regexp``
#: rather than the ``query`` filter, which re-encodes (and re-orders) the rest of the query
#: string and cannot also mask the feed path.  There is no access log to filter - nginx
#: keeps that, masked the same way.
_LOG = f"""\
	# Mask URL-borne credentials (upload keys, feed tokens) in Caddy's own log lines:
	# the error log records the full request URI whenever the upstream is down.  Same
	# patterns as backend/app/log_redact.py and frontend/nginx.conf.
	log default {{
		format filter {{
			wrap json
			fields {{
				request>uri regexp `{LOG_URI_REGEXP}` `{LOG_URI_REPLACEMENT}`
			}}
		}}
	}}
"""


class _TlsSettings(Protocol):
    """The slice of :class:`~app.config.Settings` this module reads."""

    tls_enabled: bool
    tls_domains: str
    tls_cloudflare_api_token: str
    tls_acme_email: str
    tls_acme_ca: str
    tls_redirect_http: bool
    tls_challenge: str
    edge_trusted_proxies: str


def normalize_domains(raw: str | None) -> list[str]:
    """``"a.example.com  b.example.com\\n"`` -> ``["a.example.com", "b.example.com"]``."""
    return [part for part in str(raw or "").replace(",", " ").split() if part]


def acme_directory(ca: str | None) -> str:
    """The ACME directory URL for the ``production``/``staging`` enum.

    A full URL is passed through unchanged, so a deployment pinned to some other ACME
    server through the environment keeps working.
    """
    value = str(ca or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    return ACME_DIRECTORIES.get(value.lower(), ACME_DIRECTORIES["production"])


def _has_token(settings: _TlsSettings) -> bool:
    return bool(str(getattr(settings, "tls_cloudflare_api_token", "") or "").strip())


def tls_challenge(settings: _TlsSettings) -> str:
    """The ACME challenge the settings resolve to: ``dns`` or ``http``, never ``auto``.

    ``auto`` (the default, and what a blank or unrecognised value means) is DNS-01 when a
    Cloudflare token is set and HTTP-01 otherwise.  ``entrypoint.sh`` resolves
    ``KEYUP_TLS_CHALLENGE`` the same way.
    """
    value = str(getattr(settings, "tls_challenge", "") or "").strip().lower()
    if value in {"dns", "http"}:
        return value
    return "dns" if _has_token(settings) else "http"


def tls_is_effective(settings: _TlsSettings) -> bool:
    """Whether the TLS variants may be rendered at all.

    The switch alone is not enough: a certificate needs a name, a contact address and —
    for DNS-01 — a token, and a half-filled configuration must leave the archive reachable
    on plain http rather than take the front door down.  ``entrypoint.sh`` makes the same
    checks against the environment, and says which one failed.
    """
    return bool(
        getattr(settings, "tls_enabled", False)
        and normalize_domains(getattr(settings, "tls_domains", ""))
        and (tls_challenge(settings) == "http" or _has_token(settings))
        and str(getattr(settings, "tls_acme_email", "") or "").strip()
    )


def missing_requirements(settings: _TlsSettings) -> list[str]:
    """Which TLS prerequisites are blank, in the order the form shows them.

    The token is a prerequisite only of DNS-01; HTTP-01 needs no credential.
    """
    missing = []
    if not normalize_domains(getattr(settings, "tls_domains", "")):
        missing.append("tls_domains")
    if not str(getattr(settings, "tls_acme_email", "") or "").strip():
        missing.append("tls_acme_email")
    if tls_challenge(settings) == "dns" and not _has_token(settings):
        missing.append("tls_cloudflare_api_token")
    return missing


# --------------------------------------------------------------------------- templates ---

_PLAIN = """\
# GENERATED by backend/app/edge/render.py - do not edit by hand.
#
# TLS OFF.
#
# The edge still runs, so the stack is identical to the pre-HTTPS deployment: one plain
# HTTP listener on :80 in front of nginx.  This is the mode a fresh clone starts in, before
# anybody has a Cloudflare token, and the mode to fall back to if certificates ever become
# the problem.
{
	# The admin API, on the compose network only (docs/HTTPS.md): the API container pushes
	# a new configuration here when an operator saves the HTTPS settings.  Not published to
	# the host in compose.yaml - it is unauthenticated and can rewrite the whole proxy.
	admin @@ADMIN_LISTEN@@
	# No ACME here at all: nothing in this file asks for a certificate.
	auto_https off
@@SERVERS@@@@LOG@@}

:80 {
	# Liveness for deploy.sh's health wait and for anything watching the container.
	# Answered by the edge itself so it stays meaningful while `web` is restarting.
	respond /health "ok" 200

@@PROXY@@
}
"""

_TLS_HEAD = """\
# GENERATED by backend/app/edge/render.py - do not edit by hand.
#
# TLS ON, @@REDIRECT_MODE@@.
#
@@CHALLENGE_NOTE@@{
	# The admin API, on the compose network only (docs/HTTPS.md): the API container pushes
	# a new configuration here when an operator saves the HTTPS settings.  Not published to
	# the host in compose.yaml - it is unauthenticated and can rewrite the whole proxy.
	admin @@ADMIN_LISTEN@@
	# The ACME directory: Let's Encrypt production by default, or their staging directory
	# while testing (staging has no rate limits and issues an untrusted certificate).
	acme_ca @@ACME_CA@@
@@AUTO_HTTPS@@@@SERVERS@@@@LOG@@}

# One site block for every TLS hostname (space separated - which is exactly how a
# Caddyfile lists several addresses for one site).
@@DOMAINS@@ {
@@TLS_DIRECTIVE@@
	respond /health "ok" 200

@@PROXY@@

	# Deliberately no `request_body { max_size ... }`: Caddy imposes no limit of its own,
	# and recorder pushes plus migration uploads are large.  Adding one here would give
	# the archive a silent 413 ceiling that nginx and the API do not have.
	#
	# The one header directive is X-Forwarded-For, in the proxy block.  reverse_proxy already
	# forwards the original Host and sets X-Forwarded-Proto / X-Forwarded-Host, so the API
	# sees `https` on the proto header and the browser's own hostname.
}
"""

#: The DNS-01 halves of :data:`_TLS_HEAD`.  Byte-for-byte what the template held before
#: HTTP-01 existed: the checked-in DNS-01 files must not change (``make edge-check``).
_DNS01_NOTE = """\
# Certificates come from Let's Encrypt through the ACME **DNS-01** challenge, answered by
# writing a TXT record in Cloudflare.  Nothing is fetched over port 80 from the internet,
# which is the whole point: these hostnames resolve to a LAN address and no inbound 80/443
# exists, so HTTP-01 and TLS-ALPN-01 can never succeed here.  DNS-01 also means the
# certificate is real and publicly trusted for a name that only resolves inside the house
# - no private CA to install on every phone and browser.
#
# See docs/HTTPS.md.  When this file is what the container starts on, entrypoint.sh has
# already defaulted the ACME directory and refused to select it without a token and an
# email; when the API pushes an equivalent over the admin API, the values are literals it
# resolved from the settings table and the same three checks were made there.
"""

_DNS01_TLS = """\
	tls @@ACME_EMAIL@@ {
		dns cloudflare @@CLOUDFLARE_TOKEN@@
		# The DNS-01 propagation check must ask public resolvers: a LAN resolver with
		# split-horizon answers for these names never sees the _acme-challenge TXT
		# record, and the challenge times out "waiting for record to fully propagate".
		resolvers 1.1.1.1 1.0.0.1
	}
"""

#: The HTTP-01 halves.  No ``dns`` and no ``resolvers``: Caddy's default issuer tries
#: HTTP-01 on :80 and TLS-ALPN-01 on :443 by itself.  The challenge request on :80 never
#: reaches the ``:80`` site block below - Caddy's HTTP server answers
#: ``/.well-known/acme-challenge/`` ahead of every route while an issuance is pending - so
#: the redirect there is not in its way (measured against Pebble; docs/HTTPS.md).
_HTTP01_NOTE = """\
# Certificates come from Let's Encrypt through the ACME **HTTP-01** challenge (or
# TLS-ALPN-01 on :443), which Caddy answers itself.  The CA connects to every hostname
# below on port 80 or 443, so each one must resolve publicly to this box and both ports
# must be reachable from the internet on those exact numbers (WEB_PORT=80 and
# WEB_TLS_PORT=443).  No DNS provider and no credential are involved.  For names that only
# resolve on a LAN, use the DNS-01 files instead (KEYUP_TLS_CHALLENGE=dns).
#
# Caddy answers /.well-known/acme-challenge/ on :80 before any handler in the :80 block
# at the foot of this file runs, so the redirect there does not get in the way.
#
# See docs/HTTPS.md.  When this file is what the container starts on, entrypoint.sh has
# already defaulted the ACME directory and refused to select it without an email; when
# the API pushes an equivalent over the admin API, the values are literals it resolved
# from the settings table and the same checks were made there.
"""

_HTTP01_TLS = """\
	# HTTP-01 / TLS-ALPN-01 are Caddy's defaults: nothing to configure but the contact.
	tls @@ACME_EMAIL@@
"""

_TLS_REDIRECT_80 = """\

# Plain HTTP on :80.  Three cases, and the middle one is the important one.
#
# `handle` rather than a bare `respond` + `redir`: directives inside a site block run in
# Caddy's own fixed order, not the order they are written, and `redir` sorts ahead of
# `respond` - so the naive form redirects /health too, and deploy.sh's health probe reads
# 308 instead of 200.  `handle` blocks are mutually exclusive and the more specific
# matcher wins, which is the ordering this actually wants.  (Measured, not theorised.)
:80 {
	handle /health {
		respond "ok" 200
	}

	# THE API IS NOT REDIRECTED.  Recorders push to http://<the box>/api/call-upload by IP,
	# and no certificate can ever cover an IP address - a 308 to https://<the box>/... fails
	# the handshake on a hostname mismatch and the upload is lost.  So the API keeps
	# answering over plain HTTP on the LAN, exactly as it did before TLS existed, and moving
	# recorders to the hostname is a convenience rather than a cutover prerequisite.
	# Same proxy settings as the https site: unbuffered, and no request body limit.
	handle /api/* {
		reverse_proxy @@UPSTREAM@@ {
			flush_interval -1
			header_up X-Forwarded-For {client_ip}
		}
	}

	# Everything a browser asks for goes to https.
	handle {
		redir https://{host}{uri} 308
	}
}
"""

_TLS_PLAIN_80 = """\

# Plain HTTP on :80, serving the WHOLE site - the redirect is turned off.
#
# https is still served by the block above; this is "and also http", not "instead of".
# The two are the same archive on two schemes, which is what an operator wants while
# something on the LAN cannot follow a redirect, or while a hostname is being cut over.
#
# `auto_https disable_redirects` in the global block above is the other half: without it
# Caddy synthesises its own :80 -> :443 redirect for every name in the site block, and it
# takes precedence over this catch-all - so the names would redirect and only the raw IP
# would be served in the clear, which is exactly the surprise this mode exists to avoid.
:80 {
	respond /health "ok" 200

@@PROXY@@
}
"""


def normalize_trusted_proxies(raw: str | None) -> list[str]:
    """The valid addresses in ``raw``.  Anything else is dropped: this text lands in a Caddyfile."""
    out: list[str] = []
    for part in str(raw or "").replace(",", " ").split():
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        out.append(part)
    return out


def _servers(trusted: str) -> str:
    return _SERVERS.replace("@@TRUSTED_PROXIES@@", trusted)


def _literal_trusted(settings: _TlsSettings) -> str:
    return " ".join(normalize_trusted_proxies(getattr(settings, "edge_trusted_proxies", ""))) or LOOPBACK_ONLY


def _tls_text(  # noqa: PLR0913 - one value per template hole
    *,
    domains: str,
    acme_ca: str,
    acme_email: str,
    token: str,
    redirect: bool,
    trusted: str,
    challenge: str = "dns",
) -> str:
    dns = challenge == "dns"
    head = (
        _TLS_HEAD.replace("@@CHALLENGE_NOTE@@", _DNS01_NOTE if dns else _HTTP01_NOTE)
        .replace("@@TLS_DIRECTIVE@@", _DNS01_TLS if dns else _HTTP01_TLS)
        .replace("@@ADMIN_LISTEN@@", ADMIN_LISTEN)
        .replace("@@SERVERS@@", _servers(trusted))
        .replace("@@LOG@@", _LOG)
        .replace("@@REDIRECT_MODE@@", "http REDIRECTS to https" if redirect else "http ALSO SERVED")
        .replace("@@ACME_CA@@", acme_ca)
        .replace(
            "@@AUTO_HTTPS@@",
            (
                "\t# The :80 block at the foot of this file owns plain http in both modes: it\n"
                "\t# keeps /api and /health in the clear for recorders and redirects the rest.\n"
                "\t# Caddy's own synthesised :80 -> :443 redirect for the names below would\n"
                "\t# take precedence over it and redirect the API too, so it is switched off.\n"
                "\tauto_https disable_redirects\n"
            ),
        )
        .replace("@@DOMAINS@@", domains)
        .replace("@@ACME_EMAIL@@", acme_email)
        .replace("@@CLOUDFLARE_TOKEN@@", token)
        .replace("@@PROXY@@", _PROXY)
    )
    tail = (_TLS_REDIRECT_80 if redirect else _TLS_PLAIN_80).replace("@@PROXY@@", _PROXY)
    return head + tail.replace("@@UPSTREAM@@", UPSTREAM)


def render_caddyfile(settings: _TlsSettings, *, env_placeholders: bool = False) -> str:
    """The complete Caddyfile for one configuration.

    Pure: no I/O, no environment, no clock.  Feed it a :class:`~app.config.Settings` (or
    anything carrying the ``tls_*`` attributes) and it answers with the text
    ``caddy validate`` accepts and ``POST /load`` loads.

    With ``env_placeholders`` the *presence* of the token and the email is not checked —
    the checked-in TLS artifacts are rendered before anybody has either, and the container's
    ``entrypoint.sh`` is what refuses to select them when the environment is half filled.
    """
    tls_on = settings.tls_enabled if env_placeholders else tls_is_effective(settings)
    if not tls_on:
        return (
            _PLAIN.replace("@@ADMIN_LISTEN@@", ADMIN_LISTEN)
            .replace("@@SERVERS@@", _servers(_literal_trusted(settings)))
            .replace("@@LOG@@", _LOG)
            .replace("@@PROXY@@", _PROXY)
        )
    challenge = tls_challenge(settings)
    if env_placeholders:
        return _tls_text(
            domains="{$KEYUP_TLS_DOMAINS}",
            acme_ca="{$ACME_CA}",
            acme_email="{$ACME_EMAIL}",
            token="{$CLOUDFLARE_API_TOKEN}",
            redirect=bool(settings.tls_redirect_http),
            trusted="{$EDGE_TRUSTED_PROXIES}",
            challenge=challenge,
        )
    return _tls_text(
        domains=" ".join(normalize_domains(settings.tls_domains)),
        acme_ca=acme_directory(settings.tls_acme_ca),
        acme_email=str(settings.tls_acme_email).strip(),
        token=str(settings.tls_cloudflare_api_token or "").strip(),
        redirect=bool(settings.tls_redirect_http),
        trusted=_literal_trusted(settings),
        challenge=challenge,
    )


def config_hash(text: str) -> str:
    """A stable fingerprint of a rendered Caddyfile.

    Stored as ``https_last_applied_hash`` so the worker's reconcile can tell "what Caddy is
    running is what we last pushed" from "somebody restarted the edge and it came back on
    the environment's configuration" without diffing Caddy's JSON, which is adapted output
    and not stable enough to compare against anything.  The token is inside the text, so
    this is a hash and never the text itself.
    """
    return hashlib.sha256(text.encode()).hexdigest()


class _Variant:
    """A checked-in artifact: the file name and the settings that produce it."""

    def __init__(self, filename: str, **values: Any) -> None:
        self.filename = filename
        self.values = values


#: The files ``deploy/edge/`` holds and the configuration each one is.  TLS off has no
#: redirect variant — there is nothing to redirect *to* — and no challenge; TLS on has one
#: per redirect mode per challenge, so five.  The DNS-01 pair keeps its original names.
VARIANTS = [
    _Variant("Caddyfile.plain", tls_enabled=False, tls_redirect_http=True, tls_challenge="dns"),
    _Variant("Caddyfile.tls", tls_enabled=True, tls_redirect_http=True, tls_challenge="dns"),
    _Variant("Caddyfile.tls-noredirect", tls_enabled=True, tls_redirect_http=False, tls_challenge="dns"),
    _Variant("Caddyfile.tls-http01", tls_enabled=True, tls_redirect_http=True, tls_challenge="http"),
    _Variant(
        "Caddyfile.tls-http01-noredirect",
        tls_enabled=True,
        tls_redirect_http=False,
        tls_challenge="http",
    ),
]


class _Stub:
    """A settings-shaped object for the placeholder renders, so no ``.env`` is read."""

    tls_domains = "placeholder.example.com"
    tls_cloudflare_api_token = "placeholder"  # noqa: S105 - not a credential
    tls_acme_email = "placeholder@example.com"
    tls_acme_ca = "production"

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


def checked_in_artifacts() -> dict[str, str]:
    """``{filename: text}`` — exactly what ``deploy/edge/`` must contain."""
    return {
        variant.filename: render_caddyfile(_Stub(**variant.values), env_placeholders=True)
        for variant in VARIANTS
    }


def write_artifacts(root: Path) -> list[Path]:
    """Write the artifacts under ``root/deploy/edge``; returns the paths written."""
    written = []
    for name, text in checked_in_artifacts().items():
        path = root / ARTIFACT_DIR / name
        path.write_text(text)
        written.append(path)
    return written


__all__ = [
    "ACME_DIRECTORIES",
    "ADMIN_LISTEN",
    "ARTIFACT_DIR",
    "LOG_URI_REGEXP",
    "LOG_URI_REPLACEMENT",
    "TLS_CHALLENGES",
    "UPSTREAM",
    "VARIANTS",
    "acme_directory",
    "checked_in_artifacts",
    "config_hash",
    "missing_requirements",
    "normalize_domains",
    "render_caddyfile",
    "tls_challenge",
    "tls_is_effective",
    "write_artifacts",
]
