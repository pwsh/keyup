"""Where an hourly listener row came from: ``bcfy_feed_hourly.source``.

One nullable text column.  ``NULL`` means what every existing row means and what the
poller keeps writing — this deployment's own ``a=feed`` polls — so the migration adds a
column and rewrites nothing.  ``'page'`` marks an hour reconstructed from the **public**
feed page's 24-hour sparkline (docs/BCFY_STATS.md, "The public feed page"), which is the
only backfill Broadcastify offers and which a real poll always replaces.

Nullable rather than ``NOT NULL DEFAULT 'poll'`` on purpose: the useful query is "which
rows are *not* mine", the column is read by exception, and a default would rewrite every
row of a table that exists to be cheap.

Revision ID: a4e91c2b7d60
Revises: b6d4f1c30a97
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e91c2b7d60"
down_revision: str | None = "b6d4f1c30a97"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bcfy_feed_hourly", sa.Column("source", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("bcfy_feed_hourly", "source")
