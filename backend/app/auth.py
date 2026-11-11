"""Authentication and authorization for the Keyup API (docs/ARCHITECTURE.md §3.4, §4.7).

Reads are open by default (``AUTH_REQUIRE_READ=false``) — except
everything under ``/api/admin``, which always needs a signed-in principal with ``read``
(:data:`ADMIN_PATH_PREFIX`); every request that *changes* data needs a credential.
Two credentials are accepted:

* a **session cookie** (``keyup_session``) obtained from ``POST /api/auth/login``.
  Because a cookie is attached by the browser automatically, a cookie-authenticated
  mutation must also carry ``X-Requested-With: keyup`` — a header a cross-site form
  or ``<img>`` cannot set — plus a same-site ``Origin``/``Referer``;
* an **API token** ``kup_<40 url-safe characters>`` sent as ``Authorization: Bearer …``.
  A browser never attaches one on its own, so token requests need no CSRF proof.  Tokens
  (:data:`ACCEPTED_TOKEN_PREFIXES`).

Both resolve to a :class:`Principal`: who the caller is and what scopes they hold.  The
scope vocabulary is the one in the schema — ``read``, ``write``, ``admin``, ``stream``,
``upload`` — and ``admin`` implies all of them.

Design notes
------------
* Passwords are hashed with **argon2id** (``argon2-cffi``); the parameters live inside the
  hash string, so raising them later re-hashes on the next successful login.
* Sessions and tokens are **database rows**; only a sha256 of the secret is stored, so a
  database dump cannot be replayed and revoking is a ``DELETE``/``revoked_at``.
* Two session deadlines: absolute ``AUTH_SESSION_DAYS`` (``expires_at``, 14 days) and a
  sliding idle window of ``AUTH_IDLE_HOURS`` on ``last_seen_at``.  Both are **runtime
  settings** (Admin -> Settings, group "Access") read through
  :func:`app.runtime_settings.effective_settings` rather than from the environment
  directly, so changing how long a sign-in lasts needs no deploy and the environment values
  are their defaults.  The absolute one is stamped on the row at creation and so applies to
  the next sign-in; the idle one is re-read on every request and so applies at once.
* :func:`mutation_guard` is installed as an application-level dependency in ``app.main``
  so every route — including ones added later — is covered by default.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.config import Settings, get_settings
from app.db import get_session
from app.models import ApiToken, User, UserSession
from app.runtime_settings import effective_settings

log = logging.getLogger("app.auth")

#: Name of the session cookie.  ``Path=/`` so the SPA and ``/api`` share it.
COOKIE_NAME = "keyup_session"

#: Header the SPA always sends; a cross-site form or image cannot set it.
CSRF_HEADER = "x-requested-with"
CSRF_HEADER_VALUE = "keyup"

#: Prefix of every API token minted from now on.  The rest is 40 url-safe characters of
#: ``secrets``.
TOKEN_PREFIX = "kup_"
#: Prefixes :func:`verify_token` accepts.  A token is looked up by the sha256 of the whole
#: string, so the prefix is only a cheap "this cannot possibly be one of ours" screen.
ACCEPTED_TOKEN_PREFIXES = (TOKEN_PREFIX,)
TOKEN_BODY_LENGTH = 40

#: Methods that never change data.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The whole scope vocabulary (§3.4).
SCOPES = ("read", "write", "admin", "stream", "upload")

#: Non-GET routes that must work without a credential: they are how you *get* one, or they
#: check one themselves.  ``/api/call-upload`` authenticates with the recorder's ``key`` form
#: field; ``/api/recorder/observations`` (§3.5) and ``/api/recorder/events`` (§3.6) with a
#: Bearer token carrying the ``upload`` scope — none of the three holds ``write``, so the
#: guard's rule below would refuse them all.
OPEN_MUTATION_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/call-upload",
        "/api/upload/exists",
        "/api/recorder/observations",
        "/api/recorder/events",
        "/api/recorder/tuners/status",
        "/api/recorder/spectrum",
        "/api/recorder/event-log",
        # The Broadcastify claim: a recorder asks with the same ``upload``-scoped Bearer
        # token as the poll, one HTTP request before it uploads a call, and holds no
        # ``write`` scope either (docs/EXPORTERS.md, "Recorder-direct upload").
        "/api/recorder/broadcastify/claim",
    }
)

#: Routes that authenticate themselves and therefore need no CSRF proof either — a recorder
#: posts from a Java client or a daemon, not from a browser, and carries no session cookie
#: for a cross-site page to ride on.
NO_CSRF_PATHS = frozenset(
    {
        "/api/call-upload",
        "/api/upload/exists",
        "/api/recorder/observations",
        "/api/recorder/events",
        "/api/recorder/tuners/status",
        "/api/recorder/spectrum",
        "/api/recorder/event-log",
        # The Broadcastify claim: a recorder asks with the same ``upload``-scoped Bearer
        # token as the poll, one HTTP request before it uploads a call, and holds no
        # ``write`` scope either (docs/EXPORTERS.md, "Recorder-direct upload").
        "/api/recorder/broadcastify/claim",
    }
)

#: GET routes a *recorder* reads, which authenticate an ``upload``-scoped token themselves
#: and must therefore not be held to the ``read``-scope bar the guard applies to every other
#: GET while ``AUTH_REQUIRE_READ`` is on.  An ``upload`` token holds no ``read`` scope on
#: purpose (it lives on field boxes), so without this list closing reads would stop the
#: recorders.  The whole set, checked against every client:
#:
#: * ``/api/recorder/desired-state`` — trunkd's tuner-config poll (Bearer; the route's
#:   ``require_recorder_token`` dependency demands ``upload`` or ``admin``);
#: * ``/api/upload/flow`` — the queue gauge ``tools/migrate_sdrtrunk.py`` polls before each
#:   batch, with the token in ``?key=`` (``verify_upload_key`` demands ``upload``/``admin``).
#:
#: Every other recorder request is a POST (``OPEN_MUTATION_PATHS``) or the push socket
#: (``WS_SELF_AUTH_PATHS``).
UPLOAD_READ_PATHS = frozenset({"/api/recorder/desired-state", "/api/upload/flow"})

#: WebSocket routes that authenticate themselves and must not be held to the ``read``-scope
#: bar the guard applies to a socket when ``AUTH_REQUIRE_READ`` is on.  ``/api/recorder/stream``
#: is the recorder's push channel: it checks its own ``upload``-scoped Bearer token (or
#: ``?key=``) and closes with 4401 when there is not one, and an ``upload`` token holds no
#: ``read`` scope — so the guard would otherwise refuse the one client the route exists for.
WS_SELF_AUTH_PATHS = frozenset({"/api/recorder/stream"})

#: Every read under this prefix needs a signed-in principal holding ``read`` — whatever
#: ``AUTH_REQUIRE_READ`` says, and whatever the route itself declares.  The Admin page's
#: reads carry operator data (settings incl. the ACME e-mail and webhook URL, file paths,
#: job logs, export state), so they are closed to guests here, in the app-level guard, and
#: not route by route: an admin GET added later is covered without anyone remembering to.
#: Routes that need more (``admin``) keep their own stricter dependency.
ADMIN_PATH_PREFIX = "/api/admin"
ADMIN_READ_DETAIL = "Sign in to read the Admin pages"


def is_admin_path(path: str) -> bool:
    """True for ``/api/admin`` and everything beneath it (not ``/api/administrator``)."""
    return path == ADMIN_PATH_PREFIX or path.startswith(ADMIN_PATH_PREFIX + "/")


def require_admin_read(request: HTTPConnection, session: Session) -> None:
    """401 for a guest, 403 for a credential without ``read`` (an ``upload`` token)."""
    principal = authenticate(request, session)
    if principal is None:
        raise HTTPException(status_code=401, detail=ADMIN_READ_DETAIL)
    if "read" not in principal.scopes:
        raise HTTPException(status_code=403, detail="Reading the Admin pages needs the 'read' scope")


#: Suffixes of the public syndication routes (``/api/feeds/<token>.rss``).  These carry
#: their own credential in the path — the feed token — and are read by software that has
#: no account and no cookie jar, so they must answer even when ``AUTH_REQUIRE_READ`` closes
#: the archive.  The *management* routes (``/api/feeds``, ``/api/feeds/{id}``) are not
#: listed and stay behind the normal rules.
FEED_DOCUMENT_SUFFIXES = (".rss", ".atom")
FEED_PATH_PREFIX = "/api/feeds/"
#: The built-in feeds (docs/FEEDS.md).  They live under the same prefix and end in the
#: same suffixes, but they carry **no** token and are therefore not self-authenticating:
#: they are ordinary reads, open exactly while the archive is, which is the whole point of
#: having them.
FEED_PRESET_PREFIX = "/api/feeds/preset/"


def feed_self_authenticating(request: HTTPConnection) -> bool:
    """True for the two reads a feed token authenticates by itself (docs/FEEDS.md).

    The route checks the token — an unknown or disabled one is a 404 — and the audio route
    additionally refuses a call the feed's own filters do not match, so the exemption here
    widens nothing beyond what a holder of that URL is already entitled to.
    """
    path = request.scope.get("path", request.url.path)
    if (
        path.startswith(FEED_PATH_PREFIX)
        and path.endswith(FEED_DOCUMENT_SUFFIXES)
        and not path.startswith(FEED_PRESET_PREFIX)
    ):
        return True
    return bool(
        path.startswith("/api/calls/")
        and path.endswith("/audio")
        and request.query_params.get("feed")
    )


UNAUTHENTICATED_DETAIL = "Sign in to change data"
BAD_TOKEN_DETAIL = "Invalid or expired API token"

_hasher = PasswordHasher()


# --------------------------------------------------------------------------- passwords ---


def hash_password(password: str) -> str:
    """argon2id hash of ``password`` (the parameters are embedded in the result)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time check of ``password`` against ``password_hash``."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


