"""Post-processing heuristics, exercised with the real numbers from whisper_results.json."""

from __future__ import annotations

import pytest

from app.stt.postprocess import (
    CONVERSATIONAL_MAX_DURATION_S,
    CONVERSATIONAL_MIN_DURATION_S,
    CONVERSATIONAL_NO_SPEECH_PROB,
    CREDIT_FRAME_PATTERNS,
    MAX_COMPRESSION_RATIO,
    MAX_CREDIT_FRAME_LEAD_WORDS,
    MAX_CREDIT_FRAME_WORDS,
    MAX_SHORT_CREDIT_CHARS,
    MAX_SIGNOFF_FILLER_WORDS,
    MIN_AVG_LOGPROB,
    NO_SPEECH_THRESHOLD,
    SIGNOFF_FILLER_WORDS,
    SIGNOFF_MAX_DURATION_S,
    SIGNOFF_PATTERNS,
    assess,
    collapse_repeats,
    compile_patterns,
    compile_rewrites,
    conversational_is_noise,
    hallucination_phrase,
    normalize_text,
)
from app.stt.prompt import build_prompt
from app.stt.types import Quality, TranscribeOptions, TranscriptResult, TranscriptSegment


def make_result(segments: list[dict], text: str | None = None, duration: float = 10.0) -> TranscriptResult:
    segs = [TranscriptSegment(**s) for s in segments]
    return TranscriptResult(
        text=text if text is not None else "".join(s.text for s in segs).strip(),
        language="en",
        duration=duration,
        segments=segs,
        engine="openai_compatible",
        backend_name="whisper",
        model="Systran/faster-whisper-large-v3",
        processing_ms=500,
    )


def seg(text: str, start=0.0, end=5.0, logprob=-0.3, no_speech=0.15, ratio=1.2) -> dict:
    return {
        "start": start,
        "end": end,
        "text": text,
        "avg_logprob": logprob,
        "no_speech_prob": no_speech,
        "compression_ratio": ratio,
    }


# --------------------------------------------------------------------------- normalize_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  419 on 23,  this is going to be at 784 Maple Drive. ", "419 on 23, this is going to be at 784 Maple Drive."),
        ("Copy  that\n\nEngine\t1", "Copy that Engine 1"),
        ("[MUSIC] Engine 1 responding (static)", "Engine 1 responding"),
        ("♪♪ Dispatch ♪♪", "Dispatch"),
        ("Show me 10 - 4 on that", "Show me 10-4 on that"),
        ("We’re en route — copy", "We're en route - copy"),
        ("Copy that ,, please..", "Copy that, please."),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


# --------------------------------------------------------------------------- collapse_repeats


def test_collapse_repeats_drops_consecutive_duplicates():
    # From the 44.5 s BOLO rebroadcast: the same sentence twice, differing only in final punctuation.
    kept, dropped = collapse_repeats(
        [
            " Dark in color, Buick four-door sedan,",
            " dark in color, Buick four-door sedan.",
            " Driver, female, black, struck.",
        ]
    )
    assert dropped == 1
    assert kept == [" Dark in color, Buick four-door sedan,", " Driver, female, black, struck."]


def test_collapse_repeats_keeps_non_consecutive_duplicates():
    kept, dropped = collapse_repeats(["A", "B", "A"])
    assert dropped == 0 and kept == ["A", "B", "A"]


def test_collapse_repeats_counts_a_full_loop():
    kept, dropped = collapse_repeats(["Thank you."] * 8)
    assert kept == ["Thank you."] and dropped == 7


def test_collapse_repeats_empty():
    assert collapse_repeats([]) == ([], 0)


# --------------------------------------------------------------------------- assess


def test_good_transcript_from_real_sample():
    """14.99 s EMS dispatch: logprob -0.240, no_speech 0.128, ratio 1.221 -> clean."""
    result = make_result(
        [
            seg(
                " 419 on 23, this is going to be at 784 Maple Drive, will be for a 59-year-old male"
                " subject with back and neck pain and chest pain, is conscious and breathing.",
                end=14.8,
                logprob=-0.24023437669331377,
                no_speech=0.1282958984375,
                ratio=1.2213740458015268,
            )
        ],
        duration=14.9943125,
    )
    outcome = assess(result, 14.9943125, TranscribeOptions())
    assert outcome.quality is Quality.GOOD
    assert outcome.reasons == []
    assert outcome.cleaned_text.startswith("419 on 23,")


def test_empty_text_is_noise():
    """0.42 s squelch tail with vad_filter=true returns nothing at all."""
    result = make_result([], text="", duration=0.418)
    outcome = assess(result, 0.418, TranscribeOptions())
    assert outcome.quality is Quality.NOISE
    assert outcome.reasons == ["empty_text"]
    assert outcome.cleaned_text == ""


def test_whitespace_only_text_is_noise():
    assert assess(make_result([], text="   \n  ")).quality is Quality.NOISE


def test_all_segments_non_speech_is_noise():
    """The same tail with vad_filter=false: 'Thanks for watching!' at no_speech_prob 0.866."""
    result = make_result(
        [seg(" Thanks for watching!", end=29.98, logprob=-0.7505580527441842, no_speech=0.86572265625, ratio=0.714)],
        duration=0.418,
    )
    outcome = assess(result, 0.418, TranscribeOptions())
    assert outcome.quality is Quality.NOISE
    assert f"no_speech_prob>{NO_SPEECH_THRESHOLD}" in outcome.reasons


def test_one_speech_segment_keeps_it_out_of_all_non_speech():
    result = make_result([seg("Engine 1 responding.", no_speech=0.9), seg("Copy.", no_speech=0.1)])
    outcome = assess(result)
    assert f"no_speech_prob>{NO_SPEECH_THRESHOLD}" not in outcome.reasons


@pytest.mark.parametrize(
    "phrase",
    ["Thanks for watching!", "Thank you.", "you", "Bye.", "Subscribe", "Please subscribe.", "[Music]"],
)
def test_known_hallucination_phrase_alone_is_noise(phrase):
    outcome = assess(make_result([seg(phrase, no_speech=0.2)], duration=2.0), 2.0)
    assert outcome.quality is Quality.NOISE
    assert any(r.startswith("hallucination_phrase:") or r == "empty_text" for r in outcome.reasons)


