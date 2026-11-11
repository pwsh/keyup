"""A read-only request may be cancelled; a write may not (``app.db``).

The Calls page that starved the pool is the reason this exists: five facet queries of
14-24 s each held five of the fifteen connections, ``POST /api/recorder/spectrum`` waited
30 s for one and gave up with a 500, and the archive stopped ingesting because somebody
ticked seven categories.  A runaway *read* now dies at sixty seconds and gives the
connection back; ingestion, the workers and the migrations keep no timeout at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.db import READ_ONLY_METHODS, READ_STATEMENT_TIMEOUT, get_session
from tests.conftest import requires_db

pytestmark = requires_db


class _Connection:
    """The shape :func:`get_session` reads: an ``HTTPConnection`` has a method, a WebSocket
    does not."""

    def __init__(self, method: str | None = None) -> None:
        if method is not None:
            self.method = method


def _timeout(connection: Any) -> str:
    generator = get_session(connection)
    session = next(generator)
    try:
        return str(session.scalar(text("SHOW statement_timeout")))
    finally:
        generator.close()


@pytest.mark.parametrize("method", sorted(READ_ONLY_METHODS))
def test_a_read_only_request_is_cancelled_after_a_minute(method: str) -> None:
    assert _timeout(_Connection(method)) == "1min"
    assert READ_STATEMENT_TIMEOUT == "60s"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_writing_request_keeps_no_timeout(method: str) -> None:
    """A slow write is a write that must finish: killing a reconcile or a backfill halfway
    to protect a dashboard is the wrong trade in the other direction."""
    assert _timeout(_Connection(method)) == "0"


def test_a_websocket_is_not_armed() -> None:
    """A socket has no method, and the live feed opens its own short sessions per poll."""
    assert _timeout(_Connection()) == "0"


def test_the_timeout_comes_back_after_a_rollback() -> None:
    """``SET LOCAL`` dies with its transaction, so it is re-armed on the next one — which is
    exactly what the facet path relies on when it recovers from a cancelled dimension."""
    generator = get_session(_Connection("GET"))
    session = next(generator)
    try:
        assert str(session.scalar(text("SHOW statement_timeout"))) == "1min"
        session.rollback()
        assert str(session.scalar(text("SHOW statement_timeout"))) == "1min"
    finally:
        generator.close()
