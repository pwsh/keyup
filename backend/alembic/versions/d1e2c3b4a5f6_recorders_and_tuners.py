"""``recorders`` and ``tuners``: multi-tuner management and the desired/reported contract.

A recorder (trunkd instance) appears here the first time it polls for its desired state or
posts a tuner status — like a site appearing from its first observation.  Each of its
tuners carries what the recorder last reported (``reported``) and what an operator wants of
it (``desired``), both JSONB so the field set can evolve without a migration.

Revision ID: d1e2c3b4a5f6
Revises: a7c3f10e9b24
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d1e2c3b4a5f6"
down_revision: str | None = "a7c3f10e9b24"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recorders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorder_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["token_id"], ["api_tokens.id"], name=op.f("fk_recorders_token_id_api_tokens"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recorders")),
        sa.UniqueConstraint("instance_id", name=op.f("uq_recorders_instance_id")),
    )
    # No separate index on instance_id: the unique constraint already provides one.

    op.create_table(
        "tuners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recorder_id", sa.Integer(), nullable=False),
        sa.Column("tuner_key", sa.Text(), nullable=False),
        sa.Column("reported", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("desired", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("desired_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["recorder_id"], ["recorders.id"], name=op.f("fk_tuners_recorder_id_recorders"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tuners")),
        sa.UniqueConstraint("recorder_id", "tuner_key", name="uq_tuners_recorder_key"),
    )
    op.create_index("ix_tuners_recorder_id", "tuners", ["recorder_id"])


def downgrade() -> None:
    op.drop_index("ix_tuners_recorder_id", table_name="tuners")
    op.drop_table("tuners")
    op.drop_table("recorders")