def test_hallucination_phrase_inside_real_traffic_is_kept():
    text = "Thank you, Engine 1 is clear of the scene."
    outcome = assess(make_result([seg(text)], duration=4.0), 4.0)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text == text


def test_compression_ratio_flags_repeat_loop():
    result = make_result([seg("Go ahead. " * 40, ratio=MAX_COMPRESSION_RATIO + 0.5)], duration=30.0)
    outcome = assess(result, 30.0)
    assert outcome.quality is Quality.SUSPECT
    assert "repeat_loop" in outcome.reasons


def test_real_bolo_rebroadcast_stays_under_the_ratio_threshold():
    """The genuine 44.5 s rebroadcast peaks at 2.111, below the 2.4 cut: no repeat_loop."""
    result = make_result([seg("Wanted, leaving the scene of a personal injury accident,", ratio=2.1105527638190953)], duration=44.5)
    assert "repeat_loop" not in assess(result, 44.5).reasons


def test_collapsed_repeats_reported_with_count():
    result = make_result(
        [seg(" Dark in color, Buick four-door sedan,", 0, 2), seg(" dark in color, Buick four-door sedan.", 2, 4)],
        duration=6.0,
    )
    outcome = assess(result, 6.0)
    assert outcome.quality is Quality.SUSPECT
    assert "collapsed_repeats=1" in outcome.reasons
    # the dangling comma left behind by the collapse is trimmed too
    assert outcome.cleaned_text == "Dark in color, Buick four-door sedan"


def test_low_avg_logprob_is_suspect():
    result = make_result([seg("Munjulor tenner bragg.", logprob=MIN_AVG_LOGPROB - 0.2)], duration=5.0)
    outcome = assess(result, 5.0)
    assert outcome.quality is Quality.SUSPECT
    assert "low_avg_logprob" in outcome.reasons


def test_implausible_word_rate_is_suspect():
    text = " ".join(["word"] * 40)  # 40 words in 2 s = 20 w/s
    outcome = assess(make_result([seg(text)], duration=2.0), 2.0)
    assert outcome.quality is Quality.SUSPECT
    assert any(r.startswith("words_per_second=") for r in outcome.reasons)


def test_plausible_word_rate_is_fine():
    text = " ".join(["word"] * 30)  # 30 words in 15 s = 2 w/s
    outcome = assess(make_result([seg(text)], duration=15.0), 15.0)
    assert not any(r.startswith("words_per_second=") for r in outcome.reasons)


def test_duration_argument_wins_over_engine_duration():
    text = " ".join(["word"] * 40)
    result = make_result([seg(text)], duration=600.0)  # engine says 10 minutes
    assert any(r.startswith("words_per_second=") for r in assess(result, 2.0).reasons)


def test_trailing_caption_artifact_is_trimmed():
    """30.3 s fire dispatch picks up ' Thank you for watching.' after the traffic ends at 29.2 s."""
    result = make_result(
        [
            seg(
                " Elm Grove Fire and Pinecrest Fire, EFD 2, Respond to Vegetation Fire, at 120 and 45, southbound,"
                " ditch, cross streets are 45, southbound.",
                0.0,
                29.22,
                logprob=-0.26488096160548075,
                no_speech=0.159423828125,
                ratio=1.2429906542056075,
            ),
            seg(
                " Thank you for watching.",
                30.0,
                59.98,
                logprob=-0.4885253980755806,
                no_speech=0.263427734375,
                ratio=0.7419354838709677,
            ),
        ],
        duration=30.3020625,
    )
    outcome = assess(result, 30.3020625)
    assert outcome.quality is Quality.SUSPECT
    assert "trailing_hallucination" in outcome.reasons
    assert "watching" not in outcome.cleaned_text
    assert outcome.cleaned_text.startswith("Elm Grove Fire and Pinecrest Fire,")


def test_noise_beats_suspect():
    result = make_result([seg("Thanks for watching!", no_speech=0.9, logprob=-1.5)], duration=1.0)
    outcome = assess(result, 1.0)
    assert outcome.quality is Quality.NOISE
    assert len(outcome.reasons) >= 2


def test_assess_tolerates_missing_confidence_fields():
    result = TranscriptResult(
        text="Engine 1 responding.",
        segments=[TranscriptSegment(start=0.0, end=2.0, text="Engine 1 responding.")],
        engine="faster_whisper_local",
        backend_name="local",
        model="Systran/faster-whisper-small",
    )
    outcome = assess(result, 2.0)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text == "Engine 1 responding."


def test_assess_falls_back_to_result_text_without_segments():
    result = make_result([], text="Engine 1 responding.", duration=3.0)
    assert assess(result, 3.0).cleaned_text == "Engine 1 responding."


# --------------------------------------------------------------------------- prompt echo

#: A site's place list (``STT_PLACES``), the tail of the prompt a decoder echoes.
PLACES = ["Riverton", "Lakeside", "North Fork", "Millbrook", "Pinecrest", "Elm Grove", "Greendale", "Ashby", "Fairview Heights", "Example County"]


def test_prompt_echo_is_noise():
    """Observed on a 3.1 s clip: whisper continued its own initial_prompt instead of decoding."""
    prompt = build_prompt(system_name="Example System Example County", places=PLACES)
    echoed = "Millbrook Pinecrest Elm Grove, Greendale, Ashby, Fairview Heights, Example County."
    outcome = assess(make_result([seg(echoed)], duration=3.11), 3.11, TranscribeOptions(prompt=prompt))
    assert outcome.quality is Quality.NOISE
    assert "prompt_echo" in outcome.reasons


def test_prompt_echo_needs_a_prompt():
    echoed = "Millbrook Pinecrest Elm Grove, Greendale, Ashby, Fairview Heights, Example County."
    outcome = assess(make_result([seg(echoed)], duration=3.11), 3.11, TranscribeOptions())
    assert "prompt_echo" not in outcome.reasons


def test_real_traffic_sharing_prompt_words_is_not_an_echo():
    """Place and unit names really are spoken on air; only the prompt's word *order* is a tell."""
    prompt = build_prompt(
        system_name="Example System Example County", unit_aliases=["Engine 1", "Medic 21"], places=PLACES
    )
    text = "Engine 1 responding to Lakeside from North Fork on the interstate."
    outcome = assess(make_result([seg(text)], duration=6.0), 6.0, TranscribeOptions(prompt=prompt))
    assert "prompt_echo" not in outcome.reasons
    assert outcome.quality is Quality.GOOD


