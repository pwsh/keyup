"""The retro-flag pass over transcripts stored before the pattern families existed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.flag_hallucinations import (
    PREFILTER_FRAGMENTS,
    SHORT_TEXT_PREFILTER_CHARS,
    flag_hallucinations,
)
from app.models import ActivityEvent, Call, System, Talkgroup, Transcript
from tests.conftest import requires_db
from tests.stt.test_postprocess import (
    CREDIT_FRAME_HALLUCINATIONS,
    CREDIT_FRAME_SPEECH,
    LONG_NEAR_MISSES,
    LONG_OUTRO_HALLUCINATIONS,
    SIGNOFF_HALLUCINATIONS,
    SIGNOFF_SPEECH,
    site_rewrites,  # noqa: F401 - a fixture
)

pytestmark = requires_db


def seed(
    session,  # noqa: ANN001
    text: str,
    *,
    days_ago: int = 1,
    quality: str = "good",
    duration: float = 2.0,
    engine_text: str | None = None,
    segments: list[str] | None = None,
    no_speech: float | None = None,
    edited: bool = False,
) -> Call:
    """A call and its primary transcript.  ``text`` is what the call shows; the engine said
    ``engine_text`` (default: the same), and ``edited`` makes ``text`` a person's correction."""
    system = session.execute(select(System).where(System.name == "Example System")).scalar_one_or_none()
    if system is None:
        system = System(name="Example System", protocol="P25 Phase 1")
        session.add(system)
        session.flush()
    talkgroup = session.execute(
        select(Talkgroup).where(Talkgroup.system_id == system.id, Talkgroup.tgid == 1101)
    ).scalar_one_or_none()
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=system.id, tgid=1101, alias="EC EVENT 6", priority=100)
        session.add(talkgroup)
        session.flush()

    started = datetime.now(UTC) - timedelta(days=days_ago)
    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[1101],
        recorded_at=started,
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=duration),
        duration_s=Decimal(str(duration)),
        file_path=f"20260829/EC EVENT 6/{abs(hash(text)) % 10**9}-{days_ago}-{duration}-{no_speech}.mp3",
        file_name="call.mp3",
        talkgroup_alias_at_record="EC EVENT 6",
        transcript_status="done",
        transcript_text=text,
        transcript_quality=quality,
    )
    session.add(call)
    session.flush()
    session.add(
        Transcript(
            call_id=call.id,
            engine="openai_compatible",
            backend_name="whisper",
            model="Systran/faster-whisper-large-v3",
            language="en",
            text_=engine_text if engine_text is not None else text,
            segments=[
                {"start": 0.0, "end": duration, "text": t, "no_speech_prob": no_speech}
                for t in segments or []
            ],
            no_speech_prob=no_speech,
            quality=quality,
            quality_reasons=["no_model_metrics"],
            is_primary=True,
            edited_by_user=edited,
            edited_text=text if edited else None,
        )
    )
    session.commit()
    return call


def test_flags_the_families_and_leaves_real_traffic_alone(session) -> None:  # noqa: ANN001
    credit = seed(session, "Closed captioning provided by WKNO-Memphis.")
    signoff = seed(session, "Thank you for your attention.")
    real = seed(session, "Engine 1 is clear, thank you for your time on that plate check, 10-4.")
    # matches no prefilter fragment at all: never even reaches Python
    untouched = seed(session, "Riverton 12, show me out at 784 Maple Drive.")

    report = flag_hallucinations(session, dry_run=False, batch_size=2)
    assert report["flagged"] == 2
    assert report["families"] == {"credit": 1, "signoff": 1}

    session.expire_all()
    for call_id in (credit.id, signoff.id):
        row = session.get(Call, call_id)
        assert row.transcript_status == "empty"
        assert row.transcript_text is None
        assert row.transcript_quality == "noise"
        transcript = session.execute(select(Transcript).where(Transcript.call_id == call_id)).scalar_one()
        assert transcript.quality == "noise"
        assert transcript.text_  # the engine's words are never destroyed
        assert any(r.startswith("hallucination_phrase:") for r in transcript.quality_reasons)
        assert "no_model_metrics" in transcript.quality_reasons

    for call_id in (real.id, untouched.id):
        row = session.get(Call, call_id)
        assert row.transcript_status == "done"
        assert row.transcript_text is not None

    events = (
        session.execute(select(ActivityEvent).where(ActivityEvent.kind == "stt.hallucination_backfill"))
        .scalars()
        .all()
    )
    assert events
    assert sum(e.meta["flagged"] for e in events) == 2

    # idempotent: a second pass finds nothing, because an 'empty' call is out of scope
    again = flag_hallucinations(session, dry_run=False, batch_size=2)
    assert again["flagged"] == 0


