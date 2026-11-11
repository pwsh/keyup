"""Cross-recorder reconciliation against a real PostgreSQL — docs/RECONCILIATION.md.

The scenarios here are the ones the archive actually produces when sdrtrunk and trunkd
both cover the Example System: the same transmission twice from two tuners, a late-granted
trunkd fragment of a call sdrtrunk recorded whole, an encrypted trunkd row with no audio
beside an sdrtrunk copy that has some, a backlog arriving days after the live feed, and —
the case that must *not* match — two different transmissions back to back on one talkgroup.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ingest.parser import build_call_record
from app.ingest.reconcile import (
    W_AUDIO,
    W_DURATION,
    W_METADATA,
    W_PREFERENCE,
    W_QUALITY,
    W_SOURCE_QUALITY,
    W_TIME_SOURCE,
    W_TRANSCRIPT,
    Interval,
    ReconcileConfig,
    _deciding_rule,
    find_match,
    make_primary,
    matches,
    reconcile_batch,
    score_call,
)
from app.ingest.service import ingest_file
from app.models import Call, CallSource, Conversation, Talkgroup, TranscriptionJob
from tests.conftest import requires_db
from tests.test_parser import LONGEST, PLAIN

pytestmark = requires_db

BASE = datetime(2026, 8, 29, 20, 9, 45, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _system(session, name: str = "Example System") -> int:  # noqa: ANN001
    from app.ingest.service import upsert_system

    return upsert_system(session, name, protocol="P25 Phase 1", wacn=None)


def _call(  # noqa: PLR0913
    session,  # noqa: ANN001
    *,
    started_at: datetime = BASE,
    duration: float = 8.0,
    tgids: tuple[int, ...] = (1701,),
    ingest_source: str = "file",
    time_source: str = "file_write",
    audio_state: str = "present",
    frequency_hz: int | None = 852137500,
    encrypted: bool | None = None,
    emergency: bool | None = None,
    radio_raw: str | None = None,
    talker_alias: str | None = None,
    sha256: str | None = None,
    bitrate: int | None = 32000,
    sample_rate: int | None = 22050,
    source_quality: dict | None = None,
    source_instance: str | None = None,
    transcript_status: str = "pending",
    raw_meta: dict | None = None,
    system_id: int | None = None,
    path: str | None = None,
) -> Call:
    """One call row, written the way an ingest path would write it."""
    from app.ingest.service import upsert_talkgroup

    system_id = system_id if system_id is not None else _system(session)
    primary = None
    for tgid in tgids:
        tg = upsert_talkgroup(session, system_id, tgid, alias=None, seen_at=started_at)
        primary = primary or tg
    call = Call(
        system_id=system_id,
        talkgroup_id=primary.id if primary is not None else None,
        talkgroup_ids=list(tgids),
        ingest_source=ingest_source,
        recorded_at=started_at,
        started_at=started_at,
        started_local=started_at.replace(tzinfo=None),
        ended_at=started_at + timedelta(seconds=duration),
        time_source=time_source,
        duration_s=Decimal(str(round(duration, 3))),
        frequency_hz=frequency_hz,
        encrypted=encrypted,
        emergency=emergency,
        radio_raw=radio_raw,
        talker_alias=talker_alias,
        audio_state=audio_state,
        sha256=sha256,
        bitrate=bitrate,
        sample_rate=sample_rate,
        source_quality=source_quality,
        source_instance=source_instance,
        transcript_status=transcript_status,
        raw_meta=raw_meta,
        file_path=path or f"synthetic/{ingest_source}-{started_at.timestamp()}-{duration}.mp3",
        file_name="synthetic.mp3",
    )
    session.add(call)
    session.flush()
    return call


def _reconcile(session, newcomer: Call, other: Call, settings, **kwargs):  # noqa: ANN001, ANN003, ANN202
    from app.ingest.reconcile import reconcile_pair

    return reconcile_pair(session, newcomer, other, settings, **kwargs)


@pytest.fixture
def superseded_events(monkeypatch):  # noqa: ANN001, ANN201
    """Every ``call.superseded`` payload reconciliation put on the bus, in order."""
    from app.ingest import reconcile as reconcile_module

    seen: list[dict] = []
    real = reconcile_module.notify

    def spy(session, event, payload):  # noqa: ANN001, ANN202
        if event == "call.superseded":
            seen.append(payload)
        real(session, event, payload)

    monkeypatch.setattr(reconcile_module, "notify", spy)
    return seen


# --------------------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------------------


def _interval(start_offset: float, duration: float, tgids=(1701,), freq=852137500) -> Interval:  # noqa: ANN001
    started = BASE + timedelta(seconds=start_offset)
    return Interval(
        started_at=started,
        ended_at=started + timedelta(seconds=duration),
        duration_s=duration,
        talkgroup_ids=tuple(tgids),
        frequency_hz=freq,
    )


def test_two_tuners_of_one_transmission_match(settings) -> None:  # noqa: ANN001
    """Different tuners, 1.8 s of clock disagreement, 7 % of duration difference."""
    cfg = ReconcileConfig.of(settings)
    result = matches(_interval(0, 8.0), _interval(1.8, 7.4), cfg)
    assert result.matched
    assert result.rule == "interval overlap"
    assert result.key == "talkgroup:1701"


def test_a_median_length_call_matches_across_the_file_stamp_offset(settings) -> None:  # noqa: ANN001
    """The case that decides whether this feature works at all on the real archive.

    The corpus median call is 2.2 s, and an sdrtrunk file's start is derived from a stamp
    written at ``call end + 1.7 s``, so its row sits 1.7 s after trunkd's grant time.  That
    leaves 28 % overlap — nowhere near the 50 % floor — and the pair is carried by the
    aligned-edges rung instead.
    """
    cfg = ReconcileConfig.of(settings)
    result = matches(_interval(0, 2.4), _interval(1.7, 2.4), cfg)
    assert result.matched
    assert result.rule == "aligned edges"
    assert result.overlap_ratio < cfg.min_overlap


def test_two_short_key_ups_in_a_row_are_not_collapsed_by_the_allowance(settings) -> None:  # noqa: ANN001
    """The cap that keeps the aligned-edges rung honest.

    Two 1.2 s transmissions 1.3 s apart agree on both edges to within the 2 s skew — but
    the allowance is capped at the shorter recording's own length, so 1.3 s of offset on a
    1.2 s call is too much and they stay two calls.
    """
    cfg = ReconcileConfig.of(settings)
    assert not matches(_interval(0, 1.2), _interval(1.3, 1.2), cfg).matched


def _short(start_offset: float, duration: float, radio: int | None = None) -> Interval:
    """One sub-second key-up, optionally with the radio that made it."""
    started = BASE + timedelta(seconds=start_offset)
    return Interval(
        started_at=started,
        ended_at=started + timedelta(seconds=duration),
        duration_s=duration,
        talkgroup_ids=(1701,),
        frequency_hz=852137500,
        source_radio=radio,
    )


class TestTheShortCallRung:
    """Two whole-second stamps of one sub-second key-up (the key-up pair).

    Radio 2087 keyed TRANSIT for 0.648 s and both recorders heard it.  Each reported its call
    start in whole unix seconds, so one landed on 04:59:32 and the other on 04:59:33 — two
    intervals with a 0.352 s *gap* between them.  Rung 1 has no overlap to measure and rung
    2's allowance is capped at the shorter recording (0.648 s), which is less than the second
    between them, so both copies were stored as primaries.
    """

    def test_the_production_pair_matches(self, settings) -> None:  # noqa: ANN001
        cfg = ReconcileConfig.of(settings)
        result = matches(_short(0, 0.648, radio=2087), _short(1.0, 0.648, radio=2087), cfg)
        assert result.matched
        assert result.rule == "short-call"
        assert result.key == "talkgroup:1701"
        # …and it really is the new rung doing it, not one of the old ones sneaking through.
        assert result.overlap_ratio < cfg.min_overlap

    def test_two_radios_a_second_apart_stay_two_calls(self, settings) -> None:  # noqa: ANN001
        """The guard that keeps a busy talkgroup honest: different radios, different calls."""
        cfg = ReconcileConfig.of(settings)
        result = matches(_short(0, 0.6, radio=2087), _short(1.0, 0.6, radio=4113), cfg)
        assert not result.matched

    def test_a_short_call_and_a_long_one_are_never_this_rung(self, settings) -> None:  # noqa: ANN001
        """Length is the other half of the evidence: 0.6 s and 3 s are not one key-up.

        What is asserted here is that rung 3 *declined* it: it may only ever speak for two
        recordings of the same length, because "they are the same length" is most of what it
        has instead of an overlap.  (Rung 4 no longer takes this pair either: a
        0.6 s key-up that ends 0.3 s before the 3 s call starts does not overlap it on the
        air, so it is a separate transmission, not a fragment.)
        """
        cfg = ReconcileConfig.of(settings)
        result = matches(_short(0, 0.6, radio=2087), _short(0.9, 3.0, radio=2087), cfg)
        assert result.rule != "short-call"
        assert not result.matched

        # And once the long copy is far enough away that not even rung 4 will have it, the
        # short one has nothing left to match: rung 3 does not reach across a talkgroup's
        # quiet minute the way it reaches across a rounded second.
        assert not matches(_short(0, 0.6, radio=2087), _short(30.0, 3.0, radio=2087), cfg).matched

    def test_the_rung_does_not_reach_past_a_second_of_rounding(self, settings) -> None:  # noqa: ANN001
        """Whole-second rounding moves a start by at most a second, and so does this rung.

        The clock allowance is 2 s, and using it here would collapse the pair the
        aligned-edges cap exists to keep apart.
        """
        cfg = ReconcileConfig.of(settings)
        assert not matches(_short(0, 1.2, radio=2087), _short(1.3, 1.2, radio=2087), cfg).matched


def test_back_to_back_transmissions_are_not_one_call(settings) -> None:  # noqa: ANN001
    """The case the whole design has to get right: two key-ups in a row on one talkgroup.

    They abut — the second starts when the first ends — so the only overlap is the skew
    allowance, which is taken back out before the ratio precisely so that abutting calls
    score zero rather than 2 × skew.
    """
    cfg = ReconcileConfig.of(settings)
    result = matches(_interval(0, 6.0), _interval(6.2, 5.0), cfg)
    assert not result.matched
    assert result.rule == "overlap below the floor"


def test_a_call_a_minute_later_does_not_match(settings) -> None:  # noqa: ANN001
    cfg = ReconcileConfig.of(settings)
    assert not matches(_interval(0, 6.0), _interval(60, 6.0), cfg).matched


def test_a_short_trunkd_fragment_inside_a_long_call_matches(settings) -> None:  # noqa: ANN001
    """A late grant: trunkd caught 1.2 s of a transmission sdrtrunk recorded whole.

    1.2 s of an 8 s call is 15 % of the *longer* one but 100 % of the shorter, and the
    overlap ratio is measured against the shorter — yet two ±1 s stamps can still put it
    under the floor, which is what the fragment rule is for.
    """
    cfg = ReconcileConfig.of(settings)
    result = matches(_interval(0, 8.0), _interval(6.5, 1.2), cfg)
    assert result.matched


def test_frequency_alone_matches_when_one_side_has_no_talkgroup(settings) -> None:  # noqa: ANN001
    """One traffic frequency carries one transmission at a time, whatever it is labeled."""
    cfg = ReconcileConfig.of(settings)
    a = _interval(0, 6.0, tgids=(1701,))
    b = _interval(0.5, 5.8, tgids=(), freq=852137500)
    assert matches(a, b, cfg).matched
    assert matches(a, b, cfg).key == "frequency:852137500"


def test_a_different_talkgroup_on_a_different_frequency_never_matches(settings) -> None:  # noqa: ANN001
    cfg = ReconcileConfig.of(settings)
    a = _interval(0, 6.0, tgids=(1701,), freq=852137500)
    b = _interval(0, 6.0, tgids=(1601,), freq=853112500)
    assert not matches(a, b, cfg).matched


# --------------------------------------------------------------------------------------
# choosing the primary
# --------------------------------------------------------------------------------------


def test_identical_copies_keep_the_first_as_primary(session, settings) -> None:  # noqa: ANN001
    """A tie leaves the existing primary alone — which is what makes re-running idempotent."""
    first = _call(session, ingest_source="trunkd")
    second = _call(session, ingest_source="trunkd", path="synthetic/second.mp3")

    outcome = _reconcile(session, second, first, settings)

    assert outcome.matched and not outcome.swapped
    assert outcome.primary_id == first.id
    assert second.duplicate_of == first.id
    assert first.duplicate_of is None
    assert second.transcript_status == "skipped"


def test_a_trunkd_fragment_loses_to_the_full_sdrtrunk_call_and_gives_it_its_metadata(
    session, settings  # noqa: ANN001
) -> None:
    """1.2 s of trunkd against 8 s of sdrtrunk: sdrtrunk is primary, and inherits.

    The fragment is the *richer* row — trunkd knows the talker alias and the emergency
    flag an sdrtrunk file cannot carry — but metadata richness is four ranks below
    duration, so the complete recording wins and takes the metadata with it.
    """
    sdrtrunk = _call(session, duration=8.0, ingest_source="file")
    fragment = _call(
        session,
        started_at=BASE + timedelta(seconds=6.5),
        duration=1.2,
        ingest_source="trunkd",
        time_source="upload",
        talker_alias="MEDIC 3",
        emergency=True,
        radio_raw="9200102",
        path="synthetic/fragment.mp3",
    )

    outcome = _reconcile(session, fragment, sdrtrunk, settings)

    assert outcome.matched and not outcome.swapped
    assert outcome.primary_id == sdrtrunk.id
    assert outcome.rule == "duration"
    assert fragment.duplicate_of == sdrtrunk.id
    # merged, filling only what the sdrtrunk file had nothing for
    assert sdrtrunk.talker_alias == "MEDIC 3"
    assert sdrtrunk.emergency is True
    assert sdrtrunk.radio_raw == "9200102"
    assert str(fragment.id) in sdrtrunk.raw_meta["merged_from"]
    assert "talker_alias" in sdrtrunk.reconcile_reason["merged_fields"]


def test_the_audio_copy_beats_an_encrypted_metadata_only_row_and_inherits_the_encryption(
    session, settings  # noqa: ANN001
) -> None:
    """trunkd can describe an encrypted call but not record it; sdrtrunk recorded audio.

    The row with audio becomes the primary — audio outranks everything — and takes the
    facts only trunkd had: that the call was encrypted, and with which algorithm and key.
    """
    encrypted = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        audio_state="purged",
        encrypted=True,
        duration=8.2,
        bitrate=None,
        sample_rate=None,
        raw_meta={"algorithm": "AES-256", "keyId": "0x2A", "nac": "0x4C7"},
        transcript_status="skipped",
    )
    with_audio = _call(session, duration=8.0, ingest_source="file", path="synthetic/audio.mp3")

    outcome = _reconcile(session, with_audio, encrypted, settings)

    assert outcome.swapped
    assert outcome.rule == "audio"
    assert with_audio.duplicate_of is None
    assert encrypted.duplicate_of == with_audio.id
    assert with_audio.encrypted is True
    assert with_audio.raw_meta["algorithm"] == "AES-256"
    assert with_audio.raw_meta["keyId"] == "0x2A"
    assert with_audio.raw_meta["nac"] == "0x4C7"


def test_the_sources_of_the_loser_are_copied_when_the_winner_has_none(session, settings) -> None:  # noqa: ANN001
    """trunk-recorder's ``srcList`` is the only record of who keyed up third."""
    rich = _call(session, ingest_source="trunk_recorder", time_source="sidecar", duration=8.0)
    for seq, (src, pos) in enumerate(((9200102, 0.0), (9250041, 3.2))):
        session.add(
            CallSource(call_id=rich.id, src=src, pos_s=pos, len_s=3.0, seq=seq)
        )
    session.flush()
    poor = _call(session, ingest_source="file", duration=9.4, path="synthetic/poor.mp3")

    outcome = _reconcile(session, poor, rich, settings)

    assert outcome.swapped  # the longer recording wins
    copied = session.scalars(
        select(CallSource).where(CallSource.call_id == poor.id).order_by(CallSource.seq)
    ).all()
    assert [row.src for row in copied] == [9200102, 9250041]


