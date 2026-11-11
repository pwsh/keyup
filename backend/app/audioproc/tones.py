"""Sustained-tone detection on decoded PCM (ARCHITECTURE §5.5).

A dispatch page is not a transmission like the others.  On many systems it is two pure
tones back to back — the two-tone page call on FIRE DISPATCH carries 625 Hz from 1.15 s to 2.11 s
and then 750 Hz from 2.18 s to 5.09 s before anybody speaks — and a listener scrubbing a
day of traffic wants to find exactly those, while a transcriber wants them out of the way.
Both needs come from the same measurement, so it is made once, here, and recorded on the
call.

**Why in Python rather than in ffmpeg.**  ffmpeg can *detect* silence and it can filter a
band, but it cannot answer "was one frequency carrying most of the energy for a third of a
second, and which one".  That is a handful of FFTs over an 8 kHz mono stream we have
already decoded for the trim measurement, which costs a millisecond or two per call — far
less than the ffmpeg process a filter-based approach would need anyway.

**The test a frame has to pass** is deliberately all three of:

``dominant bin >= 45 % of the frame's energy``
    speech has energy spread across formants and harmonics; a two-tone page has it in one
    place.  The dominant bin is summed with its two neighbours because a Hann window
    spreads a tone that is not exactly on a bin centre across three of them.
``frame level above -35 dBFS``
    a page is loud.  Without a floor, the near-silent squelch tail at the end of a call is
    "dominated" by whatever hum happens to be in it.
``300-3500 Hz``
    the band a page tone actually lives in.  Below it is mains hum and rumble, above it is
    the top of the vocoder's range where a sibilant looks tonal.

**The test a *run* of those frames has to pass** is where a tone is separated from a vowel.
Duration alone could not do it.  A held vowel clears 45 % for a third of a second all the
time — over seven days this detector flagged 291 calls ``single``, most of them 0.3-0.4 s
at 300-600 Hz, which is a voice — while a real warble alternates two frequencies every
quarter second and so was never caught by a 0.3 s floor at all.  What actually separates
them is **stability**: an encoder holds its frequency to a fraction of a hertz for the
whole run, and a voice does not.  Measured on the partial-warble call's 878 Hz halves the 90th
percentile deviation from the run's median is 0.0-0.8 Hz; measured on the vowel of
the held-vowel call, flagged as a tone, it is 18-25 Hz, and the neighbouring runs of the same vowel come
out at 410, 492 and 399 Hz.  So :data:`MAX_FREQ_SPREAD_HZ` is the gate, and duration is
then applied *by context*:

``part of a pattern: >= 0.15 s``
    three or more runs, gaps under :data:`MAX_PATTERN_GAP_S`, at one or two stable
    frequencies.  A warble's halves are 0.25 s and nothing else in the archive repeats
    like that, so the repetition itself carries the evidence a single short beep lacks.
    One frequency struck three or more times is allowed a wider :data:`MAX_REPEAT_GAP_S`,
    because there the sameness of the frequency is doing the work the closeness did.
``one of a pair: >= 0.4 s``
    Quik-Call II sends an A tone of about a second and a B tone of several.
``on its own: >= 0.5 s, and >= 55 % dominant``
    an isolated tone has no pattern behind it, so it has to be long *and* clean.  This is
    the rule the 291 false positives fail.
``on its own and held for a second: >= 1.0 s, >= 45 % dominant, spread <= 20 Hz``
    the second way to be isolated, and the one the held-tone call needed.  Its tone is 1.41 s of
    a single 993.8 Hz pitch at -14 dBFS, 0.87 dominant — but the peak wanders 18.3 Hz
    (p90), an order of magnitude past :data:`MAX_FREQ_SPREAD_HZ`, because a vocoder does
    not reproduce a tone's frequency the way an encoder generates it.  Held to 6 Hz that
    run is not a tone at all; held to nothing it is a vowel.  What makes it one is the two
    together: **a full second inside +/-20 Hz**.  The spread on its own decides nothing
    here — the false-positive vowel of the held-vowel call measures 18.4 Hz, which is the same
    number — and neither does the duration on its own.  The 7-day false-positive
    population peaked at 0.3-0.4 s and the held-vowel call's own runs are 0.38 s and 0.16 s, so a
    second is three times past all of it, and no voice holds one pitch that long that
    closely.  :data:`MIN_SINGLE_FRACTION` is relaxed to :data:`MIN_DOMINANT_FRACTION` on
    this path only: the stability is carrying the evidence the extra dominance carried on
    the half-second path.

**Harmonics were tried and rejected.**  A vocoded tone spreads into its own harmonics, so
folding the energy at 2x and 3x the peak back into the dominance fraction looks like it
should help.  Measured over the fixtures it helps the wrong call: it lifts nine frames of
the *vowel* in the held-vowel call over the frame test and not one frame of the tone in
the held-tone call, whose frames are 0.98 dominant already.  Speech is harmonic too, and
the peak of a voiced frame is usually itself a harmonic, so 2x and 3x of it land on more voice.  The dominance
fraction stays the single bin and its two leakage neighbours.

Frames are 32 ms with 50 % overlap, so the shortest reportable tone is measured by about
nine of them and its edges land within 16 ms.

**What comes out.**  ``segments`` is every accepted run, for the UI; ``spans`` is the same
runs merged one span per pattern (plus :data:`SPAN_MARGIN_S` either side), which is what
the attenuation is built from — attenuating only the halves that passed would punch the
gaps of a warble through untouched, which is exactly what the partial-warble call sounded like.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "ALERT_WINDOW_S",
    "FRAME_MS",
    "MAX_FREQ_SPREAD_HZ",
    "MAX_PATTERN_GAP_S",
    "MAX_SINGLE_SPREAD_HZ",
    "MAX_REPEAT_GAP_S",
    "MIN_DOMINANT_FRACTION",
    "MIN_LEVEL_DB",
    "MIN_PAIR_TONE_S",
    "MIN_PATTERN_SEGMENTS",
    "MIN_SINGLE_FRACTION",
    "MIN_SINGLE_TONE_S",
    "MIN_STABLE_SINGLE_TONE_S",
    "MIN_TONE_S",
    "PAGE_PATTERNS",
    "PATTERNS",
    "SPAN_MARGIN_S",
    "TONE_HI_HZ",
    "TONE_LO_HZ",
    "ToneReport",
    "ToneSegment",
    "detect_tones",
    "report_as_dict",
]

#: Analysis frame, milliseconds.  256 samples at 8 kHz — 31.25 Hz bins, which resolves the
#: 125 Hz step between the two tones of a page with four bins to spare.
FRAME_MS = 32.0
#: Fraction of the frame's spectral energy the dominant bin (with its two neighbours) must
#: carry before the frame counts as tonal.
MIN_DOMINANT_FRACTION = 0.45
#: Frames quieter than this are not judged at all.
MIN_LEVEL_DB = -35.0
#: The shortest run of tonal frames considered at all, seconds.  This is the floor for a
#: run that is *part of a pattern*; on its own a run has to clear :data:`MIN_SINGLE_TONE_S`.
#: 0.15 s is half a warble cycle on this system (the halves measure 0.25-0.28 s) with room
#: for the frame grid to clip an edge.
MIN_TONE_S = 0.15
#: An isolated tone — nothing repeating, nothing paired with it — has to hold this long.
#: The 7-day false-positive population peaked at 0.3-0.4 s (251 of 368 first segments);
#: half a second is past all of it and still short of the shortest real single beep.
MIN_SINGLE_TONE_S = 0.5
#: …and be this dominant.  The pass-band 0.45 admits a loud vowel; 0.55 does not, and a
#: real encoder tone sits at 0.6-0.9.
MIN_SINGLE_FRACTION = 0.55
#: The second way an isolated run can be a tone: held this long, at
#: :data:`MIN_DOMINANT_FRACTION`, inside :data:`MAX_SINGLE_SPREAD_HZ`.  A second is three
#: times the 0.3-0.4 s the false-positive population peaked at and past the longest run in
#: the held-vowel call (0.38 s), which is what makes the wider spread safe to allow.
MIN_STABLE_SINGLE_TONE_S = 1.0
#: How far the peak of a *second-long isolated* run may wander and still be a tone.  Much
#: wider than :data:`MAX_FREQ_SPREAD_HZ` because it is measuring something else: that limit
#: separates an encoder from a voice inside a third of a second, where a voice is already
#: moving, while this one asks only that a pitch stay put for a second, which a voice never
#: does at any width.  The held-tone call's vocoded 993.8 Hz tone measures 18.3 Hz here.
MAX_SINGLE_SPREAD_HZ = 20.0
#: Each tone of a two-tone pair.  Quik-Call II's A tone is about a second and its B tone
#: several, so this only excludes a pair of blips that happen to be 50 Hz apart.
MIN_PAIR_TONE_S = 0.4
#: How far a run's frequency may wander and still be an encoder rather than a voice: the
#: 90th percentile of |f - median(f)| over the run's frames.  A stable tone measures under
#: 1 Hz (the partial-warble call: 0.0-0.8 Hz); a vowel measures 18-25 Hz (the held-vowel call).  Six hertz is
#: an order of magnitude clear of the tones and a third of the way to the voices, which
#: leaves room for the parabolic interpolation to wobble on a tone riding a noisy channel.
MAX_FREQ_SPREAD_HZ = 6.0
#: The longest silence inside a repeating pattern.  A warble alternates with no gap at all;
#: 0.15 s covers an encoder that drops the carrier between halves without reaching across
#: to the next word of speech.
MAX_PATTERN_GAP_S = 0.15
#: The longest silence inside a *repeated* pattern — one frequency struck three or more
#: times, which is what an evacuation tone is and what a warble degrades to when only the
#: high halves clear the dominance test (the partial-warble call: four 878 Hz halves 0.2 s apart, the
#: 467.9 Hz halves between them too weak to be seen).  Wider than
#: :data:`MAX_PATTERN_GAP_S` because the gap here is a real silence, and safe to widen
#: because the frequency has to be *the same* each time, which speech does not do.
MAX_REPEAT_GAP_S = 0.5
#: How many runs make a *pattern* rather than a coincidence.
MIN_PATTERN_SEGMENTS = 3
#: Padding either side of a merged attenuation span.  Enough to cover the frame grid's 16 ms
#: of edge uncertainty and the tone's own attack, and short enough not to eat a syllable.
SPAN_MARGIN_S = 0.04
#: Every value ``ToneReport.pattern`` can take, weakest first.
PATTERNS = ("none", "single", "two_tone", "warble", "alert")
#: The band a page tone lives in.
TONE_LO_HZ = 300.0
TONE_HI_HZ = 3500.0
#: How far into the call a pattern still counts as a *page* rather than a tone somebody
#: keyed over later.  A page leads the transmission; eight seconds is well past the longest
#: two-tone sequence (about 5 s here) and short of any conversation.
ALERT_WINDOW_S = 8.0

#: Two tones are "the same tone continuing" while they stay within this of each other.
#: One bin and a half: wide enough to ride out the interpolation wobble on a real tone,
#: narrow enough that the 125 Hz step of a page is always two segments.
_FREQ_TOLERANCE_HZ = 45.0
#: The longest gap between two tones that still reads as one two-tone sequence.  Real
#: encoders run them back to back with a few tens of milliseconds between.
_MAX_TONE_GAP_S = 0.4
#: Two tones must differ by at least this much to be a *pair* rather than one tone the
#: detector split at a dropout.
_MIN_PAIR_SPREAD_HZ = 50.0


#: The patterns that mean somebody was paged rather than somebody keyed a tone over.  Public
#: because the filter chain attenuates these differently from a lone held tone.
PAGE_PATTERNS = frozenset({"two_tone", "warble", "alert"})
_PAGE_PATTERNS = PAGE_PATTERNS


@dataclass(frozen=True, eq=False)
class _Run:
    """One run of tonal frames and the two measurements the rules are decided from.

    ``eq=False`` so identity is the identity: the grouping passes a run between several
    lists and needs "the same run", not "an equal one".
    """

    segment: ToneSegment
    #: the least dominant frame in the run — an isolated tone is judged on its worst frame
    min_fraction: float
    #: 90th-percentile deviation of the per-frame peak from the run's median, hertz
    spread_hz: float


@dataclass(frozen=True)
class ToneSegment:
    """One sustained tone: when it ran, in the *source* timebase, and at what frequency."""

    start_s: float
    end_s: float
    hz: float
    #: Which pattern this belongs to, one of :data:`PATTERNS` — set on a *span*, blank on the
    #: per-run detail.  The filter chain reads it: a page is turned down gently so it stays
    #: audible, a console attention tone much harder (:func:`app.audioproc.chain._tone_stage`).
    pattern: str = ""
    #: How far this span was *actually* turned down, dB — set only when a stored
    #: ``audio_meta.tone_spans`` entry is read back, so the export chain can put the same
    #: number on rather than whatever the setting happens to say today.  ``None`` means
    #: "ask the config", which is what the detector itself always produces.
    attenuate_db: float | None = None

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def is_page(self) -> bool:
        """Was somebody paged, rather than somebody keying a tone over the channel?"""
        return self.pattern in PAGE_PATTERNS

    def as_dict(self) -> dict[str, float | str]:
        out: dict[str, float | str] = {
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "hz": round(self.hz, 1),
        }
        if self.pattern:
            out["pattern"] = self.pattern
        return out


@dataclass(frozen=True)
class ToneReport:
    """Every tone found in one recording, and what the sequence of them means."""

    segments: tuple[ToneSegment, ...] = ()
    #: One entry per *pattern*, first start to last end plus :data:`SPAN_MARGIN_S`.  This is
    #: what the filter chain attenuates or cuts; ``segments`` is the detail behind it.  A
    #: warble is one span, not six, because the quiet halves between its tones are part of
    #: the same noise to a listener and leaving them at full level is what the partial-warble call
    #: sounded like.
    spans: tuple[ToneSegment, ...] = ()
    #: one of :data:`PATTERNS`
    pattern: str = "none"
    #: the pattern is a page: two-tone, warble or alert, beginning inside
    #: :data:`ALERT_WINDOW_S`
    alert: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.segments)

    def as_dicts(self) -> list[dict[str, float]]:
        return [segment.as_dict() for segment in self.segments]

    def spans_as_dicts(self) -> list[dict[str, float]]:
        return [span.as_dict() for span in self.spans]


def _frames(samples: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """A ``(n_frames, frame)`` view over ``samples``; empty when the clip is too short."""
    if samples.size < frame:
        return np.empty((0, frame), dtype=np.float64)
    count = 1 + (samples.size - frame) // hop
    stride = samples.strides[0]
    return np.lib.stride_tricks.as_strided(
        samples, shape=(count, frame), strides=(hop * stride, stride), writeable=False
    )


def _peak_hz(power: np.ndarray, index: int, bin_hz: float) -> float:
    """Parabolic interpolation over the three bins around ``index``.

    A 625 Hz tone at 31.25 Hz bins lands exactly on bin 20 and needs no help, but nothing
    guarantees an encoder is on a bin centre; interpolating puts the reported frequency
    within a couple of hertz instead of within sixteen, which is what makes "625 and 750"
    read back as 625 and 750 rather than as two numbers near them.
    """
    if index <= 0 or index >= power.size - 1:
        return index * bin_hz
    left, mid, right = power[index - 1], power[index], power[index + 1]
    denom = left - 2.0 * mid + right
    if denom == 0:
        return index * bin_hz
    offset = 0.5 * (left - right) / denom
    return (index + float(np.clip(offset, -0.5, 0.5))) * bin_hz


def detect_tones(  # noqa: PLR0913 - every threshold is documented above and settable in tests
    samples: np.ndarray,
    sample_rate: int,
    *,
    frame_ms: float = FRAME_MS,
    min_fraction: float = MIN_DOMINANT_FRACTION,
    min_level_db: float = MIN_LEVEL_DB,
    min_duration_s: float = MIN_TONE_S,
    lo_hz: float = TONE_LO_HZ,
    hi_hz: float = TONE_HI_HZ,
    alert_window_s: float = ALERT_WINDOW_S,
    max_spread_hz: float = MAX_FREQ_SPREAD_HZ,
) -> ToneReport:
    """Find the sustained pure tones in ``samples`` (mono float, full scale ±1.0)."""
    if sample_rate <= 0 or samples.size == 0:
        return ToneReport()
    audio = np.asarray(samples, dtype=np.float64).reshape(-1)
    frame = max(64, int(round(sample_rate * frame_ms / 1000.0)))
    hop = max(1, frame // 2)
    blocks = _frames(audio, frame, hop)
    if blocks.shape[0] == 0:
        return ToneReport()

    window = np.hanning(frame)
    bin_hz = sample_rate / frame
    spectra = np.abs(np.fft.rfft(blocks * window, axis=1)) ** 2
    # Bin 0 is DC — a recorder's offset would otherwise "dominate" every quiet frame.
    spectra[:, 0] = 0.0
    total = spectra.sum(axis=1)
    # RMS of the *unwindowed* frame, so the level test is the level a meter would show.
    rms = np.sqrt(np.mean(blocks**2, axis=1))
    level_db = 20.0 * np.log10(np.maximum(rms, 1e-12))

    lo_bin = max(1, int(np.ceil(lo_hz / bin_hz)))
    hi_bin = min(spectra.shape[1] - 1, int(np.floor(hi_hz / bin_hz)))
    if hi_bin <= lo_bin:
        return ToneReport()

    band = spectra[:, lo_bin : hi_bin + 1]
    peak_index = band.argmax(axis=1) + lo_bin
    rows = np.arange(spectra.shape[0])
    # The tone plus the leakage a Hann window puts either side of it.
    lower = np.maximum(peak_index - 1, 0)
    upper = np.minimum(peak_index + 1, spectra.shape[1] - 1)
    concentrated = spectra[rows, lower] + spectra[rows, peak_index] + spectra[rows, upper]
    fraction = np.divide(
        concentrated, total, out=np.zeros_like(concentrated), where=total > 0
    )
    tonal = (fraction >= min_fraction) & (level_db >= min_level_db) & (total > 0)

    runs: list[_Run] = []
    run_start: int | None = None
    run_hz: list[float] = []
    run_fraction: list[float] = []
    for i in range(blocks.shape[0]):
        hz = _peak_hz(spectra[i], int(peak_index[i]), bin_hz) if tonal[i] else 0.0
        # Continuation is judged against the run's running *median*, not its mean: a mean
        # is dragged by every frame that joins it, so a voice sliding a few hertz a frame
        # can walk one run across hundreds of hertz.  The median stays put.
        continues = (
            tonal[i]
            and run_start is not None
            and abs(hz - float(np.median(run_hz))) <= _FREQ_TOLERANCE_HZ
        )
        if continues:
            run_hz.append(hz)
            run_fraction.append(float(fraction[i]))
            continue
        if run_start is not None:
            _close(runs, run_start, i, run_hz, run_fraction, hop, frame, sample_rate, min_duration_s)
        if tonal[i]:
            run_start, run_hz, run_fraction = i, [hz], [float(fraction[i])]
        else:
            run_start, run_hz, run_fraction = None, [], []
    if run_start is not None:
        _close(
            runs, run_start, blocks.shape[0], run_hz, run_fraction, hop, frame, sample_rate,
            min_duration_s,
        )

    return _classify(
        tuple(runs), alert_window_s, max_spread_hz, overlap_s=hop / sample_rate + 1e-6
    )


def _close(  # noqa: PLR0913 - a private accumulator, not an interface
    out: list[_Run],
    start_frame: int,
    end_frame: int,
    freqs: list[float],
    fractions: list[float],
    hop: int,
    frame: int,
    sample_rate: int,
    min_duration_s: float,
) -> None:
    """Turn a finished run of tonal frames into a measured run, if it held long enough.

    Everything that clears the (short) floor is kept here, stable or not — the stability
    test and the duration-by-context rules are applied in :func:`_classify`, which is the
    only place that knows whether a run has a pattern behind it.
    """
    start_s = start_frame * hop / sample_rate
    end_s = ((end_frame - 1) * hop + frame) / sample_rate
    if end_s - start_s + 1e-9 < min_duration_s:
        return
    median = float(np.median(freqs))
    # The 90th percentile rather than the standard deviation: one transitional frame at
    # either edge of a real tone lands far away and would triple an sd (the partial-warble call's
    # first half measures sd 4.0 Hz and p90 0.1 Hz).  The percentile ignores it and still
    # answers "did this frequency hold" for the frames that matter.
    spread = float(np.percentile(np.abs(np.asarray(freqs) - median), 90)) if freqs else 0.0
    out.append(
        _Run(
            segment=ToneSegment(start_s=start_s, end_s=end_s, hz=median),
            min_fraction=min(fractions) if fractions else 0.0,
            spread_hz=spread,
        )
    )


def _centres(runs: list[_Run]) -> list[float]:
    """The distinct frequencies in ``runs``, clustered at :data:`_MIN_PAIR_SPREAD_HZ`."""
    found: list[float] = []
    for run in runs:
        if not any(abs(run.segment.hz - centre) < _MIN_PAIR_SPREAD_HZ for centre in found):
            found.append(run.segment.hz)
    return found


def _chains(runs: list[_Run], max_gap_s: float) -> list[list[_Run]]:
    """``runs`` split wherever the silence between two of them exceeds ``max_gap_s``."""
    if not runs:
        return []
    out: list[list[_Run]] = [[runs[0]]]
    for run in runs[1:]:
        if run.segment.start_s - out[-1][-1].segment.end_s <= max_gap_s:
            out[-1].append(run)
        else:
            out.append([run])
    return out


def _absorb(
    chain: list[_Run], pool: list[_Run], max_gap_s: float, *, overlap_s: float = 0.0
) -> list[_Run]:
    """Pull the pattern's own frayed edges back in.

    A warble fading out ends in halves whose frequency measurement is noisy — the fading-warble call
    finishes with two at 7.6-8.2 Hz spread against 0.1-0.6 Hz for the rest — and dropping
    them would shorten the attenuation span short of the sound.  A run adjacent to an
    already-established pattern, at one of the frequencies that pattern is already
    alternating between, is part of it whatever its own spread says; the *context* is the
    evidence, which is the whole reason the pattern rules exist.

    **Adjacent includes overlapping by one hop.**  A run's end is
    ``(last_frame * hop + frame) / rate`` — the end of its last *frame*, not of its last
    hop — so two runs that are consecutive in frame numbering report end and start times
    that overlap by exactly one hop (16 ms at the 32 ms / 50 % frames this module uses).
    Testing the gap against zero therefore rejected the very edges this function exists to
    recover: the truncated-warble call is a 878.1/467.9 Hz warble running 1.224 s to 4.312 s
    whose span stopped at 3.064 s, leaving 1.25 s of un-attenuated tone that came out of
    the chain 4.6 dB *louder* than the speech behind it.  ``overlap_s`` is that hop, and
    nothing wider: a genuinely overlapping pair of runs is one run the frame loop split,
    while a run that starts well before the chain ends is a different sound.
    """
    centres = _centres(chain)
    taken = set(chain)
    grown = list(chain)
    changed = True
    while changed:
        changed = False
        for run in pool:
            if run in taken:
                continue
            near = any(abs(run.segment.hz - centre) <= _FREQ_TOLERANCE_HZ for centre in centres)
            if not near:
                continue
            before = grown[0].segment.start_s - run.segment.end_s
            after = run.segment.start_s - grown[-1].segment.end_s
            lower = -abs(overlap_s) - 1e-9
            if lower <= before <= max_gap_s or lower <= after <= max_gap_s:
                taken.add(run)
                grown.append(run)
                grown.sort(key=lambda r: r.segment.start_s)
                changed = True
    return grown


def _is_single(run: _Run, max_spread_hz: float) -> bool:
    """Is this leftover run a tone on its own?  Either way of being one (module docstring).

    ``max_spread_hz`` is the tight, encoder-grade limit every run in the pattern rules has
    already passed; a run reaching the second clause may have failed it, which is the whole
    point of the clause.
    """
    duration_s = run.segment.duration_s + 1e-9
    if (
        run.spread_hz <= max_spread_hz
        and duration_s >= MIN_SINGLE_TONE_S
        and run.min_fraction >= MIN_SINGLE_FRACTION
    ):
        return True
    return (
        duration_s >= MIN_STABLE_SINGLE_TONE_S
        and run.min_fraction >= MIN_DOMINANT_FRACTION
        and run.spread_hz <= max(MAX_SINGLE_SPREAD_HZ, max_spread_hz)
    )


def _group_pattern(chain: list[_Run]) -> str:
    """``warble`` for two alternating frequencies, ``alert`` for one repeated or three-plus."""
    return "warble" if len(_centres(chain)) == 2 else "alert"  # noqa: PLR2004 - a pair


def _classify(  # noqa: C901 - one decision, read top to bottom
    runs: tuple[_Run, ...],
    alert_window_s: float,
    max_spread_hz: float,
    *,
    overlap_s: float = 0.0,
) -> ToneReport:
    """Name the sequence, decide what counts, and merge each pattern into one span.

    Three rules, tried in that order, over the runs whose frequency actually held
    (:data:`MAX_FREQ_SPREAD_HZ`):

    ``pattern`` — three or more of them within :data:`MAX_PATTERN_GAP_S` of each other.
        Two distinct frequencies is a ``warble``; one repeated, or three or more, is an
        ``alert``.  These may be as short as :data:`MIN_TONE_S` each, because repetition is
        the evidence.
    ``two_tone`` — two left over, close together, far enough apart in frequency, each at
        least :data:`MIN_PAIR_TONE_S`.  The classic Quik-Call II page.
    ``single`` — one left over, at least :data:`MIN_SINGLE_TONE_S` and
        :data:`MIN_SINGLE_FRACTION` dominant, *or* :data:`MIN_STABLE_SINGLE_TONE_S` long at
        :data:`MIN_DOMINANT_FRACTION` and inside :data:`MAX_SINGLE_SPREAD_HZ`.
        Deliberately the hardest rule to pass: an isolated tone is the shape a held vowel
        also has, and only the second clause admits a run the tight stability limit
        rejected.
    """
    stable = [run for run in runs if run.spread_hz <= max_spread_hz]
    # Runs too wobbly for the pattern rules but long and steady enough to be a tone on
    # their own (module docstring).  They are kept out of `stable` deliberately: a pattern
    # is built from encoder-grade runs, and one of these may still be *absorbed* into a
    # pattern by context, which is the existing rule for a frayed edge.
    lenient = [
        run
        for run in runs
        if run.spread_hz > max_spread_hz and _is_single(run, max_spread_hz)
    ]
    if not stable and not lenient:
        return ToneReport()

    groups: list[tuple[str, list[_Run]]] = []
    loose: list[_Run] = []
    claimed: set[_Run] = set()
    for chain in _chains(stable, MAX_PATTERN_GAP_S):
        if len(chain) >= MIN_PATTERN_SEGMENTS:
            grown = _absorb(chain, list(runs), MAX_PATTERN_GAP_S, overlap_s=overlap_s)
            groups.append((_group_pattern(grown), grown))
            claimed.update(grown)
        else:
            loose.extend(chain)
    loose = [run for run in loose if run not in claimed]

    # Second pass, over what the first left: the same frequency struck three or more times
    # across a real silence.  A warble whose quiet halves went unseen arrives here, and so
    # does an evacuation tone.
    rest: list[_Run] = []
    for chain in _chains(loose, MAX_REPEAT_GAP_S):
        if len(chain) >= MIN_PATTERN_SEGMENTS and len(_centres(chain)) == 1:
            grown = _absorb(chain, list(runs), MAX_REPEAT_GAP_S, overlap_s=overlap_s)
            groups.append(("alert", grown))
            claimed.update(grown)
        else:
            rest.extend(chain)
    loose = [run for run in rest if run not in claimed]
    loose = sorted(
        loose + [run for run in lenient if run not in claimed],
        key=lambda run: run.segment.start_s,
    )

    index = 0
    while index < len(loose):
        first = loose[index]
        second = loose[index + 1] if index + 1 < len(loose) else None
        paired = (
            second is not None
            and second.segment.start_s - first.segment.end_s <= _MAX_TONE_GAP_S
            and abs(second.segment.hz - first.segment.hz) >= _MIN_PAIR_SPREAD_HZ
            and first.segment.duration_s + 1e-9 >= MIN_PAIR_TONE_S
            and second.segment.duration_s + 1e-9 >= MIN_PAIR_TONE_S
            # A pair is an encoder sending two tones: both halves are held to the tight
            # limit, so a lenient run never pairs with anything.
            and first.spread_hz <= max_spread_hz
            and second.spread_hz <= max_spread_hz
        )
        if paired and second is not None:
            groups.append(("two_tone", [first, second]))
            index += 2
            continue
        if _is_single(first, max_spread_hz):
            groups.append(("single", [first]))
        index += 1

    if not groups:
        return ToneReport()
    groups.sort(key=lambda group: group[1][0].segment.start_s)

    segments = sorted(
        (run.segment for _, members in groups for run in members), key=lambda seg: seg.start_s
    )
    spans = tuple(_span(name, members) for name, members in groups)
    pattern = max((name for name, _ in groups), key=PATTERNS.index)
    paging = [
        members[0].segment.start_s for name, members in groups if name in _PAGE_PATTERNS
    ]
    alert = bool(paging) and min(paging) <= alert_window_s
    return ToneReport(
        segments=tuple(segments), spans=spans, pattern=pattern, alert=alert
    )


def _span(pattern: str, members: list[_Run]) -> ToneSegment:
    """One pattern's runs as a single attenuation span, margins included.

    The span carries the pattern's *name* as well as its edges, because how far it is turned
    down depends on what it is: a two-tone page stays audible, a console attention tone does
    not need to be.
    """
    start = min(run.segment.start_s for run in members) - SPAN_MARGIN_S
    end = max(run.segment.end_s for run in members) + SPAN_MARGIN_S
    hz = float(np.median([run.segment.hz for run in members]))
    return ToneSegment(start_s=max(0.0, start), end_s=end, hz=hz, pattern=pattern)


def report_as_dict(report: ToneReport) -> dict[str, Any]:
    """The JSON shape stored under ``calls.audio_meta``."""
    return {
        "tones": report.as_dicts(),
        "tone_spans": report.spans_as_dicts(),
        "tone_pattern": report.pattern,
        "alert": report.alert,
    }
