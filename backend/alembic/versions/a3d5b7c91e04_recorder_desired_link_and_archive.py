"""``recorders`` desired state grows past the upload hold: the event-log archive and the link.

The hold (``d7c4e0b91a58``) proved the shape — a nullable desired column, a generation of
its own, and the recorder echoing back what it applied — and two more settings now want
exactly it.  Both are instance-level rather than per-tuner, so neither belongs on
``tuners.desired``:

* ``desired_event_log_archive`` — whether keyup wants the recorder to ship its daily raw
  event log here.  Three-valued for the same reason ``desired_spool_only`` is: NULL means
  "keyup has no opinion", and a recorder nobody has told anything must keep its own TOML
  value rather than be switched off by silence;
* ``desired_link`` — the whole recorder-to-recorder link block (``enabled``, ``bind``,
  ``peers``, the shared ``key``, ``hello_interval_s`` and the three grant flags) as one
  JSONB object, or NULL for "not managed here".  One column rather than eight because the
  block is applied and refused as a unit: half a link is not a link, and a peer list that
  disagrees with the key it was written beside would be the worst kind of drift.  The
  **key lives here in clear**, exactly as it must live in clear in the recorder's TOML —
  it is a shared HMAC secret keyup has to be able to hand back to the recorder, not a
  password to verify — and the API is what keeps it out of the list views.

Three more JSONB columns hold the recorder's side of the same conversation, mirroring how
a tuner's ``rejected``/``capabilities`` ride along in ``tuners.reported``.  A recorder has
no single reported blob, so they are columns beside ``uplink``/``link``/``event_log``
rather than keys inside one:

* ``recorder_applied`` — the whole ``recorder_applied`` block the recorder echoed, so an
  operator can see *what* it applied and not merely which version.  Its ``link`` half
  carries ``key_set``/``key_fingerprint`` rather than the key: trunkd does not send the
  secret back, and keyup would not store a second copy if it did;
* ``recorder_rejected`` — recorder-level ``{field: reason}``, the instance-level twin of
  the per-tuner map.  A refusal does not hold the recorder pending (it acknowledges the
  version either way), so this is the only trace one leaves;
* ``recorder_capabilities`` — ``capabilities.recorder``, chiefly ``restart_on``: which of
  these fields the recorder cannot change without restarting itself.

Nothing to backfill.  NULL is "unmanaged" on both desired columns, which is precisely the
behaviour every existing recorder already has, and the three reported columns are NULL
until a recorder new enough to fill them reports.

Revision ID: a3d5b7c91e04
Revises: bd873003af79
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3d5b7c91e04"
down_revision: str | None = "bd873003af79"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("recorders", sa.Column("desired_event_log_archive", sa.Boolean(), nullable=True))
    op.add_column("recorders", sa.Column("desired_link", postgresql.JSONB(), nullable=True))
    op.add_column("recorders", sa.Column("recorder_applied", postgresql.JSONB(), nullable=True))
    op.add_column("recorders", sa.Column("recorder_rejected", postgresql.JSONB(), nullable=True))
    op.add_column("recorders", sa.Column("recorder_capabilities", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("recorders", "recorder_capabilities")
    op.drop_column("recorders", "recorder_rejected")
    op.drop_column("recorders", "recorder_applied")
    op.drop_column("recorders", "desired_link")
    op.drop_column("recorders", "desired_event_log_archive")