def test_a_merged_call_does_not_win_on_length(session, settings) -> None:  # noqa: ANN001
    """A copy three times longer is trunkd's hang timer joining two calls, not a better one.

    The duration credit is capped at ``RECONCILE_MERGE_RATIO``, so the over-long copy gets
    exactly the credit a 1.5× copy would — and here the other side's exact time source and
    richer metadata are enough to out-argue it.
    """
    good = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        duration=8.0,
        talker_alias="ENGINE 1",
        emergency=False,
        encrypted=False,
        radio_raw="9200102",
    )
    merged = _call(
        session,
        ingest_source="file",
        time_source="file_write",
        duration=24.0,
        frequency_hz=None,
        path="synthetic/merged.mp3",
    )

    score_merged = score_call(session, merged, good, ReconcileConfig.of(settings))
    assert score_merged.parts["duration"] == 0.0
    assert "merged_suspect" in score_merged.notes

    outcome = _reconcile(session, merged, good, settings)
    assert not outcome.swapped
    assert outcome.primary_id == good.id


def test_an_exact_time_source_beats_a_derived_one(session, settings) -> None:  # noqa: ANN001
    """Same audio, same length: the copy that knows when the call started is the primary."""
    guessed = _call(session, time_source="file_write", ingest_source="file")
    exact = _call(
        session,
        time_source="upload",
        ingest_source="trunkd",
        started_at=BASE + timedelta(seconds=0.4),
        path="synthetic/exact.mp3",
    )

    outcome = _reconcile(session, exact, guessed, settings)
    assert outcome.swapped
    assert outcome.rule == "time_source"


