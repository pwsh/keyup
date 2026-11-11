"""cross-recorder call reconciliation — the decision, recorded on both sides of a pair

docs/RECONCILIATION.md, docs/ARCHITECTURE.md §5.1 step 4.  Two recorders cover one system,
so one transmission arrives twice; reconciliation keeps the better copy as the primary row
and hides the other behind ``duplicate_of``.  Two columns record why:

``calls.reconciled_at``
    when reconciliation last ruled on this row.  Set on **both** sides of a pair, so
    "reconciled and left alone as the primary" and "never looked at" are different states.
``calls.reconcile_reason``
    the ruling itself, as JSONB: the match (rule, overlap, the channel key it matched on),
    both copies' scores broken into their components, which component decided, and which
    fields were merged.  Nothing parses it back — it is there so "why is my call hidden"
    is answerable a year later without re-deriving anything.

No new ``time_source``.  The sdrtrunk stamp was investigated here and left alone: measured
against trunkd's control-channel grant times it is the instant the recorder *closed* the
file, ``call end + 1.7 s`` (sd 0.43 s over 17 pairs), so ``started_at = stamp − duration``
is right in principle and runs about two seconds late.  That bias is what
``RECONCILE_CLOCK_SKEW_SECONDS`` is sized for, and it is why ``file_write`` is the one time
source reconciliation does not treat as exact.

``ix_calls_ingested_at`` is what the batch pass walks: "everything ingested since the last
pass", which is how a backlog uploaded days after the live copy still finds its twin.

Revision ID: b8d4e2f70c15
Revises: a1b2c3d4e5f6
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8d4e2f70c15"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "calls", sa.Column("reconcile_reason", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index("ix_calls_ingested_at", "calls", [sa.text("ingested_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_calls_ingested_at", table_name="calls")
    op.drop_column("calls", "reconcile_reason")
    op.drop_column("calls", "reconciled_at")
