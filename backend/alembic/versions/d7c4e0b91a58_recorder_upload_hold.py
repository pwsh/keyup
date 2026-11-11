"""``recorders`` upload hold: recorder-level desired state and the uplink it reports (§4.8).

Until now everything keyup wanted of a recorder was said *per tuner*.  A hold on the
uploads is not a tuner's business — it is the whole instance's upload path — so it needs a
desired value on the recorder row, and a generation of its own for the recorder to echo.

Four columns, all additive:

* ``desired_spool_only`` — three-valued on purpose.  ``true`` hold, ``false`` upload, and
  NULL "keyup has no opinion", which is the default and leaves the recorder on whatever its
  own config or panel says.  A recorder nobody has ever told anything must not be
  overridden by the mere absence of a row value;
* ``desired_recorder_version`` — bumped on every operator write of the above, and kept
  apart from ``recorders.version`` (which any tuner write moves) so the counter the
  recorder echoes cannot be shifted underneath it by an unrelated tuner edit;
* ``applied_recorder_version`` — what the recorder says it has applied, NULL until one
  tells us, which is every recorder running an older binary;
* ``uplink`` — the recorder's own report of its upload path (holding or not, where the hold
  came from, what is waiting in the spool, the URL), stored verbatim as JSONB exactly as
  ``tuners.reported`` is.

Nothing to backfill: a NULL desire is the "no opinion" the old behavior already was, and
version 0 with no applied version reads as "never edited, nothing pending".

Revision ID: d7c4e0b91a58
Revises: c4f8a1e07b32
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7c4e0b91a58"
down_revision: str | None = "c4f8a1e07b32"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recorders", sa.Column("desired_spool_only", sa.Boolean(), nullable=True))
    op.add_column(
        "recorders",
        sa.Column(
            "desired_recorder_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("recorders", sa.Column("applied_recorder_version", sa.Integer(), nullable=True))
    op.add_column("recorders", sa.Column("uplink", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("recorders", "uplink")
    op.drop_column("recorders", "applied_recorder_version")
    op.drop_column("recorders", "desired_recorder_version")
    op.drop_column("recorders", "desired_spool_only")
