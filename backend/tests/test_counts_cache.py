"""The headline-count shortcuts: row estimates, the TTL cache, and what they cost.

Three separate promises are under test here, because each of them was a production
regression waiting to happen:

* :func:`app.services.counts.estimated_rows` must still be *correct* on a small table —
  a test database, a fresh deployment — or every assertion about a count in this suite
  becomes a coin flip.
* :func:`app.services.counts.cached` must actually cache, must expire, and must be
  bypassable, because the CLI passes ``force=True`` to get a real answer.
* :func:`app.ingest.service.upsert_system` must not issue an ``UPDATE`` when there is
  nothing to update.  That UPDATE took a row lock on the one ``systems`` row for the whole
  upload transaction and serialised every concurrent upload for the same system.
"""

from __future__ import annotations

import time

from sqlalchemy import event, select

from tests.conftest import requires_db

pytestmark = requires_db


# ---------------------------------------------------------------------------- estimates


def test_a_small_table_is_counted_exactly(session) -> None:  # noqa: ANN001
    """Below ``ESTIMATE_MIN_ROWS`` the answer is the truth, not ``reltuples``.

    A test database is never analyzed on any schedule a test can rely on, and an archive
    that has just been deployed has a ``reltuples`` of -1.  Both must read as "0 calls"
    rather than as "-1 calls" or as whatever the last vacuum happened to see.
    """
    from app.models import Call
    from app.services import counts

    assert counts.estimated_rows(session, Call) == 0

    from app.models import System

    session.add(System(name="Estimates"))
    session.commit()
    assert counts.estimated_rows(session, System) == 1
    # A mapped class, a Table and a bare name all name the same table.
    assert counts.estimated_rows(session, System.__table__) == 1
    assert counts.estimated_rows(session, "systems") == 1


# -------------------------------------------------------------------------- the TTL cache


def test_cached_reuses_expires_and_can_be_forced() -> None:
    from app.services import counts

    counts.invalidate()
    calls = {"n": 0}

    def compute() -> int:
        calls["n"] += 1
        return calls["n"]

    assert counts.cached("t", 60.0, compute) == 1
    assert counts.cached("t", 60.0, compute) == 1  # reused, not recomputed
    assert calls["n"] == 1

    assert counts.cached("t", 60.0, compute, force=True) == 2  # force bypasses
    assert counts.cached("t", 60.0, compute) == 2  # …and refreshes the entry

    time.sleep(0.02)
    assert counts.cached("t", 0.01, compute) == 3  # a shorter TTL expires it

    counts.invalidate("t")
    assert counts.cached("t", 60.0, compute) == 4


def test_cached_at_reports_when_the_value_was_measured() -> None:
    from app.services import counts

    counts.invalidate("when")
    assert counts.cached_at("when") is None
    before = time.time()
    counts.cached("when", 60.0, lambda: 7)
    measured = counts.cached_at("when")
    assert measured is not None
    assert before <= measured <= time.time()


def test_an_expired_entry_is_dropped_when_read_and_swept_on_any_write() -> None:
    """Expiry-on-read: a stale entry does not sit in the table until someone asks again."""
    from app.services import counts

    counts.invalidate()
    counts.cached("short", 0.01, lambda: 1)
    counts.cached("long", 60.0, lambda: 2)
    assert counts.size() == 2
    time.sleep(0.02)
    # Writing an unrelated key sweeps the expired one…
    counts.cached("other", 60.0, lambda: 3)
    assert counts.size() == 2
    assert counts.cached_at("short") is None
    # …and reading an expired key drops it before recomputing.
    counts.cached("gone", 0.01, lambda: 4)
    time.sleep(0.02)
    assert counts.cached("gone", 0.01, lambda: 5) == 5


def test_the_cache_is_bounded_and_evicts_the_least_recently_used() -> None:
    """Keys built from a request (facet filters) must not grow the table without bound."""
    from app.services import counts

    counts.invalidate()
    counts.cached("keep", 60.0, lambda: "kept")
    for n in range(counts.MAX_ENTRIES * 3):
        counts.cached(f"facets:{n}", 60.0, lambda n=n: n)
        if n % 50 == 0:
            # Reading "keep" makes it recently used, so the cap takes other keys first.
            assert counts.cached("keep", 60.0, lambda: "recomputed") == "kept"
        assert counts.size() <= counts.MAX_ENTRIES
    assert counts.size() == counts.MAX_ENTRIES
    assert counts.cached_at("facets:0") is None  # the oldest went first
    assert counts.cached_at(f"facets:{counts.MAX_ENTRIES * 3 - 1}") is not None
    assert counts.cached("keep", 60.0, lambda: "recomputed") == "kept"


