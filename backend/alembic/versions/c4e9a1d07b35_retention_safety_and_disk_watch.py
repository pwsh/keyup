"""Retention safety and the disk-free alert.

**Four FK-side indexes.**  Every foreign key to ``calls.id`` is checked when a call is
deleted, and four had no index on the referencing column: ``activity_events.call_id``,
``conversations.first_call_id``, ``recorder_gaps.resumed_call_id`` and
``notifications.call_id`` (all ``ON DELETE SET NULL``).  Each deleted call was therefore a
sequential scan of each table — for ``activity_events``, 6.3 M rows on production — so one
500-call retention batch meant thousands of full scans, and a first retention run would
have taken days.  Retention is not switched on in production
yet; these must land before it is.  ``activity_events.call_id`` is partial
(``WHERE call_id IS NOT NULL``): the planner proves ``call_id = $1`` implies it, and the
system/site lines without a call never enter the index.

``CREATE INDEX CONCURRENTLY`` inside an ``autocommit_block``, like b8d4e60a17c2's drops: a
plain build takes a SHARE lock that blocks every insert into ``activity_events`` — every
call accepted — for the length of the build.  ``IF NOT EXISTS`` makes a re-run after a
failure pick up where it stopped; a failed concurrent build leaves an INVALID index behind,
which the upgrade drops before it retries.

**Two enum values.**  ``audio_state`` gains ``missing`` (retention found no file where the
row said there was one — kept apart from ``purged``, and reversible), and ``alert_kind``
gains ``disk_low``.  Also in the autocommit block: PostgreSQL cannot use an enum value
added in the same transaction.  The downgrade leaves them — an enum value cannot be
dropped — after moving any ``missing`` rows back to ``present``.

**``disk_samples``** — the worker's five-minute free-space readings (app/disk_watch.py).

**The archive-path history.**  ``settings.retention_archive_roots`` is seeded from the
archive paths already configured (the global setting and every policy's), so a recording
already archived under one of them stays playable once :func:`app.audio.audio_roots`
starts reading the history instead of the single global path.

Revision ID: c4e9a1d07b35
Revises: 0ebc81310604
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e9a1d07b35"
down_revision: str | None = "0ebc81310604"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: name -> the concurrent build.  Order: smallest table first, so a failure on the big one
#: leaves the cheap three in place.
INDEXES: dict[str, str] = {
    "ix_notifications_call_id": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_notifications_call_id ON notifications (call_id)"
    ),
    "ix_recorder_gaps_resumed_call_id": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_recorder_gaps_resumed_call_id "
        "ON recorder_gaps (resumed_call_id)"
    ),
    "ix_conversations_first_call_id": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_conversations_first_call_id "
        "ON conversations (first_call_id)"
    ),
    "ix_activity_events_call_id": (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_activity_events_call_id "
        "ON activity_events (call_id) WHERE call_id IS NOT NULL"
    ),
}

ARCHIVE_ROOTS_KEY = "retention_archive_roots"


def _drop_if_invalid(bind: sa.engine.Connection, name: str) -> None:
    """A concurrent build that failed leaves an INVALID index that ``IF NOT EXISTS`` skips."""
    invalid = bind.exec_driver_sql(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %(name)s AND NOT i.indisvalid",
        {"name": name},
    ).first()
    if invalid is not None:
        bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def _seed_archive_roots(bind: sa.engine.Connection) -> None:
    paths: list[str] = []
    row = bind.exec_driver_sql(
        "SELECT value FROM settings WHERE key = 'retention_archive_path'"
    ).first()
    if row is not None and isinstance(row[0], str) and row[0].strip():
        paths.append(row[0].strip())
    for (path,) in bind.exec_driver_sql(
        "SELECT DISTINCT archive_path FROM retention_policies WHERE coalesce(archive_path, '') <> ''"
    ):
        paths.append(path.strip())
    paths = [p for i, p in enumerate(paths) if p.startswith("/") and p not in paths[:i]]
    if not paths:
        return
    existing = bind.exec_driver_sql(
        "SELECT value FROM settings WHERE key = %(key)s", {"key": ARCHIVE_ROOTS_KEY}
    ).first()
    known = list(existing[0]) if existing is not None and isinstance(existing[0], list) else []
    merged = known + [p for p in paths if p not in known]
    bind.exec_driver_sql(
        "INSERT INTO settings (key, value, description, updated_by) "
        "VALUES (%(key)s, CAST(%(value)s AS jsonb), %(description)s, 'migration') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        {
            "key": ARCHIVE_ROOTS_KEY,
            "value": json.dumps(merged),
            "description": "every archive path retention has used (app.retention)",
        },
    )


def upgrade() -> None:
    op.create_table(
        "disk_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("free_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False, server_default=sa.text("'ok'")),
    )
    op.create_index("ix_disk_samples_path_at", "disk_samples", ["path", "at"])
    _seed_archive_roots(op.get_bind())

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.exec_driver_sql("ALTER TYPE audio_state ADD VALUE IF NOT EXISTS 'missing'")
        bind.exec_driver_sql("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'disk_low'")
        for name, create in INDEXES.items():
            _drop_if_invalid(bind, name)
            bind.exec_driver_sql(create)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name in reversed(list(INDEXES)):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    # ``missing`` / ``disk_low`` stay in their enums (PostgreSQL cannot drop a value); the
    # rows that use them are put back to what the older code understands.
    op.execute("UPDATE calls SET audio_state = 'present' WHERE audio_state = 'missing'")
    op.execute("DELETE FROM alert_rules WHERE kind = 'disk_low'")
    op.drop_index("ix_disk_samples_path_at", table_name="disk_samples")
    op.drop_table("disk_samples")
    # The archive-path history is left in ``settings``: the older code ignores the key.
