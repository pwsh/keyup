"""Every read under ``/api/admin`` needs a sign-in, and ``/api/status`` hides operator data.

The admin reads carry the operator's settings (the ACME e-mail, the webhook URL, the
Broadcastify owner), file paths, job logs and export state.  They are closed in the
app-level guard (:func:`app.auth.mutation_guard`) rather than route by route, and the guard
test below walks the *application's* routes, so an admin GET added later is held to the
same rule without anyone adding it to a list here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def _calls(dependant):  # noqa: ANN001, ANN202
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def admin_get_routes() -> list[tuple[str, bool]]:
    """``(path, needs_admin)`` for every GET the app serves under ``/api/admin``.

    Walks the routers as mounted (``app.routes`` holds one included-router entry per
    ``include_router`` call), so nothing has to be listed by hand.
    """
    from app.auth import is_admin_path, require_admin
    from app.main import app

    found: list[tuple[str, bool]] = []
    for entry in app.routes:
        routes = [(entry, "")]
        original = getattr(entry, "original_router", None)
        if original is not None:
            routes = [(route, entry.include_context.prefix) for route in original.routes]
        for route, prefix in routes:
            if not isinstance(route, APIRoute) or "GET" not in route.methods:
                continue
            path = prefix + route.path
            if is_admin_path(path):
                found.append((path, require_admin in set(_calls(route.dependant))))
    return sorted(set(found))


def concrete(path: str) -> str:
    """A template path with every ``{param}`` filled in; the guard answers before routing."""
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


ADMIN_GETS = admin_get_routes()


def test_the_walk_finds_the_admin_reads() -> None:
    paths = {path for path, _ in ADMIN_GETS}
    assert len(paths) >= 25
    assert {"/api/admin/settings", "/api/admin/webhooks", "/api/admin/exports"} <= paths


@pytest.mark.parametrize(("path", "needs_admin"), ADMIN_GETS, ids=[p for p, _ in ADMIN_GETS])
def test_every_admin_get_is_closed_to_guests(client, read_token, path, needs_admin) -> None:  # noqa: ANN001
    url = concrete(path)
    guest = client.get(url)
    assert guest.status_code == 401, f"{path} answered a guest with {guest.status_code}"
    signed_in = client.get(url, headers=bearer(read_token))
    if needs_admin:
        # The route's own, stricter dependency still applies on top of the guard.
        assert signed_in.status_code == 403, f"{path}: {signed_in.status_code}"
    else:
        assert signed_in.status_code == 200, f"{path}: {signed_in.status_code} {signed_in.text[:200]}"


def test_an_upload_token_cannot_read_the_admin_pages(client, session) -> None:  # noqa: ANN001
    """A recorder's token lives on a field box and holds no ``read``."""
    from app.auth import create_api_token

    _, upload = create_api_token(session, "tests-upload", ["upload"], created_by="tests")
    assert client.get("/api/admin/settings", headers=bearer(upload)).status_code == 403


def test_a_signed_in_non_admin_reads_the_admin_page(client, api_user, user_password) -> None:  # noqa: ANN001
    """The SPA's ``/admin`` route needs a sign-in, not the admin scope — so does the API."""
    login = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    assert client.get("/api/admin/settings").status_code == 200
    assert client.get("/api/admin/users").status_code == 403  # admin-only stays admin-only


def test_metrics_need_a_read_credential(client, read_token) -> None:  # noqa: ANN001
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics", headers=bearer(read_token)).status_code == 200


def test_status_hides_operator_fields_from_guests(client, session, read_token) -> None:  # noqa: ANN001
    from app.models import HealthCondition

    now = datetime.now(UTC)
    session.add_all(
        [
            HealthCondition(
                key="backup_stale",
                check="backup_stale",
                active=True,
                raised_at=now,
                checked_at=now,
                message="no database backup in /srv/keyup/backups yet",
            ),
            HealthCondition(
                key="recorder_silent:signal",
                check="recorder_silent",
                active=True,
                raised_at=now,
                checked_at=now,
                value=12.0,
                message="recorder signal has not reported for 12 min (threshold 5 min)",
            ),
        ]
    )
    session.commit()

    full = client.get("/api/status", headers=bearer(read_token)).json()
    guest = client.get("/api/status").json()

    # Signed in: everything, as before.
    assert full["audio_root"]
    assert full["version"]
    assert full["ingest"] is not None
    assert full["ingest"]["audio_root"] == full["audio_root"]
    assert all(v["path"].startswith("/") for v in full["disk_volumes"])
    assert {h["check"]: h["message"] for h in full["health"]}["backup_stale"].startswith("no database")

    # A guest: same shape, the operator-only fields blanked.
    assert guest["audio_root"] == ""
    assert guest["version"] == ""
    assert guest["uptime_s"] == 0
    assert guest["ingest"] is None
    assert guest["database"] == {"ok": True, "detail": None}
    assert full["audio_root"] not in repr(guest)
    for volume in guest["disk_volumes"]:
        assert not volume["path"].startswith("/")
    if guest["disk"] is not None:
        assert guest["disk"]["path"] == "audio storage"
    messages = {h["check"]: h["message"] for h in guest["health"]}
    assert messages["backup_stale"] is None
    assert messages["recorder_silent"].startswith("recorder signal")  # the public banner keeps it

    # What the public pages read is still there.
    for key in ("status", "ok", "db_ok", "tz", "queue_depth"):
        assert guest[key] == full[key], key
    assert guest["counts"].keys() == full["counts"].keys()
    assert guest["queue"].keys() == full["queue"].keys()
    assert [v["level"] for v in guest["disk_volumes"]] == [v["level"] for v in full["disk_volumes"]]
