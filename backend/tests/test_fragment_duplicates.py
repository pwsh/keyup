"""``keyup reconcile repair-fragments`` — :mod:`app.repair_fragment_duplicates`.

Before the duplicate rules were tightened, a short key-up that started just after a longer
call ended counted as a ``fragment inside`` it, a shared traffic frequency joined two
talkgroups, and an identical ``sha256`` joined voiceless recordings across talkgroups.  The repair re-judges those
duplicates with the fixed rules and frees the ones they refuse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

import app.repair_fragment_duplicates as repair_module
from app.exporters import ledger
from app.models import ActivityEvent, Call, CallExport, TranscriptionJob
from app.repair_fragment_duplicates import (
    FENCED_REASON,
    REPAIR_RULE,
    repair_fragment_duplicates,
)
from tests.conftest import requires_db
from tests.test_reconcile import BASE, _call

pytestmark = requires_db

TG = 1701


def _hidden(session, primary: Call, dup: Call, *, rule: str = "fragment inside", key: str | None = None,  # noqa: ANN001
            merged: list[str] | None = None) -> None:
    """``dup`` in the state an old, looser ruling left it: hidden behind ``primary``, out of its
    conversation, its pending transcription cancelled to ``skipped``."""
    reason = {
        "rule": "duration",
        "winner": "existing",
        "winner_id": primary.id,
        "match": {"rule": rule, "key": key or f"talkgroup:{TG}", "overlap_s": 0.0, "overlap_ratio": 0.0},
        "scores": {str(dup.id): {}, str(primary.id): {}},
        "merged_fields": [*(merged or []), "raw_meta.merged_from"],
        "at": datetime.now(UTC).isoformat(),
    }
    dup.duplicate_of = primary.id
    dup.conversation_id = None
    dup.transcript_status = "skipped"
    dup.reconciled_at = datetime.now(UTC)
    dup.reconcile_reason = dict(reason)
    primary.reconciled_at = datetime.now(UTC)
    primary.reconcile_reason = dict(reason)
    session.flush()


def _trunkd(session, start: datetime, duration: float, **kwargs) -> Call:  # noqa: ANN001, ANN003
    kwargs.setdefault("path", f"synthetic/t-{start.timestamp()}-{duration}-{kwargs.get('tgids', (TG,))}.mp3")
    return _call(
        session, started_at=start, duration=duration, ingest_source="trunkd", time_source="upload", **kwargs
    )


def _reply_after(session, start: datetime = BASE) -> tuple[Call, Call]:  # noqa: ANN001
    """An 8 s call and a 1.2 s reply that starts 0.5 s after it ended — the old rung's
    skew-widened containment called that "inside"; they were never on the air together."""
    primary = _trunkd(session, start, 8.0)
    reply = _trunkd(session, start + timedelta(seconds=8.5), 1.2)
    _hidden(session, primary, reply)
    return primary, reply


def _jobs(session, call_id: int) -> list[TranscriptionJob]:  # noqa: ANN001
    return list(session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == call_id)))


def test_a_fragment_that_was_never_on_the_air_with_its_primary_is_freed_and_queued(session, settings) -> None:  # noqa: ANN001
    primary, reply = _reply_after(session)
    session.commit()

    report = repair_fragment_duplicates(session, settings)
    body = report.as_dict()

    assert (body["examined"], body["refused_by_new_rules"], body["repaired"]) == (1, 1, 1)
    assert body["rematched_elsewhere"] == 0
    assert body["transcription_queued"] == 1
    assert body["transcription_queued_seconds"] == 1.2
    assert body["by_rule"] == {"fragment inside": {"examined": 1, "refused": 1}}

    session.expire_all()
    reply = session.get(Call, reply.id)
    assert reply.duplicate_of is None
    assert reply.reconciled_at is None
    assert reply.reconcile_reason["rule"] == REPAIR_RULE
    assert reply.reconcile_reason["previous"]["match"]["rule"] == "fragment inside"
    assert reply.conversation_id is not None
    assert reply.transcript_status == "queued"
    (job,) = _jobs(session, reply.id)
    # The backlog band: the talkgroup's own priority (100) + 500, behind every live call.
    assert job.priority >= 500
    # One summary line in the activity log.
    assert session.scalar(
        select(func.count()).select_from(ActivityEvent).where(ActivityEvent.kind == "reconcile.repair_fragments")
    ) == 1


def test_an_overlapping_true_fragment_stays_hidden(session, settings) -> None:  # noqa: ANN001
    primary = _trunkd(session, BASE, 8.0)
    fragment = _trunkd(session, BASE + timedelta(seconds=3.0), 1.2)
    _hidden(session, primary, fragment)
    session.commit()

    report = repair_fragment_duplicates(session, settings)

    assert (report.examined, report.refused_by_new_rules, report.repaired) == (1, 0, 0)
    session.expire_all()
    fragment = session.get(Call, fragment.id)
    assert fragment.duplicate_of == primary.id
    assert fragment.transcript_status == "skipped"
    assert _jobs(session, fragment.id) == []


def test_a_cross_talkgroup_identical_hash_is_freed(session, settings) -> None:  # noqa: ANN001
    squelch = "c" * 64
    primary = _trunkd(session, BASE, 1.7, tgids=(2,), sha256=squelch, path="synthetic/sq-a.mp3")
    other_tg = _trunkd(
        session, BASE + timedelta(seconds=5), 1.7, tgids=(TG,), sha256=squelch, path="synthetic/sq-b.mp3"
    )
    _hidden(session, primary, other_tg, rule="identical sha256", key=f"sha256:{squelch[:12]}")
    session.commit()

    report = repair_fragment_duplicates(session, settings)

    assert report.refused_by_new_rules == 1
    assert report.as_dict()["by_rule"] == {"identical sha256": {"examined": 1, "refused": 1}}
    session.expire_all()
    assert session.get(Call, other_tg.id).duplicate_of is None


def test_a_same_channel_identical_hash_stays_hidden(session, settings) -> None:  # noqa: ANN001
    """The exact rung as the fix applies it: same system, kind, channel, inside 60 s."""
    squelch = "d" * 64
    primary = _trunkd(session, BASE, 1.7, sha256=squelch, path="synthetic/sq-c.mp3")
    copy = _trunkd(session, BASE + timedelta(seconds=20), 1.7, sha256=squelch, path="synthetic/sq-d.mp3")
    _hidden(session, primary, copy, rule="identical sha256", key=f"sha256:{squelch[:12]}")
    session.commit()

    report = repair_fragment_duplicates(session, settings)

    assert (report.examined, report.refused_by_new_rules) == (1, 0)
    session.expire_all()
    assert session.get(Call, copy.id).duplicate_of == primary.id


def test_a_hash_hit_is_judged_on_the_primary_as_it_was_before_the_merge(session, settings) -> None:  # noqa: ANN001
    """A primary with no talkgroup that inherited the duplicate's does not "share" it."""
    squelch = "e" * 64
    primary = _trunkd(session, BASE, 1.7, tgids=(), frequency_hz=851_000_000, sha256=squelch, path="synthetic/m-a.mp3")
    dup = _trunkd(session, BASE + timedelta(seconds=10), 1.7, sha256=squelch, path="synthetic/m-b.mp3")
    _hidden(session, primary, dup, rule="identical sha256", key=f"sha256:{squelch[:12]}",
            merged=["talkgroup_ids", "talkgroup_id"])
    primary.talkgroup_ids = list(dup.talkgroup_ids)
    primary.talkgroup_id = dup.talkgroup_id
    session.commit()

    report = repair_fragment_duplicates(session, settings)
    body = report.as_dict()

    assert body["refused_by_new_rules"] == 1
    assert body["merge_side_effects"]["primaries_with_filled_fields"] == 1
    assert body["merge_side_effects"]["by_field"] == {"talkgroup_id": 1, "talkgroup_ids": 1}
    assert body["merge_side_effects"]["still_equal_to_separated_row"] == {"talkgroup_id": 1, "talkgroup_ids": 1}
    # Counted, not undone.
    session.expire_all()
    assert session.get(Call, primary.id).talkgroup_ids == [TG]


