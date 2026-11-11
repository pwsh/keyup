"""``recorders.station`` / ``counters`` / ``reported_at``: the recorder describes itself (§4.8).

Until now keyup knew a recorder's *tuners* in detail and the recorder itself barely at all —
an ``instance_id``, a version string and a last-seen stamp.  A recorder posts three more
things now: who and where the station is (operator-entered: name, location, lat/lon,
elevation, contact, notes), the instance-level lifetime counters (calls started/ended,
uploaded/spooled/failed, spool bytes, free disk), and — inside each tuner entry, so needing
no migration — the per-tuner hardware description, gain settings, RSSI and frequency error.

Both new columns are nullable JSONB and both are written *only when the push carries them*:
absent means "the recorder did not say", not "clear it", so a recorder running an older
binary cannot null out what a newer one reported.  ``reported_at`` records when either was
last actually reported, which is deliberately not ``last_seen_at`` — every contact bumps
that one, so it cannot tell stale self-description from a recorder gone quiet.

Purely additive: a database with recorders in it needs no backfill, and an unchanged
recorder keeps behaving exactly as it did.

Revision ID: b6f2a9c4e771
Revises: a4c8e1b7d260
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6f2a9c4e771"
down_revision: str | None = "a4c8e1b7d260"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recorders", sa.Column("station", postgresql.JSONB(), nullable=True))
    op.add_column("recorders", sa.Column("counters", postgresql.JSONB(), nullable=True))
    op.add_column(
        "recorders", sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("recorders", "reported_at")
    op.drop_column("recorders", "counters")
    op.drop_column("recorders", "station")
