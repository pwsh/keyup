"""``health_conditions`` and two alert kinds, ``health`` and ``emergency``.

The operator safety net: the worker checks every minute for a
hot recorder gone silent, no new calls, transcription stalled and the database backup
overdue (:mod:`app.health`), and this table is those alerts' memory — one row per subject,
``active`` while it is wrong — so each is raised once and cleared once, across restarts.

``health`` is the alert kind those checks deliver on.  ``emergency`` fires on ``call.new``
for a call whose P25 emergency flag is set, optionally narrowed to talkgroups or
categories (:func:`app.alerts.emergency_scope`).  Both values are added in an
``autocommit_block`` (PostgreSQL cannot use an enum value inside the transaction that
added it), and stay on downgrade (there is no ``DROP VALUE``); the rules that use them are
deleted instead, since the older code has no matcher for them.

Revision ID: d1a7e5c30b48
Revises: ec34a3a39b74
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a7e5c30b48"
down_revision: str | None = "ec34a3a39b74"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_conditions",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("check", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("good_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
    )
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'health'")
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'emergency'")


def downgrade() -> None:
    op.execute("DELETE FROM alert_rules WHERE kind IN ('health', 'emergency')")
    op.drop_table("health_conditions")