def test_domain_corruptions_and_long_signoffs_are_left_alone(session, site_rewrites) -> None:  # noqa: ANN001
    """The dry run's false positives: the check runs on normalized text, with the duration.

    627 archive rows were a callsign and a place name written as domains (repaired by the
    deployment's ``STT_TEXT_REWRITES``), and a 4.9 s "116, goodbye." is a unit clearing the
    air — none of them is a caption artifact.
    """
    kept = [
        seed(session, "XCOM.com, go ahead."),
        seed(session, "xcom.com.com.com.com."),
        seed(session, "Xcom.com.au copy"),
        seed(session, "pinevalley.com bus 12 is loading"),
        seed(session, "116, goodbye.", duration=4.9),
        seed(session, "Thank you for your time this morning.", duration=9.0),
    ]
    credit = seed(session, "cc by rev.com")  # a genuine captioner credit, still rejected
    signoff = seed(session, "Goodbye, everyone.", duration=2.0)

    report = flag_hallucinations(session, dry_run=False)
    assert report["flagged"] == 2
    assert report["families"] == {"credit": 1, "signoff": 1}

    session.expire_all()
    for call in kept:
        assert session.get(Call, call.id).transcript_status == "done", call.transcript_text
    assert session.get(Call, credit.id).transcript_status == "empty"
    assert session.get(Call, signoff.id).transcript_status == "empty"


def test_dry_run_counts_without_writing(session) -> None:  # noqa: ANN001
    call = seed(session, "Thanks for watching!")
    report = flag_hallucinations(session, dry_run=True)
    assert report["dry_run"] is True
    assert report["flagged"] == 1
    assert report["families"] == {"phrase": 1}

    session.expire_all()
    assert session.get(Call, call.id).transcript_status == "done"


def test_since_limits_the_window_and_patterns_are_merged(session) -> None:  # noqa: ANN001
    old = seed(session, "Closed captioning by the closed captioning project LLC", days_ago=30)
    new = seed(session, "This was brought to you by the public broadcasting fund.", days_ago=1)

    cutoff = datetime.now(UTC) - timedelta(days=7)
    report = flag_hallucinations(session, dry_run=False, since=cutoff, patterns="brought to you by")
    assert report["flagged"] == 1
    assert report["families"] == {"custom": 1}

    session.expire_all()
    assert session.get(Call, old.id).transcript_status == "done"
    assert session.get(Call, new.id).transcript_status == "empty"


# --------------------------------------------------------------------------------------
# human text and the exact phrase list
# --------------------------------------------------------------------------------------


def test_a_person_s_correction_is_never_judged_or_nulled(session) -> None:  # noqa: ANN001
    """The engine hallucinated; a person typed what was really said, and it happens to be
    "Thank you." on a thirty-second call.  The pass must not touch either."""
    call = seed(
        session,
        "Thank you.",
        duration=30.0,
        engine_text="Thanks for watching!",
        edited=True,
    )

    report = flag_hallucinations(session, dry_run=False)
    assert report["flagged"] == 0

    session.expire_all()
    row = session.get(Call, call.id)
    assert (row.transcript_text, row.transcript_status) == ("Thank you.", "done")
    transcript = session.execute(select(Transcript).where(Transcript.call_id == call.id)).scalar_one()
    assert transcript.quality == "good"


