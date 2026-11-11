"""``site_message_kinds``: what the control channel has ever said, and when it changed.

docs/UNKNOWN-DATA-PLAN.md phase 5, the ``new_unknown`` alert.  The message census
(``GET /api/sites/{id}/message-mix``) is a rolling histogram folded out of the stored
observation payloads; it can say what a site is transmitting *now* but not whether it has
ever transmitted it before.  This table answers that: one row per ``(site, mfid, opcode)``,
carrying the running count, the first and last sighting, and — for a kind the recorder
cannot decode — the newest hex exemplar with the one it replaced.

Two more ``alert_kind`` values come with it: ``new_kind`` and ``exemplar_changed``, raised
by :mod:`app.rr.message_kinds` at observation ingest, not by a call event.  They are added
in an ``autocommit_block`` for the usual reason (PostgreSQL cannot use an enum value added
by the transaction still adding it), and ``downgrade`` leaves them in place because
PostgreSQL has no DROP VALUE.

The table starts empty and is filled by ``keyup sites seed-message-kinds``, which replays
the stored observation windows without raising anything.  A deployment that never runs it
is still safe: the first window a site posts into an empty table seeds that site silently.

Revision ID: a4d9e13c7b62
Revises: b6e1c30d94af
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4d9e13c7b62"
down_revision: str | None = "b6e1c30d94af"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'new_kind'")
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'exemplar_changed'")

    op.create_table(
        "site_message_kinds",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("mfid", sa.Integer(), nullable=False),
        sa.Column("opcode", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("exemplar", sa.Text(), nullable=True),
        sa.Column("exemplar_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exemplar_previous", sa.Text(), nullable=True),
        sa.Column("known", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "mfid", "opcode", name="uq_site_message_kinds_identity"),
    )
    op.create_index(
        "ix_site_message_kinds_site_first_seen",
        "site_message_kinds",
        ["site_id", sa.text("first_seen_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_site_message_kinds_site_first_seen", table_name="site_message_kinds")
    op.drop_table("site_message_kinds")
    # alert_kind keeps 'new_kind' and 'exemplar_changed': PostgreSQL cannot drop an enum
    # label, and a rule carrying one is harmless once nothing raises it.
