"""merge tuner edits and shared talkgroups

Revision ID: 90e283a5863d
Revises: e2b9d47a1c60, e6a2f80c4b19
Create Date: 2026-09-03 00:01:29.148295-05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '90e283a5863d'
down_revision: str | None = ('e2b9d47a1c60', 'e6a2f80c4b19')
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
