"""The finished-job window migration: partial ``finished_at`` indexes for the "done lately" windows.

``1054b8161324`` adds ``(finished_at) WHERE status IN ('done', 'failed')`` to
``transcription_jobs`` and ``audio_jobs``.  These tests check the indexes are there and
valid, that the admin STT queue block and the live activity panel's statements can use
them, that those endpoints still count the right rows, and that the migration goes down
and back up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from tests.conftest import BACKEND_DIR, requires_db

pytestmark = requires_db

MIGRATION = "1054b8161324"
BEFORE = "554fe0551d12"
NEW = {
    "ix_transcription_jobs_finished": "transcription_jobs",
    "ix_audio_jobs_finished": "audio_jobs",
}


def _plan(session: Session, sql: str) -> str:
    return "\n".join(r[0] for r in session.execute(text(f"EXPLAIN {sql}")))


def test_the_indexes_exist_and_are_valid(session: Session) -> None:
    rows = session.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"
        ),
        {"names": list(NEW)},
    ).all()
    assert {name: valid for name, valid in rows} == dict.fromkeys(NEW, True)


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        # app/api/admin.py _queue_block: the hour, with the ten minutes as a FILTER
        (
            "SELECT status, count(*), count(*) FILTER (WHERE finished_at >= now() - interval "
            "'10 minutes') FROM transcription_jobs WHERE status IN ('done', 'failed') "
            "AND finished_at >= now() - interval '1 hour' GROUP BY status",
            "ix_transcription_jobs_finished",
        ),
        # app/api/status_activity.py _jobs_block, once per table
        (
            "SELECT count(*) FROM transcription_jobs WHERE status = 'done' "
            "AND finished_at >= now() - interval '10 minutes'",
            "ix_transcription_jobs_finished",
        ),
        (
            "SELECT count(*) FROM audio_jobs WHERE status = 'done' "
            "AND finished_at >= now() - interval '10 minutes'",
            "ix_audio_jobs_finished",
        ),
    ],
)
def test_the_statements_can_use_them(session: Session, sql: str, index: str) -> None:
    """With sequential scans priced out, the planner reaches for the index built for it.

    The test tables are empty, so this proves the index *matches* the statement — its
    predicate is implied and its column fits — not what production's planner will choose
    on real statistics; that was measured on production-shaped data (see the migration).
    """
    session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = _plan(session, sql)
    session.rollback()
    assert index in plan, plan


def _job(session: Session, model, call_id: int, status: str, finished_ago: timedelta | None) -> None:  # noqa: ANN001
    now = datetime.now(UTC)
    finished = None if finished_ago is None else now - finished_ago
    started = None if finished is None else finished - timedelta(seconds=4)
    session.add(model(call_id=call_id, status=status, started_at=started, finished_at=finished))


def test_the_windows_count_what_they_did(session: Session) -> None:
    """The rewritten statements count the same rows the full scans did."""
    from app.api.admin import _queue_block
    from app.api.status_activity import _jobs_block
    from app.models import AudioJob, Call, System, TranscriptionJob

    system = System(name="kc8")
    session.add(system)
    session.flush()
    at = datetime.now(UTC)
    calls = []
    for n in range(9):
        call = Call(
            system_id=system.id,
            recorded_at=at,
            started_at=at,
            started_local=at.replace(tzinfo=None),
            ended_at=at + timedelta(seconds=5),
            duration_s=5,
            file_path=f"kc8/{n}.mp3",
            file_name=f"{n}.mp3",
        )
        session.add(call)
        calls.append(call)
    session.flush()
    ids = [c.id for c in calls]
    for model in (TranscriptionJob, AudioJob):
        _job(session, model, ids[0], "done", timedelta(minutes=2))  # current 5-min window, last 10
        _job(session, model, ids[1], "done", timedelta(minutes=7))  # previous window, last 10
        _job(session, model, ids[2], "done", timedelta(minutes=30))  # last hour only
        _job(session, model, ids[3], "done", timedelta(hours=3))  # outside everything
        _job(session, model, ids[4], "failed", timedelta(minutes=3))  # failed in the hour
        _job(session, model, ids[5], "cancelled", timedelta(minutes=1))  # never counted
        _job(session, model, ids[6], "queued", None)
        _job(session, model, ids[7], "queued", None)
        _job(session, model, ids[8], "running", None)
    session.flush()

    queue = _queue_block(session)
    assert (queue.completed_1h, queue.failed_1h) == (3, 1)
    assert queue.throughput_per_min == 0.2  # two done in the last ten minutes
    assert queue.mean_processing_s == pytest.approx(4.0)
    assert (queue.queued_live + queue.queued_backlog, queue.running) == (2, 1)

    now = datetime.now(UTC)
    for model in (TranscriptionJob, AudioJob):
        block = _jobs_block(session, model, now - timedelta(minutes=5), now - timedelta(minutes=10))
        assert block["queued"] == 2
        assert (block["done"]["current"], block["done"]["previous"]) == (1, 1), block
    session.rollback()


def test_the_migration_goes_down_and_up(migrated_database) -> None:  # noqa: ANN001, ARG001
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    from app.db import engine

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    def names() -> set[str]:
        engine.dispose()
        inspector = inspect(engine)
        found = {ix["name"] for table in set(NEW.values()) for ix in inspector.get_indexes(table)}
        return found & set(NEW)

    assert names() == set(NEW)
    command.downgrade(config, BEFORE)
    try:
        assert names() == set()
    finally:
        command.upgrade(config, "head")
    assert names() == set(NEW)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    script = ScriptDirectory.from_config(config)
    assert current == script.get_current_head()
    assert MIGRATION in {rev.revision for rev in script.walk_revisions()}
