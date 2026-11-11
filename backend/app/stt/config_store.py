"""The ``stt_backends`` table, and the version stamp that makes an edit reach the worker.

Two processes have to agree about which backends exist: the API, which answers the admin
page and accepts the operator's edits, and ``keyup worker``, which owns the one
:class:`~app.stt.router.BackendRouter` that actually routes jobs.  They share no memory, so
"the operator added a backend" has to travel through the database — the same crossing
:mod:`app.stt.stats_store` makes in the other direction for the counters.

Two things live here:

* **the rows** — :func:`entries` returns the backends as the plain dicts
  :func:`app.stt.router.build_backends` and the admin page both already speak, so nothing
  downstream has to learn a new shape.  The precedence rule is one sentence and is enforced
  in exactly one place, :func:`entries`: *the database wins; the environment only seeds.*
  ``STT_BACKENDS`` is copied into the table by the migration that creates it, and read
  again only when the table has no rows at all — a deployment that has never touched the
  admin page keeps running off its environment exactly as before, and one that has cannot
  be surprised by a stale ``.env`` on the host.

* **the version** — :func:`version` / :func:`bump` maintain a single integer in the
  ``settings`` table.  Every write to the table bumps it; the worker compares it on its
  ten-second publish tick and rebuilds its router when it has moved
  (:mod:`app.stt.hotswap`).  A counter rather than a timestamp because the only question
  either side asks is "is this the same configuration I built from?", and two clocks are
  one more thing to be wrong about.

``api_key`` is stored here and never leaves: :func:`entries` includes it because the router
needs it, and the API's response models simply do not have the field.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Integer, Text, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings, SttBackend, get_settings
from app.models import Setting, SttBackendConfig

log = logging.getLogger(__name__)

__all__ = [
    "CONFIG_VERSION_KEY",
    "EDITABLE_FIELDS",
    "bump",
    "all_rows",
    "entries",
    "entries_for",
    "entries_from_env",
    "live_rows",
    "row_entry",
    "seed_from_env",
    "version",
]

#: the ``settings`` row the worker polls.  Not in ``SETTINGS_SPEC``: it is not a knob an
#: operator sets, it is a stamp the CRUD endpoints maintain.
CONFIG_VERSION_KEY = "stt_config_version"

#: the columns a POST/PUT may assign — every field of :class:`app.config.SttBackend` except
#: the name (the key) plus ``notes``.  ``api_key`` is here because it is *writable*; that it
#: is never readable is the response models' business.
EDITABLE_FIELDS = (
    "kind",
    "base_url",
    "api_key",
    "model",
    "device",
    "compute_type",
    "cpu_threads",
    "cache_dir",
    "use_prompt",
    "use_hotwords",
    "concurrency",
    "priority",
    "timeout",
    "enabled",
    "notes",
)

#: the fields :func:`row_entry` puts on the wire to ``build_backends`` and the admin page
_ENTRY_FIELDS = (
    "name",
    "kind",
    "base_url",
    "api_key",
    "model",
    "device",
    "compute_type",
    "cpu_threads",
    "cache_dir",
    "use_prompt",
    "use_hotwords",
    "concurrency",
    "priority",
    "timeout",
    "enabled",
)


# --------------------------------------------------------------------------------------
# the version stamp
# --------------------------------------------------------------------------------------


def version(session: Session) -> int:
    """The current STT configuration version.  ``0`` when nothing has ever been written."""
    raw = session.scalar(select(Setting.value).where(Setting.key == CONFIG_VERSION_KEY))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def bump(session: Session, *, by: str | None = None) -> int:
    """Advance the version and return the new value.  Does not commit.

    An ``INSERT … ON CONFLICT DO UPDATE`` that reads the old value from the row itself, so
    two admins saving at once cannot both write the same number and leave a worker that
    already applied one of them thinking it is up to date.
    """
    stmt = pg_insert(Setting).values(
        key=CONFIG_VERSION_KEY,
        value=1,
        description="Bumped on every change to stt_backends; the worker rebuilds its router when it moves.",
        updated_by=by,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.key],
        set_={
            # ``value`` is JSONB, so the arithmetic has to come out of it and go back in.
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


def row_entry(row: SttBackendConfig) -> dict[str, Any]:
    """One row as the dict ``build_backends`` and ``_configured_backends`` consume."""
    return {field: getattr(row, field) for field in _ENTRY_FIELDS}


def live_rows(session: Session) -> list[SttBackendConfig]:
    """Every backend that has not been deleted, cheapest priority first."""
    return list(
        session.scalars(
            select(SttBackendConfig)
            .where(SttBackendConfig.deleted_at.is_(None))
            .order_by(SttBackendConfig.priority, SttBackendConfig.name)
        )
    )


def all_rows(session: Session) -> list[SttBackendConfig]:
    """Every backend including the soft-deleted ones — the admin page's full list."""
    return list(
        session.scalars(
            select(SttBackendConfig).order_by(SttBackendConfig.priority, SttBackendConfig.name)
        )
    )


