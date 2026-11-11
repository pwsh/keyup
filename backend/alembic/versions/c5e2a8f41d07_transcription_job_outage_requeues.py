"""``transcription_jobs.outage_requeues``: cap the automatic requeue after an outage

docs/ARCHITECTURE.md §5.2 ("Automatic recovery from a backend outage").

The unpark pass (b6e1c30d94af) requeues a job parked as ``backend_unavailable`` once a
backend is healthy again, and resets ``attempts`` to zero when it does.  That is right for
an outage and a loop for a recording every backend times out on while serving everybody
else: on 2026-09-23 call 5237413 was parked and requeued like that for two hours, holding a
slot on all three Whisper servers each round, until an operator parked it by hand.

The worker now tells the two apart, and this counter is the backstop for when it cannot:
the unpark pass increments it and skips a row that has reached the cap, and the worker parks
a job that fails like an outage past the cap as ``outage_requeue_cap``, where the pass does
not look.  An operator's requeue resets it.

``NOT NULL DEFAULT 0`` is a catalog-only change on PostgreSQL 11 and later: no table
rewrite, however many jobs there are.

Revision ID: c5e2a8f41d07
Revises: d1a7e5c30b48
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e2a8f41d07"
down_revision: str | None = "d1a7e5c30b48"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transcription_jobs",
        sa.Column("outage_requeues", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("transcription_jobs", "outage_requeues")
