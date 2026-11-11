"""Rebuilding the transcription router when the operator changes the backends (§5.2).

The router is built once, at worker start-up, from whatever backends were configured then.
That was fine while "configured" meant an environment variable — you had to restart the
process to change one anyway — and is exactly wrong now that the admin page writes
``stt_backends`` rows: an operator who adds a GPU server wants it transcribing, not waiting
for someone to remember to restart a service.

Doing it safely is the whole content of this module.  A router is not a value: it owns a
semaphore per backend that bounds concurrency, and threads are inside
:meth:`~app.stt.router.BackendRouter.transcribe` right now holding those permits.  Mutating
the live router's backend list under them would corrupt exactly the accounting the
semaphores exist to keep.  So nothing is ever mutated:

* :class:`RouterHandle` is what every consumer holds — the job runner, the queue gauge, the
  stats publisher.  It forwards to whichever router is current.
* A reload builds a **new** router and rebinds one attribute (:meth:`RouterHandle.swap`).
  Attribute assignment is atomic, so a thread either gets the old router or the new one and
  never a half-built anything.
* Attempts already in flight finish on the old router.  They release its permits, its
  counters record what they did, and it is then unreferenced and collected.  Nothing is
  cancelled, no job is lost, and the new router simply starts with fresh permits — briefly
  allowing up to two generations' worth of concurrency against one backend, which is the
  price of never interrupting work in progress and is bounded by the worker's thread count
  regardless.

The counters *do* reset on a swap, and that is honest rather than convenient: the numbers
described a router that no longer exists.  ``stt_backend_stats.config_version`` records
which configuration a row's counters belong to, so the page can say the worker has picked
the edit up instead of leaving the operator to infer it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from . import config_store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .router import BackendRouter
    from .types import TranscribeOptions, TranscriptResult

log = logging.getLogger(__name__)

__all__ = ["RouterHandle", "RouterReloader"]


class RouterHandle:
    """A stable reference to the router of the moment.

    Duck-types the parts of :class:`~app.stt.router.BackendRouter` its consumers use, so it
    can be passed anywhere a router is expected: the job runner calls :meth:`transcribe`,
    :mod:`app.ingest.flow`'s queue gauge and :class:`~app.stt.stats_store.StatsPublisher`
    call :meth:`stats`, and the publisher reads :attr:`config_version` to stamp its rows.
    """

    def __init__(self, router: BackendRouter, *, config_version: int = 0) -> None:
        self._router = router
        self.config_version = config_version
        self._on_health_change: Callable[[], None] | None = None
        #: guards *replacement*, not use: two reload ticks must not both build and swap.
        #: Readers take no lock at all — they read one attribute.
        self._swap_lock = threading.Lock()

    # -- the current router -------------------------------------------------------------

    @property
    def current(self) -> BackendRouter:
        """The router in force right now.  Re-read it per call; never cache it."""
        return self._router

    @property
    def backends(self) -> list[Any]:
        return self._router.backends

    @property
    def on_health_change(self) -> Callable[[], None] | None:
        return self._router.on_health_change

    @on_health_change.setter
    def on_health_change(self, listener: Callable[[], None] | None) -> None:
        # Remembered on the handle as well, so a router built by a later reload gets it
        # too — otherwise the first swap would silently stop the "a backend just went
        # down" wake-up and the page would go back to lagging a whole publish interval.
        self._on_health_change = listener
        self._router.on_health_change = listener

    # -- what consumers call ------------------------------------------------------------

    def transcribe(
        self, path: Path, options: TranscribeOptions, *, prefer: str | None = None
    ) -> TranscriptResult:
        """Route one job.  Resolved against the current router *once*, at entry.

        Which is the point: a swap that happens while this call is inside a backend cannot
        reach it.  The attempt finishes, releases its permit and updates its counters on
        the router that started it.
        """
        return self._router.transcribe(path, options, prefer=prefer)

    def stats(self) -> dict[str, dict[str, Any]]:
        return self._router.stats()

    def health_report(self, *, force: bool = False) -> dict[str, Any]:
        return self._router.health_report(force=force)

    def invalidate_health(self, name: str | None = None) -> None:
        self._router.invalidate_health(name)

    # -- the swap -----------------------------------------------------------------------

    def swap(self, router: BackendRouter, *, config_version: int) -> BackendRouter:
        """Install ``router`` and return the one it replaced.

        The order matters: the health listener is attached *before* the new router becomes
        visible, so no thread can pick it up in a state where a health flip goes nowhere.
        """
        with self._swap_lock:
            previous = self._router
            listener = getattr(self, "_on_health_change", None) or previous.on_health_change
            router.on_health_change = listener
            self._router = router
            self.config_version = config_version
            return previous


class RouterReloader:
    """Polls ``stt_config_version`` and swaps a freshly built router into a handle.

    Given to :class:`~app.stt.stats_store.StatsPublisher` as its per-tick callback rather
    than run on a thread of its own: the publisher already wakes every ten seconds with a
    session in hand, the check is one indexed read, and hanging the reload off the same tick
    means a swap and the publish that reports it happen in that order, so the row the page
    reads already carries the new ``config_version``.
    """

    def __init__(
        self,
        handle: RouterHandle,
        session_factory: Callable[[], Session],
        build: Callable[[], BackendRouter],
    ) -> None:
        self.handle = handle
        self.session_factory = session_factory
        self.build = build
        #: reloads that actually rebuilt something — asserted by the tests, and a cheap
        #: answer to "has this worker ever picked anything up"
        self.swaps = 0

    def read_version(self) -> int:
        session = self.session_factory()
        try:
            return config_store.version(session)
        finally:
            session.close()

    def poll(self) -> bool:
        """One check.  Returns whether a swap happened.  Never raises.

        A build that fails — an operator saved a backend whose kind needs a package this
        host has not got — leaves the *old* router in place and logs.  Keeping the
        transcription that currently works is worth more than applying every edit, and the
        version is deliberately not marked applied, so the next tick tries again and the
        page keeps showing the worker on the older configuration.
        """
        try:
            wanted = self.read_version()
        except Exception:  # noqa: BLE001 - an unreadable settings table must not stop work
            log.exception("stt: could not read the configuration version")
            return False
        if wanted == self.handle.config_version:
            return False
        try:
            router = self.build()
        except Exception:  # noqa: BLE001 - see the docstring
            log.exception(
                "stt: configuration v%s could not be built; staying on v%s",
                wanted,
                self.handle.config_version,
            )
            return False
        previous = self.handle.swap(router, config_version=wanted)
        self.swaps += 1
        log.info(
            "stt: reloaded backends at config v%s: %s (was %s)",
            wanted,
            ", ".join(b.name for b in router.backends) or "none",
            ", ".join(b.name for b in previous.backends) or "none",
        )
        return True