def test_the_engine_text_is_judged_with_its_segments_collapsed(session) -> None:  # noqa: ANN001
    call = seed(
        session,
        "Thank you.",
        duration=30.0,
        engine_text="Thank you. Thank you. Thank you. Thank you. Thank you. Thank you.",
        segments=["Thank you."] * 6,
        no_speech=0.1,
    )

    report = flag_hallucinations(session, dry_run=False)
    assert report["reasons"] == {"phrase:thank you": 1}
    session.expire_all()
    assert session.get(Call, call.id).transcript_status == "empty"


def test_the_exact_phrase_list_reaches_the_check(session) -> None:  # noqa: ANN001
    """The prefilter used to have no fragment for "you" / "thank you" / "okay", so those
    rows never reached Python at all.  The conversational ones are gated as in the worker."""
    you = seed(session, "You.", duration=2.0)
    long_thanks = seed(session, "Thank you.", duration=30.0, no_speech=0.1)
    doubtful_okay = seed(session, "Okay.", duration=1.5, no_speech=0.5)
    real_okay = seed(session, "Okay.", duration=1.5, no_speech=0.05)
    ovms_thanks = seed(session, "Thank you.", duration=1.5)  # no metrics: length alone

    report = flag_hallucinations(session, dry_run=False)
    assert report["flagged"] == 3

    session.expire_all()
    for call in (you, long_thanks, doubtful_okay):
        assert session.get(Call, call.id).transcript_status == "empty"
    for call in (real_okay, ovms_thanks):
        assert session.get(Call, call.id).transcript_status == "done"


def test_the_prefilter_covers_every_exact_phrase() -> None:
    from app.stt.postprocess import CONVERSATIONAL_PHRASES, HALLUCINATION_PHRASES

    for phrase in HALLUCINATION_PHRASES | CONVERSATIONAL_PHRASES:
        by_fragment = any(fragment in phrase for fragment in PREFILTER_FRAGMENTS)
        by_length = len(f"{phrase}.") <= SHORT_TEXT_PREFILTER_CHARS
        assert by_fragment or by_length, phrase


# --------------------------------------------------------------------------------------
# broadcast credits ("This is a production of Example Public Television.")
# --------------------------------------------------------------------------------------


def test_the_prefilter_selects_every_broadcast_credit() -> None:
    """Every frame's examples carry a fragment of their own: none relies on being short."""
    for text, _duration, _frame in CREDIT_FRAME_HALLUCINATIONS:
        assert any(fragment in text.lower() for fragment in PREFILTER_FRAGMENTS), text


def test_flags_broadcast_credits_and_keeps_the_real_traffic(session) -> None:  # noqa: ANN001
    credits = [seed(session, text, duration=duration) for text, duration, _ in CREDIT_FRAME_HALLUCINATIONS]
    real = [seed(session, text, duration=duration) for text, duration in CREDIT_FRAME_SPEECH]

    report = flag_hallucinations(session, dry_run=False, batch_size=7)
    assert report["families"] == {"credit_frame": len(credits)}
    assert report["flagged"] == len(credits)
    assert all(reason.startswith("credit_frame:") for reason in report["reasons"])

    session.expire_all()
    for call in credits:
        row = session.get(Call, call.id)
        assert (row.transcript_status, row.transcript_text, row.transcript_quality) == (
            "empty",
            None,
            "noise",
        ), call.id
        transcript = session.execute(select(Transcript).where(Transcript.call_id == call.id)).scalar_one()
        assert any(r.startswith("hallucination_phrase:credit_frame:") for r in transcript.quality_reasons)
    for call in real:
        assert session.get(Call, call.id).transcript_status == "done"


