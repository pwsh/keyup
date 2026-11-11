"""Where the STT backends' counters live so the API can read them.

The per-backend counters an operator watches — attempts, successes, errors, real-time
factor, last error, in-flight — are held in memory by the router inside the ``keyup
worker`` process.  The API process has its own memory and no router, so the admin
"backends" page was building a throwaway router and reporting *its* counters: structurally
zero, forever, no matter how much work the worker was doing.

``stt_backend_stats`` is the crossing: one row per configured backend, upserted by the
worker every few seconds and immediately when a backend's health flips, read by the API.
Rows are never deleted for age — a row that stopped being updated is exactly the "worker
not reporting" state the page has to be able to show.

The same row carries the operator's runtime enable/disable override (``admin_enabled``,
NULL = follow ``STT_BACKENDS``), which travels the other way: written by the API, read by
the worker's router at selection time.  Keeping it here rather than in its own table means
the page reads one row per backend and the override survives the worker's upserts, which
never assign it.

Revision ID: e7b3d9a24c18
Revises: d3a9f5c81b70
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b3d9a24c18"
down_revision: str | None = "d3a9f5c81b70"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stt_backend_stats",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text()),
        sa.Column("base_url", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("admin_enabled", sa.Boolean()),
        sa.Column("admin_changed_at", sa.DateTime(timezone=True)),
        sa.Column("admin_changed_by", sa.Text()),
        sa.Column("healthy", sa.Boolean()),
        sa.Column("health_detail", sa.Text()),
        sa.Column("disqualified", sa.Text()),
        sa.Column("in_flight", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("successes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("audio_s", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("processing_s", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("mean_rtf", sa.Float()),
        sa.Column("mean_processing_s", sa.Float()),
        sa.Column("recent_completed_1m", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("recent_failed_1m", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("worker_instance", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("name", name="pk_stt_backend_stats"),
    )


def downgrade() -> None:
    op.drop_table("stt_backend_stats")
