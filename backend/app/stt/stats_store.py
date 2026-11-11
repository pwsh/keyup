"""Publishing the router's counters where another process can read them (ARCHITECTURE §5.2).

:class:`~app.stt.router.BackendStats` lives in the memory of whichever process owns the
router, and the only process that owns one is ``keyup worker``.  The API process answers
the admin "backends" page, has no router, and used to build a throwaway one — which is why
the page reported zeros however hard the worker was working.

This module is the crossing, in both directions:

* **out** — :class:`StatsPublisher` upserts one ``stt_backend_stats`` row per backend every
  :data:`PUBLISH_INTERVAL_S` seconds, and immediately when a backend's health flips, so the
  page's worst-case lag is the cadence and a server going down shows up at once.  Rows are
  never deleted: :func:`is_stale` turns a row nobody has updated for
  :data:`STALE_AFTER_S` into the "worker not reporting" state, which is information the
  operator wants rather than an absence to hide.
* **in** — :func:`override_map` reads the operator's runtime enable/disable overrides back,
  and the worker's router calls it (through :func:`override_provider`) on a five-second
  cache at selection time.

The upsert sets only non-key columns — see the lock note in :mod:`app.ingest.service`; it
also leaves the ``admin_*`` columns alone, because those belong to the operator and the
worker is only ever reporting.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import SttBackendStat

log = logging.getLogger(__name__)

__all__ = [
    "PUBLISH_INTERVAL_S",
    "STALE_AFTER_S",
    "StatsPublisher",
    "is_stale",
    "override_map",
    "override_provider",
    "publish",
    "set_override",
]

#: how often the worker republishes every row
PUBLISH_INTERVAL_S = 10.0

#: a row older than this is reported as stale — six publish intervals, so a slow pass or a
#: momentarily unreachable database does not make a healthy worker look dead
STALE_AFTER_S = 60.0

#: the columns the worker's upsert may assign.  Deliberately a list rather than "everything
#: but the primary key": ``admin_enabled``/``admin_changed_*`` are the operator's, and a
#: worker that overwrote them would undo a click every ten seconds.
PUBLISHED_COLUMNS = (
    "kind",
    "base_url",
    "model",
    "priority",
    "concurrency",
    "enabled",
    "config_enabled",
    "healthy",
    "health_detail",
    "disqualified",
    "in_flight",
    "attempts",
    "successes",
    "errors",
    "consecutive_errors",
    "last_error",
    "last_error_at",
    "last_success_at",
    "audio_s",
    "processing_s",
    "mean_rtf",
    "mean_processing_s",
    "recent_completed_1m",
    "recent_failed_1m",
    "worker_instance",
    "started_at",
    "config_version",
    "updated_at",
)


def _row_values(
    name: str,
    entry: dict[str, Any],
    *,
    worker_instance: str,
    started_at: datetime,
    now: datetime,
    config_version: int | None = None,
) -> dict[str, Any]:
    """One backend's ``stt_backend_stats`` row, from the router's ``stats()`` entry."""
    return {
        "name": name,
        "kind": entry.get("kind"),
        "base_url": entry.get("base_url"),
        "model": entry.get("model"),
        "priority": int(entry.get("priority") or 100),
        "concurrency": int(entry.get("concurrency") or 1),
        "enabled": bool(entry.get("enabled", True)),
        "config_enabled": bool(entry.get("config_enabled", True)),
        "healthy": entry.get("healthy"),
        "health_detail": (entry.get("health_detail") or "")[:2000] or None,
        "disqualified": (entry.get("disqualified") or None),
        "in_flight": int(entry.get("in_flight") or 0),
        "attempts": int(entry.get("attempts") or 0),
        "successes": int(entry.get("successes") or 0),
        "errors": int(entry.get("errors") or 0),
        "consecutive_errors": int(entry.get("consecutive_errors") or 0),
        "last_error": str(entry["last_error"])[:2000] if entry.get("last_error") else None,
        "last_error_at": entry.get("last_error_at"),
        "last_success_at": entry.get("last_success_at"),
        "audio_s": float(entry.get("audio_s") or 0.0),
        "processing_s": float(entry.get("processing_s") or 0.0),
        "mean_rtf": entry.get("mean_rtf"),
        "mean_processing_s": entry.get("mean_processing_s"),
        "recent_completed_1m": int(entry.get("recent_completed_1m") or 0),
        "recent_failed_1m": int(entry.get("recent_failed_1m") or 0),
        "worker_instance": worker_instance,
        "started_at": started_at,
        "config_version": config_version,
        "updated_at": now,
    }


def upsert_statement(values: dict[str, Any]) -> Any:
    """The ``INSERT … ON CONFLICT (name) DO UPDATE`` for one row.

    Exposed so a test can assert what the SET assigns: never ``name`` (the key, which would
    take ``FOR UPDATE`` on the row instead of ``FOR NO KEY UPDATE``) and never the
    operator's ``admin_*`` columns.
    """
    stmt = pg_insert(SttBackendStat).values(**values)
    return stmt.on_conflict_do_update(
        index_elements=[SttBackendStat.name],
        set_={column: stmt.excluded[column] for column in PUBLISHED_COLUMNS if column in values},
    )


def publish(
    session: Session,
    stats: dict[str, dict[str, Any]],
    *,
    worker_instance: str,
    started_at: datetime,
    now: datetime | None = None,
    config_version: int | None = None,
) -> int:
    """Upsert one row per backend from a router ``stats()`` snapshot.  Commits.

    ``config_version`` is the ``stt_backends`` generation the publishing router was built
    from (:mod:`app.stt.hotswap`).  It rides along with the counters because it is only
    meaningful next to them: it says *these* numbers came from *that* configuration, which
    is how the admin page tells "the worker has applied your edit" from "the worker is
    still running the configuration you replaced".
    """
    stamp = now or datetime.now(UTC)
    written = 0
    for name, entry in stats.items():
        values = _row_values(
            name,
            entry,
            worker_instance=worker_instance,
            started_at=started_at,
            now=stamp,
            config_version=config_version,
        )
        session.execute(upsert_statement(values))
        written += 1
    session.commit()
    return written


def is_stale(updated_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Has the worker stopped reporting this row?"""
    if updated_at is None:
        return True
    stamp = now or datetime.now(UTC)
    if updated_at.tzinfo is None:  # pragma: no cover - the column is timestamptz
        updated_at = updated_at.replace(tzinfo=UTC)
    return stamp - updated_at > timedelta(seconds=STALE_AFTER_S)


