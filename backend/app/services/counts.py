"""Cheap answers to "how many rows are there" — estimates and a small TTL cache.

``calls`` is three million rows in five gigabytes of heap.  A ``SELECT count(*)`` over it
is a full sequential scan every time, and ``GET /api/status`` — polled every ten seconds by
the container health check *and* by every open tab — was running four of them plus two
``GROUP BY``s over ``ingest_files`` and ``transcription_jobs``.  A 60-second sample of
``pg_stat_activity`` on production had a bare ``count(*) FROM calls`` in 19 % of the
snapshots: the archive was spending a fifth of its database time answering a number nobody
reads to the last digit.

Two tools, both deliberately small:

:func:`estimated_rows`
    ``pg_class.reltuples`` — the row count the last ``ANALYZE`` measured, which is free
    (one catalog lookup: 0.5 ms against 381 ms for the ``count(*)`` it replaces on
    production) and within a couple of percent of the truth on a table autovacuum keeps up
    with — 3 027 842 against a real 3 083 935, measured.  Falls back to an exact count
    when the estimate is unusable:
    a table that has never been analyzed (``reltuples < 0`` since PostgreSQL 14), a
    non-PostgreSQL backend, or a table small enough that the exact count costs nothing —
    see :data:`ESTIMATE_MIN_ROWS`.  That last rule is what keeps test databases exact.
:func:`cached`
    a process-local, thread-safe TTL cache, modelled on :func:`app.ingest.flow.counts`:
    one key, one value, one expiry, ``force=True`` to bypass it.  The trade is the same one
    the flow gauge makes — the number can be up to ``ttl_s`` old, and nothing that reads it
    needs it to be newer than that.  It is bounded: an expired entry is dropped when it is
    read or when any entry is written, and past :data:`MAX_ENTRIES` the least recently used
    entry goes.  Keys that are built from a request (the facet cache keys on the filter)
    would otherwise grow the table for as long as the process lives.

Neither is a substitute for a real count where the exact number is the point (an invoice, a
reconciliation, a test assertion).  They are for headline figures on a dashboard.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("app.services.counts")

__all__ = [
    "DEFAULT_TTL_S",
    "ESTIMATE_MIN_ROWS",
    "MAX_ENTRIES",
    "cached",
    "cached_at",
    "estimated_rows",
    "invalidate",
    "size",
]

#: How long a cached value is reused when the caller does not say.  One minute: long enough
#: that a health check every ten seconds and a dozen open tabs cost one query a minute,
#: short enough that an operator watching a backfill sees it move.
DEFAULT_TTL_S = 60.0

#: Below this many rows, :func:`estimated_rows` counts exactly instead of estimating.  An
#: exact count of ten thousand rows is a millisecond; the estimate's whole reason to exist
#: is tables where the scan is seconds.  It also means a freshly-migrated test database —
#: where ``reltuples`` is whatever autovacuum last happened to see — answers exactly, so a
#: test may assert ``counts["calls"] == 1``.
ESTIMATE_MIN_ROWS = 100_000

#: The most entries :func:`cached` keeps.  The fixed keys (status, overview, ingest, feed
#: presets, …) are a dozen; the rest are facet sets, one per distinct filter a reader has
#: asked about in the last minute.  A facet set is 10-50 KB, so the cap holds the table to
#: a few megabytes whatever the traffic does.
MAX_ENTRIES = 256


# --------------------------------------------------------------------------------------
# row estimates
# --------------------------------------------------------------------------------------


def _table_name(table: Any) -> str:
    """The SQL table name for a model class, a ``Table``, or a plain string."""
    name = getattr(table, "__tablename__", None)
    if name is None:
        name = getattr(table, "name", None)
    return str(name if name is not None else table)


def _exact(session: Session, name: str) -> int:
    # The name comes from a mapped class in this codebase, never from a request; quoting it
    # is belt-and-braces against a table whose name needs it.
    return int(session.scalar(text(f'SELECT count(*) FROM "{name}"')) or 0)


def estimated_rows(session: Session, table: Any) -> int:
    """Roughly how many rows ``table`` holds, without scanning it.

    ``table`` may be a mapped class, a :class:`~sqlalchemy.Table`, or a table name.  The
    answer comes from ``pg_class.reltuples`` when that is both available and worth using,
    and from ``count(*)`` otherwise — so the *contract* is "a row count", and the only thing
    that varies is how expensive it was.  Estimates are only ever returned for tables of at
    least :data:`ESTIMATE_MIN_ROWS` rows, which is where an exact count starts to hurt.
    """
    name = _table_name(table)
    dialect = getattr(getattr(session, "bind", None), "dialect", None)
    if getattr(dialect, "name", "") != "postgresql":
        # SQLite and friends have no reltuples; they also have no five-gigabyte tables.
        return _exact(session, name)
    try:
        estimate = session.scalar(
            text("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(:name)"),
            {"name": name},
        )
    except Exception:  # noqa: BLE001 - a missing catalog row must not break a health check
        log.debug("counts: reltuples unavailable for %s", name, exc_info=True)
        estimate = None
    if estimate is None or int(estimate) < ESTIMATE_MIN_ROWS:
        # Never analyzed (-1), genuinely empty, or small enough that exact is free.
        return _exact(session, name)
    return int(estimate)


# --------------------------------------------------------------------------------------
# the TTL cache
# --------------------------------------------------------------------------------------

_lock = threading.Lock()
#: key -> (monotonic instant it was computed, wall-clock instant, ttl in seconds, value),
#: least recently used first.
_entries: OrderedDict[str, tuple[float, float, float, Any]] = OrderedDict()


def invalidate(key: str | None = None) -> None:
    """Drop one cached value, or all of them (tests, and after a bulk write)."""
    with _lock:
        if key is None:
            _entries.clear()
        else:
            _entries.pop(key, None)


def size() -> int:
    """How many entries the cache holds right now (tests, diagnostics)."""
    with _lock:
        return len(_entries)


def cached_at(key: str) -> float | None:
    """Wall-clock :func:`time.time` when ``key`` was last computed, or ``None``."""
    with _lock:
        entry = _entries.get(key)
    return None if entry is None else entry[1]


def _prune(now: float) -> None:
    """Drop expired entries, then the least recently used past :data:`MAX_ENTRIES`.

    Called with ``_lock`` held, on every write.  The sweep is a walk over at most
    :data:`MAX_ENTRIES` tuples, which is nothing next to the query the write just cached.
    """
    for key in [k for k, entry in _entries.items() if now - entry[0] >= entry[2]]:
        del _entries[key]
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)


def cached[T](
    key: str,
    ttl_s: float,
    compute: Callable[[], T],
    *,
    force: bool = False,
) -> T:
    """``compute()``, remembered under ``key`` for ``ttl_s`` seconds.

    ``force=True`` recomputes and refreshes the entry — what a CLI command or an explicit
    "refresh" button wants, where the caller asked for the number *now*.

    ``compute`` runs outside the lock: two threads arriving on a cold key both compute, and
    the second one's answer wins.  That is deliberate — holding a lock across a database
    query would make this cache a serialisation point of its own, which is the problem it
    was written to solve.

    An entry found expired is removed on the spot, and every write sweeps the rest and
    trims the table to :data:`MAX_ENTRIES`, least recently used first.
    """
    now = time.monotonic()
    if not force:
        with _lock:
            entry = _entries.get(key)
            if entry is not None:
                if now - entry[0] < ttl_s:
                    _entries.move_to_end(key)
                    return entry[3]  # type: ignore[no-any-return]
                del _entries[key]
    value = compute()
    with _lock:
        stamp = time.monotonic()
        _entries[key] = (stamp, time.time(), ttl_s, value)
        _entries.move_to_end(key)
        _prune(stamp)
    return value
