"""``link_method`` gains ``text`` and ``time+text``: a match the transcript made.

The vocabulary was written when the only evidence Keyup could offer was *when* a call
happened, *whose* channel it was on and *which* radio keyed up: ``time``, ``time+agency``,
``unit``, ``manual``.  A consumer that re-scores our candidates with knowledge we do not
have — a CAD integration checks whether the words of its own incident were actually said — had
nowhere to record that, and had to file a transcript match as ``time`` or as ``manual``.
Both are wrong in the same direction: they hide the strongest evidence there is.

``text`` is "the transcript names call-specific words"; ``time+text`` is that plus nothing
but the window — which is a weaker claim than ``text`` on an agency's own channel and is
worth being able to say separately.

``ALTER TYPE … ADD VALUE`` runs in an ``autocommit_block`` for the reason ``d4e5f6a7b8c9``
documents: PostgreSQL will not let the transaction that added an enum value *use* it.
``IF NOT EXISTS`` makes the step re-runnable after a half-applied upgrade.

Downgrade cannot remove a value — PostgreSQL has no ``DROP VALUE`` — so it rewrites the
rows that use one back to ``time``, the honest weakest reading of a link whose real basis
the old vocabulary cannot express.

Revision ID: e6a2f80c4b19
Revises: b5d0c9a3e714
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e6a2f80c4b19"
down_revision: str | None = "b5d0c9a3e714"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_METHODS = ("text", "time+text")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_METHODS:
            op.execute(f"ALTER TYPE link_method ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    for value in NEW_METHODS:
        op.execute(f"UPDATE external_links SET link_method = 'time' WHERE link_method = '{value}'")
