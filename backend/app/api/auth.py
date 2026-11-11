"""Sign in, sign out, and "who am I" (§4.7).

Reading this archive needs no account.  A session is only needed to *change* something —
edit a talkgroup, correct a transcript, run an admin action — so these three routes are the
whole surface.  ``POST /auth/login`` and ``POST /auth/logout`` skip the credential check
(they are how a credential is obtained) but not the CSRF check, so a third-party page still
cannot sign somebody out.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    COOKIE_NAME,
    OptionalPrincipal,
    clear_session_cookie,
    client_ip,
    create_session,
    delete_session,
    effective_scopes,
    hash_password,
    needs_rehash,
    set_session_cookie,
    verify_password_or_dummy,
)
from app.config import get_settings
from app.db import get_session
from app.models import User
from app.runtime_settings import effective_settings
from app.schemas import LoginRequest, MeResponse, UserOut

log = logging.getLogger("app.api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])
DbSession = Annotated[Session, Depends(get_session)]

#: In-memory sliding window: five failures per username and per address in fifteen minutes.
#: The API image runs one uvicorn worker, so process memory is a complete view; a fan-out
#: deployment would move this into a table without changing the interface.
_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_LIMIT = 5
_WINDOW_S = 15 * 60

#: The dictionary is keyed by whatever username was *attempted*, so it grows with the
#: attacker's imagination.  Once it is this big the entries whose window has expired are
#: dropped, and if that is not enough the oldest are (L1): a full table would otherwise be
#: a slow memory leak reachable without any credential at all.
_MAX_TRACKED = 10_000


def _prune(now: float) -> None:
    """Forget expired windows, and the oldest keys when there are still too many."""
    if len(_FAILURES) <= _MAX_TRACKED:
        return
    cutoff = now - _WINDOW_S
    for key, hits in list(_FAILURES.items()):
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            del _FAILURES[key]
    while len(_FAILURES) > _MAX_TRACKED:
        oldest = min(_FAILURES, key=lambda k: _FAILURES[k][0])
        del _FAILURES[oldest]


def _retry_after(keys: list[str]) -> int:
    now = time.monotonic()
    wait = 0
    for key in keys:
        hits = _FAILURES[key]
        while hits and hits[0] <= now - _WINDOW_S:
            hits.popleft()
        if len(hits) >= _LIMIT:
            wait = max(wait, max(1, int(hits[0] + _WINDOW_S - now) + 1))
    return wait


def _record_failure(keys: list[str]) -> None:
    now = time.monotonic()
    _prune(now)
    for key in keys:
        _FAILURES[key].append(now)


def _reset(keys: list[str]) -> None:
    for key in keys:
        _FAILURES.pop(key, None)


def reset_throttle() -> None:
    """Tests clear the window between cases."""
    _FAILURES.clear()


def user_out(user: User) -> UserOut:
    scopes = list(user.scopes or ())
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        scopes=scopes,
        is_admin="admin" in scopes,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post(
    "/login",
    response_model=MeResponse,
    summary="Sign in",
    description=(
        "Exchanges a username and password for the `keyup_session` cookie (HttpOnly, "
        "SameSite=Lax). The cookie alone is not enough to change data: every mutation must also "
        "carry `X-Requested-With: keyup` and a same-site `Origin`. Five failures per username "
        "or address in fifteen minutes are answered with 429."
    ),
    responses={401: {"description": "Wrong credentials"}, 429: {"description": "Too many attempts"}},
)
def login(
    body: LoginRequest, request: Request, response: Response, db: DbSession
) -> MeResponse:
    username = body.username.strip().lower()
    keys = [f"user:{username}"]
    address = client_ip(request)
    if address:
        keys.append(f"ip:{address}")
    wait = _retry_after(keys)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed sign-in attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )

    from sqlalchemy import func, select  # noqa: PLC0415 - local, tiny

    user = db.scalar(select(User).where(func.lower(User.username) == username))
    # argon2 runs whether or not the username exists (or is active): an unknown user
    # answering faster than a wrong password would enumerate the accounts.
    password_ok = verify_password_or_dummy(user.password_hash if user else None, body.password)
    if user is None or not user.is_active or not password_ok:
        _record_failure(keys)
        log.warning("auth.login.failed user=%s ip=%s", username, address)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    _reset(keys)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    # The session this browser held until now is being replaced by the cookie below; left
    # in the table it would stay valid for whoever else has a copy of it.
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        delete_session(db, previous)
    token = create_session(db, user, request)
    # The effective settings, not the environment's: Max-Age on the cookie has to agree with
    # the expires_at create_session just stamped on the row, and that one is read through the
    # settings table (group "Access", auth_session_days).  A cookie outliving its row is a
    # browser that believes it is signed in until the next request says 401.
    set_session_cookie(response, token, effective_settings(db))
    log.info("auth.login.ok user=%s", user.username)
    return MeResponse(
        user=user_out(user),
        scopes=sorted(effective_scopes(user.scopes)),
        auth_kind="session",
        auth_required_read=get_settings().auth_require_read,
    )


@router.post(
    "/logout",
    response_model=MeResponse,
    summary="Sign out",
    description="Deletes the session row and clears the cookie. Safe to call when already signed out.",
)
def logout(request: Request, response: Response, db: DbSession) -> MeResponse:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        delete_session(db, cookie)
    clear_session_cookie(response)
    return MeResponse(
        user=None, scopes=[], auth_kind=None, auth_required_read=get_settings().auth_require_read
    )


@router.get(
    "/me",
    response_model=MeResponse,
    summary="The current session",
    description=(
        "Answers 200 for anonymous callers too, with `user: null` — the SPA uses it to decide "
        "whether to show the editing controls. `auth_required_read` reports whether reading this "
        "deployment needs an account at all."
    ),
)
def me(principal: OptionalPrincipal) -> MeResponse:
    settings = get_settings()
    if principal is None:
        return MeResponse(
            user=None, scopes=[], auth_kind=None, auth_required_read=settings.auth_require_read
        )
    return MeResponse(
        user=user_out(principal.user) if principal.user is not None else None,
        scopes=sorted(principal.scopes),
        auth_kind=principal.kind,
        auth_required_read=settings.auth_require_read,
    )


__all__ = ["reset_throttle", "router", "user_out"]