def test_the_recorder_preference_is_only_a_tie_break(session, settings) -> None:  # noqa: ANN001
    """Everything else equal, ``RECONCILE_PREFER_SOURCES`` decides — and nothing more."""
    a = _call(session, ingest_source="file", time_source="upload")
    b = _call(session, ingest_source="trunkd", time_source="upload", path="synthetic/b.mp3")

    outcome = _reconcile(session, b, a, settings)
    assert outcome.swapped
    assert outcome.rule == "recorder"


# --------------------------------------------------------------------------------------
# what a swap moves
# --------------------------------------------------------------------------------------


def test_a_swap_moves_the_conversation_and_requeues_transcription(session, settings) -> None:  # noqa: ANN001
    """The new primary takes the thread, and the queue place the transcript it lost.

    Audio is never copied between two tuners' recordings, so the displaced transcript does
    not transfer; the new primary is queued at priority 1 instead, ahead of the backlog,
    because the archive briefly shows a primary with no text where it had one.
    """
    from app.ingest.service import assign_conversation

    old = _call(session, ingest_source="file", time_source="file_write", transcript_status="done")
    assign_conversation(session, old, settings.conversation_gap_seconds)
    conversation_id = old.conversation_id
    assert conversation_id is not None

    new = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=0.3),
        path="synthetic/new.mp3",
    )

    outcome = _reconcile(session, new, old, settings)

    assert outcome.swapped
    assert new.conversation_id == conversation_id
    assert old.conversation_id is None
    conversation = session.get(Conversation, conversation_id)
    assert conversation.first_call_id == new.id
    job = session.scalars(select(TranscriptionJob).where(TranscriptionJob.call_id == new.id)).one()
    assert (job.status, job.priority) == ("queued", 1)
    assert new.transcript_status == "queued"
    # the old copy keeps its own transcript; nothing was copied across
    assert old.transcript_status == "done"


def test_a_swap_repoints_a_third_copy_at_the_new_primary(session, settings) -> None:  # noqa: ANN001
    """Three copies of one transmission end as one primary and two duplicates."""
    first = _call(session, ingest_source="file", time_source="file_write")
    third = _call(session, ingest_source="file", time_source="file_write", path="synthetic/third.mp3")
    third.duplicate_of = first.id
    session.flush()

    winner = _call(
        session, ingest_source="trunkd", time_source="upload", path="synthetic/winner.mp3"
    )
    outcome = _reconcile(session, winner, first, settings)

    assert outcome.swapped
    session.refresh(third)
    assert third.duplicate_of == winner.id
    assert first.duplicate_of == winner.id
    assert winner.duplicate_of is None


def test_a_swap_tells_the_live_feed_the_old_primary_is_gone(  # noqa: ANN001
    session, settings, superseded_events
) -> None:
    """The demoted row is on somebody's screen and nothing else would take it off.

    The winner arrives as its own ``call.new``; a ``call.new`` for the loser is filtered
    out server-side (the call filters exclude duplicates), so without this event the
    operator is left looking at both copies of one transmission.
    """
    old = _call(session, ingest_source="file", time_source="file_write")
    new = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=0.3),
        path="synthetic/new.mp3",
    )

    outcome = _reconcile(session, new, old, settings)

    assert outcome.swapped
    assert len(superseded_events) == 1
    event = superseded_events[0]
    assert (event["id"], event["by"]) == (old.id, new.id)
    assert event["talkgroup_id"] == old.talkgroup_id
    assert event["started_at"].startswith("2026-08-29T20:09:45")


