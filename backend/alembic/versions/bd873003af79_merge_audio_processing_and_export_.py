"""merge audio processing and export selection

Revision ID: bd873003af79
Revises: a4c9e1f27b60, c3f7b1e08d54
Create Date: 2026-09-03 01:51:16.768173-05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'bd873003af79'
down_revision: str | None = ('a4c9e1f27b60', 'c3f7b1e08d54')
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
