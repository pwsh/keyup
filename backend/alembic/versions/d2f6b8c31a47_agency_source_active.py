"""agencies: provenance, an active flag and an edit stamp

Keyup can now hold agencies the seeded agency list has no code for — a neighbouring county,
a railroad, a school district — so an agency row has to say where it came from.  ``source``
is the whole point: :func:`app.seed.seed_agencies` refreshes seeded rows and never touches
``local`` ones, and only a ``local`` row may be deleted.

The data step marks every row that exists when this runs as seeded, which is true by
construction — until now the only way a row got here was the seed.  (The value was
originally the name of the installation the list came from; revision 3506d783396e renamed
it to ``seed`` on databases that already had it, and this step now writes ``seed`` directly
on any database that has not run it yet.)  ``active``
starts true so nothing disappears from a picker, and ``edited_at`` starts NULL, meaning
"nobody here has renamed this", which is what lets the seed keep refreshing names.

Revision ID: d2f6b8c31a47
Revises: b7c4d2e91a35
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2f6b8c31a47"
down_revision: str | None = "b7c4d2e91a35"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agencies",
        # The server default is 'local' so that a row inserted by something that has not
        # been taught about the column is treated as *not* seed-owned — the safe way round,
        # since the seed then leaves it alone rather than overwriting it.
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'local'")),
    )
    op.add_column(
        "agencies",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("agencies", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
    # Everything already in the table came from the shared seed; nothing else could put a
    # row there before this revision.
    op.execute("UPDATE agencies SET source = 'seed'")
    op.create_index("ix_agencies_source", "agencies", ["source"])


def downgrade() -> None:
    op.drop_index("ix_agencies_source", table_name="agencies")
    op.drop_column("agencies", "edited_at")
    op.drop_column("agencies", "active")
    op.drop_column("agencies", "source")
