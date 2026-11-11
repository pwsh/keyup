"""stt_engine enum: add 'openvino_genai' and 'ovms' (OpenVINO in-process and Model Server backends).

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-08-31

Added inside an ``autocommit_block`` because PostgreSQL cannot use an enum value inside the
transaction that added it.  Enum values cannot be dropped, so ``downgrade`` is a no-op.
"""

from __future__ import annotations

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE stt_engine ADD VALUE IF NOT EXISTS 'openvino_genai'")
        op.execute("ALTER TYPE stt_engine ADD VALUE IF NOT EXISTS 'ovms'")


def downgrade() -> None:
    # PostgreSQL has no DROP VALUE; leaving the extra label in place is harmless.
    pass