# --------------------------------------------------------------------------------------
# widened sign-offs ("Thanks for listening.", "Thank you for joining us, and we'll see you
# next time.") and the real closings judged like "Thank you."
# --------------------------------------------------------------------------------------


def test_the_prefilter_selects_every_signoff() -> None:
    """A chained sign-off can run past the length bound, so each needs a fragment."""
    for text, _duration, _pattern in SIGNOFF_HALLUCINATIONS:
        assert any(fragment in text.lower() for fragment in PREFILTER_FRAGMENTS), text


def test_flags_the_widened_signoffs_and_keeps_the_real_traffic(session) -> None:  # noqa: ANN001
    signoffs = [
        seed(session, text, duration=duration, no_speech=0.1) for text, duration, _ in SIGNOFF_HALLUCINATIONS
    ]
    # whisper doubted the audio: a closing judged like "Thank you." is noise
    doubtful = seed(session, "Have a great day.", duration=1.3, no_speech=0.25)
    real = [
        seed(session, text, duration=duration, no_speech=no_speech)
        for text, duration, no_speech in SIGNOFF_SPEECH
    ]
    confident = seed(session, "Have a good day.", duration=1.2, no_speech=0.025)

    report = flag_hallucinations(session, dry_run=False, batch_size=7)
    assert report["families"] == {"phrase": 1, "signoff": len(signoffs)}
    assert report["reasons"]["phrase:have a great day"] == 1

    session.expire_all()
    for call in [*signoffs, doubtful]:
        row = session.get(Call, call.id)
        assert (row.transcript_status, row.transcript_text, row.transcript_quality) == (
            "empty",
            None,
            "noise",
        ), call.id
    for call in [*real, confident]:
        assert session.get(Call, call.id).transcript_status == "done", call.transcript_text


# --------------------------------------------------------------------------------------
# long-call outros, and the calls stuck in ``skipped`` with text on screen
# --------------------------------------------------------------------------------------


def test_flags_pure_outros_on_long_calls_and_keeps_the_near_misses(session) -> None:  # noqa: ANN001
    outros = [seed(session, text, duration=duration, no_speech=0.05) for text, duration in LONG_OUTRO_HALLUCINATIONS]
    near = [seed(session, text, duration=duration, no_speech=0.05) for text, duration in LONG_NEAR_MISSES]

    report = flag_hallucinations(session, dry_run=False, batch_size=5)
    assert report["flagged"] == len(outros)

    session.expire_all()
    for call in outros:
        assert session.get(Call, call.id).transcript_status == "empty", call.transcript_text
    for call in near:
        assert session.get(Call, call.id).transcript_status == "done", call.transcript_text


def _skip(session, call: Call, *, keep_text: bool) -> None:  # noqa: ANN001
    """The September repairs left 356 calls ``skipped`` with their text and label behind."""
    row = session.get(Call, call.id)
    row.transcript_status = "skipped"
    if not keep_text:
        row.transcript_text = None
    session.commit()


def test_a_skipped_call_that_still_shows_text_is_judged(session) -> None:  # noqa: ANN001
    """Production: "Thank you for listening." on 1.5 s and 2.2 s calls, labeled good, never
    flagged because the pass read ``done`` only — while the call list showed the text."""
    shown = seed(session, "Thank you for listening.", duration=1.512, no_speech=0.143)
    _skip(session, shown, keep_text=True)
    # a skipped copy that shows nothing is not this pass's business
    hidden = seed(session, "Thank you for listening.", duration=2.232, no_speech=0.095)
    _skip(session, hidden, keep_text=False)

    report = flag_hallucinations(session, dry_run=False)
    assert report["flagged"] == 1

    session.expire_all()
    row = session.get(Call, shown.id)
    assert (row.transcript_status, row.transcript_text, row.transcript_quality) == ("empty", None, "noise")
    assert session.get(Call, hidden.id).transcript_status == "skipped"
