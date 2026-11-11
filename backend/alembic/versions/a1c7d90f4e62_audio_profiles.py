"""Audio profiles: per-talkgroup filter chains (§5.5, docs/AUDIO.md).

One table and one column.  ``audio_profiles`` holds a named, ordered list of filter steps
as JSONB — never a rendered ffmpeg string, so nothing an operator typed is stored in the
form it would be executed in — and ``talkgroups.audio_profile_id`` points a channel at one.

The three shipped profiles are seeded here rather than by application code so that a fresh
database and a migrated one agree: "Default" is the global chain written as data, "Aircraft
cabin" is the 1.7–2.2 kHz cut that makes medevac helicopter transmissions
intelligible, and "Speech band-pass" is the telephone band.  They are inserted with
``ON CONFLICT (name) DO NOTHING`` so a deployment that already made a profile called
"Default" by hand keeps it.

``audio_profile_id`` is ``ON DELETE SET NULL``: the API refuses to delete a profile that is
in use, so a row losing its profile can only come from direct SQL, and falling back to the
global chain is the right behavior for that.

Revision ID: a1c7d90f4e62
Revises: dc55460c81d8
Create Date: 2026-09-04
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c7d90f4e62"
down_revision: str | None = "dc55460c81d8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: Kept as a literal rather than imported from ``app.audioproc.profiles``: a migration has
#: to keep doing what it did the day it ran, and the module's builtins will move on.
BUILTINS = (
    (
        "Default",
        "The shipped chain, as data: FFT denoise at a -25 dB noise floor, then the "
        "-50 dBFS gate. What a talkgroup with no profile gets from the global settings.",
        [
            {"kind": "afftdn", "params": {"nf": -25.0}},
            {
                "kind": "agate",
                "params": {"threshold": -50.0, "ratio": 4.0, "attack": 5.0, "release": 250.0},
            },
        ],
    ),
    (
        "Aircraft cabin",
        "Rotor and turbine noise: a 40 dB hole from 1.7 to 2.2 kHz over a 250 Hz "
        "high-pass, with a harder denoise behind it. Written for the medevac transports "
        "on the author's system, where the global chain leaves the beat audible and anything "
        "aggressive enough to remove it eats the speech.",
        [
            {"kind": "highpass", "params": {"f": 250.0}},
            {"kind": "equalizer", "params": {"f": 1950.0, "width": 500.0, "gain_db": -40.0}},
            {"kind": "afftdn", "params": {"nf": -28.0, "nr": 12.0}},
        ],
    ),
    (
        "Speech band-pass",
        "The telephone band and nothing else: 300 Hz to 3 kHz, then a denoise. For a "
        "channel whose noise sits at both ends of the spectrum rather than in one band.",
        [
            {"kind": "highpass", "params": {"f": 300.0}},
            {"kind": "lowpass", "params": {"f": 3000.0}},
            {"kind": "afftdn", "params": {"nf": -25.0}},
        ],
    ),
)


def upgrade() -> None:
    op.create_table(
        "audio_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "chain",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_audio_profiles_name"),
    )

    op.add_column("talkgroups", sa.Column("audio_profile_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_talkgroups_audio_profile_id",
        "talkgroups",
        "audio_profiles",
        ["audio_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_talkgroups_audio_profile_id", "talkgroups", ["audio_profile_id"])

    bind = op.get_bind()
    for name, description, chain in BUILTINS:
        bind.execute(
            sa.text(
                "INSERT INTO audio_profiles (name, description, chain, builtin) "
                "VALUES (:name, :description, CAST(:chain AS jsonb), true) "
                "ON CONFLICT (name) DO NOTHING"
            ),
            {"name": name, "description": description, "chain": json.dumps(chain)},
        )


def downgrade() -> None:
    op.drop_index("ix_talkgroups_audio_profile_id", table_name="talkgroups")
    op.drop_constraint("fk_talkgroups_audio_profile_id", "talkgroups", type_="foreignkey")
    op.drop_column("talkgroups", "audio_profile_id")
    op.drop_table("audio_profiles")
