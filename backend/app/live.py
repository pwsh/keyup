"""The live bus: PostgreSQL ``LISTEN/NOTIFY`` fanned out to WebSocket and SSE clients (§4.4).

The ingest worker and the transcription worker do not talk to the API process — they talk
to the database, and announce what they did with ``pg_notify('keyup_events', …)``
(:func:`app.db.notify`).  One background thread here holds a dedicated autocommit psycopg
connection with ``LISTEN keyup_events`` and pushes every notification into an
``asyncio.Queue`` per connected client.

Two consequences worth stating:

* the payload carries **identifiers only** (``pg_notify`` refuses payloads over 8000
  bytes), so a consumer re-reads the call row and applies the client's filter to it — which
  is also what makes a filtered live feed possible at all;
* a slow client cannot slow anything else down: its queue is bounded and, once full, its
  oldest events are dropped rather than blocking the listener thread.

Every event is stamped with a process-wide ``seq`` as it is published, so two events about
the same call are never mistaken for one another (the API's render cache keys on it).  And
anything that loses events says so: a listener that had to reconnect publishes a
:data:`GAP_EVENT` to every subscriber (notifications sent while it was away are gone —
``NOTIFY`` has no replay), and a subscriber whose queue overflowed is flagged, so the socket
can tell its client to backfill rather than leave a hole in the list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import psycopg

from app.config import get_settings
from app.db import EVENT_CHANNEL

log = logging.getLogger("app.live")

#: How many events one client may fall behind before its oldest are dropped.
QUEUE_MAXSIZE = 200
#: Reconnect backoff for the listener thread, seconds.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

#: Published to every subscriber when the listener comes back after losing its connection:
#: whatever was notified in between is lost, and a client has to refetch its window.
GAP_EVENT = "live.gap"


def dsn(database_url: str | None = None) -> str:
    """psycopg's own DSN — SQLAlchemy's ``postgresql+psycopg://`` prefix stripped."""
    url = database_url or get_settings().database_url
    return url.replace("postgresql+psycopg://", "postgresql://").replace("postgres+psycopg://", "postgresql://")


class Subscriber:
    """One connected client: an asyncio queue plus the loop it belongs to."""

    __slots__ = ("dropped", "loop", "overflowed", "queue")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.dropped = 0
        #: Set when an event was dropped for want of room; the consumer clears it once it
        #: has told its client there is a gap to backfill.
        self.overflowed = False

    def offer(self, event: dict[str, Any]) -> None:
        """Runs on the subscriber's own loop (``call_soon_threadsafe``)."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            self.overflowed = True
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)


class LiveBus:
    """Owns the listener thread and the set of subscribers.

    Started from the app's lifespan and stopped with it.  Starting it twice is a no-op, and
    a failure to reach the database is logged and retried rather than raised: the API must
    keep serving reads when the live feed cannot connect.
    """

    def __init__(self, database_url: str | None = None) -> None:
        self._dsn = dsn(database_url)
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_event_at: float | None = None
        self.events_seen = 0
        #: The last ``seq`` handed out; see :meth:`publish`.
        self._seq = 0
        self._seq_lock = threading.Lock()
        #: How many times the listener has had to reconnect (each one a possible gap).
        self.reconnects = 0

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-bus", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.connected = False

    # ------------------------------------------------------------------ subscribers

    def subscribe(self, loop: asyncio.AbstractEventLoop | None = None) -> Subscriber:
        subscriber = Subscriber(loop or asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan one event out to every subscriber, on each one's own loop.

        The event gets a ``seq`` first — one number per published event, increasing — so
        that a consumer caching per event can tell a second edit to a call from a repeat of
        the first.  A copy is stamped, not the caller's dict.
        """
        with self._seq_lock:
            self._seq += 1
            event = {**event, "seq": self._seq}
        self.events_seen += 1
        self.last_event_at = time.time()
        with self._lock:
            targets = list(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, event)
            except RuntimeError:  # the loop has closed under a disconnected client
                self.unsubscribe(subscriber)

    # ------------------------------------------------------------------ the thread

    def _run(self) -> None:
        backoff = RECONNECT_MIN_S
        listened_before = False
        while not self._stop.is_set():
            try:
                with psycopg.connect(self._dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {EVENT_CHANNEL}")
                    self.connected = True
                    backoff = RECONNECT_MIN_S
                    log.info("live: listening on %s", EVENT_CHANNEL)
                    if listened_before:
                        # Everything notified while we were away went to nobody.  Say so,
                        # so every client refetches the window rather than keeping a hole.
                        self.reconnects += 1
                        self.publish({"event": GAP_EVENT, "reason": "reconnected"})
                    listened_before = True
                    while not self._stop.is_set():
                        for notification in conn.notifies(timeout=1.0):
                            self._handle(notification.payload)
                            if self._stop.is_set():
                                break
            except Exception as exc:  # noqa: BLE001 - the feed must not take the API down
                self.connected = False
                if self._stop.is_set():
                    break
                log.warning("live: listener disconnected (%s); retrying in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(RECONNECT_MAX_S, backoff * 2)
        self.connected = False

    def _handle(self, payload: str) -> None:
        try:
            event = json.loads(payload)
        except ValueError:
            log.warning("live: ignoring unparseable payload %r", payload[:120])
            return
        if not isinstance(event, dict) or "event" not in event:
            return
        self.publish(event)


#: The process-wide bus.  ``app.main`` starts and stops it in the lifespan.
bus = LiveBus()


def iter_events(subscriber: Subscriber) -> Iterator[dict[str, Any]]:  # pragma: no cover - helper
    while True:
        yield subscriber.queue.get_nowait()


__all__ = ["GAP_EVENT", "QUEUE_MAXSIZE", "LiveBus", "Subscriber", "bus", "dsn"]
