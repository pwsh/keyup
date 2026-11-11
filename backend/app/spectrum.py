"""The spectrum bus: last-frame-per-tuner cache and an in-process SSE fan-out (§4.8).

A recorder streaming a live spectrum posts small FFT frames many times a second.  Unlike
the call push or the status report, these are big, ephemeral and worthless a moment later:
persisting them or routing them through Postgres ``LISTEN/NOTIFY`` (whose payload is capped
at 8 KB) would be wrong on both counts.  So the spectrum path never touches the database.

Instead this module keeps, entirely in the API process:

* a **last-frame cache** keyed by ``(instance_id, tuner_key)`` — one frame per tuner, so a
  page that opens the view mid-stream sees something at once rather than a blank wait;
* a set of **subscribers**, each an :class:`asyncio.Queue` bound to one ``(instance, tuner)``
  and to the event loop it was created on, exactly as :mod:`app.live` fans the DB bus out.

The design mirrors :class:`app.live.LiveBus` (a bounded queue per client, oldest dropped
under back-pressure, publish from any thread via ``call_soon_threadsafe``) but owns no
thread and no connection: frames arrive by a direct :meth:`SpectrumHub.ingest` call from
the ingest endpoint's request handler, not from a listener.  When nobody is subscribed the
hub only updates the cache and fans out to no one, so a tuner that is emitting costs
nothing while no operator is looking at it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from typing import Any

log = logging.getLogger("app.spectrum")

#: How many frames one subscriber may fall behind before its oldest are dropped.  A frame is
#: only useful while it is current, so a slow client should lose old frames, not stall the
#: publisher — one or two in flight is plenty.
QUEUE_MAXSIZE = 4

#: A defensive cap on distinct ``(instance, tuner)`` pairs the cache will hold, so a flood of
#: bogus keys cannot grow it without bound even though the ingest path already ignores tuners
#: keyup does not know.  Oldest-inserted keys are evicted first.
MAX_CACHED = 256

#: A frame is a small JSON-able mapping — see ``SpectrumFrameIn`` for the shape.
Frame = dict[str, Any]


class SpectrumSubscriber:
    """One connected spectrum viewer: a bounded queue, its loop, and the tuner it wants."""

    __slots__ = ("dropped", "instance", "loop", "queue", "tuner")

    def __init__(self, loop: asyncio.AbstractEventLoop, instance: str, tuner: str) -> None:
        self.loop = loop
        self.instance = instance
        self.tuner = tuner
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.dropped = 0

    def offer(self, frame: Frame) -> None:
        """Enqueue a frame, dropping the oldest if full.  Runs on the subscriber's loop."""
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(frame)


class SpectrumHub:
    """Owns the last-frame cache and the set of subscribers.  Process-wide, no I/O."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], Frame] = {}
        self._subscribers: set[SpectrumSubscriber] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ cache

    def latest(self, instance: str, tuner: str) -> Frame | None:
        """The last frame seen for one tuner, or ``None``."""
        with self._lock:
            return self._cache.get((instance, tuner))

    def store(self, frame: Frame) -> None:
        key = (frame["instance_id"], frame["tuner_key"])
        with self._lock:
            # Re-insert last so eviction is oldest-first; cap the total keys held.
            self._cache.pop(key, None)
            self._cache[key] = frame
            while len(self._cache) > MAX_CACHED:
                oldest = next(iter(self._cache))
                del self._cache[oldest]

    # ------------------------------------------------------------------ subscribers

    def subscribe(
        self, instance: str, tuner: str, loop: asyncio.AbstractEventLoop | None = None
    ) -> SpectrumSubscriber:
        subscriber = SpectrumSubscriber(loop or asyncio.get_running_loop(), instance, tuner)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: SpectrumSubscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------------ publish

    def publish(self, frame: Frame) -> None:
        """Fan a frame out to every subscriber watching its tuner, on each one's loop."""
        instance = frame["instance_id"]
        tuner = frame["tuner_key"]
        with self._lock:
            targets = [
                s for s in self._subscribers if s.instance == instance and s.tuner == tuner
            ]
        for subscriber in targets:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, frame)
            except RuntimeError:  # the loop has closed under a disconnected client
                self.unsubscribe(subscriber)

    def ingest(self, frame: Frame) -> None:
        """Cache a frame as the tuner's latest and publish it to subscribers."""
        self.store(frame)
        self.publish(frame)


#: The process-wide hub.  Import and use directly; it needs no lifespan.
hub = SpectrumHub()


__all__ = ["MAX_CACHED", "QUEUE_MAXSIZE", "SpectrumHub", "SpectrumSubscriber", "hub"]
