"""``bcfy_claims`` and ``call_exports.uploaded_by``: recorder-direct Broadcastify upload

docs/EXPORTERS.md ("Recorder-direct upload").

With ``broadcastify_upload_at = recorder`` each trunkd recorder registers its own call
with Broadcastify and PUTs the MP3 the moment the transmission ends, instead of waiting
for the call to reach keyup, be stored, and be picked up by the exporter worker three
seconds later.  Two things have to exist here for that:

``bcfy_claims``
    the tie-break.  Two recorders covering one site hear every call, and keyup itself
    still uploads whatever no recorder answered for.  Broadcastify keys a duplicate on
    ``(system, tg, ts)`` with a whole-second ``ts``, so two uploaders whose idea of the
    start of the same transmission differs by a second do not look like a duplicate to
    them — they look like two calls, and the archive gets both.  So every uploader asks
    here first, inside a per-talkgroup advisory lock, and a claim within ±2 s of an
    existing one loses.  ``instance`` is the recorder's ``instance_id`` or the literal
    ``keyup``.  ``outcome``/``response``/``call_id`` are stamped afterwards, because a
    claim is made before the call has been pushed and therefore before it has a row.

``call_exports.uploaded_by``
    which of them actually sent it.  Nullable and not backfilled: rows written before
    this existed have no answer, and writing ``keyup`` across them would make history
    look like it was observed rather than assumed.

Revision ID: a4d7f2c9b168
Revises: c1d5e9a37f24
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d7f2c9b168"
down_revision: str | None = "c1d5e9a37f24"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("call_exports", sa.Column("uploaded_by", sa.Text(), nullable=True))
    op.create_table(
        "bcfy_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tgid", sa.Integer(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("duration_s", sa.Numeric(8, 3), nullable=True),
        sa.Column("instance", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("response", sa.Text(), nullable=True),
        sa.Column("call_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bcfy_claims_tgid_ts", "bcfy_claims", ["tgid", "ts"])


def downgrade() -> None:
    op.drop_index("ix_bcfy_claims_tgid_ts", table_name="bcfy_claims")
    op.drop_table("bcfy_claims")
    op.drop_column("call_exports", "uploaded_by")