def test_short_prompt_word_run_is_not_an_echo():
    prompt = build_prompt(system_name="Example System Example County", places=PLACES)
    outcome = assess(make_result([seg("North Fork.")], duration=2.0), 2.0, TranscribeOptions(prompt=prompt))
    assert "prompt_echo" not in outcome.reasons


def test_closed_captioning_artifact_is_noise():
    """Observed on a 0.60 s clip in the 40-file benchmark."""
    text = "Closed captioning by The Closed Captioning Project llc"
    outcome = assess(make_result([seg(text, logprob=-0.9)], duration=0.6), 0.6)
    assert outcome.quality is Quality.NOISE


# --------------------------------------------------------------------------- pattern families


@pytest.mark.parametrize(
    ("text", "family"),
    [
        # credit — rejected wherever they occur
        ("Closed captioning provided by WKNO-Memphis.", "credit"),
        ("Captioning provided by W-K-N-O, Memphis", "credit"),
        ("Closed captioning by Kris Brandhagen. brandhagen.com", "credit"),
        ("Captioning provided by Aeverine Zinn Digital Media Group", "credit"),
        ("UOFI MAINT 3, closed captioning provided by WKNO-Memphis.", "credit"),
        ("Subtitles by the SubMaster team", "credit"),
        ("Transcribed by ESO, translated by --", "credit"),
        ("Transcription by CastingWords LLC", "credit"),
        ("Subtitles by the Amara.org community!", "credit"),
        ("Visit www.mooji.org for more", "credit"),
        ("Copyright 2023 by the broadcaster", "credit"),
        ("All rights reserved.", "credit"),
        (
            "This has been a presentation of the Center for Autism and Related Disorders",
            "credit",
        ),
        # The same credit read for a different sponsor, and mid-utterance: the frame is
        # what condemns it, not the name that follows.
        ("53-01, this has been a presentation of the Ohio Channel.", "credit"),
        ("gpknives.com", "credit"),  # .com, under the short-text limit
        # sign-off — rejected only as the whole utterance
        ("Thank you for watching!", "phrase"),  # also on the exact list
        ("Alright, thanks so much for watching, everyone.", "signoff"),
        ("Thank you for listening.", "signoff"),
        ("Okay, thank you for your attention.", "signoff"),
        ("Thank you for joining us today.", "signoff"),
        ("And see you next time!", "signoff"),
        ("See you in the next one.", "signoff"),
        ("Don't forget to like and subscribe.", "signoff"),
        ("Bye bye.", "phrase"),
        ("Goodbye, everyone.", "signoff"),
    ],
)
def test_hallucination_families_are_rejected(text, family):
    reason = hallucination_phrase(text, None, 2.0)
    assert reason is not None, text
    assert reason.split(":", 1)[0] == family, reason
    outcome = assess(make_result([seg(text, no_speech=0.2)], duration=2.0), 2.0)
    assert outcome.quality is Quality.NOISE
    assert f"hallucination_phrase:{reason}" in outcome.reasons


@pytest.mark.parametrize(
    "text",
    [
        # dispatch speech that merely contains a family's words
        "Engine 1 is clear, and thank you for your time getting me that plate on the Buick.",
        "Dispatch, we are at the end of Main Street at the railroad crossing, no traffic.",
        "Copy, tell him to subscribe to the county alert page and call back if it happens again.",
        "He said he would see you next time he is on shift, we are clear of the residence.",
        "10-4, the reporting party is listening on a scanner and thanks us for watching the house.",
    ],
)
def test_real_traffic_containing_family_words_is_kept(text):
    assert hallucination_phrase(text, None, 12.0) is None, text
    outcome = assess(make_result([seg(text)], duration=12.0), 12.0)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text == text


def test_dot_com_only_condemns_short_text():
    assert hallucination_phrase("tylerbites.com") is not None
    long_text = "Registration comes back to a business, the website on the door is tylerbites.com, standby."
    assert len(long_text) >= MAX_SHORT_CREDIT_CHARS
    assert hallucination_phrase(long_text) is None


def test_signoff_needs_the_whole_utterance():
    assert hallucination_phrase("Okay then, thank you for watching.", None, 2.0) is not None
    filler = " ".join(["copy"] * (MAX_SIGNOFF_FILLER_WORDS + 2))
    assert hallucination_phrase(f"{filler} thank you for watching", None, 2.0) is None


@pytest.mark.parametrize(
    ("text", "duration"),
    [
        # a 4.9 s "116, goodbye." is a unit signing off the air, not a caption artifact
        ("116, goodbye.", 4.9),
        ("EC law 4, thank you for your time.", 3.0),
        # long, and something besides the outro in it: a person
        ("Thank you for your time, sir, and thank you both.", 5.1),
        ("All is well, thank you for watching.", 6.5),
        ("Goodbye, everyone.", SIGNOFF_MAX_DURATION_S),
        # a duration nobody recorded is not evidence either
        ("Goodbye, everyone.", None),
    ],
)
def test_signoff_family_is_gated_on_duration_and_digits(text, duration):
    """Sign-offs are things people say: with filler around them only a squelch-tail-length
    call with no unit number in it is a hallucination, and a longer one only when it is a
    pure outro (the first two are the kind of thing a person says on the air)."""
    assert hallucination_phrase(text, None, duration) is None
    result = make_result([seg(text)], duration=duration or 10.0)
    assert assess(result, duration).quality is not Quality.NOISE


def test_signoff_gates_do_not_reach_the_credit_family():
    """A credit line is a credit line at any length, with any number in it."""
    text = "Copyright 2023 WKNO-Memphis, all rights reserved, 52 units standing by."
    assert hallucination_phrase(text, None, 60.0) == "credit:copyright"
    assert assess(make_result([seg(text)], duration=60.0), 60.0).quality is Quality.NOISE


# --------------------------------------------------------------------------- widened sign-offs
# Short, digit-free whisper outros that slipped past the original patterns, at the short
# durations they turn up on.

