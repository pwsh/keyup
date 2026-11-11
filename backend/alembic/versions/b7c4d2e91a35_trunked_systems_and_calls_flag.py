"""``systems.trunked`` and ``talkgroups.export_bcfy_calls``: the Broadcastify Calls gate.

Two columns, one rule between them: **Broadcastify Calls publishes trunked systems only**
(docs/EXPORTERS.md).  Calls files each upload under a system id issued for a trunked
system; a conventional channel has nothing to be filed under, and keyup archives both
kinds side by side, so the distinction has to be a fact the database holds rather than
something an operator remembers.

``systems.trunked`` is **nullable**, and NULL means "nobody has said" — read as a guess
from ``systems.protocol`` (:func:`app.models.infer_trunked`, mirrored in SQL by
:func:`app.exporters.gate.trunked_system_clause`).  The derivation is written **once**,
here, over the rows that already exist: everything whose protocol names a trunked family
(P25/APCO, DMR, Motorola/SmartNet/SmartZone, EDACS, LTR, NXDN, TETRA, or the word "trunk")
and does not also say "conventional"/"analog" becomes ``true``, everything else ``false``.
After this, an existing system's answer is a stored fact an operator can edit; only systems
created later fall back to the guess.

``talkgroups.export_bcfy_calls`` splits the per-talkgroup checkbox that Broadcastify's two
products used to share.  ``talkgroups.export_broadcastify`` is **kept, and now belongs to
the live Feed alone** — the Feed keeps the column so an operator's existing selection goes
on meaning exactly what it meant — while the new column is the **Calls** flag and is seeded
from the old one, so the split changes nothing on the day it lands.  The one exception is
the rule above: a talkgroup on a system that is not trunked has its Calls flag forced
``false``, because Calls would refuse it anyway and a ticked box that cannot publish is a
lie the UI would have to keep explaining.

Neither column changes what is published today: the per-talkgroup flags are read only when
``export_<service>_talkgroup_mode`` is ``allow``/``deny``, and both Broadcastify switches
ship off.

Revision ID: b7c4d2e91a35
Revises: a4e91c2b7d60
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c4d2e91a35"
down_revision: str | None = "a4e91c2b7d60"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: Kept as literals rather than imported from :mod:`app.models`: a migration has to keep
#: describing the database it actually wrote, and a later edit to the hint list must not
#: silently change what this revision did.
_TRUNKED = "(p25|apco|phase 1|phase 2|dmr|mototrbo|motorola|smartnet|smartzone|edacs|ltr|nxdn|tetra|trunk)"
_CONVENTIONAL = "(conventional|analog|analogue|narrowband fm)"


def upgrade() -> None:
    op.add_column("systems", sa.Column("trunked", sa.Boolean(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE systems SET trunked = "
            "(protocol IS NOT NULL AND protocol ~* :trunked AND protocol !~* :conventional)"
        ).bindparams(trunked=_TRUNKED, conventional=_CONVENTIONAL)
    )
    op.add_column(
        "talkgroups",
        sa.Column("export_bcfy_calls", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.execute(sa.text("UPDATE talkgroups SET export_bcfy_calls = export_broadcastify"))
    op.execute(
        sa.text(
            "UPDATE talkgroups SET export_bcfy_calls = false WHERE system_id IN "
            "(SELECT id FROM systems WHERE trunked IS NOT TRUE)"
        )
    )


def downgrade() -> None:
    op.drop_column("talkgroups", "export_bcfy_calls")
    op.drop_column("systems", "trunked")
