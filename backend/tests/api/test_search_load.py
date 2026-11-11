"""A burst of text searches must queue or be refused, not exhaust Postgres' shared memory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import search_load
from tests.api.conftest import Archive
from tests.conftest import requires_db

pytestmark = requires_db

SETTING = "max_parallel_workers_per_gather"


def slots(monkeypatch: pytest.MonkeyPatch, count: int) -> asyncio.Semaphore:
    semaphore = asyncio.Semaphore(count)
    monkeypatch.setattr(search_load, "_slots", semaphore)
    monkeypatch.setattr(search_load, "SEARCH_WAIT_S", 0.01)
    return semaphore


@pytest.mark.parametrize("path", ["/api/search", "/api/calls", "/api/calls/facets"])
def test_a_text_search_is_refused_when_every_slot_stays_busy(
    client: TestClient, archive: Archive, monkeypatch: pytest.MonkeyPatch, path: str  # noqa: ARG001
) -> None:
    slots(monkeypatch, 0)
    response = client.get(path, params={"q": "fire"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(search_load.RETRY_AFTER_S)


def test_browsing_without_a_query_needs_no_slot(
    client: TestClient, archive: Archive, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    slots(monkeypatch, 0)
    assert client.get("/api/calls").status_code == 200


def test_a_blank_query_needs_no_slot(
    client: TestClient, archive: Archive, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    slots(monkeypatch, 0)
    assert client.get("/api/calls", params={"q": "   "}).status_code == 200


def test_a_finished_search_gives_its_slot_back(
    client: TestClient, archive: Archive, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    semaphore = slots(monkeypatch, 1)
    for _ in range(2):
        assert client.get("/api/search", params={"q": "fire"}).status_code == 200
    assert semaphore._value == 1


def request_for(query: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(query_params=query)


def workers(session: Session) -> str:
    return session.execute(text(f"SHOW {SETTING}")).scalar_one()


def test_a_text_search_uses_fewer_parallel_workers(session: Session) -> None:
    search_load.fewer_parallel_workers(request_for({"q": "fire"}), session)
    assert workers(session) == str(search_load.PARALLEL_WORKERS)
    session.rollback()


def test_the_limit_survives_a_rolled_back_transaction(session: Session) -> None:
    """A facet that times out rolls back, which would otherwise end a SET LOCAL."""
    search_load.fewer_parallel_workers(request_for({"q": "fire"}), session)
    session.rollback()
    assert workers(session) == str(search_load.PARALLEL_WORKERS)
    session.rollback()


def test_a_transaction_already_open_is_limited_too(session: Session) -> None:
    session.execute(text("SELECT 1"))
    search_load.fewer_parallel_workers(request_for({"q": "fire"}), session)
    assert workers(session) == str(search_load.PARALLEL_WORKERS)
    session.rollback()


def test_browsing_keeps_the_default_workers(session: Session) -> None:
    before = workers(session)
    session.rollback()
    search_load.fewer_parallel_workers(request_for({}), session)
    assert workers(session) == before
    session.rollback()
