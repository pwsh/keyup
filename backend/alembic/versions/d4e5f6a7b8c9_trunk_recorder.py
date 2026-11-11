"""trunk-recorder ingest: call_sources, sidecar metadata, two new enum values

docs/ARCHITECTURE.md §1.7 and §3.2.  Three things:

* ``call_sources`` — trunk-recorder's ``srcList``: every radio that keyed up inside one
  call, with its offset into the audio.  sdrtrunk ingest writes one row at ``pos_s = 0``.
* the sidecar columns on ``calls`` — ``audio_type``, ``signal_db``, ``noise_db``,
  ``freq_error``, ``recorder_call_num``, and ``raw_meta``, which keeps the recorder's own
  metadata verbatim (the whole sidecar, or an sdrtrunk file's ID3 frame map).
* two enum values: ``ingest_source='trunk_recorder'`` and ``time_source='sidecar'``.

The enum values are added inside an ``autocommit_block``.  PostgreSQL cannot *use* a value
added by the transaction it is still in ("unsafe use of new value"), and before 12 could
not add one inside a transaction at all; committing the ``ALTER TYPE`` on its own sidesteps
both.  ``IF NOT EXISTS`` makes the step re-runnable after a half-applied upgrade.

Downgrade removes the table and the columns.  It does **not** remove the enum values:
PostgreSQL cannot drop one, and rebuilding both types would mean rewriting every row of
``calls``.  Rows written by a trunk-recorder ingest are rewritten to ``file``/``file_write``
first, so the downgraded schema is consistent with the old vocabulary.

Revision ID: d4e5f6a7b8c9
Revises: c7a1e0d24b93
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c7a1e0d24b93"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ingest_source ADD VALUE IF NOT EXISTS 'trunk_recorder'")
        op.execute("ALTER TYPE time_source ADD VALUE IF NOT EXISTS 'sidecar'")

    op.add_column("calls", sa.Column("audio_type", sa.Text(), nullable=True))
    op.add_column("calls", sa.Column("signal_db", sa.Integer(), nullable=True))
    op.add_column("calls", sa.Column("noise_db", sa.Integer(), nullable=True))
    op.add_column("calls", sa.Column("freq_error", sa.Integer(), nullable=True))
    op.add_column("calls", sa.Column("recorder_call_num", sa.Integer(), nullable=True))
    op.add_column(
        "calls", sa.Column("raw_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )

    op.create_table(
        "call_sources",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("radio_id", sa.BigInteger(), nullable=True),
        sa.Column("src", sa.BigInteger(), nullable=True),
        sa.Column("pos_s", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("len_s", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("emergency", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("tag", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], name=op.f("fk_call_sources_call_id_calls"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["radio_id"],
            ["radios.id"],
            name=op.f("fk_call_sources_radio_id_radios"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_sources")),
    )
    op.create_index("ix_call_sources_call", "call_sources", ["call_id"], unique=False)
    op.create_index("ix_call_sources_radio", "call_sources", ["radio_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_call_sources_radio", table_name="call_sources")
    op.drop_index("ix_call_sources_call", table_name="call_sources")
    op.drop_table("call_sources")

    # The enum values survive (PostgreSQL cannot drop one), so put the rows that use them
    # back on the vocabulary the older schema knows before the columns go.
    op.execute("UPDATE calls SET ingest_source = 'file' WHERE ingest_source = 'trunk_recorder'")
    op.execute("UPDATE calls SET time_source = 'file_write' WHERE time_source = 'sidecar'")

    op.drop_column("calls", "raw_meta")
    op.drop_column("calls", "recorder_call_num")
    op.drop_column("calls", "freq_error")
    op.drop_column("calls", "noise_db")
    op.drop_column("calls", "signal_db")
    op.drop_column("calls", "audio_type")
