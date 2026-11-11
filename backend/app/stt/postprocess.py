"""Hallucination detection, repeat-loop collapse and quality labeling (ARCHITECTURE §5.2).

Whisper on short, noisy, squelch-tailed P25 audio fails in a small number of very
recognizable ways, all of which showed up in a corpus study of the archive's own calls:

* a 0.42 s squelch tail with ``vad_filter=false`` transcribes as ``"Thanks for watching!"``
  (``no_speech_prob=0.866``, ``avg_logprob=-0.751``) — pure hallucination;
* the same clip with ``vad_filter=true`` returns empty text and language ``nn``;
* a 30.3 s fire dispatch picks up a trailing ``" Thank you for watching."`` segment
  (``no_speech_prob=0.263``) after the real traffic ends at 29.2 s;
* a 44.5 s BOLO rebroadcast reaches ``compression_ratio=2.11`` with genuinely repeated content.

The thresholds below are tuned against those numbers.  Labels are advisory: ``noise`` rows are
hidden from search by default, ``suspect`` rows are shown with a warning badge, and nothing is
ever discarded — the raw text stays on the transcript row so a human can correct it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache

from .types import Quality, QualityAssessment, TranscribeOptions, TranscriptResult, TranscriptSegment

log = logging.getLogger("app.stt.postprocess")

__all__ = [
    "CONVERSATIONAL_MAX_DURATION_S",
    "CONVERSATIONAL_MIN_DURATION_S",
    "CONVERSATIONAL_NO_SPEECH_PROB",
    "CONVERSATIONAL_PHRASES",
    "CREDIT_FRAME_PATTERNS",
    "CREDIT_PATTERNS",
    "HALLUCINATION_PATTERNS",
    "HALLUCINATION_PHRASES",
    "HALLUCINATION_REWRITES",
    "MAX_COMPRESSION_RATIO",
    "MAX_CREDIT_FRAME_LEAD_WORDS",
    "MAX_CREDIT_FRAME_WORDS",
    "MAX_SHORT_CREDIT_CHARS",
    "MAX_SIGNOFF_FILLER_WORDS",
    "MAX_WORDS_PER_SECOND",
    "MIN_AVG_LOGPROB",
    "NO_SPEECH_THRESHOLD",
    "SIGNOFF_MAX_DURATION_S",
    "SHORT_CREDIT_PATTERNS",
    "SIGNOFF_CLOSINGS",
    "SIGNOFF_CONNECTORS",
    "SIGNOFF_FILLER_WORDS",
    "SIGNOFF_PATTERNS",
    "assess",
    "collapse_repeats",
    "compile_patterns",
    "compile_rewrites",
    "configured_rewrites",
    "conversational_is_noise",
    "conversational_phrase",
    "hallucination_phrase",
    "normalize_text",
]

# --------------------------------------------------------------------------- thresholds

NO_SPEECH_THRESHOLD = 0.6
"""Above this on *every* segment, the clip is silence/noise.  Observed: 0.866 on the squelch
tail, 0.10-0.16 on real traffic."""

MAX_COMPRESSION_RATIO = 2.4
"""gzip ratio of the segment text; above this whisper is looping.  Observed ceiling on genuine
(if repetitive) dispatch traffic: 2.11, so 2.4 leaves real rebroadcasts alone."""

MIN_AVG_LOGPROB = -1.0
"""Below this the decode is a guess.  Observed range on real traffic: -0.24 to -0.49."""

MAX_WORDS_PER_SECOND = 6.0
"""No human dispatcher exceeds ~4 w/s; more text than this means invented words."""

MIN_PROMPT_ECHO_WORDS = 3
"""Shorter runs than this match the domain prompt by coincidence — units and place names really
are spoken on the radio.  Observed failure: a 3.1 s clip transcribed as the verbatim place list
from its own ``initial_prompt``."""

# Matched against the *whole* transcript only.  These are the YouTube-caption artifacts
# whisper falls back on when it is fed noise; as a whole utterance they are never real radio
# traffic, but as a fragment ("thank you") they legitimately occur, so substring matching
# would destroy good transcripts.
HALLUCINATION_PHRASES: frozenset[str] = frozenset(
    {
        "thanks for watching",
        "thanks for watching!",
        "thank you for watching",
        "thanks for watching everyone",
        "subscribe",
        "please subscribe",
        "like and subscribe",
        "don't forget to subscribe",
        "you",
        "bye bye",
        "goodbye",
        "so",
        "the end",
        "music",
        "applause",
        "silence",
        "beep",
        "transcription by castingwords",
        "subtitles by the amara.org community",
        "closed captioning by the closed captioning project llc",
        "closed captioning by the closed captioning project",
        "captions by the closed captioning project",
        "www.mooji.org",
    }
)

# --------------------------------------------------------------------------- conversational
#
# Some whole-utterance hallucinations are also the commonest real transmissions on a dispatch
# channel: "Okay.", "Thank you.", "Thanks." and "Bye." are what a unit says to close an
# exchange.  On the exact list above they were rejected at any length and any confidence,
# which threw away real traffic.  They are judged here instead,
# on the evidence whisper gives about the audio behind them — see
# :func:`conversational_is_noise`.  Kept but not trusted, they are labeled ``suspect``.
#
# "you" and "so" stay on the exact list: nobody keys up to say only that, and they are the
# two words whisper writes most often over silence.

CONVERSATIONAL_PHRASES: frozenset[str] = frozenset(
    {
        "okay",
        "ok",
        "thank you",
        "thank you very much",
        "thanks",
        "bye",
        # The closings whisper also writes over a short silence (2026-10-10 survey of 1-5 s
        # digit-free calls: "Have a great day." x843, "…good day" x397, "…good night"
        # x239, "…nice day" x136, "…wonderful day" x118, "Take care." x64) — and that units
        # really do say: the same words close 185 and 190 calls that carry a unit number
        # ("10-4, have a good night.", "Take care, Engine 3, back in service.").  So they
        # are judged like "Thank you.", not rejected with the sign-offs.
        "have a good day",
        "have a great day",
        "have a nice day",
        "have a wonderful day",
        "have a good night",
        "have a good one",
        "take care",
    }
)

CONVERSATIONAL_NO_SPEECH_PROB = 0.2
"""At or above this ``no_speech_prob`` a bare acknowledgement is noise.  Between the highest
value observed on real traffic (0.10-0.16) and the lowest on a hallucinated tail (0.263 on the
30.3 s fire dispatch's " Thank you for watching.")."""

CONVERSATIONAL_MIN_DURATION_S = 1.0
"""Shorter than this, the "call" is a squelch tail (the hallucinated one was 0.418 s), not a
person keying up to say a word."""

CONVERSATIONAL_MAX_DURATION_S = 5.0
"""Longer than this, one or two words cannot be what the call held: the classic hallucination
is a 30 s recording of noise transcribed as "Thank you."  The sign-off family's gate, for the
same reason (:data:`SIGNOFF_MAX_DURATION_S`)."""


def conversational_phrase(text: str | None) -> str | None:
    """The :data:`CONVERSATIONAL_PHRASES` key ``text`` is, as a whole utterance, or ``None``."""
    key = _key(text)
    return key if key in CONVERSATIONAL_PHRASES else None


def conversational_is_noise(duration_s: float | None, no_speech_prob: float | None) -> bool:
    """Is a bare "Okay." / "Thank you." over this audio a hallucination?

    Yes when whisper itself scored the audio as probably not speech
    (:data:`CONVERSATIONAL_NO_SPEECH_PROB`), or when the audio is too short to be a person
    (:data:`CONVERSATIONAL_MIN_DURATION_S`) or far too long for the word
    (:data:`CONVERSATIONAL_MAX_DURATION_S`).  With neither number known there is nothing to
    judge on, and the answer stays what it always was: reject.  An engine that reports no
    ``no_speech_prob`` (OVMS, openvino-genai) is judged on the duration alone.
    """
    if no_speech_prob is not None and no_speech_prob >= CONVERSATIONAL_NO_SPEECH_PROB:
        return True
    if duration_s is None or duration_s <= 0:
        return no_speech_prob is None
    return duration_s < CONVERSATIONAL_MIN_DURATION_S or duration_s > CONVERSATIONAL_MAX_DURATION_S


# --------------------------------------------------------------------------- pattern families
#
# The exact set above only fires on a whole utterance it matches character for character,
# and whisper's caption-credit hallucinations are endlessly variable: a production survey of
# this archive found 1,005 transcripts containing "captioning provided by" (919 of them
# beginning "closed caption") in a dozen spellings — "closed captioning provided by
# WKNO-Memphis.", "captioning provided by W-K-N-O, Memphis", "closed captioning by Kris
# Brandhagen. brandhagen.com", "captioning provided by Aeverine Zinn Digital Media Group" —
# 913 of which were still labeled ``good``.  None is radio traffic; all of them average
# under 2.6 s of audio.  Patterns catch the family the exact list cannot enumerate.
#
# Two classes, because the evidence differs:
#
# *credit* patterns name a thing that has no place on a police radio at all — a captioner,
# a subtitler, a URL, a copyright line.  Wherever they appear in the text, the text is a
# hallucination, and matching a substring is safe precisely because a dispatcher never says
# "closed captioning".
#
# *sign-off* patterns name phrases people really do say ("thank you for your time"), so
# they condemn only when they are the *whole* utterance — bare, or wrapped in at most
# :data:`MAX_SIGNOFF_FILLER_WORDS` words of filler on either side.  A 40-word transcript
# that happens to end with one is real traffic and is kept.

MAX_SHORT_CREDIT_CHARS = 40
"""``.com`` is only evidence of a hallucination in a short utterance.  Longer real traffic
reads out websites and email addresses; a 2 s call that is nothing but a domain does not."""

MAX_SIGNOFF_FILLER_WORDS = 3
"""How many words, in total, may sit around the sign-off phrases of an utterance and still
leave them the whole of it ("Okay," / "and I'll" / "everyone").  Each must also be on
:data:`SIGNOFF_FILLER_WORDS`."""

SIGNOFF_FILLER_WORDS: frozenset[str] = frozenset(
    {
        # The words left over in the hallucinated chains of the 2026-10-10 production dry run
        # ("Thank you for watching, *and I'll* see you next time.", "*Okay*, thanks for
        # listening, *everyone*.") — and nothing that carries content.  A name, a unit,
        # "sir", "go ahead" or "we're out" left over is a person closing a real exchange:
        # "Thanks for your time, Alex.", "Lakeside out, thanks for your time.", "Thank
        # you for listening, Riverton, go ahead." were the dry run's false positives without it.
        "and", "so", "okay", "ok", "alright", "all", "right", "well", "um", "uh", "oh", "hey",
        "yeah", "yes", "guys", "everyone", "everybody", "folks", "again", "also", "too",
        "i'll", "we'll", "i", "we", "will", "you", "very", "much", "now", "today", "tonight",
        "then",
    }
)  # fmt: skip
"""The only words :data:`MAX_SIGNOFF_FILLER_WORDS` may be, compared lower-cased and stripped
of surrounding punctuation."""

SIGNOFF_MAX_DURATION_S = 5.0
"""A sign-off with filler around it is only evidence on a *short* call.  The hallucinated ones
average 1.7-2.6 s of audio; the false positives a production dry run turned up were a 4.9 s
"116, goodbye." and six-to-ten-second calls that really do end "thank you for your time, and
I'll see you next time."  At or above this the utterance must be a **pure** outro to be
rejected — sign-offs, closings and the connectors that join them (:data:`SIGNOFF_CONNECTORS`)
and not one other word — for the reason :data:`CONVERSATIONAL_MAX_DURATION_S` rejects a 30 s
"Thank you.": a long recording whose entire transcript is a stock outro is whisper writing
over noise (a 2026-10-11 survey: 108 calls of 5.1-40.6 s that were nothing but "Thank you
for joining us." / "Thank you for your attention." / …, and 122 more of "Thank you for
watching, and I'll see you next time." up to 42.4 s).  A real long call that ends with a
sign-off has other words in it, and keeps its text.

With the duration unknown the family does not vote at all: that is how the trailing-segment
check (:func:`_is_trailing_hallucination`) asks only for the families that hold at any length,
so a real call whose last segment is "Thank you for your time." keeps it.  Every call in the
archive has a duration (3,086,996 with text, none without), so nothing is lost."""

_DIGIT_RE = re.compile(r"\d")
"""A digit in the text is a unit number, a ten-code, an address or a plate, and no
caption-credit sign-off has one: it is the cheapest evidence that a person was talking.
Guards the sign-off and broadcast-credit families only — a credit line with a copyright
year is still a credit."""

CREDIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"closed caption",
        r"captioning (provided )?by",
        r"subtitles? by",
        r"transcribed by",
        r"transcription by",
        r"amara\.org",
        r"\bwww\.",
        r"copyright",
        r"all rights reserved",
        r"brandhagen",
        r"wkno",
        # "This has been a presentation of the Center for Autism and Related Disorders" —
        # a stock closing credit whisper falls back on, and no dispatcher has ever
        # presented anything.  Both spellings: the sponsor by name, and the frame it
        # arrives in, which carries the other sponsors the same credit is read for.
        r"presentation of the center for autism",
        r"(this )?has been a presentation of",
        r"\[music\]",
        r"♪",
    )
)
"""Reject wherever they occur in the text."""