#: A real argon2id hash of a random string, made once on first use with the same
#: parameters as every stored hash, so a sign-in for a username that does not exist costs
#: the same argon2 verification as one for a username that does.
_dummy_hash: str | None = None


def verify_password_or_dummy(password_hash: str | None, password: str) -> bool:
    """:func:`verify_password`, or the same work against a dummy hash when there is no user.

    Skipping argon2 for an unknown username answers measurably faster than a wrong
    password for a real one, which tells a prober which usernames exist.  Always ``False``
    when ``password_hash`` is ``None``.
    """
    global _dummy_hash  # noqa: PLW0603 - a one-time lazy constant
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash(secrets.token_urlsafe(24))
        verify_password(_dummy_hash, password)
        return False
    return verify_password(password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# ------------------------------------------------------------------------ secret plumbing ---


def hash_secret(secret: str) -> str:
    """sha256 of a session token or API token; only this ever reaches the database."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_token() -> str:
    """``kup_`` + 40 url-safe characters (§4.7)."""
    body = secrets.token_urlsafe(48)[:TOKEN_BODY_LENGTH]
    return f"{TOKEN_PREFIX}{body}"


def normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Keep only known scopes, de-duplicated, in the canonical order."""
    wanted = {s.strip().lower() for s in (scopes or ()) if s and s.strip()}
    unknown = wanted - set(SCOPES)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown scope(s): {', '.join(sorted(unknown))}")
    return [s for s in SCOPES if s in wanted]


def effective_scopes(scopes: list[str] | None) -> frozenset[str]:
    """``admin`` implies every other scope; ``write`` implies ``read``."""
    held = set(scopes or ())
    if "admin" in held:
        return frozenset(SCOPES)
    if "write" in held:
        held.add("read")
    return frozenset(held)


# ------------------------------------------------------------------------------ principal ---


@dataclass(frozen=True)
class Principal:
    """Who is making this request and what they may do."""

    kind: str  # "session" | "token"
    scopes: frozenset[str]
    user: User | None = None
    token: ApiToken | None = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    @property
    def label(self) -> str:
        """What to write into an audit column (``created_by``, ``updated_by``)."""
        if self.user is not None and self.token is not None:
            return f"{self.user.username} (token:{self.token.label})"
        if self.user is not None:
            return self.user.username
        return f"token:{self.token.label}" if self.token is not None else "anonymous"


# ------------------------------------------------------------------------------- cookies ---


def cookie_secure(settings: Settings | None = None) -> bool:
    """``AUTH_COOKIE_SECURE`` = auto | true | false; ``auto`` follows ``PUBLIC_URL``.

    Derived from the configured public URL rather than the scheme this process sees:
    behind a TLS-terminating proxy every request arrives as plain http, and trusting that
    would drop the ``Secure`` flag on an https deployment.
    """
    settings = settings or get_settings()
    value = (settings.auth_cookie_secure or "auto").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    return settings.public_url.strip().lower().startswith("https://")


def set_session_cookie(response: Response, token: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.auth_session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings),
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    response.delete_cookie(
        COOKIE_NAME, httponly=True, samesite="lax", secure=cookie_secure(settings), path="/"
    )


# ------------------------------------------------------------------------------ sessions ---


def _is_private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def client_ip(request: HTTPConnection, settings: Settings | None = None) -> str | None:
    """Best guess at the caller's address.

    ``X-Forwarded-For`` is a request header like any other: anybody who can reach this
    process can write whatever they like into it, and the value ends up in ``sessions.ip``
    and in the login rate limiter's key.  So it is believed only when ``TRUSTED_PROXY_HOPS``
    says how many proxies are in front — the default, 0, ignores the header entirely — and
    then only the entry that many hops from the right, which is the address the *last*
    trusted proxy observed and the first one a client cannot forge.
    """
    peer = request.client.host if request.client else None
    hops = max(0, int((settings or get_settings()).trusted_proxy_hops))
    if not hops or not peer or not _is_private(peer):
        return peer
    chain = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",")]
    chain = [p for p in chain if p]
    if len(chain) >= hops:
        return chain[-hops]
    if chain:  # a shorter chain than configured: the leftmost is the best we have
        return chain[0]
    real = (request.headers.get("x-real-ip") or "").strip()
    return real or peer


