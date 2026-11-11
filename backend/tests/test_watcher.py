"""The watcher: settle detection and the inotify path end to end."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from sqlalchemy import select

from app.ingest.service import wait_until_settled
from app.models import Call
from tests.conftest import requires_db
from tests.test_parser import PLAIN


def test_wait_until_settled_waits_for_a_growing_file(tmp_path: Path) -> None:
    import threading

    target = tmp_path / "growing.mp3"
    target.write_bytes(b"a" * 10)

    def grow() -> None:
        for _ in range(3):
            time.sleep(0.1)
            with target.open("ab") as handle:
                handle.write(b"a" * 10)

    thread = threading.Thread(target=grow)
    thread.start()
    started = time.monotonic()
    assert wait_until_settled(target, settle_seconds=0.2, timeout=10) is True
    thread.join()
    # it cannot have returned before the writer stopped
    assert time.monotonic() - started >= 0.3
    assert target.stat().st_size == 40


def test_wait_until_settled_on_a_missing_file(tmp_path: Path) -> None:
    assert wait_until_settled(tmp_path / "nope.mp3", settle_seconds=0.1, timeout=1) is False


@requires_db
def test_watcher_ingests_a_file_dropped_into_the_tree(
    session, settings, fixture_audio, tmp_path  # noqa: ANN001
) -> None:
    from app.ingest.watcher import IngestWatcher

    tree = tmp_path / "audio"
    tree.mkdir()
    local = settings.model_copy(
        update={"audio_root": tree, "ingest_settle_seconds": 0.2, "ingest_scan_minutes": 60}
    )

    watcher = IngestWatcher(local, root=tree)
    watcher.start(workers=1, initial_scan=False)
    try:
        destination = tree / "20260828" / "EXAMPLE EMS OPS"
        destination.mkdir(parents=True)
        shutil.copy2(fixture_audio / PLAIN, destination / Path(PLAIN).name)

        deadline = time.monotonic() + 30
        call = None
        while time.monotonic() < deadline:
            session.rollback()  # see other transactions' commits
            call = session.execute(select(Call)).scalars().first()
            if call is not None:
                break
            time.sleep(0.25)
    finally:
        watcher.shutdown()

    assert call is not None, "the watcher never ingested the dropped recording"
    assert call.file_path.endswith(Path(PLAIN).name)
    assert call.talkgroup_ids == [1421]
