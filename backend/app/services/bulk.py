"""One patch, many rows: the shared half of ``POST /{talkgroups,radios,units,personnel}/bulk``.

The four bulk endpoints differ only in their table, their validation and their words.  What
they have in common — resolve the ids, refuse the batch if any of them is gone, write the
same fields onto every row, and leave one line in the activity log saying what happened —
lives here, so that "bulk edit" means the same thing on all four pages.

Two decisions worth stating:

* **All or nothing.**  A missing id is a 404 for the whole request rather than a silently
  smaller ``updated`` count.  The operator ticked *n* rows; being told "updated 97" without
  being told which three were skipped is worse than being told to reload.
* **One activity line, not one per row.**  A hundred-row edit is one operator action, and a
  log that buries the day's ingest under a hundred ``talkgroup_edited`` rows has made the
  activity page useless for the thing it exists for.  The ids go in ``meta``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activity


def resolve(db: Session, model: Any, ids: list[int], *, noun: str) -> list[Any]:
    """The rows for ``ids``, in id order, or a 404 naming the ones that are not there.

    Duplicated ids are collapsed: ticking a row twice is not an error, it is the same row.
    """
    wanted = list(dict.fromkeys(ids))
    rows = list(db.scalars(select(model).where(model.id.in_(wanted)).order_by(model.id)))
    missing = sorted(set(wanted) - {row.id for row in rows})
    if missing:
        listed = ", ".join(str(i) for i in missing[:10])
        more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
        raise HTTPException(status_code=404, detail=f"no {noun} {listed}{more}")
    return rows


def apply_fields(rows: list[Any], fields: dict[str, Any]) -> None:
    """Write every key of ``fields`` onto every row.

    ``fields`` comes from ``model_dump(exclude_unset=True)``, so a key that is present with
    ``None`` clears the column exactly as the single ``PATCH`` would, and a key that was
    never sent is not touched at all.
    """
    for row in rows:
        for key, value in fields.items():
            setattr(row, key, value)


def record(
    db: Session,
    *,
    kind: str,
    noun: str,
    ids: list[int],
    fields: dict[str, Any],
    by: str,
) -> None:
    """The single activity line summarizing the edit: how many rows, and which columns."""
    names = ", ".join(sorted(fields))
    activity.record(
        db,
        source="system",
        level="info",
        kind=kind,
        message=f"Edited {len(ids)} {noun} ({names})",
        meta={"ids": ids, "fields": sorted(fields), "count": len(ids), "by": by},
    )


def require_changes(fields: dict[str, Any]) -> None:
    """A bulk edit that changes nothing is a mistake worth naming, not a no-op 200."""
    if not fields:
        raise HTTPException(status_code=422, detail="changes must set at least one field")


__all__ = ["apply_fields", "record", "require_changes", "resolve"]
