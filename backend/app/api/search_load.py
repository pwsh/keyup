"""How many full-text searches may run at once, now that anyone can start one.

Each search runs its count, its ranked page and its facets through parallel Postgres workers,
and those share ``/dev/shm``. Thirty at once exhausted its 1 GB and answered 500
("could not resize shared memory segment"). So a search waits for one of a few slots, and
uses fewer workers while it runs.

The wait happens before anything touches the database, and on the event loop rather than in
a worker thread, so a queue of searches holds neither a pooled connection nor a thread that
a recorder's upload needs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db import get_session

SEARCH_SLOTS = 4
SEARCH_WAIT_S = 5.0
RETRY_AFTER_S = 2
PARALLEL_WORKERS = 1

_slots = asyncio.Semaphore(SEARCH_SLOTS)
_LIMIT_SQL = f"SET LOCAL max_parallel_workers_per_gather = {PARALLEL_WORKERS}"


def _is_text_search(request: Request) -> bool:
    return bool(request.query_params.get("q", "").strip())


async def search_slot(request: Request) -> AsyncIterator[None]:
    """Hold a slot for the length of a text search; a request with no ``q`` needs none."""
    if not _is_text_search(request):
        yield
        return
    try:
        await asyncio.wait_for(_slots.acquire(), SEARCH_WAIT_S)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="Too many searches are running. Try again in a moment.",
            headers={"Retry-After": str(RETRY_AFTER_S)},
        ) from exc
    try:
        yield
    finally:
        _slots.release()


def _limit_transaction(_session: Session, _transaction: Any, connection: Any) -> None:
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(_LIMIT_SQL)


def fewer_parallel_workers(request: Request, db: Annotated[Session, Depends(get_session)]) -> None:
    """Limit every transaction of this request: a facet timeout rolls back and starts another."""
    if not _is_text_search(request):
        return
    event.listen(db, "after_begin", _limit_transaction)
    if db.in_transaction():
        _limit_transaction(db, None, db.connection())


SEARCH_LOAD = [Depends(search_slot), Depends(fewer_parallel_workers)]
