"""Engine, session factory and the FastAPI session dependency.

Synchronous SQLAlchemy 2 over psycopg 3.  Everything (API, ingest worker, CLI) uses
``SessionLocal``; the API additionally uses ``get_session`` as a dependency so a request
gets one session that is closed when the response is done.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterable
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from app.config import get_settings

#: Postgres channel the ingest worker notifies and the API listens on (§5.1 step 6).
EVENT_CHANNEL = "keyup_events"

engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


#: How long a statement issued by a **read-only** HTTP request may run before PostgreSQL
#: cancels it.  Sixty seconds, and the number is a backstop rather than a budget: nothing
#: the API serves should take a tenth of it.
#:
#: It exists because a read cannot be allowed to stop ingestion.  The pool is five
#: connections with ten of overflow, shared by every reader *and* by the recorders' pushes;
#: one afternoon a Calls page with seven categories ticked launched five facet queries of
#: 14-24 s each (see :data:`app.services.calls.FACET_TIMEOUT`), the pool emptied, and
#: ``POST /api/recorder/spectrum`` timed out at 30 s with a 500 — the archive stopped
#: recording because somebody clicked a filter.  A runaway read now dies at sixty seconds
#: and gives its connection back, whatever new query shape finds a bad plan next.
#:
#: **Read-only GET and HEAD requests only**, deliberately.  Ingestion (``POST
#: /api/call-upload``, the events and spectrum batches), migrations, the CLI and the
#: workers keep no timeout at all: a slow write is a write that must finish, and killing a
#: reconcile or a backfill halfway through to protect a dashboard would be the wrong trade
#: in the other direction.  WebSocket connections are not armed either — they have no
#: method, and the live feed opens its own short sessions per poll anyway.
#:
#: The arming is an ``after_begin`` listener rather than a ``SET LOCAL`` in this function,
#: which matters twice: a GET that never touches the database does not open a transaction
#: (and so does not take a pool slot) just to set a GUC, and a statement that *is* cancelled
#: aborts its transaction — the next one this session begins is armed again on its own.
#: ``SET LOCAL`` also means the setting dies with the transaction, so nothing can leak onto
#: a connection the pool hands to a writer next.
READ_STATEMENT_TIMEOUT = "60s"

#: The methods that may not write, and are therefore safe to cancel.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})


def _arm_read_timeout(session: Session, transaction: Any, connection: Any) -> None:
    """``SET LOCAL statement_timeout`` as each transaction of a read-only request begins."""
    if getattr(connection.dialect, "name", "") != "postgresql":
        return  # SQLite and friends have no statement_timeout
    connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{READ_STATEMENT_TIMEOUT}'")


def get_session(connection: HTTPConnection) -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request.

    ``HTTPConnection`` rather than ``Request`` because the WebSocket routes take this
    dependency too (through :func:`release_session`) and a ``Request`` parameter cannot be
    resolved there.  Its only use is the method, which a WebSocket does not have — so a
    socket is never armed.  See :data:`READ_STATEMENT_TIMEOUT`.
    """
    session = SessionLocal()
    if getattr(connection, "method", "").upper() in READ_ONLY_METHODS:
        event.listen(session, "after_begin", _arm_read_timeout)
    try:
        yield session
    finally:
        session.close()


