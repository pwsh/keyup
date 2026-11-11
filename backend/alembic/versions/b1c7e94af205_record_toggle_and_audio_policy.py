"""The per-talkgroup **Record** toggle, and the per-call audio policy it decides.

``talkgroups.record`` is the second half of the pair ``transcribe`` has been alone in for
as long as there has been a directory: ``transcribe`` says whether the words are worth
keeping, this one says whether the *audio* is.  Default **true**, so the column changes
nothing on its own — every existing channel keeps recording exactly as it did.

``calls.audio_policy`` is what the ingest wrote down about that decision **at the moment
the call landed**, in the same way ``transcript_status`` is.  It is deliberately not
re-derived from the talkgroup later: an operator who turns Record back on should not have
yesterday's calls quietly become keepers, and one who turns it off should not have the
whole archive of that channel deleted by the next pass.  ``keep`` (the default, and every
row that exists today) or ``discard``.

``audio_state`` grows a fourth value, ``discarded``.  It sits beside ``purged`` rather than
reusing it because the two are different facts: ``purged`` is retention expiring a
recording that was kept for its window, ``discarded`` is a channel whose audio was never
meant to be kept at all.  Everything that already reads ``audio_state <> 'present'`` — the
API's ``has_audio``, the 410 on the audio route, the player — treats the new value
correctly without being told about it.

Revision ID: b1c7e94af205
Revises: a4d9e13c7b62
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c7e94af205"
down_revision: str | None = "a4d9e13c7b62"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "talkgroups",
        sa.Column("record", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "calls",
        sa.Column(
            "audio_policy", sa.Text(), nullable=False, server_default=sa.text("'keep'")
        ),
    )
    # PostgreSQL 12+ takes ADD VALUE inside the migration's transaction; the new label may
    # not be *used* in the same transaction, which nothing here does.
    op.execute("ALTER TYPE audio_state ADD VALUE IF NOT EXISTS 'discarded'")
    # The discard pass's whole working set, and nothing else: a partial index over the two
    # columns it filters on keeps a pass that finds nothing to a single index probe, which
    # is what it does once a minute for ever on a deployment that never turns Record off.
    op.create_index(
        "ix_calls_audio_policy_discard",
        "calls",
        ["ingested_at"],
        postgresql_where=sa.text("audio_policy = 'discard' AND audio_state = 'present'"),
    )


def downgrade() -> None:
    op.drop_index("ix_calls_audio_policy_discard", table_name="calls")
    op.drop_column("calls", "audio_policy")
    op.drop_column("talkgroups", "record")
    # The enum label is left in place: dropping a value from a PostgreSQL enum means
    # rewriting the type and every column that uses it, and a spare label costs nothing.