def create_session(session: Session, user: User, request: Request | None = None) -> str:
    """Insert a session row and return the raw token to put in the cookie.

    The absolute deadline is stamped on the row here and never moved, so a change to
    ``auth_session_days`` applies to the next sign-in rather than retroactively to sessions
    already open — which is the honest reading of "a sign-in lasts N days".  The *idle*
    deadline is the other way round: :func:`resolve_session` re-reads it on every request,
    so shortening it signs idle sessions out at once.
    """
    settings = effective_settings(session)
    raw = new_session_token()
    now = datetime.now(UTC)
    row = UserSession(
        user_id=user.id,
        # The column is named ``token`` but only ever holds its sha256 — the raw value
        # exists in the caller's cookie and nowhere else.
        token=hash_secret(raw),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.auth_session_days),
        ip=client_ip(request) if request is not None else None,
        user_agent=((request.headers.get("user-agent") or "")[:500] or None) if request else None,
    )
    session.add(row)
    user.last_login_at = now
    session.commit()
    return raw


def delete_session(session: Session, raw: str) -> None:
    row = session.scalar(select(UserSession).where(UserSession.token == hash_secret(raw)))
    if row is not None:
        session.delete(row)
        session.commit()


def revoke_user_sessions(session: Session, user_id: int, *, keep: str | None = None) -> int:
    """Delete every session ``user_id`` holds — except the one whose raw token is ``keep``.

    Called whenever a password changes: a session stolen before the change must not
    outlive it.  ``keep`` spares the browser that made the change
    when it is the user's own.  Does not commit, so the caller's password write and this
    delete land together or not at all.  Returns how many sessions were ended.
    """
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep:
        stmt = stmt.where(UserSession.token != hash_secret(keep))
    return int(session.execute(stmt).rowcount or 0)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def resolve_session(session: Session, raw: str) -> UserSession | None:
    """The live session for ``raw``, sliding its idle window; else ``None``.

    An expired, idled-out or revoked session is deleted on sight, which keeps the table
    small and makes "signed out everywhere" the natural result of doing nothing.
    """
    settings = effective_settings(session)
    row = session.scalar(select(UserSession).where(UserSession.token == hash_secret(raw)))
    if row is None:
        return None
    now = datetime.now(UTC)
    expires_at = _aware(row.expires_at)
    last_seen = _aware(row.last_seen_at) or _aware(row.created_at) or now
    idled = last_seen + timedelta(hours=settings.auth_idle_hours) <= now
    if row.revoked_at is not None or (expires_at is not None and expires_at <= now) or idled:
        session.delete(row)
        session.commit()
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if (now - last_seen).total_seconds() > 60:  # do not write on every request
        row.last_seen_at = now
        session.commit()
    return row