# --------------------------------------------------------------------------------------
# the operator's overrides, read back
# --------------------------------------------------------------------------------------


def override_map(session: Session) -> dict[str, bool]:
    """``{backend name: enabled}`` for the backends the operator has overridden."""
    rows = session.execute(
        select(SttBackendStat.name, SttBackendStat.admin_enabled).where(
            SttBackendStat.admin_enabled.is_not(None)
        )
    ).all()
    return {name: bool(enabled) for name, enabled in rows}


def override_provider(session_factory: Callable[[], Session]) -> Callable[[], dict[str, bool]]:
    """A no-argument reader for :class:`~app.stt.router.BackendRouter`.

    Its own session per call, because the router asks from whichever worker thread happens
    to be picking a backend and psycopg connections are not shared across threads.
    """

    def read() -> dict[str, bool]:
        session = session_factory()
        try:
            return override_map(session)
        finally:
            session.close()

    return read


def set_override(
    session: Session, name: str, enabled: bool | None, *, changed_by: str | None = None
) -> None:
    """Write (or clear, with ``None``) one operator override.  Does not commit.

    An upsert rather than an update: the operator may well disable a backend before the
    worker has ever published a row for it, and "the row is not there yet" must not be the
    difference between a switch that works and one that silently does nothing.
    """
    now = datetime.now(UTC)
    stmt = pg_insert(SttBackendStat).values(
        name=name,
        admin_enabled=enabled,
        admin_changed_at=now,
        admin_changed_by=changed_by,
        updated_at=now,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SttBackendStat.name],
            set_={
                "admin_enabled": stmt.excluded.admin_enabled,
                "admin_changed_at": stmt.excluded.admin_changed_at,
                "admin_changed_by": stmt.excluded.admin_changed_by,
            },
        )
    )


# --------------------------------------------------------------------------------------
# the publisher thread
# --------------------------------------------------------------------------------------


class StatsPublisher:
    """Republishes the router's counters on a cadence, and on demand.

    ``wake()`` is what the router's health-change hook calls: a backend that just went down
    should be red on the page now, not up to :data:`PUBLISH_INTERVAL_S` seconds from now.
    """

    def __init__(
        self,
        router: Any,
        session_factory: Callable[[], Session],
        *,
        worker_instance: str,
        interval_s: float = PUBLISH_INTERVAL_S,
        started_at: datetime | None = None,
        on_tick: Callable[[], Any] | None = None,
    ) -> None:
        self.router = router
        self.session_factory = session_factory
        self.worker_instance = worker_instance
        self.interval_s = interval_s
        self.started_at = started_at or datetime.now(UTC)
        #: run at the top of every pass, before the publish.  The worker passes
        #: :meth:`app.stt.hotswap.RouterReloader.poll` here rather than starting a second
        #: thread: this loop already wakes on the right cadence, and doing the reload first
        #: means the row this pass writes already carries the version it just applied.
        self.on_tick = on_tick
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        """Publish as soon as the loop can get to it."""
        self._wake.set()

    def publish_once(self) -> int:
        """One pass.  Never raises: a database blip must not take the worker down."""
        session = self.session_factory()
        try:
            return publish(
                session,
                self.router.stats(),
                worker_instance=self.worker_instance,
                started_at=self.started_at,
                config_version=getattr(self.router, "config_version", None),
            )
        except Exception:  # noqa: BLE001 - reporting is not the work
            log.exception("stt: could not publish backend stats")
            session.rollback()
            return 0
        finally:
            session.close()

    def _tick(self) -> None:
        """The reload hook, if there is one.  A broken hook must not stop the reporting."""
        if self.on_tick is None:
            return
        try:
            self.on_tick()
        except Exception:  # noqa: BLE001 - reporting outlives whatever the hook was doing
            log.exception("stt: the publisher's tick hook raised")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self.publish_once()
            self._wake.wait(self.interval_s)
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="stt-stats", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        # One last pass so the page shows the final counters, and the row's age from then on
        # is honestly "this worker stopped".
        self.publish_once()