#: (text, call duration, index into SIGNOFF_PATTERNS)
SIGNOFF_HALLUCINATIONS: list[tuple[str, float, int]] = [
    # two sign-offs chained — the filler limit used to fail them
    ("Thank you for watching, and I'll see you next time.", 1.9, 0),
    ("Thank you for watching, and I'll see you next time.", 4.9, 0),
    ("Thank you for watching, and we'll see you next time.", 2.2, 0),
    ("Thank you for joining us, and we'll see you next time.", 1.7, 1),
    ("Thank you for watching, and I'll see you in the next video.", 2.2, 0),
    ("Thank you for watching. We'll see you next time.", 1.7, 0),
    ("Thank you so much for joining us, and we'll see you next time.", 2.4, 1),
    ("Thank you for watching, and see you next time.", 2.2, 0),
    # …and with a closing chained on
    ("Thank you for watching, and have a great day.", 1.9, 0),
    ("Thank you for watching and have a great day.", 1.9, 0),
    ("Thank you for listening and have a great day.", 1.9, 1),
    ("Thank you for your attention and have a great weekend.", 2.2, 1),
    ("Thank you for watching. We'll see you next time. Good night.", 1.3, 0),
    ("Thank you for watching, and I'll see you next time. Bye!", 1.4, 0),
    # "thanks" and an addressee — the pattern spelled out only "thank you"
    ("Thank you for tuning in.", 1.7, 1),
    ("Thanks for listening.", 1.7, 1),
    ("Thanks for your time.", 1.7, 1),
    ("Thanks for joining us.", 1.7, 1),
    ("Thank you all for joining us.", 1.7, 1),
    ("Thank you all for tuning in.", 1.5, 1),
    ("Thank you guys for watching.", 1.5, 1),
    # "see you" with an addressee, or "on the next one"
    ("See you guys next time.", 1.9, 2),
    ("I'll see you guys next time.", 1.9, 2),
    ("See you on the next one.", 1.3, 2),
]

#: (text, call duration, no_speech_prob) — what a person saying something near these phrases looks like
SIGNOFF_SPEECH: list[tuple[str, float, float]] = [
    # a unit number or ten-code: a person was talking
    ("10-4, thanks for listening.", 1.5, 0.05),
    ("10-1, I'll see you in the next one.", 1.5, 0.05),
    ("We'll see you next week, Riverton 10-8, 5-0.", 2.1, 0.05),
    ("10-4, have a good night.", 2.4, 0.05),
    ("ECOM 73, back at the station, you guys have a good night.", 3.9, 0.05),
    ("Take care, Engine 3, back in service.", 2.4, 0.05),
    ("No, thank you. Take care, Engine 3. Engine 3 is back to service.", 4.0, 0.05),
    ("No further questions. We'll see you in 12. Thank you.", 5.3, 0.002),
    # closings with no sign-off in them: they never condemn on their own
    ("Copy. Have a good night.", 2.2, 0.013),
    ("Thank you. Have a good day.", 1.7, 0.021),
    ("Copy that. Thank you. Have a good day.", 1.7, 0.008),
    ("See you around.", 1.7, 0.023),
    ("Bye, everybody.", 2.1, 0.022),
    ("Thanks for letting me know.", 1.6, 0.004),
    ("Yep, I'll take care of that.", 1.4, 0.022),
    # long, and a word besides the outro: kept (the long-call rule wants a pure outro)
    ("Thank you for your time this morning.", 6.9, 0.078),
    ("Okay, don't worry about it. I'll swing down here in a minute and take care of it.", 6.5, 0.017),
    # sign-offs a person says: what is left beside the sign-off is a name, a unit, "sir",
    # "go ahead" or "we're out" — not on SIGNOFF_FILLER_WORDS (invented names throughout)
    ("Thank you for listening, Riverton, go ahead.", 2.377, 0.083),
    ("Lakeside out, thanks for your time.", 1.692, 0.034),
    ("Thank you for your time, and we're out.", 2.128, 0.051),
    ("Thank you for your time, and go ahead.", 1.954, 0.031),
    ("Chief, thanks for your time, thank you very much.", 2.56, 0.120),
    ("Thank you for your time, sir. Take care.", 4.728, 0.029),
    ("Thank you for your time, sir. Ashby, please.", 3.108, 0.069),
    ("Thanks for your time, Alex.", 1.672, 0.052),
    ("Thanks for your time, Sam.", 1.2, 0.036),
    ("Thanks for your time, Robin.", 1.603, 0.060),
    ("Thanks for your time, Casey.", 2.294, 0.018),
    ("Thanks for your time, Jordan.", 1.852, 0.022),
    ("Thanks for your time, Quinn.", 1.515, 0.044),
    ("Thanks for your time, Morgan.", 1.855, 0.052),
    ("Thanks for your time, sir.", 1.194, 0.051),
    ("Thanks, Riley. Thanks for your time, Avery.", 1.855, 0.018),
    ("Thank you for your time, Taylor. Sounds good.", 1.174, 0.041),
    ("Jamie, thank you very much for your time.", 2.952, 0.086),
    ("Thank you very much for your time. Thank you, Millbrook.", 3.108, 0.070),
    ("OK, Pat Example, have a nice day, bye bye.", 2.592, 0.030),
    ("Thank you for listening, I'm coming down.", 1.855, 0.092),
]


@pytest.mark.parametrize(("text", "duration", "pattern"), SIGNOFF_HALLUCINATIONS)
def test_widened_signoffs_are_rejected(text, duration, pattern):
    reason = hallucination_phrase(text, None, duration, no_speech_prob=0.1)
    assert reason == f"signoff:{SIGNOFF_PATTERNS[pattern].pattern[:80]}", (text, reason)
    outcome = assess(make_result([seg(text, end=duration, no_speech=0.1)], duration=duration), duration)
    assert outcome.quality is Quality.NOISE
    assert f"hallucination_phrase:{reason}" in outcome.reasons


@pytest.mark.parametrize(("text", "duration", "no_speech"), SIGNOFF_SPEECH)
def test_real_traffic_near_the_signoffs_is_kept(text, duration, no_speech):
    assert hallucination_phrase(text, None, duration, no_speech_prob=no_speech) is None, text
    outcome = assess(make_result([seg(text, end=duration, no_speech=no_speech)], duration=duration), duration)
    assert outcome.quality is not Quality.NOISE
    assert outcome.cleaned_text == text


