"""The admin-API client: pushing a configuration, reading one back, and the token check.

``respx`` stands in for Caddy and for Cloudflare, so none of this touches a network.  The
certificate probe is a real TLS handshake and is exercised by the live smoke test in the
Makefile instead; what is pinned here is that it fails *softly* when there is nothing to
handshake with, because the Admin page calls it on every status poll.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import Settings
from app.edge.admin import apply, certificate_status, status, verify_cloudflare_token

BASE = "http://edge-test:2019"


def _settings(**values: object) -> Settings:
    base = {
        "tls_enabled": True,
        "tls_domains": "keyup.example.com",
        "tls_cloudflare_api_token": "cf-token-value",
        "tls_acme_email": "nobody@example.com",
        "tls_redirect_http": True,
    }
    return Settings(_env_file=None, **{**base, **values})


# ------------------------------------------------------------------------------- apply ---


@respx.mock
def test_apply_posts_the_rendered_caddyfile_as_a_caddyfile() -> None:
    """The content type is what tells Caddy to adapt rather than parse JSON."""
    route = respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    result = apply(_settings(), BASE)
    assert result.ok
    assert result.mode == "tls"
    assert result.domains == ["keyup.example.com"]
    request = route.calls.last.request
    assert request.headers["content-type"] == "text/caddyfile"
    body = request.content.decode()
    assert "keyup.example.com {" in body
    assert "cf-token-value" in body  # resolved on this side; there is no edge env to expand


@respx.mock
def test_apply_reports_a_rejected_configuration_without_raising() -> None:
    respx.post(f"{BASE}/load").mock(return_value=httpx.Response(400, text="adapting: line 3: bad"))
    result = apply(_settings(), BASE)
    assert not result.ok
    assert "line 3" in result.detail


@respx.mock
def test_apply_reports_an_unreachable_edge_without_raising() -> None:
    """Start-up, a settings save and the reconcile all call this; none may fail on it."""
    respx.post(f"{BASE}/load").mock(side_effect=httpx.ConnectError("nope"))
    result = apply(_settings(), BASE)
    assert not result.ok
    assert "ConnectError" in result.detail


@respx.mock
def test_the_apply_result_never_carries_the_token() -> None:
    respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    result = apply(_settings(), BASE)
    assert "cf-token-value" not in repr(result)
    assert len(result.config_hash) == 64


# ------------------------------------------------------------------------------ status ---

_CONFIG_TLS = {
    "apps": {
        "http": {
            "servers": {
                "srv0": {
                    "listen": [":443"],
                    "routes": [{"match": [{"host": ["keyup.example.com"]}], "handle": []}],
                }
            }
        }
    }
}
_CONFIG_PLAIN = {"apps": {"http": {"servers": {"srv0": {"listen": [":80"], "routes": []}}}}}


@respx.mock
def test_status_reads_the_hostnames_out_of_caddys_adapted_config() -> None:
    respx.get(f"{BASE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_TLS))
    state = status(_settings(), BASE, probe_certificates=False)
    assert state.reachable
    assert state.mode == "tls"
    assert state.caddy_domains == ["keyup.example.com"]
    assert state.desired_mode == "tls"


@respx.mock
def test_status_calls_the_plain_variant_plain() -> None:
    """No host matcher anywhere is exactly what the TLS-off configuration looks like."""
    respx.get(f"{BASE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_PLAIN))
    state = status(_settings(tls_enabled=False), BASE, probe_certificates=False)
    assert state.mode == "plain"
    assert state.caddy_domains == []
    assert state.desired_mode == "plain"


@respx.mock
def test_status_says_unreachable_rather_than_off() -> None:
    """ "The edge is not answering" and "TLS is off" must not look the same on the page."""
    respx.get(f"{BASE}/config/").mock(side_effect=httpx.ConnectError("nope"))
    state = status(_settings(), BASE, probe_certificates=False)
    assert not state.reachable
    assert state.mode == "unknown"
    assert "ConnectError" in (state.error or "")


def test_the_certificate_probe_fails_softly() -> None:
    """It runs on every status poll; an unroutable edge must be a message, not a 500."""
    result = certificate_status("keyup.example.com", host="127.0.0.1", port=1, timeout=2.0)
    assert not result.ok
    assert result.error


# -------------------------------------------------------------------- Cloudflare token ---

CF = "https://cf.test/client/v4"


@respx.mock
def test_the_token_check_asks_both_questions() -> None:
    verify = respx.get(f"{CF}/user/tokens/verify").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"status": "active"}})
    )
    zones = respx.get(f"{CF}/zones").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": [{"id": "z1", "permissions": ["#dns_records:edit"]}]}
        )
    )
    result = verify_cloudflare_token("tok", ["keyup.example.com"], base_url=CF)
    assert result["ok"], result
    assert result["zones"][0]["zone"] == "example.com"
    assert verify.called and zones.called
    # Never in a query string, always in the header.
    assert zones.calls.last.request.headers["authorization"] == "Bearer tok"
    assert "tok" not in str(zones.calls.last.request.url)


@respx.mock
def test_a_token_scoped_to_the_wrong_zone_is_caught_before_an_issuance() -> None:
    """The failure this endpoint exists for: it verifies, and then DNS-01 fails anyway."""
    respx.get(f"{CF}/user/tokens/verify").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"status": "active"}})
    )
    respx.get(f"{CF}/zones").mock(return_value=httpx.Response(200, json={"success": True, "result": []}))
    result = verify_cloudflare_token("tok", ["keyup.example.com"], base_url=CF)
    assert not result["ok"]
    assert "example.com" in result["detail"]
    assert result["zones"][0]["found"] is False


@respx.mock
def test_a_dead_token_says_so_and_asks_cloudflare_nothing_else() -> None:
    respx.get(f"{CF}/user/tokens/verify").mock(
        return_value=httpx.Response(
            401, json={"success": False, "errors": [{"message": "Invalid API Token"}]}
        )
    )
    zones = respx.get(f"{CF}/zones")
    result = verify_cloudflare_token("tok", ["keyup.example.com"], base_url=CF)
    assert not result["ok"]
    assert "Invalid API Token" in result["detail"]
    assert not zones.called


@pytest.mark.parametrize("token", ["", "   ", None])
def test_a_blank_token_is_answered_without_a_request(token: str | None) -> None:
    result = verify_cloudflare_token(token or "", ["keyup.example.com"])
    assert not result["ok"]
    assert "token" in result["detail"]
