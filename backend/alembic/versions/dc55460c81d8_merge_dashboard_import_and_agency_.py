"""merge dashboard import and agency source heads

Revision ID: dc55460c81d8
Revises: c9e3f1a7b420, d2f6b8c31a47
Create Date: 2026-09-04 12:45:34.907938-05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'dc55460c81d8'
down_revision: str | None = ('c9e3f1a7b420', 'd2f6b8c31a47')
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