def test_chained_signoffs_share_one_filler_allowance():
    """Filler is counted over the whole utterance, wherever it sits between the phrases."""
    filler = ["okay", "and", "everyone", "again"]
    for n in range(len(filler) + 1):
        words = filler[:n] + [""] * (len(filler) - n)
        text = f"{words[0]} thank you for watching {words[1]} see you next time {words[2]} {words[3]}"
        reason = hallucination_phrase(text, None, 2.0)
        assert (reason is not None) is (n <= MAX_SIGNOFF_FILLER_WORDS), (n, text, reason)


@pytest.mark.parametrize(
    "text",
    [
        # the big hallucinated families keep going under the closed filler vocabulary
        "Thank you for watching, and I'll see you next time.",
        "Thank you for watching, and we'll see you next time.",
        "Thank you for joining us, and we'll see you next time.",
        "Thank you for watching and have a great day.",
        "Thank you all for joining us.",
        "See you guys next time.",
        "Thanks for your time.",
        "Thank you for tuning in.",
        # the talk-show outro is a closing, not filler
        "Thank you for your time. Thank you for having me.",
    ],
)
def test_the_big_signoff_families_survive_the_filler_vocabulary(text):
    assert hallucination_phrase(text, None, 2.0, no_speech_prob=0.1) is not None, text


def test_filler_must_come_from_the_closed_vocabulary():
    """Three words of filler are allowed only if every one is on SIGNOFF_FILLER_WORDS."""
    assert {"and", "i'll", "we'll", "okay", "everyone", "again"} <= SIGNOFF_FILLER_WORDS
    assert not SIGNOFF_FILLER_WORDS & {"sir", "ma'am", "chief", "go", "ahead", "out", "copy", "over"}
    assert hallucination_phrase("Okay, thanks for your time, everyone.", None, 2.0) is not None
    for word in ("sir", "Alex", "copy", "over", "out"):
        assert hallucination_phrase(f"Okay, thanks for your time, {word}.", None, 2.0) is None, word
    # the price: hallucinations whose leftover carries words
    # nothing vouches for are kept ("First of all, …")
    assert hallucination_phrase("First of all, thank you so much for joining us.", None, 1.5) is None
    # a bare talk-show line is still only a closing
    assert hallucination_phrase("Thank you for having me.", None, 1.5) is None


def test_closings_alone_never_condemn():
    """Units say "Take care." and "Have a good day."; chained to each other they are still
    only closings, and the sign-off family needs a sign-off in the text."""
    for text in ("Thank you. Have a good day. Take care.", "See you later, bye.", "Good night, take care."):
        assert hallucination_phrase(text, None, 2.0, no_speech_prob=0.1) is None, text


def test_a_signoff_word_inside_a_longer_word_is_not_a_signoff():
    """Matches are whole words: "subscribers" is not "subscribe", "Thanksgiving" not "thanks"."""
    assert hallucination_phrase("Subscribers.", None, 1.5) is None
    assert hallucination_phrase("Thanksgiving for watching.", None, 1.5) is None


def test_the_end_mid_sentence_is_held_by_the_total_filler_limit():
    """The old per-side limit let "the end" carry three words either side, and flagged
    "That's the end of the call."; four words of filler in total is a
    sentence."""
    assert hallucination_phrase("That's the end of the call.", None, 1.7) is None
    assert hallucination_phrase("And so, the end.", None, 1.7) == "signoff:the end"


# --------------------------------------------------------------------------- long-call outros
# Calls over 5 s: text that is nothing but an outro is whisper writing over noise at any
# length; one other word and it is a person.

#: (text, call duration) — whisper outros, at long durations they turn up on
LONG_OUTRO_HALLUCINATIONS: list[tuple[str, float]] = [
    ("Thank you for watching, and I'll see you next time.", 42.4),
    ("Thank you for joining us.", 26.7),
    ("Thank you for your attention.", 15.8),
    ("Thank you for watching, and I'll see you in the next video.", 49.6),
    ("Thank you for your time. Thank you.", 10.2),
    ("Thank you for your time.", 10.1),
    ("Thank you for listening.", 5.2),
    ("Thank you for watching, and we'll see you next time.", 19.5),
    ("Thank you for joining us, and we'll see you next time.", 11.7),
    ("Thank you for watching, and have a great day.", 6.5),
    ("Thank you for tuning in.", 5.6),
    ("Thanks for tuning in.", 8.2),
    ("Thank you for watching. We'll see you next time.", 6.6),
    ("Thank you very much for joining us. Thank you, thank you.", 40.6),
    ("Thank you for your time. Thank you for having me.", 5.6),
    ("I'll see you next time. Thank you.", 5.1),
    ("Thank you for joining us. We'll see you next time.", 30.6),
]

#: (text, call duration) — near-misses over 5 s: a word or three that is not
#: part of the outro, which the long-call rule must keep
LONG_NEAR_MISSES: list[tuple[str, float]] = [
    ("Thank you for joining us today.", 20.9),
    ("Thank you for joining us today, and we'll see you next time.", 28.3),
    ("Thank you for your time, sir, and thank you both.", 5.1),
    ("Thank you for listening to Radio Fire.", 13.6),
    ("Thank you for listening to Just Call.", 10.5),
    ("Thank you for joining us. License orders.", 31.9),
    ("Thank you for joining us for Dispatch.", 5.1),
    ("See you on the next phone call.", 5.6),
    ("Thank you, gentlemen. See you on the next one.", 5.3),
    ("All right. Thank you for watching.", 5.6),
    ("Thank you for your time. Thank you. Bye. Go back.", 5.3),
    ("Thank you for tuning in and helping out.", 12.3),
    ("Thank you for your time right now.", 5.6),
]


@pytest.mark.parametrize(("text", "duration"), LONG_OUTRO_HALLUCINATIONS)
def test_a_pure_outro_is_rejected_on_a_long_call(text, duration):
    reason = hallucination_phrase(text, None, duration, no_speech_prob=0.05)
    assert reason is not None and reason.startswith(("signoff:", "phrase:")), (text, reason)
    outcome = assess(make_result([seg(text, end=duration, no_speech=0.05)], duration=duration), duration)
    assert outcome.quality is Quality.NOISE


@pytest.mark.parametrize(("text", "duration"), LONG_NEAR_MISSES)
def test_a_long_call_with_any_other_word_is_kept(text, duration):
    assert hallucination_phrase(text, None, duration, no_speech_prob=0.05) is None, text
    outcome = assess(make_result([seg(text, end=duration, no_speech=0.05)], duration=duration), duration)
    assert outcome.quality is not Quality.NOISE


