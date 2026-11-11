"""The far-hash duplicate bug and its repair — ``app.repair_hash_duplicates``.

Reconciliation's exact rung read an identical ``sha256`` as proof of the same transmission
whatever the time.  Identical bytes only prove the same *recording*: a short recording with
no voice in it — a squelch tail, 0.14-2 s — encodes to byte-identical MP3 bytes every time,
so one hash spreads across hundreds of unrelated calls.  A 1.7 s unit call was ruled a
duplicate of a 1.7 s group call on another talkgroup a week earlier; hundreds of rows, one
hash covering over a hundred of them.

The first three tests are the rung; the rest are the repair that frees the rows already
written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.ingest.reconcile import EXACT_HASH_MAX_SKEW_S, ReconcileConfig, find_match
from app.models import Call, Conversation, TranscriptionJob
from app.repair_hash_duplicates import FarHashRepairReport, repair_far_hash_duplicates
from tests.conftest import requires_db
from tests.test_reconcile import BASE, _call

pytestmark = requires_db

#: the hash a voiceless 1.7 s recording produces every time it is encoded
SQUELCH = "b" * 64


# --------------------------------------------------------------------------------------
# the rung
# --------------------------------------------------------------------------------------


def test_an_identical_hash_from_another_day_is_not_a_match(session, settings) -> None:  # noqa: ANN001
    """The regression: the 2026-08-31 group call and the 2026-09-08 unit call.

    Same bytes, eight days and a different talkgroup apart — nothing else about them says
    one transmission, and before the fix the hash alone ruled them one.
    """
    _call(
        session,
        started_at=BASE,
        duration=1.7,
        tgids=(2,),
        sha256=SQUELCH,
        path="synthetic/squelch-old.mp3",
    )
    later = _call(
        session,
        started_at=BASE + timedelta(days=8),
        duration=1.7,
        tgids=(1101,),
        frequency_hz=851000000,
        sha256=SQUELCH,
        path="synthetic/squelch-new.mp3",
    )
    session.commit()

    other, result = find_match(session, later, ReconcileConfig.of(settings))

    assert other is None
    assert result.matched is False
    assert result.rule != "identical sha256"


def test_a_far_hash_falls_through_to_the_interval_rung(session, settings) -> None:  # noqa: ANN001
    """Skipped exactly like an excluded hit: the scan continues and the interval rung
    still gets to answer, so a real overlapping copy is found rather than the far one."""
    far = _call(
        session,
        started_at=BASE - timedelta(days=3),
        duration=1.7,
        tgids=(2,),
        sha256=SQUELCH,
        path="synthetic/far.mp3",
    )
    overlapping = _call(
        session,
        started_at=BASE,
        duration=8.0,
        ingest_source="file",
        path="synthetic/overlapping.mp3",
    )
    newcomer = _call(
        session,
        started_at=BASE + timedelta(seconds=0.5),
        duration=8.0,
        ingest_source="trunkd",
        time_source="upload",
        sha256=SQUELCH,
        path="synthetic/newcomer.mp3",
    )
    session.commit()

    other, result = find_match(session, newcomer, ReconcileConfig.of(settings))

    assert other is not None
    assert other.id == overlapping.id, "the far hash hit should not have won"
    assert other.id != far.id
    assert result.rule == "interval overlap"


def test_an_identical_hash_inside_the_window_still_matches(session, settings) -> None:  # noqa: ANN001
    """Two recorders' copies of one key-up differ by a second or two of clock skew, and a
    retransmitted file carries its original timestamp.  Both stay inside the window."""
    first = _call(
        session,
        started_at=BASE,
        duration=1.7,
        tgids=(2,),
        sha256=SQUELCH,
        path="synthetic/near-a.mp3",
    )
    second = _call(
        session,
        started_at=BASE + timedelta(seconds=EXACT_HASH_MAX_SKEW_S - 5),
        duration=1.7,
        # Same talkgroup as the first copy: the hash rung also requires the same
        # system, kind and channel, because voiceless audio encodes to identical bytes on
        # any talkgroup (tests/test_reconcile.py covers the cross-talkgroup refusal).
        tgids=(2,),
        sha256=SQUELCH,
        path="synthetic/near-b.mp3",
    )
    session.commit()

    other, result = find_match(session, second, ReconcileConfig.of(settings))

    assert other is not None
    assert other.id == first.id
    assert result.rule == "identical sha256"


# --------------------------------------------------------------------------------------
# the repair
# --------------------------------------------------------------------------------------


def _hash_duplicate(session, *, gap_s: float, **kwargs) -> tuple[Call, Call]:  # noqa: ANN001, ANN003
    """A primary and a row hidden behind it on ``identical sha256``, ``gap_s`` apart —
    in exactly the state the bug left behind: out of its conversation, its pending
    transcript cancelled to ``skipped``."""
    started = kwargs.pop("started_at", BASE)
    path = kwargs.pop("path", "synthetic/hashdup")
    primary = _call(session, started_at=started, duration=1.7, sha256=SQUELCH, path=f"{path}-p.mp3", **kwargs)
    copy = _call(
        session,
        started_at=started + timedelta(seconds=gap_s),
        duration=1.7,
        sha256=SQUELCH,
        transcript_status="skipped",
        path=f"{path}-d.mp3",
        **kwargs,
    )
    copy.duplicate_of = primary.id
    copy.conversation_id = None
    copy.reconciled_at = datetime.now(UTC)
    copy.reconcile_reason = {
        "rule": "tie",
        "winner_id": primary.id,
        "match": {"rule": "identical sha256", "key": f"sha256:{SQUELCH[:12]}"},
    }
    session.flush()
    return primary, copy


def test_the_repair_restores_a_far_hash_duplicate(session, settings) -> None:  # noqa: ANN001
    primary, copy = _hash_duplicate(
        session, gap_s=8 * 24 * 3600, ingest_source="trunkd", time_source="upload"
    )
    session.commit()

    report = repair_far_hash_duplicates(session, settings)

    assert isinstance(report, FarHashRepairReport)
    assert (report.scanned, report.restored) == (1, 1)
    assert report.conversations_reattached == 1
    assert report.transcriptions_requeued == 1
    assert report.commits == 1

    session.refresh(copy)
    assert copy.duplicate_of is None
    assert copy.reconciled_at is None
    assert copy.reconcile_reason["rule"] == "repair:far-hash-duplicate"
    assert copy.reconcile_reason["previous"]["rule"] == "tie"
    assert copy.reconcile_reason["at"]

    # Back in a thread of its own, counted once, and back in the queue.
    assert copy.conversation_id is not None
    conv = session.get(Conversation, copy.conversation_id)
    assert conv.call_count == 1
    assert copy.transcript_status == "queued"
    job = session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == copy.id)).one()
    assert job.status == "queued"

    session.refresh(primary)
    assert primary.duplicate_of is None


def test_the_repair_leaves_a_near_hash_duplicate_alone(session, settings) -> None:  # noqa: ANN001
    """Two copies of one transmission a couple of seconds apart are a *correct* ruling."""
    _primary, copy = _hash_duplicate(session, gap_s=2.0, ingest_source="trunkd", time_source="upload")
    session.commit()

    report = repair_far_hash_duplicates(session, settings)

    assert report.as_dict()["scanned"] == 0
    session.refresh(copy)
    assert copy.duplicate_of is not None
    assert copy.transcript_status == "skipped"


def test_the_repair_is_idempotent(session, settings) -> None:  # noqa: ANN001
    _primary, copy = _hash_duplicate(
        session, gap_s=8 * 24 * 3600, ingest_source="trunkd", time_source="upload"
    )
    session.commit()

    repair_far_hash_duplicates(session, settings)
    second = repair_far_hash_duplicates(session, settings)

    assert second.as_dict() == {
        "scanned": 0,
        "restored": 0,
        "conversations_reattached": 0,
        "transcriptions_requeued": 0,
        "commits": 0,
        "dry_run": False,
    }
    session.refresh(copy)
    conv = session.get(Conversation, copy.conversation_id)
    assert conv.call_count == 1, "a second run must not count the call into its thread twice"


def test_a_dry_run_counts_and_writes_nothing(session, settings) -> None:  # noqa: ANN001
    _primary, copy = _hash_duplicate(
        session, gap_s=8 * 24 * 3600, ingest_source="trunkd", time_source="upload"
    )
    session.commit()

    report = repair_far_hash_duplicates(session, settings, dry_run=True)

    assert report.dry_run is True
    assert (report.scanned, report.restored) == (1, 1)
    assert report.commits == 0
    session.rollback()
    session.refresh(copy)
    assert copy.duplicate_of is not None
    assert copy.transcript_status == "skipped"
