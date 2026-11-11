"""``bcfy_feed_daily``, and the hourly export's uptime pair on ``bcfy_feed_hourly``.

The feed dashboard offers two CSV exports (docs/BCFY_STATS.md, "Importing the dashboard
exports"), and they are the only listener history that reaches further back than a day.
This revision makes room for both.

``bcfy_feed_hourly.online_checks`` / ``online_reports`` are the hourly export's uptime pair
— how many times Broadcastify looked at the feed in that hour, and how many of those looks
found it up.  Both are **nullable**, and NULL is the normal state: a row this deployment
polled has no such figure to record, and only an imported row fills them in.

``bcfy_feed_daily`` is a new table with nothing but the daily export in it.  It is
deliberately not derived from ``bcfy_feed_hourly``: the two exports overlap for about a week
and need not agree there, so keeping them apart means neither can silently replace the
other.  ``uptime_pct`` is the only uptime figure in this schema at all — a poll knows only
whether the feed was up at one instant, and the hourly rollup does not keep even that.

Nothing here changes an existing row or an existing read.  The columns are added empty and
the table is created empty; both stay that way until somebody imports a file.

Revision ID: c9e3f1a7b420
Revises: b7c4d2e91a35
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e3f1a7b420"
down_revision: str | None = "b7c4d2e91a35"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bcfy_feed_hourly", sa.Column("online_checks", sa.Integer(), nullable=True))
    op.add_column("bcfy_feed_hourly", sa.Column("online_reports", sa.Integer(), nullable=True))
    op.create_table(
        "bcfy_feed_daily",
        sa.Column("feed_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("uptime_pct", sa.Float(), nullable=True),
        sa.Column("listeners_max", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("feed_id", "day"),
    )
    # The summary reads a window of days across whichever feed is configured; the primary
    # key already serves `feed_id, day`, and this serves the day-ordered scan.
    op.create_index("ix_bcfy_feed_daily_day", "bcfy_feed_daily", ["day"])


def downgrade() -> None:
    op.drop_index("ix_bcfy_feed_daily_day", table_name="bcfy_feed_daily")
    op.drop_table("bcfy_feed_daily")
    op.drop_column("bcfy_feed_hourly", "online_reports")
    op.drop_column("bcfy_feed_hourly", "online_checks")
