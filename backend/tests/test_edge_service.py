"""Applying the HTTPS settings, remembering it, and the five-minute reconcile.

This is the layer with a database: the bookkeeping rows and the "has the edge been
restarted out from under us?" comparison the worker runs.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.edge import service as edge_service
from app.models import Setting
from app.runtime_settings import SETTABLE_KEYS, invalidate, read_overrides
from tests.conftest import requires_db

pytestmark = requires_db

BASE = "http://edge-test:2019"

_CONFIG_TLS = {
    "apps": {
        "http": {
            "servers": {"srv0": {"routes": [{"match": [{"host": ["keyup.example.com"]}], "handle": []}]}}
        }
    }
}
_CONFIG_PLAIN = {"apps": {"http": {"servers": {"srv0": {"routes": []}}}}}


@pytest.fixture
def tls_on(session: Session) -> None:
    """The settings table configured for TLS, the way the Admin page would leave it."""
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


def test_the_https_group_is_settable_and_layers_over_the_environment(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    """The precedence the docs promise: a row that is set wins over the environment."""
    assert {"tls_enabled", "tls_domains", "tls_redirect_http"} <= SETTABLE_KEYS
    settings = edge_service.https_settings(session)
    assert settings.tls_enabled is True
    assert settings.tls_domains == "keyup.example.com"


@respx.mock
def test_a_successful_apply_writes_the_three_bookkeeping_rows(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    result = edge_service.apply_now(session, reason="test", base_url=BASE)
    assert result.ok
    assert edge_service.read_note(session, edge_service.APPLIED_AT_KEY)
    assert edge_service.read_note(session, edge_service.LAST_ERROR_KEY) is None
    assert edge_service.read_note(session, edge_service.APPLIED_HASH_KEY) == result.config_hash


@respx.mock
def test_a_failed_apply_is_recorded_rather_than_raised(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    respx.post(f"{BASE}/load").mock(return_value=httpx.Response(400, text="adapting: bad"))
    result = edge_service.apply_now(session, reason="test", base_url=BASE)
    assert not result.ok
    assert "adapting" in str(edge_service.read_note(session, edge_service.LAST_ERROR_KEY))
    # No hash: nothing was loaded, so the reconcile must not believe the edge is configured.
    assert edge_service.read_note(session, edge_service.APPLIED_HASH_KEY) is None


def test_the_bookkeeping_rows_are_not_runtime_settings(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    """They live in the same table but must never be layered onto ``Settings``."""
    for key in (edge_service.APPLIED_AT_KEY, edge_service.LAST_ERROR_KEY, edge_service.APPLIED_HASH_KEY):
        assert key not in SETTABLE_KEYS
    session.add(Setting(key=edge_service.APPLIED_AT_KEY, value="whenever", updated_by="tests"))
    session.commit()
    assert edge_service.APPLIED_AT_KEY not in read_overrides(session)


# --------------------------------------------------------------------------- reconcile ---


@respx.mock
def test_the_reconcile_does_nothing_when_the_edge_serves_what_we_pushed(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    load = respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    edge_service.apply_now(session, reason="test", base_url=BASE)
    pushes = load.call_count
    respx.get(f"{BASE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_TLS))
    assert edge_service.reconcile(session, base_url=BASE) is None
    assert load.call_count == pushes


@respx.mock
def test_the_reconcile_re_applies_after_the_edge_restarts_on_the_environment(
    session: Session, tls_on
) -> None:  # noqa: ANN001, ARG001
    """The case it exists for: a restarted edge came back plain while the settings say TLS."""
    load = respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    edge_service.apply_now(session, reason="test", base_url=BASE)
    pushes = load.call_count
    respx.get(f"{BASE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_PLAIN))
    result = edge_service.reconcile(session, base_url=BASE)
    assert result is not None and result.ok
    assert load.call_count == pushes + 1


@respx.mock
def test_the_reconcile_applies_when_nothing_has_ever_been_pushed(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    """A fresh database, or every previous apply failed — the same situation either way."""
    load = respx.post(f"{BASE}/load").mock(return_value=httpx.Response(200, text=""))
    respx.get(f"{BASE}/config/").mock(return_value=httpx.Response(200, json=_CONFIG_TLS))
    assert edge_service.reconcile(session, base_url=BASE) is not None
    assert load.called


@respx.mock
def test_the_reconcile_is_silent_when_the_edge_is_unreachable(session: Session, tls_on) -> None:  # noqa: ANN001, ARG001
    """Nothing to reconcile against, and hammering /load at a dead socket helps nobody."""
    load = respx.post(f"{BASE}/load")
    respx.get(f"{BASE}/config/").mock(side_effect=httpx.ConnectError("nope"))
    assert edge_service.reconcile(session, base_url=BASE) is None
    assert not load.called
