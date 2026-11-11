"""feeds.token_id: a feed made by an API token is owned by that token.

A token principal has no user, so every feed a token created was stored with
``user_id NULL`` — and the ownership check compared ``NULL == NULL``.  Any ``read`` token
could list every other token's feeds with their secret URLs, and any ``write`` token could
edit, rotate or delete them.

The new column records the creating token.  ``ON DELETE SET NULL``: deleting a token must
not delete the feeds it published (subscribers still hold those URLs); the feed simply
becomes an admin's to manage, which is also what every existing token-made feed becomes,
since nothing recorded which token made it.

Revision ID: 09bfeda20009
Revises: b8e4d2a61f37
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "09bfeda20009"
down_revision: str | None = "b8e4d2a61f37"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("api_tokens.id", ondelete="SET NULL", name="fk_feeds_token_id_api_tokens"),
            nullable=True,
        ),
    )
    op.create_index("ix_feeds_token_id", "feeds", ["token_id"])


def downgrade() -> None:
    op.drop_index("ix_feeds_token_id", table_name="feeds")
    op.drop_constraint("fk_feeds_token_id_api_tokens", "feeds", type_="foreignkey")
    op.drop_column("feeds", "token_id")
