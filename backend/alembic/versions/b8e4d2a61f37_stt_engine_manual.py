"""stt_engine enum: add 'manual' (a person's caption on a call that had no transcript).

Revision ID: b8e4d2a61f37
Revises: a7f3c2d91b64
Create Date: 2026-09-23

``PATCH /api/calls/{id}`` on a call with no transcript row used to write only
``calls.transcript_text``, which the transcription worker then overwrote.  The caption now
becomes a primary ``transcripts`` row of its own, ``engine='manual'`` and
``edited_by_user=true``, and the worker keeps it primary.

Added inside an ``autocommit_block`` because PostgreSQL cannot use an enum value inside the
transaction that added it.  Enum values cannot be dropped, so ``downgrade`` is a no-op.
"""

from __future__ import annotations

from alembic import op

revision: str = "b8e4d2a61f37"
down_revision: str | None = "a7f3c2d91b64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE stt_engine ADD VALUE IF NOT EXISTS 'manual'")


def downgrade() -> None:
    # PostgreSQL has no DROP VALUE; leaving the extra label in place is harmless.
    pass
