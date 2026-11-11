"""``call_exports``: the outbound upload ledger for Broadcastify Calls and OpenMHz.

One row per (call, service).  The ``UNIQUE (call_id, service)`` is the deduplication
guarantee the exporter worker leans on: a restart, a second worker or a repeated discovery
pass all race on the same insert and only one of them can win, so a recording is never
uploaded to the same service twice.  Nothing here enables an exporter — both services ship
default-disabled (``export_broadcastify_enabled`` / ``export_openmhz_enabled`` in the
``settings`` table) and this table stays empty until one is turned on.

Revision ID: e5f6a7b8c9d0
Revises: d1e2c3b4a5f6
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d1e2c3b4a5f6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

STATUSES = ("pending", "uploaded", "skipped", "failed")


def upgrade() -> None:
    status = postgresql.ENUM(*STATUSES, name="call_export_status", create_type=False)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "call_exports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("status", status, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("remote_id", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["call_id"], ["calls.id"], name=op.f("fk_call_exports_call_id_calls"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_exports")),
        sa.UniqueConstraint("call_id", "service", name="uq_call_exports_call_service"),
    )
    op.create_index("ix_call_exports_claim", "call_exports", ["service", "status", "next_attempt_at"])
    op.create_index("ix_call_exports_status", "call_exports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_call_exports_status", table_name="call_exports")
    op.drop_index("ix_call_exports_claim", table_name="call_exports")
    op.drop_table("call_exports")
    postgresql.ENUM(name="call_export_status").drop(op.get_bind(), checkfirst=True)
