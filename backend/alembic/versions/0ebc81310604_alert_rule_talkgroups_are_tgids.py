"""Stored talkgroup alert rules: every id is an on-air tgid, not a ``talkgroups.id``.

A ``talkgroup`` alert rule matched each of its ids against the call's tgids **and** its
``talkgroups.id`` primary key.  The two number spaces overlap (43 collisions on production
when c2d54c0 fixed the same bug in ``?talkgroup=``), so a rule for one channel also fired
on an unrelated one.  The matcher now reads the ids as tgids only;
``a7f3c2d91b64`` translated ``feeds`` and ``saved_searches`` but not
``alert_rules``, and this migration does.

Where the ids live, on ``kind = 'talkgroup'`` rows only: ``filters.talkgroup_ids`` (what
the API and the Alerts page write), ``filters.talkgroups`` (accepted by the matcher) and
the comma/space-separated ``pattern``.  ``filters.tgids`` is by name already a tgid list
and is left alone, as are the text kinds' patterns — a keyword rule's ``52`` is a word.

**Which reading wins differs from a7f3c2d91b64, on purpose.**  There, the SPA's talkgroup
picker had written primary keys, so a value that could be read either way was taken as the
pk.  Rule ids were never written by a picker: they are typed by hand as the number on the
air, and the Alerts page's own help calls them channels.  So here:

* a value that is some talkgroup's ``tgid`` stays exactly as it is;
* a value that is no ``tgid`` but is a ``talkgroups.id`` becomes that row's ``tgid`` —
  the only reading under which the rule ever fired;
* anything else (a number this archive has never seen) stays as it is.

That makes the upgrade a fixed point: every value it writes is a known tgid, which a
second run leaves alone.

Production at the time of writing holds **0 alert rules** (``SELECT count(*) FROM
alert_rules`` = 0, 2026-09-23), so nothing there is touched.  Written for the deployments
and backups that have some.

The downgrade is a no-op, and loses nothing: the old matcher accepted a tgid too, so a
rewritten rule fires on the same channel after a downgrade as it did before it.

Revision ID: 0ebc81310604
Revises: 09bfeda20009
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0ebc81310604"
down_revision: str | None = "09bfeda20009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: The ``filters`` keys whose values the matcher reads as talkgroup ids.  ``tgids`` is not
#: here: its name already says what it holds.
FILTER_KEYS = ("talkgroup_ids", "talkgroups")

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


def _translate(value: Any, pk_to_tgid: dict[int, int], tgids: set[int]) -> Any:
    """One stored id under the rule in the module docstring."""
    number = _as_int(value)
    if number is None or number in tgids or number not in pk_to_tgid:
        return value
    return pk_to_tgid[number]


def _rewrite_pattern(pattern: str, pk_to_tgid: dict[int, int], tgids: set[int]) -> str:
    """Translate each numeric token in place; separators and words are kept verbatim."""
    return _TOKEN.sub(
        lambda m: str(_translate(m.group(0), pk_to_tgid, tgids))
        if _as_int(m.group(0)) is not None
        else m.group(0),
        pattern,
    )


def rewrite(conn: sa.Connection, pk_to_tgid: dict[int, int], tgids: set[int]) -> int:
    """Rewrite every talkgroup rule's stored ids; return how many rows changed."""
    changed = 0
    rows = list(
        conn.execute(sa.text("SELECT id, pattern, filters FROM alert_rules WHERE kind = 'talkgroup'"))
    )
    for row_id, pattern, filters in rows:
        new_filters = dict(filters) if isinstance(filters, dict) else filters
        touched = False
        if isinstance(new_filters, dict):
            for key in FILTER_KEYS:
                if key not in new_filters:
                    continue
                raw = new_filters[key]
                values = raw if isinstance(raw, list) else [raw]
                rewritten = [_translate(v, pk_to_tgid, tgids) for v in values]
                if rewritten != values:
                    new_filters[key] = rewritten if isinstance(raw, list) else rewritten[0]
                    touched = True
        new_pattern = pattern
        if isinstance(pattern, str) and pattern:
            new_pattern = _rewrite_pattern(pattern, pk_to_tgid, tgids)
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
    pk_to_tgid: dict[int, int] = {}
    tgids: set[int] = set()
    for row_id, tgid in conn.execute(sa.text("SELECT id, tgid FROM talkgroups WHERE tgid IS NOT NULL")):
        pk_to_tgid[int(row_id)] = int(tgid)
        tgids.add(int(tgid))
    rewrite(conn, pk_to_tgid, tgids)


def downgrade() -> None:
    # The old matcher read a tgid as well as a pk, so the rewritten rules still fire on the
    # channel they name; there is nothing to put back.
    pass