SHORT_CREDIT_PATTERNS: tuple[re.Pattern[str], ...] = (re.compile(r"\.com\b", re.IGNORECASE),)
"""Credit patterns that only apply below :data:`MAX_SHORT_CREDIT_CHARS` characters."""

#: "Thank you all", "Thanks guys", "Thank you so much": who is thanked, and how much.
_THANKS = r"thank(s| you)( (all|guys|everyone|everybody))?( (so|very) much)?"

# A production survey (2026-10-10) of short, digit-free calls still labeled ``good`` or
# ``suspect`` found the sign-offs the first version of this family missed, because it was
# too literal ("Thanks for listening." x266: only "thank you" was spelled out; "Thank you
# for tuning in." x461) or too strict about the whole utterance: whisper chains its
# outros, and "Thank you for watching, and I'll see you next time." (x1,708 under 5 s) is
# two sign-offs joined by two words, not one sign-off wrapped in three.  So the utterance
# test is now about the *whole* text: every word of it must belong to a sign-off or a
# closing (:data:`SIGNOFF_CLOSINGS`), bar :data:`MAX_SIGNOFF_FILLER_WORDS` words in total.
SIGNOFF_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"thank(s| you) (so much )?for watching",
        # "thank you all for tuning in", "thanks for listening", "thanks for joining us"
        _THANKS + r" for (listening|your attention|your time|joining us|tuning in|watching)",
        # "see you next time", "see you guys next time", "see you on the next one"
        r"see you( (guys|all|again))? (next time|(in|on) the next( (video|one|episode))?)",
        r"(please |don't forget to )?(like and )?subscribe",
        r"the end",
        r"bye bye",
        r"goodbye",
    )
)
"""Reject only when they, chained with :data:`SIGNOFF_CLOSINGS`, are the whole utterance
(see :data:`MAX_SIGNOFF_FILLER_WORDS`)."""

