"""agencies.source: the seeded provenance is called ``seed``

The seeded agency rows used to be labelled with the name of the one installation whose list
they came from.  The seed is now a configurable CSV (``keyup seed-agencies --csv`` /
``KEYUP_SEED_AGENCIES``), so the provenance is simply ``seed``; ``local`` is unchanged.

Data only, no schema change.  Upgrade rewrites every row whose ``agencies.source`` is not
one of the values the code now knows (``seed``, ``local`` — ``app.models.AGENCY_SOURCES`` at
this revision, copied here so a later change to that tuple cannot change what this revision
did) to ``'seed'``, and touches nothing else — not the codes, names, ``active`` or
``edited_at`` — so the seed keeps treating exactly the same rows as its own.  Before this
revision only the seed ever wrote anything but ``local``, so the rows it catches are the
seeded ones under their old label.  On a database with no such rows (a fresh install) it
changes nothing.

Downgrade is a no-op on purpose: the old label was installation-specific and this revision
does not know it, so it cannot be put back.  ``seed`` rows stay ``seed``.

Revision ID: 3506d783396e
Revises: c5e2a8f41d07
Create Date: 2026-11-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3506d783396e"
down_revision: str | None = "c5e2a8f41d07"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


#: The provenances the code knows at this revision (``app.models.AGENCY_SOURCES``).
KNOWN_SOURCES = ("seed", "local")


def upgrade() -> None:
    op.execute(
        "UPDATE agencies SET source = 'seed' "
        f"WHERE source NOT IN ({', '.join(repr(s) for s in KNOWN_SOURCES)})"
    )


def downgrade() -> None:
    # The legacy label is not recorded anywhere, so there is nothing to restore it from.
    pass
