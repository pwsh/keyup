"""``keyup fix-upload-timestamps`` — putting the backlog back on the day it was recorded.

The fixture is the production shape in miniature: a few hundred pre-2025 sdrtrunk
recordings pushed by ``tools/migrate_sdrtrunk.py`` without a ``dateTime``, every one of
them stamped with the *upload* day because the parser rejected its filename, most of them
then chained behind a ``duplicate_of`` that reconciliation only believed because the times
said so — plus the two rows the repair must handle differently: a genuine pair of copies
that has to re-link once the true times are back, and a name the parser still cannot read,
which must be left exactly as it is and reported.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app import repair_timestamps as repair_mod
from app.cli import app
from app.models import Call, Conversation, Setting
from app.repair_timestamps import (
    DEFAULT_REGROUP_BATCH_SIZE,
    MAX_BATCH_ATTEMPTS,
    RECONCILE_DONE_KEY,
    RECONCILE_WATERMARK_KEY,
    REGROUP_WATERMARK_KEY,
    REPAIR_RULE,
    REQUEUE_CLASS,
    TimestampRepair,
    _lock_talkgroup,
    _lock_talkgroups,
    fix_upload_timestamps,
    regroup_repaired,
    repair_timestamps,
    rereconcile_repaired,
)
from tests.conftest import requires_db

pytestmark = requires_db

#: the day the migration ran — every backlog call was filed here
UPLOAD_DAY = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
#: the first call's true stamp, in the archive's own zone, as its filename spells it
FIRST_STAMP = datetime(2024, 2, 23, 21, 4, 14)
#: how many mis-stamped calls the fixture builds
BACKLOG = 120
#: a name from the era the parser could not read, verbatim
LEGACY_NAME = "20240223_210414Example_System_Central_T-Control__TO_1102_FROM_5226020.mp3"
#: and one nothing can read: it is not an sdrtrunk recording name at all
UNPARSED_NAME = "recording-2024-02-23.mp3"


def _name(stamp: datetime, tgid: int, radio: int) -> str:
    """The pre-2025 rendering: no underscore after the time, spaces as underscores."""
    return f"{stamp:%Y%m%d_%H%M%S}Example_System_Central_T-Control__TO_{tgid}_FROM_{radio}.mp3"


def _system(session) -> int:  # noqa: ANN001
    from app.ingest.service import upsert_system

    return upsert_system(session, "Example System", protocol="P25 Phase 1", wacn=101010)


def _push(  # noqa: PLR0913
    session,  # noqa: ANN001
    *,
    system_id: int,
    file_name: str,
    tgid: int,
    stored_at: datetime,
    duration: float = 3.0,
    time_source: str = "upload",
    transcript_status: str = "pending",
    transcript_text: str | None = None,
    path: str | None = None,
) -> Call:
    """One row exactly as ``POST /call-upload`` wrote it during the migration."""
    from app.ingest.service import upsert_talkgroup

    talkgroup = upsert_talkgroup(session, system_id, tgid, alias=None, seen_at=stored_at)
    call = Call(
        system_id=system_id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[tgid],
        ingest_source="upload",
        ingest_priority="backlog",
        recorded_at=stored_at + timedelta(seconds=duration),
        started_at=stored_at,
        started_local=stored_at.replace(tzinfo=None),
        ended_at=stored_at + timedelta(seconds=duration),
        time_source=time_source,
        duration_s=Decimal(str(round(duration, 3))),
        audio_state="present",
        bitrate=32000,
        sample_rate=22050,
        transcript_status=transcript_status,
        transcript_text=transcript_text,
        file_path=path or f"uploads/2026/09/01/{file_name}",
        file_name=file_name,
    )
    session.add(call)
    session.flush()
    return call


@pytest.fixture
def backlog(session):  # noqa: ANN001, ANN201
    """The archive as the migration left it, and the ids the assertions need."""
    system_id = _system(session)
    calls: list[Call] = []
    for i in range(BACKLOG):
        stamp = FIRST_STAMP + timedelta(minutes=i)
        tgid = 1701 + (i % 4)
        calls.append(
            _push(
                session,
                system_id=system_id,
                file_name=_name(stamp, tgid, 5226020 + i),
                tgid=tgid,
                # Every call filed a second apart on the upload day, which is what
                # ``now() - duration`` produces when a million of them arrive in a row.
                stored_at=UPLOAD_DAY + timedelta(seconds=i),
            )
        )

    # The damage reconciliation then did: the times said these were all one transmission,
    # so most of the archive was chained behind whichever row it saw first.
    for call in calls[1:40]:
        call.duplicate_of = calls[0].id
        call.reconciled_at = UPLOAD_DAY
        call.reconcile_reason = {"rule": "interval overlap", "winner_id": calls[0].id}

    # A transcript that has to survive: the bytes never changed, only the archive's belief
    # about when they were recorded.
    calls[50].transcript_status = "done"
    calls[50].transcript_text = "engine 1 responding"

    # A genuine pair — the same transmission recorded twice, one second and a hair of
    # duration apart, which the true times will re-link and the bogus ones never could.
    twin_stamp = FIRST_STAMP + timedelta(minutes=BACKLOG + 5)
    first = _push(
        session,
        system_id=system_id,
        file_name=_name(twin_stamp, 1110, 5226999),
        tgid=1110,
        stored_at=UPLOAD_DAY + timedelta(seconds=BACKLOG + 1),
        duration=6.0,
    )
    second = _push(
        session,
        system_id=system_id,
        file_name=_name(twin_stamp, 1110, 5227000),
        tgid=1110,
        stored_at=UPLOAD_DAY + timedelta(seconds=BACKLOG + 2),
        duration=5.6,
        path="uploads/2026/09/01/twin-second.mp3",
    )

    # The row the repair cannot help: keep it stamped as it is, and say so.
    unparsed = _push(
        session,
        system_id=system_id,
        file_name=UNPARSED_NAME,
        tgid=1701,
        stored_at=UPLOAD_DAY + timedelta(seconds=BACKLOG + 3),
    )

    # A live trunkd call on the same talkgroup, already correct: it must not be touched.
    live = _push(
        session,
        system_id=system_id,
        file_name="trunkd-1701-1740337454.mp3",
        tgid=1701,
        stored_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        path="uploads/2026/08/31/trunkd.mp3",
    )
    live.ingest_source = "trunkd"
    session.commit()
    return {
        "system_id": system_id,
        "ids": [c.id for c in calls],
        "chained": [c.id for c in calls[1:40]],
        "transcribed": calls[50].id,
        "twins": (first.id, second.id),
        "unparsed": unparsed.id,
        "live": live.id,
    }


def _reload(session, call_id: int) -> Call:  # noqa: ANN001
    session.expire_all()
    return session.get(Call, call_id)


# --------------------------------------------------------------------------------------
# phase A — the timestamps
# --------------------------------------------------------------------------------------


def test_a_dry_run_reports_the_damage_and_writes_nothing(session, settings, backlog) -> None:  # noqa: ANN001
    report = repair_timestamps(session, settings, dry_run=True)

    assert report.repaired == BACKLOG + 2  # every backlog call, twins included
    assert report.unparsed == 1
    assert report.unparsed_sample == [UNPARSED_NAME]
    # The window it would restore is the archive's real one, not the upload day.
    assert report.earliest is not None and report.earliest.year == 2024
    assert _reload(session, backlog["ids"][0]).started_at == UPLOAD_DAY
    assert _reload(session, backlog["chained"][0]).duplicate_of == backlog["ids"][0]


def test_the_times_come_back_off_the_filename(session, settings, backlog) -> None:  # noqa: ANN001
    repair_timestamps(session, settings, dry_run=False, batch_size=25)

    call = _reload(session, backlog["ids"][0])
    # 21:04:14 America/Winnipeg on 2024-02-23 is 03:04:14 UTC the next day, and the stamp is
    # the instant sdrtrunk *closed* the file — so it is the call's end, not its start.
    assert call.ended_at == datetime(2024, 2, 24, 3, 4, 14, tzinfo=UTC)
    assert call.recorded_at == call.ended_at
    assert call.started_at == call.ended_at - timedelta(seconds=3)
    assert call.started_local == datetime(2024, 2, 23, 21, 4, 11)
    assert call.time_source == "file_write"
    assert float(call.duration_s) == 3.0


def test_every_ruling_made_on_the_wrong_times_is_cleared(session, settings, backlog) -> None:  # noqa: ANN001
    repair_timestamps(session, settings, dry_run=False)

    session.expire_all()
    for call_id in backlog["chained"]:
        call = session.get(Call, call_id)
        assert call.duplicate_of is None
        assert call.reconciled_at is None
        assert call.reconcile_reason is None


def test_calls_that_did_not_arrive_over_the_push_path_are_left_alone(session, settings, backlog) -> None:  # noqa: ANN001
    repair_timestamps(session, settings, dry_run=False)

    live = _reload(session, backlog["live"])
    assert live.started_at == datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    assert live.time_source == "upload"


def test_a_name_that_still_will_not_parse_keeps_its_stamp(session, settings, backlog) -> None:  # noqa: ANN001
    report = repair_timestamps(session, settings, dry_run=False)

    assert report.unparsed == 1
    assert report.unparsed_sample == [UNPARSED_NAME]
    stuck = _reload(session, backlog["unparsed"])
    assert stuck.started_at == UPLOAD_DAY + timedelta(seconds=BACKLOG + 3)
    assert stuck.time_source == "upload"


def test_a_row_whose_stored_time_already_agrees_is_not_rewritten(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    """A push that really did send a ``dateTime`` matching its filename is not damage."""
    system_id = _system(session)
    honest = _push(
        session,
        system_id=system_id,
        file_name=LEGACY_NAME,
        tgid=1102,
        stored_at=datetime(2024, 2, 24, 3, 4, 11, tzinfo=UTC),
        path="uploads/2024/02/23/honest.mp3",
    )
    session.commit()

    repair_timestamps(session, settings, dry_run=False)

    assert _reload(session, honest.id).time_source == "upload"


def test_the_repair_is_self_limiting(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    first = repair_timestamps(session, settings, dry_run=False)
    second = repair_timestamps(session, settings, dry_run=False)

    assert first.repaired == BACKLOG + 2
    assert second.repaired == 0
    # …and the unparsed row is still counted, because it is still in scope.
    assert second.unparsed == 1


def test_an_interrupted_repair_resumes(session, settings, backlog) -> None:  # noqa: ANN001
    """Kill it mid-run: what was committed stays, and the rest is picked up next time."""

    def die(event: dict) -> None:
        if event["batch"] == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        repair_timestamps(session, settings, dry_run=False, batch_size=20, on_batch=die)

    session.rollback()
    done = session.scalars(
        select(Call).where(Call.time_source == "file_write", Call.ingest_source == "upload")
    ).all()
    assert 0 < len(done) < BACKLOG

    rest = repair_timestamps(session, settings, dry_run=False, batch_size=20)
    assert rest.repaired == BACKLOG + 2 - len(done)
    session.expire_all()
    assert session.get(Call, backlog["ids"][-1]).time_source == "file_write"


def test_a_row_hidden_behind_a_repaired_call_is_freed_and_ruled_on_again(session, settings, backlog) -> None:  # noqa: ANN001
    """The other end of a ``duplicate_of``, which phase A cannot repair and must not leave.

    This row is a trunkd call: its own time was always right, so nothing in phase A's scope
    describes it — but reconciliation hid it behind a backlog row on the strength of two
    times that were both wrong, and that backlog row has just moved to 2024.  Left alone it
    would stay a duplicate of a transmission two years away from it.
    """
    hidden = _push(
        session,
        system_id=backlog["system_id"],
        file_name="trunkd-1701-1756650000.mp3",
        tgid=1701,
        stored_at=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        path="uploads/2026/08/31/trunkd-hidden.mp3",
    )
    hidden.ingest_source = "trunkd"
    hidden.duplicate_of = backlog["ids"][0]
    hidden.reconciled_at = UPLOAD_DAY
    hidden.reconcile_reason = {"rule": "interval overlap", "winner_id": backlog["ids"][0]}
    session.commit()

    report = repair_timestamps(session, settings, dry_run=False)

    assert report.unhidden == 1
    row = _reload(session, hidden.id)
    assert row.duplicate_of is None
    assert row.reconciled_at is None
    # Marked, not blanked: it is the only thing that says this row belongs to the repair.
    assert row.reconcile_reason["rule"] == REPAIR_RULE
    assert row.started_at == datetime(2026, 8, 31, 13, 30, tzinfo=UTC)  # its time was never wrong

    # …and phase B walks it, so the ruling is made again rather than merely deleted.
    second = rereconcile_repaired(session, settings)
    assert second.reconcile_scanned == BACKLOG + 3
    assert _reload(session, hidden.id).duplicate_of is None  # nothing in 2026 matches it


def test_a_freed_row_is_given_a_conversation_again(session, settings, threaded) -> None:  # noqa: ANN001
    """A duplicate leaves its thread, so an unhidden row has none until phase C offers it one."""
    hidden = _push(
        session,
        system_id=threaded["system_id"],
        file_name="trunkd-1701-1756650001.mp3",
        tgid=1701,
        stored_at=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        path="uploads/2026/08/31/trunkd-hidden-2.mp3",
    )
    hidden.ingest_source = "trunkd"
    hidden.duplicate_of = threaded["ids"][0]
    session.commit()

    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    regroup_repaired(session, settings, batch_size=25)

    row = _reload(session, hidden.id)
    assert row.duplicate_of is None
    assert row.conversation_id is not None


# --------------------------------------------------------------------------------------
# phase B — reconciliation on the true times
# --------------------------------------------------------------------------------------


def test_genuine_duplicates_relink_and_everything_else_becomes_primary(session, settings, backlog) -> None:  # noqa: ANN001
    repair_timestamps(session, settings, dry_run=False)
    report = rereconcile_repaired(session, settings, batch_size=25)

    assert report.reconcile_pairs == 1
    session.expire_all()
    first, second = backlog["twins"]
    pair = [session.get(Call, first), session.get(Call, second)]
    duplicates = [c for c in pair if c.duplicate_of is not None]
    assert len(duplicates) == 1
    assert duplicates[0].duplicate_of == next(c.id for c in pair if c.duplicate_of is None)
    # The 39 rows chained behind an unrelated call are their own transmissions again.
    for call_id in backlog["chained"]:
        assert session.get(Call, call_id).duplicate_of is None


def test_a_repaired_call_keeps_its_transcript(session, settings, backlog) -> None:  # noqa: ANN001
    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)

    call = _reload(session, backlog["transcribed"])
    assert call.transcript_status == "done"
    assert call.transcript_text == "engine 1 responding"


def test_reconciliation_resumes_from_its_watermark(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    repair_timestamps(session, settings, dry_run=False)

    def die(event: dict) -> None:
        if event["batch"] == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        rereconcile_repaired(session, settings, batch_size=20, on_batch=die)
    session.rollback()
    mark = session.get(Setting, RECONCILE_WATERMARK_KEY)
    assert mark is not None and mark.value

    report = rereconcile_repaired(session, settings, batch_size=20)
    # It resumed rather than re-walking: fewer rows than the archive holds.
    assert report.reconcile_scanned < BACKLOG
    assert report.reconcile_pairs == 1
    assert session.get(Setting, RECONCILE_WATERMARK_KEY).value is None


def test_reconciliation_is_not_walked_again_when_nothing_new_was_repaired(session, settings, backlog) -> None:  # noqa: ANN001
    """"B finished, C was interrupted, run it again" must not re-walk a million rows."""
    repair_timestamps(session, settings, dry_run=False)
    first = rereconcile_repaired(session, settings)
    assert first.reconcile_scanned == BACKLOG + 2
    assert session.get(Setting, RECONCILE_DONE_KEY).value

    again = rereconcile_repaired(session, settings)
    assert again.reconcile_skipped is True
    assert again.reconcile_scanned == 0

    # New damage — a call the migration pushed after the last run — clears the marker.
    _push(
        session,
        system_id=backlog["system_id"],
        file_name=_name(FIRST_STAMP + timedelta(days=1), 1701, 5226999),
        tgid=1701,
        stored_at=UPLOAD_DAY + timedelta(seconds=500),
    )
    session.commit()
    repair_timestamps(session, settings, dry_run=False)
    assert session.get(Setting, RECONCILE_DONE_KEY).value is None

    third = rereconcile_repaired(session, settings)
    assert third.reconcile_skipped is False
    assert third.reconcile_scanned == BACKLOG + 3


# --------------------------------------------------------------------------------------
# phase C — conversations
# --------------------------------------------------------------------------------------


@pytest.fixture
def threaded(session, backlog):  # noqa: ANN001, ANN201
    """One enormous conversation per talkgroup, which is what the bogus times produced."""
    from app.ingest.service import assign_conversation

    for call_id in backlog["ids"]:
        assign_conversation(session, session.get(Call, call_id), 20)
    session.commit()
    return backlog


def test_the_bogus_threads_are_rebuilt_on_the_true_times(session, settings, threaded) -> None:  # noqa: ANN001
    before = session.scalars(select(Conversation)).all()
    # A handful of enormous threads: every call claimed to be seconds from the last, so a
    # talkgroup's whole day collapsed into one exchange.
    assert len(before) < BACKLOG / 4
    assert max(conv.call_count for conv in before) > 10

    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    report = regroup_repaired(session, settings, batch_size=25)

    assert report.regrouped >= BACKLOG
    session.expire_all()
    # A minute apart on a 20 s gap is one thread per call, not one per talkgroup.
    conversations = session.scalars(select(Conversation)).all()
    assert len(conversations) >= BACKLOG
    assert all(conv.call_count > 0 for conv in conversations)
    assert report.conversations_emptied >= 1
    for conv in conversations:
        assert conv.started_at.year in (2024, 2026)

    call = _reload(session, threaded["ids"][0])
    conv = session.get(Conversation, call.conversation_id)
    assert conv.started_at == call.started_at


def test_a_duplicate_does_not_rejoin_a_thread(session, settings, threaded) -> None:  # noqa: ANN001, ARG001
    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    regroup_repaired(session, settings)

    session.expire_all()
    for call in session.scalars(select(Call).where(Call.duplicate_of.is_not(None))):
        assert call.conversation_id is None


def test_the_regroup_resumes_from_its_watermark(session, settings, threaded) -> None:  # noqa: ANN001, ARG001
    repair_timestamps(session, settings, dry_run=False)

    def die(event: dict) -> None:
        if event["batch"] == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        regroup_repaired(session, settings, batch_size=20, on_batch=die)
    session.rollback()
    assert session.get(Setting, REGROUP_WATERMARK_KEY).value

    report = regroup_repaired(session, settings, batch_size=20)
    assert report.regrouped < BACKLOG
    assert session.get(Setting, REGROUP_WATERMARK_KEY).value is None


def test_the_conversation_lock_is_taken_before_a_thread_is_touched(session, settings, threaded) -> None:  # noqa: ANN001
    """The deadlock this phase used to be able to take with a live call on the same talkgroup.

    ``assign_conversation`` takes ``pg_advisory_xact_lock(4242, tgid)`` and *then* writes the
    conversation row; this phase used to leave the old thread first, which takes that row's
    write lock at the next flush, and ask for the advisory lock afterwards — the two orders
    are an ABBA pair.  So the recorded statement stream must show the lock before anything
    writes a ``conversations`` row.
    """
    from sqlalchemy import event

    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    session.commit()

    statements: list[str] = []
    bind = session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        regroup_repaired(session, settings, batch_size=25)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    locks = [i for i, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql]
    writes = [
        i
        for i, sql in enumerate(statements)
        if sql.lstrip().upper().startswith(("UPDATE CONVERSATIONS", "INSERT INTO CONVERSATIONS"))
    ]
    assert locks and writes
    assert locks[0] < writes[0]
    # one per call, not one per batch: every talkgroup a batch touches is held by name
    assert len(locks) >= BACKLOG


def test_the_lock_helper_says_whether_there_was_anything_to_lock(session) -> None:  # noqa: ANN001
    assert _lock_talkgroup(session, None) is False
    assert _lock_talkgroup(session, 1701) is True


def test_an_emptied_thread_another_system_points_at_is_kept(session, settings, threaded) -> None:  # noqa: ANN001
    """``external_links.conversation_id`` cascades, so dropping the thread deletes the link.

    A bogus grouping is this repair's to dissolve; a CAD incident number
    somebody attached to it is not.
    """
    from app.models import ExternalLink

    doomed = session.get(Call, threaded["ids"][0]).conversation_id
    assert doomed is not None
    session.add(
        ExternalLink(
            conversation_id=doomed,
            source="cad",
            external_id="2026-000123",
            link_confidence="strong",
            link_method="manual",
        )
    )
    session.commit()

    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    report = regroup_repaired(session, settings, batch_size=25)

    assert report.conversations_kept_linked == 1
    session.expire_all()
    kept = session.get(Conversation, doomed)
    assert kept is not None and kept.call_count == 0
    assert session.scalars(select(ExternalLink)).all()


# --------------------------------------------------------------------------------------
# the lock order, and losing a race for it
#
# The production failure: phase B aborted at 86 % of 1,041,258 rows with
#   Process A waits for advisory lock [16384,4242,6,2]; blocked by process B.
#   Process B waits for advisory lock [16384,4242,2,2]; blocked by process A.
# — two transactions, each holding several of the per-talkgroup conversation locks, each
# asking for one the other held.  Nothing was wrong with either; only the *order* was.
# --------------------------------------------------------------------------------------


def _advisory_keys(statements: list[tuple[str, object]]) -> list[int]:
    """The talkgroup keys of every ``pg_advisory_xact_lock(4242, …)`` in a statement stream."""
    keys = []
    for sql, params in statements:
        if "pg_advisory_xact_lock" in sql and isinstance(params, dict) and params.get("cls") == 4242:
            keys.append(params["key"])
    return keys


def _record(session):  # noqa: ANN001, ANN201
    """Capture ``(statement, parameters)`` for everything the session runs."""
    from sqlalchemy import event

    statements: list[tuple[str, object]] = []
    bind = session.get_bind()

    def listener(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        statements.append((statement, parameters))

    event.listen(bind, "before_cursor_execute", listener)
    return statements, lambda: event.remove(bind, "before_cursor_execute", listener)


def _first_write(statements: list[tuple[str, object]]) -> int:
    for i, (sql, _params) in enumerate(statements):
        head = sql.lstrip().upper()
        if head.startswith(("UPDATE ", "INSERT INTO ", "DELETE FROM ")):
            return i
    return len(statements)


def _prelock_run(statements: list[tuple[str, object]]) -> list[int]:
    """The batch's opening burst of conversation locks.

    One unbroken run of ``pg_advisory_xact_lock(4242, …)`` with nothing between them: that
    is the up-front acquisition.  Everything after it is ``assign_conversation`` asking
    again for a key the transaction already holds, which costs nothing and cannot block.
    """
    keys: list[int] = []
    for sql, params in statements:
        is_lock = (
            "pg_advisory_xact_lock" in sql
            and isinstance(params, dict)
            and params.get("cls") == 4242
        )
        if is_lock:
            keys.append(params["key"])
        elif keys:
            break
    return keys


def test_phase_b_takes_every_talkgroup_lock_it_can_predict_first_and_in_order(  # noqa: ANN201
    session, settings, backlog,  # noqa: ANN001
):
    """Ascending talkgroup order, before the batch writes anything.

    A total order over the keys is what makes the ABBA cycle impossible: two transactions
    that each want several of these locks both ask for the lower key first, so one of them
    always already holds everything the other is waiting behind.
    """
    repair_timestamps(session, settings, dry_run=False)
    session.commit()

    statements, stop = _record(session)
    try:
        rereconcile_repaired(session, settings, batch_size=1_000)
    finally:
        stop()

    leading = _prelock_run(statements)
    assert leading, "the batch took no conversation lock at all"
    assert leading == sorted(leading)
    assert len(leading) == len(set(leading))
    # And nothing was written before them.
    assert len(leading) <= _first_write(statements)
    # Every talkgroup in the batch, not merely the ones a swap happened to reach.
    tgids = set(
        session.scalars(
            select(Call.talkgroup_id).where(Call.talkgroup_id.is_not(None)).distinct()
        )
    )
    assert set(leading) == tgids


def test_phase_c_takes_the_batch_s_locks_up_front_and_in_order(session, settings, threaded) -> None:  # noqa: ANN001, ARG001
    """Same order, and phase C can predict *all* of its keys: it only touches its own rows."""
    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)
    session.commit()

    statements, stop = _record(session)
    try:
        regroup_repaired(session, settings, batch_size=25)
    finally:
        stop()

    leading = _prelock_run(statements)
    assert leading, "the batch took no conversation lock at all"
    assert leading == sorted(leading)
    assert len(leading) == len(set(leading))
    assert len(leading) <= _first_write(statements)


def test_the_lock_helper_takes_a_set_in_ascending_order_and_never_twice(session) -> None:  # noqa: ANN001
    statements, stop = _record(session)
    held: set[int] = set()
    try:
        _lock_talkgroups(session, [1104, 1701, None, 1103, 1701], held)
        # Already held: the transaction does not ask again, which is what keeps the order
        # a *total* one rather than "sorted within each batch".
        _lock_talkgroups(session, [1103, 1701], held)
    finally:
        stop()

    assert _advisory_keys(statements) == [1103, 1104, 1701]
    assert held == {1701, 1103, 1104}


def _deadlock() -> Exception:
    """The exception psycopg raises and SQLAlchemy re-raises for the production failure."""
    from psycopg.errors import DeadlockDetected
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "SELECT pg_advisory_xact_lock(%(cls)s, %(key)s)",
        {"cls": 4242, "key": 6},
        DeadlockDetected("deadlock detected"),
    )


@pytest.mark.parametrize("phase", ["repair", "reconcile", "regroup"])
def test_a_batch_that_deadlocks_is_run_again_and_counted_once(session, settings, threaded, phase) -> None:  # noqa: ANN001, ARG001
    """Postgres picked this transaction as the victim; the rows are still there.

    The batch is self-contained and committed on its own, so losing one costs a re-read and
    a sleep — not the run.  What must not happen is the retried batch reporting its rows
    twice, so nothing is counted into the summary until the ``COMMIT``.
    """
    seen: list[tuple[str, int]] = []

    def hook(name: str, attempt: int) -> None:
        seen.append((name, attempt))
        if name == phase and attempt == 1 and len([s for s in seen if s == (name, 1)]) == 1:
            raise _deadlock()

    events: list[dict] = []
    monkey = repair_mod.BATCH_FAULT_HOOK
    sleeper = repair_mod.time.sleep
    repair_mod.time.sleep = lambda _s: None  # the jitter is proven by reading it, not waiting
    repair_mod.BATCH_FAULT_HOOK = hook
    try:
        body = fix_upload_timestamps(
            session,
            settings,
            dry_run=False,
            batch_size=1_000,
            regroup_batch_size=1_000,
            regroup=True,
            on_batch=events.append,
        )
    finally:
        repair_mod.BATCH_FAULT_HOOK = monkey
        repair_mod.time.sleep = sleeper

    # The injected deadlock was retried, and the phase still walked its rows exactly once.
    assert (phase, 2) in seen
    assert body["repaired"] == BACKLOG + 2
    assert body["reconcile"]["scanned"] == BACKLOG + 2
    assert body["regroup"]["calls"] == BACKLOG + 2
    # One progress event per committed batch — the lost attempt reported nothing.
    assert len([e for e in events if e["phase"] == phase]) == 1
    # And the watermarks are cleared, which is how each phase says it reached the end.
    assert session.get(Setting, REGROUP_WATERMARK_KEY).value is None
    assert session.get(Setting, RECONCILE_WATERMARK_KEY).value is None


def test_a_batch_that_keeps_deadlocking_gives_up_and_stays_resumable(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    """Five attempts, then the operator hears about it — with the watermark still standing."""
    from sqlalchemy.exc import OperationalError

    attempts: list[int] = []

    def hook(name: str, attempt: int) -> None:
        if name == "repair":
            attempts.append(attempt)
            raise _deadlock()

    sleeper = repair_mod.time.sleep
    repair_mod.time.sleep = lambda _s: None
    repair_mod.BATCH_FAULT_HOOK = hook
    try:
        with pytest.raises(OperationalError):
            repair_timestamps(session, settings, dry_run=False, batch_size=1_000)
    finally:
        repair_mod.BATCH_FAULT_HOOK = None
        repair_mod.time.sleep = sleeper

    assert attempts == list(range(1, MAX_BATCH_ATTEMPTS + 1))
    session.rollback()
    # Nothing was repaired, so a re-run starts from the top and finds all of it — phase A
    # is resumable without a watermark.
    assert repair_timestamps(session, settings, dry_run=False).repaired == BACKLOG + 2


def test_something_that_is_not_a_deadlock_is_not_retried(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    """A bug is not a lock race: it is raised the first time, not five times."""
    from psycopg.errors import UndefinedColumn
    from sqlalchemy.exc import OperationalError

    attempts: list[int] = []

    def hook(name: str, attempt: int) -> None:
        attempts.append(attempt)
        raise OperationalError("SELECT 1", {}, UndefinedColumn("no such column"))

    repair_mod.BATCH_FAULT_HOOK = hook
    try:
        with pytest.raises(OperationalError):
            repair_timestamps(session, settings, dry_run=False)
    finally:
        repair_mod.BATCH_FAULT_HOOK = None
    assert attempts == [1]
    session.rollback()


# --------------------------------------------------------------------------------------
# the transcription queue the repair feeds
# --------------------------------------------------------------------------------------


def test_a_promoted_primary_is_queued_in_the_backlog_band(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    """A swap on a 2024 call must not queue in front of the live feed.

    ``_swap`` puts a displaced transcription at priority 1 — right for the one live call
    that provoked it, catastrophic for a repair that promotes hundreds of thousands of
    them.  The repair asks for the ``backlog`` class instead, which is the talkgroup's own
    priority pushed into the band that is meant to be starved by live traffic.
    """
    from app.ingest.flow import BACKLOG_BAND
    from app.models import TranscriptionJob

    # Two copies of one 2024 transmission, the later-arriving one the better recording —
    # so reconciliation on the true times promotes it and the archive briefly has a primary
    # with no text where the copy it displaced had some.  That is the swap that re-queues.
    stamp = FIRST_STAMP + timedelta(minutes=BACKLOG + 20)
    _push(
        session,
        system_id=backlog["system_id"],
        file_name=_name(stamp, 1120, 7000001),
        tgid=1120,
        stored_at=UPLOAD_DAY + timedelta(seconds=900),
        duration=6.0,
        transcript_status="done",
        transcript_text="the text on the copy this displaces",
    )
    _push(
        session,
        system_id=backlog["system_id"],
        file_name=_name(stamp, 1120, 7000002),
        tgid=1120,
        stored_at=UPLOAD_DAY + timedelta(seconds=901),
        duration=8.0,
        path="uploads/2026/09/01/swap-winner.mp3",
    )
    session.commit()

    repair_timestamps(session, settings, dry_run=False)
    report = rereconcile_repaired(session, settings)
    assert report.reconcile_swaps >= 1

    session.expire_all()
    jobs = session.scalars(select(TranscriptionJob).where(TranscriptionJob.status == "queued")).all()
    assert jobs, "the swap enqueued nothing to assert about"
    # Never 1: a repair's promoted primary waits behind the live feed, not in front of it.
    assert all(job.priority >= BACKLOG_BAND for job in jobs)
    assert REQUEUE_CLASS == "backlog"


def test_the_live_path_still_queues_a_swap_ahead_of_the_backlog(session, settings, backlog) -> None:  # noqa: ANN001, ARG001
    """The default is unchanged: only the repair passes ``backlog``."""
    from app.ingest.reconcile import ReconcileConfig, _swap
    from app.models import TranscriptionJob

    system_id = backlog["system_id"]
    ReconcileConfig.of(settings)
    loser = _push(
        session,
        system_id=system_id,
        file_name=_name(FIRST_STAMP, 1199, 1),
        tgid=1199,
        stored_at=UPLOAD_DAY,
        transcript_status="done",
        transcript_text="text that was on the copy this displaces",
    )
    winner = _push(
        session,
        system_id=system_id,
        file_name=_name(FIRST_STAMP, 1199, 2),
        tgid=1199,
        stored_at=UPLOAD_DAY,
        path="uploads/2026/09/01/live-winner.mp3",
    )
    session.flush()

    _swap(session, winner, loser, settings)
    session.commit()

    job = session.scalars(
        select(TranscriptionJob).where(TranscriptionJob.call_id == winner.id)
    ).one()
    assert job.priority == 1


# --------------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------------


def test_the_cli_is_dry_by_default_and_says_what_is_pending(session, backlog) -> None:  # noqa: ANN001
    result = CliRunner().invoke(app, ["fix-upload-timestamps"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dry_run"] is True
    assert body["repaired"] == BACKLOG + 2
    assert body["unparsed"] == 1
    assert body["unparsed_sample"] == [UNPARSED_NAME]
    assert body["reconcile"]["ran"] is False
    session.expire_all()
    assert session.get(Call, backlog["ids"][0]).started_at == UPLOAD_DAY


def test_the_cli_repairs_reconciles_and_regroups(session, threaded) -> None:  # noqa: ANN001
    result = CliRunner().invoke(
        app, ["fix-upload-timestamps", "--apply", "--batch-size", "40", "--regroup"]
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dry_run"] is False
    assert body["repaired"] == BACKLOG + 2
    assert body["reconcile"] == {
        "ran": True,
        "skipped": False,
        "scanned": BACKLOG + 2,
        "pairs": 1,
        "swaps": body["reconcile"]["swaps"],
    }
    assert body["regroup"]["ran"] is True
    assert body["regroup"]["pending"] == 0
    session.expire_all()
    assert session.get(Call, threaded["ids"][0]).started_at.year == 2024


def test_the_cli_recommends_pausing_the_scheduled_reconcile_pass(session, backlog) -> None:  # noqa: ANN001, ARG001
    """The one thing the command cannot fix for the operator: the job it runs beside.

    ``RECONCILE_ENABLED``/``RECONCILE_MINUTES`` drive a worker pass that takes the same
    per-talkgroup locks over a different set of rows.  The repair does not touch the job —
    it is the live archive's, not this run's — so it says so instead.
    """
    result = CliRunner().invoke(app, ["fix-upload-timestamps", "--apply", "--no-reconcile"])

    assert result.exit_code == 0, result.output
    assert "RECONCILE_ENABLED=false" in result.output


def test_the_cli_reports_the_conversations_it_was_not_asked_to_rebuild(session, backlog) -> None:  # noqa: ANN001, ARG001
    result = CliRunner().invoke(app, ["fix-upload-timestamps", "--apply", "--no-reconcile"])

    body = json.loads(result.stdout)
    assert body["reconcile"]["ran"] is False
    assert body["regroup"]["ran"] is False
    assert body["regroup"]["pending"] == BACKLOG + 2


def test_the_report_shape_is_json_serializable() -> None:
    """The summary goes to stdout for a script to read; nothing in it may be a datetime."""
    report = TimestampRepair(earliest=datetime.now(UTC), latest=datetime.now(UTC))
    json.dumps(report.as_dict())


def test_fix_upload_timestamps_runs_the_phases_in_order(session, settings, threaded) -> None:  # noqa: ANN001, ARG001
    body = fix_upload_timestamps(session, settings, dry_run=False, batch_size=30, regroup=True)

    assert body["repaired"] == BACKLOG + 2
    assert body["reconcile"]["ran"] and body["regroup"]["ran"]
    assert body["regroup"]["pending"] == 0
    # Phase C keeps its own, smaller batch: it is the one that holds a lock the feed waits on.
    assert body["regroup"]["batch_size"] == DEFAULT_REGROUP_BATCH_SIZE


# --------------------------------------------------------------------------------------
# phase C — the cost of a batch, against the weight of the archive behind it
# --------------------------------------------------------------------------------------

#: talkgroups the scaling fixture spreads its threads over
SCALE_TALKGROUPS = 20
#: repaired calls it leaves for the regroup to place, at either weight
SCALE_CALLS = 200
#: threads it says the regroup has already rebuilt, at the lighter of the two weights
SCALE_THREADS = 4_000

#: The grouper's own question (:func:`app.ingest.service.assign_conversation`), asked of a
#: talkgroup at the moment the regroup's frontier reaches it.  ``EXPLAIN`` on this is the
#: only thing that shows the cost the bug had: it never changed how *many* statements a
#: batch ran, only how much of the archive each one read.
_GROUPER_LOOKUP = """
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM conversations
WHERE talkgroup_id = :tg
  AND ended_at >= (:at)::timestamptz - interval '20 seconds'
  AND started_at <= (:at)::timestamptz + interval '24 seconds'