def purge_expired_sessions(session: Session) -> int:
    now = datetime.now(UTC)
    idle_cutoff = now - timedelta(hours=effective_settings(session).auth_idle_hours)
    rows = list(
        session.scalars(
            select(UserSession).where(
                (UserSession.expires_at <= now) | (UserSession.last_seen_at <= idle_cutoff)
            )
        )
    )
    for row in rows:
        session.delete(row)
    if rows:
        session.commit()
    return len(rows)


# -------------------------------------------------------------------------- API tokens ---


def create_api_token(
    session: Session, label: str, scopes: list[str], *, created_by: str | None = None
) -> tuple[ApiToken, str]:
    """Mint a token.  Returns the row **and** the raw secret, which is shown exactly once."""
    raw = new_api_token()
    row = ApiToken(
        label=label,
        token_hash=hash_secret(raw),
        token_prefix=raw[: len(TOKEN_PREFIX) + 6],
        scopes=normalize_scopes(scopes) or ["read"],
        created_by=created_by,
    )
    session.add(row)
    session.commit()
    return row, raw


def verify_token(session: Session, raw: str) -> ApiToken | None:
    """The live :class:`~app.models.ApiToken` for ``raw``, or ``None``.

    Also used by the recorder push endpoint (§1.6), which presents the secret in a form
    field rather than a header.  ``last_used_at`` is stamped at most once a minute so a
    busy recorder does not turn every upload into an extra write.
    """
    if not raw or not raw.startswith(ACCEPTED_TOKEN_PREFIXES):
        return None
    row = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_secret(raw)))
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    expires_at = _aware(row.expires_at)
    if expires_at is not None and expires_at <= now:
        return None
    last_used = _aware(row.last_used_at)
    if last_used is None or (now - last_used).total_seconds() > 60:
        row.last_used_at = now
        session.commit()
    return row