def test_a_freed_row_that_is_a_copy_of_another_call_ends_linked_to_it(session, settings) -> None:  # noqa: ANN001
    primary, reply = _reply_after(session)
    # The reply's real twin: the other recorder's copy of the same key-up.
    twin = _call(
        session,
        started_at=BASE + timedelta(seconds=8.3),
        duration=4.0,
        ingest_source="file",
        time_source="sidecar",
        path="synthetic/twin.mp3",
    )
    session.commit()

    report = repair_fragment_duplicates(session, settings)

    assert report.repaired == 1
    assert report.rematched_elsewhere == 1
    session.expire_all()
    reply, twin, primary = (session.get(Call, i) for i in (reply.id, twin.id, primary.id))
    assert reply.duplicate_of == twin.id or twin.duplicate_of == reply.id
    assert primary.duplicate_of is None
    assert reply.duplicate_of != primary.id


def test_a_dry_run_changes_nothing(session, settings) -> None:  # noqa: ANN001
    primary, reply = _reply_after(session)
    session.commit()
    before = (reply.duplicate_of, reply.transcript_status, dict(reply.reconcile_reason))

    report = repair_fragment_duplicates(session, settings, dry_run=True)

    assert report.dry_run
    assert (report.refused_by_new_rules, report.repaired, report.transcription_queued) == (1, 0, 1)
    assert report.commits == 0
    session.expire_all()
    reply = session.get(Call, reply.id)
    assert (reply.duplicate_of, reply.transcript_status, reply.reconcile_reason) == before
    assert _jobs(session, reply.id) == []
    assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 0


