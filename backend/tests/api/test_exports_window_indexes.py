"""The exports-window migration: the Exports tab's time-window index and the partial job-queue indexes.

``554fe0551d12`` adds ``call_exports (updated_at)`` and replaces the two whole-history
``(status, priority, scheduled_at)`` claim indexes with ``(priority, scheduled_at) WHERE
status = 'queued'`` plus ``(status) WHERE status IN ('running', 'failed')``.  These tests
check the indexes are there and valid, that the statements they were built for can use
them, and that the migration goes down and back up.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from tests.conftest import BACKEND_DIR, requires_db

pytestmark = requires_db

MIGRATION = "554fe0551d12"
BEFORE = "3216a949614d"
NEW = {
    "ix_call_exports_updated_at": "call_exports",
    "ix_transcription_jobs_queued": "transcription_jobs",
    "ix_transcription_jobs_running_failed": "transcription_jobs",
    "ix_audio_jobs_queued": "audio_jobs",
    "ix_audio_jobs_running_failed": "audio_jobs",
}
OLD = {
    "ix_transcription_jobs_claim": "transcription_jobs",
    "ix_audio_jobs_claim": "audio_jobs",
}


def _plan(session: Session, sql: str) -> str:
    return "\n".join(r[0] for r in session.execute(text(f"EXPLAIN {sql}")))


def test_the_indexes_exist_and_are_valid(session: Session) -> None:
    rows = session.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"
        ),
        {"names": [*NEW, *OLD]},
    ).all()
    assert {name: valid for name, valid in rows} == dict.fromkeys(NEW, True)


@pytest.mark.parametrize(
    ("sql", "index"),
    [
        # the two claim statements' SELECTs (app/stt/worker.py, app/audioproc/jobs.py)
        (
            "SELECT id FROM transcription_jobs WHERE status = 'queued' AND scheduled_at <= now() "
            "ORDER BY priority ASC, scheduled_at FOR UPDATE SKIP LOCKED LIMIT 1",
            "ix_transcription_jobs_queued",
        ),
        (
            "SELECT id FROM transcription_jobs WHERE status = 'queued' AND scheduled_at <= now() "
            "AND priority < 500 ORDER BY priority ASC, scheduled_at FOR UPDATE SKIP LOCKED LIMIT 1",
            "ix_transcription_jobs_queued",
        ),
        (
            "SELECT id FROM audio_jobs WHERE status = 'queued' AND scheduled_at <= now() "
            "ORDER BY priority ASC, scheduled_at FOR UPDATE SKIP LOCKED LIMIT 1",
            "ix_audio_jobs_queued",
        ),
        # what used to lean on the full index's leading ``status``
        (
            "SELECT count(*) FROM transcription_jobs WHERE status = 'running'",
            "ix_transcription_jobs_running_failed",
        ),
        (
            "SELECT id FROM audio_jobs WHERE status = 'running' "
            "AND locked_at < now() - interval '30 minutes'",
            "ix_audio_jobs_running_failed",
        ),
        # the Exports tab's window and its recent-rows table
        (
            "SELECT service, status, count(*) FROM call_exports "
            "WHERE updated_at >= now() - interval '24 hours' GROUP BY service, status",
            "ix_call_exports_updated_at",
        ),
        (
            "SELECT * FROM call_exports ORDER BY updated_at DESC, id DESC LIMIT 50",
            "ix_call_exports_updated_at",
        ),
    ],
)
def test_the_statements_can_use_them(session: Session, sql: str, index: str) -> None:
    """With sequential scans priced out, the planner reaches for the index built for it.

    The test tables are empty, so this proves the index *matches* the statement — its
    predicate is implied and its columns fit — not what production's planner will choose
    on real statistics; that was measured on production-shaped data (see the migration).
    """
    session.execute(text("SET LOCAL enable_seqscan = off"))
    session.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = _plan(session, sql)
    session.rollback()
    assert index in plan, plan


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
        tables = set(NEW.values()) | set(OLD.values())
        found = {ix["name"] for table in tables for ix in inspector.get_indexes(table)}
        return found & (set(NEW) | set(OLD))

    assert names() == set(NEW)
    command.downgrade(config, BEFORE)
    try:
        assert names() == set(OLD)
    finally:
        command.upgrade(config, "head")
    assert names() == set(NEW)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    script = ScriptDirectory.from_config(config)
    assert current == script.get_current_head()
    assert MIGRATION in {rev.revision for rev in script.walk_revisions()}