def test_the_ingest_path_says_nothing_when_the_existing_primary_keeps_the_seat(  # noqa: ANN001
    session, settings, superseded_events
) -> None:
    """Nothing was on screen to remove: the newcomer is announced as a duplicate."""
    first = _call(session, ingest_source="trunkd", time_source="upload")
    second = _call(session, ingest_source="file", path="synthetic/copy.mp3")

    outcome = _reconcile(session, second, first, settings)

    assert not outcome.swapped
    assert second.duplicate_of == first.id
    assert superseded_events == []


def test_a_batch_pass_demotion_is_announced_even_without_a_swap(  # noqa: ANN001
    session, settings, superseded_events
) -> None:
    """In the batch pass both rows were primaries, so the loser is on screen either way."""
    live = _call(session, ingest_source="trunkd", time_source="upload", duration=8.0)
    backlog = _call(
        session,
        ingest_source="file",
        time_source="file_write",
        started_at=BASE + timedelta(seconds=1.8),
        duration=8.0,
        path="synthetic/backlog-live.mp3",
    )
    session.commit()

    report = reconcile_batch(session, settings)
    session.commit()

    # The backlog copy arrived later and lost the tie-break, so it is the one demoted —
    # no swap, and the old code would have sent nothing at all.
    assert (report.pairs, report.swaps) == (1, 0)
    session.refresh(backlog)
    assert backlog.duplicate_of == live.id
    assert [(e["id"], e["by"]) for e in superseded_events] == [(backlog.id, live.id)]


def test_make_primary_forces_a_swap_by_hand(session, settings) -> None:  # noqa: ANN001
    winner = _call(session, ingest_source="trunkd", time_source="upload")
    loser = _call(session, ingest_source="file", time_source="file_write", path="synthetic/l.mp3")
    _reconcile(session, loser, winner, settings)
    assert loser.duplicate_of == winner.id

    outcome = make_primary(session, loser, settings)

    assert outcome.swapped
    assert loser.duplicate_of is None
    assert winner.duplicate_of == loser.id
    assert loser.reconcile_reason["rule"] == "manual"

    with pytest.raises(ValueError, match="already the primary"):
        make_primary(session, loser, settings)


# --------------------------------------------------------------------------------------
# the batch pass
# --------------------------------------------------------------------------------------


def test_the_batch_pass_finds_a_backlog_copy_and_is_idempotent(session, settings) -> None:  # noqa: ANN001
    """trunkd recorded it live; the sdrtrunk backlog is uploaded two days later.

    The backlog row carries the ``file_write`` time source and is 1.8 s off, which is
    exactly what the clock-skew allowance is sized for.  It has audio and trunkd's copy was
    a metadata-only encrypted row, so the backlog copy wins — and the second run of the
    same pass changes nothing at all.
    """
    live = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        audio_state="purged",
        encrypted=True,
        duration=8.1,
        transcript_status="skipped",
    )
    backlog = _call(
        session,
        ingest_source="file",
        time_source="file_write",
        started_at=BASE + timedelta(seconds=1.8),
        duration=8.0,
        path="synthetic/backlog.mp3",
    )
    session.commit()

    first = reconcile_batch(session, settings)
    session.commit()

    assert (first.pairs, first.swaps) == (1, 1)
    session.refresh(live)
    session.refresh(backlog)
    assert backlog.duplicate_of is None
    assert live.duplicate_of == backlog.id
    assert backlog.encrypted is True

    second = reconcile_batch(session, settings)
    session.commit()
    assert (second.pairs, second.swaps) == (0, 0)
    session.refresh(backlog)
    assert backlog.duplicate_of is None


def test_the_batch_pass_leaves_the_existing_primary_when_durations_are_equal(
    session, settings  # noqa: ANN001
) -> None:
    """Both copies complete, both with audio: trunkd keeps the seat on the tie-break."""
    live = _call(session, ingest_source="trunkd", time_source="upload", duration=8.0)
    backlog = _call(
        session,
        ingest_source="file",
        time_source="file_write",
        started_at=BASE + timedelta(seconds=1.8),
        duration=8.0,
        path="synthetic/backlog2.mp3",
    )
    session.commit()

    report = reconcile_batch(session, settings)
    session.commit()

    assert report.pairs == 1
    assert report.swaps == 0
    session.refresh(backlog)
    assert backlog.duplicate_of == live.id


def test_the_batch_pass_can_be_scoped_and_can_report_without_writing(session, settings) -> None:  # noqa: ANN001
    other_system = _system(session, "Some Other System")
    _call(session, ingest_source="trunkd", time_source="upload")
    _call(session, ingest_source="file", path="synthetic/copy.mp3")
    _call(session, system_id=other_system, ingest_source="file", path="synthetic/other-a.mp3")
    _call(
        session,
        system_id=other_system,
        ingest_source="trunkd",
        time_source="upload",
        path="synthetic/other-b.mp3",
    )
    session.commit()

    scoped = reconcile_batch(session, settings, system="Example System", dry_run=True)
    assert scoped.pairs == 1
    session.rollback()
    assert session.scalar(select(Call.duplicate_of).where(Call.file_path == "synthetic/copy.mp3")) is None

    since = reconcile_batch(session, settings, since=datetime.now(UTC) + timedelta(days=1))
    assert since.scanned == 0


def test_the_batch_pass_does_nothing_when_reconciliation_is_off(session, settings) -> None:  # noqa: ANN001
    _call(session, ingest_source="trunkd", time_source="upload")
    _call(session, ingest_source="file", path="synthetic/copy.mp3")
    session.commit()

    report = reconcile_batch(session, settings.model_copy(update={"reconcile_enabled": False}))
    assert (report.scanned, report.pairs) == (0, 0)


# --------------------------------------------------------------------------------------
# through the ingest path
# --------------------------------------------------------------------------------------


def test_ingest_reconciles_against_a_call_the_other_recorder_pushed(
    session, settings, fixture_audio  # noqa: ANN001
) -> None:
    """The file watcher's copy meets the trunkd row that is already there.

    The sdrtrunk file is 16 s of audio and the trunkd row is an encrypted metadata-only
    one over the same slice, so the file takes the primary seat and the trunkd row keeps
    the encryption facts it contributed.
    """
    record = build_call_record(fixture_audio / LONGEST, fixture_audio, tz=settings.tz)
    live = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=record.started_at + timedelta(seconds=1.1),
        duration=record.duration_s - 0.4,
        tgids=tuple(record.talkgroup_ids),
        frequency_hz=record.frequency_hz,
        audio_state="purged",
        encrypted=True,
        transcript_status="skipped",
    )
    session.commit()

    result = ingest_file(session, fixture_audio / LONGEST, settings=settings, audio_root=fixture_audio)

    assert result.status == "ingested"
    new = session.get(Call, result.call_id)
    session.refresh(live)
    assert new.duplicate_of is None
    assert live.duplicate_of == new.id
    assert new.encrypted is True
    assert new.reconcile_reason["rule"] == "audio"
    # counted once: the transmission was already in the directory's totals
    assert session.scalar(select(Talkgroup.call_count).where(Talkgroup.tgid == record.tgid)) == 1


