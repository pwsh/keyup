"""The third deadlock pair: the directory rows, taken in two different orders (§5.2b).

Production deadlocked three times in twelve hours — once failing a ``POST /api/call-upload``
the recorder had to retry, twice failing ``POST /api/recorder/events`` — always on the same
two statements, and always with the same ``CONTEXT``::

    A: INSERT INTO radios (…) ON CONFLICT … DO UPDATE …        while inserting index tuple
    B: INSERT INTO talkgroups (…) ON CONFLICT uq_talkgroups_system_tgid DO UPDATE …

Neither path was wrong on its own.  ``POST /api/call-upload`` upserted the call's talkgroup
and then its radio; ``POST /api/recorder/events`` resolved each event's radio and then its
talkgroup, for a whole batch in one transaction.  One radio affiliating to one talkgroup is
therefore two rows taken in opposite orders by two connections, which is the only recipe
Postgres has no way out of.

:mod:`app.db` §5.2b fixes it by giving both paths one order — talkgroups, then radios, then
affiliations, ascending within a kind — so this module runs the two of them against each
other in the interleaving that used to deadlock and asserts that both commit.  The barrier
below is what makes the interleaving deterministic: each transaction stops after its *first*
directory row and waits for the other to take its own before going on.  With the two orders
that is a cycle; with one order the second transaction is already waiting on the first, the
barrier simply times out, and both finish.
"""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, select, text

from app.models import Radio, RadioAffiliation, RadioEvent, Site, System, Talkgroup
from tests.conftest import requires_db
from tests.test_parser import SHORTEST

pytestmark = requires_db

#: ``SHORTEST`` is ``…_TO_1701_FROM_9200006.mp3`` — one talkgroup and one radio, which is
#: exactly the pair a ``group_affiliation`` event names.
TGID = 1701
RADIO_ID = 9200006
SYSTEM_NAME = "Example System"
SITE_NAME = "Central"
NOW = datetime(2026, 9, 1, 20, 9, 45, tzinfo=UTC)

#: Long enough that a healthy wait for the other transaction is never cut short, short
#: enough that a regression fails the suite instead of hanging it.
LOCK_TIMEOUT_MS = 15_000
#: How long a transaction waits at the barrier for the other one to take its first row.
#: Once the order is one order the second transaction is *blocked* on the first and can
#: never arrive, so this timing out is the fixed behaviour, not a failure.
BARRIER_SECONDS = 3.0

_local = threading.local()


def _hook(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
    """After this transaction's first ``radios``/``talkgroups`` write, wait for the other.

    Armed per thread, and fired once: what matters is that both transactions are holding
    one directory row of some kind before either reaches for the second.
    """
    barrier = getattr(_local, "barrier", None)
    if barrier is None or barrier["fired"]:
        return
    if "INSERT INTO radios" not in statement and "INSERT INTO talkgroups" not in statement:
        return
    barrier["fired"] = True
    barrier["first"] = "radios" if "INSERT INTO radios" in statement else "talkgroups"
    barrier["mine"].set()
    barrier["theirs"].wait(timeout=BARRIER_SECONDS)


def _seed(session) -> None:  # noqa: ANN001
    """A complete, committed directory: the two paths must contend on *these* rows only.

    Every column either path would otherwise fill is set here, so neither transaction
    writes ``systems`` or ``sites`` on its way past them.  ``upsert_system`` short-circuits
    to a SELECT when the row is complete and ``upsert_site`` is an ``ON CONFLICT DO
    NOTHING``, so nothing upstream of the talkgroup takes a lock the other side wants.
    """
    system = System(
        name=SYSTEM_NAME, protocol="P25 Phase 1", wacn=101010, system_id=52, nac=52
    )
    session.add(system)
    session.flush()
    session.add(
        Site(
            system_id=system.id,
            name=SITE_NAME,
            site_number=1,
            rfss=10,
            lra=10,
            nac=52,
        )
    )
    session.add(
        Talkgroup(
            system_id=system.id,
            tgid=TGID,
            first_seen_at=NOW,
            last_seen_at=NOW,
            call_count=0,
        )
    )
    session.add(
        Radio(
            system_id=system.id,
            radio_id=RADIO_ID,
            wacn=0,
            home_system_id=0,
            kind="unknown",
            first_seen_at=NOW,
            last_seen_at=NOW,
            call_count=0,
        )
    )
    session.commit()


def _batch() -> dict:
    """One radio-events batch: the radio registers, then affiliates to the talkgroup."""
    return {
        "recorder": "trunkd",
        "recorder_version": "0.0.1",
        "instance": "recorder-a",
        "system": {"label": SYSTEM_NAME, "wacn": 101010, "system_id": 52, "nac": 52},
        "site": {"label": SITE_NAME, "rfss": 10, "site": 1, "lra": 10},
        "events": [
            {
                "at": NOW.isoformat(),
                "kind": "unit_registration",
                "opcode": 44,
                "radio_id": RADIO_ID,
                "talkgroup": None,
                "result": "accept",
            },
            {
                "at": (NOW + timedelta(seconds=2)).isoformat(),
                "kind": "group_affiliation",
                "opcode": 40,
                "radio_id": RADIO_ID,
                "talkgroup": TGID,
                "result": "accept",
            },
        ],
    }


def _stage_recording(tmp_path: Path, fixture_audio: Path) -> tuple[Path, Path]:
    """``SHORTEST`` copied into a tree of its own, so ingest has a real file to read."""
    root = tmp_path / "audio"
    target = root / SHORTEST
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture_audio / SHORTEST, target)
    return root, target


