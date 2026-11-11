"""The ingest worker: inotify plus a periodic full scan (docs/ARCHITECTURE.md 5.1 step 1).

Two mechanisms on purpose.  watchdog reacts in milliseconds but misses everything that
happened while the process was down (and, on some filesystems, everything full stop), so
a full ``ingest_tree`` runs every ``INGEST_SCAN_MINUTES`` as the backstop.  The scan skips
paths the ledger already settled, so the belt costs almost nothing next to the braces.

**Several roots, reloaded live.**  The watcher used to take one directory — ``AUDIO_ROOT``,
read from the environment at start-up — which made "point Keyup at the second recorder's
spool as well" a config edit plus a restart, and made ``AUDIO_ROOT`` the one piece of ingest
configuration deliberately excluded from the admin page.  It now runs one set of watches per
enabled ``ingest_sources`` row of kind ``watch_dir`` and re-reads that table every
:data:`RELOAD_SECONDS`, so adding, editing, disabling or removing a directory takes effect
without touching the process.  ``AUDIO_ROOT`` is still the archive root every
``calls.file_path`` is relative to — it seeds the first row and is the fallback when the
table has nothing enabled — a directory outside it is simply recorded with an absolute path.

**No root is enumerated whole.**  Under each of those roots the same pruning the scan does
applies to the watches: the root gets a *shallow* watch, each surviving child directory gets
a recursive one, and ``uploads/`` and every hidden directory get none.  inotify needs a watch
per directory and watchdog adds them by walking at schedule time, so a recursive watch on an
archive root would try to cover ``uploads/`` — a day directory times 256 buckets times every
day held — and run into ``fs.inotify.max_user_watches`` for a subtree the push endpoint has
already ingested.  A day directory that appears later is adopted into the source that owns
its root (:meth:`IngestWatcher._adopt`).

Run it with ``keyup ingest watch`` or ``python -m app.ingest.watcher``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import (
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.ingest import sources as source_store
from app.ingest.parser import is_recording_name
from app.ingest.service import (
    await_trunk_recorder_call,
    ingest_file,
    ingest_tree,
    iter_recordings,
    scan_skip_dirs,
    wait_until_settled,
)
from app.ingest.sources import WatchDir
from app.ingest.tr_parser import SIDECAR_SUFFIX, is_trunk_recorder_call

log = logging.getLogger("app.ingest.watcher")

#: how long a queue worker blocks before checking the stop flag again
POLL_SECONDS = 1.0

#: how often the watcher asks the database whether its set of directories has changed.
#: Short enough that saving a source feels immediate, long enough that the read — one
#: indexed lookup of a single integer in the common case — is free.
RELOAD_SECONDS = 30

#: what the queue carries: the file, and which source row it came from (``None`` for the
#: fallback root, which is nobody's row).
WorkItem = tuple[Path, int | None]


def _as_str(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


def _is_recording(path: str) -> bool:
    """True for anything that might be (part of) a call: audio, or a sidecar.

    A ``.json`` is a trigger in its own right because an encrypted trunk-recorder call is
    *only* a sidecar — there is no audio event to wait for — and because the sidecar is the
    last of a call's files to land, so seeing it means the call is complete (§1.7).
    """
    if is_recording_name(path):
        return True
    return path.lower().endswith(SIDECAR_SUFFIX) and is_trunk_recorder_call(path)


class RecordingEventHandler(FileSystemEventHandler):
    """Put every new or moved recording — audio or sidecar — on the work queue.

    sdrtrunk creates the file at call start and closes it at call end, and some setups
    write to a temporary name and rename, so both ``created`` and ``moved`` matter.  The
    handler does no work itself — a watchdog callback must return promptly or events are
    dropped.

    One handler per watched directory, each carrying its ``source_id``, so the drain thread
    knows whose dialect settings (trunk-recorder's WAV/M4A preference, its sidecar wait) to
    apply to the file and whose row to blame when it fails.  ``on_new_dir`` is set on the
    handler watching a root shallowly, where a directory event means a subtree nobody is
    watching yet.
    """

    def __init__(
        self,
        work: queue.Queue[WorkItem],
        source_id: int | None = None,
        *,
        on_new_dir: Callable[[Path], None] | None = None,
    ) -> None:
        self.work = work
        self.source_id = source_id
        self.on_new_dir = on_new_dir

    def _offer(self, raw_path: str | bytes) -> None:
        path = _as_str(raw_path)
        if _is_recording(path):
            self.work.put((Path(path), self.source_id))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            # The root is watched non-recursively (see ``IngestWatcher._schedule_root``), so
            # a new day directory is a new subtree nobody is watching yet.
            if self.on_new_dir is not None:
                self.on_new_dir(Path(_as_str(event.src_path)))
            return
        if isinstance(event, FileCreatedEvent):
            self._offer(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory and isinstance(event, FileMovedEvent):
            self._offer(event.dest_path)

    def on_closed(self, event: FileSystemEvent) -> None:
        # inotify's IN_CLOSE_WRITE, where the platform provides it: the strongest signal
        # that a recording is complete.  The settle check still runs, so a double offer
        # is harmless (ingest_file is idempotent on file_path).
        if not event.is_directory:
            self._offer(event.src_path)


class IngestWatcher:
    """Owns the observer, the queue workers and the periodic-scan scheduler.

    ``root`` is the archive root: what ``calls.file_path`` is relative to, and — in
    single-root mode — the only directory watched.  Passing it explicitly turns the database
    reload *off*, which is what a test or a one-off ``keyup ingest watch /some/dir`` wants:
    "watch exactly this" must not be quietly overruled by a table.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        root: Path | None = None,
        use_sources: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.root = Path(root) if root is not None else Path(self.settings.audio_root)
        #: read the directories from ``ingest_sources``?  Default: yes, unless the caller
        #: named a root, in which case they meant that one.
        self.use_sources = use_sources if use_sources is not None else root is None
        self.work: queue.Queue[WorkItem] = queue.Queue()
        self.stop = threading.Event()
        self.observer: Observer | None = None  # type: ignore[valid-type]
        self.scheduler: BackgroundScheduler | None = None
        self.threads: list[threading.Thread] = []
        self._scan_lock = threading.Lock()
        #: source id -> (the row as we applied it, every watchdog watch it owns).  There is
        #: more than one per source — a shallow watch on the root, a recursive watch per
        #: child — and adoption adds to the list.  The spec is kept so a reload can tell
        #: "this row changed" from "this row is the same".
        self._watches: dict[int, tuple[WatchDir, list[Any]]] = {}
        #: the watches covering ``AUDIO_ROOT`` when no row asked for anything.  Held apart
        #: from ``_watches`` because they belong to no source, and dropped the moment the
        #: table does have rows — otherwise the seed landing (or a reload that failed once
        #: and then succeeded) would leave the archive root watched twice.
        self._fallback_watches: list[Any] = []
        #: the generation the current set of watches was built from; -1 = never loaded
        self._version = -1
        #: does the table contain any ``watch_dir`` row at all, enabled or not?  This is what
        #: separates "nobody has configured sources" (fall back to AUDIO_ROOT) from "the
        #: operator has switched them all off" (watch nothing, which is what they asked for).
        self._watch_dir_rows = 0
        self._reload_lock = threading.Lock()

    # -- the roots ---------------------------------------------------------------------

    def active_roots(self) -> list[tuple[Path, int | None]]:
        """The directories being watched and scanned right now, with their source ids.

        Falls back to ``AUDIO_ROOT`` when the table names none.  A deployment whose operator
        disabled every source has asked for nothing to be watched; one that has simply never
        written a row has not, and must keep ingesting exactly as it did before.
        """
        if not self.use_sources or self._version < 0:
            return [(self.root, None)]
        if self._watches:
            return [(spec.path, sid) for sid, (spec, _) in sorted(self._watches.items())]
        return [] if self._watch_dir_rows else [(self.root, None)]

    def _spec(self, source_id: int | None) -> WatchDir | None:
        entry = self._watches.get(source_id) if source_id is not None else None
        return entry[0] if entry else None

    def reload_sources(self, *, force: bool = False) -> bool:
        """Bring the watchdog watches in line with the table.  Returns whether anything moved.

        Cheap when nothing has changed: one read of ``ingest_config_version``.  Never raises
        — an unreachable database leaves the watcher running on the directories it already
        has, which is far better than a watcher that stops watching because a query failed.
        """
        if not self.use_sources:
            return False
        if not self._reload_lock.acquire(blocking=False):
            return False
        try:
            session = SessionLocal()
            try:
                version = source_store.version(session)
                if not force and version == self._version:
                    return False
                self._watch_dir_rows = source_store.count_watch_dirs(session)
                wanted = {spec.id: spec for spec in source_store.watch_dirs(session)}
                changed = self._apply(session, wanted)
                session.commit()
                self._version = version
                return changed
            except Exception:  # noqa: BLE001 - a failed reload must not stop the watcher
                log.exception("ingest: could not reload the watch directories")
                session.rollback()
                return False
            finally:
                session.close()
        finally:
            self._reload_lock.release()

    def _apply(self, session: Any, wanted: dict[int, WatchDir]) -> bool:
        """Unschedule what has gone, schedule what is new.  Caller holds the reload lock."""
        changed = False
        if self._watch_dir_rows and self._fallback_watches:
            # The table has spoken — by naming directories, or by disabling all of them.
            # Either way the guess we made in its absence is now a duplicate.
            self._clear_fallback()
            changed = True
        for source_id in set(self._watches) - set(wanted):
            self._unschedule(source_id)
            changed = True
        for source_id, spec in wanted.items():
            existing = self._watches.get(source_id)
            if existing is not None and existing[0] == spec:
                continue  # same row, same path, same knobs — leave the watch alone
            if existing is not None:
                # An edited row is a new watch, not a mutated one: watchdog has no way to
                # repoint a watch, and dropping it first keeps the two from both firing.
                self._unschedule(source_id)
            state, detail = source_store.path_state(spec.path)
            if state != "ok":
                log.warning("ingest: source %s (%s) is %s", spec.label, spec.path, state)
                source_store.record_error(session, source_id, f"{state}: {detail}")
                changed = True
                continue
            source_store.record_error(session, source_id, None)
            # Registered before the watches exist, and extended in place afterwards: a
            # watch can fire the instant it is scheduled, and ``_adopt`` needs somewhere to
            # put what it schedules and a spec to read the source's preferences from.
            watches: list[Any] = []
            self._watches[source_id] = (spec, watches)
            watches.extend(
                self._schedule_root(spec.path, source_id, recursive=spec.recursive)
            )
            log.info("ingest: watching %s (%s)", spec.path, spec.label)
            changed = True
        return changed

    def _unschedule(self, source_id: int) -> None:
        spec, watches = self._watches.pop(source_id)
        self._drop(watches)
        log.info("ingest: stopped watching %s (%s)", spec.path, spec.label)

    def _clear_fallback(self) -> None:
        self._drop(self._fallback_watches)
        self._fallback_watches = []
        log.info("ingest: stopped the fallback watch on %s", self.root)

    def _drop(self, watches: list[Any]) -> None:
        if self.observer is None:
            return
        for watch in watches:
            with contextlib.suppress(Exception):  # already gone, or the observer is stopping
                self.observer.unschedule(watch)

    # -- what is watched ---------------------------------------------------------------

    def _skipped(self, name: str) -> bool:
        return name.startswith(".") or name in scan_skip_dirs(self.settings)

    def _adopt_for(self, root: Path, source_id: int | None) -> Callable[[Path], None]:
        """The ``on_new_dir`` callback for one root, closed over whose root it is."""

        def _on_new_dir(directory: Path) -> None:
            self._adopt(directory, root=root, source_id=source_id)

        return _on_new_dir

    def _schedule_root(
        self, path: Path, source_id: int | None, *, recursive: bool = True
    ) -> list[Any]:
        """Watch one root shallowly and each of its subtrees, minus the pruned ones.

        Not ``recursive=True`` on the root, for the same reason the scan no longer walks it
        whole: inotify needs a watch **per directory**, and watchdog adds them by walking the
        tree at schedule time.  Under the archive layout ``uploads/`` is a day directory
        times 256 buckets times every day the archive holds — of the order of 100k
        directories, past ``fs.inotify.max_user_watches`` on a stock kernel, for a subtree
        whose files the push endpoint has already ingested by the time they exist.

        Everything else keeps the recursive watch it always had, and a day directory created
        later is adopted by :meth:`_adopt`.  A source whose row says ``recursive=False`` gets
        the shallow watch and nothing else: it asked for one directory, not a tree.
        """
        if self.observer is None:  # INGEST_WATCH=false: scanned, not watched
            return []
        if not recursive:
            return [
                self.observer.schedule(
                    RecordingEventHandler(self.work, source_id), str(path), recursive=False
                )
            ]
        handler = RecordingEventHandler(
            self.work, source_id, on_new_dir=self._adopt_for(path, source_id)
        )
        watches = [self.observer.schedule(handler, str(path), recursive=False)]
        skipped = []
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if self._skipped(entry.name):
                    skipped.append(entry.name)
                    continue
                watches.append(self.observer.schedule(handler, entry.path, recursive=True))
        if skipped:
            log.info("not watching %s under %s", ", ".join(sorted(skipped)), path)
        return watches

    def _adopt(self, directory: Path, *, root: Path, source_id: int | None) -> None:
        """Start watching a directory that appeared under a root, and catch it up.

        The watch is scheduled *after* the directory exists, so anything written into it in
        between would otherwise wait for the next periodic scan; the walk here closes that
        window.  It is bounded by construction — the subtree is seconds old — and
        ``ingest_file`` is idempotent on ``file_path``, so a doubled offer costs a ledger
        lookup.
        """
        if self.observer is None or directory.parent != root or self._skipped(directory.name):
            return
        try:
            watch = self.observer.schedule(
                RecordingEventHandler(
                    self.work, source_id, on_new_dir=self._adopt_for(root, source_id)
                ),
                str(directory),
                recursive=True,
            )
        except OSError:  # a directory that vanished again is not an error
            log.warning("could not watch the new directory %s", directory, exc_info=True)
            return
        self._remember(source_id, watch)
        log.info("watching new directory %s", directory)
        spec = self._spec(source_id)
        prefer_audio = (spec.tr_prefer_audio if spec else None) or self.settings.tr_prefer_audio
        for path in iter_recordings(directory, prefer_audio=prefer_audio):
            self.work.put((path, source_id))

    def _remember(self, source_id: int | None, watch: Any) -> None:
        """Hold on to an adopted watch so unscheduling the source takes it with it."""
        if source_id is None:
            self._fallback_watches.append(watch)
            return
        entry = self._watches.get(source_id)
        if entry is not None:
            entry[1].append(watch)

    # -- the queue ---------------------------------------------------------------------

    def _drain(self) -> None:
        session = SessionLocal()
        try:
            while not self.stop.is_set():
                try:
                    path, source_id = self.work.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                try:
                    spec = self._spec(source_id)
                    if is_trunk_recorder_call(path):
                        # One call fires up to three events (wav, m4a, json).  This waits
                        # for the sidecar and collapses all of them onto one path, so the
                        # ledger sees one file and the call is written with its exact times.
                        # Two spools may not be configured alike, so a source's own
                        # preference wins over the deployment's default when it has one.
                        path = await_trunk_recorder_call(
                            path,
                            prefer_audio=(
                                (spec.tr_prefer_audio if spec else None)
                                or self.settings.tr_prefer_audio
                            ),
                            wait_seconds=(
                                (spec.tr_sidecar_wait_seconds if spec else None)
                                if (spec and spec.tr_sidecar_wait_seconds is not None)
                                else self.settings.tr_sidecar_wait_seconds
                            ),
                        )
                    if not wait_until_settled(
                        path, settle_seconds=self.settings.ingest_settle_seconds
                    ):
                        log.debug("never settled, leaving to the next scan: %s", path)
                        continue
                    result = ingest_file(
                        session, path, settings=self.settings, audio_root=self.root, run_id=None
                    )
                    if result.status not in ("skipped",):
                        log.info("watch %s %s", result.status, result.file_path)
                    if source_id is not None and result.status == "ingested":
                        source_store.touch_activity(session, source_id)
                        session.commit()
                except Exception:  # noqa: BLE001 - a worker thread must never die
                    log.exception("watch worker failed on %s", path)
                    session.rollback()
                finally:
                    self.work.task_done()
        finally:
            session.close()

    # -- the periodic scan -------------------------------------------------------------

    def periodic_scan(self) -> None:
        """Full ``ingest_tree`` over every active root; never two at once."""
        if not self._scan_lock.acquire(blocking=False):
            log.warning("previous scan still running, skipping this tick")
            return
        try:
            for directory, source_id in self.active_roots():
                state, detail = source_store.path_state(directory)
                if state != "ok":
                    log.warning("scan: skipping %s (%s)", directory, state)
                    self._note_error(source_id, f"{state}: {detail}")
                    continue
                try:
                    summary = ingest_tree(
                        SessionLocal,
                        directory,
                        workers=self.settings.ingest_workers,
                        settings=self.settings,
                        # The archive root, not the directory being scanned: file_path stays
                        # relative to AUDIO_ROOT wherever the recording was found.
                        audio_root=self.root,
                        kind="watch",
                    )
                except Exception:  # noqa: BLE001 - one bad root must not skip the others
                    log.exception("periodic scan failed for %s", directory)
                    self._note_error(source_id, "scan failed; see the worker log")
                    continue
                log.info(
                    "scan %s: seen=%d new=%d duplicate=%d skipped=%d failed=%d in %.1fs (walk %s)",
                    directory,
                    summary.files_seen,
                    summary.files_new,
                    summary.files_duplicate,
                    summary.files_skipped,
                    summary.files_failed,
                    summary.elapsed_s,
                    summary.walk.as_dict() if summary.walk is not None else "n/a",
                )
                self._note_error(source_id, None)
        finally:
            self._scan_lock.release()

    def _note_error(self, source_id: int | None, message: str | None) -> None:
        """Record (or clear) a source's last error from the scan.  Best effort."""
        if source_id is None:
            return
        session = SessionLocal()
        try:
            source_store.record_error(session, source_id, message)
            session.commit()
        except Exception:  # noqa: BLE001 - a status column is never worth a failed scan
            log.debug("ingest: could not record the source error", exc_info=True)
            session.rollback()
        finally:
            session.close()

    # -- lifecycle ---------------------------------------------------------------------

    def start(self, *, workers: int = 2, initial_scan: bool = True) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        for i in range(max(1, workers)):
            thread = threading.Thread(target=self._drain, name=f"ingest-worker-{i}", daemon=True)
            thread.start()
            self.threads.append(thread)

        if self.settings.ingest_watch:
            self.observer = Observer()
            self.observer.start()
        else:
            log.info("INGEST_WATCH=false — periodic scan only")

        if self.use_sources:
            # Forced: the first load has nothing to compare against, and a deployment whose
            # version happens to be 0 still needs its rows.
            self.reload_sources(force=True)
            if not self._watches and not self._watch_dir_rows:
                log.info("no watch_dir source is configured; falling back to %s", self.root)
                self._fallback_watches = self._schedule_root(self.root, None)
        else:
            self._fallback_watches = self._schedule_root(self.root, None)
            log.info("watching %s", self.root)

        self.scheduler = BackgroundScheduler(timezone=self.settings.tz)
        self.scheduler.add_job(
            self.periodic_scan,
            "interval",
            minutes=self.settings.ingest_scan_minutes,
            id="ingest_scan",
            max_instances=1,
            coalesce=True,
        )
        if self.use_sources:
            self.scheduler.add_job(
                self.reload_sources,
                "interval",
                seconds=RELOAD_SECONDS,
                id="ingest_sources_reload",
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()
        log.info("full rescan every %d min", self.settings.ingest_scan_minutes)

        if initial_scan:
            # Catch up on whatever arrived while the worker was down, before waiting.
            self.periodic_scan()

    def shutdown(self) -> None:
        self.stop.set()
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
        for thread in self.threads:
            thread.join(timeout=5)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self.stop.wait(POLL_SECONDS):
                pass
        finally:
            self.shutdown()


def run_watcher(settings: Settings | None = None, *, root: Path | None = None) -> None:
    """Block until SIGINT/SIGTERM, ingesting everything that lands under the active roots."""
    watcher = IngestWatcher(settings, root=root)

    def _signal(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
        log.info("signal %s — shutting down", signum)
        watcher.stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # A handler can only be installed from the main thread; when the watcher is
        # embedded elsewhere the caller owns the shutdown signal.
        with contextlib.suppress(ValueError):
            signal.signal(sig, _signal)
    watcher.run_forever()


def main() -> None:  # pragma: no cover - process entry point
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )
    run_watcher(settings)


if __name__ == "__main__":  # pragma: no cover
    main()
