"""Tuner edits, instance attribution, archived event logs and link peers (§4.8).

Four unrelated-looking additions that all arrive with the same trunkd release, so they
share one migration rather than four:

* **Instance attribution.**  ``recorder_observations.instance`` and ``radio_events.instance``
  record *which recorder instance* said a thing.  Both tables already carry a ``recorder``
  name ("trunkd") — that is the *software*, not the machine, and two boxes hearing one site
  were until now indistinguishable in either table.  Nullable for ever: every row written
  before this, and every row from an older binary, genuinely does not know.  The index on
  ``(instance, at)`` is what makes the recorder page's "only this instance" filter a range
  scan instead of a sequential read of the whole event table.
* **Archived event logs.**  ``event_log_archives`` is the receipt for one uploaded
  ``<date>.jsonl.gz``: which instance, which day, how big, its sha256 and where it landed
  under ``EVENT_LOG_ROOT``.  Unique on ``(instance, date)`` because a day is uploaded once;
  a re-upload of the same digest is a 409 and a re-upload of a *different* digest replaces
  the row (the recorder rebuilt the day).
* **Link peers and event-log status.**  ``recorders.link`` and ``recorders.event_log`` are
  what the recorder reports about the peers it is linked to and about its own archive
  backlog.  Both are opaque JSONB, stored verbatim exactly as ``tuners.reported`` and
  ``recorders.uplink`` are: the field set is the recorder's to grow.

Nothing to backfill.  Every column is nullable and every absence reads as "the recorder did
not say", which is what an older binary means.

Revision ID: e2b9d47a1c60
Revises: d7c4e0b91a58
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2b9d47a1c60"
down_revision: str | None = "d7c4e0b91a58"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recorder_observations", sa.Column("instance", sa.Text(), nullable=True))
    op.add_column("radio_events", sa.Column("instance", sa.Text(), nullable=True))
    op.create_index("ix_radio_events_instance_at", "radio_events", ["instance", "at"])

    op.add_column("recorders", sa.Column("link", postgresql.JSONB(), nullable=True))
    op.add_column("recorders", sa.Column("event_log", postgresql.JSONB(), nullable=True))

    op.create_table(
        "event_log_archives",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("instance", "date", name="uq_event_log_archives_instance_date"),
    )
    op.create_index(
        "ix_event_log_archives_instance_date",
        "event_log_archives",
        ["instance", sa.text("date DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_event_log_archives_instance_date", table_name="event_log_archives")
    op.drop_table("event_log_archives")
    op.drop_column("recorders", "event_log")
    op.drop_column("recorders", "link")
    op.drop_index("ix_radio_events_instance_at", table_name="radio_events")
    op.drop_column("radio_events", "instance")
    op.drop_column("recorder_observations", "instance")
