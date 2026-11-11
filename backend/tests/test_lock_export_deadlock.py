"""The second deadlock pair: the export worker's held ledger row against reconcile.

Production deadlocked 17 times a day on one pair of statements, with no overlap at all
with the §5.2 pair in :mod:`tests.test_lock_order`::

    A: UPDATE call_exports SET call_id=$1, updated_at=$2 WHERE call_exports.id = $3
    B: INSERT INTO activity_events (source, level, kind, message, call_id, meta) VALUES (…)

B is :meth:`app.exporters.worker.ExportWorker._handle`.  ``ledger.claim`` takes the
``call_exports`` row with ``FOR UPDATE SKIP LOCKED`` and holds it across the encode and the
whole HTTP upload — seconds — and only then records the outcome; the activity row's
``call_id`` foreign key makes Postgres take ``FOR KEY SHARE`` on the ``calls`` row.

A is ingest reconcile: :func:`app.db.lock_calls` on both copies of a transmission, then
:func:`app.exporters.ledger.repoint_exports` moving the loser's ledger rows to the winner —
an UPDATE that waits on the row B is holding.  While ``lock_calls`` took a plain
``FOR UPDATE``, B's foreign-key lookup waited on A in turn and the cycle was complete.

``FOR NO KEY UPDATE`` does not conflict with ``FOR KEY SHARE``, so B's insert goes through,
B commits, B releases the ledger row, and A simply waits its turn.  This module asserts the
statement the lock is actually compiled to, and then runs the two transactions against each
other in the interleaving that used to deadlock.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import event, select, text

from app.models import ActivityEvent, CallExport
from tests.conftest import requires_db
from tests.test_exporters import seed_call

pytestmark = requires_db

#: Long enough that a healthy wait for the other transaction's commit is never cut short,
#: short enough that a regression fails the test instead of hanging the suite.
LOCK_TIMEOUT_MS = 15_000


def test_lock_calls_compiles_to_for_no_key_update(session, tmp_path) -> None:  # noqa: ANN001
    """The strength itself, read off the statement the PostgreSQL dialect actually emits.

    ``with_for_update(key_share=True)`` is easy to misread as the weaker ``FOR KEY SHARE``
    (that would be ``read=True, key_share=True``), and the difference between the two is
    the difference between "reconcile still excludes the STT worker" and "it does not".  So
    the compiled text is asserted rather than the keyword argument.
    """
    from app.db import engine, lock_calls

    call = seed_call(session, root=tmp_path, file_path="20260901/EC EVENT 6/compile.mp3")
    session.commit()

    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        assert lock_calls(session, call.id) == [call.id]
    finally:
        event.remove(engine, "before_cursor_execute", before)

    lock_sql = next(s for s in statements if "FROM calls" in s and "FOR" in s)
    normalized = " ".join(lock_sql.split())
    assert normalized.endswith("FOR NO KEY UPDATE"), normalized
    assert "FOR KEY SHARE" not in normalized
    assert "ORDER BY calls.id" in normalized  # the id ordering is load-bearing too


def _no_hang(session) -> None:  # noqa: ANN001
    """Bound every lock wait in this session, so a regression fails rather than hangs."""
    session.execute(text(f"SET lock_timeout = {LOCK_TIMEOUT_MS}"))


def test_the_export_upload_and_a_reconcile_repoint_do_not_deadlock(session, tmp_path) -> None:  # noqa: ANN001
    """The two transactions, interleaved by hand in the order production hit.

    Thread 1 is the export worker: claim the ledger row for call X with ``FOR UPDATE`` and
    hold it (the upload), then write the activity row for the call.  Thread 2 is reconcile:
    once the claim is held, ``lock_calls`` both copies and repoint X's ledger rows onto Y.

    Before ``FOR NO KEY UPDATE`` this raised ``DeadlockDetected`` — as a ``psycopg`` error
    through SQLAlchemy — from whichever transaction Postgres chose as the victim, so the
    assertion is simply that neither thread saw anything.
    """
    from app.db import SessionLocal

    loser = seed_call(session, root=tmp_path, file_path="20260901/EC EVENT 6/loser.mp3")
    winner = seed_call(session, root=tmp_path, file_path="20260901/EC EVENT 6/winner.mp3")
    loser_id, winner_id = loser.id, winner.id

    export_row = CallExport(call_id=loser_id, service="broadcastify", status="pending")
    session.add(export_row)
    session.commit()
    export_id = export_row.id

    claimed = threading.Event()  # thread 1 holds the ledger row
    locked = threading.Event()  # thread 2 holds the two call rows
    errors: list[BaseException] = []

    def exporter() -> None:
        """``ledger.claim``'s lock, held across an upload, then the outcome it records."""
        from app import activity

        db = SessionLocal()
        try:
            _no_hang(db)
            held = db.execute(
                text("SELECT id, call_id FROM call_exports WHERE id = :id FOR UPDATE"),
                {"id": export_id},
            ).first()
            assert held is not None
            claimed.set()
            # The upload: seconds of HTTP with the ledger row still locked.  Here, just
            # long enough for reconcile to take the call rows it is about to wait on.
            assert locked.wait(timeout=30), "reconcile never took the call locks"
            row = activity.record(
                db,
                source="export",
                level="info",
                kind="export.uploaded",
                message="t",
                call_id=loser_id,
                meta={},
            )
            # ``activity.record`` swallows its own failures by design, so the deadlock
            # would arrive as a None here and as a poisoned transaction at commit.
            assert row is not None, "the activity insert did not land"
            db.commit()
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, from a thread
            errors.append(exc)
            claimed.set()
        finally:
            db.close()

    def reconciler() -> None:
        """``lock_calls`` on both copies, then the repoint that waits on the ledger row."""
        from app.db import lock_calls
        from app.exporters.ledger import repoint_exports

        db = SessionLocal()
        try:
            _no_hang(db)
            assert claimed.wait(timeout=30), "the exporter never claimed its row"
            lock_calls(db, [loser_id, winner_id])
            locked.set()
            # The UPDATE itself is an ORM change, so it is emitted by the flush inside
            # ``commit`` — which is where this transaction waits on the claimed row.
            repoint_exports(db, winner_id=winner_id, loser_id=loser_id)
            db.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            locked.set()
        finally:
            db.close()

    threads = [threading.Thread(target=exporter), threading.Thread(target=reconciler)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
        assert not thread.is_alive(), "a thread is still waiting on a lock"

    assert errors == [], f"the interleaving raised: {errors!r}"

    # And both transactions did their work: the ledger row moved to the winner, and the
    # exporter's activity line is on the call it uploaded.
    session.expire_all()
    assert session.get(CallExport, export_id).call_id == winner_id
    kinds = session.scalars(
        select(ActivityEvent.kind).where(ActivityEvent.call_id == loser_id)
    ).all()
    assert "export.uploaded" in kinds


@pytest.mark.parametrize("other", ["for update", "plain update"])
def test_for_no_key_update_still_excludes_the_other_calls_writers(session, tmp_path, other) -> None:  # noqa: ANN001
    """The §5.2 ordering rule is only useful if the weaker lock still serializes writers.

    A plain ``UPDATE calls`` and a ``SELECT … FOR UPDATE`` both conflict with
    ``FOR NO KEY UPDATE``, so a second transaction attempting either while ``lock_calls``
    holds the row must block.  Asserted with a 250 ms ``lock_timeout``: the wait itself is
    the evidence, and the timeout error is what proves it waited.
    """
    from psycopg.errors import LockNotAvailable
    from sqlalchemy.exc import OperationalError

    from app.db import SessionLocal, lock_calls

    call = seed_call(session, root=tmp_path, file_path="20260901/EC EVENT 6/excl.mp3")
    call_id = call.id
    session.commit()

    holder = SessionLocal()
    contender = SessionLocal()
    try:
        lock_calls(holder, [call_id])
        contender.execute(text("SET lock_timeout = 250"))
        statement = (
            "SELECT id FROM calls WHERE id = :id FOR UPDATE"
            if other == "for update"
            else "UPDATE calls SET transcript_status = 'queued' WHERE id = :id"
        )
        with pytest.raises(OperationalError) as caught:
            contender.execute(text(statement), {"id": call_id})
        assert isinstance(caught.value.orig, LockNotAvailable)
    finally:
        contender.rollback()
        contender.close()
        holder.rollback()
        holder.close()