SIGNOFF_CLOSINGS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "…and have a great day" (x90 after "thank you for watching", x74 after "…for
        # listening") — but on its own a real closing; see CONVERSATIONAL_PHRASES
        r"have a (good|great|nice|wonderful) (day|night|evening|weekend|one)",
        r"take care",
        r"good night",
        r"see you( (guys|all|again|soon|later|then|around))?",
        # the talk-show outro: "Thank you for your time. Thank you for having me." (x59 in the
        # dry run, ~110 rows with its variants) — alone, a real thing to say on a radio
        _THANKS + r" for having (me|us)",
        r"(i|we) (really )?appreciate it",
        _THANKS,
        r"bye( for now)?",
    )
)
"""Closings a sign-off is chained with ("…see you next time, have a great day, bye").  They
never condemn an utterance themselves — "Take care." and "See you." are what units say —
they only stop counting as filler next to a sign-off."""

_SEE_YOU_LEAD = r"(i'll|we'll|i will|we will)"
SIGNOFF_CONNECTORS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "…, and I'll see you next time" / "We'll see you next time."
        r"(and )?" + _SEE_YOU_LEAD + r"(?= see you)",
        # "…, and have a great day" / "…, and thank you" / "…, and take care"
        r"and(?= (have a|see you|thank|take care|bye|good night|" + _SEE_YOU_LEAD + r" see you))",
    )
)
"""The words that join outro phrases, recognized only *in place* — "and" right before the
next phrase, "I'll" / "we'll" right before "see you".  Used by the long-call rule alone
(:data:`SIGNOFF_MAX_DURATION_S`): there no filler is allowed, and "and I'll" in "Thank you
for watching, and I'll see you next time." is part of the outro, not something said beside
it.  Under 5 s they are ordinary filler and the rule there is unchanged."""

