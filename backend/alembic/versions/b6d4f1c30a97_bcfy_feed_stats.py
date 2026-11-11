"""Broadcastify feed-owner statistics: samples, their hourly rollup, and archive listings.

Three tables behind ``docs/BCFY_STATS.md``:

``bcfy_feed_samples``
    one row per ``a=feed`` poll, with the whole response in ``raw``.  Storing the raw JSON
    is deliberate and is the reason this feature can ship before anybody has seen a real
    response: the owner API's field names are undocumented, so the mapping that fills
    ``listeners``/``online``/``extracted`` is a guess that will be corrected, and the
    correction re-derives history from ``raw`` instead of losing it.

``bcfy_feed_hourly``
    the poller's own rollup, upserted as each sample lands.  A year of minute samples is
    half a million rows; the same year is 8 760 here, which is what makes a months-long
    chart and a 7x24 heatmap cheap.

``bcfy_archives``
    the per-day archive listing, unique on ``(feed_id, start_at)`` so a re-sync updates a
    block's (time-limited) download URL rather than duplicating it.

Revision ID: b6d4f1c30a97
Revises: c1f70a4b82d5
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b6d4f1c30a97"
down_revision: str | None = "c1f70a4b82d5"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bcfy_feed_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feed_id", sa.Text(), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("listeners", sa.Integer(), nullable=True),
        sa.Column("online", sa.Boolean(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extracted", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bcfy_feed_samples")),
    )
    op.create_index(
        "ix_bcfy_feed_samples_feed_polled", "bcfy_feed_samples", ["feed_id", "polled_at"], unique=False
    )

    op.create_table(
        "bcfy_feed_hourly",
        sa.Column("feed_id", sa.Text(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("listeners_avg", sa.Float(), nullable=True),
        sa.Column("listeners_max", sa.Integer(), nullable=True),
        sa.Column("samples", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("feed_id", "hour", name=op.f("pk_bcfy_feed_hourly")),
    )
    op.create_index("ix_bcfy_feed_hourly_hour", "bcfy_feed_hourly", ["hour"], unique=False)

    op.create_table(
        "bcfy_archives",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feed_id", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bcfy_archives")),
        sa.UniqueConstraint("feed_id", "start_at", name="uq_bcfy_archives_feed_start"),
    )
    op.create_index("ix_bcfy_archives_feed_day", "bcfy_archives", ["feed_id", "day"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bcfy_archives_feed_day", table_name="bcfy_archives")
    op.drop_table("bcfy_archives")
    op.drop_index("ix_bcfy_feed_hourly_hour", table_name="bcfy_feed_hourly")
    op.drop_table("bcfy_feed_hourly")
    op.drop_index("ix_bcfy_feed_samples_feed_polled", table_name="bcfy_feed_samples")
    op.drop_table("bcfy_feed_samples")
