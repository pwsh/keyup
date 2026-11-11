"""The ``ingest_sources`` table: where recordings come from, and whether it still works.

``AUDIO_ROOT`` was ingest's entire configuration, read once at process start.  It was
deliberately left out of the runtime settings — a switch that needs a restart to take effect
is worse than no switch — and this module is what removes the restart rather than the
switch: :class:`app.ingest.watcher.IngestWatcher` holds one watchdog watch per enabled
``watch_dir`` row and re-reads the table on a short cadence, so a directory added here is
being watched within half a minute.

Three things live here, shared by the watcher and the admin API so that neither can drift
from the other:

* :func:`version` / :func:`bump` — the same generation counter :mod:`app.stt.config_store`
  keeps for the backends, under its own key.  The watcher's reload is a single indexed read
  in the common case, which is what makes polling every 30 seconds unremarkable.
* :func:`watch_dirs` — the rows the watcher acts on.
* :func:`path_state` — the answer to "can this process actually read that directory", asked
  on save *and* on every read, because a path that was fine when it was saved is exactly the
  kind of thing that stops being fine at three in the morning.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Integer, Text, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import IngestSource, Setting

log = logging.getLogger(__name__)

__all__ = [
    "CONFIG_VERSION_KEY",
    "PATH_STATES",
    "WatchDir",
    "bump",
    "count_watch_dirs",
    "path_state",
    "record_error",
    "touch_activity",
    "version",
    "watch_dirs",
]

#: the ``settings`` row the watcher polls
CONFIG_VERSION_KEY = "ingest_config_version"

#: ``ok`` is the only one that gets watched.  The others are distinguished because they are
#: different operator actions: create the directory, fix the path, fix the permissions.
PATH_STATES = ("ok", "missing", "not_a_directory", "unreadable", "n/a")


@dataclass(frozen=True, slots=True)
class WatchDir:
    """One enabled ``watch_dir`` row, flattened to what the watcher needs.

    A frozen snapshot rather than the ORM row: the watcher compares the set it is running
    against the set the table wants on every tick, from a thread that has no session, and a
    detached instance would be a lazy-load waiting to happen.
    """

    id: int
    label: str
    path: Path
    recursive: bool = True
    tr_prefer_audio: str | None = None
    tr_sidecar_wait_seconds: float | None = None


# --------------------------------------------------------------------------------------
# the version stamp
# --------------------------------------------------------------------------------------


def version(session: Session) -> int:
    """The current ingest-sources generation.  ``0`` when nothing has ever been written."""
    raw = session.scalar(select(Setting.value).where(Setting.key == CONFIG_VERSION_KEY))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def bump(session: Session, *, by: str | None = None) -> int:
    """Advance the generation and return the new value.  Does not commit."""
    stmt = pg_insert(Setting).values(
        key=CONFIG_VERSION_KEY,
        value=1,
        description="Bumped on every change to ingest_sources; the watcher reloads when it moves.",
        updated_by=by,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={
            # ``jsonb -> text -> int -> jsonb``.  The JSON index operators cannot help
            # here: the value is a bare number, not an object with a key to reach into.
            "value": func.to_jsonb(cast(cast(Setting.value, Text), Integer) + 1),
            "updated_by": stmt.excluded.updated_by,
        },
    ).returning(Setting.value)
    result = session.execute(stmt).scalar()
    try:
        return int(result)
    except (TypeError, ValueError):  # pragma: no cover - RETURNING always gives the number
        return version(session)


# --------------------------------------------------------------------------------------
# the rows
# --------------------------------------------------------------------------------------


def watch_dirs(session: Session) -> list[WatchDir]:
    """Every enabled ``watch_dir``, oldest first (so the seeded AUDIO_ROOT leads)."""
    rows = session.scalars(
        select(IngestSource)
        .where(
            IngestSource.kind == "watch_dir",
            IngestSource.enabled.is_(True),
            IngestSource.path.is_not(None),
        )
        .order_by(IngestSource.id)
    )
    return [
        WatchDir(
            id=row.id,
            label=row.label,
            path=Path(row.path or ""),
            recursive=row.recursive,
            tr_prefer_audio=row.tr_prefer_audio,
            tr_sidecar_wait_seconds=row.tr_sidecar_wait_seconds,
        )
        for row in rows
    ]


def count_watch_dirs(session: Session) -> int:
    """How many ``watch_dir`` rows exist, enabled or not.

    The watcher needs this to tell "nobody has ever configured a source" — in which case
    ``AUDIO_ROOT`` is still the answer and ingest must carry on exactly as before — from
    "the operator has switched every source off", which is an instruction, not an absence.
    """
    return int(
        session.scalar(
            select(func.count()).select_from(IngestSource).where(IngestSource.kind == "watch_dir")
        )
        or 0
    )


def path_state(path: str | os.PathLike[str] | None) -> tuple[str, str | None]:
    """``(state, detail)`` for a watch directory — see :data:`PATH_STATES`.

    ``os.access`` rather than a ``try: open``: the question is whether *this* process, with
    the uid it is actually running as, may list the directory, and that is not something a
    ``Path.exists()`` can answer for an NFS mount or a root-owned spool.  ``R_OK`` to list
    it and ``X_OK`` to descend into it — a directory that is readable but not executable
    yields names and then denies every stat on them, which is a confusing way to fail.
    """
    if path is None or str(path).strip() == "":
        return "n/a", None
    candidate = Path(str(path))
    if not candidate.exists():
        return "missing", f"{candidate} does not exist"
    if not candidate.is_dir():
        return "not_a_directory", f"{candidate} is not a directory"
    if not os.access(candidate, os.R_OK | os.X_OK):
        return "unreadable", f"{candidate} is not readable by this process"
    return "ok", None


def record_error(session: Session, source_id: int, message: str | None) -> None:
    """Write (or clear, with ``None``) a source's last error.  Does not commit.

    The watcher's half of the row.  Kept here rather than inline in the watcher so that the
    truncation and the "clearing an already-clear error is not a write" rule live with the
    column rather than with one of its two writers.
    """
    row = session.get(IngestSource, source_id)
    if row is None:
        return
    if message is None:
        if row.last_error is not None:
            row.last_error = None
            row.last_error_at = None
        return
    text = message[:2000]
    if row.last_error == text:
        return
    row.last_error = text
    row.last_error_at = func.now()


def touch_activity(session: Session, source_id: int) -> None:
    """Mark that this source produced something just now.  Does not commit."""
    row = session.get(IngestSource, source_id)
    if row is not None:
        row.last_activity_at = func.now()