# --------------------------------------------------------------------------- broadcast credits
#
# Whisper's other stock fallback is the closing credit of a public-television or government
# video: "This is a production of Example Public Television.", "This video is brought to you by
# the U.S. Department of Defense.", "Thank you for listening to the U.S. Department of
# Defense Radio and Television."  A production dry run (2026-10-10) found 831 of them still
# labeled ``good`` or ``suspect``, most on 1-3 s calls ("This is a production of Example Public
# Television." alone 92 times, "Thank you for watching this video, and I'll see you next
# time." on calls up to 19 s).  Some run long: a 17.5 s loop of "…brought to you by the
# U.S. Department of Transportation and the U.S. Department of Transportation and…" is the
# same hallucination unable to stop.
#
# Neither of the families above fits.  The words are not credit-only words — "Department
# of Transportation", "Department of Health" and even "a production of" occur in real
# dispatch ("contact the on-call person for the Department of Health", "transport's going
# to do a production of RIDDLE"), so a substring match would destroy traffic.  And the
# sign-off family's duration gate would wave the long loops through.  So the evidence is
# position and content instead:
#
# * the text must **open** with the credit frame — at most
#   :data:`MAX_CREDIT_FRAME_LEAD_WORDS` words before it, as for a sign-off — because a
#   dispatcher who mentions a department does so mid-sentence, never as "This is a
#   presentation of …";
# * and the text must contain **no digit** (:data:`_DIGIT_RE`): a unit number, address or
#   ten-code means a person was talking.  This is the deliberate trade-off — a real call
#   with a credit glued onto its front ("This program is brought to you by Missing True
#   Title, 110 West Aspen Street, …", 28 s) is *kept*, hallucinated prefix and all,
#   because discarding it would lose the address it carries.
#
# There is no duration gate: a digit-free credit frame is a hallucination at any length, up
# to a generous word-count backstop (:data:`MAX_CREDIT_FRAME_WORDS`) for the real call that
# opens with something frame-shaped and then keeps talking.
#
# The frames are matched against the comparison key (:func:`_key`: lower case, punctuation
# gone), so "U.S." is ``u s`` and the word counts are exact.

MAX_CREDIT_FRAME_LEAD_WORDS = MAX_SIGNOFF_FILLER_WORDS
"""How many words may come before a broadcast-credit frame ("Okay, so this is a production
of …").  The sign-off family's filler allowance, for the same reason."""

MAX_CREDIT_FRAME_WORDS = 60
"""A backstop, not a duration gate: longer than this, a digit-free text that opens with a
credit frame is a person talking after a hallucinated opener.  The longest credit the
production dry run proposed was 46 words (a "thank you for watching this video" outro said
two and a half times); the one real call that opened with a frame and had no digit ran 78.
A longer *loop* of credits is still caught live — by the worker's repeat-loop rule, which
exists for exactly that."""