def test_the_long_rule_allows_no_filler_but_the_connectors_in_place():
    """Over 5 s the filler allowance is gone; "and" / "I'll" count only where they join."""
    assert hallucination_phrase("Okay, thanks for listening.", None, 2.0) is not None
    assert hallucination_phrase("Okay, thanks for listening.", None, 8.0) is None
    assert hallucination_phrase("Thanks for listening, everyone.", None, 8.0) is None
    assert hallucination_phrase("Thanks for listening, and I'll see you next time.", None, 8.0) is not None
    # "and" with nothing after it to join, or "I'll" not before "see you", is a word
    assert hallucination_phrase("Thanks for listening, and.", None, 8.0) is None
    assert hallucination_phrase("I'll thank you for your time.", None, 8.0) is None
    # the digit guard holds at every length
    assert hallucination_phrase("Thank you for your time, 10-4.", None, 8.0) is None


def test_with_the_duration_unknown_the_signoff_family_does_not_vote():
    """None is how the trailing-segment check asks for the families that hold at any length."""
    for text in ("Thank you for your time.", "Thank you for watching, and I'll see you next time."):
        assert hallucination_phrase(text, None, None) is None
        assert hallucination_phrase(text, None, 0.0) is None


def test_a_real_call_ending_with_a_signoff_segment_keeps_it():
    """The long-call rule judges a whole call, not its last segment."""
    result = make_result(
        [
            seg("Engine 1, respond to 784 Maple Drive, caller is on scene.", end=7.0, no_speech=0.05),
            seg(" Thank you for your time.", start=7.0, end=8.5, no_speech=0.05),
        ],
        duration=8.5,
    )
    outcome = assess(result, 8.5)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text.endswith("Thank you for your time.")
    assert "trailing_hallucination" not in outcome.reasons


@pytest.mark.parametrize(
    ("text", "duration", "no_speech"),
    [
        # whisper sure it heard a person (low no_speech_prob values)
        ("Have a good day.", 1.2, 0.025),
        ("Have a great day.", 1.3, 0.008),
        ("Have a good night.", 1.4, 0.015),
        ("Take care.", 1.7, 0.010),
        ("Thank you.", 1.3, 0.02),
    ],
)
def test_real_closings_are_judged_like_thank_you(text, duration, no_speech):
    """Kept on confident audio — labeled ``suspect``, as a bare "Thank you." is — and
    rejected only when whisper doubts the audio or the call is far too long for the words."""
    assert hallucination_phrase(text, None, duration, no_speech_prob=no_speech) is None
    outcome = assess(make_result([seg(text, end=duration, no_speech=no_speech)], duration=duration), duration)
    assert outcome.quality is Quality.SUSPECT
    assert any(r.startswith("conversational_phrase:") for r in outcome.reasons)

    key = text.lower().rstrip(".")
    doubtful = hallucination_phrase(text, None, duration, no_speech_prob=CONVERSATIONAL_NO_SPEECH_PROB)
    assert doubtful == f"phrase:{key}"
    assert hallucination_phrase(text, None, 30.0, no_speech_prob=no_speech) == f"phrase:{key}"


#: A deployment's ``STT_TEXT_REWRITES``: a callsign and a place name whisper writes as domains.
SITE_REWRITES = r"\bxcom(\.com)+(\.au)?\b => XCOM; \bpine ?valley(\.com)+\b => Pine Valley"


@pytest.fixture
def site_rewrites(monkeypatch):
    """Point the transcript repairs at :data:`SITE_REWRITES`, as the setting would."""
    import app.stt.postprocess as postprocess

    monkeypatch.setattr(postprocess, "configured_rewrites", lambda: postprocess.compile_rewrites(SITE_REWRITES))


def test_no_site_rewrites_by_default():
    """Nothing site-specific is built in: without the setting the text is only tidied."""
    assert compile_rewrites("") == ()
    assert normalize_text("XCOM.com, go ahead.") == "XCOM.com, go ahead."


def test_rewrite_rules_skip_what_they_cannot_use():
    rules = compile_rewrites("# a comment\nno arrow here; (unclosed => X; \\bok\\b => OK")
    assert [(p.pattern, r) for p, r in rules] == [("\\bok\\b", "OK")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # whisper writes a live callsign as a domain, sometimes several over
        ("XCOM.com, go ahead.", "XCOM, go ahead."),
        ("xcom.com.com.com.com. 10-4", "XCOM. 10-4"),
        ("Xcom.com.au copy", "XCOM copy"),
        # …and a place name the same way
        ("pinevalley.com is on scene", "Pine Valley is on scene"),
        ("Pine Valley.com bus 12", "Pine Valley bus 12"),
    ],
)
def test_domain_corruptions_are_rewritten_not_rejected(raw, expected, site_rewrites):
    """627 real transmissions in one archive: repaired, and cleared of the .com pattern."""
    assert normalize_text(raw) == expected
    assert hallucination_phrase(raw, None, 2.0) is None
    outcome = assess(make_result([seg(raw)], duration=2.0), 2.0)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text == expected


def test_rev_com_credit_survives_the_rewrites():
    """"cc by rev.com" is a genuine captioner credit and stays rejected."""
    assert hallucination_phrase("cc by rev.com", None, 2.0) == "credit:\\.com\\b"


def test_operator_patterns_are_merged_from_the_setting():
    # not opening with a broadcast-credit frame ("This program was …"): only the operator's
    # pattern can condemn it
    text = "Underwriting was brought to you by the Corporation for Public Broadcasting."
    assert hallucination_phrase(text) is None
    opts = TranscribeOptions(hallucination_patterns="brought to you by\npublic broadcasting")
    outcome = assess(make_result([seg(text)], duration=5.0), 5.0)
    assert outcome.quality is Quality.GOOD
    outcome = assess(make_result([seg(text)], duration=5.0), 5.0, opts)
    assert outcome.quality is Quality.NOISE
    assert "hallucination_phrase:custom:brought to you by" in outcome.reasons


def test_invalid_operator_patterns_are_skipped(caplog):
    patterns = compile_patterns("((unclosed\n\n# a comment\nmotorola solutions ; * bad")
    assert [p.pattern for p in patterns] == ["motorola solutions"]
    assert hallucination_phrase("Motorola Solutions test pattern", patterns) is not None
    assert hallucination_phrase("Engine 1 responding.", patterns) is None


