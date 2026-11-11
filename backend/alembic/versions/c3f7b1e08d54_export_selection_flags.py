"""per-talkgroup and per-frequency export selection flags

Both columns default **true** on both tables, which is deliberately a no-op: they are only
consulted when ``export_<service>_talkgroup_mode`` is ``allow`` or ``deny``, and the shipped
mode is ``all`` (docs/EXPORTERS.md).  Adding them changes nothing about what is uploaded —
and nothing is uploaded at all while both enable switches are off, which is how they ship.

Revision ID: c3f7b1e08d54
Revises: 90e283a5863d
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f7b1e08d54"
down_revision: str | None = "90e283a5863d"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLES = ("talkgroups", "site_frequencies")
_COLUMNS = ("export_broadcastify", "export_openmhz")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.add_column(
                table,
                sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.text("true")),
            )


def downgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.drop_column(table, column)