def test_a_second_run_is_a_no_op(session, settings) -> None:  # noqa: ANN001
    _reply_after(session)
    primary = _trunkd(session, BASE + timedelta(minutes=5), 8.0)
    fragment = _trunkd(session, BASE + timedelta(minutes=5, seconds=3), 1.2)
    _hidden(session, primary, fragment)
    session.commit()

    first = repair_fragment_duplicates(session, settings, batch=1)
    second = repair_fragment_duplicates(session, settings, batch=1)

    assert (first.refused_by_new_rules, first.repaired) == (1, 1)
    assert (second.refused_by_new_rules, second.repaired, second.transcription_queued) == (0, 0, 0)
    assert second.examined == 1  # only the true fragment is left, and it stays hidden


def test_the_window_bounds_the_duplicates_start(session, settings) -> None:  # noqa: ANN001
    _reply_after(session)
    session.commit()

    outside = repair_fragment_duplicates(session, settings, since=BASE + timedelta(days=1))
    inside = repair_fragment_duplicates(session, settings, since=BASE, until=BASE + timedelta(days=1))

    assert (outside.examined, outside.repaired) == (0, 0)
    assert (inside.examined, inside.repaired) == (1, 1)


def _live(*calls: Call) -> None:
    for call in calls:
        call.ingest_priority = "live"


def test_old_calls_are_not_put_into_the_export_ledger(session, settings) -> None:  # noqa: ANN001
    """A week-old freed row stays out of discovery; one inside the live window is released."""
    now = datetime.now(UTC)
    old_primary, old_reply = _reply_after(session)
    recent_primary, recent_reply = _reply_after(session, now - timedelta(minutes=5))
    _live(old_primary, old_reply, recent_primary, recent_reply)
    # The ruling's own ledger mark on the recent row: skipped as a copy of its primary.
    session.add(CallExport(call_id=recent_reply.id, service="openmhz", status="skipped",
                           skip_reason=f"already exported as call {recent_primary.id}", next_attempt_at=now))
    session.commit()

    report = repair_fragment_duplicates(session, settings)
    assert report.repaired == 2
    assert report.exports_released == 1
    assert report.notified == 1, "only the row an open page could be showing gets a frame"

    mark = now - timedelta(days=60)
    ledger.discover(session, service="openmhz", watermark=mark, settings=settings)

    def status(call: Call) -> list[str]:
        return [r.status for r in session.scalars(select(CallExport).where(CallExport.call_id == call.id))]

    assert status(old_reply) == []
    assert status(recent_reply) == ["pending"], "a freed call inside the window is exported like any other"


def test_without_an_age_bound_an_old_freed_row_is_fenced(session, settings) -> None:  # noqa: ANN001
    from app.exporters import gate

    settings = settings.model_copy(update={"export_max_age_minutes": 0})
    _, reply = _reply_after(session)
    _live(reply)
    gate.set_watermark(session, "openmhz", BASE - timedelta(days=1))
    session.commit()

    report = repair_fragment_duplicates(session, settings)
    ledger.discover(session, service="openmhz", watermark=BASE - timedelta(days=1), settings=settings)

    assert report.exports_fenced == 1
    rows = list(session.scalars(select(CallExport).where(CallExport.call_id == reply.id)))
    assert [(r.status, r.skip_reason) for r in rows] == [("skipped", FENCED_REASON)]


