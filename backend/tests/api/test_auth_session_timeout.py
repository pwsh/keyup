"""The session deadlines are runtime settings, and both halves are actually read.

Two deadlines, and they behave differently on purpose:

* ``auth_session_days`` is **absolute** and stamped on the row when the session is created,
  so a change applies to the next sign-in.  The cookie's ``Max-Age`` has to agree with it.
* ``auth_idle_hours`` is **sliding** and re-read on every request, so shortening it signs
  idle sessions out at once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Setting, User, UserSession
from app.runtime_settings import invalidate
from tests.api.conftest import CSRF_HEADERS, bearer


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    invalidate()
    yield
    invalidate()


def _set(session: Session, **values: object) -> None:
    for key, value in values.items():
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value, updated_by="tests"))
        else:
            row.value = value
    session.commit()
    invalidate()


def _login(client: TestClient, api_user: User, password: str):  # noqa: ANN202
    return client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": password},
        headers=CSRF_HEADERS,
    )


def test_the_default_absolute_deadline_is_fourteen_days(
    client: TestClient, session: Session, api_user: User, user_password: str
) -> None:
    assert _login(client, api_user, user_password).status_code == 200
    row = session.scalars(select(UserSession)).one()
    life = row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC)
    assert 13.9 < life.total_seconds() / 86400 < 14.1


def test_the_setting_changes_how_long_a_new_sign_in_lasts(
    client: TestClient, session: Session, api_user: User, user_password: str
) -> None:
    _set(session, auth_session_days=1)
    assert _login(client, api_user, user_password).status_code == 200
    row = session.scalars(select(UserSession)).one()
    life = row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC)
    assert 0.9 < life.total_seconds() / 86400 < 1.1


def test_the_cookie_max_age_agrees_with_the_row(
    client: TestClient, session: Session, api_user: User, user_password: str
) -> None:
    """A cookie outliving its row is a browser that thinks it is signed in until a 401."""
    _set(session, auth_session_days=3)
    response = _login(client, api_user, user_password)
    assert "Max-Age=259200" in response.headers["set-cookie"]


def test_the_idle_window_is_re_read_on_every_request(
    client: TestClient, session: Session, api_user: User, user_password: str
) -> None:
    """Shortening it must sign an idle session out now, not on its next sign-in."""
    assert _login(client, api_user, user_password).status_code == 200

    def age(hours: int) -> None:
        session.expire_all()  # the app's own session has been sliding this row forward
        row = session.scalars(select(UserSession)).one()
        row.last_seen_at = datetime.now(UTC) - timedelta(hours=hours)
        session.commit()

    age(3)
    _set(session, auth_idle_hours=12)
    assert client.get("/api/auth/me").json()["user"] is not None

    age(3)
    _set(session, auth_idle_hours=1)
    assert client.get("/api/auth/me").json()["user"] is None
    assert session.scalars(select(UserSession)).all() == []


@pytest.mark.parametrize(
    ("key", "value"),
    [("auth_session_days", "0"), ("auth_session_days", "366"), ("auth_idle_hours", "0")],
)
def test_an_out_of_range_deadline_is_refused_at_the_settings_endpoint(
    client: TestClient, admin_token: str, key: str, value: str
) -> None:
    """A value pydantic would reject does not fail alone: it drops *every* override."""
    response = client.put(
        "/api/admin/settings", json={key: value}, headers={**bearer(admin_token), **CSRF_HEADERS}
    )
    assert response.status_code == 422
    assert key in response.json()["detail"]


def test_the_deadlines_are_offered_in_the_settings_list(client: TestClient, admin_token: str) -> None:
    rows = {r["key"]: r for r in client.get("/api/admin/settings", headers=bearer(admin_token)).json()}
    assert rows["auth_session_days"]["default_value"] == "14"
    assert rows["auth_session_days"]["kind"] == "int"
    assert rows["auth_idle_hours"]["kind"] == "int"