def test_compile_patterns_accepts_nothing():
    assert compile_patterns(None) == ()
    assert compile_patterns("") == ()
    assert compile_patterns("   \n  ") == ()


# --------------------------------------------------------------------------- broadcast credits
# The credit-frame family: rejected only when the text opens with
# the frame and has no digit — at any length.

DOD = "the U.S. Department of Defense"

#: (text, call duration, index into CREDIT_FRAME_PATTERNS)
CREDIT_FRAME_HALLUCINATIONS: list[tuple[str, float, int]] = [
    # 1 — "this is / has been a production of"
    ("This is a production of Example Public Television.", 1.2, 0),
    ("This has been a production of Example Public Television.", 1.5, 0),
    ("This has been a production of Lakeside.", 1.1, 0),
    ("This has been a production of WGBH.", 3.1, 0),
    ("This is a presentation of the U.S. Department of Defense.", 2.4, 0),
    ("This was a presentation of the U.S. Department of Defense.", 1.2, 0),
    ("This is a presentation of the U.S. Department of Defense.", 9.6, 0),
    ("This is a presentation of the U.S. Department of Transportation.", 1.1, 0),
    ("This is a presentation of the U.S. Department of Health and Human Services.", 1.3, 0),
    ("This is a production of the U.S. Department of Defense.", 2.2, 0),
    ("This is a project of the U.S. Department of Defense.", 1.5, 0),
    ("This has been a production of the U.S. Department of State.", 1.7, 0),
    ("This has been a production of the Center for Autism and Related Disorders.", 1.4, 0),
    ("This has been a production of the Center for Autism and Related Disorders, LLC.", 2.4, 0),
    ("This program is a production of Example Public Television.", 2.3, 0),
    # 2 — "this video is brought to you by"
    (f"This video is brought to you by {DOD}.", 1.5, 1),
    (f"This interview is brought to you by {DOD}.", 1.3, 1),
    ("This video was made possible in part by.", 2.0, 1),
    (
        "This message is brought to you by the U.S. Department of Transportation and the U.S."
        " Department of Transportation and the U.S. Department of Transportation.",
        17.5,
        1,
    ),
    (
        "This recording is brought to you by the U.S. Department of Health and Human Services and"
        " the U.S. Department of Health Services and the U.S. Department of Health Services.",
        17.0,
        1,
    ),
    (
        "This video is brought to you by the Department of Health and Human Services and the"
        " Department of Health and Human Services.",
        7.5,
        1,
    ),
    (
        "This video was made possible in part by the support of the U.S. Department of"
        " Transportation and the U.S. Department of Transportation.",
        10.9,
        1,
    ),
    (
        "This video was produced with the support of the U.S. Department of Health and Human"
        " Services and the U.S. Department of Health and Human Services and the U.S. Department"
        " of Health and Human Services.",
        17.5,
        1,
    ),
    # 3 — "thank you for listening to this podcast"
    (f"Thank you for listening to this edition of {DOD}.", 1.3, 2),
    (f"Thank you for listening to this episode of {DOD} Radio and Television.", 7.3, 2),
    (
        "Thank you for listening to this podcast. If you have any questions or other problems,"
        " please post them in the comments.",
        2.1,
        2,
    ),
    (
        "Thank you for listening to this video. If you have any questions or other problems,"
        " please post them in the comments.",
        11.0,
        2,
    ),
    ("Thank you for listening to this program.", 7.1, 2),
    (
        "Thank you for listening to this presentation. If you have any questions or other"
        " questions, please feel free to ask them in the comments.",
        4.0,
        2,
    ),
    (
        "Thank you for listening to this interview. If you have any questions or comments,"
        " please post them in the comments section of this video.",
        6.2,
        2,
    ),
    # 4 — "thank you for listening to the U.S. Department of"
    (f"Thank you for listening to {DOD}.", 1.9, 3),
    (f"Thank you for listening to {DOD} Radio and Television.", 3.1, 3),
    # 5 — nothing but an agency name
    ("U.S. Department of Defense.", 1.5, 4),
    ("This is the U.S. Department of Defense.", 2.4, 4),
    ("In service of the U.S. Department of Defense", 1.1, 4),
    ("In service of the U.S. Department of Health and Human Services", 1.1, 4),
]

CREDIT_FRAME_SPEECH: list[tuple[str, float]] = [
    # a hallucinated credit glued onto a real call: the digits keep it, prefix and all
    (
        "This program is brought to you by Missing True Title, 110 West Aspen Street, Fernvale,"
        " 110 West Aspen Street, Fernvale, 0909, White, 0-64, Focus",
        28.0,
    ),
    (
        "This show is a representation of the previous broadcast, last known to be Westbound Route 12"
        " from the 2600 block",
        28.0,
    ),
    ("Thank you for listening to 28.3 Illinois Customs of a Mary, U Union", 10.0),
    (
        "Thank you for joining us for this edition of the U.S. Department of Defense and Speech"
        " Issues, Facial Droopy Code 3. So we're going to start in the last 15 minutes.",
        11.0,
    ),
    # a department named mid-sentence
    (
        "Good afternoon, University of Lifeguard here, Boundary Facility, Department of Defense,"
        " EC V-MED 28, company from Example Memorial",
        20.0,
    ),
    (
        "I'm 21 on Riverton PD, you're on Route 2551, Juniper Road, Department of Defense, Boyd 7,"
        " you can go Code 2",
        12.0,
    ),
    (
        "29, 29, when you get unstuck, you're going to 600 2nd Street, Southwest, Department of"
        " Health 3",
        9.0,
    ),
    (
        "I need you to contact the on-call person for the Department of Health. You can give them"
        " my phone number",
        8.0,
    ),
    ("EXAMPLE COUNTY DEPARTMENT OF HEALTH AND PSYCHIATRICS TO 316-81 SPRING AVENUE", 6.0),
    ("ecom, our Department of Transportation called one of their bus drivers", 5.0),
    ("All right, um, transport's going to do a production of RIDDLE in four minutes by itself", 9.2),
    ("United Center for Autism, 784 Maple Drive, 59-year-old female", 6.0),
    # "a broadcast of" is a dispatch verb, not a credit (25.6 s, no digit anywhere)
    (
        "This is a broadcast of the officers that will be near the infield gates. We have a report"
        " of some suspicious persons outside of the infield gates.",
        25.6,
    ),
    # undotted "us" is a word, not "U.S."
    ("Can you get us Department of Transportation on the line", 3.0),
]


