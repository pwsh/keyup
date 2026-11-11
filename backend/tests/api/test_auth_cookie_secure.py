"""The session cookie's ``Secure`` flag follows the configured public URL.

Behind the TLS-terminating edge (docs/HTTPS.md) every request reaches this process as
plain http, so the flag cannot be decided from the scheme the ASGI app sees — it is
derived from ``PUBLIC_URL``, which is why a cutover to https means editing that variable
by hand.  These are pure settings tests: no database, no client.
"""

from __future__ import annotations

import pytest
from fastapi import Response

from app.auth import COOKIE_NAME, clear_session_cookie, cookie_secure, set_session_cookie
from app.config import Settings


def _settings(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        ("https://keyup.example.com", True),
        ("HTTPS://KEYUP.EXAMPLE.COM", True),
        ("  https://keyup.example.com  ", True),
        ("http://192.0.2.60", False),
        ("http://localhost:8096", False),
    ],
)
def test_auto_follows_the_public_url_scheme(public_url: str, expected: bool) -> None:
    assert cookie_secure(_settings(public_url=public_url, auth_cookie_secure="auto")) is expected


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_an_explicit_true_overrides_a_plain_public_url(value: str) -> None:
    assert cookie_secure(_settings(public_url="http://192.0.2.60", auth_cookie_secure=value)) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_an_explicit_false_overrides_an_https_public_url(value: str) -> None:
    """The escape hatch: an https public URL that is nevertheless reached over http."""
    settings = _settings(public_url="https://keyup.example.com", auth_cookie_secure=value)
    assert cookie_secure(settings) is False


def test_an_unrecognised_value_falls_back_to_auto() -> None:
    settings = _settings(public_url="https://keyup.example.com", auth_cookie_secure="perhaps")
    assert cookie_secure(settings) is True


def _cookie_header(response: Response) -> str:
    headers = [v for k, v in response.raw_headers]
    return next(h.decode() for h in headers if h.decode().startswith(f"{COOKIE_NAME}="))


def test_the_session_cookie_carries_secure_on_an_https_deployment() -> None:
    settings = _settings(public_url="https://keyup.example.com", auth_cookie_secure="auto")
    response = Response()
    set_session_cookie(response, "a-session-token", settings)
    header = _cookie_header(response)
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header


def test_the_session_cookie_omits_secure_on_a_plain_http_deployment() -> None:
    settings = _settings(public_url="http://192.0.2.60", auth_cookie_secure="auto")
    response = Response()
    set_session_cookie(response, "a-session-token", settings)
    assert "Secure" not in _cookie_header(response)


def test_clearing_the_cookie_uses_the_same_flags() -> None:
    """A Set-Cookie that clears must match the one that set, or the browser keeps it."""
    settings = _settings(public_url="https://keyup.example.com", auth_cookie_secure="auto")
    response = Response()
    clear_session_cookie(response, settings)
    assert "Secure" in _cookie_header(response)