def get_probe_session() -> Generator[Session, None, None]:
    """One session for the liveness probe, deliberately **not** armed with a timeout.

    ``GET /api/healthz`` is a single ``SELECT 1`` polled every ten seconds by the container
    health check, and it exists precisely so the probe costs one statement (see
    :mod:`app.api.status`).  Arming it would add a ``SET LOCAL`` round trip to every poll
    to bound a query that cannot run long by construction — a cost with nothing on the
    other side of it.  Nothing else may use this: a route that reads real rows takes
    :func:`get_session`.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def release_session(session: Annotated[Session, Depends(get_session)]) -> None:
    """Dependency for a route that outlives its response: give the connection back **now**.

    A WebSocket route's dependencies are cleaned up when the *socket closes*, and every
    request goes through :func:`app.auth.mutation_guard`, which reads the settings table
    and usually the ``sessions``/``users`` rows too.  So a socket inherited a session that
    had run a SELECT — and therefore held an open read transaction — for its whole life.
    Production had two such sessions ``idle in transaction`` for 53 and 4 minutes: one
    pool slot each, one snapshot each pinning autovacuum off the busiest table in the
    archive, and a recorder's push channel stays up for days.

    FastAPI unwinds the exit stack for an *HTTP* request before it streams the response
    body, so an SSE route does not currently inherit the session the same way.  The SSE
    routes declare this anyway: that is a fact about a framework version, not a promise
    the route makes, and the cost of saying so is one no-op call.

    The session object stays perfectly usable — SQLAlchemy begins a new transaction on the
    next statement — so this is not "close the session", it is "stop holding the
    connection while nothing is being asked of it".  A route that declares this must not
    then take ``DbSession`` and query it across an ``await``; open a short
    ``SessionLocal()`` per poll instead, in a worker thread, as the live feed does.
    """
    session.close()


#: What a WebSocket or SSE route declares to hand its connection back at accept time.
ReleasedSession = Annotated[None, Depends(release_session)]


# ``get_db`` is the conventional FastAPI name; keep an alias so shared code reads the same.
get_db = get_session


#: The lock order for any transaction that writes both ``calls`` and ``transcription_jobs``
#: (§5.2).  Two paths race for every call — the STT worker's claim, which skips a job the
#: audio pass has already found no speech in, and the audio pass itself, which cancels the
#: transcription of a call it has just classified — and they used to take the two rows in
#: opposite orders.  Postgres resolved that the only way it can: 27 deadlocks in a day, a
#: dozen of them surfacing as a failed ``POST /api/call-upload`` the recorder had to retry.
#:
#: The rule is **the call row first, then the job row**, everywhere, with no exceptions:
#: either take it explicitly with :func:`lock_calls`, or make the ``UPDATE calls`` the
#: first of the two statements — an ``UPDATE`` is its own row lock.  Note that the session
#: factory is ``autoflush=False``: assigning to ``call.transcript_status`` does *not* write
#: the row, it writes at flush, so an ORM assignment followed by an
#: ``update(TranscriptionJob)`` takes the two rows in the wrong order.  Call
#: :func:`lock_calls` first in that shape.
#:
#: The *strength* of that lock is the second story, and it cost another 17 deadlocks a day.
#: The export worker (:meth:`app.exporters.worker.ExportWorker._handle`) claims a
#: ``call_exports`` row with ``FOR UPDATE SKIP LOCKED`` and holds it across the whole HTTP
#: upload — seconds — then writes an ``activity_events`` row whose ``call_id`` foreign key
#: makes Postgres take ``FOR KEY SHARE`` on the ``calls`` row.  Meanwhile ingest reconcile
#: (:mod:`app.ingest.reconcile`) locks both copies of a transmission here and then has
#: :func:`app.exporters.ledger.repoint_exports` UPDATE ``call_exports.call_id``, which waits
#: on the claimed ledger row.  A plain ``FOR UPDATE`` conflicts with ``FOR KEY SHARE``, so
#: the exporter's FK lookup waited on reconcile while reconcile waited on the exporter's
#: ledger row: a cycle Postgres could only break by killing one of them.
#:
#: ``FOR NO KEY UPDATE`` is the right strength, and is what :func:`lock_calls` takes.  It
#: promises only that the row's *key* columns are not touched — which is true, nothing under
#: a ``lock_calls`` transaction rewrites ``calls.id`` or deletes the row — so it does not
#: conflict with the ``FOR KEY SHARE`` an FK insert takes: the exporter's activity row
#: lands, it commits, it releases the ledger row, and reconcile's UPDATE simply waits its
#: turn instead of deadlocking.  It still conflicts with every other ``FOR NO KEY UPDATE``,
#: with a plain ``UPDATE calls`` (which is itself a non-key update), with ``FOR UPDATE`` and
#: with ``DELETE``, so the §5.2 ordering rule above is unchanged: all of the writers it
#: serializes are non-key updates of ``calls`` and they still exclude each other.
#: **§5.2b — the directory rows.**  A second pair, on different tables and with the same
#: shape.  Production deadlocked three times in twelve hours, once failing a
#: ``POST /api/call-upload`` the recorder had to retry and twice failing
#: ``POST /api/recorder/events``, always on the same two statements::
#:
#:     A: INSERT INTO radios (…) ON CONFLICT … DO UPDATE …        while inserting index tuple
#:     B: INSERT INTO talkgroups (…) ON CONFLICT uq_talkgroups_system_tgid DO UPDATE …
#:
#: An ``ON CONFLICT`` insert takes its lock when it inserts the index tuple, so the two
#: paths were locking the same two directory rows in opposite orders: the upload upserted
#: the talkgroup and then the radio, while a radio-events batch resolved the radio and then
#: the talkgroup — and a batch holds *many* rows of both kinds for its whole transaction,
#: so two batches from two recorders could deadlock with each other as well.
#:
#: The rule is **talkgroups, then radios, then affiliations — and within a kind in
#: ascending key order (``tgid``, then ``radio_id``)**.  Every multi-row writer of these
#: tables collects the keys it needs *before* it takes any of them and then issues the
#: upserts in that order:
#:
#: * :func:`app.ingest.events.record_events` parses the whole batch first (phase 1), then
#:   upserts the talkgroups and the radios it collected in sorted order (phase 2), then
#:   walks the events again for the affiliations and the ``radio_events`` rows (phase 3);
#: * :func:`app.ingest.service.upsert_call_radios` is the single ascending pass both call
#:   paths take for the keying radio, a unit call's target and every ``srcList`` entry,
#:   and both of them sort the call's talkgroups ahead of it;
#: * :func:`app.ingest.service.write_call_sources` defers its affiliation touches to the
#:   end of the call, in ascending radio order.
#:
#: Patches and sites are on their own tables and neither path takes them in a contended
#: order, but they belong with the talkgroups — before the radios — and that is where both
#: call paths leave them.  The bulk importers (``app.ingest.playlist_import``, the RR sync,
#: the migration tool) run alone against the directory rather than beside ingest; they
#: still sort within an entry, which costs nothing and keeps the rule uniform.
def lock_calls(session: Session, call_ids: int | Iterable[int | None] | None) -> list[int]:
    """Take the ``calls`` row locks this transaction needs, before it touches any job row.

    ``SELECT id FROM calls WHERE id IN (…) ORDER BY id FOR NO KEY UPDATE`` — ordered, so two
    callers locking the same pair of calls cannot deadlock against each other either, and
    held until the caller commits.  Pending ORM changes are flushed first: a call that has
    not been INSERTed yet has no row to lock.

    ``key_share=True`` is SQLAlchemy's spelling of ``FOR NO KEY UPDATE`` (``read=True``
    would be the weaker ``FOR KEY SHARE``); see the note above for why the weaker mode is
    the correct one here and what it still excludes.

    Returns the ids actually locked (a call deleted underneath the caller is simply
    absent), which lets a caller tell "gone" from "locked" without a second query.
    """
    from app.models import Call  # noqa: PLC0415 - app.models must not import app.db

    if call_ids is None:
        return []
    wanted = [call_ids] if isinstance(call_ids, int) else [int(i) for i in call_ids if i is not None]
    ids = sorted(set(wanted))
    if not ids:
        return []
    session.flush()
    return list(
        session.scalars(
            select(Call.id)
            .where(Call.id.in_(ids))
            .order_by(Call.id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE
        )
    )


def notify(session: Session, event: str, payload: dict[str, Any]) -> None:
    """Emit a live-bus event on ``keyup_events``.

    The payload is ``{"event": ..., **payload}``.  ``pg_notify`` refuses payloads over
    8000 bytes, so callers should send identifiers, not rows; the API re-reads the call.
    """
    body = json.dumps({"event": event, **payload}, default=str)
    if len(body.encode()) > 7900:  # pragma: no cover - defensive
        body = json.dumps({"event": event, "id": payload.get("id")}, default=str)
    session.execute(text("SELECT pg_notify(:chan, :payload)"), {"chan": EVENT_CHANNEL, "payload": body})
