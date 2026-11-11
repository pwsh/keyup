"""Site channel audit: recorder observations, site_frequencies, adjacent_sites (§3.5).

docs/CHANNEL_AUDIT.md.  Three tables and the columns that let them be joined to what the
directory already knows:

* ``recorder_observations`` — the raw ``POST /api/recorder/observations`` document, so the
  normalized rows can be rebuilt without asking the recorder to see the site again;
* ``site_frequencies`` — one row per (site, frequency, source), where *source* is the air,
  RadioReference or the recorder's own configuration.  Keeping the three apart is the
  whole point: the audit is the difference between them;
* ``adjacent_sites`` — the neighbors a control channel announces.

Plus ``sites.nac/rfss/lra/rr_site_id/rr_synced_at``, ``systems.nac/rr_system_id/
rr_synced_at``, and a fifth ``alert_kind`` value, ``channel_audit``.

The enum value is added in an ``autocommit_block`` for the usual reason (PostgreSQL cannot
use a value added by the transaction that is still adding it).  ``downgrade`` drops the
tables and the columns but leaves the enum value in place — PostgreSQL has no DROP VALUE.

Revision ID: f3b7c1a94d20
Revises: e1f2a3b4c5d6
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3b7c1a94d20"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# ``create_type=False``: the two types are created (and dropped) explicitly below, so
# ``create_table`` does not try to create them a second time.
ROLE = postgresql.ENUM(
    "primary_control",
    "alternate_control",
    "voice",
    "unknown",
    name="site_frequency_role",
    create_type=False,
)
SOURCE = postgresql.ENUM(
    "observed", "radioreference", "configured", name="site_frequency_source", create_type=False
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_kind ADD VALUE IF NOT EXISTS 'channel_audit'")

    bind = op.get_bind()
    ROLE.create(bind, checkfirst=True)
    SOURCE.create(bind, checkfirst=True)

    op.add_column("systems", sa.Column("nac", sa.Integer(), nullable=True))
    op.add_column("systems", sa.Column("rr_system_id", sa.Integer(), nullable=True))
    op.add_column("systems", sa.Column("rr_synced_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("sites", sa.Column("nac", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("rfss", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("lra", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("rr_site_id", sa.Integer(), nullable=True))
    op.add_column("sites", sa.Column("rr_synced_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "recorder_observations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("system_id", sa.Integer(), nullable=True),
        sa.Column("site_id", sa.Integer(), nullable=True),
        sa.Column("recorder", sa.Text(), nullable=False),
        sa.Column("recorder_version", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_s", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["system_id"],
            ["systems.id"],
            name=op.f("fk_recorder_observations_system_id_systems"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_recorder_observations_site_id_sites"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recorder_observations")),
    )
    op.create_index(
        "ix_recorder_observations_site_observed",
        "recorder_observations",
        ["site_id", sa.text("observed_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_recorder_observations_observed_at",
        "recorder_observations",
        [sa.text("observed_at DESC")],
        unique=False,
    )

    op.create_table(
        "site_frequencies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("frequency_hz", sa.BigInteger(), nullable=False),
        sa.Column("role", ROLE, nullable=False),
        sa.Column("source", SOURCE, nullable=False),
        sa.Column("channel_ref", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("grant_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_site_frequencies_site_id_sites"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_frequencies")),
        sa.UniqueConstraint(
            "site_id", "frequency_hz", "source", name="uq_site_frequencies_site_freq_source"
        ),
    )
    op.create_index("ix_site_frequencies_site_source", "site_frequencies", ["site_id", "source"])

    op.create_table(
        "adjacent_sites",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("system_id_p25", sa.Integer(), nullable=False),
        sa.Column("rfss", sa.Integer(), nullable=False),
        sa.Column("site_number", sa.Integer(), nullable=False),
        sa.Column("lra", sa.Integer(), nullable=True),
        sa.Column("control_frequency_hz", sa.BigInteger(), nullable=True),
        sa.Column("channel_ref", sa.Text(), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_adjacent_sites_site_id_sites"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adjacent_sites")),
        sa.UniqueConstraint(
            "site_id", "system_id_p25", "rfss", "site_number", name="uq_adjacent_sites_identity"
        ),
    )
    op.create_index("ix_adjacent_sites_site", "adjacent_sites", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_adjacent_sites_site", table_name="adjacent_sites")
    op.drop_table("adjacent_sites")
    op.drop_index("ix_site_frequencies_site_source", table_name="site_frequencies")
    op.drop_table("site_frequencies")
    op.drop_index("ix_recorder_observations_observed_at", table_name="recorder_observations")
    op.drop_index("ix_recorder_observations_site_observed", table_name="recorder_observations")
    op.drop_table("recorder_observations")

    for column in ("rr_synced_at", "rr_site_id", "lra", "rfss", "nac"):
        op.drop_column("sites", column)
    for column in ("rr_synced_at", "rr_system_id", "nac"):
        op.drop_column("systems", column)

    bind = op.get_bind()
    SOURCE.drop(bind, checkfirst=True)
    ROLE.drop(bind, checkfirst=True)
    # alert_kind keeps its 'channel_audit' value: PostgreSQL cannot drop an enum label, and
    # rebuilding the type would mean rewriting every alert_rules row.
