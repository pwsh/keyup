"""calls.ingest_priority — the admission class a pushed call arrived under

docs/ARCHITECTURE.md §4.1, §5.1, docs/RECORDERS.md "Priority and flow control".

``POST /api/call-upload`` accepts a ``priority`` class (``urgent``/``live``/``backlog``/
``bulk``, or a bare integer) and turns it into the ``transcription_jobs.priority`` the call
is enqueued at.  That job is transient — it is claimed, finished and eventually vacuumed —
so the class it was chosen from has to live on the call if "which rows came from the
archive migration and which from the live recorder" is to stay answerable in SQL a year
later.  Hence a column on ``calls`` rather than a lookup through the job.

Text, not an enum: an integer priority is a legal value of the field, and a new class must
not need a migration to name.  NULL means "arrived without an explicit priority" — every
file-watched call, and every recorder that has not opted into the flow-control contract.

The index is partial on the non-NULL values, because the interesting query is "show me the
migrated calls" and the overwhelming majority of rows are NULL.

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f3b7c1a94d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("ingest_priority", sa.Text(), nullable=True))
    op.create_index(
        "ix_calls_ingest_priority",
        "calls",
        ["ingest_priority"],
        postgresql_where=sa.text("ingest_priority IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_calls_ingest_priority", table_name="calls")
    op.drop_column("calls", "ingest_priority")
