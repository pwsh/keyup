"""``recorders.monitor``: where the recorder serves its own live audio monitor.

A recorder that can be listened to has a panel of its own — a page it serves beside the
recording, streaming what its tuners are hearing right now.  Keyup does not proxy that
audio and never will: the stream is the recorder's business, and the only thing the status
page needs in order to send an operator to it is *where it is*.  So the recorder reports a
``monitor`` block on its tuner-status push and this column keeps the last one it sent —
``{enabled, url, stream_url, token_required, streams, max_streams}``, stored verbatim as
JSONB exactly as ``link`` and ``event_log`` are, because the field set belongs to trunkd.

Nothing to backfill.  NULL is the honest value for every recorder that exists when this
runs: none of them has reported a monitor, and a recorder on an older binary never will —
its rows keep a NULL here and the panel simply shows no link, which is the behaviour the
page has today.

Revision ID: e5b1d93c27af
Revises: c4e91f3b57a8
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5b1d93c27af"
down_revision: str | None = "c4e91f3b57a8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recorders", sa.Column("monitor", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("recorders", "monitor")
