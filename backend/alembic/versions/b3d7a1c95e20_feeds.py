"""``feeds``: a saved call filter published as RSS and Atom.

A feed is a ``GET /calls`` filter object plus the secret that lets a reader fetch it
without an account.  The token is the credential, so it is ``UNIQUE`` and indexed — every
fetch is a lookup by it — and ``enabled`` exists so a leaked URL can be turned off
without losing the filter that took someone ten minutes to build.

``max_items`` is stored rather than passed on the URL for the same reason: what a feed
publishes is the feed's configuration, not the reader's.

Revision ID: b3d7a1c95e20
Revises: e5b1d93c27af
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3d7a1c95e20"
down_revision: str | None = "e5b1d93c27af"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feeds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("query", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("include_audio", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("max_items", sa.Integer(), nullable=False, server_default=sa.text("50")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetch_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("user_id", "name", name="uq_feeds_user_name"),
    )
    op.create_index("ix_feeds_token", "feeds", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_feeds_token", table_name="feeds")
    op.drop_table("feeds")
