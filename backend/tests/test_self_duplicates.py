"""The self-duplicate bug and its repair — ``app.repair_self_duplicates``.

Reconciliation's exact-hash rung applied its exclusion list to the row it *found* and not
to the primary that row resolves to, so a call whose bytes were stored twice matched its
own copy, followed it home, tied with itself, and was written ``duplicate_of = <its own
id>``.  18,708 rows on production, every one hidden from every listing.  The first two
tests here are the bug; the rest are the repair that frees the rows already written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.ingest.reconcile import reconcile_batch, reconcile_pair
from app.models import Call, Conversation, Talkgroup, TranscriptionJob
from app.repair_self_duplicates import SelfDupRepairReport, repair_self_duplicates
from tests.conftest import requires_db
from tests.test_reconcile import BASE, _call

pytestmark = requires_db

HASH = "a" * 64


# --------------------------------------------------------------------------------------
# the bug
# --------------------------------------------------------------------------------------


def test_a_calls_own_copy_never_makes_it_a_duplicate_of_itself(session, settings) -> None:  # noqa: ANN001
    """The regression.  A primary ``A`` with a resolved copy ``B`` of the same bytes.

    The batch pass walks primaries, so it sees ``A`` and asks for its match with ``A``
    excluded.  The hash query dutifully skips ``A`` and returns ``B`` — and ``B`` points at
    ``A``, so following it home hands ``reconcile_pair`` the pair ``(A, A)``.  Before the
    fix this test ends with ``A.duplicate_of == A.id``.
    """
    primary = _call(
        session, ingest_source="trunkd", time_source="upload", sha256=HASH, path="synthetic/self-a.mp3"
    )
    copy = _call(session, ingest_source="file", sha256=HASH, path="synthetic/self-b.mp3")
    copy.duplicate_of = primary.id
    session.commit()

    report = reconcile_batch(session, settings)
    session.commit()

    session.refresh(primary)
    session.refresh(copy)
    assert primary.duplicate_of is None, "a call was ruled a duplicate of itself"
    assert copy.duplicate_of == primary.id
    assert report.pairs == 0


def test_reconciling_a_row_against_itself_is_a_no_op(session, settings) -> None:  # noqa: ANN001
    """The guard at the write itself, for whatever else might ever ask the question."""
    call = _call(session, ingest_source="trunkd", time_source="upload")
    session.commit()

    outcome = reconcile_pair(session, call, call, settings)

    assert outcome.matched is False
    assert outcome.duplicate_id is None
    assert outcome.rule == "self"
    session.refresh(call)
    assert call.duplicate_of is None
    assert call.reconciled_at is None


# --------------------------------------------------------------------------------------
# the repair
# --------------------------------------------------------------------------------------


def _self_duplicate(session, **kwargs) -> Call:  # noqa: ANN001, ANN003
    """A row in exactly the state the bug left behind: hidden behind its own id, out of
    its conversation, its pending transcript cancelled to ``skipped``."""
    call = _call(session, transcript_status="skipped", **kwargs)
    session.flush()
    call.duplicate_of = call.id
    call.conversation_id = None
    call.reconciled_at = datetime.now(UTC)
    call.reconcile_reason = {
        "rule": "tie",
        "winner_id": call.id,
        "match": {"matched": True, "rule": "identical sha256"},
        "scores": {str(call.id): {"total": 3.0}},
    }
    session.flush()
    return call


def test_the_repair_restores_a_self_duplicate(session, settings) -> None:  # noqa: ANN001
    call = _self_duplicate(session, ingest_source="trunkd", time_source="upload", sha256=HASH)
    session.commit()

    report = repair_self_duplicates(session, settings)

    assert isinstance(report, SelfDupRepairReport)
    assert (report.scanned, report.restored) == (1, 1)
    assert report.conversations_reattached == 1
    assert report.transcriptions_requeued == 1
    assert report.commits == 1

    session.refresh(call)
    assert call.duplicate_of is None
    assert call.reconciled_at is None
    assert call.reconcile_reason["rule"] == "repair:self-duplicate"
    # The bogus ruling is kept, because it is the evidence this was the bug.
    assert call.reconcile_reason["previous"]["rule"] == "tie"
    assert call.reconcile_reason["at"]

    # Back in a thread, counted once.
    assert call.conversation_id is not None
    conv = session.get(Conversation, call.conversation_id)
    assert conv.call_count == 1
    assert conv.first_call_id == call.id

    # And back in the queue.
    assert call.transcript_status == "queued"
    job = session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == call.id)).one()
    assert job.status == "queued"


def test_the_repair_is_idempotent(session, settings) -> None:  # noqa: ANN001
    call = _self_duplicate(session, ingest_source="trunkd", time_source="upload", sha256=HASH)
    session.commit()

    repair_self_duplicates(session, settings)
    second = repair_self_duplicates(session, settings)

    assert second.as_dict() == {
        "scanned": 0,
        "restored": 0,
        "conversations_reattached": 0,
        "transcriptions_requeued": 0,
        "commits": 0,
        "dry_run": False,
    }
    session.refresh(call)
    conv = session.get(Conversation, call.conversation_id)
    assert conv.call_count == 1, "a second run must not count the call into its thread twice"
    assert (
        session.scalar(select(TranscriptionJob.id).where(TranscriptionJob.call_id == call.id).limit(1))
        is not None
    )


def test_the_repair_requeues_the_backlog_behind_the_live_feed(session, settings) -> None:  # noqa: ANN001
    """A 2022 recording must not re-transcribe in front of today's traffic."""
    live = _self_duplicate(session, ingest_source="trunkd", time_source="upload", path="synthetic/live.mp3")
    backlog = _self_duplicate(
        session,
        ingest_source="file",
        started_at=BASE + timedelta(minutes=5),
        path="synthetic/backlog.mp3",
    )
    backlog.ingest_priority = "backlog"
    session.commit()

    repair_self_duplicates(session, settings)

    talkgroup_priority = session.scalar(select(Talkgroup.priority).where(Talkgroup.id == live.talkgroup_id))
    priorities = dict(
        session.execute(
            select(TranscriptionJob.call_id, TranscriptionJob.priority).where(
                TranscriptionJob.call_id.in_([live.id, backlog.id])
            )
        ).all()
    )
    assert priorities[live.id] == talkgroup_priority
    assert priorities[backlog.id] == talkgroup_priority + 500


