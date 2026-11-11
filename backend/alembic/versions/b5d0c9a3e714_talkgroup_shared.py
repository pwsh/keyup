"""``talkgroups.shared``: the shared channels no single agency owns.

On a typical county system a handful of channels — the dispatch channel, the law and fire
interop channels, event channels — are used by every agency and owned by none.  None of
them carries an ``agency_code``, which is true (nobody owns them) and useless
(``?agency=`` can therefore never surface the very channel a call went out on).  The
column lets a consumer read that set out of the API instead of hardcoding it.

``NOT NULL DEFAULT false``: every existing channel is owned by somebody or by nobody, and
neither is "shared" until an operator says so — with ``keyup mark-shared`` (a tgid list or
``--file``), the Talkgroups page, or ``PATCH /api/talkgroups/{id}``.

This revision originally also marked one installation's shared channels in a data step.
That step was removed when the project was published: it named that site's tgids, and it
was a no-op on any fresh database anyway (no talkgroups exist when migrations first run).
A database that already ran it keeps what it did; Alembic never re-runs a revision.

Revision ID: b5d0c9a3e714
Revises: d7c4e0b91a58
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5d0c9a3e714"
down_revision: str | None = "d7c4e0b91a58"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "talkgroups",
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("talkgroups", "shared")
