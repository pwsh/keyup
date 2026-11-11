"""The operational activity log: :mod:`app.activity` and the ingest emit point."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import activity
from app.models import ActivityEvent
from tests.conftest import EXAMPLE_AGENCIES, requires_db

pytestmark = requires_db


def test_record_inserts_and_returns_the_row(session: Session) -> None:
    row = activity.record(
        session,
        source="system",
        level="info",
        kind="test.event",
        message="hello",
        meta={"n": 1},
        notify_bus=False,
    )
    assert row is not None
    assert row.id is not None  # flushed
    session.commit()
    fetched = session.get(ActivityEvent, row.id)
    assert fetched is not None
    assert fetched.source == "system"
    assert fetched.kind == "test.event"
    assert fetched.meta == {"n": 1}


def test_record_is_defensive_and_never_raises() -> None:
    """A broken session must not turn a log line into a failed ingest."""

    class Boom:
        def add(self, _row):  # noqa: ANN001, ANN202
            raise RuntimeError("db is on fire")

    # No exception escapes, and the failure is reported as None.
    result = activity.record(
        Boom(),  # type: ignore[arg-type]
        source="system",
        level="error",
        kind="test.boom",
        message="should be swallowed",
        notify_bus=False,
    )
    assert result is None


def test_a_database_error_in_record_does_not_abort_the_callers_transaction(
    session: Session, monkeypatch
) -> None:  # noqa: ANN001
    """A failed statement in the log line must not silently roll back the caller.

    PostgreSQL aborts the whole transaction on a failed statement and the caller's COMMIT
    then becomes a ROLLBACK without raising, so the swallow has to be a SAVEPOINT.
    """
    from sqlalchemy import text

    from app.db import SessionLocal

    def broken(session, _event, _payload):  # noqa: ANN001, ANN202
        session.execute(text("SELECT 1 / 0"))

    kept = activity.record(session, source="system", level="info", kind="kept", message="the work")
    monkeypatch.setattr(activity, "notify", broken)
    assert activity.record(session, source="system", level="info", kind="lost", message="x") is None
    session.commit()

    with SessionLocal() as fresh:
        kinds = list(fresh.scalars(select(ActivityEvent.kind)))
    assert kept is not None
    assert kinds == ["kept"]


def test_recent_filters_by_source_and_level(session: Session) -> None:
    activity.record(session, source="ingest", level="info", kind="a", message="i", notify_bus=False)
    activity.record(session, source="upload", level="warn", kind="b", message="u", notify_bus=False)
    activity.record(session, source="transcribe", level="error", kind="c", message="t", notify_bus=False)
    session.commit()

    assert {r.source for r in activity.recent(session, limit=10)} == {"ingest", "upload", "transcribe"}
    assert [r.source for r in activity.recent(session, limit=10, source="upload")] == ["upload"]
    assert [r.level for r in activity.recent(session, limit=10, level="error")] == ["error"]


def test_recent_is_newest_first_and_keyset_paginates(session: Session) -> None:
    ids = []
    for n in range(5):
        row = activity.record(
            session, source="system", level="info", kind="seq", message=str(n), notify_bus=False
        )
        assert row is not None
        ids.append(row.id)
    session.commit()

    first = activity.recent(session, limit=2)
    assert [r.id for r in first] == [ids[4], ids[3]]  # newest first
    page2 = activity.recent(session, limit=2, before_id=first[-1].id)
    assert [r.id for r in page2] == [ids[2], ids[1]]


def test_recent_filters_by_since(session: Session) -> None:
    old = activity.record(session, source="system", level="info", kind="old", message="old", notify_bus=False)
    session.flush()
    assert old is not None
    old.at = datetime.now(UTC) - timedelta(days=2)
    activity.record(session, source="system", level="info", kind="new", message="new", notify_bus=False)
    session.commit()

    cutoff = datetime.now(UTC) - timedelta(days=1)
    kinds = [r.kind for r in activity.recent(session, limit=10, since=cutoff)]
    assert kinds == ["new"]


def test_prune_deletes_only_old_rows(session: Session) -> None:
    old = activity.record(session, source="system", level="info", kind="old", message="x", notify_bus=False)
    session.flush()
    assert old is not None
    old.at = datetime.now(UTC) - timedelta(days=40)
    activity.record(session, source="system", level="info", kind="new", message="y", notify_bus=False)
    session.commit()

    removed = activity.prune(session, keep_days=30)
    assert removed == 1
    remaining = list(session.scalars(select(ActivityEvent)))
    assert [r.kind for r in remaining] == ["new"]

    # keep_days <= 0 is "keep for ever" and a no-op.
    assert activity.prune(session, keep_days=0) == 0


def test_ingest_records_a_call_accepted_activity_row(
    session: Session, settings, fixture_audio: Path  # noqa: ANN001
) -> None:
    from app.ingest.service import ingest_file
    from app.seed import seed_agencies

    seed_agencies(session, path=EXAMPLE_AGENCIES)
    session.commit()

    relative = "20260829/EXAMPLE EVENT 1/20260829_030253_Example-System_Central_T-Control__TO_1701.mp3"
    result = ingest_file(session, fixture_audio / relative, settings=settings, audio_root=fixture_audio)
    session.commit()
    assert result.call_id is not None

    rows = list(
        session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "call.accepted"))
    )
    assert len(rows) == 1
    assert rows[0].source == "ingest"
    assert rows[0].call_id == result.call_id
    assert rows[0].meta is not None and rows[0].meta["had_audio"] is True


def test_activity_events_count_is_bounded_helper(session: Session) -> None:
    """Sanity: the table starts empty for each test (truncate) so counts are deterministic."""
    assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 0
