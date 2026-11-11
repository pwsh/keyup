"""The Exports tab's time window gets an index; the two job queues get partial claim indexes.

**``call_exports (updated_at)``.**  ``GET /admin/exports`` asks eight questions of the
ledger, and six of them are "over the last 24 hours, by service": the status tallies, the
duplicate / late / already-exported skips, the Broadcastify responses and the recorder
uploads and claims.  None had an index to use, so each was a parallel sequential scan of the
whole ledger (823 k rows, 230 MB on production, 2026-09-23: 20-28 ms apiece), and the
"recent rows" table — ``ORDER BY updated_at DESC, id DESC LIMIT 50`` — was a ninth scan
plus a top-N sort (30 ms).  The tab polls every 15 s.

A code review suggested ``(service, updated_at)``.  A plain
``(updated_at)`` is what the queries actually want: none of them filters on ``service`` —
they *group* by it across the window — and the recent-rows query orders by ``updated_at``
across every service, which an index that leads with ``service`` cannot hand back in order.
Measured on PostgreSQL 18 against an 820 k-row copy of the ledger's shape: the 24-hour
group-by is 10.5 ms with either index (41 ms with none; the two-column one gets there by
skip scan), while the recent-rows query is 0.03 ms with ``(updated_at)`` and still 60-70 ms
with ``(service, updated_at)``.  The all-time tallies still scan; the endpoint caches its
ledger figures for a minute (app/api/admin.py, ``EXPORTS_STATUS_TTL_S``) for those.

**Partial queue indexes**.  ``ix_transcription_jobs_claim`` and
``ix_audio_jobs_claim`` were ``(status, priority, scheduled_at)`` over every job ever run —
113 MB and 90 MB on production for 3.0 M and 3.3 M rows, of which 60 k and 0 are queued.
The claim statements (app/stt/worker.py ``_claim_sql``, app/audioproc/jobs.py ``_CLAIM``)
only ever read the ``queued`` slice, so ``(priority, scheduled_at) WHERE status = 'queued'``
answers them from an index of about 2 MB that stays in cache.

The full indexes are **dropped**, not kept alongside.  Adding the partial index alone
changes nothing: with both present the planner keeps choosing the full one for the claim
(it can put ``scheduled_at <= now()`` in the index condition behind the ``status``
equality, and the cost estimates come out level), so the 100 MB index would stay hot and
the small one would sit unused.  The other readers that leaned on the full index's leading
``status`` — the ``running``/``failed`` counts on the stats and status pages, the stale-job
reapers (``status = 'running' AND locked_at < …``) and the parked-failure retries — get a
second partial index, ``(status) WHERE status IN ('running', 'failed')``, which holds a
handful of rows.  ``queued``/``running`` together were already served by the partial unique
``ix_*_open_call``.  What loses its index is a filter on ``done`` or ``cancelled`` alone:
the admin job list's total when an operator filters it to one of those (an admin-only
count over most of the table, a sequential scan rather than an index-only one).  The
"completed in the last hour / ten minutes" figures on the admin STT queue were already
sequential scans and are unchanged.

All builds are ``CREATE INDEX CONCURRENTLY`` inside an ``autocommit_block``, like
c4e9a1d07b35: a plain build takes a SHARE lock that blocks every job insert (every call
accepted) for the length of the build.  ``IF NOT EXISTS`` makes a re-run pick up where a
failure stopped, and a failed concurrent build's INVALID leftover is dropped before the
retry.  The new indexes are built before the old ones are dropped, so the claim is never
without an index.  The downgrade rebuilds the two full indexes the same way, then drops
the new ones.

Revision ID: 554fe0551d12
Revises: 3216a949614d
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "554fe0551d12"
down_revision: str | None = "3216a949614d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: name -> the concurrent build.  Smallest table first.
NEW_INDEXES: dict[str, str] = {
    "ix_call_exports_updated_at": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_call_exports_updated_at ON call_exports (updated_at)"
    ),
    "ix_transcription_jobs_queued": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcription_jobs_queued "
        "ON transcription_jobs (priority, scheduled_at) WHERE status = 'queued'"
    ),
    "ix_transcription_jobs_running_failed": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcription_jobs_running_failed "
        "ON transcription_jobs (status) WHERE status IN ('running', 'failed')"
    ),
    "ix_audio_jobs_queued": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audio_jobs_queued "
        "ON audio_jobs (priority, scheduled_at) WHERE status = 'queued'"
    ),
    "ix_audio_jobs_running_failed": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audio_jobs_running_failed "
        "ON audio_jobs (status) WHERE status IN ('running', 'failed')"
    ),
}

#: The whole-history claim indexes the partial ones replace, as the downgrade rebuilds them.
OLD_INDEXES: dict[str, str] = {
    "ix_transcription_jobs_claim": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcription_jobs_claim "
        "ON transcription_jobs (status, priority, scheduled_at)"
    ),
    "ix_audio_jobs_claim": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audio_jobs_claim "
        "ON audio_jobs (status, priority, scheduled_at)"
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
        for name in OLD_INDEXES:
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, create in OLD_INDEXES.items():
            _drop_if_invalid(bind, name)
            bind.exec_driver_sql(create)
        for name in reversed(list(NEW_INDEXES)):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