def test_the_cli_keeps_the_batch_pass_and_adds_the_repair(session) -> None:  # noqa: ANN001
    """``keyup reconcile`` is a group now; with no subcommand it is still the batch pass."""
    import json

    from typer.testing import CliRunner

    from app.cli import app

    _reply_after(session)
    session.commit()

    batch = CliRunner().invoke(app, ["reconcile", "--dry-run"])
    assert batch.exit_code == 0, batch.output
    assert "pairs" in json.loads(batch.stdout)

    bad = CliRunner().invoke(app, ["reconcile", "repair-fragments", "--requeue-priority", "soon"])
    assert bad.exit_code == 2

    dry = CliRunner().invoke(app, ["reconcile", "repair-fragments", "--dry-run", "--since", "2026-08-01"])
    assert dry.exit_code == 0, dry.output
    body = json.loads(dry.stdout)
    assert (body["refused_by_new_rules"], body["repaired"], body["dry_run"]) == (1, 0, True)


# --------------------------------------------------------------------------------------
# chains: a duplicate whose stored primary is itself a duplicate
# --------------------------------------------------------------------------------------


def _chain(session, *rows: Call) -> None:  # noqa: ANN001
    """``rows[0] → rows[1] → … → rows[-1]``, each link an old, looser ruling.  Hidden from the
    primary end first so every row keeps its own duplicate-side ``reconcile_reason``."""
    for dup, target in reversed(list(zip(rows, rows[1:], strict=False))):
        _hidden(session, target, dup)


def _refused_chain(session, start: datetime = BASE) -> tuple[Call, Call, Call, Call]:  # noqa: ANN001
    """``tail → middle → fragment → primary``.  ``fragment`` really is inside the 8 s
    primary; ``middle`` is a reply after it ended and ``tail`` a key-up 2 s after that —
    neither ever on the air with the row it hangs from.  ``tail`` is written first, so it
    is judged while its chain is still whole."""
    tail = _trunkd(session, start + timedelta(seconds=12), 1.0)
    primary = _trunkd(session, start, 8.0)
    fragment = _trunkd(session, start + timedelta(seconds=3), 1.2)
    middle = _trunkd(session, start + timedelta(seconds=8.5), 1.2)
    _chain(session, tail, middle, fragment, primary)
    return tail, middle, fragment, primary


def _matching_chain(session, start: datetime = BASE) -> tuple[Call, Call, Call]:  # noqa: ANN001
    """``tail → fragment → primary`` where both are true fragments of the primary."""
    tail = _trunkd(session, start + timedelta(seconds=5), 1.2)
    primary = _trunkd(session, start, 8.0)
    fragment = _trunkd(session, start + timedelta(seconds=3), 1.2)
    _chain(session, tail, fragment, primary)
    return tail, fragment, primary


def test_a_three_link_chain_with_a_refused_middle_link_is_freed(session, settings, monkeypatch) -> None:  # noqa: ANN001
    tail, middle, fragment, primary = _refused_chain(session)
    session.commit()
    locked: list[list[int]] = []
    real_lock_calls = repair_module.lock_calls

    def spy(session, ids):  # noqa: ANN001, ANN202
        locked.append(sorted(ids))
        return real_lock_calls(session, ids)

    monkeypatch.setattr(repair_module, "lock_calls", spy)

    report = repair_fragment_duplicates(session, settings)
    body = report.as_dict()

    assert (body["examined"], body["refused_by_new_rules"], body["repaired"]) == (3, 2, 2)
    assert (body["chains_followed"], body["chains_collapsed"], body["chains_unresolved"]) == (2, 0, 0)
    assert body["skipped_changed_underneath"] == 0
    # Every row of the tail's chain was row-locked before it was freed.
    assert sorted([tail.id, middle.id, fragment.id, primary.id]) in locked

    session.expire_all()
    tail, middle, fragment = (session.get(Call, c.id) for c in (tail, middle, fragment))
    assert tail.duplicate_of is None
    assert tail.reconcile_reason["rule"] == REPAIR_RULE
    assert tail.reconcile_reason["primary_was"] == primary.id
    assert tail.reconcile_reason["chain_via"] == [middle.id, fragment.id]
    assert tail.transcript_status == "queued"
    assert middle.duplicate_of is None
    assert fragment.duplicate_of == primary.id, "the true fragment stays hidden"