def bearer_secret(request: HTTPConnection) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


# ------------------------------------------------------------------------ authentication ---


def authenticate(request: HTTPConnection, session: Session) -> Principal | None:
    """Identify the caller from a bearer token or the session cookie.

    Memoized on ``request.state``: this runs once in :func:`mutation_guard` and again in
    whatever dependency the route declares, and neither the session's sliding write nor
    the token's ``last_used_at`` touch should happen twice for one request.

    A *presented* bearer token that does not resolve is a 401 rather than "anonymous":
    silently ignoring it would turn a typo into a confusing error further down.
    """
    if getattr(request.state, "auth_resolved", False):
        return getattr(request.state, "principal", None)
    request.state.auth_resolved = True
    request.state.principal = None

    secret = bearer_secret(request)
    if secret is not None:
        token = verify_token(session, secret)
        if token is None:
            log.warning("auth.token.rejected prefix=%s", secret[:10])
            raise HTTPException(status_code=401, detail=BAD_TOKEN_DETAIL)
        principal = Principal(kind="token", scopes=effective_scopes(token.scopes), token=token)
        request.state.principal = principal
        return principal

    cookie = request.cookies.get(COOKIE_NAME)
    row = resolve_session(session, cookie) if cookie else None
    if row is None:
        return None
    user = session.get(User, row.user_id)
    if user is None:
        return None
    principal = Principal(kind="session", scopes=effective_scopes(user.scopes), user=user)
    request.state.principal = principal
    return principal


