"""The watcher across several roots, reloaded from the table while it runs.

The old watcher took one directory at start-up and that was that; the claim now is that
adding, disabling and removing an ``ingest_sources`` row all take effect on a live watcher
without restarting it.  These tests drive :meth:`IngestWatcher.reload_sources` directly
rather than waiting on the thirty-second scheduler job — the tick is APScheduler's business,
what has to be proved is that a tick does the right thing.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from sqlalchemy import select

from app.ingest import sources as source_store
from app.models import Call, IngestSource
from tests.conftest import requires_db
from tests.test_parser import PLAIN

pytestmark = requires_db


def add_source(session, label: str, path: Path, *, enabled: bool = True) -> IngestSource:  # noqa: ANN001
    row = IngestSource(kind="watch_dir", label=label, path=str(path), enabled=enabled)
    session.add(row)
    source_store.bump(session)
    session.commit()
    return row


def test_an_explicit_root_turns_the_table_off(session, settings, tmp_path) -> None:  # noqa: ANN001, ARG001
    """"Watch exactly this" must not be quietly overruled by rows in a database."""
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "audio"
    tree.mkdir()
    watcher = IngestWatcher(settings, root=tree)
    assert watcher.use_sources is False
    assert watcher.active_roots() == [(tree, None)]
    assert watcher.reload_sources(force=True) is False


def test_an_empty_table_falls_back_to_the_audio_root(session, settings, tmp_path) -> None:  # noqa: ANN001, ARG001
    """A deployment that has never configured a source keeps ingesting exactly as before."""
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "audio"
    tree.mkdir()
    local = settings.model_copy(update={"audio_root": tree})
    watcher = IngestWatcher(local)
    watcher.reload_sources(force=True)
    assert watcher.active_roots() == [(tree, None)]


def test_every_source_disabled_means_nothing_is_watched(session, settings, tmp_path) -> None:  # noqa: ANN001
    """…but switching them all off is an instruction, not an absence."""
    from app.ingest.watcher import IngestWatcher

    spool = tmp_path / "spool"
    spool.mkdir()
    add_source(session, "off", spool, enabled=False)
    watcher = IngestWatcher(settings.model_copy(update={"audio_root": tmp_path / "audio"}))
    watcher.reload_sources(force=True)
    assert watcher.active_roots() == []


def test_sources_are_added_and_removed_without_a_restart(session, settings, tmp_path) -> None:  # noqa: ANN001
    from app.ingest.watcher import IngestWatcher

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    add_source(session, "one", one)

    watcher = IngestWatcher(settings.model_copy(update={"audio_root": tmp_path / "audio"}))
    watcher.start(workers=1, initial_scan=False)
    try:
        assert [path for path, _ in watcher.active_roots()] == [one]

        row = add_source(session, "two", two)
        assert watcher.reload_sources() is True
        assert sorted(path for path, _ in watcher.active_roots()) == sorted([one, two])

        # A reload with nothing new must not churn the watches.
        assert watcher.reload_sources() is False

        row.enabled = False
        source_store.bump(session)
        session.commit()
        assert watcher.reload_sources() is True
        assert [path for path, _ in watcher.active_roots()] == [one]

        session.delete(session.get(IngestSource, row.id))
        source_store.bump(session)
        session.commit()
        watcher.reload_sources()
        assert [path for path, _ in watcher.active_roots()] == [one]
    finally:
        watcher.shutdown()


def test_an_unreadable_source_is_skipped_and_recorded(session, settings, tmp_path) -> None:  # noqa: ANN001
    """A path that is not there is a red row, not a crashed watcher."""
    from app.ingest.watcher import IngestWatcher

    good = tmp_path / "good"
    good.mkdir()
    add_source(session, "good", good)
    bad = add_source(session, "bad", tmp_path / "never-mounted")

    watcher = IngestWatcher(settings.model_copy(update={"audio_root": tmp_path / "audio"}))
    watcher.start(workers=1, initial_scan=False)
    try:
        assert [path for path, _ in watcher.active_roots()] == [good]
        session.expire_all()
        row = session.get(IngestSource, bad.id)
        assert row.last_error.startswith("missing:")
        assert row.last_error_at is not None
    finally:
        watcher.shutdown()


def test_a_repointed_source_moves_its_watch(session, settings, tmp_path) -> None:  # noqa: ANN001
    from app.ingest.watcher import IngestWatcher

    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    row = add_source(session, "spool", before)

    watcher = IngestWatcher(settings.model_copy(update={"audio_root": tmp_path / "audio"}))
    watcher.start(workers=1, initial_scan=False)
    try:
        assert [path for path, _ in watcher.active_roots()] == [before]
        row.path = str(after)
        source_store.bump(session)
        session.commit()
        assert watcher.reload_sources() is True
        assert [path for path, _ in watcher.active_roots()] == [after]
    finally:
        watcher.shutdown()


def test_a_file_dropped_into_a_newly_added_source_is_ingested(
    session, settings, fixture_audio, tmp_path
) -> None:  # noqa: ANN001
    """End to end: no source at start-up, one added while running, a call in the archive."""
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "audio"
    tree.mkdir()
    spool = tmp_path / "spool"
    spool.mkdir()
    local = settings.model_copy(
        update={"audio_root": tree, "ingest_settle_seconds": 0.2, "ingest_scan_minutes": 60}
    )
    add_source(session, "the archive", tree)

    watcher = IngestWatcher(local)
    watcher.start(workers=1, initial_scan=False)
    try:
        add_source(session, "the new spool", spool)
        assert watcher.reload_sources() is True

        destination = spool / "20260828" / "EXAMPLE EMS OPS"
        destination.mkdir(parents=True)
        shutil.copy2(fixture_audio / PLAIN, destination / Path(PLAIN).name)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            session.expire_all()
            if session.scalar(select(Call.id)) is not None:
                break
            time.sleep(0.25)
        assert session.scalar(select(Call.id)) is not None, "the new source never ingested"
    finally:
        watcher.shutdown()


def test_the_scan_covers_every_active_root(session, settings, fixture_audio, tmp_path) -> None:  # noqa: ANN001
    """The periodic backstop has to follow the sources too, not just the inotify path."""
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "audio"
    tree.mkdir()
    spool = tmp_path / "spool" / "20260828" / "EXAMPLE EMS OPS"
    spool.mkdir(parents=True)
    shutil.copy2(fixture_audio / PLAIN, spool / Path(PLAIN).name)

    local = settings.model_copy(update={"audio_root": tree, "ingest_scan_minutes": 60})
    add_source(session, "the spool", tmp_path / "spool")

    watcher = IngestWatcher(local)
    # No observer at all: this is the scan path on its own.
    watcher.reload_sources(force=True)
    watcher.periodic_scan()
    session.expire_all()
    assert session.scalar(select(Call.id)) is not None


def _watch_paths(watcher, source_id: int) -> set[Path]:  # noqa: ANN001
    """Where watchdog is actually looking on behalf of one source row."""
    _spec, watches = watcher._watches[source_id]  # noqa: SLF001
    return {Path(watch.path) for watch in watches}


def test_uploads_under_a_watched_root_is_neither_watched_nor_scanned(
    session, settings, fixture_audio, tmp_path
) -> None:  # noqa: ANN001
    """The pruning that keeps the scan off ``uploads/`` has to hold per source root too.

    A source root is watched shallowly with one recursive watch per surviving child, so the
    prune is what stops watchdog walking a subtree of ~100k directories at schedule time and
    running the process out of inotify watches — for files ``POST /api/call-upload`` has
    already written the ledger row for.
    """
    from app.ingest.watcher import IngestWatcher

    spool = tmp_path / "spool"
    day = spool / "20260828" / "EXAMPLE EMS OPS"
    day.mkdir(parents=True)
    pushed = spool / "uploads" / "2026" / "08" / "29" / "ab"
    pushed.mkdir(parents=True)
    shutil.copy2(fixture_audio / PLAIN, pushed / Path(PLAIN).name)

    local = settings.model_copy(update={"audio_root": tmp_path / "audio", "ingest_scan_minutes": 60})
    row = add_source(session, "the spool", spool)

    watcher = IngestWatcher(local)
    watcher.start(workers=1, initial_scan=False)
    try:
        watched = _watch_paths(watcher, row.id)
        assert spool in watched, "the root itself is watched, shallowly"
        assert spool / "20260828" in watched, "a real day directory keeps its recursive watch"
        assert not any(spool / "uploads" == path or (spool / "uploads") in path.parents
                       for path in watched), f"uploads/ must not be watched: {watched}"

        # …and the backstop does not enumerate it either, so the copy above stays unseen.
        watcher.periodic_scan()
        session.expire_all()
        assert session.scalar(select(Call.id)) is None
    finally:
        watcher.shutdown()


def test_a_new_day_directory_is_adopted_by_the_source_that_owns_it(
    session, settings, fixture_audio, tmp_path
) -> None:  # noqa: ANN001
    """Adoption has to carry the *right* source id, not just any watch.

    With one root there was nothing to get wrong.  With two, a day directory created under
    the second spool must be watched on the second row's behalf — that id is what picks the
    trunk-recorder dialect for the file and what gets the activity stamp and the blame.
    """
    from app.ingest.watcher import IngestWatcher

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = add_source(session, "one", one)
    second = add_source(session, "two", two)

    local = settings.model_copy(
        update={
            "audio_root": tmp_path / "audio",
            "ingest_settle_seconds": 0.2,
            "ingest_scan_minutes": 60,
        }
    )
    watcher = IngestWatcher(local)
    watcher.start(workers=1, initial_scan=False)
    try:
        fresh = two / "20260829"
        fresh.mkdir()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and fresh not in _watch_paths(watcher, second.id):
            time.sleep(0.1)
        assert fresh in _watch_paths(watcher, second.id), "the new directory was never adopted"
        assert fresh not in _watch_paths(watcher, first.id), "adopted by the wrong source"

        # The file lands in the adopted subtree, so only the adopted watch can see it.
        talkgroup = fresh / "EXAMPLE EMS OPS"
        talkgroup.mkdir()
        shutil.copy2(fixture_audio / PLAIN, talkgroup / Path(PLAIN).name)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            session.expire_all()
            if session.scalar(select(Call.id)) is not None:
                break
            time.sleep(0.25)
        assert session.scalar(select(Call.id)) is not None, "the adopted directory never ingested"

        # touch_activity is keyed on the source id the queue item carried.
        session.expire_all()
        assert session.get(IngestSource, second.id).last_activity_at is not None
        assert session.get(IngestSource, first.id).last_activity_at is None
    finally:
        watcher.shutdown()
