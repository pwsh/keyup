"""Unit-to-unit (private) calls: ``calls.kind``, ``calls.target_radio_id``, ``private_call``

docs/RECORDERS.md ("Unit-to-unit calls"), docs/RADIO_EVENTS.md, docs/EXPORTERS.md.

A P25 private call is one radio talking to one radio.  It is granted a traffic channel and
it is recordable, but it is **not on a talkgroup at all** — there is nothing for
``calls.talkgroup_id`` to point at and nothing for ``calls.talkgroup_ids`` to hold.  Until
now the archive had no way to say that: a call with no talkgroup meant *conventional*
traffic keyed by its frequency, which is a different fact with different handling — the
exporters publish conventional slots by frequency and must never publish these.

So ``calls.kind`` — ``'group'`` or ``'unit'`` — says who the transmission was addressed to,
and ``calls.target_radio_id`` names the other end.  Both are plain columns rather than new
``call_type`` enum values: ``call_type`` describes how the channel was granted (group,
patch, individual) and every consumer keying on a talkgroup needs to branch on *this*
question, cheaply, in SQL.

``kind`` is ``NOT NULL DEFAULT 'group'`` with a CHECK, so every existing row keeps meaning
exactly what it meant and nothing is backfilled.  ``target_radio_id`` is a nullable FK onto
``radios`` with its own time-ordered index, because "what did this radio take part in" has
to read both ends of a private call.

The ``radio_event_kind`` enum gains ``private_call``: the control-channel grant is an
observation in its own right, and it arrives whether or not a recording follows.  Added in
an ``autocommit_block`` because PostgreSQL cannot use an enum value added by the
transaction it is still in; ``IF NOT EXISTS`` makes a half-applied upgrade re-runnable.

Downgrade drops the two columns.  It cannot remove the enum value (PostgreSQL has no
``ALTER TYPE … DROP VALUE``), so it rewrites those rows to ``other`` — which keeps the
opcode and the decoded fields in ``meta``, which is what ``other`` is for.

Revision ID: b6e4c81f27a3
Revises: a1c7d90f4e62
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e4c81f27a3"
down_revision: str | None = "a1c7d90f4e62"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calls",
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'group'")),
    )
    op.create_check_constraint("ck_calls_kind", "calls", "kind IN ('group', 'unit')")
    op.add_column("calls", sa.Column("target_radio_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_calls_target_radio_id_radios",
        "calls",
        "radios",
        ["target_radio_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        "CREATE INDEX ix_calls_target_radio_started ON calls (target_radio_id, started_at DESC)"
    )
    # A unit call is rare next to group traffic, so the exporters' "not a unit call" and the
    # calls page's ``kind=unit`` both want a partial index rather than one over every row.
    op.execute("CREATE INDEX ix_calls_kind_unit ON calls (started_at DESC) WHERE kind = 'unit'")

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE radio_event_kind ADD VALUE IF NOT EXISTS 'private_call'")


def downgrade() -> None:
    op.execute("UPDATE radio_events SET kind = 'other' WHERE kind = 'private_call'")
    op.execute("DROP INDEX IF EXISTS ix_calls_kind_unit")
    op.execute("DROP INDEX IF EXISTS ix_calls_target_radio_started")
    op.drop_constraint("fk_calls_target_radio_id_radios", "calls", type_="foreignkey")
    op.drop_column("calls", "target_radio_id")
    op.drop_constraint("ck_calls_kind", "calls", type_="check")
    op.drop_column("calls", "kind")