def current_principal(
    request: HTTPConnection, session: Annotated[Session, Depends(get_session)]
) -> Principal | None:
    """FastAPI dependency: the caller's :class:`Principal`, or ``None`` when anonymous."""
    return authenticate(request, session)


OptionalPrincipal = Annotated["Principal | None", Depends(current_principal)]


def current_user_optional(principal: OptionalPrincipal) -> User | None:
    """The signed-in user, or ``None`` — never raises for an anonymous cookie visitor."""
    return principal.user if principal is not None else None


CurrentUser = Annotated["User | None", Depends(current_user_optional)]


def require_principal(principal: OptionalPrincipal) -> Principal:
    if principal is None:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
    return principal


RequiredPrincipal = Annotated[Principal, Depends(require_principal)]


def require_scope(scope: str):  # noqa: ANN201 - returns a FastAPI dependency
    """Dependency factory: 401 when anonymous, 403 when the scope is missing.

    ``admin`` implies every scope and ``write`` implies ``read``, so a route asking for
    ``read`` accepts anybody with a credential at all.
    """

    def dependency(principal: OptionalPrincipal) -> Principal:
        if principal is None:
            raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail=f"This action needs the {scope!r} scope")
        return principal

    return dependency


def require_admin(principal: OptionalPrincipal) -> Principal:
    """401 when anonymous, 403 without the ``admin`` scope."""
    if principal is None:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="This action needs the 'admin' scope")
    return principal


AdminPrincipal = Annotated[Principal, Depends(require_admin)]
WritePrincipal = Annotated[Principal, Depends(require_scope("write"))]


# ---------------------------------------------------------------------------------- CSRF ---


def host_of(value: str | None) -> str | None:
    """The host part of a URL or an ``Origin`` header, lower-cased."""
    if not value:
        return None
    parts = urlsplit(value)
    return (parts.netloc or parts.path).lower() or None


def allowed_hosts(request: HTTPConnection) -> set[str]:
    """Hosts a state-changing request may claim to come from."""
    hosts = {h for h in (host_of(get_settings().public_url),) if h}
    for header in ("host", "x-forwarded-host"):
        value = request.headers.get(header)
        if value:
            hosts.add(value.split(",")[0].strip().lower())
    if request.url.netloc:
        hosts.add(request.url.netloc.lower())
    return {h for h in hosts if h}


def check_csrf(request: HTTPConnection) -> None:
    """Reject a cookie-authenticated mutation a third-party page could have made.

    Two independent proofs: the custom header ``X-Requested-With: keyup`` (a plain
    form or ``<img>`` cannot set it) and an ``Origin``/``Referer`` naming this site.
    Command-line clients simply send both.
    """
    if request.headers.get(CSRF_HEADER, "").strip().lower() != CSRF_HEADER_VALUE:
        raise HTTPException(
            status_code=403,
            detail=f"Missing {CSRF_HEADER}: {CSRF_HEADER_VALUE} header on a state-changing request",
        )
    source = host_of(request.headers.get("origin") or request.headers.get("referer"))
    if source is None:
        raise HTTPException(
            status_code=403, detail="Missing Origin/Referer header on a state-changing request"
        )
    if source not in allowed_hosts(request):
        raise HTTPException(status_code=403, detail=f"Cross-origin request from {source!r} rejected")


# ------------------------------------------------------------------------- central guard ---


