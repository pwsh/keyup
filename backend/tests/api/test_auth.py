"""Authentication, CSRF and scopes (§4.7).

The important test in this file is :func:`test_every_mutating_route_is_guarded`: it walks
the application's own route table, so a route added later without thinking about auth fails
the suite rather than shipping open.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

SAFE = {"GET", "HEAD", "OPTIONS"}


def _api_routes(app):  # noqa: ANN001, ANN202
    """Every APIRoute of the app, including the ones inside included routers."""
    seen: list[APIRoute] = []

    def walk(routes) -> None:  # noqa: ANN001
        for route in routes:
            if isinstance(route, APIRoute):
                seen.append(route)
            inner = getattr(route, "routes", None)
            if inner:
                walk(inner)
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)

    walk(app.routes)
    return seen


def test_every_mutating_route_refuses_an_anonymous_caller(client, archive) -> None:
    """Walk the app's own route table and prove nothing that writes is open.

    The application-level guard runs before path and body validation, so a bare request
    with the CSRF headers and no credential must answer 401 for every mutating route —
    a 2xx here would mean a route shipped without auth.
    """
    from app.auth import OPEN_MUTATION_PATHS
    from app.main import app

    checked = 0
    for route in _api_routes(app):
        # Routes reached through an included router carry their own path, without the
        # /api prefix the application mounts them under.
        full = route.path if route.path.startswith("/api") else f"/api{route.path}"
        for method in sorted(set(route.methods or ()) - SAFE):
            if full in OPEN_MUTATION_PATHS:
                continue
            path = full.format_map({name: "1" for name in _path_params(full)})
            response = client.request(method, path, json={}, headers=CSRF_HEADERS)
            assert response.status_code == 401, f"{method} {full} answered {response.status_code}"
            checked += 1
    assert checked > 20  # the suite would pass vacuously if the walk found nothing


def _path_params(path: str) -> list[str]:
    import re

    return re.findall(r"{([^}]+)}", path)


def test_the_guard_is_installed_on_the_application() -> None:
    from app.auth import mutation_guard
    from app.main import app

    assert mutation_guard in [d.dependency for d in app.router.dependencies]


def test_reads_are_open(client, archive) -> None:
    assert client.get("/api/calls").status_code == 200
    assert client.get("/api/auth/me").json() == {
        "user": None,
        "scopes": [],
        "auth_kind": None,
        "auth_required_read": False,
    }


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/calls/{call}", {"starred": True}),
        ("post", "/api/calls/{call}/transcribe", {}),
        ("post", "/api/links", {"call_id": 1, "external_id": "x"}),
        ("post", "/api/alerts", {"name": "n", "kind": "keyword"}),
    ],
)
def test_mutations_without_a_credential_are_refused(client, archive, method, path, body) -> None:
    target = path.format(call=archive.call_ids[0])
    # No CSRF header at all: 403, because a cross-site page could have sent this.
    assert getattr(client, method)(target, json=body).status_code == 403
    # CSRF proof but no credential: 401.
    response = getattr(client, method)(target, json=body, headers=CSRF_HEADERS)
    assert response.status_code == 401
    assert response.json()["detail"] == "Sign in to change data"


def test_a_bearer_token_needs_no_csrf_headers(client, archive, write_token) -> None:
    response = client.patch(
        f"/api/calls/{archive.call_ids[0]}", json={"starred": True}, headers=bearer(write_token)
    )
    assert response.status_code == 200


def test_a_read_only_token_cannot_write(client, archive, read_token) -> None:
    response = client.patch(
        f"/api/calls/{archive.call_ids[0]}", json={"starred": True}, headers=bearer(read_token)
    )
    assert response.status_code == 403
    assert "write" in response.json()["detail"]


def test_admin_routes_need_the_admin_scope(client, archive, write_token, admin_token) -> None:
    assert client.get("/api/admin/tokens", headers=bearer(write_token)).status_code == 403
    assert client.get("/api/admin/tokens", headers=bearer(admin_token)).status_code == 200
    assert client.post("/api/admin/ingest/rescan", headers=bearer(write_token)).status_code == 403


def test_admin_implies_every_other_scope(client, archive, admin_token) -> None:
    me = client.get("/api/auth/me", headers=bearer(admin_token)).json()
    assert set(me["scopes"]) == {"read", "write", "admin", "stream", "upload"}
    assert me["auth_kind"] == "token"


def test_a_presented_but_invalid_token_is_a_401_not_anonymous(client, archive) -> None:
    response = client.get("/api/auth/me", headers=bearer("kup_not-a-token"))
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired API token"


def test_a_revoked_token_stops_working(client, archive, session, write_token) -> None:
    from datetime import UTC, datetime

    from app.auth import hash_secret
    from app.models import ApiToken

    row = session.query(ApiToken).filter(ApiToken.token_hash == hash_secret(write_token)).one()
    row.revoked_at = datetime.now(UTC)
    session.commit()
    assert client.get("/api/auth/me", headers=bearer(write_token)).status_code == 401


def test_only_the_hash_of_a_token_is_stored(session) -> None:
    from app.auth import create_api_token, hash_secret

    row, raw = create_api_token(session, "storage", ["read"])
    assert raw.startswith("kup_")
    assert len(raw) == 44  # 'kup_' + 40 url-safe characters
    assert row.token_hash == hash_secret(raw)
    assert raw not in row.token_hash


def test_login_sets_a_session_cookie_and_logout_clears_it(client, api_user, user_password) -> None:
    from app.auth import COOKIE_NAME

    bad = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": "wrong"},
        headers=CSRF_HEADERS,
    )
    assert bad.status_code == 401

    good = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert good.status_code == 200
    assert good.json()["user"]["username"] == "tester"
    assert good.json()["auth_kind"] == "session"
    assert COOKIE_NAME in client.cookies

    me = client.get("/api/auth/me").json()
    assert me["user"]["username"] == "tester"
    assert me["user"]["is_admin"] is False

    client.post("/api/auth/logout", headers=CSRF_HEADERS)
    assert client.get("/api/auth/me").json()["user"] is None


def test_login_is_case_insensitive_on_the_username(client, api_user, user_password) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "TESTER", "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200


def test_a_cross_origin_mutation_is_rejected(client, archive, api_user, user_password) -> None:
    client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    response = client.patch(
        f"/api/calls/{archive.call_ids[0]}",
        json={"starred": True},
        headers={"X-Requested-With": "keyup", "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "evil.example" in response.json()["detail"]


def test_login_throttles_repeated_failures(client, api_user) -> None:
    from app.api.auth import reset_throttle

    reset_throttle()
    try:
        codes = [
            client.post(
                "/api/auth/login",
                json={"username": api_user.username, "password": "nope"},
                headers=CSRF_HEADERS,
            ).status_code
            for _ in range(7)
        ]
        assert codes[:5] == [401] * 5
        assert 429 in codes
    finally:
        reset_throttle()


def test_password_hashes_are_argon2id_and_verify(session) -> None:
    from app.auth import hash_password, verify_password

    digest = hash_password("s3cret")
    assert digest.startswith("$argon2id$")
    assert verify_password(digest, "s3cret")
    assert not verify_password(digest, "s3cre")


# ----------------------------------------------------------- closed reads need `read` ---


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    """A recorder's token: ``upload`` only, stored on a field box."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


