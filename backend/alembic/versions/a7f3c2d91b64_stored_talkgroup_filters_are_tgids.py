"""Stored call filters: ``query->'talkgroup'`` holds on-air tgids, not ``talkgroups.id``.

``?talkgroup=`` used to accept **either** the on-air ``tgid`` or this archive's
``talkgroups.id`` and matched both readings at once.  The two number spaces overlap — on
production 43 talkgroups have an ``id`` equal to some *other* talkgroup's ``tgid`` — so a
filter built from the SPA's picker (which sent the primary key) quietly returned another
channel's traffic as well.  The parameter now means the on-air tgid and nothing else, and
the SPA sends tgids; this migration brings the filter objects that were saved under the
old reading along with it.

Two tables hold a ``GET /calls`` filter object verbatim: ``feeds.query`` (the RSS/Atom
feeds) and ``saved_searches.query``.  Each value under ``talkgroup`` that matches a
``talkgroups.id`` is replaced by that row's ``tgid``; a value that matches no row is left
exactly as it is, because it is already an on-air number this archive has never seen a
talkgroup row for (an old channel, another system) and inventing a change would lose it.

**A value that matches both a ``talkgroups.id`` and some talkgroup's ``tgid`` is read as
the primary key** — that is what the SPA meant when it wrote the filter, so it is the
reading that preserves the author's intent.  The consequence, said plainly: for such a
value the rewrite is not a fixed point.  Running this migration twice would translate it a
second time (the tgid it produced may itself be another row's ``id``).  Everything else —
and that is every value in every row on production, see below — is untouched by a second
run, because a value that matches no ``talkgroups.id`` is never rewritten.

Production at the time of writing holds **0 rows to touch**: ``feeds`` is empty (0 rows)
and ``saved_searches`` is empty (0 rows), so no stored filter names a talkgroup at all.
The migration is written for the deployments and backups that do.

The downgrade is best-effort and lossy by nature: it maps a value back to a
``talkgroups.id`` only when exactly one talkgroup carries that ``tgid``, since nothing
records which values this migration rewrote.

Revision ID: a7f3c2d91b64
Revises: b3d7a1c95e20
Create Date: 2026-09-22
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "a7f3c2d91b64"
down_revision: str | None = "b3d7a1c95e20"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: The tables whose ``query`` column is a ``GET /calls`` filter object.
TABLES = ("feeds", "saved_searches")


def _as_int(value: Any) -> int | None:
    """``3``/``"3"`` → 3; anything else → ``None`` (left alone rather than mangled)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _remap(conn: sa.Connection, mapping: dict[int, int]) -> int:
    """Apply ``mapping`` to every ``query->'talkgroup'`` value; return rows changed."""
    changed = 0
    for table in TABLES:
        rows = list(
            conn.execute(
                sa.text(f"SELECT id, query FROM {table} WHERE jsonb_exists(query, 'talkgroup')")  # noqa: S608
            )
        )
        for row_id, query in rows:
            raw = (query or {}).get("talkgroup")
            values = raw if isinstance(raw, list) else [raw]
            rewritten: list[Any] = []
            touched = False
            for value in values:
                number = _as_int(value)
                if number is not None and number in mapping:
                    rewritten.append(mapping[number])
                    touched = touched or mapping[number] != number
                else:
                    rewritten.append(value)
            if not touched:
                continue
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET query = jsonb_set(query, '{{talkgroup}}', "  # noqa: S608
                    "CAST(:value AS jsonb)) WHERE id = :id"
                ),
                {"value": json.dumps(rewritten if isinstance(raw, list) else rewritten[0]), "id": row_id},
            )
            changed += 1
    return changed


def upgrade() -> None:
    conn = op.get_bind()
    # ``talkgroups.id`` → its on-air number.  The pk reading wins for a value that could be
    # read either way; see the module docstring for why, and for what that costs.
    mapping = {
        int(row[0]): int(row[1])
        for row in conn.execute(sa.text("SELECT id, tgid FROM talkgroups WHERE tgid IS NOT NULL"))
    }
    _remap(conn, mapping)


def downgrade() -> None:
    conn = op.get_bind()
    # tgid → pk, only where the tgid names exactly one talkgroup.  A tgid two systems both
    # use has no single primary key to go back to, so those values stay as they are.
    counts: dict[int, list[int]] = {}
    for row in conn.execute(sa.text("SELECT id, tgid FROM talkgroups WHERE tgid IS NOT NULL")):
        counts.setdefault(int(row[1]), []).append(int(row[0]))
    mapping = {tgid: ids[0] for tgid, ids in counts.items() if len(ids) == 1}
    _remap(conn, mapping)
