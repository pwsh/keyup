"""``transcription_jobs.parked_reason``: tell an outage apart from a bad recording

docs/ARCHITECTURE.md §5.2 ("Automatic recovery from a backend outage").

A job that has failed ``max_attempts`` times is parked as ``failed`` and nothing ever
looks at it again.  That is right when the *recording* is the problem, and wrong when the
transcription server was down: on 2026-09-07 a Whisper outage parked 395 jobs that were
all perfectly transcribable, and an operator had to find them and call
``POST /api/admin/jobs/requeue`` by hand.

This column is what lets a later pass tell the two apart cheaply.  The worker stamps
``'backend_unavailable'`` when it parks a job whose last error was
``NoBackendAvailableError`` — every backend failed — and leaves it NULL for every other
failure.  The partial index is because the unpark pass runs every two minutes and finds
nothing almost every time.

Nothing is backfilled: rows parked before this existed have no recorded reason, and
guessing one from ``last_error`` would make history look like it was observed.

Revision ID: b6e1c30d94af
Revises: a4d7f2c9b168
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e1c30d94af"
down_revision: str | None = "a4d7f2c9b168"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transcription_jobs", sa.Column("parked_reason", sa.Text(), nullable=True))
    op.create_index(
        "ix_transcription_jobs_parked",
        "transcription_jobs",
        ["parked_reason"],
        postgresql_where=sa.text("parked_reason IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_transcription_jobs_parked", table_name="transcription_jobs")
    op.drop_column("transcription_jobs", "parked_reason")
