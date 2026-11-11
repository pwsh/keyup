"""``conversations (talkgroup_id, ended_at)`` — the index the conversation grouper needs.

:func:`app.ingest.service.assign_conversation` asks one question of this table, once per
call ingested and once per call the timestamp repair regroups::

    SELECT * FROM conversations
     WHERE talkgroup_id = ? AND ended_at >= ? AND started_at <= ?
     ORDER BY ended_at DESC LIMIT 1

Both indexes the table had answer only half of it:

* ``ix_conversations_talkgroup_started (talkgroup_id, started_at)`` seeks the talkgroup and
  the ``started_at <=`` bound — which every thread the talkgroup has *ever* had satisfies,
  because they all started before the call being placed.  ``ended_at >=`` then throws all
  of them away, one row at a time.
* ``ix_conversations_ended_at (ended_at)`` seeks the ``ended_at >=`` bound but not the
  talkgroup, so it walks every thread on *every* talkgroup newer than the call.

Either way the work per call grows with the archive.  Live ingest never noticed: it places
calls at the newest end, where "the talkgroup's whole history" and "the last few seconds"
are the same handful of rows once the planner has a recent-enough ``started_at`` bound.
The backlog timestamp repair walks 2022 forwards through a million calls, and by January
2024 each lookup was reading and discarding every thread the phase had already rebuilt on
that talkgroup — measured at 250, then 1000, then 2000 rows discarded *per call* as the
already-regrouped tail grew, which is the phase's 1000-calls-per-2-seconds collapsing to
1000-calls-per-5-minutes.

``(talkgroup_id, ended_at)`` makes both halves index conditions and hands the
``ORDER BY ended_at DESC LIMIT 1`` its rows already sorted, so the scan stops at the first
thread that can possibly match: flat, whatever the archive weighs.

Built ``CONCURRENTLY`` and outside a transaction: ``conversations`` has a row per thread in
a million-call archive, and both the live feed and an interrupted repair have to keep
running while it is created.

Revision ID: a9e4b2c17f08
Revises: f8c2a4e19d53
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a9e4b2c17f08"
down_revision: str | None = "f8c2a4e19d53"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

INDEX = "ix_conversations_talkgroup_ended"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON conversations (talkgroup_id, ended_at)"
        )
        # The planner cannot choose this index over the one it has been choosing while its
        # picture of the table is the one it gathered before the repair rewrote every
        # timestamp in it (see app.repair_timestamps.regroup_repaired).
        op.execute("ANALYZE conversations")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