@pytest.fixture
def closed_reads(client, admin_token):  # noqa: ANN001, ANN201
    client.put(
        "/api/admin/settings", json={"auth_require_read": "true"}, headers=bearer(admin_token)
    ).raise_for_status()
    try:
        yield
    finally:
        client.put("/api/admin/settings", json={"auth_require_read": ""}, headers=bearer(admin_token))


def test_closed_reads_need_the_read_scope_not_just_a_credential(client, archive, closed_reads, upload_token, read_token) -> None:  # noqa: ANN001, ARG001, E501
    """With ``AUTH_REQUIRE_READ`` on, an ``upload`` token used to read everything — the
    archive and the built-in preset feeds — because a GET only asked for *a* credential.
    The socket branch already asked for the scope."""
    for path in ("/api/calls", f"/api/calls/{archive.call_ids[0]}", "/api/feeds/preset/all.rss", "/api/talkgroups"):
        refused = client.get(path, headers=bearer(upload_token))
        assert refused.status_code == 403, path
        assert "read" in refused.json()["detail"]
        assert client.get(path, headers=bearer(read_token)).status_code == 200, path
        assert client.get(path).status_code == 401, path


def test_the_recorders_own_reads_survive_closed_reads(client, archive, closed_reads, upload_token, read_token) -> None:  # noqa: ANN001, ARG001, E501
    """Every GET a recorder or the migration tool makes with its ``upload`` token: trunkd's
    desired-state poll (Bearer) and migrate_sdrtrunk's flow gauge (``?key=``). Each route
    checks the token itself, so the exemption opens nothing to anybody else."""
    desired = client.get(
        "/api/recorder/desired-state", params={"instance": "rec-1"}, headers=bearer(upload_token)
    )
    assert desired.status_code == 200, desired.text
    assert client.get("/api/upload/flow", params={"key": upload_token}).status_code == 200
    assert client.get("/api/upload/flow", headers=bearer(upload_token)).status_code == 200

    # ...and they still refuse everyone without an upload token.
    assert client.get("/api/recorder/desired-state", params={"instance": "rec-1"}).status_code == 401
    assert client.get(
        "/api/recorder/desired-state", params={"instance": "rec-1"}, headers=bearer(read_token)
    ).status_code == 403
    assert client.get("/api/upload/flow").status_code == 401
    assert client.get("/api/upload/flow", params={"key": read_token}).status_code == 401