def entries_from_env(settings: Settings | None = None) -> list[dict[str, Any]]:
    """``STT_BACKENDS`` as entry dicts — the seed, and the fallback for an empty table."""
    settings = settings or get_settings()
    return [backend.model_dump() for backend in settings.stt_backends]


def entries(session: Session, settings: Settings | None = None) -> list[dict[str, Any]]:
    """The backends this deployment runs on, database first.

    The whole precedence rule, in one function: any row at all — even a disabled or
    soft-deleted one — means the operator owns this configuration and ``STT_BACKENDS`` is
    not consulted.  A table with no rows whatsoever falls back to the environment, which is
    what keeps a deployment whose migration has run but whose seed found nothing (an empty
    ``STT_BACKENDS`` at upgrade time, later filled in) from losing its transcription.
    """
    if session.scalar(select(SttBackendConfig.name).limit(1)) is None:
        return entries_from_env(settings)
    return [row_entry(row) for row in live_rows(session)]


def entries_for(
    session_factory: Callable[[], Session] | None,
    settings: Settings | None = None,
    *,
    fallback_to_env: bool = True,
) -> list[dict[str, Any]]:
    """:func:`entries` with its own session, optionally falling back to the environment.

    The worker builds its router from this at **start-up**, before anything has proved the
    database is reachable.  Failing to start because the ``settings`` table could not be
    read would be a worse outcome than starting on the environment's backends, so there an
    unreadable table is logged and the environment answers.

    A **reload** is the opposite case and passes ``fallback_to_env=False``.  By then a
    router built from the operator's rows is already running, and a transient read failure
    is not evidence that they changed their mind — swallowing it would swap the deployment
    onto whatever stale list is still in the host's ``.env`` and, worse, let the caller
    stamp that as the new configuration version, so the page would report the edit applied
    and no later tick would ever correct it.  Letting the read raise leaves the old router
    in place and the version unmarked, which is what a transient failure deserves.
    """
    if session_factory is None:
        if not fallback_to_env:
            raise RuntimeError("no session factory: stt_backends cannot be read")
        return entries_from_env(settings)
    session = None
    try:
        # Opening the session is inside the guard too: a dead connection pool fails here,
        # not in the query, and start-up must survive that the same way.
        session = session_factory()
        return entries(session, settings)
    except Exception:  # noqa: BLE001 - a router on the env's backends beats no router
        if not fallback_to_env:
            raise
        log.exception("stt: could not read stt_backends; falling back to STT_BACKENDS")
        return entries_from_env(settings)
    finally:
        if session is not None:
            session.close()


def seed_from_env(
    session: Session, backends: Sequence[SttBackend] | None = None, settings: Settings | None = None
) -> int:
    """Copy ``STT_BACKENDS`` into an **empty** table; return how many rows were written.

    Refuses to write anything if the table already has a row, which is the seeding half of
    the precedence rule and the reason the migration can be re-run (or the table restored
    from a dump) without the environment quietly reappearing on top of the operator's work.
    """
    if session.scalar(select(SttBackendConfig.name).limit(1)) is not None:
        return 0
    settings = settings or get_settings()
    rows = list(backends if backends is not None else settings.stt_backends)
    for backend in rows:
        values = backend.model_dump()
        session.add(SttBackendConfig(source="env", **values))
    return len(rows)
