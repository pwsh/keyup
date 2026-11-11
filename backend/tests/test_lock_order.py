"""The one lock order for ``calls`` and ``transcription_jobs`` (§5.2).

Production deadlocked 27 times in a day on a single pair of statements: the STT worker's
claim cancelled the job of a call the audio pass had found no speech in and *then* wrote
``calls``, while the audio pass wrote ``calls`` and *then* cancelled the job.  Postgres
resolves that the only way it can, and a dozen of those surfaced as a failed
``POST /api/call-upload`` the recorder had to spool and retry.

The rule is **the call row first, then the job row** (:func:`app.db.lock_calls`).  It is
asserted two ways here: by reading the statements each path actually emits, per
transaction, and by running the two paths against the same calls in two threads.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.models import Call, TranscriptionJob
from tests.conftest import requires_db
from tests.test_stt_worker import FakeRouter, make_result, seed_call

pytestmark = requires_db


# --------------------------------------------------------------------------------------
# reading the statements
# --------------------------------------------------------------------------------------

def _is_calls_lock(sql: str) -> bool:
    """An ``UPDATE calls``, or a ``SELECT … FROM calls`` that takes a row lock.

    Both strengths count.  :func:`app.db.lock_calls` takes ``FOR NO KEY UPDATE`` rather
    than ``FOR UPDATE`` (see its own note, and :mod:`tests.test_lock_export_deadlock`) —
    weaker only against the ``FOR KEY SHARE`` a foreign key takes, and still mutually
    exclusive with every writer of ``calls``, which is all §5.2 needs.
    """
    lowered = " ".join(sql.lower().split())
    if lowered.startswith("update calls"):
        return True
    locking = "for update" in lowered or "for no key update" in lowered
    return "from calls" in lowered and locking


def _is_job_write(sql: str) -> bool:
    lowered = " ".join(sql.lower().split())
    if "transcription_jobs" not in lowered:
        return False
    return lowered.startswith(("update", "insert", "delete", "with")) or "for update" in lowered


@contextmanager
def sql_log():
    """Every statement this process runs, with the transaction boundaries marked."""
    from app.db import engine

    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        statements.append(statement)

    def boundary(conn) -> None:  # noqa: ANN001
        statements.append("-- END --")

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "commit", boundary)
    event.listen(engine, "rollback", boundary)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "commit", boundary)
        event.remove(engine, "rollback", boundary)


def assert_calls_first(statements: list[str]) -> None:
    """No transaction may write a job row before it has locked the call row."""
    transaction: list[str] = []
    for sql in [*statements, "-- END --"]:
        if sql == "-- END --":
            locked = next((i for i, s in enumerate(transaction) if _is_calls_lock(s)), None)
            wrote = next((i for i, s in enumerate(transaction) if _is_job_write(s)), None)
            if locked is not None and wrote is not None:
                assert locked < wrote, (
                    "transcription_jobs was written before the calls row was locked:\n"
                    + "\n".join(f"  {i}: {' '.join(s.split())[:110]}" for i, s in enumerate(transaction))
                )
            transaction = []
            continue
        transaction.append(sql)


# --------------------------------------------------------------------------------------
# the two racing paths, read statement by statement
# --------------------------------------------------------------------------------------


@pytest.fixture
def worker(session, settings, tmp_path):  # noqa: ANN001, ANN201
    from app.db import SessionLocal
    from app.stt.worker import SttWorker

    return SttWorker(
        router=FakeRouter(make_result()),
        settings=settings,
        session_factory=SessionLocal,
        worker_name="lock-order",
        audio_root=tmp_path,
    )


def test_the_stt_claim_locks_the_call_before_it_cancels_the_job(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job = seed_call(session, root=tmp_path)
    call.audio_meta = {"content": "none", "speech_s": 0.02}
    session.commit()

    with sql_log() as statements:
        assert worker.claim() is None

    assert_calls_first(statements)
    session.expire_all()
    assert session.get(TranscriptionJob, job.id).status == "cancelled"
    assert session.get(Call, call.id).transcript_status == "skipped"


def test_the_claim_of_a_job_with_audio_locks_the_call_first_too(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job = seed_call(session, root=tmp_path)

    with sql_log() as statements:
        claimed = worker.claim()

    assert claimed is not None and claimed.id == job.id
    assert_calls_first(statements)


def test_the_audio_pass_locks_the_call_before_it_cancels_the_job(session, tmp_path) -> None:  # noqa: ANN001
    from app.audioproc.jobs import empty_clip_meta
    from app.db import SessionLocal

    call, job = seed_call(session, root=tmp_path)

    other = SessionLocal()
    try:
        with sql_log() as statements:
            empty_clip_meta(other, other.get(Call, call.id), "no frames")
            other.commit()
    finally:
        other.close()

    assert_calls_first(statements)
    session.expire_all()
    assert session.get(TranscriptionJob, job.id).status == "cancelled"


def test_a_job_the_audio_pass_already_cancelled_is_left_alone(session, worker, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The claim re-reads the job under the call's lock, and does nothing to a dead one.

    The two paths still race — the audio pass can cancel a job in the instant between the
    claim statement and the lock — and when it wins there is nothing left for the claim to
    say: the row is finished, with the reason the pass wrote on it.
    """
    call, job = seed_call(session, root=tmp_path)

    from app.stt import worker as worker_module

    original = worker_module.lock_calls

    def cancel_then_lock(db, ids):  # noqa: ANN001, ANN202
        session.execute(
            TranscriptionJob.__table__.update()
            .where(TranscriptionJob.id == job.id)
            .values(status="cancelled", last_error="no speech", finished_at=datetime.now(UTC))
        )
        session.commit()
        return original(db, ids)

    monkeypatch.setattr(worker_module, "lock_calls", cancel_then_lock)
    assert worker.claim() is None

    session.expire_all()
    row = session.get(TranscriptionJob, job.id)
    assert (row.status, row.last_error) == ("cancelled", "no speech")