def test_ingest_with_reconciliation_off_keeps_the_old_dedupe(
    session, settings, fixture_audio, tmp_path  # noqa: ANN001
) -> None:
    """``RECONCILE_ENABLED=false`` is today's behavior, unchanged: first copy wins."""
    original = fixture_audio / PLAIN
    tree = tmp_path / "audio"
    (tree / "a").mkdir(parents=True)
    (tree / "b").mkdir(parents=True)
    shutil.copy2(original, tree / "a" / original.name)
    shutil.copy2(original, tree / "b" / original.name)
    local = settings.model_copy(update={"audio_root": tree, "reconcile_enabled": False})

    first = ingest_file(session, tree / "a" / original.name, settings=local, audio_root=tree)
    second = ingest_file(session, tree / "b" / original.name, settings=local, audio_root=tree)

    assert first.status == "ingested"
    assert second.status == "duplicate"
    assert second.duplicate_of == first.call_id
    assert session.get(Call, second.call_id).reconciled_at is None


def test_a_null_sha256_is_never_a_match(session, settings) -> None:  # noqa: ANN001
    """Two audio-less rows on different talkgroups minutes apart are two transmissions.

    ``sha256`` is NULL for a metadata-only call, and ``sha256 = NULL`` renders as ``IS
    NULL``, which would make the first such row the "original" of every one after it.
    """
    first = _call(session, audio_state="purged", encrypted=True, sha256=None, tgids=(1701,))
    later = _call(
        session,
        audio_state="purged",
        encrypted=True,
        sha256=None,
        tgids=(1999,),
        frequency_hz=853112500,
        started_at=BASE + timedelta(minutes=10),
        path="synthetic/later.mp3",
    )
    session.commit()

    other, result = find_match(session, later, ReconcileConfig.of(settings))
    assert other is None
    assert first.duplicate_of is None


def test_an_sdrtrunk_file_start_is_derived_and_therefore_late(
    session, settings, fixture_audio  # noqa: ANN001
) -> None:
    """The measurement reconciliation's clock-skew allowance is sized for.

    sdrtrunk stamps a file when it *closes* it — ``call end + 1.7 s`` (sd 0.43 s) measured
    against trunkd's control-channel grant times — so ``started_at`` is that minus the
    duration and lands about two seconds late.  That is why ``file_write`` is the one time
    source the score does not treat as exact, and why the default skew is 2 s: an sdrtrunk
    file and a trunkd push of one transmission must still overlap.
    """
    result = ingest_file(session, fixture_audio / PLAIN, settings=settings, audio_root=fixture_audio)
    call = session.get(Call, result.call_id)

    assert call.time_source == "file_write"
    assert call.ended_at == call.recorded_at
    assert (call.ended_at - call.started_at).total_seconds() == pytest.approx(
        float(call.duration_s), abs=0.001
    )

    # a trunkd push of the same transmission, carrying the true grant time, still matches
    cfg = ReconcileConfig.of(settings)
    true_start = call.started_at - timedelta(seconds=1.7)
    live = Interval(
        started_at=true_start,
        ended_at=true_start + timedelta(seconds=float(call.duration_s)),
        duration_s=float(call.duration_s),
        talkgroup_ids=tuple(call.talkgroup_ids),
        frequency_hz=call.frequency_hz,
    )
    result = matches(Interval.of(call), live, cfg)
    assert result.matched, result
    assert result.rule == "aligned edges"  # 2.4 s of call, 1.7 s of offset


def test_the_score_prefers_the_recorder_that_knows_when_the_call_began(
    session, settings  # noqa: ANN001
) -> None:
    """``file_write`` is derived, and derived loses to a grant time, all else equal."""
    derived = _call(session, ingest_source="file", time_source="file_write")
    exact = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=1.7),
        path="synthetic/grant.mp3",
    )
    cfg = ReconcileConfig.of(settings)

    assert score_call(session, exact, derived, cfg).parts["time_source"] == 300.0
    assert score_call(session, derived, exact, cfg).parts["time_source"] == 0.0


# --------------------------------------------------------------------------------------
# source quality (docs/RECONCILIATION.md "Source quality")
# --------------------------------------------------------------------------------------


def _pair(session, **quality):  # noqa: ANN001, ANN003, ANN202
    """Two copies of one transmission from two receivers, identical but for the reports.

    Everything else is deliberately equal, so any difference in the totals can only be the
    source-quality term.
    """
    left = _call(
        session,
        ingest_source="trunkd",
        source_instance="hackrf-a",
        source_quality=quality.get("left"),
        path="synthetic/a.mp3",
    )
    right = _call(
        session,
        ingest_source="trunkd",
        source_instance="hackrf-b",
        source_quality=quality.get("right"),
        path="synthetic/b.mp3",
    )
    return left, right


def test_the_receiver_that_heard_it_better_takes_the_seat(session, settings) -> None:  # noqa: ANN001
    """Everything else tied, the decoder's own frame counts decide."""
    clean, holey = _pair(
        session,
        left={"instance": "hackrf-a", "voice_frames": 420, "bad_frames": 1},
        right={"instance": "hackrf-b", "voice_frames": 430, "bad_frames": 40},
    )
    cfg = ReconcileConfig.of(settings)

    good = score_call(session, clean, holey, cfg)
    bad = score_call(session, holey, clean, cfg)

    # 430 − 5×40 = 230 against 420 − 5×1 = 415: more frames out is not a better copy.
    assert good.parts["source_quality"] == 700.0
    assert bad.parts["source_quality"] == 0.0
    assert good.total > bad.total
    assert _deciding_rule(good, bad) == "source_quality"


def test_source_quality_outranks_everything_it_is_meant_to(session, settings) -> None:
    """700 clears time source + a full metadata set + encoding + transcript + preference."""
    cfg = ReconcileConfig.of(settings)
    beneath = W_TIME_SOURCE + W_METADATA * 8 + W_QUALITY + W_TRANSCRIPT + W_PREFERENCE
    assert beneath < W_SOURCE_QUALITY
    # …and stays under the ranks that must keep beating it.
    assert W_SOURCE_QUALITY < W_DURATION
    assert W_SOURCE_QUALITY < W_AUDIO

    heard_better = _call(
        session,
        ingest_source="file",
        time_source="file_write",
        source_instance="hackrf-a",
        source_quality={"voice_frames": 400, "bad_frames": 0},
        path="synthetic/heard.mp3",
    )
    knows_more = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        radio_raw="9250041",
        talker_alias="IC ENG 1",
        encrypted=False,
        emergency=False,
        source_instance="hackrf-b",
        source_quality={"voice_frames": 400, "bad_frames": 20},
        transcript_status="done",
        path="synthetic/knows.mp3",
    )

    assert (
        score_call(session, heard_better, knows_more, cfg).total
        > score_call(session, knows_more, heard_better, cfg).total
    )