@pytest.mark.parametrize("status", ["done", "empty"])
def test_the_repair_leaves_a_finished_transcript_alone(session, settings, status: str) -> None:  # noqa: ANN001
    call = _self_duplicate(session, ingest_source="trunkd", time_source="upload")
    call.transcript_status = status
    session.commit()

    report = repair_self_duplicates(session, settings)

    assert (report.restored, report.transcriptions_requeued) == (1, 0)
    session.refresh(call)
    assert call.duplicate_of is None
    assert call.transcript_status == status
    assert (
        session.scalar(select(TranscriptionJob.id).where(TranscriptionJob.call_id == call.id).limit(1))
        is None
    )


def test_a_dry_run_counts_and_writes_nothing(session, settings) -> None:  # noqa: ANN001
    call = _self_duplicate(session, ingest_source="trunkd", time_source="upload")
    session.commit()

    report = repair_self_duplicates(session, settings, dry_run=True)

    assert report.dry_run is True
    assert (report.scanned, report.restored) == (1, 1)
    assert report.conversations_reattached == 1
    assert report.transcriptions_requeued == 1
    assert report.commits == 0
    session.rollback()
    session.refresh(call)
    assert call.duplicate_of == call.id
    assert call.transcript_status == "skipped"


def test_the_repair_commits_in_batches(session, settings) -> None:  # noqa: ANN001
    for index in range(5):
        _self_duplicate(
            session,
            ingest_source="trunkd",
            time_source="upload",
            started_at=BASE + timedelta(minutes=index),
            path=f"synthetic/batched-{index}.mp3",
        )
    session.commit()

    report = repair_self_duplicates(session, settings, batch=2)

    assert (report.scanned, report.restored) == (5, 5)
    assert report.commits == 3
    assert session.scalar(select(Call.id).where(Call.duplicate_of == Call.id).limit(1)) is None
