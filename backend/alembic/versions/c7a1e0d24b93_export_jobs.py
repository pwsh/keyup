"""export_jobs

The API's asynchronous bundle export (docs/ARCHITECTURE.md 4.1): one row per
``POST /api/exports``, holding the filter that was asked for, the status of the
background thread that zips the audio, and where the finished file landed.

``job_status`` already exists (transcription_jobs uses it), so the enum is referenced
with ``create_type=False`` rather than created a second time.

Revision ID: c7a1e0d24b93
Revises: b1c2d3e4f5a6
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a1e0d24b93"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

job_status = postgresql.ENUM(
    "queued", "running", "done", "failed", "cancelled", name="job_status", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "export_jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", job_status, server_default=sa.text("'queued'"), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_jobs")),
    )
    op.create_index("ix_export_jobs_created_at", "export_jobs", [sa.text("created_at DESC")], unique=False)


def downgrade() -> None:
    op.drop_index("ix_export_jobs_created_at", table_name="export_jobs")
    op.drop_table("export_jobs")