def mutation_guard(
    request: HTTPConnection, session: Annotated[Session, Depends(get_session)]
) -> None:
    """Installed on the FastAPI app itself, so it covers every route by default.

    Safe methods pass straight through while ``AUTH_REQUIRE_READ`` is false — the archive
    stays readable without an account.  Turn it on and *everything* needs a credential.
    Anything that writes needs a credential plus, for a cookie, a CSRF proof.

    Typed as :class:`HTTPConnection` rather than ``Request`` because an application-level
    dependency also runs for WebSocket routes, which have no method to check; the live feed
    is a read and is handled by the ``AUTH_REQUIRE_READ`` branch below.
    """
    settings = effective_settings(session)
    path = request.scope.get("path", request.url.path)
    if request.scope.get("type") != "http":
        # A WebSocket has no method; the live feed is a read, so it follows the read rule.
        # It needs the *scope* and not merely a credential (M1): an ``upload`` token is
        # the recorder's, and the recorder has no business tailing the archive.
        if path in WS_SELF_AUTH_PATHS:
            return
        if is_admin_path(path):
            require_admin_read(request, session)
            return
        if settings.auth_require_read:
            principal = authenticate(request, session)
            if principal is None:
                raise HTTPException(status_code=401, detail="Sign in to read this archive")
            if "read" not in principal.scopes:
                raise HTTPException(status_code=403, detail="This feed needs the 'read' scope")
        return
    if request.method.upper() in SAFE_METHODS:
        # Admin reads are closed to guests even while the archive is open (see
        # ADMIN_PATH_PREFIX); nothing under /api/admin is on any of the open lists below.
        if is_admin_path(path):
            require_admin_read(request, session)
            return
        if not settings.auth_require_read:
            return
        # Reading is closed: /api/docs and the sign-in routes still have to answer.
        # ``/api/healthz`` joins ``/api/status`` here for the same reason: a container
        # health check has no account, and a deployment that closes reading must not
        # thereby report itself unhealthy.
        if path in OPEN_MUTATION_PATHS or path.startswith(
            ("/api/docs", "/api/openapi", "/api/status", "/api/healthz")
        ):
            return
        # A syndication feed carries its credential in the URL and is fetched by software
        # that cannot sign in; the route validates the token itself.
        if feed_self_authenticating(request):
            return
        # The recorder's reads check their own ``upload`` token (see UPLOAD_READ_PATHS).
        if path in UPLOAD_READ_PATHS:
            return
        principal = authenticate(request, session)
        if principal is None:
            raise HTTPException(status_code=401, detail="Sign in to read this archive")
        # The scope, not merely a credential — exactly as the socket branch above: an
        # ``upload`` token is a recorder's, stored on a field box, and must not read the
        # archive (or the built-in preset feeds) just because reads are closed.
        if "read" not in principal.scopes:
            raise HTTPException(status_code=403, detail="Reading this archive needs the 'read' scope")
        return

    principal = authenticate(request, session)
    cookie_or_anonymous = principal is None or principal.kind != "token"
    if cookie_or_anonymous and path not in NO_CSRF_PATHS:
        check_csrf(request)
    if path in OPEN_MUTATION_PATHS:
        return
    if principal is None:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
    if "write" not in principal.scopes:
        raise HTTPException(status_code=403, detail="This action needs the 'write' scope")


def user_count(session: Session) -> int:
    from sqlalchemy import func  # noqa: PLC0415 - local, keeps the import list short

    return int(session.scalar(select(func.count()).select_from(User)) or 0)


__all__ = [
    "ACCEPTED_TOKEN_PREFIXES",
    "ADMIN_PATH_PREFIX",
    "COOKIE_NAME",
    "CSRF_HEADER",
    "CSRF_HEADER_VALUE",
    "SCOPES",
    "TOKEN_PREFIX",
    "UPLOAD_READ_PATHS",
    "WS_SELF_AUTH_PATHS",
    "AdminPrincipal",
    "CurrentUser",
    "OptionalPrincipal",
    "Principal",
    "RequiredPrincipal",
    "WritePrincipal",
    "allowed_hosts",
    "authenticate",
    "clear_session_cookie",
    "create_api_token",
    "create_session",
    "current_user_optional",
    "delete_session",
    "feed_self_authenticating",
    "hash_password",
    "host_of",
    "is_admin_path",
    "mutation_guard",
    "normalize_scopes",
    "require_admin",
    "require_scope",
    "revoke_user_sessions",
    "set_session_cookie",
    "verify_password",
    "verify_password_or_dummy",
    "verify_token",
]
