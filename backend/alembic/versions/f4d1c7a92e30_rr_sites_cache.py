"""``rr_sites``: the whole RadioReference site list, kept so neighbors can be named.

``app.rr.sync`` already fetches every site of a system in one ``getTrsSites`` call, but the
loop kept only the one that maps to a Keyup site and discarded the rest.  This table retains
all of them — a reference cache, not "a site the recorder heard" — so the channel audit can
resolve the neighbors a control channel announces (``adjacent_sites``) against a name
without a second RR call.

Two partial unique indexes give the upsert a stable target: ``(system_id, rr_site_id)`` when
RR carries a site id, and ``(system_id, rfss, site_number)`` when it does not.

Revision ID: f4d1c7a92e30
Revises: c9f1a2b30d44
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4d1c7a92e30"
down_revision: str | None = "c9f1a2b30d44"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rr_sites",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("system_id", sa.Integer(), nullable=False),
        sa.Column("rr_site_id", sa.Integer(), nullable=True),
        sa.Column("rr_system_id", sa.Integer(), nullable=True),
        sa.Column("site_number", sa.Integer(), nullable=True),
        sa.Column("rfss", sa.Integer(), nullable=True),
        sa.Column("nac", sa.Integer(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("county", sa.Text(), nullable=True),
        sa.Column("control_frequency_hz", sa.BigInteger(), nullable=True),
        sa.Column("frequencies", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["system_id"], ["systems.id"], name=op.f("fk_rr_sites_system_id_systems"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rr_sites")),
    )
    # RR's own site id is the identity when it carries one; the (rfss, site_number) pair is
    # the fallback for a record that does not.  Two partial indexes rather than one nullable
    # constraint so each upsert has a single, unambiguous conflict target.
    op.create_index(
        "uq_rr_sites_system_rr_site",
        "rr_sites",
        ["system_id", "rr_site_id"],
        unique=True,
        postgresql_where=sa.text("rr_site_id IS NOT NULL"),
    )
    op.create_index(
        "uq_rr_sites_system_rfss_number",
        "rr_sites",
        ["system_id", "rfss", "site_number"],
        unique=True,
        postgresql_where=sa.text("rr_site_id IS NULL"),
    )
    op.create_index("ix_rr_sites_system_id", "rr_sites", ["system_id"])


def downgrade() -> None:
    op.drop_index("ix_rr_sites_system_id", table_name="rr_sites")
    op.drop_index("uq_rr_sites_system_rfss_number", table_name="rr_sites")
    op.drop_index("uq_rr_sites_system_rr_site", table_name="rr_sites")
    op.drop_table("rr_sites")
