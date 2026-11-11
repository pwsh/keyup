"""radio events: presence from the control channel (docs/RADIO_EVENTS.md, §3.6)

What the recorder decodes off the control channel and used to throw away: unit
registration, de-registration, group affiliation, location registration, SNDCP data
grants and pages, and the acknowledge/deny/queued responses to all of them.  Until now
``radio_affiliations`` meant "was heard talking"; with these rows it can also mean "was
present", and the two are kept apart rather than silently merged.

Four things:

``radio_events``
    one row per decoded message, ``kind`` + JSONB ``meta`` rather than a table per opcode
    family (~600 events/hour on a busy site, and the rare commands are too rare to model).
    ``uq_radio_events_identity`` is the idempotency key: the recorder spools batches and
    retries them, so the same event arrives more than once and the retry must be free.
    The nullable parts are COALESCE'd into the index because NULLs do not collide.
``radios.last_registered_at`` / ``last_deregistered_at``
    presence, as opposed to ``last_seen_at`` which is now "heard talking *or* seen on the
    control channel".
``radio_affiliations.source`` / ``last_affiliated_at``
    ``source`` defaults to ``call``, which is what every existing row is — they were all
    written by ingest from a call.  ``affiliation`` is set by a ``group_affiliation``
    event and never downgraded.
``radio_event_kind`` / ``radio_affiliation_source``
    native enums, so a later opcode family is ``ALTER TYPE … ADD VALUE`` and not a check
    constraint rewrite.

Retention is not schema: ``RADIO_EVENTS_KEEP_DAYS`` (90) is applied by the nightly pass.

Revision ID: c9f1a2b30d44
Revises: b8d4e2f70c15
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9f1a2b30d44"
down_revision: str | None = "b8d4e2f70c15"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

RADIO_EVENT_KINDS = (
    "unit_registration",
    "unit_deregistration",
    "unit_registration_command",
    "group_affiliation",
    "group_affiliation_query",
    "location_registration",
    "sndcp_grant",
    "sndcp_page",
    "ack",
    "deny",
    "queued",
    "other",
)

# ``create_type=False``: both types are created explicitly below, so ``create_table`` and
# ``add_column`` do not try to create them a second time.
KIND = postgresql.ENUM(*RADIO_EVENT_KINDS, name="radio_event_kind", create_type=False)
AFFILIATION_SOURCE = postgresql.ENUM(
    "call", "affiliation", name="radio_affiliation_source", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    KIND.create(bind, checkfirst=True)
    AFFILIATION_SOURCE.create(bind, checkfirst=True)

    op.create_table(
        "radio_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("system_id", sa.Integer(), nullable=True),
        sa.Column("site_id", sa.Integer(), nullable=True),
        sa.Column("kind", KIND, nullable=False),
        sa.Column("opcode", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("mfid", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("radio_pk", sa.BigInteger(), nullable=True),
        sa.Column("radio_id", sa.BigInteger(), nullable=True),
        sa.Column("radio_wacn", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("radio_system", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("target_radio_id", sa.BigInteger(), nullable=True),
        sa.Column("talkgroup_tgid", sa.Integer(), nullable=True),
        sa.Column("talkgroup_pk", sa.Integer(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("frequency_hz", sa.BigInteger(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["system_id"], ["systems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["radio_pk"], ["radios.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["talkgroup_pk"], ["talkgroups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_radio_events_identity",
        "radio_events",
        [
            sa.text("COALESCE(site_id, 0)"),
            "at",
            "kind",
            "opcode",
            sa.text("COALESCE(radio_id, -1)"),
            sa.text("COALESCE(talkgroup_tgid, -1)"),
        ],
        unique=True,
    )
    op.create_index("ix_radio_events_at", "radio_events", [sa.text("at DESC")])
    op.create_index("ix_radio_events_radio_at", "radio_events", ["radio_pk", sa.text("at DESC")])
    op.create_index("ix_radio_events_talkgroup_at", "radio_events", ["talkgroup_pk", sa.text("at DESC")])
    op.create_index("ix_radio_events_site_kind_at", "radio_events", ["site_id", "kind", sa.text("at DESC")])

    op.add_column("radios", sa.Column("last_registered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("radios", sa.Column("last_deregistered_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column(
        "radio_affiliations",
        sa.Column("source", AFFILIATION_SOURCE, server_default=sa.text("'call'"), nullable=False),
    )
    op.add_column(
        "radio_affiliations", sa.Column("last_affiliated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("radio_affiliations", "last_affiliated_at")
    op.drop_column("radio_affiliations", "source")
    op.drop_column("radios", "last_deregistered_at")
    op.drop_column("radios", "last_registered_at")
    op.drop_index("ix_radio_events_site_kind_at", table_name="radio_events")
    op.drop_index("ix_radio_events_talkgroup_at", table_name="radio_events")
    op.drop_index("ix_radio_events_radio_at", table_name="radio_events")
    op.drop_index("ix_radio_events_at", table_name="radio_events")
    op.drop_index("uq_radio_events_identity", table_name="radio_events")
    op.drop_table("radio_events")
    bind = op.get_bind()
    AFFILIATION_SOURCE.drop(bind, checkfirst=True)
    KIND.drop(bind, checkfirst=True)
