"""``call_exports.response`` / ``response_code``: what the service actually answered

docs/EXPORTERS.md ("Responses").

The ledger recorded the *outcome* of an attempt — uploaded, skipped, failed — and threw
the service's own words away.  For Broadcastify Calls that loses the only number an
operator wants when the queue looks busy but nothing is arriving: how many of those
skipped rows were ``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL``, the duplicate answer, as
opposed to a call we declined to send.  On this deployment that is 3 485 rows against 63
uploads, and none of it was visible without reading ``skip_reason`` prose.

So two nullable columns, written on **every** attempt:

``response_code``
    the integer that leads the stage-1 line — ``0`` success, ``1`` skipped or rejected,
    ``100`` no API key.  Nullable because a failure that never reached stage 1 (a timeout,
    a 5xx, an ffmpeg error) has no code, and because OpenMHz has no such line at all.
``response``
    the raw line, capped at 500 characters and redacted of any API key first.  A success
    line is stored as ``0 <last path segment>``: the rest of the upload URL is a presigned
    signature with a lifetime of minutes and has no business in a ledger row.

Both are nullable with no backfill.  The 5 000-odd existing rows genuinely have no
recorded response and inventing one from ``skip_reason`` would make history look like it
was observed; the status endpoint counts duplicates from ``skip_reason`` as well as from
these columns for exactly that reason.

Revision ID: c1d5e9a37f24
Revises: b6e4c81f27a3
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d5e9a37f24"
down_revision: str | None = "b6e4c81f27a3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("call_exports", sa.Column("response_code", sa.Integer(), nullable=True))
    op.add_column("call_exports", sa.Column("response", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("call_exports", "response")
    op.drop_column("call_exports", "response_code")
