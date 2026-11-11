"""Drop seven indexes no query wants, and narrow an eighth to the rows anyone filters on.

``pg_stat_user_indexes`` on the production archive, over the two weeks since the stats
were last reset, lists eleven non-unique indexes with fewer than fifty scans between them.
"Unused" is not on its own a reason to drop an index — an index can be unused because the
feature that needs it is rare, or because the pass that needs it has not run yet — so each
one below was matched against the queries in ``backend/app`` and checked with EXPLAIN on
production before it was touched.  Four of the eleven are kept, and the reasons are
recorded here rather than in a commit message because the next person to read
``idx_scan = 0`` will otherwise reach for them again.

**Dropped**

``ix_calls_started_local`` (197 MB, 0 scans)
    ``started_local`` is a *presentation* column — the naive local (``TZ``) wall clock,
    carried so CAD consumers and the exporters do not each re-derive it.  Every reference to it
    in the codebase is a read of the value (``app/exports.py``, ``app/webhooks.py``,
    ``app/schemas.py``) or a write of it (``app/ingest/parser.py``).  Nothing filters or
    orders by it; ``started_at`` is the clock the archive actually queries, and
    ``ix_calls_started_at`` serves that.  The single largest index in the list.

``ix_calls_ingest_priority`` (42 MB)
    Partial on ``ingest_priority IS NOT NULL``, and the only query in the tree that has
    ``ingest_priority`` in a WHERE clause is the backfill's, which looks for
    ``IS NULL`` — the rows this index deliberately excludes.  ``app/backfill_priority.py``
    says so in its own comment.  Elsewhere it is a column read (the webhook scope check
    tests it on a call already in hand) or a projected output column.

``ix_radio_events_instance_at`` (36 MB)
    ``instance`` is never a leading filter.  Both queries that use it —
    ``GET /api/radios/{id}/events`` and the site summary — start from ``radio_pk`` or
    ``site_id`` and add ``instance`` as a narrowing term, and there are three distinct
    instance values across 822 K rows, so it has no selectivity to contribute.  EXPLAIN on
    production picked this index for the radio query and produced the *worse* plan: an
    index scan on ``instance`` with ``radio_pk`` demoted to a heap filter.  Dropping it
    hands those queries back to ``ix_radio_events_radio_at`` and
    ``ix_radio_events_site_kind_at``, which lead on the selective column.

``ix_activity_events_source`` (36 MB)
    Eight distinct values over 3.34 M rows, of which ``upload`` (1.77 M) and
    ``transcribe`` (1.47 M) are 97% of the table.  For the values anyone actually has, the
    index selects most of the table and the planner is right to ignore it; for the tail
    (``admin``, 6 rows) the activity list's ``ORDER BY id DESC LIMIT n`` walks the primary
    key backwards, which is what it does today.

``ix_transcripts_primary`` (33 MB)
    Partial on ``(call_id) WHERE is_primary``.  Every query that uses it —
    ``app/api/calls.py``, ``app/stt/worker.py``, ``app/flag_hallucinations.py`` — is
    ``call_id = ? AND is_primary``, and ``ix_transcripts_call_id`` already covers the
    leading term.  A call has one to three transcripts, so the ``is_primary`` recheck
    costs a heap tuple, not a scan.  This is the textbook "an existing index covers the
    shape" case.

``ix_radio_affiliations_last_heard`` (17 MB)
    The directory's affiliation list orders by
    ``coalesce(last_affiliated_at, last_heard_at)`` (``app/api/directory.py``), and a plain
    b-tree on ``last_heard_at`` cannot serve an expression — EXPLAIN on production
    confirms a seq scan over the 30 K-row table, which at that size is the correct plan
    anyway.  The composite ``ix_radio_affiliations_talkgroup`` still covers the per-talkgroup
    lookups.

``ix_activity_events_level`` (36 MB) — dropped and *replaced*
    99.1% of the table is ``level = 'info'``, so for the common value the index is dead
    weight, and it is the two rare values an operator ever filters to: 31 155 ``warn`` and
    2 130 ``error``.  So it comes back as ``ix_activity_events_level_at``, a partial index
    on ``(level, id DESC) WHERE level <> 'info'`` — a few hundred KB instead of 36 MB, and
    a strictly better one, because carrying ``id DESC`` means it serves the activity list's
    ``ORDER BY id DESC LIMIT n`` as well as the filter and the sort node disappears.
    Postgres proves ``level = 'error'`` implies ``level <> 'info'`` and uses it; verified
    with EXPLAIN against a 200 K-row copy of the shape before this was written.

**Kept, despite the scan count**

``ix_ingest_files_call_id`` (89 MB, 0 scans)
    This one is a trap.  ``ingest_files.call_id`` is a foreign key to ``calls.id`` with
    ``ON DELETE SET NULL``, and Postgres does not index the referencing side of a foreign
    key for you: every ``DELETE FROM calls`` has to find the ``ingest_files`` rows pointing
    at it.  It reads zero scans because retention has never deleted a call — both keep
    windows ship at 0, "keep for ever" — and the moment an operator sets one, this index is
    the difference between a batch of 500 deletions and 500 sequential scans of a 3.08 M
    row, 1.19 GB table.  Keep.

``ix_radio_events_at`` (32 MB, <50 scans)
    ``app/retention.py``'s ``purge_radio_events`` is ``DELETE FROM radio_events WHERE
    at < cutoff``, and ``RADIO_EVENTS_KEEP_DAYS`` is 90.  The oldest row in the table is
    younger than that, so the pass has not yet had anything to delete — the same "not used
    yet" as above, not "not needed".  Keep.

``ix_calls_tags`` (13 MB, <50 scans)
    ``app/api/filters.py`` builds ``calls.tags && ARRAY[…]``, which is exactly and only
    what a GIN index answers; without it that filter is a sequential scan of the 9.4 GB
    ``calls`` table.  Low scan counts mean the tag filter is rarely used, not that it is
    free to make unusable.  It is also the smallest index in the list.  Keep.

``ix_webhook_deliveries_created`` (22 MB, <50 scans)
    ``app/webhooks.py``'s ``prune`` is ``status = 'delivered' AND created_at < cutoff``,
    which is this index's shape, and it runs nightly.  22 MB for a 59 K-row table is not a
    reason to drop it — it is the same insert/delete bloat that put the table's heap at
    453 MB, and the fix is the ``VACUUM FULL`` in docs/OPERATIONS.md (which rebuilds the
    indexes with it) plus the per-table autovacuum in the preceding migration.  Keep.

``DROP INDEX CONCURRENTLY`` throughout, inside an ``autocommit_block`` because it cannot
run in a transaction.  A plain drop would be fast too — it is a catalog delete — but it
takes an ACCESS EXCLUSIVE lock on the table, and on ``calls`` that means every reader on a
live archive queues behind it.  The concurrent form takes SHARE UPDATE EXCLUSIVE instead.
The cost is that a failure part-way leaves the work done so far committed; ``IF EXISTS``
on both directions makes a re-run pick up where it stopped.

Revision ID: b8d4e60a17c2
Revises: a3f7c21e9b40
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b8d4e60a17c2"
down_revision: str | None = "a3f7c21e9b40"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: ``index -> the CREATE that downgrade() puts back``.  Written out rather than generated
#: so a downgrade restores the index that was actually there, expression and partial
#: predicate included, and not an approximation of it.
DROPPED: dict[str, str] = {
    "ix_calls_started_local": "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_calls_started_local ON calls (started_local)",
    "ix_calls_ingest_priority": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_calls_ingest_priority ON calls (ingest_priority) "
        "WHERE ingest_priority IS NOT NULL"
    ),
    "ix_radio_events_instance_at": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_radio_events_instance_at "
        "ON radio_events (instance, at)"
    ),
    "ix_activity_events_source": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_activity_events_source ON activity_events (source)"
    ),
    "ix_activity_events_level": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_activity_events_level ON activity_events (level)"
    ),
    "ix_transcripts_primary": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcripts_primary ON transcripts (call_id) "
        "WHERE is_primary"
    ),
    "ix_radio_affiliations_last_heard": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_radio_affiliations_last_heard "
        "ON radio_affiliations (last_heard_at)"
    ),
}

#: the replacement for ``ix_activity_events_level``: the 33 K rows an operator filters to,
#: in the order the activity list reads them.
CREATE_LEVEL_AT = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_activity_events_level_at "
    "ON activity_events (level, id DESC) WHERE level <> 'info'"
)
DROP_LEVEL_AT = "DROP INDEX CONCURRENTLY IF EXISTS ix_activity_events_level_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name in DROPPED:
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        bind.exec_driver_sql(CREATE_LEVEL_AT)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.exec_driver_sql(DROP_LEVEL_AT)
        for create in DROPPED.values():
            bind.exec_driver_sql(create)
