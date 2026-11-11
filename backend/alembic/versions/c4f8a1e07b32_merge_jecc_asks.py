"""Merge point: the outbound webhook queue and main's GUI-configuration line.

``a2e7b4c60d19`` (``webhook_deliveries``) was cut from ``f8c2a4e19d53``, and while it was
being written ``main`` went on through ``a9e4b2c17f08`` (the conversations index),
``b3e91c47d5a2`` (``stt_backends``, ``ingest_sources``) and their own merge point
``a9d7e2f4b610``.  Nothing in either line touches a table or a type the other does — a new
standalone queue table on one side, new configuration tables and an index on the other — so
there is nothing to reconcile and nothing to write here.  Alembic only needs to be told
that in the form it understands: a revision with two parents.

Without it ``alembic heads`` reports two heads and ``alembic upgrade head`` refuses to
choose between them, which strands every deployment — and every test database — on
whichever of the pair it happened to reach first.

Revision ID: c4f8a1e07b32
Revises: a2e7b4c60d19, a9d7e2f4b610
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c4f8a1e07b32"
down_revision: tuple[str, ...] | None = ("a2e7b4c60d19", "a9d7e2f4b610")
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the merge is the whole of this revision."""


def downgrade() -> None:
    """Nothing to undo."""
