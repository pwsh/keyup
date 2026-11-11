"""``units``, ``personnel`` and ``radio_assignments``: structured identity (§3.1).

Until now a radio carried a flat ``alias`` plus a free-text ``unit_id``, which could say
*"E1"* but not what E1 is, could not hold two agencies' identical callsigns apart, and had
nowhere at all to put the person holding the handset.  These three tables are that
structure: a unit is the thing a callsign names, a person is a person, and an assignment
ties either of them to a radio for a stretch of time.

Nothing here changes ``radios``.  ``radios.unit_id`` stays exactly where it is and keeps
its meaning — it is the denormalized label the whole archive reads on every call row — and
becomes *maintained by* the current unit assignment rather than replaced by it.  That is
why this migration is additive and needs no backfill: an archive with no assignments yet
behaves precisely as it did yesterday.

Two partial unique indexes carry the only rule the application cannot enforce for itself:
at most one current unit assignment and one current person assignment per radio.

Revision ID: a4c8e1b7d260
Revises: e5f6a7b8c9d0
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4c8e1b7d260"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

UNIT_KINDS = ("vehicle", "station", "team", "other")


def upgrade() -> None:
    kind = postgresql.ENUM(*UNIT_KINDS, name="unit_kind", create_type=False)
    kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "units",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("callsign", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("agency_code", sa.Text(), nullable=True),
        sa.Column("kind", kind, server_default=sa.text("'other'"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agency_code"],
            ["agencies.code"],
            name=op.f("fk_units_agency_code_agencies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_units")),
        sa.UniqueConstraint("agency_code", "callsign", name="uq_units_agency_callsign"),
    )
    # Two NULL agency codes are not equal in SQL, so the unique constraint above lets an
    # agency-less "E1" be created twice.  This index is the other half of that key.
    op.create_index(
        "uq_units_callsign_no_agency",
        "units",
        ["callsign"],
        unique=True,
        postgresql_where=sa.text("agency_code IS NULL"),
    )
    op.create_index("ix_units_callsign", "units", ["callsign"])
    op.create_index("ix_units_agency_code", "units", ["agency_code"])

    op.create_table(
        "personnel",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("rank", sa.Text(), nullable=True),
        sa.Column("badge", sa.Text(), nullable=True),
        sa.Column("agency_code", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agency_code"],
            ["agencies.code"],
            name=op.f("fk_personnel_agency_code_agencies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_personnel")),
    )
    op.create_index("ix_personnel_name", "personnel", ["name"])
    op.create_index("ix_personnel_agency_code", "personnel", ["agency_code"])

    op.create_table(
        "radio_assignments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("radio_pk", sa.BigInteger(), nullable=False),
        sa.Column("unit_id", sa.Integer(), nullable=True),
        sa.Column("person_id", sa.Integer(), nullable=True),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(unit_id IS NOT NULL) <> (person_id IS NOT NULL)",
            name="ck_radio_assignments_unit_xor_person",
        ),
        sa.ForeignKeyConstraint(
            ["radio_pk"],
            ["radios.id"],
            name=op.f("fk_radio_assignments_radio_pk_radios"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["units.id"], name=op.f("fk_radio_assignments_unit_id_units"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["personnel.id"],
            name=op.f("fk_radio_assignments_person_id_personnel"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radio_assignments")),
    )
    # "At most one current assignment of each kind per radio", enforced where the race
    # actually happens rather than in the request handler.
    op.create_index(
        "uq_radio_assignments_current_unit",
        "radio_assignments",
        ["radio_pk"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL AND unit_id IS NOT NULL"),
    )
    op.create_index(
        "uq_radio_assignments_current_person",
        "radio_assignments",
        ["radio_pk"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL AND person_id IS NOT NULL"),
    )
    op.create_index("ix_radio_assignments_radio", "radio_assignments", ["radio_pk", "effective_from"])
    op.create_index("ix_radio_assignments_unit", "radio_assignments", ["unit_id"])
    op.create_index("ix_radio_assignments_person", "radio_assignments", ["person_id"])


def downgrade() -> None:
    op.drop_table("radio_assignments")
    op.drop_index("ix_personnel_agency_code", table_name="personnel")
    op.drop_index("ix_personnel_name", table_name="personnel")
    op.drop_table("personnel")
    op.drop_index("ix_units_agency_code", table_name="units")
    op.drop_index("ix_units_callsign", table_name="units")
    op.drop_index("uq_units_callsign_no_agency", table_name="units")
    op.drop_table("units")
    postgresql.ENUM(name="unit_kind").drop(op.get_bind(), checkfirst=True)