def test_a_copy_with_no_quality_report_scores_exactly_as_before(session, settings) -> None:  # noqa: ANN001
    plain_a, plain_b = _pair(session)
    cfg = ReconcileConfig.of(settings)

    a = score_call(session, plain_a, plain_b, cfg)
    b = score_call(session, plain_b, plain_a, cfg)

    assert a.parts["source_quality"] == 0.0
    assert b.parts["source_quality"] == 0.0
    assert a.total == b.total
    assert _deciding_rule(a, b) == "tie"


def test_one_sided_and_unusable_reports_add_no_term(session, settings) -> None:  # noqa: ANN001
    cfg = ReconcileConfig.of(settings)
    good = {"instance": "hackrf-a", "voice_frames": 400}

    # Only one side reports: there is nothing to compare it with.
    one_sided, silent = _pair(session, left=good)
    assert score_call(session, one_sided, silent, cfg).parts["source_quality"] == 0.0

    # A report that never says how much voice it decoded says nothing comparable.
    session.rollback()
    scored, wordless = _pair(session, left=good, right={"instance": "hackrf-b", "bad_frames": 3})
    assert score_call(session, scored, wordless, cfg).parts["source_quality"] == 0.0


def test_two_copies_from_one_receiver_are_not_ranked_against_each_other(
    session, settings  # noqa: ANN001
) -> None:
    """Comparing an instance with itself says nothing about which receiver to keep."""
    left = _call(
        session,
        source_instance="hackrf-a",
        source_quality={"instance": "hackrf-a", "voice_frames": 400, "bad_frames": 0},
        path="synthetic/one.mp3",
    )
    right = _call(
        session,
        source_instance="hackrf-a",
        source_quality={"instance": "hackrf-a", "voice_frames": 200, "bad_frames": 0},
        path="synthetic/two.mp3",
    )
    cfg = ReconcileConfig.of(settings)

    assert score_call(session, left, right, cfg).parts["source_quality"] == 0.0
    assert score_call(session, right, left, cfg).parts["source_quality"] == 0.0


# --------------------------------------------------------------------------------------
# the audio-processing queue follows the primary seat
# --------------------------------------------------------------------------------------


def _audio_jobs(session, call: Call):  # noqa: ANN001, ANN202
    from app.models import AudioJob

    return list(session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).all())


def test_a_swap_queues_the_audio_pass_for_the_new_primary(session, settings) -> None:  # noqa: ANN001
    """The bug this exists for: the seat moved and the processed file did not follow it.

    Two recorders send the same transmission.  The first arrival was primary at ingest, so
    the processing job was queued for *it* and wrote *its* ``.proc.mp3``.  When the second
    arrival scores higher and takes the seat, the row that is now played has never been
    through the chain — and before this, nothing ever revisited it, so ``processed_path``
    stayed NULL for good while its hidden copy held a file nobody could reach.
    """
    from app.models import AudioJob, Talkgroup

    old = _call(session, ingest_source="file", time_source="file_write")
    old.processed_path = "synthetic/old.proc.mp3"
    session.add(AudioJob(call_id=old.id, status="queued", priority=3))
    session.flush()

    new = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=0.3),
        path="synthetic/new.mp3",
    )

    outcome = _reconcile(session, new, old, settings)

    assert outcome.swapped
    assert new.duplicate_of is None
    assert old.duplicate_of == new.id

    job = next(row for row in _audio_jobs(session, new) if row.status == "queued")
    assert outcome.audio_job_id == job.id
    assert outcome.reason["requeued_audio_job"] == job.id
    # ``live`` resolved against this talkgroup — the band the upload itself would have used.
    talkgroup = session.get(Talkgroup, new.talkgroup_id)
    assert job.priority == talkgroup.priority

    # and the copy's pass is called off: it would write a file no listener can reach
    assert [row.status for row in _audio_jobs(session, old)] == ["cancelled"]


def test_a_swap_does_not_requeue_a_new_primary_that_is_already_processed(session, settings) -> None:  # noqa: ANN001
    """The winner came in with its own processed copy — a second pass is wasted work."""
    old = _call(session, ingest_source="file", time_source="file_write")
    new = _call(
        session,
        ingest_source="trunkd",
        time_source="upload",
        started_at=BASE + timedelta(seconds=0.3),
        path="synthetic/new.mp3",
    )
    new.processed_path = "synthetic/new.proc.mp3"
    session.flush()

    outcome = _reconcile(session, new, old, settings)

    assert outcome.swapped
    assert outcome.audio_job_id is None
    assert _audio_jobs(session, new) == []


def test_the_first_arrival_keeping_the_seat_queues_no_extra_audio_job(session, settings) -> None:  # noqa: ANN001
    """No swap, no requeue: the row that was processed at ingest is still the row played."""
    from app.models import AudioJob

    first = _call(session, ingest_source="trunkd", time_source="upload")
    session.add(AudioJob(call_id=first.id, status="queued", priority=3))
    session.flush()

    second = _call(session, ingest_source="file", time_source="file_write", path="synthetic/2.mp3")

    outcome = _reconcile(session, second, first, settings)

    assert not outcome.swapped
    assert outcome.audio_job_id is None
    assert second.duplicate_of == first.id
    assert _audio_jobs(session, second) == []
    # the standing primary's own job is untouched
    assert [row.status for row in _audio_jobs(session, first)] == ["queued"]


def test_a_row_demoted_by_the_batch_pass_has_its_queued_audio_job_cancelled(session, settings) -> None:  # noqa: ANN001
    """In the batch pass the loser *was* a primary, so it really does have a pass waiting."""
    from app.models import AudioJob

    loser = _call(session, ingest_source="file", time_source="file_write")
    session.add(AudioJob(call_id=loser.id, status="queued", priority=3))
    winner = _call(session, ingest_source="trunkd", time_source="upload", path="synthetic/w.mp3")
    session.add(AudioJob(call_id=winner.id, status="running", priority=3))
    session.flush()

    _reconcile(session, loser, winner, settings)

    assert loser.duplicate_of == winner.id
    assert [row.status for row in _audio_jobs(session, loser)] == ["cancelled"]
    # a running pass is left alone — it is mid-ffmpeg on a file this must not pull away
    assert [row.status for row in _audio_jobs(session, winner)] == ["running"]


# --------------------------------------------------------------------------------------
# the batch pass commits in chunks (the 122 s stall of 2026-09-03)
# --------------------------------------------------------------------------------------


def _pairs_fixture(session, system_id: int, tag: str, pairs: int) -> None:  # noqa: ANN001
    """``pairs`` reconcilable pairs plus one unmatched call, all on one system."""
    for i in range(pairs):
        at = BASE + timedelta(minutes=10 * i)
        _call(
            session,
            system_id=system_id,
            started_at=at,
            ingest_source="trunkd",
            time_source="upload",
            duration=8.0,
            path=f"synthetic/{tag}-live-{i}.mp3",
        )
        _call(
            session,
            system_id=system_id,
            started_at=at + timedelta(seconds=1.8),
            ingest_source="file",
            time_source="file_write",
            duration=8.0,
            path=f"synthetic/{tag}-backlog-{i}.mp3",
        )
    _call(
        session,
        system_id=system_id,
        started_at=BASE + timedelta(hours=3),
        ingest_source="trunkd",
        time_source="upload",
        path=f"synthetic/{tag}-alone.mp3",
    )
    session.commit()