ORDER BY ended_at DESC LIMIT 1
"""

#: Where the scaling fixture's still-to-regroup calls sit, and the instant the lookup above
#: is measured at: the frontier, with the whole rebuilt archive behind it.
SCALE_FRONTIER = datetime(2024, 6, 1, tzinfo=UTC)


def _scale_fixture(session, *, threads: int) -> None:  # noqa: ANN001
    """Phase C's shape at a chosen weight, built in bulk because the point is the weight.

    ``threads`` threads dated 2022-2024 — what the regroup has already rebuilt and what the
    next call's lookup has to get past — plus one bogus upload-day thread per talkgroup and
    :data:`SCALE_CALLS` repaired calls still carrying the repair's marker.
    """
    from sqlalchemy import text

    from app.models import Base

    names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    session.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    session.execute(text("INSERT INTO systems (id, name) VALUES (1, 'Example System')"))
    session.execute(
        text(
            "INSERT INTO talkgroups (id, system_id, tgid, alias) "
            "SELECT g, 1, 1100 + g, 'tg' || g FROM generate_series(1, :n) g"
        ),
        {"n": SCALE_TALKGROUPS},
    )
    # The bogus grouping the repair is dissolving, stamped on the day of the upload.
    session.execute(
        text(
            """
            INSERT INTO conversations (id, talkgroup_id, started_at, ended_at, call_count)
            SELECT g, g, :upload, :upload + interval '1 hour', 0
            FROM generate_series(1, :n) g
            """
        ),
        {"n": SCALE_TALKGROUPS, "upload": UPLOAD_DAY},
    )
    session.commit()
    # The planner's picture of the table is taken here, before the repair fills it with the
    # 2022-2024 threads below — which is the state the production job ran in.
    session.execute(text("ANALYZE conversations"))
    session.commit()
    session.execute(
        text(
            """
            INSERT INTO conversations (id, talkgroup_id, started_at, ended_at, call_count)
            SELECT :n + g, 1 + (g % :n),
                   timestamp with time zone '2022-01-01 00:00:00+00'
                     + (g / :n) * interval '600 seconds',
                   timestamp with time zone '2022-01-01 00:00:00+00'
                     + (g / :n) * interval '600 seconds' + interval '30 seconds',
                   1
            FROM generate_series(1, :threads) g
            """
        ),
        {"n": SCALE_TALKGROUPS, "threads": threads},
    )
    session.execute(
        text(
            """
            INSERT INTO calls (id, system_id, talkgroup_id, talkgroup_ids, conversation_id,
                               started_at, ended_at, recorded_at, started_local, duration_s,
                               ingest_source, time_source, file_path, file_name, sha256)
            SELECT g, 1, 1 + (g % :n), ARRAY[1100 + 1 + (g % :n)], 1 + (g % :n),
                   :frontier + g * interval '90 seconds',
                   :frontier + g * interval '90 seconds' + interval '3 seconds',
                   :frontier + g * interval '90 seconds' + interval '3 seconds',
                   (:frontier + g * interval '90 seconds')::timestamp,
                   3.0, 'upload', 'file_write',
                   'scale/' || g || '.mp3', g || '.mp3', md5(g::text)
            FROM generate_series(1, :calls) g
            """
        ),
        {"n": SCALE_TALKGROUPS, "frontier": SCALE_FRONTIER, "calls": SCALE_CALLS},
    )
    for table in ("systems", "talkgroups", "conversations", "calls"):
        session.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT max(id) FROM {table}))"
            )
        )
    session.commit()


def _lookup_cost(session) -> tuple[str, int]:  # noqa: ANN001
    """``(index the grouper's lookup uses, pages it reads)`` at the fixture's frontier."""
    from sqlalchemy import text

    body = "\n".join(
        row[0]
        for row in session.execute(
            text(_GROUPER_LOOKUP), {"tg": 7, "at": SCALE_FRONTIER + timedelta(hours=3)}
        ).all()
    )
    scan = next(
        (line for line in body.splitlines() if "Index Scan" in line or "Seq Scan" in line),
        "",
    )
    index = scan.split("using ")[-1].split(" ")[0] if "using " in scan else "(no index)"
    pages = max(
        int(line.split("shared hit=")[-1].split(" ")[0].split("\n")[0])
        for line in body.splitlines()
        if "shared hit=" in line
    )
    return index, pages


def _regroup_counting_statements(session, settings, *, batch_size: int) -> list[int]:  # noqa: ANN001
    """Statements per committed batch, in order."""
    from sqlalchemy import event

    engine = session.get_bind()
    seen = [0]

    def count(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        seen[0] += 1

    event.listen(engine, "before_cursor_execute", count)
    per_batch: list[int] = []
    mark = [0]

    def on_batch(_info: dict) -> None:
        per_batch.append(seen[0] - mark[0])
        mark[0] = seen[0]

    try:
        regroup_repaired(session, settings, batch_size=batch_size, on_batch=on_batch)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return per_batch


def test_the_grouper_has_an_index_for_the_whole_question_it_asks(session) -> None:  # noqa: ANN001
    """The migration's index is on the migrated database, over the right two columns.

    ``assign_conversation`` asks for a talkgroup *and* a time bound on ``ended_at``; an
    index that covers only one of the two leaves the other to be checked by reading and
    discarding rows, which is what made phase C slower the further it got.
    """
    from sqlalchemy import inspect

    indexes = {
        ix["name"]: [c for c in ix["column_names"]]
        for ix in inspect(session.get_bind()).get_indexes("conversations")
    }
    assert "ix_conversations_talkgroup_ended" in indexes, indexes
    assert indexes["ix_conversations_talkgroup_ended"] == ["talkgroup_id", "ended_at"]


def test_a_regroup_batch_costs_the_same_whatever_the_archive_behind_it_weighs(  # noqa: ANN201
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
):
    """Double the archive, and a batch must do the same work — not twice as much.

    This is the production failure written down: phase C held at 1,000 calls per two
    seconds for half a million rows and then fell to 1,000 calls per five minutes, because
    the grouper's lookup was reading every thread the phase had already rebuilt on the
    talkgroup and throwing it away.  Nothing about the *number* of statements changed —
    that is why the check has to be on what each one reads.
    """
    costs = []
    for threads in (SCALE_THREADS, 2 * SCALE_THREADS):
        _scale_fixture(session, threads=threads)
        per_batch = _regroup_counting_statements(session, settings, batch_size=100)
        index, pages = _lookup_cost(session)
        costs.append((per_batch, index, pages))

    (light_batches, light_index, light_pages), (heavy_batches, heavy_index, heavy_pages) = costs

    # The statement count per batch never depended on the archive, and must not start to.
    assert light_batches == heavy_batches, (light_batches, heavy_batches)
    assert len(light_batches) == SCALE_CALLS // 100

    # Neither may the pages each statement reads.  Both weights must reach the lookup
    # through the index that answers all of it, and read about the same amount doing so.
    assert light_index == "ix_conversations_talkgroup_ended", light_index
    assert heavy_index == "ix_conversations_talkgroup_ended", heavy_index
    assert heavy_pages <= light_pages * 1.5, (light_pages, heavy_pages)


def test_the_regroup_keeps_the_planner_s_picture_of_the_threads_current(  # noqa: ANN201
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
    threaded,  # noqa: ANN001, ARG001
):
    """Phase C rewrites the timestamp of every thread in the table, so it re-``ANALYZE``s.

    The index above only helps if the planner will choose it, and it will not while its
    statistics describe the table as it was before the repair moved a million calls out of
    2026 and into 2022-2024.
    """
    statements: list[str] = []
    engine = session.get_bind()

    from sqlalchemy import event

    def record(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001, ANN002
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        repair_timestamps(session, settings, dry_run=False)
        rereconcile_repaired(session, settings)
        regroup_repaired(session, settings, batch_size=25)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert any("ANALYZE conversations" in s for s in statements)


def test_the_regroup_writes_conversation_id_once_per_call(session, settings, threaded) -> None:  # noqa: ANN001, ARG001
    """One row written on ``calls`` per regrouped call, not two.

    ``_leave_conversation`` nulled ``conversation_id`` and ``assign_conversation`` wrote it
    again a moment later; the grouper's first query autoflushes, so the null went to the
    database on its own and every regrouped call cost two writes to the widest table in the
    archive.  SQLAlchemy folds same-shape UPDATEs into one ``executemany``, so the count
    that shows it is rows, not statements.
    """
    from sqlalchemy import event

    from app.db import engine

    repair_timestamps(session, settings, dry_run=False)
    rereconcile_repaired(session, settings)

    rows = 0

    def watch(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, ARG001, PLR0913
        nonlocal rows
        flat = " ".join(statement.split()).lower()
        if flat.startswith("update calls set") and "conversation_id" in flat:
            rows += len(parameters) if executemany else 1

    event.listen(engine, "after_cursor_execute", watch)
    try:
        report = regroup_repaired(session, settings, batch_size=DEFAULT_REGROUP_BATCH_SIZE)
    finally:
        event.remove(engine, "after_cursor_execute", watch)

    assert report.regrouped > 0
    # At most one write per regrouped call — fewer, because a row that had no thread and
    # joins none (the duplicate, the unparsed name) is never written at all.  Before the
    # collapse this was 241 for 122 calls.
    assert rows <= report.regrouped
    assert rows >= report.regrouped - 5
