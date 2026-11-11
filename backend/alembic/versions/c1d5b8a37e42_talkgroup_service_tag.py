"""``talkgroups.service_tag``: RadioReference's own classification of a channel.

Keyup already had ``talkgroups.category`` — a coarse local bucket the UI colors and the
calls filter groups by.  What it did not have was the classification RadioReference itself
publishes: a *service tag* ("Fire Dispatch", "Law Tac", "Public Works"), which is finer
than the bucket (three fire roles, all of them ``fire``) and is somebody else's vocabulary,
so it is stored verbatim rather than folded into the local slug.  Keeping the two apart
means the RR sync can fill in a classification without ever destroying an operator's, and
a re-sync can tell "RR says Fire-Tac" from "an operator called this fire".

One nullable Text column, no backfill and no constraint: existing rows are simply
unclassified by RR until a sync or an import says otherwise, and the vocabulary lives in
``app.models.RR_SERVICE_TAGS`` rather than in a database enum, because RR can add a tag
without asking us and a CHECK would then reject a value RR really did return.

Revision ID: c1d5b8a37e42
Revises: b6f2a9c4e771
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1d5b8a37e42"
down_revision: str | None = "b6f2a9c4e771"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("talkgroups", sa.Column("service_tag", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("talkgroups", "service_tag")