@pytest.mark.parametrize(("text", "duration", "frame"), CREDIT_FRAME_HALLUCINATIONS)
def test_broadcast_credit_frames_are_rejected(text, duration, frame):
    reason = hallucination_phrase(text, None, duration)
    assert reason == f"credit_frame:{CREDIT_FRAME_PATTERNS[frame].pattern[:80]}", (text, reason)
    outcome = assess(make_result([seg(text, end=duration)], duration=duration), duration)
    assert outcome.quality is Quality.NOISE
    assert f"hallucination_phrase:{reason}" in outcome.reasons


@pytest.mark.parametrize(("text", "duration"), CREDIT_FRAME_SPEECH)
def test_real_traffic_with_credit_words_is_kept(text, duration):
    assert hallucination_phrase(text, None, duration) is None, text
    outcome = assess(make_result([seg(text, end=duration)], duration=duration), duration)
    assert outcome.quality is not Quality.NOISE


def test_credit_frame_must_open_the_text():
    frame = "this is a production of Example Public Television."
    lead = " ".join(["okay"] * MAX_CREDIT_FRAME_LEAD_WORDS)
    assert hallucination_phrase(f"{lead}, {frame}", None, 2.0) is not None
    lead = " ".join(["okay"] * (MAX_CREDIT_FRAME_LEAD_WORDS + 1))
    assert hallucination_phrase(f"{lead}, {frame}", None, 2.0) is None


def test_credit_frame_word_backstop():
    """A digit-free call that opens with a frame and then keeps talking is a person."""
    opener = "This video is brought to you by the county."
    words = opener.split()
    tail = " ".join(["copy that, we are clear"] * 20).split()
    long_text = " ".join(words + tail[: MAX_CREDIT_FRAME_WORDS - len(words) + 1])
    assert len(long_text.split()) == MAX_CREDIT_FRAME_WORDS + 1
    assert hallucination_phrase(long_text, None, 20.0) is None
    at_limit = " ".join(long_text.split()[:MAX_CREDIT_FRAME_WORDS])
    assert hallucination_phrase(at_limit, None, 20.0) is not None


def test_agency_name_frame_needs_the_whole_utterance():
    assert hallucination_phrase("U.S. Department of Defense.", None, 1.5) is not None
    assert (
        hallucination_phrase("U.S. Department of Defense is on scene with the hazmat team now.", None, 4.0)
        is None
    )


# --------------------------------------------------------------------------- conversational words
# "Okay." / "Thank you." are real dispatch traffic; they are
# rejected only on evidence about the audio behind them.


@pytest.mark.parametrize("text", ["Okay.", "OK.", "Thank you.", "Thanks.", "Thank you very much.", "Bye."])
def test_a_real_short_acknowledgement_is_kept_but_suspect(text):
    """A 1.5 s call, whisper confident it heard speech: a unit acknowledging."""
    result = make_result([seg(text, end=1.5, no_speech=0.05)], duration=1.5)
    assert hallucination_phrase(text, None, 1.5, no_speech_prob=0.05) is None
    outcome = assess(result, 1.5)
    assert outcome.quality is Quality.SUSPECT
    assert outcome.cleaned_text == text
    assert any(r.startswith("conversational_phrase:") for r in outcome.reasons)


def test_the_classic_thirty_second_thank_you_is_still_rejected():
    """Thirty seconds of noise transcribed as "Thank you." — two words cannot fill it,
    however confident the decoder was."""
    result = make_result([seg("Thank you.", end=30.0, no_speech=0.1)], duration=30.0)
    outcome = assess(result, 30.0)
    assert outcome.quality is Quality.NOISE
    assert "hallucination_phrase:phrase:thank you" in outcome.reasons
    assert hallucination_phrase("Thank you.", None, 30.0, no_speech_prob=0.1) == "phrase:thank you"


@pytest.mark.parametrize(
    ("duration", "no_speech", "rejected"),
    [
        (1.5, CONVERSATIONAL_NO_SPEECH_PROB, True),  # whisper doubts it heard a person
        (1.5, 0.866, True),
        (0.418, 0.05, True),  # a squelch tail, not a key-up
        (CONVERSATIONAL_MIN_DURATION_S, 0.05, False),
        (CONVERSATIONAL_MAX_DURATION_S, 0.05, False),
        (CONVERSATIONAL_MAX_DURATION_S + 0.5, 0.05, True),
        (1.5, None, False),  # OVMS: no metrics, judged on the length alone
        (None, 0.05, False),
        (None, None, True),  # nothing to judge on: rejected, as it always was
    ],
)
def test_the_conversational_gate(duration, no_speech, rejected):
    assert conversational_is_noise(duration, no_speech) is rejected
    reason = hallucination_phrase("Okay.", None, duration, no_speech_prob=no_speech)
    assert (reason == "phrase:okay") is rejected


def test_you_and_so_stay_on_the_exact_list():
    """Nobody keys up to say only "you" — they are whisper's words for silence."""
    assert hallucination_phrase("You.", None, 1.5, no_speech_prob=0.05) == "phrase:you"
    assert hallucination_phrase("So.", None, 1.5, no_speech_prob=0.05) == "phrase:so"


def test_a_real_closing_okay_is_not_trimmed_as_a_trailing_hallucination():
    result = make_result(
        [
            seg("Engine 1, respond to 784 Maple Drive.", end=4.0, no_speech=0.1),
            seg("Okay.", start=4.2, end=5.0, no_speech=0.08),
        ],
        duration=5.0,
    )
    outcome = assess(result, 5.0)
    assert outcome.cleaned_text.endswith("Okay.")
    assert "trailing_hallucination" not in outcome.reasons


def test_a_doubtful_trailing_thank_you_is_still_trimmed():
    result = make_result(
        [
            seg("Engine 1, respond to 784 Maple Drive.", end=29.2, no_speech=0.1),
            seg(" Thank you.", start=29.2, end=30.3, no_speech=0.263),
        ],
        duration=30.3,
    )
    outcome = assess(result, 30.3)
    assert outcome.cleaned_text == "Engine 1, respond to 784 Maple Drive."
    assert "trailing_hallucination" in outcome.reasons