# --------------------------------------------------------------------------------------
# the two paths at once
# --------------------------------------------------------------------------------------


def test_the_audio_pass_and_the_stt_claim_do_not_deadlock(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Both paths over the same calls, in lockstep, in two threads.

    Each thread holds its first row lock while the other takes its own — the shape that
    deadlocked in production — and the assertion is simply that nothing raises:
    ``DeadlockDetected`` arrives as a ``psycopg`` error through SQLAlchemy, so any
    exception at all fails this.
    """
    from app.audioproc.jobs import empty_clip_meta
    from app.db import SessionLocal

    rounds = 8
    calls = []
    for i in range(rounds):
        call, _ = seed_call(
            session,
            root=tmp_path,
            file_path=f"20260829/EC EVENT 6/lock-{i}.mp3",
            scheduled_at=datetime.now(UTC) - timedelta(seconds=rounds - i),
        )
        call.audio_meta = {"content": "none", "speech_s": 0.01}
        calls.append(call.id)
    session.commit()

    gate = threading.Barrier(2, timeout=20)
    errors: list[BaseException] = []

    def audio_side() -> None:
        db = SessionLocal()
        try:
            for call_id in calls:
                gate.wait()
                row = db.get(Call, call_id)
                if row is not None:
                    empty_clip_meta(db, row, "no frames")
                db.commit()
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, from a thread
            errors.append(exc)
            gate.abort()
        finally:
            db.close()

    def stt_side() -> None:
        try:
            for _ in calls:
                gate.wait()
                worker.claim()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            gate.abort()

    threads = [threading.Thread(target=audio_side), threading.Thread(target=stt_side)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a thread is still waiting on a lock"

    assert errors == []
    session.expire_all()
    open_jobs = session.scalars(
        select(TranscriptionJob.id).where(TranscriptionJob.status.in_(("queued", "running")))
    ).all()
    assert open_jobs == []