def test_an_events_batch_and_a_call_do_not_deadlock_on_the_directory(  # noqa: ANN201
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
    fixture_audio,  # noqa: ANN001
):
    """``POST /api/recorder/events`` against the file-ingest path, on one radio and one
    talkgroup, each holding its first row before it asks for its second.

    Before §5.2b this raised ``DeadlockDetected`` — through ``psycopg`` — in whichever
    transaction Postgres chose as the victim: for the events thread as a raised error, for
    the ingest thread as a ``failed`` result with the deadlock message in the ledger.  Both
    are asserted, because the two paths report a failure very differently.
    """
    from app.db import SessionLocal, engine

    _seed(session)
    root, recording = _stage_recording(tmp_path, fixture_audio)

    events_first = threading.Event()
    call_first = threading.Event()
    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}

    def run_events() -> None:
        from app.ingest.events import record_events

        _local.barrier = {"mine": events_first, "theirs": call_first, "fired": False}
        db = SessionLocal()
        try:
            db.execute(text(f"SET lock_timeout = {LOCK_TIMEOUT_MS}"))
            outcomes["events"] = record_events(db, _batch(), settings=settings)
            db.commit()
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, from a thread
            errors.append(exc)
        finally:
            events_first.set()  # never leave the other thread waiting on a dead one
            db.rollback()
            db.close()
            _local.barrier = None

    def run_call() -> None:
        from app.ingest.service import ingest_file

        _local.barrier = {"mine": call_first, "theirs": events_first, "fired": False}
        db = SessionLocal()
        try:
            db.execute(text(f"SET lock_timeout = {LOCK_TIMEOUT_MS}"))
            outcomes["call"] = ingest_file(
                db, recording, settings=settings, audio_root=root
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            call_first.set()
            db.rollback()
            db.close()
            _local.barrier = None

    event.listen(engine, "after_cursor_execute", _hook)
    try:
        threads = [
            threading.Thread(target=run_events, name="events"),
            threading.Thread(target=run_call, name="call"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
            assert not thread.is_alive(), f"{thread.name} is still waiting on a lock"
    finally:
        event.remove(engine, "after_cursor_execute", _hook)

    assert errors == [], f"the interleaving raised: {errors!r}"

    ingested = outcomes["call"]
    assert ingested.status == "ingested", f"the call did not land: {ingested!r}"
    batch_result = outcomes["events"]
    assert batch_result.stored == 2, batch_result
    assert batch_result.rejected == 0, batch_result.errors

    # And both transactions really did their work on the contended rows.
    session.expire_all()
    talkgroup = session.scalars(select(Talkgroup).where(Talkgroup.tgid == TGID)).one()
    radio = session.scalars(select(Radio).where(Radio.radio_id == RADIO_ID)).one()
    assert talkgroup.call_count == 1  # the call counted; the two events did not
    assert radio.call_count == 1
    assert session.scalar(select(RadioEvent.id).limit(1)) is not None
    affiliation = session.scalars(
        select(RadioAffiliation).where(
            RadioAffiliation.radio_id_fk == radio.id,
            RadioAffiliation.talkgroup_id_fk == talkgroup.id,
        )
    ).one()
    # The call path wrote the "heard" half, the events path raised the source to
    # 'affiliation' — whichever of them committed second.
    assert affiliation.source == "affiliation"
    assert affiliation.last_affiliated_at is not None


def test_both_directory_paths_take_the_talkgroup_first(  # noqa: ANN201
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
    fixture_audio,  # noqa: ANN001
):
    """The rule itself, read off the statements each path actually emits.

    The test above would still pass if both paths took the *radio* first — any single order
    is deadlock-free.  This one pins the order §5.2b chose, which is the one the exporters
    and the reconcile paths were already written against.
    """
    from app.db import engine
    from app.ingest.events import record_events
    from app.ingest.service import ingest_file

    _seed(session)
    root, recording = _stage_recording(tmp_path, fixture_audio)

    seen: list[str] = []

    def watch(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        for table in ("radios", "talkgroups", "radio_affiliations"):
            if f"INSERT INTO {table}" in statement:
                seen.append(table)

    event.listen(engine, "after_cursor_execute", watch)
    try:
        record_events(session, _batch(), settings=settings)
        session.commit()
        batch_order = list(seen)
        seen.clear()
        ingest_file(session, recording, settings=settings, audio_root=root)
        call_order = list(seen)
    finally:
        event.remove(engine, "after_cursor_execute", watch)

    for name, order in (("events", batch_order), ("call", call_order)):
        assert "talkgroups" in order and "radios" in order, (name, order)
        assert order.index("talkgroups") < order.index("radios"), (name, order)
        if "radio_affiliations" in order:
            assert order.index("radios") < order.index("radio_affiliations"), (name, order)
