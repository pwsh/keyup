"""The builtin "Default" profile follows the global chain's new band-pass and denoiser.

The global noise section changed (the 2026-09-10 audio review): a
300-3400 Hz band-pass now runs ahead of the denoiser and ``afftdn`` runs at
``nf=-30:nr=18`` rather than ``nf=-25``.  "Default" is that section *written as data* —
assigning it to a talkgroup is supposed to change nothing — so a row still carrying the
old two stages would silently freeze those channels on the chain we just measured as the
worse one.

The update is deliberately narrow, and idempotent for that reason: it fires only on the
``builtin`` row named "Default" **whose chain is still exactly the one this migration's
predecessor inserted**.  A builtin an operator has edited is an operator's decision — they
are editable on purpose — and every custom profile is untouched by name.  Running it twice
matches nothing the second time.

Revision ID: e9a4c71b05d3
Revises: b1c7e94af205
Create Date: 2026-09-10
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e9a4c71b05d3"
down_revision: str | None = "b1c7e94af205"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GATE = {
    "kind": "agate",
    "params": {"threshold": -50.0, "ratio": 4.0, "attack": 5.0, "release": 250.0},
}

OLD_CHAIN = [{"kind": "afftdn", "params": {"nf": -25.0}}, _GATE]
OLD_DESCRIPTION = (
    "The shipped chain, as data: FFT denoise at a -25 dB noise floor, then the "
    "-50 dBFS gate. What a talkgroup with no profile gets from the global settings."
)

NEW_CHAIN = [
    {"kind": "highpass", "params": {"f": 300.0}},
    {"kind": "lowpass", "params": {"f": 3400.0}},
    {"kind": "afftdn", "params": {"nf": -30.0, "nr": 18.0}},
    _GATE,
]
NEW_DESCRIPTION = (
    "The shipped chain, as data: the 300-3400 Hz speech band-pass, FFT denoise at "
    "a -30 dB noise floor taking 18 dB out, then the -50 dBFS gate. What a "
    "talkgroup with no profile gets from the global settings."
)

_SQL = sa.text(
    "UPDATE audio_profiles SET chain = CAST(:new AS jsonb), description = :description, "
    "updated_at = now() "
    "WHERE name = 'Default' AND builtin IS TRUE AND chain = CAST(:old AS jsonb)"
)


def _swap(old: list[dict], new: list[dict], description: str) -> None:
    op.get_bind().execute(
        _SQL, {"old": json.dumps(old), "new": json.dumps(new), "description": description}
    )


def upgrade() -> None:
    _swap(OLD_CHAIN, NEW_CHAIN, NEW_DESCRIPTION)


def downgrade() -> None:
    _swap(NEW_CHAIN, OLD_CHAIN, OLD_DESCRIPTION)