# ------------------------------------------------------------------------- sessions ---


def _login(client, username: str, password: str) -> str:  # noqa: ANN001
    """Sign in on a fresh cookie jar and return the raw session token it was given."""
    from app.auth import COOKIE_NAME

    client.cookies.clear()
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}, headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    return client.cookies[COOKIE_NAME]


def _live_sessions(session, user_id: int) -> int:  # noqa: ANN001
    from sqlalchemy import func, select

    from app.models import UserSession

    session.expire_all()
    return int(
        session.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id))
    )


def _signed_in_as(client, raw: str) -> str | None:  # noqa: ANN001
    from app.auth import COOKIE_NAME

    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, raw)
    user = client.get("/api/auth/me").json()["user"]
    client.cookies.clear()
    return user["username"] if user else None


@pytest.fixture
def admin_user(session, user_password):  # noqa: ANN001, ANN201
    from app.auth import hash_password
    from app.models import User

    user = User(username="boss", password_hash=hash_password(user_password), scopes=["admin"])
    session.add(user)
    session.commit()
    return user


def test_an_admin_password_reset_ends_every_session_the_user_holds(client, session, api_user, admin_user, user_password, admin_token) -> None:  # noqa: ANN001, E501
    """A stolen session must not survive the reset meant to lock the thief out (X2)."""
    first = _login(client, api_user.username, user_password)
    second = _login(client, api_user.username, user_password)
    bystander = _login(client, admin_user.username, user_password)
    assert _live_sessions(session, api_user.id) == 2

    client.cookies.clear()
    response = client.patch(
        f"/api/admin/users/{api_user.id}", json={"password": "a-new-one"}, headers=bearer(admin_token)
    )
    assert response.status_code == 200, response.text
    assert _live_sessions(session, api_user.id) == 0
    assert _signed_in_as(client, first) is None
    assert _signed_in_as(client, second) is None
    assert _signed_in_as(client, bystander) == "boss"  # somebody else's sessions are untouched

    # An edit that does not set a password signs nobody out.
    third = _login(client, api_user.username, "a-new-one")
    client.cookies.clear()
    client.patch(
        f"/api/admin/users/{api_user.id}", json={"display_name": "T"}, headers=bearer(admin_token)
    ).raise_for_status()
    assert _signed_in_as(client, third) == "tester"


