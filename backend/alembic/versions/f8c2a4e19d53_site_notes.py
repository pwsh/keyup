"""``sites.notes``: the operator's own free text about a tower site.

Every other editable directory row already has one — ``talkgroups.notes``,
``radios.notes``, ``units.notes`` — and a site was the odd one out: the only place to write
"the school district's traffic lands here by mistake, see the refile" was
``description``, which the RadioReference sync fills in and the channel-audit panel reads
as RR's description of the site.  Overloading it would make an operator's note look like
documentation RR published.

So one nullable Text column, no backfill and no constraint, kept strictly apart from
``description`` the same way ``talkgroups.category`` is kept apart from ``service_tag``:
one column is somebody else's fact, the other is ours.

Revision ID: f8c2a4e19d53
Revises: e7b3d9a24c18
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f8c2a4e19d53"
down_revision: str | None = "e7b3d9a24c18"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "notes")
