"""``webhook_deliveries``: the outbound event queue for consumers like a CAD integration.

One row per event a subscribed consumer is owed, written in the same transaction as the
work that caused it and drained by a worker job.  Nothing here turns webhooks on — the
whole feature ships off (``webhook_url`` blank, ``webhook_events`` empty, both in the
``settings`` table) and this table stays empty until an operator fills those in.

No unique constraint and no foreign key, both deliberately.  Unlike ``call_exports``, two
identical events genuinely can occur — a call re-transcribed after a correction — and a
delivery is meant to outlive the row it describes, so that "this call was stored" stays
answerable after retention has deleted the call.

Revision ID: a2e7b4c60d19
Revises: f8c2a4e19d53
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2e7b4c60d19"
down_revision: str | None = "f8c2a4e19d53"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

STATUSES = ("pending", "delivered", "failed")


def upgrade() -> None:
    status = postgresql.ENUM(*STATUSES, name="webhook_delivery_status", create_type=False)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", status, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index("ix_webhook_deliveries_claim", "webhook_deliveries", ["status", "next_at"])
    op.create_index(
        "ix_webhook_deliveries_created", "webhook_deliveries", [sa.text("created_at DESC")]
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_claim", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    postgresql.ENUM(name="webhook_delivery_status").drop(op.get_bind(), checkfirst=True)