def test_an_admin_changing_their_own_password_keeps_the_browser_that_did_it(client, session, admin_user, user_password) -> None:  # noqa: ANN001, E501
    elsewhere = _login(client, admin_user.username, user_password)
    here = _login(client, admin_user.username, user_password)  # the jar now holds it
    response = client.patch(
        f"/api/admin/users/{admin_user.id}", json={"password": "rotated"}, headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    assert _live_sessions(session, admin_user.id) == 1
    assert _signed_in_as(client, here) == "boss"
    assert _signed_in_as(client, elsewhere) is None


def test_the_cli_password_command_ends_the_users_sessions(client, session, api_user, user_password) -> None:  # noqa: ANN001
    import json

    from typer.testing import CliRunner

    from app.cli import app

    raw = _login(client, api_user.username, user_password)
    result = CliRunner().invoke(app, ["user", "passwd", api_user.username, "--password", "from-cli"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sessions_ended"] == 1
    assert _live_sessions(session, api_user.id) == 0
    assert _signed_in_as(client, raw) is None
    assert _login(client, api_user.username, "from-cli")


def test_signing_in_again_retires_the_session_the_browser_held(client, session, api_user, user_password) -> None:  # noqa: ANN001
    """The cookie being replaced names a live session; left in the table it stays valid for
    anybody holding a copy of it."""
    from app.auth import COOKIE_NAME

    old = _login(client, api_user.username, user_password)  # the jar now holds it
    again = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 200
    new = client.cookies[COOKIE_NAME]
    assert new != old
    assert _live_sessions(session, api_user.id) == 1
    assert _signed_in_as(client, old) is None
    assert _signed_in_as(client, new) == "tester"


def test_an_unknown_username_costs_the_same_argon2_work(client, session, api_user, monkeypatch) -> None:  # noqa: ANN001
    """Skipping the hash for a username that does not exist answers faster than a wrong
    password, which enumerates the accounts (X5). Every refusal verifies exactly once."""
    from app import auth
    from app.api.auth import reset_throttle

    calls: list[str] = []
    real = auth.verify_password

    def counting(password_hash: str, password: str) -> bool:
        calls.append(password_hash[:9])
        return real(password_hash, password)

    monkeypatch.setattr(auth, "verify_password", counting)
    reset_throttle()
    try:
        api_user.is_active = False
        session.commit()
        for username in ("nobody-here", api_user.username):
            calls.clear()
            response = client.post(
                "/api/auth/login", json={"username": username, "password": "x"}, headers=CSRF_HEADERS
            )
            assert response.status_code == 401
            assert calls == ["$argon2id"], username
    finally:
        reset_throttle()


# -------------------------------------------------------------------- the client IP ---


def _request(peer: str, forwarded_for: str | None):  # noqa: ANN202
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", forwarded_for.encode())] if forwarded_for else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 5000)})


def _hops(hops: int):  # noqa: ANN202
    from app.config import get_settings

    return get_settings().model_copy(update={"trusted_proxy_hops": hops})


def test_two_trusted_hops_find_the_browser_behind_edge_and_web() -> None:
    """Production: browser -> edge (Caddy, 172.18.0.5) -> web (nginx, 172.18.0.4) -> api.
    Caddy sets X-Forwarded-For to the address it saw (dropping whatever the client sent),
    nginx appends Caddy's, so the API holds ``<client>, <edge>`` from peer ``<web>``."""
    from app.auth import client_ip

    web, edge = "172.18.0.4", "172.18.0.5"
    assert client_ip(_request(web, f"198.51.100.205, {edge}"), _hops(2)) == "198.51.100.205"
    # The recorders push through the same edge, and resolve the same way.
    assert client_ip(_request(web, f"192.0.2.30, {edge}"), _hops(2)) == "192.0.2.30"
    assert client_ip(_request(web, f"198.51.100.78, {edge}"), _hops(2)) == "198.51.100.78"
    # A public address two hops out is believed just the same.
    assert client_ip(_request(web, f"203.0.113.9, {edge}"), _hops(2)) == "203.0.113.9"
    # uvicorn runs with --forwarded-allow-ips '*', so it may already have put the leftmost
    # entry in as the peer; the answer does not change.
    assert client_ip(_request("198.51.100.205", f"198.51.100.205, {edge}"), _hops(2)) == "198.51.100.205"
    # 0 ignores the header and 1 would name the edge: the two wrong answers.
    assert client_ip(_request(web, f"198.51.100.205, {edge}"), _hops(0)) == web
    assert client_ip(_request(web, f"198.51.100.205, {edge}"), _hops(1)) == edge
    # Something reaching the API with no proxy in between is its own peer.
    assert client_ip(_request("192.0.2.30", None), _hops(2)) == "192.0.2.30"


def test_the_login_limit_is_per_client_behind_the_proxies(api_user, monkeypatch) -> None:  # noqa: ANN001
    """With the peer always nginx, five bad sign-ins from anybody locked *everybody* out
    for fifteen minutes (X1). Keyed on the real client, one prober locks out only itself."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.api.auth import reset_throttle
    from app.main import app

    monkeypatch.setattr(auth, "get_settings", lambda: _hops(2))
    behind_proxies = TestClient(app, client=("172.18.0.4", 50000))

    def attempt(n: int, client_address: str) -> int:
        return behind_proxies.post(
            "/api/auth/login",
            json={"username": f"guess-{n}", "password": "x"},
            headers={**CSRF_HEADERS, "X-Forwarded-For": f"{client_address}, 172.18.0.5"},
        ).status_code

    reset_throttle()
    try:
        assert [attempt(n, "198.51.100.99") for n in range(5)] == [401] * 5
        assert attempt(5, "198.51.100.99") == 429  # the prober is stopped...
        assert attempt(6, "198.51.100.205") == 401  # ...and nobody else is
    finally:
        reset_throttle()
