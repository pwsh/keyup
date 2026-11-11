"""Recorder standby policy, and which receiver a call's audio came from.

Two features, one revision, because they are two halves of the same question: *several
recorders now hear the same P25 site, so which one is supposed to be recording it, and
whose copy of a transmission do we keep?*

**Recorder policy** — ``recorders.priority``, ``recorders.standby`` and
``recorders.promoted_at``.  ``standby`` is the operator's declaration of what a box is for:
``hot`` records whenever it is healthy, ``warm`` sits registered with its tuners disabled
until the arbiter (:mod:`app.arbiter`) promotes it because the hot side went quiet, and
``cold`` is never touched automatically — a bench radio must not be keyed up by a
background job.  ``priority`` orders the warm spares (lower is preferred).  ``promoted_at``
records that the *arbiter* was the one that enabled a recorder, and it is a column rather
than a fact held in the job's memory on purpose: an API or worker restart must not orphan a
promotion, and an operator's own manual enable (``promoted_at`` NULL) must never be
auto-demoted afterwards.

**Call source** — ``calls.source_instance`` and ``calls.source_quality``.  A recorder may
post a ``sourceQuality`` blob with an upload ({"instance", "voice_frames", "bad_frames",
"rssi_dbfs"}); storing it is what lets the dedupe path compare a second receiver's copy
with the one already in the archive and keep the better audio, and what keeps "why is this
rec-02's recording?" answerable afterwards.

Purely additive.  Every existing recorder becomes ``hot`` at priority 100 — which is what
they already were in effect — and every existing call keeps NULL for both new columns,
which reads correctly as "nobody said".

Revision ID: d3a9f5c81b70
Revises: c1d5b8a37e42
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3a9f5c81b70"
down_revision: str | None = "c1d5b8a37e42"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

RECORDER_STANDBY = sa.Enum("hot", "warm", "cold", name="recorder_standby")


def upgrade() -> None:
    RECORDER_STANDBY.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "recorders",
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
    )
    op.add_column(
        "recorders",
        sa.Column("standby", RECORDER_STANDBY, nullable=False, server_default=sa.text("'hot'")),
    )
    op.add_column("recorders", sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("calls", sa.Column("source_instance", sa.Text(), nullable=True))
    op.add_column("calls", sa.Column("source_quality", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "source_quality")
    op.drop_column("calls", "source_instance")
    op.drop_column("recorders", "promoted_at")
    op.drop_column("recorders", "standby")
    op.drop_column("recorders", "priority")
    RECORDER_STANDBY.drop(op.get_bind(), checkfirst=True)
