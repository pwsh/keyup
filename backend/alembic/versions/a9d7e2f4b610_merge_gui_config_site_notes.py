"""Merge point: ``sites.notes`` and the operator-editable configuration tables.

Two unrelated lines of work landed on ``e7b3d9a24c18`` at the same time.  ``main`` went on
to ``f8c2a4e19d53`` (``sites.notes``) and then ``a9e4b2c17f08`` (the conversations index),
while the GUI-configuration branch added ``b3e91c47d5a2`` (``stt_backends``,
``ingest_sources`` and ``stt_backend_stats.config_version``).  Neither touches anything the
other does, so there is nothing to reconcile and nothing to write here; alembic only needs
to be told that in the form it understands, which is a revision with two parents — the
branch's head, and whatever ``main``'s head is at the moment of the merge.

Without it ``alembic heads`` reports two heads and ``alembic upgrade head`` refuses to
choose between them, which would strand every deployment — and every test database — on
whichever of the pair it happened to reach first.

Revision ID: a9d7e2f4b610
Revises: b3e91c47d5a2, a9e4b2c17f08
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a9d7e2f4b610"
down_revision: tuple[str, ...] | None = ("b3e91c47d5a2", "a9e4b2c17f08")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the merge is the whole of this revision."""


def downgrade() -> None:
    """Nothing to undo."""
