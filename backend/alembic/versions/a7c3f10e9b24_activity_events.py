"""``activity_events``: a bounded operational log of the ingest/transcription pipeline.

One row per notable pipeline moment (a call accepted, a duplicate rejected, a
transcription queued/done/failed, an error), written in the same transaction as the work
it records.  The Admin → Activity page backfills recent rows then tails the live bus, and
``keyup activity --follow`` polls it; the nightly retention pass prunes rows older than
``ACTIVITY_RETENTION_DAYS``.

Revision ID: a7c3f10e9b24
Revises: f4d1c7a92e30
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3f10e9b24"
down_revision: str | None = "f4d1c7a92e30"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "activity_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], name=op.f("fk_activity_events_call_id_calls"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_events")),
    )
    op.create_index("ix_activity_events_at", "activity_events", [sa.text("at DESC")])
    op.create_index("ix_activity_events_source", "activity_events", ["source"])
    op.create_index("ix_activity_events_level", "activity_events", ["level"])


def downgrade() -> None:
    op.drop_index("ix_activity_events_level", table_name="activity_events")
    op.drop_index("ix_activity_events_source", table_name="activity_events")
    op.drop_index("ix_activity_events_at", table_name="activity_events")
    op.drop_table("activity_events")
