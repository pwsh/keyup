"""Stored radio alert rules: every id is an on-air radio number, not a ``radios.id``.

A ``radio`` alert rule matched each of its ids against the call's ``calls.radio_id`` — a
``radios.id`` row id — **and** the on-air number of the radio behind it.  The two number
spaces overlap, so a rule for radio ``57`` also fired on whichever radio happened to be
row 57.  The matcher now reads the ids as on-air numbers only (the radio half of
what ``0ebc81310604`` did for talkgroups), and this migration rewrites the stored ones.

Where the ids live, on ``kind = 'radio'`` rows only: ``filters.radio_ids`` (what the API
writes), ``filters.radios`` (accepted by the matcher) and the comma/space-separated
``pattern``.  The rule is the same as ``0ebc81310604``'s, for the same reason — rule ids
are typed by hand as the number on the air, never picked from a list of rows:

* a value that is some radio's ``radio_id`` stays exactly as it is;
* a value that is no ``radio_id`` but is a ``radios.id`` becomes that row's ``radio_id`` —
  the only reading under which the rule ever fired;
* anything else (a number this archive has never seen) stays as it is.

Every value it writes is a known radio number, so a second run leaves it alone.

Production holds **0 alert rules** (2026-09-23), so nothing there is touched; this is for
the deployments and backups that have some.  The downgrade is a no-op and loses nothing:
the old matcher accepted the on-air number too.

Revision ID: 3216a949614d
Revises: c4e9a1d07b35
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "3216a949614d"
down_revision: str | None = "c4e9a1d07b35"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: The ``filters`` keys whose values the matcher reads as radio ids.
FILTER_KEYS = ("radio_ids", "radios")

_TOKEN = re.compile(r"[^\s,]+")


def _as_int(value: Any) -> int | None:
    """``3``/``"3"`` → 3; anything else → ``None`` (left alone rather than mangled)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _translate(value: Any, pk_to_number: dict[int, int], numbers: set[int]) -> Any:
    """One stored id under the rule in the module docstring."""
    number = _as_int(value)
    if number is None or number in numbers or number not in pk_to_number:
        return value
    return pk_to_number[number]


def _rewrite_pattern(pattern: str, pk_to_number: dict[int, int], numbers: set[int]) -> str:
    """Translate each numeric token in place; separators and words are kept verbatim."""
    return _TOKEN.sub(
        lambda m: str(_translate(m.group(0), pk_to_number, numbers))
        if _as_int(m.group(0)) is not None
        else m.group(0),
        pattern,
    )


def rewrite(conn: sa.Connection, pk_to_number: dict[int, int], numbers: set[int]) -> int:
    """Rewrite every radio rule's stored ids; return how many rows changed."""
    changed = 0
    rows = list(conn.execute(sa.text("SELECT id, pattern, filters FROM alert_rules WHERE kind = 'radio'")))
    for row_id, pattern, filters in rows:
        new_filters = dict(filters) if isinstance(filters, dict) else filters
        touched = False
        if isinstance(new_filters, dict):
            for key in FILTER_KEYS:
                if key not in new_filters:
                    continue
                raw = new_filters[key]
                values = raw if isinstance(raw, list) else [raw]
                rewritten = [_translate(v, pk_to_number, numbers) for v in values]
                if rewritten != values:
                    new_filters[key] = rewritten if isinstance(raw, list) else rewritten[0]
                    touched = True
        new_pattern = pattern
        if isinstance(pattern, str) and pattern:
            new_pattern = _rewrite_pattern(pattern, pk_to_number, numbers)
            touched = touched or new_pattern != pattern
        if not touched:
            continue
        conn.execute(
            sa.text(
                "UPDATE alert_rules SET pattern = :pattern, filters = CAST(:filters AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "pattern": new_pattern,
                "filters": json.dumps(new_filters) if new_filters is not None else None,
                "id": row_id,
            },
        )
        changed += 1
    return changed


def upgrade() -> None:
    conn = op.get_bind()
    pk_to_number: dict[int, int] = {}
    numbers: set[int] = set()
    for row_id, number in conn.execute(sa.text("SELECT id, radio_id FROM radios")):
        pk_to_number[int(row_id)] = int(number)
        numbers.add(int(number))
    rewrite(conn, pk_to_number, numbers)


def downgrade() -> None:
    # The old matcher read the on-air number as well as the row id, so the rewritten rules
    # still fire on the radio they name; there is nothing to put back.
    pass