def _outcome(session, tag: str) -> dict[str, str | None]:  # noqa: ANN001
    """Who ended up a duplicate of whom, named by file path so two runs compare."""
    by_id = {
        call.id: call
        for call in session.scalars(select(Call).where(Call.file_path.like(f"synthetic/{tag}-%")))
    }
    return {
        call.file_path: (by_id[call.duplicate_of].file_path if call.duplicate_of else None)
        for call in by_id.values()
    }


def test_the_batch_pass_commits_every_chunk_and_matches_one_transaction(
    session, settings  # noqa: ANN001
) -> None:
    """Two chunks' worth of candidates and one over commit three times, not once.

    The pass used to walk its whole window in a single transaction, holding every row it
    had rewritten and the conversation advisory lock with them; an upload waited 122 s
    behind one on 2026-09-03.  Chunking must not change what the pass decides.
    """
    from sqlalchemy import event

    every = 2
    chunked_system = _system(session, "Chunked System")
    _pairs_fixture(session, chunked_system, "chunked", pairs=2)  # 2N + 1 = 5 candidates

    commits = 0

    def count(_session) -> None:  # noqa: ANN001
        nonlocal commits
        commits += 1

    event.listen(session, "after_commit", count)
    try:
        chunked = reconcile_batch(session, settings, system="Chunked System", commit_every=every)
    finally:
        event.remove(session, "after_commit", count)

    assert chunked.scanned == 2 * every + 1
    assert chunked.pairs == 2
    assert commits >= 3
    assert chunked.commits == commits

    one_system = _system(session, "One Transaction System")
    _pairs_fixture(session, one_system, "solo", pairs=2)
    single = reconcile_batch(
        session, settings, system="One Transaction System", commit_every=1_000_000
    )
    session.commit()

    assert single.commits == 1
    assert (single.scanned, single.pairs, single.swaps) == (
        chunked.scanned,
        chunked.pairs,
        chunked.swaps,
    )
    assert {
        key.replace("chunked", "X"): (value or "").replace("chunked", "X")
        for key, value in _outcome(session, "chunked").items()
    } == {
        key.replace("solo", "X"): (value or "").replace("solo", "X")
        for key, value in _outcome(session, "solo").items()
    }


# --------------------------------------------------------------------------------------
# Distinct transmissions are never folded together
# --------------------------------------------------------------------------------------


def _key_up(  # noqa: PLR0913
    start_offset: float,
    duration: float,
    *,
    tgids: tuple[int, ...] = (1701,),
    freq: int | None = 852137500,
    radio: int | None = None,
    time_source: str | None = None,
) -> Interval:
    started = BASE + timedelta(seconds=start_offset)
    return Interval(
        started_at=started,
        ended_at=started + timedelta(seconds=duration),
        duration_s=duration,
        talkgroup_ids=tgids,
        frequency_hz=freq,
        source_radio=radio,
        time_source=time_source,
    )


class TestTheFragmentRungNeedsRealOverlap:
    """Rung 4 used to accept anything inside the long call *widened by the 2 s skew*, so a
    reply that began after the call ended counted as a fragment of it (U2)."""

    def test_a_reply_by_another_radio_after_the_call_is_not_a_fragment(self, settings) -> None:  # noqa: ANN001
        cfg = ReconcileConfig.of(settings)
        result = matches(_key_up(0, 8.0, radio=1), _key_up(8.5, 1.2, radio=2), cfg)
        assert not result.matched

    def test_a_key_up_after_the_call_is_not_a_fragment_even_with_no_radio(self, settings) -> None:  # noqa: ANN001
        """Without radios to tell them apart, it is the gap alone that says two calls."""
        cfg = ReconcileConfig.of(settings)
        assert not matches(_key_up(0, 8.0), _key_up(8.5, 1.2), cfg).matched

    def test_a_key_up_just_before_the_call_is_not_a_fragment(self, settings) -> None:  # noqa: ANN001
        cfg = ReconcileConfig.of(settings)
        assert not matches(_key_up(-1.5, 1.0), _key_up(0, 8.0), cfg).matched

    def test_two_radios_never_make_one_fragment(self, settings) -> None:  # noqa: ANN001
        """Half on the air during the call (0.5 s of 1.2 s, under the rung-1 floor), but keyed
        by a different radio: rung 3's guard applies to rung 4 too."""
        cfg = ReconcileConfig.of(settings)
        assert not matches(_key_up(0, 8.0, radio=1), _key_up(7.5, 1.2, radio=2), cfg).matched
        # the same radio (or an unknown one) is still the late-grant fragment it always was
        assert matches(_key_up(0, 8.0, radio=1), _key_up(7.5, 1.2, radio=1), cfg).rule == (
            "fragment inside"
        )
        assert matches(_key_up(0, 8.0, radio=1), _key_up(7.5, 1.2), cfg).rule == "fragment inside"

    def test_an_undecoded_source_is_not_a_different_radio(self, session, settings) -> None:  # noqa: ANN001
        """Radio 0 is "the receiver did not decode the source", not a radio of its own: a
        fragment filed under it is still the copy of a call another receiver decoded."""
        from app.ingest.service import upsert_radio

        system_id = _system(session)
        nobody = upsert_radio(session, system_id, radio_id=0, wacn=0, home_system=0, seen_at=BASE)
        talker = upsert_radio(session, system_id, radio_id=5200005, wacn=0, home_system=0, seen_at=BASE)
        whole = _call(session, system_id=system_id, time_source="upload", path="synthetic/r0-whole.mp3")
        fragment = _call(
            session, system_id=system_id, started_at=BASE + timedelta(seconds=7.5), duration=1.2,
            time_source="upload", path="synthetic/r0-fragment.mp3",
        )
        whole.radio_id, fragment.radio_id = talker, nobody
        session.commit()

        other, result = find_match(session, fragment, ReconcileConfig.of(settings))
        assert other is not None and other.id == whole.id
        assert result.rule == "fragment inside"

        # a *decoded* different radio in the same place is refused
        someone = upsert_radio(session, system_id, radio_id=5245024, wacn=0, home_system=0, seen_at=BASE)
        fragment.radio_id = someone
        session.commit()
        other, _ = find_match(session, fragment, ReconcileConfig.of(settings))
        assert other is None

    def test_a_late_stamped_sdrtrunk_fragment_still_matches(self, settings) -> None:  # noqa: ANN001
        """The one offset the archive knows: a ``file_write`` copy sits 1.7 s late.

        sdrtrunk caught the last 1.2 s of an 8 s trunkd call; its stamp puts that at
        [8.5, 9.7], after the call ended — but on the air it was [6.8, 8.0], inside it.
        """
        cfg = ReconcileConfig.of(settings)
        result = matches(
            _key_up(0, 8.0, time_source="upload"),
            _key_up(8.5, 1.2, time_source="file_write"),
            cfg,
        )
        assert result.matched
        assert result.rule == "fragment inside"

    def test_the_offset_is_not_slack_in_the_other_direction(self, settings) -> None:  # noqa: ANN001
        """A trunkd key-up after an sdrtrunk call ended is further away on the air, not closer."""
        cfg = ReconcileConfig.of(settings)
        result = matches(
            _key_up(0, 8.0, time_source="file_write"),
            _key_up(8.5, 1.2, time_source="upload"),
            cfg,
        )
        assert not result.matched


