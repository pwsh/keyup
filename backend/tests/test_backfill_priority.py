"""``keyup backfill-ingest-priority`` — recovering the class of the pre-column rows.

The rule under test is small and the stakes are not: ``ingest_priority`` is what the
exporters' live-band guard reads before publishing a recording to somebody else's public
service, so a row wrongly stamped ``live`` is a decade-old recording republished as though
it were happening now.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.backfill_priority import BACKLOG_AFTER, backfill_ingest_priority, classify, remaining
from app.models import Call, System

from .conftest import requires_db

pytestmark = requires_db


def test_trunkd_is_always_live() -> None:
    now = datetime.now(UTC)
    assert classify("trunkd", now - timedelta(days=400), now) == "live"


def test_an_upload_is_backlog_only_when_it_lagged() -> None:
    now = datetime.now(UTC)
    assert classify("upload", now - BACKLOG_AFTER * 2, now) == "backlog"
    assert classify("upload", now - timedelta(hours=2), now) == "live"
    # Exactly on the boundary is live: the rule is "more than a day", and a recorder that
    # spooled for precisely 24 hours is still a recorder that spooled.
    assert classify("upload", now - BACKLOG_AFTER, now) == "live"


def test_a_disk_row_recovers_nothing() -> None:
    now = datetime.now(UTC)
    assert classify("file", now - timedelta(days=400), now) is None
    assert classify("trunk_recorder", now - timedelta(days=400), now) is None


def _call(session, *, source: str, lag: timedelta, index: int, priority: str | None = None):  # noqa: ANN001, ANN202
    started = datetime(2024, 3, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=index)
    call = Call(
        system_id=session.scalars(select(System.id)).first(),
        ingest_source=source,
        ingest_priority=priority,
        recorded_at=started + timedelta(seconds=3),
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=3),
        duration_s=3,
        ingested_at=started + lag,
        file_path=f"uploads/{source}-{index}.mp3",
        file_name=f"{source}-{index}.mp3",
    )
    session.add(call)
    return call


@pytest.fixture
def archive(session):  # noqa: ANN001, ANN201
    """Four rows the backfill must sort, and two it must refuse to touch."""
    session.add(System(name="Example System"))
    session.commit()
    _call(session, source="trunkd", lag=timedelta(seconds=5), index=0)
    _call(session, source="upload", lag=timedelta(minutes=2), index=1)
    _call(session, source="upload", lag=timedelta(days=400), index=2)
    _call(session, source="upload", lag=timedelta(days=3), index=3)
    _call(session, source="file", lag=timedelta(days=400), index=4)
    _call(session, source="trunk_recorder", lag=timedelta(days=400), index=5)
    # One row that already carries a class: the backfill is for NULLs only.
    _call(session, source="upload", lag=timedelta(days=400), index=6, priority="urgent")
    session.commit()
    return session


def _priorities(session) -> dict[str, str | None]:  # noqa: ANN001
    return {
        call.file_name: call.ingest_priority
        for call in session.scalars(select(Call).order_by(Call.id))
    }


def test_a_dry_run_writes_nothing(archive) -> None:  # noqa: ANN001
    before = _priorities(archive)
    report = backfill_ingest_priority(archive, dry_run=True)
    assert (report["live"], report["backlog"]) == (2, 2)
    assert report["dry_run"] is True
    assert _priorities(archive) == before


def test_apply_stamps_each_row_with_its_class(archive) -> None:  # noqa: ANN001
    assert remaining(archive) == 4
    report = backfill_ingest_priority(archive, dry_run=False)
    assert (report["live"], report["backlog"], report["scanned"]) == (2, 2, 4)

    after = _priorities(archive)
    assert after["trunkd-0.mp3"] == "live"
    assert after["upload-1.mp3"] == "live"
    assert after["upload-2.mp3"] == "backlog"
    assert after["upload-3.mp3"] == "backlog"
    # Off-disk rows keep their NULL: nothing on them says when they were recorded
    # relative to now, and a guessed class would make the exporters publish them.
    assert after["file-4.mp3"] is None
    assert after["trunk_recorder-5.mp3"] is None
    # And an existing class is never rewritten.
    assert after["upload-6.mp3"] == "urgent"


def test_it_resumes_and_is_idempotent(archive) -> None:  # noqa: ANN001
    """A batch size of one is the interrupted run, replayed one commit at a time."""
    backfill_ingest_priority(archive, dry_run=False, batch_size=1)
    assert remaining(archive) == 0
    again = backfill_ingest_priority(archive, dry_run=False)
    assert again["scanned"] == 0


def test_limit_bounds_a_dry_run(archive) -> None:  # noqa: ANN001
    report = backfill_ingest_priority(archive, dry_run=True, batch_size=2, limit=3)
    assert report["scanned"] == 3