def test_ingest_status_is_cached_and_force_refreshes_it(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """The whole dict is one cache entry, so the numbers in it agree with each other."""
    from app.ingest.service import ingest_file, ingest_status
    from app.services import counts

    counts.invalidate()
    assert ingest_status(session)["calls"] == 0

    ingest_file(
        session,
        fixture_audio
        / "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3",
        settings=settings,
        audio_root=fixture_audio,
    )
    session.commit()

    assert ingest_status(session)["calls"] == 0  # still the cached measurement
    fresh = ingest_status(session, force=True)
    assert fresh["calls"] == 1
    assert fresh["ledger"] == {"ingested": 1}
    assert ingest_status(session)["calls"] == 1  # force refreshed the entry


# --------------------------------------------------------------- upsert_system row locks


def _statements(callable_):  # noqa: ANN001, ANN202
    """Run ``callable_`` and return every SQL statement the engine sent."""
    from app.db import engine

    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", before)
    try:
        callable_()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_upsert_system_reads_without_writing_when_nothing_is_missing(session) -> None:  # noqa: ANN001
    """The hot path: the row exists and is complete, so no UPDATE and no row lock.

    ``ON CONFLICT DO UPDATE`` locks the conflicting row for the rest of the transaction —
    and an upload's transaction goes on to reconcile, write the audio row and queue the
    exports.  Every upload for the same system therefore queued behind the first one.  The
    fix is to look before writing, and this is the assertion that keeps it fixed.
    """
    from app.ingest.service import upsert_system

    first = upsert_system(session, "Example System", protocol="P25", wacn=0xBEE00)
    session.commit()

    seen = _statements(lambda: upsert_system(session, "Example System", protocol="P25", wacn=0xBEE00))
    joined = " ".join(seen).upper()
    assert "INSERT INTO SYSTEMS" not in joined, seen
    assert "ON CONFLICT" not in joined, joined
    assert any("SELECT" in s and "FROM systems" in s for s in seen), seen

    assert upsert_system(session, "Example System", protocol="P25", wacn=0xBEE00) == first


def test_upsert_system_still_inserts_and_still_fills_a_gap(session) -> None:  # noqa: ANN001
    """The two cases that must keep writing: absent row, and a NULL this call can fill."""
    from app.ingest.service import upsert_system
    from app.models import System

    created = _statements(lambda: upsert_system(session, "Example County", protocol=None, wacn=None))
    assert any("ON CONFLICT" in s.upper() for s in created), created
    session.commit()

    # The row exists but knows no protocol; a call that carries one must still fill it in.
    filled = _statements(lambda: upsert_system(session, "Example County", protocol="P25", wacn=None))
    assert any("ON CONFLICT" in s.upper() for s in filled), filled
    session.commit()

    row = session.execute(select(System).where(System.name == "Example County")).scalar_one()
    assert row.protocol == "P25"
    assert row.wacn is None

    # …and now that it is known, the next identical call is a read again.
    quiet = _statements(lambda: upsert_system(session, "Example County", protocol="P25", wacn=None))
    assert not any("ON CONFLICT" in s.upper() for s in quiet), quiet

    # A call carrying a WACN the row still lacks writes once more, then goes quiet.
    wacn = _statements(lambda: upsert_system(session, "Example County", protocol="P25", wacn=0xBEE00))
    assert any("ON CONFLICT" in s.upper() for s in wacn), wacn
    session.commit()
    assert (
        session.execute(select(System.wacn).where(System.name == "Example County")).scalar_one()
        == 0xBEE00
    )


def test_upsert_system_is_unchanged_end_to_end_for_an_upload(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """Two calls from the same system still land on one ``systems`` row with its fields."""
    from app.ingest.service import ingest_file
    from app.models import Call, System

    for name in (
        "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3",
        "20260829/EXAMPLE EVENT 1/20260829_030253_Example-System_Central_T-Control__TO_1701.mp3",
    ):
        ingest_file(session, fixture_audio / name, settings=settings, audio_root=fixture_audio)
    session.commit()

    systems = session.execute(select(System)).scalars().all()
    assert len(systems) == 1
    call_system_ids = set(session.execute(select(Call.system_id)).scalars().all())
    assert call_system_ids == {systems[0].id}