CREDIT_FRAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "This (program) is / has been a production of Example Public Television."  Not "a
        # broadcast of": that is a dispatch verb, and "This is a broadcast of [for] the
        # officers near the infield gates …" was a real 25.6 s call with no digit in it.
        r"this( (video|program|programme|show|recording|broadcast))? (is|was|has been) (a|an|the)"
        r" (production|presentation|project|program|programme) of",
        # "This video is brought to you by …", "This video was made possible in part by …"
        r"this (video|program|programme|recording|message|interview|episode|podcast|show"
        r"|broadcast|presentation) (is|was|has been) (brought to you|produced|made possible"
        r"|presented|funded|sponsored|supported)",
        # "Thank you for listening to this podcast.  If you have any questions …"
        r"thank(s| you) for (listening|watching|joining us)( to| for)? (this|our|the) (podcast"
        r"|video|program|programme|presentation|interview|episode|edition|show|broadcast"
        r"|recording|series)",
        # "Thank you for listening to the U.S. Department of Defense (Radio and Television)."
        r"thank(s| you) for listening to the (u s|united states) department of",
        # The whole utterance is an agency name: "U.S. Department of Defense.", "This is the
        # U.S. Department of Defense.", "In service of the U.S. Department of Defense."
        # Anchored at the end and only a few words long — the dotted "U.S." is required,
        # because "can you get us Department of Transportation" is a real request.
        r"(the )?(u s|united states) department of( \S+){1,5}$",
    )
)
"""Reject when the text opens with one (:data:`MAX_CREDIT_FRAME_LEAD_WORDS`), has no digit
and is no longer than :data:`MAX_CREDIT_FRAME_WORDS`."""

HALLUCINATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    CREDIT_PATTERNS + SHORT_CREDIT_PATTERNS + CREDIT_FRAME_PATTERNS + SIGNOFF_PATTERNS
)
"""Every builtin pattern, in the order :func:`hallucination_phrase` tries them."""

HALLUCINATION_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = ()
r"""Builtin corruption repairs: none.  The ones worth having are site-specific, so they come
from ``STT_TEXT_REWRITES`` (:func:`configured_rewrites`).

Corruptions, not hallucinations: text that is real traffic with a hallucinated tail glued
onto one word.  A short callsign or a place name that reads like a brand ("XCOM", "Pine
Valley") is written by whisper as a domain — ``XCOM.com``, ``pinevalley.com`` — sometimes
in runs it cannot stop writing (``xcom.com.com.com.``) and with a ``.au`` it invented
outright.  One deployment found 900 such calls in its archive.  The repeat groups matter: a
rewrite that leaves ``.com`` behind hands the text straight to the credit pattern below,
which is exactly how a dry run there proposed discarding 627 real transmissions.  Rewriting
in :func:`normalize_text` repairs the transcript *and* clears it of the pattern, so a rule
looks like ``\bxcom(\.com)+(\.au)?\b => XCOM``."""


