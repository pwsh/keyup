"""ingest_source='trunkd' — the fourth way a call reaches the archive

docs/ARCHITECTURE.md §1.6, docs/RECORDERS.md §5.  trunkd pushes over the same
Rdio-Scanner contract trunk-recorder's uploader uses — same ``sources``/``frequencies``
lists, same bare-member ``patches`` — so nothing in the *shape* of the form tells the two
apart.  It names itself instead (``recorder=trunkd``), and that name needs a value in the
``ingest_source`` enum so "which recorder produced this row" stays answerable in SQL.

One value, no columns: trunkd's extra metadata (``site`` aside) is ``nac``, ``algorithm``
and ``keyId``, which live in ``calls.raw_meta`` like every other recorder-specific field.

The value is added inside an ``autocommit_block`` for the reason
``d4e5f6a7b8c9`` documents: PostgreSQL cannot *use* an enum value added by the transaction
it is still in.  ``IF NOT EXISTS`` makes the step re-runnable after a half-applied upgrade.

Downgrade cannot remove the value (PostgreSQL has no ``ALTER TYPE … DROP VALUE``), so it
rewrites the rows that use it to ``upload`` — which is what they would have been before
this revision — leaving the schema consistent with the old vocabulary.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ingest_source ADD VALUE IF NOT EXISTS 'trunkd'")


def downgrade() -> None:
    op.execute("UPDATE calls SET ingest_source = 'upload' WHERE ingest_source = 'trunkd'")
