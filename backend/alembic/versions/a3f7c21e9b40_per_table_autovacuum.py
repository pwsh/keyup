"""Per-table autovacuum for the eight tables the archive actually churns.

The cluster runs the shipped global defaults — ``autovacuum_vacuum_scale_factor = 0.2``,
``autovacuum_analyze_scale_factor = 0.1`` — and no table here has ever carried a
``reloptions`` of its own.  Twenty percent is a fine number for a table of ten thousand
rows and a terrible one for ``calls``: it means Postgres waits for **615 000** dead
tuples before it will vacuum, and the measured state on 2026-09-15 was 593 K dead against
3.07 M live (19%) — that is, sitting just under the trigger, permanently, which is where a
0.2 scale factor parks any table that is updated steadily.  The dead space is not
reclaimed, index scans walk it, and the planner's statistics drift a tenth of the table
out of date between analyses.

So the scale factor is replaced with a *threshold* on these tables: 2% plus a flat floor,
which for ``calls`` means a vacuum every ~71 K dead rows instead of every 615 K, and an
analyze every ~41 K instead of every 308 K.  The tables are big enough that 2% is still
thousands of rows — this is not a vacuum storm, it is roughly eight times more often than
today on the tables that need it and no change at all on the ~200 tables that do not.

``webhook_deliveries`` gets much smaller numbers because it is not sized like the others:
59 K live rows, but every row is inserted, delivered, and deleted seven days later, and it
had grown to 453 MB of heap for 37 MB of live payload — an 11x bloat that a 0.2 scale
factor on a 59 K-row table (11 800 dead rows) could never keep ahead of.  Its one-off
``VACUUM FULL`` is an operator step, written up in docs/OPERATIONS.md; these settings are
what stop it coming back.

Plain ``ALTER TABLE … SET (…)``: a catalog update, no rewrite and no scan.  It takes a
brief ACCESS EXCLUSIVE lock to swap ``pg_class.reloptions`` and releases it immediately,
which is why this is safe in a migration on a live archive.

Revision ID: a3f7c21e9b40
Revises: e9a4c71b05d3
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a3f7c21e9b40"
down_revision: str | None = "e9a4c71b05d3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: ``table -> (vacuum_threshold, analyze_threshold)``.  The scale factors are the same
#: everywhere (2% / 1%); only the flat floor is sized to the table, because the floor is
#: what governs a table whose 2% is smaller than a day's churn.
#:
#: Measured live rows on 2026-09-15, for the record:
#:   call_sources 3.50 M, activity_events 3.34 M, ingest_files 3.08 M, calls 3.08 M,
#:   transcription_jobs 1.59 M, audio_jobs 1.58 M, call_exports 454 K,
#:   webhook_deliveries 59 K.
TABLES: dict[str, tuple[int, int]] = {
    # the millions: 2% is already 30-70 K rows, so the floor only matters early
    "calls": (10_000, 5_000),
    "transcription_jobs": (10_000, 5_000),
    "audio_jobs": (10_000, 5_000),
    "activity_events": (10_000, 5_000),
    "ingest_files": (10_000, 5_000),
    "call_sources": (10_000, 5_000),
    # 454 K rows: 2% is ~9 K, so a 10 K floor would *raise* its trigger.  Half it.
    "call_exports": (5_000, 2_500),
    # 59 K rows and the whole table turns over weekly — 2% is 1 200 rows, and the floor is
    # what does the work here.  This is the one table where these numbers mean "vacuum
    # several times a day", which is exactly the intent.
    "webhook_deliveries": (1_000, 500),
}

VACUUM_SCALE = 0.02
ANALYZE_SCALE = 0.01

#: what ``downgrade()`` restores: the cluster defaults, reached by *removing* the
#: reloptions rather than writing the global values into them.  RESET is the honest
#: inverse — a table with no reloptions follows ``postgresql.conf``, and if an operator
#: later retunes the cluster these tables follow it too, which writing 0.2/0.1 in here
#: would silently prevent.
OPTIONS = (
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_threshold",
    "autovacuum_analyze_scale_factor",
    "autovacuum_analyze_threshold",
)


def upgrade() -> None:
    bind = op.get_bind()
    for table, (vacuum_threshold, analyze_threshold) in TABLES.items():
        bind.exec_driver_sql(
            f"ALTER TABLE {table} SET ("  # noqa: S608 - table names are this module's literals
            f"autovacuum_vacuum_scale_factor = {VACUUM_SCALE}, "
            f"autovacuum_vacuum_threshold = {vacuum_threshold}, "
            f"autovacuum_analyze_scale_factor = {ANALYZE_SCALE}, "
            f"autovacuum_analyze_threshold = {analyze_threshold})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    options = ", ".join(OPTIONS)
    for table in TABLES:
        bind.exec_driver_sql(f"ALTER TABLE {table} RESET ({options})")  # noqa: S608