@lru_cache(maxsize=8)
def compile_rewrites(spec: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile ``STT_TEXT_REWRITES``: ``pattern => replacement`` rules, one per line or
    ``;``-separated, ``#`` lines ignored, matched case-insensitively.

    A rule without ``=>`` or whose pattern does not compile is logged once (the cache is
    keyed on the text) and skipped, like an operator hallucination pattern.
    """
    out: list[tuple[re.Pattern[str], str]] = []
    for chunk in spec.replace(";", "\n").splitlines():
        item = chunk.strip()
        if not item or item.startswith("#"):
            continue
        pattern, sep, replacement = item.partition("=>")
        if not sep or not pattern.strip():
            log.warning("stt.text_rewrites: ignoring %r (expected 'pattern => replacement')", item)
            continue
        try:
            out.append((re.compile(pattern.strip(), re.IGNORECASE), replacement.strip()))
        except re.error as exc:
            log.warning("stt.text_rewrites: ignoring invalid pattern %r: %s", pattern.strip(), exc)
    return tuple(out)


def configured_rewrites() -> tuple[tuple[re.Pattern[str], str], ...]:
    """The builtin repairs plus the deployment's ``STT_TEXT_REWRITES``."""
    try:
        from app.config import get_settings  # noqa: PLC0415 - optional: postprocess is also a library

        spec = get_settings().stt_text_rewrites
    except Exception:  # noqa: BLE001 - no settings (a bare library use) means no site rules
        spec = ""
    return HALLUCINATION_REWRITES + compile_rewrites(spec)

# Bracketed sound annotations whisper emits for non-speech audio: [MUSIC], (buzzing), ♪ ... ♪
_ANNOTATION_RE = re.compile(r"[\[\(\{][^\]\)\}]{0,40}[\]\)\}]|[♪♫]+")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_REPEATED_PUNCT_RE = re.compile(r"([,.!?;:])\1{1,}")
_DASH_SPACE_RE = re.compile(r"(?<=\d)\s*-\s*(?=\d)")
_KEY_STRIP_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_text(text: str | None) -> str:
    """Tidy raw engine text: whitespace, sound annotations and common radio artifacts.

    Keeps case and meaningful punctuation — this is what gets stored and searched, not a
    comparison key.
    """
    if not text:
        return ""
    out = _ANNOTATION_RE.sub(" ", text)
    out = out.replace("’", "'").replace("“", '"').replace("”", '"')
    out = out.replace("—", "-").replace("–", "-")
    out = _WHITESPACE_RE.sub(" ", out).strip()
    out = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", out)
    out = _REPEATED_PUNCT_RE.sub(r"\1", out)
    # "10 - 4" / "10- 4" -> "10-4"; ten-codes and unit numbers are the most common artifact.
    out = _DASH_SPACE_RE.sub("-", out)
    # Repair the known corruptions ("XCOM.com" -> "XCOM") before anything judges the text.
    for pattern, replacement in configured_rewrites():
        out = pattern.sub(replacement, out)
    return out.strip(" -,")


def _key(text: str | None) -> str:
    """Case/punctuation-insensitive comparison key for repeat detection."""
    return _WHITESPACE_RE.sub(" ", _KEY_STRIP_RE.sub(" ", normalize_text(text).lower())).strip()


def collapse_repeats(texts: Sequence[str]) -> tuple[list[str], int]:
    """Drop consecutive segment texts that are identical once normalized.

    Returns ``(kept, n_dropped)``.  Real dispatch repeats itself ("dark in color, Buick
    four-door sedan," twice in a row in the 44.5 s sample), and so does a looping decoder;
    collapsing is safe for both because the information content is unchanged.
    """
    kept: list[str] = []
    dropped = 0
    last_key: str | None = None
    for text in texts:
        key = _key(text)
        if key and key == last_key:
            dropped += 1
            continue
        kept.append(text)
        if key:
            last_key = key
    return kept, dropped


@lru_cache(maxsize=8)
def _compile_spec(spec: str) -> tuple[re.Pattern[str], ...]:
    """Compile an operator-supplied pattern list, skipping (and logging) what will not compile.

    Cached on the setting's text: the setting changes about once a year and this runs on
    every transcript.  The cache is also what makes "logged once" true — a bad regex is
    reported the first time that exact setting value is seen, not once per call.
    """
    out: list[re.Pattern[str]] = []
    for chunk in spec.replace(";", "\n").splitlines():
        item = chunk.strip()
        if not item or item.startswith("#"):
            continue
        try:
            out.append(re.compile(item, re.IGNORECASE))
        except re.error as exc:
            log.warning("stt: ignoring an invalid stt_hallucination_patterns entry %r: %s", item, exc)
    return tuple(out)


def compile_patterns(spec: str | Iterable[str] | None) -> tuple[re.Pattern[str], ...]:
    """Operator patterns from ``stt_hallucination_patterns``: one per line, or ``;``-separated.

    Entries that are not valid regular expressions are logged once and skipped — a typo in a
    settings box must not stop the transcription worker.
    """
    if not spec:
        return ()
    if not isinstance(spec, str):
        spec = "\n".join(str(item) for item in spec)
    return _compile_spec(spec)


def _label(pattern: re.Pattern[str]) -> str:
    """The reason-string detail for a pattern hit: its own source, trimmed."""
    return pattern.pattern[:80]


def _whole_words(pattern: re.Pattern[str], text: str, taken: list[tuple[int, int]]) -> bool:
    """Claim ``pattern``'s whole-word matches in ``text`` that overlap nothing in ``taken``."""
    found = False
    for match in pattern.finditer(text):
        start, end = match.span()
        if (start > 0 and text[start - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
            continue
        if any(start < e and s < end for s, e in taken):
            continue
        taken.append((start, end))
        found = True
    return found


_FILLER_STRIP = ".,;:!?\"'()-"


def _signoff(text: str, *, pure: bool = False) -> re.Pattern[str] | None:
    """The sign-off ``text`` consists of, or ``None`` (see :data:`SIGNOFF_PATTERNS`).

    The text is a sign-off when at least one :data:`SIGNOFF_PATTERNS` phrase is in it and
    what is left once every sign-off and :data:`SIGNOFF_CLOSINGS` phrase is taken out is
    at most :data:`MAX_SIGNOFF_FILLER_WORDS` words of :data:`SIGNOFF_FILLER_WORDS` — "and",
    "I'll", "everyone", "okay" — however they are spread around the phrases.

    ``pure`` is the long-call rule (:data:`SIGNOFF_MAX_DURATION_S`): the connectors that
    join the phrases are taken out too (:data:`SIGNOFF_CONNECTORS`), and nothing at all may
    be left.
    """
    taken: list[tuple[int, int]] = []
    first: re.Pattern[str] | None = None
    for pattern in SIGNOFF_PATTERNS:
        if _whole_words(pattern, text, taken) and first is None:
            first = pattern
    if first is None:
        return None
    for pattern in SIGNOFF_CLOSINGS + (SIGNOFF_CONNECTORS if pure else ()):
        _whole_words(pattern, text, taken)
    rest = list(text)
    for start, end in taken:
        rest[start:end] = " " * (end - start)
    filler = [word.strip(_FILLER_STRIP).lower() for word in "".join(rest).split()]
    filler = [word for word in filler if word]
    if pure:
        return None if filler else first
    if len(filler) > MAX_SIGNOFF_FILLER_WORDS or not SIGNOFF_FILLER_WORDS.issuperset(filler):
        return None
    return first


def _credit_frame(key: str, cleaned: str) -> re.Pattern[str] | None:
    """The broadcast-credit frame ``key`` opens with, or ``None`` (see :data:`CREDIT_FRAME_PATTERNS`).

    ``key`` is :func:`_key` of ``cleaned``.  The first match of a pattern is its earliest, so
    if that one starts too far in, no later one can qualify.
    """
    if _DIGIT_RE.search(cleaned) or len(key.split()) > MAX_CREDIT_FRAME_WORDS:
        return None
    for pattern in CREDIT_FRAME_PATTERNS:
        match = pattern.search(key)
        if match is not None and len(key[: match.start()].split()) <= MAX_CREDIT_FRAME_LEAD_WORDS:
            return pattern
    return None


def hallucination_phrase(
    text: str,
    extra_patterns: Sequence[re.Pattern[str]] | None = None,
    duration_s: float | None = None,
    *,
    no_speech_prob: float | None = None,
) -> str | None:
    """Why ``text`` is a caption artifact rather than radio traffic, or ``None``.

    The return value is the family and the evidence — ``phrase:<key>`` for an exact match
    against :data:`HALLUCINATION_PHRASES`, ``credit:<regex>``, ``custom:<regex>`` for an
    operator pattern, ``credit_frame:<regex>``, ``signoff:<regex>`` — and callers prefix it
    with ``hallucination_phrase:`` to make the stored reason string.

    ``extra_patterns`` are compiled operator patterns (:func:`compile_patterns`); they are
    treated like the builtin credit family, rejecting wherever they occur.

    ``duration_s`` is the call's audio length and gates the **sign-off** family only: those
    phrases are things people say, so with filler around them they condemn only a call short
    enough to be a squelch tail (:data:`SIGNOFF_MAX_DURATION_S`); a longer call only when its
    text is a pure outro with not one other word; neither with a digit anywhere in it, nor
    with the duration unknown.  Credit patterns need no such gate — nobody says "closed
    captioning" on a radio, at any length.

    The **broadcast-credit** frames (:data:`CREDIT_FRAME_PATTERNS`) are not gated on
    duration either, but only condemn a text that *opens* with one and has no digit in it.

    ``duration_s`` and ``no_speech_prob`` together gate the **conversational** words
    (:data:`CONVERSATIONAL_PHRASES`): a bare "Okay." is rejected as ``phrase:okay`` only when
    :func:`conversational_is_noise` says the audio behind it was not a person talking.
    """
    cleaned = normalize_text(text)
    if not cleaned:
        return None
    key = _key(cleaned)
    if key in HALLUCINATION_PHRASES:
        return f"phrase:{key}"
    if key in CONVERSATIONAL_PHRASES and conversational_is_noise(duration_s, no_speech_prob):
        return f"phrase:{key}"
    for pattern in CREDIT_PATTERNS:
        if pattern.search(cleaned):
            return f"credit:{_label(pattern)}"
    if len(cleaned) < MAX_SHORT_CREDIT_CHARS:
        for pattern in SHORT_CREDIT_PATTERNS:
            if pattern.search(cleaned):
                return f"credit:{_label(pattern)}"
    for pattern in extra_patterns or ():
        if pattern.search(cleaned):
            return f"custom:{_label(pattern)}"
    frame = _credit_frame(key, cleaned)
    if frame is not None:
        return f"credit_frame:{_label(frame)}"
    if duration_s is not None and duration_s > 0 and not _DIGIT_RE.search(cleaned):
        signoff = _signoff(cleaned, pure=duration_s >= SIGNOFF_MAX_DURATION_S)
        if signoff is not None:
            return f"signoff:{_label(signoff)}"
    return None


def _is_hallucination_phrase(
    text: str,
    extra_patterns: Sequence[re.Pattern[str]] | None = None,
    duration_s: float | None = None,
    *,
    no_speech_prob: float | None = None,
) -> bool:
    return hallucination_phrase(text, extra_patterns, duration_s, no_speech_prob=no_speech_prob) is not None


def _echoes_prompt(text: str, prompt: str | None) -> bool:
    """True when the transcript is a verbatim run of words lifted from the domain prompt.

    Whisper treats ``initial_prompt`` as prior text, so on a clip with little decodable speech it
    sometimes simply continues the prompt.  Observed on a 3.1 s clip whose entire transcript was
    the prompt's own place list.  Contiguous-subsequence matching keeps this precise: a dispatcher
    saying "Riverton" or "Engine 1" shares words with the prompt but never its word *order*.
    """
    if not prompt:
        return False
    words = _key(text).split()
    if len(words) < MIN_PROMPT_ECHO_WORDS:
        return False
    return " ".join(words) in " ".join(_key(prompt).split())


def _is_trailing_hallucination(
    text: str, segment: TranscriptSegment, extra: Sequence[re.Pattern[str]]
) -> bool:
    """Is the last kept segment an artifact glued onto real traffic?

    The sign-off family is not consulted (no duration is passed, as before): a call that
    ends "thank you for your time" said it.  A conversational word is judged on the
    segment's own span and ``no_speech_prob``; ``segment`` is the call's last segment,
    which is either the kept one or a repeat of it that the collapse dropped.
    """
    if conversational_phrase(text) is None:
        return _is_hallucination_phrase(text, extra)
    span = segment.end - segment.start if segment.end > segment.start else None
    # The "too short to be a person" floor is about a whole call, not one word inside a
    # longer one — a closing "Okay." is well under a second — so only the upper bound
    # and the model's own doubt vote here.
    if span is not None:
        span = max(span, CONVERSATIONAL_MIN_DURATION_S)
    return conversational_is_noise(span, segment.no_speech_prob)


def assess(
    result: TranscriptResult,
    duration_s: float | None = None,
    opts: TranscribeOptions | None = None,
) -> QualityAssessment:
    """Label a transcript ``good`` / ``suspect`` / ``noise`` and explain why.

    ``duration_s`` should be the authoritative audio duration from ingest; the engine-reported
    duration is used as a fallback.  ``opts`` supplies the domain prompt, which is needed to spot
    a transcript that merely echoes it back; it may be ``None``.
    """
    segments = result.segments
    seg_texts = [s.text for s in segments]
    kept_texts, collapsed = collapse_repeats(seg_texts)
    if kept_texts:
        cleaned = normalize_text(" ".join(t.strip() for t in kept_texts))
    else:
        cleaned = normalize_text(result.text)

    reasons: list[str] = []
    quality = Quality.GOOD

    def mark(level: Quality, reason: str) -> None:
        nonlocal quality
        if reason not in reasons:
            reasons.append(reason)
        if level is Quality.NOISE or (level is Quality.SUSPECT and quality is Quality.GOOD):
            quality = level

    # --- 1. nothing at all (VAD ate the clip, or genuine silence)
    if not cleaned:
        return QualityAssessment(quality=Quality.NOISE, reasons=["empty_text"], cleaned_text="")

    # --- 2. every segment scored as non-speech
    probs = [s.no_speech_prob for s in segments if s.no_speech_prob is not None]
    if probs and all(p > NO_SPEECH_THRESHOLD for p in probs):
        mark(Quality.NOISE, f"no_speech_prob>{NO_SPEECH_THRESHOLD}")

    # --- 3. a known caption artifact: the exact list, the builtin families, or the
    #     operator's own patterns from ``stt_hallucination_patterns``.  The sign-off family
    #     needs the audio length, so it is resolved here rather than at check 7.
    #     The conversational words ("Okay.", "Thank you.") are judged on the worst segment's
    #     ``no_speech_prob`` and the audio length; kept, they are still only ``suspect``.
    audio_s = duration_s if duration_s and duration_s > 0 else result.duration
    extra = compile_patterns(opts.hallucination_patterns if opts else None)
    worst_no_speech = max(probs) if probs else None
    phrase = hallucination_phrase(cleaned, extra, audio_s, no_speech_prob=worst_no_speech)
    if phrase is not None:
        mark(Quality.NOISE, f"hallucination_phrase:{phrase}")
    else:
        spoken = conversational_phrase(cleaned)
        if spoken is not None:
            mark(Quality.SUSPECT, f"conversational_phrase:{spoken}")

    # --- 4. decoder loop (gzip ratio blows up on repeated tokens)
    if any(
        s.compression_ratio is not None and s.compression_ratio > MAX_COMPRESSION_RATIO for s in segments
    ):
        mark(Quality.SUSPECT, "repeat_loop")

    # --- 5. consecutive identical segments were merged away
    if collapsed:
        mark(Quality.SUSPECT, f"collapsed_repeats={collapsed}")

    # --- 6. low decode confidence
    logprobs = [s.avg_logprob for s in segments if s.avg_logprob is not None]
    if logprobs and min(logprobs) < MIN_AVG_LOGPROB:
        mark(Quality.SUSPECT, "low_avg_logprob")

    # --- 6b. the engine reported no confidence signals at all
    #     OVMS answers with text and nothing else, and openvino-genai reports no per-segment
    #     probabilities either.  Checks 2 and 6 then have no input and simply do not vote, so
    #     the label rests on the text-based heuristics alone.  Say so on the row rather than
    #     letting a "good" from three checks look like a "good" from five.  Advisory only: it
    #     never changes the label.
    if segments and not probs and not logprobs and "no_model_metrics" not in reasons:
        reasons.append("no_model_metrics")

    # --- 7. more words than could physically have been spoken
    if audio_s and audio_s > 0:
        wps = len(cleaned.split()) / audio_s
        if wps > MAX_WORDS_PER_SECOND:
            mark(Quality.SUSPECT, f"words_per_second={wps:.1f}")

    # --- 8. the decoder just continued the domain prompt instead of decoding audio
    if _echoes_prompt(cleaned, opts.prompt if opts else None):
        mark(Quality.NOISE, "prompt_echo")

    # --- 9. trailing caption artifact appended after the real traffic ended
    #     (observed: " Thank you for watching." at 30-60 s on a 30.3 s fire dispatch).
    #     A closing "Okay." is judged on its own segment's length and ``no_speech_prob``:
    #     a unit acknowledging at the end of an exchange is real traffic, not a tail.
    if len(kept_texts) > 1 and _is_trailing_hallucination(kept_texts[-1], segments[-1], extra):
        trimmed = kept_texts[:-1]
        cleaned = normalize_text(" ".join(t.strip() for t in trimmed))
        mark(Quality.SUSPECT, "trailing_hallucination")

    return QualityAssessment(quality=quality, reasons=reasons, cleaned_text=cleaned)