class TestTheFrequencyKey:
    """A shared traffic frequency is the channel only for a copy with no talkgroup (U3)."""

    def test_two_talkgroups_on_one_frequency_are_two_calls(self, settings) -> None:  # noqa: ANN001
        cfg = ReconcileConfig.of(settings)
        result = matches(
            _key_up(0, 8.0, tgids=(100,)), _key_up(8.3, 1.0, tgids=(200,)), cfg
        )
        assert not result.matched
        assert result.rule == "different channel"

    def test_two_short_key_ups_on_two_talkgroups_are_two_calls(self, settings) -> None:  # noqa: ANN001
        """Rung 3 would have taken these on the frequency alone: 0.6 s each, 0.9 s apart."""
        cfg = ReconcileConfig.of(settings)
        result = matches(
            _key_up(0, 0.6, tgids=(100,)), _key_up(0.9, 0.6, tgids=(200,)), cfg
        )
        assert not result.matched
        assert result.rule == "different channel"

    def test_overlapping_calls_on_two_talkgroups_are_two_calls(self, settings) -> None:  # noqa: ANN001
        cfg = ReconcileConfig.of(settings)
        assert not matches(
            _key_up(0, 6.0, tgids=(100,)), _key_up(0.2, 6.0, tgids=(200,)), cfg
        ).matched


class TestTheExactRungChecksKindSystemAndChannel:
    """Byte-identical voiceless recordings inside the 60 s window (U4)."""

    SQUELCH = "c" * 64

    def test_a_unit_call_is_not_a_copy_of_a_group_call_with_the_same_bytes(
        self, session, settings  # noqa: ANN001
    ) -> None:
        from app.ingest.service import upsert_radio

        system_id = _system(session)
        group = _call(
            session, system_id=system_id, duration=1.7, tgids=(2,), sha256=self.SQUELCH,
            path="synthetic/squelch-group.mp3",
        )
        caller = upsert_radio(session, system_id, radio_id=5238001, wacn=0, home_system=0, seen_at=BASE)
        callee = upsert_radio(session, system_id, radio_id=5238109, wacn=0, home_system=0, seen_at=BASE)
        unit = _call(
            session, system_id=system_id, started_at=BASE + timedelta(seconds=4), duration=1.7,
            tgids=(), sha256=self.SQUELCH, path="synthetic/squelch-unit.mp3",
        )
        unit.kind = "unit"
        unit.talkgroup_id = None
        unit.radio_id, unit.target_radio_id = caller, callee
        session.commit()

        cfg = ReconcileConfig.of(settings)
        other, result = find_match(session, unit, cfg)
        assert other is None
        assert result.rule != "identical sha256"
        # …and the other way round: the group call does not find the unit call either
        other, result = find_match(session, group, cfg)
        assert other is None

    def test_the_same_bytes_on_another_talkgroup_are_not_a_copy(self, session, settings) -> None:  # noqa: ANN001
        system_id = _system(session)
        _call(
            session, system_id=system_id, duration=1.7, tgids=(2,), sha256=self.SQUELCH,
            path="synthetic/squelch-a.mp3",
        )
        second = _call(
            session, system_id=system_id, started_at=BASE + timedelta(seconds=20), duration=1.7,
            tgids=(1701,), frequency_hz=851000000, sha256=self.SQUELCH,
            path="synthetic/squelch-b.mp3",
        )
        session.commit()
        other, _ = find_match(session, second, ReconcileConfig.of(settings))
        assert other is None

    def test_the_same_bytes_in_another_system_are_not_a_copy(self, session, settings) -> None:  # noqa: ANN001
        _call(session, duration=1.7, sha256=self.SQUELCH, path="synthetic/squelch-sys-a.mp3")
        second = _call(
            session, system_id=_system(session, "Another Network"), duration=1.7,
            started_at=BASE + timedelta(seconds=20), sha256=self.SQUELCH,
            path="synthetic/squelch-sys-b.mp3",
        )
        session.commit()
        other, _ = find_match(session, second, ReconcileConfig.of(settings))
        assert other is None

    def test_the_same_bytes_on_the_same_talkgroup_are_still_a_copy(self, session, settings) -> None:  # noqa: ANN001
        """A retransmitted file: same talkgroup, 20 s apart — the rung's reason to exist."""
        first = _call(session, duration=1.7, sha256=self.SQUELCH, path="synthetic/retx-a.mp3")
        second = _call(
            session, started_at=BASE + timedelta(seconds=20), duration=1.7,
            sha256=self.SQUELCH, path="synthetic/retx-b.mp3",
        )
        session.commit()
        other, result = find_match(session, second, ReconcileConfig.of(settings))
        assert other is not None and other.id == first.id
        assert result.rule == "identical sha256"


class TestStaleRowsAfterTheLock:
    """``lock_calls`` selects ids only; the ruling must see the rows as they are now (U5)."""

    def test_a_demotion_between_match_and_lock_is_not_overwritten(self, session, settings) -> None:  # noqa: ANN001
        from sqlalchemy import text

        from app.ingest.reconcile import reconcile_pair

        a = _call(session, ingest_source="trunkd", time_source="upload", path="synthetic/u5-a.mp3")
        b = _call(
            session, started_at=BASE + timedelta(seconds=1.8), path="synthetic/u5-b.mp3"
        )
        c = _call(
            session, started_at=BASE + timedelta(seconds=0.2), ingest_source="trunkd",
            time_source="upload", path="synthetic/u5-c.mp3",
        )
        session.commit()
        # A concurrent batch pass demotes B behind A, behind the ORM's back: the session
        # still believes B is a primary.
        session.execute(
            text("UPDATE calls SET duplicate_of = :a WHERE id = :b"), {"a": a.id, "b": b.id}
        )
        assert b.duplicate_of is None

        outcome = reconcile_pair(session, c, b, settings)
        session.commit()

        assert outcome.matched
        primaries = [row for row in (a, b, c) if row.duplicate_of is None]
        assert len(primaries) == 1, "one transmission, one primary"
        assert b.duplicate_of == primaries[0].id
        assert {a.id, c.id} >= {primaries[0].id}

    def test_the_batch_lock_never_waits_while_holding(self, session, settings) -> None:  # noqa: ANN001
        """A key held elsewhere makes the try fail at once instead of blocking."""
        from sqlalchemy import text

        from app.db import engine
        from app.ingest.reconcile import RECONCILE_LOCK_CLASS, _lock_pair

        a = _call(session, path="synthetic/lock-a.mp3")
        b = _call(session, started_at=BASE + timedelta(seconds=1), path="synthetic/lock-b.mp3")
        session.commit()
        with engine.connect() as other_conn, other_conn.begin():
            other_conn.execute(
                text("SELECT pg_advisory_xact_lock(:cls, :key)"),
                {"cls": RECONCILE_LOCK_CLASS, "key": 1701},
            )
            assert _lock_pair(session, a, b) is False
        session.rollback()
        assert _lock_pair(session, a, b) is True
        session.rollback()