def test_a_chain_whose_tail_matches_the_final_primary_collapses(session, settings) -> None:  # noqa: ANN001
    tail, fragment, primary = _matching_chain(session)
    session.commit()

    dry = repair_fragment_duplicates(session, settings, dry_run=True)
    assert (dry.refused_by_new_rules, dry.chains_followed, dry.chains_collapsed) == (0, 1, 1)
    session.expire_all()
    assert session.get(Call, tail.id).duplicate_of == fragment.id, "a dry run writes nothing"

    report = repair_fragment_duplicates(session, settings)

    assert (report.refused_by_new_rules, report.repaired) == (0, 0)
    assert (report.chains_followed, report.chains_collapsed) == (1, 1)
    session.expire_all()
    tail = session.get(Call, tail.id)
    assert tail.duplicate_of == primary.id
    assert tail.transcript_status == "skipped"
    assert tail.reconcile_reason["match"]["rule"] == "fragment inside", "the ruling is kept"
    assert tail.reconcile_reason["chain_collapsed"]["via"] == [fragment.id]
    assert session.get(Call, fragment.id).duplicate_of == primary.id
    assert session.scalar(
        select(func.count()).select_from(ActivityEvent).where(ActivityEvent.kind == "reconcile.repair_fragments")
    ) == 1


def test_a_cycle_is_detected_and_skipped(session, settings) -> None:  # noqa: ANN001
    a = _trunkd(session, BASE, 8.0)
    b = _trunkd(session, BASE + timedelta(seconds=8.5), 1.2)
    _hidden(session, b, a)
    _hidden(session, a, b)
    b.reconcile_reason = {**b.reconcile_reason, "winner_id": a.id}
    session.commit()
    assert (a.duplicate_of, b.duplicate_of) == (b.id, a.id)

    for dry_run in (True, False):
        report = repair_fragment_duplicates(session, settings, dry_run=dry_run)
        assert (report.examined, report.chains_unresolved) == (2, 2)
        assert (report.refused_by_new_rules, report.repaired, report.chains_collapsed) == (0, 0, 0)

    session.expire_all()
    assert (session.get(Call, a.id).duplicate_of, session.get(Call, b.id).duplicate_of) == (b.id, a.id)


def test_a_chain_past_the_hop_limit_is_skipped_until_it_shortens(session, settings) -> None:  # noqa: ANN001
    primary = _trunkd(session, BASE, 8.0)
    links = [_trunkd(session, BASE + timedelta(seconds=1 + i * 0.4), 1.0) for i in range(12)]
    _chain(session, *links, primary)
    session.commit()

    first = repair_fragment_duplicates(session, settings)
    # links[0] is 12 hops from the primary and links[1] 11; links[2] is exactly the limit.
    assert first.chains_unresolved == 2
    assert first.chains_collapsed == 10 - 1  # links[2..10]; links[11] points at it already

    second = repair_fragment_duplicates(session, settings)
    assert (second.chains_unresolved, second.chains_collapsed, second.refused_by_new_rules) == (0, 2, 0)
    session.expire_all()
    assert {session.get(Call, c.id).duplicate_of for c in links} == {primary.id}


def test_after_one_apply_a_dry_run_has_nothing_left_to_refuse(session, settings) -> None:  # noqa: ANN001
    """Convergence: what a dry run refuses is what an apply repairs, and after the apply
    the chains are gone."""
    _refused_chain(session)
    _matching_chain(session, BASE + timedelta(minutes=5))
    session.commit()

    before = repair_fragment_duplicates(session, settings, dry_run=True)
    applied = repair_fragment_duplicates(session, settings)
    after = repair_fragment_duplicates(session, settings, dry_run=True)

    assert before.refused_by_new_rules == applied.repaired == 2
    assert before.chains_collapsed == applied.chains_collapsed == 1
    assert applied.skipped_changed_underneath == 0
    assert (after.refused_by_new_rules, after.chains_followed, after.chains_collapsed) == (0, 0, 0)
    assert after.chains_unresolved == 0


