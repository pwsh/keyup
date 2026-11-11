"""audio processing: processed copies, tone/alert flags, the audio_jobs queue

Adds the columns §5.4 writes onto a call — the processed copy's path, everything the pass
measured, and the two booleans a reader filters on — plus the queue the pass is driven
from.

``calls.alert`` is deliberately *not* ``calls.emergency``: emergency is what the radio
asserted and the recorder reported, alert is what the audio sounds like (a page), and
neither may overwrite the other.  Both new booleans are NOT NULL with a false default, so
the whole archive reads as "no tone found yet" until the backfill runs, and both indexes
are partial: pages are a small minority of the rows and an index over the false side would
be the whole table.

``job_status`` already exists (transcription_jobs, export_jobs), so it is referenced with
``create_type=False`` rather than created again.

Revision ID: a4c9e1f27b60
Revises: 90e283a5863d
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c9e1f27b60"
down_revision: str | None = "90e283a5863d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

job_status = postgresql.ENUM(
    "queued", "running", "done", "failed", "cancelled", name="job_status", create_type=False
)


def upgrade() -> None:
    op.add_column("calls", sa.Column("processed_path", sa.Text(), nullable=True))
    op.add_column(
        "calls", sa.Column("audio_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "calls",
        sa.Column(
            "tone_detected", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "calls", sa.Column("alert", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.create_index(
        "ix_calls_tone_detected", "calls", ["tone_detected"], postgresql_where=sa.text("tone_detected")
    )
    op.create_index("ix_calls_alert", "calls", ["alert"], postgresql_where=sa.text("alert"))

    op.create_table(
        "audio_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("status", job_status, server_default=sa.text("'queued'"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audio_jobs_claim", "audio_jobs", ["status", "priority", "scheduled_at"])
    op.create_index("ix_audio_jobs_call_id", "audio_jobs", ["call_id"])
    op.create_index(
        "ix_audio_jobs_open_call",
        "audio_jobs",
        ["call_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_audio_jobs_open_call", table_name="audio_jobs")
    op.drop_index("ix_audio_jobs_call_id", table_name="audio_jobs")
    op.drop_index("ix_audio_jobs_claim", table_name="audio_jobs")
    op.drop_table("audio_jobs")
    op.drop_index("ix_calls_alert", table_name="calls")
    op.drop_index("ix_calls_tone_detected", table_name="calls")
    op.drop_column("calls", "alert")
    op.drop_column("calls", "tone_detected")
    op.drop_column("calls", "audio_meta")
    op.drop_column("calls", "processed_path")
