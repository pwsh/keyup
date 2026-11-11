"""calls.audio_state / audio_archived_at (retention tiers, §5.3)

Retention can purge a recording's MP3 while keeping the row, its transcript and every
link to it.  Without a marker the API cannot tell "audio not written yet" from "audio
deliberately removed", so the call carries its own tier: ``present`` (on disk at
``audio_root/file_path``), ``archived`` (moved to the policy's ``archive_path`` —
``file_path`` points at the new location) or ``purged`` (deleted).

Revision ID: b1c2d3e4f5a6
Revises: a30a7b714af5
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a30a7b714af5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIO_STATE = sa.Enum("present", "archived", "purged", name="audio_state")


def upgrade() -> None:
    AUDIO_STATE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "calls",
        sa.Column("audio_state", AUDIO_STATE, nullable=False, server_default=sa.text("'present'")),
    )
    op.add_column("calls", sa.Column("audio_archived_at", sa.DateTime(timezone=True), nullable=True))
    # Retention scans "everything still on disk older than N days"; the partial index keeps
    # that scan off the 99 % of rows that are present.
    op.create_index(
        "ix_calls_audio_state",
        "calls",
        ["audio_state"],
        postgresql_where=sa.text("audio_state <> 'present'"),
    )


def downgrade() -> None:
    op.drop_index("ix_calls_audio_state", table_name="calls")
    op.drop_column("calls", "audio_archived_at")
    op.drop_column("calls", "audio_state")
    AUDIO_STATE.drop(op.get_bind(), checkfirst=True)