def test_a_row_moved_by_the_pass_is_retried_once_at_the_end(session, settings, monkeypatch) -> None:  # noqa: ANN001
    first_primary, reply = _reply_after(session)
    # Where another row's repair "moved" the reply between its verdict and its locks.
    second_primary = _trunkd(session, BASE + timedelta(seconds=0.2), 8.0)
    session.commit()
    attempts: list[int] = []
    real_lock_chain = repair_module._lock_chain

    def moved_on_first_try(session, dup, chain, report):  # noqa: ANN001, ANN202
        attempts.append(dup.id)
        if len(attempts) == 1:
            session.execute(
                update(Call)
                .where(Call.id == dup.id)
                .values(duplicate_of=second_primary.id)
                .execution_options(synchronize_session=False)
            )
        return real_lock_chain(session, dup, chain, report)

    monkeypatch.setattr(repair_module, "_lock_chain", moved_on_first_try)

    report = repair_fragment_duplicates(session, settings)

    assert attempts == [reply.id, reply.id]
    assert (report.examined, report.refused_by_new_rules, report.repaired) == (1, 1, 1)
    assert report.skipped_changed_underneath == 0
    session.expire_all()
    reply = session.get(Call, reply.id)
    assert reply.duplicate_of is None
    assert reply.reconcile_reason["primary_was"] == second_primary.id


def test_a_row_that_keeps_moving_is_skipped_after_its_retry(session, settings, monkeypatch) -> None:  # noqa: ANN001
    primary, reply = _reply_after(session)
    session.commit()
    attempts: list[int] = []

    def always_moved(session, dup, chain, report):  # noqa: ANN001, ANN202, ARG001
        attempts.append(dup.id)
        return False

    monkeypatch.setattr(repair_module, "_lock_chain", always_moved)

    report = repair_fragment_duplicates(session, settings)

    assert attempts == [reply.id, reply.id]
    assert (report.refused_by_new_rules, report.repaired, report.skipped_changed_underneath) == (1, 0, 1)
    session.expire_all()
    assert session.get(Call, reply.id).duplicate_of == primary.id


# --------------------------------------------------------------------------------------
# the audio pass follows the Admin page's switch
# --------------------------------------------------------------------------------------


def _audio_jobs(session, call_id: int) -> list:  # noqa: ANN001
    from app.models import AudioJob

    return list(session.scalars(select(AudioJob).where(AudioJob.call_id == call_id)))


def test_a_repair_with_audio_switched_off_queues_no_audio_pass(session, settings) -> None:  # noqa: ANN001
    """The 2026-09-23 history repair: the environment says on (the default), the settings
    table says off, and 450,962 audio jobs were queued only for the worker to cancel them.
    The table wins, as it does for ingest and the upload route."""
    from app import runtime_settings
    from app.models import Setting

    assert settings.audio_process_enabled is True  # the process default the CLI hands in
    _primary, reply = _reply_after(session)
    session.add(Setting(key="audio_process_enabled", value=False))
    session.commit()
    runtime_settings.invalidate()

    report = repair_fragment_duplicates(session, settings)

    assert (report.repaired, report.transcription_queued) == (1, 1)
    assert report.audio_queued == 0
    session.expire_all()
    assert _audio_jobs(session, reply.id) == []


def test_a_repair_with_audio_switched_on_queues_the_pass_as_before(session, settings) -> None:  # noqa: ANN001
    from app import runtime_settings
    from app.models import Setting

    _primary, reply = _reply_after(session)
    session.add(Setting(key="audio_process_enabled", value=True))
    session.commit()
    runtime_settings.invalidate()

    report = repair_fragment_duplicates(session, settings)

    assert report.repaired == 1
    assert report.audio_queued == 1
    session.expire_all()
    (job,) = _audio_jobs(session, reply.id)
    assert job.status == "queued"
    # The backlog band, as the transcription: a repair never goes in front of live calls.
    assert job.priority >= 500


@pytest.mark.parametrize(("switch", "queued"), [(False, 0), (True, 1)])
def test_the_batch_pass_honours_the_switch_too(session, settings, switch, queued) -> None:  # noqa: ANN001
    """``reconcile_batch`` (the scheduled pass and ``keyup reconcile``) re-queues the audio
    pass of a primary it promotes by swap, and it is handed the process settings too."""
    from app import runtime_settings
    from app.ingest.reconcile import reconcile_batch
    from app.models import AudioJob, Setting

    # Two primaries of one transmission; the later arrival (trunkd) is the better copy, so
    # the pass swaps the seat and the new primary has never been through the chain.
    _call(session, ingest_source="file", time_source="file_write")
    new = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=0.3),
        path="synthetic/batch-new.mp3",
    )
    session.add(Setting(key="audio_process_enabled", value=switch))
    session.commit()
    runtime_settings.invalidate()

    report = reconcile_batch(session, settings)
    session.commit()

    assert (report.pairs, report.swaps) == (1, 1)
    jobs = list(session.scalars(select(AudioJob).where(AudioJob.call_id == new.id)))
    assert len(jobs) == queued
