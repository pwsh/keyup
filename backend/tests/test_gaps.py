"""Recorder-gap detection is *authoritative*, not append-only (``app/ingest/gaps.py``).

A gap is a hole in the recording, and a hole can be filled: the backlog migration lands the
calls that were missing and the stretch that used to be an outage is now ordinary traffic.
The upsert alone could never express that — it is keyed on ``(system_id, started_at)`` and a
filled hole produces a *different* start — so production accumulated a row spanning two
years beside dozens of shorter ones all ending at the same resuming call, every one of them
about calls that now exist.  These tests pin the deletion half: what a pass removes, and
just as importantly what it leaves alone when it only looked at part of the table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ingest.gaps import detect_gaps
from app.models import Call, RecorderGap, System

from .conftest import requires_db

pytestmark = requires_db

BASE = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _system(session, name: str = "Example System") -> System:  # noqa: ANN001
    system = session.scalars(select(System).where(System.name == name)).first()
    if system is None:
        system = System(name=name)
        session.add(system)
        session.flush()
    return system


def _call(session, system: System, started: datetime, *, duration: float = 3.0) -> Call:  # noqa: ANN001
    """One row with the columns gap detection reads, and nothing it does not."""
    call = Call(
        system_id=system.id,
        ingest_source="trunkd",
        ingest_priority="live",
        recorded_at=started + timedelta(seconds=duration),
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=duration),
        duration_s=duration,
        ingested_at=started + timedelta(seconds=duration + 1),
        file_path=f"gaps/{system.id}-{started.isoformat()}.mp3",
        file_name=f"{started.isoformat()}.mp3",
    )
    session.add(call)
    session.flush()
    return call


def _stored(session) -> list[RecorderGap]:  # noqa: ANN001
    return list(session.scalars(select(RecorderGap).order_by(RecorderGap.started_at)))


def test_a_filled_hole_stops_being_a_gap(session) -> None:  # noqa: ANN001
    """The headline case: calls arrive inside a known gap and the old row goes with it."""
    system = _system(session)
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    session.commit()

    first = detect_gaps(session, gap_minutes=30)
    session.commit()
    assert (first.gaps_found, first.gaps_new, first.gaps_removed) == (1, 1, 0)
    (gap,) = _stored(session)
    assert gap.started_at == BASE + timedelta(seconds=3)

    # The migration fills the hole to within an hour of the far end.
    for minutes in range(20, 121, 20):
        _call(session, system, BASE + timedelta(minutes=minutes))
    session.commit()

    second = detect_gaps(session, gap_minutes=30)
    session.commit()
    assert second.gaps_found == 1
    assert second.gaps_removed == 1
    (only,) = _stored(session)
    assert only.started_at == BASE + timedelta(minutes=120, seconds=3)
    assert only.ended_at == BASE + timedelta(hours=3)


def test_a_near_duplicate_of_a_detected_gap_is_collapsed(session) -> None:  # noqa: ANN001
    """Same resuming call, different start — the ghost left by a re-stamped ``ended_at``.

    It is deleted even though it starts *before* ``since``: the pass just established
    positively where the gap ending at that call begins, so the other row is wrong rather
    than merely unexamined.
    """
    system = _system(session)
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    _call(session, system, BASE + timedelta(hours=6))
    session.commit()

    ghost = RecorderGap(
        system_id=system.id,
        started_at=BASE + timedelta(hours=1),  # the whole-hole row production kept
        ended_at=BASE + timedelta(hours=6),  # …ending at the same resuming call
        duration_s=5 * 3600,
    )
    session.add(ghost)
    session.commit()

    summary = detect_gaps(session, gap_minutes=30, since=BASE + timedelta(hours=2))
    session.commit()

    assert summary.gaps_found == 1
    assert summary.gaps_removed == 1
    (only,) = _stored(session)
    assert only.started_at == BASE + timedelta(hours=3, seconds=3)
    assert only.ended_at == BASE + timedelta(hours=6)


def test_since_leaves_a_gap_the_window_could_not_have_re_detected(session) -> None:  # noqa: ANN001
    """A row starting before the scanned window is evidence, not stale evidence."""
    system = _system(session)
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    session.commit()

    old = RecorderGap(
        system_id=system.id,
        started_at=BASE - timedelta(days=30),
        ended_at=BASE - timedelta(days=29),
        duration_s=86400,
    )
    session.add(old)
    session.commit()

    summary = detect_gaps(session, gap_minutes=30, since=BASE - timedelta(hours=1))
    session.commit()

    assert summary.gaps_removed == 0
    assert {gap.started_at for gap in _stored(session)} == {
        BASE - timedelta(days=30),
        BASE + timedelta(seconds=3),
    }


def test_a_scheduled_pass_reconciles_the_whole_table(session) -> None:  # noqa: ANN001
    """No ``since`` and no ``system_id`` is the worker's hourly call — it cleans everything."""
    system = _system(session)
    other = _system(session, "Example County")
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    session.commit()

    for owner, start in ((system, BASE - timedelta(days=400)), (other, BASE - timedelta(days=2))):
        session.add(
            RecorderGap(
                system_id=owner.id,
                started_at=start,
                ended_at=start + timedelta(hours=1),
                duration_s=3600,
            )
        )
    session.commit()

    summary = detect_gaps(session, gap_minutes=30)
    session.commit()

    assert summary.gaps_removed == 2
    (only,) = _stored(session)
    assert only.system_id == system.id
    assert only.started_at == BASE + timedelta(seconds=3)


