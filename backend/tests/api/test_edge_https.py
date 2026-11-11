"""``/api/admin/https`` — status, apply, and the Cloudflare token check.

Caddy and Cloudflare are stood in for by ``respx``.  What these pin is the contract the
HTTPS tab renders against, and the two rules that matter for a page holding a credential:
the token never comes back out, and a save pushes the new configuration by itself.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Setting
from app.runtime_settings import invalidate
from tests.api.conftest import CSRF_HEADERS, bearer

EDGE = "http://edge:2019"

_CONFIG_TLS = {
    "apps": {
        "http": {
            "servers": {"srv0": {"routes": [{"match": [{"host": ["keyup.example.com"]}], "handle": []}]}}
        }
    }
}


@pytest.fixture
def tls_settings(session: Session) -> None:
    for key, value in {
        "tls_enabled": True,
        "tls_domains": "keyup.example.com",
        "tls_cloudflare_api_token": "cf-token-value",
        "tls_acme_email": "nobody@example.com",
        "tls_redirect_http": True,
    }.items():
        session.add(Setting(key=key, value=value, updated_by="tests"))
    session.commit()
    invalidate()
    yield
    invalidate()


@respx.mock
def test_status_reports_the_mode_and_never_the_token(
    client: TestClient, admin_token: str, tls_settings: None
) -> None:
    respx.get(f"{EDGE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_TLS))
    body = client.get("/api/admin/https/status", headers=bearer(admin_token)).json()
    assert body["desired_mode"] == "tls"
    assert body["mode"] == "tls"
    assert body["reachable"] is True
    assert body["domains"] == ["keyup.example.com"]
    assert body["caddy_domains"] == ["keyup.example.com"]
    assert body["redirect_http"] is True
    assert body["acme_directory"].endswith("/directory")
    assert body["has_token"] is True
    assert body["missing"] == []
    assert "cf-token-value" not in str(body)


@respx.mock
def test_status_separates_unreachable_from_off(
    client: TestClient, admin_token: str, tls_settings: None
) -> None:
    respx.get(f"{EDGE}/config/").mock(side_effect=httpx.ConnectError("nope"))
    body = client.get("/api/admin/https/status", headers=bearer(admin_token)).json()
    assert body["reachable"] is False
    assert body["mode"] == "unknown"
    assert body["desired_mode"] == "tls"
    assert body["error"]


@respx.mock
def test_status_names_what_is_still_blank(client: TestClient, admin_token: str, session: Session) -> None:
    """The switch on with nothing filled in is the commonest half-configured state."""
    session.add(Setting(key="tls_enabled", value=True, updated_by="tests"))
    session.commit()
    invalidate()
    respx.get(f"{EDGE}/config/").mock(return_value=httpx.Response(200, json={"apps": {}}))
    body = client.get("/api/admin/https/status", headers=bearer(admin_token)).json()
    # `auto` with no token is HTTP-01, which needs no token: only the name and the contact.
    assert body["missing"] == ["tls_domains", "tls_acme_email"]
    assert body["desired_mode"] == "plain"
    # Asking for DNS-01 makes the token a requirement again.
    session.add(Setting(key="tls_challenge", value="dns", updated_by="tests"))
    session.commit()
    invalidate()
    body = client.get("/api/admin/https/status", headers=bearer(admin_token)).json()
    assert body["missing"] == ["tls_domains", "tls_acme_email", "tls_cloudflare_api_token"]
    assert body["desired_mode"] == "plain"
    invalidate()


def test_status_is_admin_only(client: TestClient, read_token: str) -> None:
    assert client.get("/api/admin/https/status", headers=bearer(read_token)).status_code == 403


@respx.mock
def test_apply_pushes_the_configuration_and_records_the_outcome(
    client: TestClient, admin_token: str, session: Session, tls_settings: None
) -> None:
    load = respx.post(f"{EDGE}/load").mock(return_value=httpx.Response(200, text=""))
    body = client.post("/api/admin/https/apply", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert body["mode"] == "tls"
    assert load.called
    assert "keyup.example.com {" in load.calls.last.request.content.decode()
    respx.get(f"{EDGE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_TLS))
    status_body = client.get("/api/admin/https/status", headers=bearer(admin_token)).json()
    assert status_body["last_applied_at"]
    assert status_body["last_error"] is None


@respx.mock
def test_apply_reports_a_refused_configuration(
    client: TestClient, admin_token: str, tls_settings: None
) -> None:
    respx.post(f"{EDGE}/load").mock(return_value=httpx.Response(400, text="adapting: bad"))
    body = client.post("/api/admin/https/apply", headers=bearer(admin_token)).json()
    assert body["ok"] is False
    assert "adapting" in body["detail"]


@respx.mock
def test_saving_an_https_setting_applies_it_by_itself(
    client: TestClient, admin_token: str, session: Session
) -> None:
    """No "and now press Apply": the save is the change, so the save is the push."""
    load = respx.post(f"{EDGE}/load").mock(return_value=httpx.Response(200, text=""))
    response = client.put(
        "/api/admin/settings",
        json={
            "tls_enabled": "true",
            "tls_domains": "saved.example.com",
            "tls_acme_email": "nobody@example.com",
            "tls_cloudflare_api_token": "cf-token-value",
        },
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200
    assert load.called
    assert "saved.example.com {" in load.calls.last.request.content.decode()
    invalidate()


@respx.mock
def test_saving_something_else_does_not_touch_the_edge(
    client: TestClient, admin_token: str, session: Session
) -> None:
    load = respx.post(f"{EDGE}/load")
    client.put(
        "/api/admin/settings",
        json={"stt_language": "en"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert not load.called
    invalidate()


def test_the_token_is_write_only_in_the_settings_list(
    client: TestClient, admin_token: str, tls_settings: None
) -> None:
    """The Admin page may set a Cloudflare token and must never be a place to read one."""
    rows = client.get("/api/admin/settings", headers=bearer(admin_token)).json()
    token_row = next(r for r in rows if r["key"] == "tls_cloudflare_api_token")
    assert token_row["kind"] == "secret"
    assert token_row["value"] == ""
    assert token_row["is_default"] is False  # …but it does say one is stored
    assert "cf-token-value" not in str(rows)


@respx.mock
def test_test_dns_proves_the_token_against_cloudflare(
    client: TestClient, admin_token: str, tls_settings: None
) -> None:
    respx.get("https://api.cloudflare.com/client/v4/user/tokens/verify").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"status": "active"}})
    )
    respx.get("https://api.cloudflare.com/client/v4/zones").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [{"id": "z1", "permissions": []}]})
    )
    body = client.post("/api/admin/https/test-dns", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert body["zones"][0]["zone"] == "example.com"
    assert "cf-token-value" not in str(body)


def test_test_dns_is_admin_only(client: TestClient, write_token: str) -> None:
    assert client.post("/api/admin/https/test-dns", headers=bearer(write_token)).status_code == 403
