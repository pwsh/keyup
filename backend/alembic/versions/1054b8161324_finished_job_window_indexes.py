"""Partial ``finished_at`` indexes on the two job tables, for "done in the last N minutes".

Three readers ask how many jobs finished in a recent window, and none had an index to use:

* the admin STT queue block (app/api/admin.py ``_queue_block``): completed/failed and the
  mean processing time over the last hour, and ``done`` over the last ten minutes for the
  throughput and ETA.  Both were parallel sequential scans of ``transcription_jobs``
  (3.05 M rows, 441 MB of heap): 84 ms and 76 ms warm on production on 2026-09-23, 554 ms
  and 90 ms in an earlier measurement;
* the live activity panel (app/api/status_activity.py ``_jobs_block``), ``done`` in the
  current and previous window plus the queue depth, once per table per poll.  It had no
  ``WHERE`` at all, so it read every row of ``transcription_jobs`` (132 ms) and of
  ``audio_jobs`` (3.31 M rows, 350 MB: 121 ms) on each poll.  That endpoint is open.

The index is ``(finished_at) WHERE status IN ('done', 'failed')``.  Every one of those
queries filters on ``status = 'done'`` or ``status IN ('done', 'failed')``, both of which
imply the predicate, so the planner can use it; it leaves out the ~200 k queued/running and
cancelled rows on ``transcription_jobs`` and the ~520 k cancelled ones on ``audio_jobs``.
It is not small — nearly every job ends ``done`` — about 60 MB on a 3 M-row table (measured
on a production-shaped copy, PostgreSQL 18.6), but the queries only ever touch its right
edge, so what stays in cache is a few pages.  On that copy the hour query went from a
sequential scan to an index range scan of 2.8 k rows in 1.4 ms, and the panel's windows to
0.6 ms.  A BRIN index would be tiny, and ``finished_at`` correlates 0.998 with the heap
order, but one late-finishing backlog job whose new row version lands in an old page makes
that page's whole block range match "the last hour"; a btree does not degrade that way.

``status`` is already in partial-index predicates on both tables, so a job finishing was
never a HOT update; this adds one index entry per finished job and no lost HOT updates.

Built ``CONCURRENTLY IF NOT EXISTS`` inside an ``autocommit_block`` like 554fe0551d12 — a
plain build's SHARE lock would block every job insert for the length of the build — and a
failed concurrent build's INVALID leftover is dropped before the retry.  The downgrade drops
them the same way.

Revision ID: 1054b8161324
Revises: 554fe0551d12
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1054b8161324"
down_revision: str | None = "554fe0551d12"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: name -> the concurrent build.
NEW_INDEXES: dict[str, str] = {
    "ix_transcription_jobs_finished": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcription_jobs_finished "
        "ON transcription_jobs (finished_at) WHERE status IN ('done', 'failed')"
    ),
    "ix_audio_jobs_finished": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audio_jobs_finished "
        "ON audio_jobs (finished_at) WHERE status IN ('done', 'failed')"
    ),
}


def _drop_if_invalid(bind: sa.engine.Connection, name: str) -> None:
    """A concurrent build that failed leaves an INVALID index that ``IF NOT EXISTS`` skips."""
    invalid = bind.exec_driver_sql(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %(name)s AND NOT i.indisvalid",
        {"name": name},
    ).first()
    if invalid is not None:
        bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, create in NEW_INDEXES.items():
            _drop_if_invalid(bind, name)
            bind.exec_driver_sql(create)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name in reversed(list(NEW_INDEXES)):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