def test_a_system_scoped_pass_never_touches_another_system(session) -> None:  # noqa: ANN001
    """``system_id`` bounds the deletion exactly as it bounds the detection."""
    system = _system(session)
    other = _system(session, "Example County")
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    session.commit()

    session.add(
        RecorderGap(
            system_id=other.id,
            started_at=BASE - timedelta(days=2),
            ended_at=BASE - timedelta(days=2) + timedelta(hours=1),
            duration_s=3600,
        )
    )
    session.commit()

    summary = detect_gaps(session, gap_minutes=30, system_id=system.id)
    session.commit()

    assert summary.gaps_removed == 0
    assert {gap.system_id for gap in _stored(session)} == {system.id, other.id}


def test_a_repeat_pass_removes_nothing(session) -> None:  # noqa: ANN001
    """Idempotence still holds: the second run refreshes the same rows and deletes none."""
    system = _system(session)
    _call(session, system, BASE)
    _call(session, system, BASE + timedelta(hours=3))
    session.commit()

    detect_gaps(session, gap_minutes=30)
    session.commit()
    again = detect_gaps(session, gap_minutes=30)
    session.commit()

    assert (again.gaps_found, again.gaps_new, again.gaps_removed) == (1, 0, 0)
    assert len(_stored(session)) == 1


def test_a_call_hidden_as_a_duplicate_does_not_close_a_gap(session) -> None:  # noqa: ANN001
    """The SQL ignores ``duplicate_of`` rows, and pruning must not smuggle them back in."""
    system = _system(session)
    _call(session, system, BASE)
    primary = _call(session, system, BASE + timedelta(hours=3))
    hidden = _call(session, system, BASE + timedelta(minutes=90))
    hidden.duplicate_of = primary.id
    session.commit()

    summary = detect_gaps(session, gap_minutes=30)
    session.commit()

    assert summary.gaps_found == 1
    (only,) = _stored(session)
    assert only.started_at == BASE + timedelta(seconds=3)
    assert float(only.duration_s) == pytest.approx(3 * 3600 - 3, abs=1)


def test_the_hourly_pass_looks_back_a_week_and_the_nightly_pass_at_everything(session, settings) -> None:  # noqa: ANN001
    """The worker's two passes.  The hourly one reads only the recent window, so a
    stored gap older than it — here one a backlog upload has since filled — is left for the
    nightly whole-table pass on ``CRON_GAPS``, which removes it."""
    from app.ingest.gaps import RECENT_WINDOW
    from app.worker import Worker

    system = _system(session)
    now = datetime.now(UTC).replace(microsecond=0)
    _call(session, system, now - timedelta(hours=3))
    _call(session, system, now - timedelta(hours=1))
    stale_start = now - RECENT_WINDOW - timedelta(days=30)
    session.add(
        RecorderGap(
            system_id=system.id,
            started_at=stale_start,
            ended_at=stale_start + timedelta(hours=1),
            duration_s=3600,
        )
    )
    session.commit()
    worker = Worker(settings)

    worker.run_gaps()
    session.expire_all()
    assert {gap.started_at for gap in _stored(session)} == {
        stale_start,
        now - timedelta(hours=3) + timedelta(seconds=3),
    }

    worker.run_gaps_full()
    session.expire_all()
    assert {gap.started_at for gap in _stored(session)} == {now - timedelta(hours=3) + timedelta(seconds=3)}
