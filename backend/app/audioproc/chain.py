"""What to do to one recording, decided from its own measurements (ARCHITECTURE §5.5).

Everything in this module is pure: numbers and settings in, numbers and an ffmpeg ``-af``
string out.  No file is opened and no process is started, which is what makes the decisions
— *should this clip be trimmed, is it too short for loudnorm, how much gain may it have* —
testable without ffmpeg and reviewable without reading a subprocess call.
:mod:`app.audioproc.process` is the half that runs it.

The chain is built in the order the audio has to meet it:

1. **trim** — ``atrim`` to the first and last sample that clears the dead-air threshold,
   with ``audio_trim_keep_ms`` of padding left on each end so a word does not start on
   sample zero.  ``asetpts`` renumbers afterwards, because everything downstream (the tone
   envelope, ``enable=between(t,…)``) is expressed in the trimmed timebase.
2. **tones** — attenuated or cut, per ``audio_tone_handling``.  Before the loudness stage
   on purpose: a page tone is the loudest thing in the archive, and normalizing *first*
   would set the whole clip's gain from a tone that is about to be turned down 24 dB,
   leaving the speech that follows as quiet as it started.
3. **band-pass and noise reduction** — ``highpass``/``lowpass`` at
   ``audio_bandpass_low_hz`` / ``audio_bandpass_high_hz`` (300-3800 Hz, the band a P25
   vocoder actually carries), then ``afftdn=nf=-35:nr=18`` by default; ``arnndn`` when a
   model is available.  Before the gain, so the gain is not applied to noise that is about
   to be removed.  The band edges come first because the rumble below 300 Hz was never in
   the vocoder's output, and removing it outright is both cheaper and safer than asking a
   denoiser to guess at it: measured over 20 production calls, this chain gains **+17.2 dB**
   of SNR against +0.3 for the band-pass with no denoiser behind it (and the pre-v4
   ``afftdn=nf=-25`` alone measured +4.0 on the earlier corpus), with the 2-8 Hz speech
   envelope unharmed (the 2026-09-14 band-pass measurement).

   **Both numbers moved off their v4 values because they were cutting the top of the
   voice** — the operator heard it before it was measured.  ``lowpass`` is a two-pole
   Butterworth, so a 3400 Hz corner is already about -2 dB at 3 kHz, and an IMBE/AMBE
   vocoder carries consonant energy to about 3.7 kHz; 3800 is above everything an 8 kHz
   source can hold and measures as a no-op on this archive's own files (-0.01 dB in
   2.5-3.8 kHz) while still being a real edge for a higher-rate source.  ``nf=-30`` is the
   larger error: the *measured* noise floor of a production call is a median -63 dBFS, so
   -30 told ``afftdn`` the floor sat 33 dB above where it is, and spectral subtraction
   against an invented floor takes the quietest real thing in the band with it — the
   fricatives.  ``nf=-35`` removes the same noise (-18.3 dB of the noise-frame level,
   against -18.6 for ``nf=-30``) for -1.06 dB of 2.5-3.8 kHz speech energy instead of
   -3.35.  ``anlmdn`` is selectable and **not recommended**: on this system's vocoder audio
   it *raised* the noise floor 8-9 dB (Δsnr -6.05 on radio 5233201, +0.35 on 5233054) — it
   makes the hiss worse, not better.
4. **gate** — ``agate``, at a threshold read off *this clip's* level and capped by
   ``audio_gate_db`` (:func:`gate_threshold_db`).  This is the filter that keeps a 30 dB
   lift on a quiet talker from turning the hiss between his words into a roar — and the
   threshold is relative because a fixed one applied *before* the gain sat above the
   content of the quiet calls it was meant to protect, which is the other half of the same
   problem.
5. **loudness** — ``loudnorm`` two-pass for anything at least :data:`LOUDNORM_MIN_S` long,
   a plain measured ``volume=<x>dB`` (plus :data:`VOLUME_BRANCH_OFFSET_DB`, which is what
   makes the two branches land in the same place) for anything shorter.  loudnorm needs about three
   seconds to measure an integrated loudness at all; asked for one on a 1.4 s call it
   reports a number derived from almost nothing and applies a wild correction, which on
   this corpus is most of the traffic (the median call is 2.2 s).
6. **limiter** — ``alimiter`` at ``audio_true_peak_db``, always, whichever branch ran.  It
   is the one guarantee the file makes: nothing in the archive clips.  Two details are load
   bearing.  ``level=disabled`` turns off ``alimiter``'s "auto level", which otherwise
   *raises* the whole clip until its loudest sample sits exactly on the ceiling — a limiter
   that guarantees the peak instead of capping it, and the reason 804 of one afternoon's
   930 calls came out of the pass reading ``peak_db_after = 0.0``.  And the ceiling itself
   is well below 0 dBFS (-4 by default) because the number the limiter enforces is a
   **PCM** peak, while what a listener decodes is a 16 kbps MP3 at 8 kHz: a lossy codec
   reconstructs a waveform that overshoots the one it was given, by 1.5-2 dB on this
   material, so a chain that stops at -1.5 dBFS decodes above full scale and clips.

Stage 3 (band edges and denoiser) and stage 4 — and only those — are what an **audio
profile** replaces when the call's talkgroup has one (:mod:`app.audioproc.profiles`,
docs/AUDIO.md).  They are the stages that are about *this channel's noise*; the trim, the
tone envelope and the loudness tail are about every recording and stay where they are.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.audioproc.tones import ToneSegment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Settings

__all__ = [
    "GATE_BELOW_LEVEL_DB",
    "GATE_FLOOR_DB",
    "GATE_MAX_DB",
    "LOUDNORM_MIN_S",
    "NOISE_REDUCERS",
    "PIPELINE_VERSION",
    "TONE_HANDLINGS",
    "VOLUME_BRANCH_OFFSET_DB",
    "AudioConfig",
    "LoudnormStats",
    "Measurement",
    "depth_for",
    "filter_chain",
    "gain_for",
    "gate_threshold_db",
    "tone_envelope",
]

#: Bumped when a change here would make an already-processed file wrong.  Stored in
#: ``audio_meta.version``; a row whose version is behind (or whose
#: ``audio_reprocess_version`` override is ahead) is what a lazy reprocess looks for.
#:
#: 2 — the tone detector gained the frequency-stability test and pattern-aware duration
#: rules (``app.audioproc.tones``), so v1 rows carry both false ``single`` flags on speech
#: and warbles attenuated on only half their halves.  Everything the pass heard changed;
#: the archive has to be re-heard.
#:
#: 3 — four changes that each make a v2 file wrong (the 2026-09-10 audio review):
#: ``_absorb`` now accepts the one-hop overlap between consecutive runs, so a warble is
#: attenuated for its whole length rather than half of it; the gate threshold follows the
#: clip's own level (:func:`gate_threshold_db`) instead of sitting above the content of a
#: quiet one; the short-clip branch carries :data:`VOLUME_BRANCH_OFFSET_DB`, so short and
#: long calls land at the same level; and ``content`` can now be ``static``.
#:
#: 5 — the band edges and the denoiser's assumed floor move: ``lowpass`` 3400 -> 3800 Hz and
#: ``afftdn``'s ``nf`` -30 -> -35 dB (the 2026-09-14 band-pass measurement).  A v4 file was
#: filtered by a chain that took 3.35 dB out of the 2.5-3.8 kHz band, which is where the
#: consonants are, so a v4 file is *dulled* rather than wrong; the bump tags the new output
#: so a row's ``audio_meta.version`` still says which chain produced it.
PIPELINE_VERSION = 5

#: Below this, ``loudnorm``'s integrated measurement is not meaningful and the measured-gain
#: branch runs instead.  EBU R128 integrates over 400 ms blocks with a relative gate; three
#: seconds is the shortest clip that gives it enough ungated blocks to average.
LOUDNORM_MIN_S = 3.0

NOISE_REDUCERS = ("off", "afftdn", "arnndn", "anlmdn")
TONE_HANDLINGS = ("keep", "attenuate", "remove")

#: agate's shape.  A 4:1 ratio below the threshold ducks the noise floor without slamming
#: it to nothing, 5 ms attack lets a consonant through and 250 ms release is long enough
#: that the gate does not chatter between syllables.
_GATE_RATIO = 4.0
_GATE_ATTACK_MS = 5
_GATE_RELEASE_MS = 250

#: How far under the clip's own measured level the gate is allowed to sit, and the window
#: the answer is held inside — see :func:`gate_threshold_db`.
GATE_BELOW_LEVEL_DB = 30.0
GATE_FLOOR_DB = -70.0
GATE_MAX_DB = -40.0

#: The short-clip branch's RMS-to-LUFS correction, dB.
#:
#: :func:`gain_for` aims ``audio_target_lufs`` — a **LUFS** number — at a clip's **RMS**,
#: and the two are not the same scale: LUFS is K-weighted and gated, so it reads a speech
#: clip several decibels above the flat RMS of the same samples, and a chain that hands the
#: RMS the LUFS target lands under it.  Adding the gap back on the branch that is short by
#: it is what stops a listener hearing a step at :data:`LOUDNORM_MIN_S`.
#:
#: **Why 1.0 and not the 3.2 dB the review measured.**  That 3.2 is the distance between the
#: two branches' *population* medians over 500 calls — ``volume`` at -21.41 dB with a median
#: duration of 1.58 s against ``loudnorm`` at -18.20 dB at 6.41 s — and those are different
#: calls, so it holds the scale error *and* whatever else distinguishes a short transmission
#: from a long one.  A controlled measurement separates them: the **same** recording cut
#: either side of three seconds lands within 0.2 dB at 2 s vs 4 s, and 0.35-3.5 dB apart at
#: the two branches' median durations, depending on how much inter-word silence the clip
#: carries (loudnorm gates quiet blocks out of its integration and a flat RMS does not).
#: Median about 1 dB across both pairs, so that is the constant; at 3.2 a continuous-speech
#: pair comes out 3 dB the *wrong* way.  Much of the population gap was the gate above,
#: which was taking up to 17 dB off exactly the short quiet calls that make up the ``volume``
#: branch — so the population number cannot be trusted until the archive has been reprocessed
#: at v3, and this constant is worth re-measuring then.
VOLUME_BRANCH_OFFSET_DB = 1.0

#: loudnorm's loudness range target.  11 LU is the EBU short-form default and suits speech.
_LRA = 11.0

#: ``alimiter``'s shape, appended after the ``limit=`` this chain computes.
#:
#: ``level=disabled`` is the important one: ffmpeg's ``alimiter`` defaults to ``level=1``
#: ("auto level"), which normalizes the output so the loudest sample lands *on* the ceiling.
#: That turns a limiter into a maximizer — it lifts a clip that was never near clipping —
#: and it is why a processed file measured its peak at exactly the ceiling every time.  With
#: it off, the filter only ever attenuates, which is the whole job.
#:
#: 5 ms attack / 50 ms release are the filter's own defaults, written out because a limiter
#: whose timing is implicit is a limiter nobody can reason about from the stored chain.
_LIMITER_OPTIONS = "level=disabled:attack=5:release=50"


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _num(value: float, places: int = 3) -> str:
    """A filter-graph number: fixed point, no exponent, no trailing noise."""
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class AudioConfig:
    """The runtime settings this module reads, snapshotted for one pass."""

    enabled: bool = True
    trim_silence: bool = True
    trim_threshold_db: float = -40.0
    trim_keep_ms: int = 150
    target_lufs: float = -16.0
    true_peak_db: float = -4.0
    max_gain_db: float = 30.0
    gate_db: float = -50.0
    silent_below_db: float = -60.0
    #: the level a stretch of the *processed* copy has to clear to count as speech, and how
    #: many seconds of it a call needs before it is one (``audio_meta.content``)
    speech_floor_db: float = -40.0
    min_speech_seconds: float = 0.25
    #: …and the two numbers that separate broadband static from a quiet talker: how flat a
    #: frame's in-band spectrum has to be to count as noise-shaped, and what share of the
    #: live frames have to be (``app.audioproc.process.flat_fraction``, ``content=static``).
    static_flatness: float = 0.30
    static_frame_share: float = 0.70
    #: the speech band kept ahead of the denoiser, Hz; 0 on either edge drops that half
    bandpass_low_hz: float = 300.0
    bandpass_high_hz: float = 3800.0
    noise_reduction: str = "afftdn"
    noise_floor_db: float = -35.0
    #: ``afftdn``'s ``nr=`` — how many dB of the assumed floor to remove.  0 omits it.
    noise_reduction_nr: float = 18.0
    rnnoise_model: str = ""
    tone_detect: bool = True
    tone_handling: str = "attenuate"
    tone_attenuate_db: float = 24.0
    #: …and the depth used for a span that belongs to a *page* — two_tone, warble, alert.  A
    #: page is information: the operator wants it audible under the dispatch that follows,
    #: where the console's single held attention tone is only loud.
    page_tone_attenuate_db: float = 18.0
    reprocess_version: int = PIPELINE_VERSION

    @classmethod
    def from_settings(cls, settings: Settings) -> AudioConfig:
        """Read the ``audio_*`` keys off a (runtime-merged) settings object."""
        get = lambda name, default: getattr(settings, f"audio_{name}", default)  # noqa: E731
        noise = str(get("noise_reduction", "afftdn") or "off").strip().lower()
        handling = str(get("tone_handling", "attenuate") or "keep").strip().lower()
        return cls(
            enabled=bool(get("process_enabled", True)),
            trim_silence=bool(get("trim_silence", True)),
            trim_threshold_db=float(get("trim_threshold_db", -40.0)),
            trim_keep_ms=int(get("trim_keep_ms", 150)),
            target_lufs=float(get("target_lufs", -16.0)),
            true_peak_db=float(get("true_peak_db", -4.0)),
            max_gain_db=float(get("max_gain_db", 30.0)),
            gate_db=float(get("gate_db", -50.0)),
            silent_below_db=float(get("silent_below_db", -60.0)),
            speech_floor_db=float(get("speech_floor_db", -40.0)),
            min_speech_seconds=float(get("min_speech_seconds", 0.25)),
            static_flatness=float(get("static_flatness", 0.30)),
            static_frame_share=float(get("static_frame_share", 0.70)),
            bandpass_low_hz=float(get("bandpass_low_hz", 300.0)),
            bandpass_high_hz=float(get("bandpass_high_hz", 3800.0)),
            noise_reduction=noise if noise in NOISE_REDUCERS else "afftdn",
            noise_floor_db=float(get("noise_floor_db", -35.0)),
            noise_reduction_nr=float(get("noise_reduction_nr", 18.0)),
            rnnoise_model=str(get("rnnoise_model", "") or "").strip(),
            tone_detect=bool(get("tone_detect", True)),
            tone_handling=handling if handling in TONE_HANDLINGS else "attenuate",
            tone_attenuate_db=float(get("tone_attenuate_db", 24.0)),
            page_tone_attenuate_db=float(get("page_tone_attenuate_db", 18.0)),
            reprocess_version=int(get("reprocess_version", PIPELINE_VERSION)),
        )


@dataclass(frozen=True)
class Measurement:
    """What the decoded PCM of one recording says about it, before anything is done."""

    duration_s: float
    #: whole-clip RMS and peak, dBFS.  ``-inf`` becomes -100, which is what an empty call
    #: measures and is far enough below anything real to compare against.
    mean_db: float
    peak_db: float
    #: dead air the trim would take off each end, seconds (0 when trimming is off)
    head_trim_s: float = 0.0
    tail_trim_s: float = 0.0
    #: RMS of what is left after the trim — the number the gain is computed from, because
    #: half a second of silence at the head drags the whole-clip mean down and would buy
    #: the speech a lift it does not need.
    trimmed_mean_db: float | None = None
    sample_rate: int = 8000

    @property
    def kept_s(self) -> float:
        return max(0.0, self.duration_s - self.head_trim_s - self.tail_trim_s)

    @property
    def level_db(self) -> float:
        return self.mean_db if self.trimmed_mean_db is None else self.trimmed_mean_db


@dataclass(frozen=True)
class LoudnormStats:
    """``loudnorm``'s first-pass JSON, as the second pass needs it back."""

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> LoudnormStats | None:
        try:
            stats = cls(
                input_i=float(data["input_i"]),
                input_tp=float(data["input_tp"]),
                input_lra=float(data["input_lra"]),
                input_thresh=float(data["input_thresh"]),
                target_offset=float(data["target_offset"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        # ffmpeg reports -inf (and "-inf" as a string) for a clip with nothing in it; a
        # second pass fed those produces silence or an error, so it is not attempted.
        values = (stats.input_i, stats.input_tp, stats.input_lra, stats.input_thresh)
        if any(math.isinf(v) or math.isnan(v) for v in values):
            return None
        return stats


def gain_for(measurement: Measurement, config: AudioConfig) -> float:
    """The ``volume=`` lift for the short-clip branch, dB, capped by ``audio_max_gain_db``.

    Measured against the *trimmed* mean toward ``audio_target_lufs``, plus
    :data:`VOLUME_BRANCH_OFFSET_DB` — the measured distance between the LUFS the target is
    written in and the RMS this branch reads.  Without it this branch lands 3.2 dB under the
    ``loudnorm`` branch and a listener hears every call shorter than :data:`LOUDNORM_MIN_S`
    as quieter than the one before it.  The limiter behind it means the correction costs
    headroom rather than clipping if a clip turns out not to need all of it.

    Attenuation is allowed as well as gain: a call that arrives 6 dB hot is brought down.
    A clip with no measurable level at all gets nothing; lifting silence only lifts hiss.
    """
    level = measurement.level_db
    if level <= -99.0:
        return 0.0
    wanted = config.target_lufs - level + VOLUME_BRANCH_OFFSET_DB
    return float(max(-config.max_gain_db, min(config.max_gain_db, wanted)))


def gate_threshold_db(measurement: Measurement, config: AudioConfig) -> float:
    """Where ``agate`` sits for *this* clip, dBFS.

    ``audio_gate_db`` alone is a threshold in absolute dBFS applied *before* the gain, and
    that is only the right number for a clip that arrived at a normal level.  Production
    one static call measures -46.28 dBFS whole-clip: the -50 dB default therefore sat 3.7 dB
    under the **content**, so a 4:1 downward ratio was applied to the speech itself and the
    ``volume=30dB`` the chain asked for came out as 19 dB.  Eighteen of the eighteen calls
    more than 6 dB below the archive's median level are on this branch, and none is a
    ``loudnorm`` undershoot.

    So the threshold follows the clip: :data:`GATE_BELOW_LEVEL_DB` under the trimmed mean,
    held inside ``[GATE_FLOOR_DB, GATE_MAX_DB]`` and never *above* the configured
    ``audio_gate_db``.  Two properties that made this the choice over moving the gate behind
    the gain stage:

    * On a normally-levelled call it is **exactly today's number** — a clip at -20 dBFS asks
      for -50, which is the default — so the change touches the quiet tail and leaves the
      calls the chain already gets right bit-identical.  Moving the gate after the gain
      changes the level of every call in the archive.
    * It keeps the gate where the chain's argument puts it, ahead of the lift, which is the
      whole reason it exists: gate *then* lift raises speech and not the hiss between the
      words, while lift *then* gate has already made the roar it is trying to prevent.

    ``audio_gate_db`` keeps its meaning as the operator's ceiling — "never gate above this" —
    and a clip with no measurable level at all gets it unchanged, because there is no level
    to be relative to.
    """
    ceiling = min(config.gate_db, GATE_MAX_DB)
    level = measurement.level_db
    if level <= -99.0:
        return config.gate_db
    return float(min(ceiling, max(GATE_FLOOR_DB, level - GATE_BELOW_LEVEL_DB)))


def _tone_stage(
    tones: tuple[ToneSegment, ...],
    config: AudioConfig,
    offset_s: float,
    *,
    select_works: bool = True,
) -> list[str]:
    """The tone envelope, with every span shifted into the post-trim timebase.

    ``tones`` is :attr:`~app.audioproc.tones.ToneReport.spans` — one entry per *pattern*,
    already merged — not the per-segment detail, so a warble is one ``volume`` stage over
    the whole burst.

    **How far a span comes down depends on what it is.**  A two-tone page, a warble or a
    repeated alert is the *message*: somebody was toned out, and a listener wants to hear it
    under the dispatch that follows, so it gets ``page_tone_attenuate_db`` (18 dB — quieter,
    still plainly there).  A ``single`` is the console's held attention tone — the attention-tone call is
    1.95 s of 993 Hz at -10.5 dB in front of 17 s of speech — which carries nothing at all
    and is simply loud, so it gets the deeper ``tone_attenuate_db``.
    """
    spans = []
    for segment in tones:
        start = max(0.0, segment.start_s - offset_s)
        end = max(start, segment.end_s - offset_s)
        if end > start:
            spans.append((start, end, segment.is_page))
    if not spans or config.tone_handling == "keep":
        return []
    if config.tone_handling == "attenuate":
        return _attenuation_stages(spans, config)
    # remove: drop the samples and renumber.  The clip gets shorter, which is why
    # ``tone_removed`` goes in the meta — a caller comparing timings against `duration_s`
    # has to know the processed copy is no longer the same length.
    if not select_works:
        return [_trim_cut([(a, b) for a, b, _ in spans]), "asetpts=N/SR/TB"]
    inside = "+".join(f"between(t,{_num(a)},{_num(b)})" for a, b, _ in spans)
    return [f"aselect='not({inside})'", "asetpts=N/SR/TB"]


def _trim_cut(cuts: list[tuple[float, float]]) -> str:
    """The tone cut without ``aselect``: ``atrim`` each piece that stays, ``concat`` them.

    For an ffmpeg whose ``aselect`` passes every frame whatever its expression — 8.0.x does
    (an upstream regression, fixed in their master) — see
    :func:`app.audioproc.process.aselect_works`.  ``aselect`` drops whole decoded frames
    whose start falls in a span; this cuts at the sample, so the two differ by under one
    frame at each edge.  Still one input and one output, so it stays a plain ``-af`` chain:
    the stage before feeds ``asplit`` and the stage after continues from ``concat``.
    """
    pieces: list[tuple[float, float | None]] = []
    cursor = 0.0
    for start, end in sorted(cuts):
        if start > cursor:
            pieces.append((cursor, start))
        cursor = max(cursor, end)
    pieces.append((cursor, None))

    def trim(start: float, end: float | None) -> str:
        opts = ([f"start={_num(start)}"] if start > 0.0 else []) + (
            [f"end={_num(end)}"] if end is not None else []
        )
        return "atrim=" + ":".join(opts)

    if len(pieces) == 1:
        return trim(*pieces[0])
    n = len(pieces)
    graph = f"asplit={n}" + "".join(f"[tcut{i}]" for i in range(n))
    for i, (start, end) in enumerate(pieces):
        graph += f";[tcut{i}]{trim(start, end)}[tkeep{i}]"
    return graph + ";" + "".join(f"[tkeep{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1"


def depth_for(is_page: bool, config: AudioConfig) -> float:
    """The attenuation one span gets, in dB — see :func:`_tone_stage`."""
    return config.page_tone_attenuate_db if is_page else config.tone_attenuate_db


def _attenuation_stages(spans: Sequence[tuple[float, float, bool]], config: AudioConfig) -> list[str]:
    """One `volume` per span rather than one expression.

    ``enable`` takes a boolean, a separate stage per span keeps the graph readable in a log
    line, and each span carries its own depth — which one expression could not express at all.
    """
    return [
        f"volume=volume=-{_num(abs(depth_for(page, config)), 1)}dB:"
        f"enable='between(t,{_num(a)},{_num(b)})'"
        for a, b, page in spans
    ]


def tone_envelope(
    spans: Sequence[ToneSegment], config: AudioConfig, offset_s: float = 0.0
) -> list[str]:
    """The attenuation stages for ``spans``, shifted by ``offset_s`` — the *public* form.

    The stored chain builds this once, inside :func:`_tone_stage`.  The **export** chain has
    to build it a second time (:func:`app.exporters.loudness.export_filter`): its loudness
    stage measures the processed file, sees a call that is nothing but an attenuated tone,
    and lifts the tone straight back to the export target — the tone-only call left for
    OpenMHz at -11 LUFS with the 993 Hz tone we had just taken down to -33 dB.  Re-applying
    the same envelope *after* the loudness stage is what stops an export ever being louder in
    a tone span than the file it came from.
    """
    stages = []
    for segment in spans:
        start = max(0.0, segment.start_s - offset_s)
        end = max(start, segment.end_s - offset_s)
        if end <= start:
            continue
        depth = (
            segment.attenuate_db
            if segment.attenuate_db is not None
            else depth_for(segment.is_page, config)
        )
        stages.append(
            f"volume=volume=-{_num(abs(depth), 1)}dB:enable='between(t,{_num(start)},{_num(end)})'"
        )
    return stages


def _bandpass_stages(config: AudioConfig) -> list[str]:
    """The speech band-pass that runs ahead of the denoiser, or nothing when it is off.

    **The low edge is the one that earns its keep.**  Below 300 Hz there is only rumble the
    codec never carried, and cutting it is free intelligibility rather than a trade: measured
    on 20 known-good calls it is most of the SNR the noise section gains over the old
    ``afftdn=nf=-25``, and it leaves the 2-8 Hz speech envelope alone, which a filter that
    was eating the speech would not.

    **The high edge is a guard rail, and 3400 Hz was inside the voice.**  ``lowpass`` is
    ffmpeg's two-pole Butterworth, so a 3400 Hz corner is already about -2 dB at 3 kHz, and
    an IMBE/AMBE vocoder puts consonant energy out to roughly 3.7 kHz — on 20 production
    calls the 3400 edge cost **3.35 dB** of the 2.5-3.8 kHz band against the raw file, which
    is what an operator hears as a voice with its top taken off.  At 3800 Hz the edge is
    above everything an 8 kHz recording can carry (this archive's originals are 8 kHz
    16 kbps MP3, down 50 dB by 3.75 kHz, and the filter measures -0.01 dB there), so it
    costs nothing today and is still a real edge if a source ever arrives at a higher rate.
    Keeping it beats setting it to 0 for exactly that reason
    (the 2026-09-14 band-pass measurement).

    ``0`` on either setting drops that edge, which is how an operator says "leave the low
    end alone" without having to switch the whole stage off.
    """
    stages = []
    if config.bandpass_low_hz > 0.0:
        stages.append(f"highpass=f={_num(config.bandpass_low_hz, 1)}")
    if config.bandpass_high_hz > 0.0:
        stages.append(f"lowpass=f={_num(config.bandpass_high_hz, 1)}")
    return stages


def _noise_stage(config: AudioConfig) -> list[str]:
    """The denoiser stage, or nothing when it is switched off.

    ``afftdn`` subtracts an *assumed* noise floor — ``nf`` — from every bin, up to ``nr`` dB
    of it, so the two numbers do different jobs: ``nr`` is how hard it pushes and ``nf`` is
    where it thinks the noise is.  Getting ``nf`` wrong in the loud direction does not buy
    more noise reduction; it buys the same reduction applied to quiet *speech*.  Measured on
    20 production calls, ``nf`` from -30 to -45 dB all take the same ~18 dB off the
    noise-frame level (the ``nr`` budget), while the 2.5-3.8 kHz speech energy lost runs
    -2.17, -1.06, -0.50, -0.25 dB across that sweep.  The floors on this system measure a
    median -63 dBFS, so -30 was 33 dB above reality and the fricatives paid for it; -35 is
    the value that keeps the whole noise reduction and holds the high band inside 1 dB
    (the 2026-09-14 band-pass measurement).
    """
    kind = config.noise_reduction
    if kind == "off":
        return []
    if kind == "anlmdn":
        # Selectable, not recommended: measured to *raise* the noise floor 8-9 dB on this
        # system's vocoder audio (the 2026-09-10 audio review).
        return ["anlmdn"]
    if kind == "arnndn" and config.rnnoise_model:
        return [f"arnndn=m='{config.rnnoise_model}'"]
    # ``arnndn`` with no model reaches here: the Admin page refuses to *save* that
    # combination, so this is only the case where the model file vanished afterwards.
    # Falling back to afftdn keeps the call processed rather than failing the job.
    stage = f"afftdn=nf={_num(config.noise_floor_db, 1)}"
    if config.noise_reduction_nr > 0.0:
        stage += f":nr={_num(config.noise_reduction_nr, 2)}"
    return [stage]


def filter_chain(
    measurement: Measurement,
    tones: tuple[ToneSegment, ...],
    config: AudioConfig,
    *,
    loudness: LoudnormStats | str | None = None,
    profile: Sequence[str] | None = None,
    select_works: bool = True,
) -> str:
    """The whole ``-af`` string for one call.

    ``loudness`` selects the loudness stage: ``"measure"`` builds the first loudnorm pass
    (``print_format=json``, nothing is encoded from it), a :class:`LoudnormStats` builds the
    second, and ``None`` uses the measured-gain branch — which is what a clip shorter than
    :data:`LOUDNORM_MIN_S` always gets, and what a longer one falls back to when the first
    pass could not measure it.

    ``profile`` is a talkgroup's own filter chain, already rendered stage by stage by
    :func:`app.audioproc.profiles.render_steps`.  When it is given it stands **in place of**
    the global band-pass, the denoiser and the gate — the stages that are about this
    channel's noise, and a profile that wants band edges declares its own — and
    everything else is unchanged: the trim is still measured from this clip, the tone
    envelope is still the detector's, and the loudness and limiter still land where the
    archive says they land.  An empty list is a profile too: it means "no denoiser, no
    gate", which is the honest way to say "this channel is clean, leave it alone".

    ``select_works`` is :func:`app.audioproc.process.aselect_works` for the installed
    ffmpeg: when it is false, tone removal cuts with ``atrim``/``concat`` instead of
    ``aselect`` (see :func:`_trim_cut`).
    """
    stages: list[str] = []

    head = measurement.head_trim_s if config.trim_silence else 0.0
    tail = measurement.tail_trim_s if config.trim_silence else 0.0
    if head > 0.0 or tail > 0.0:
        end = max(head, measurement.duration_s - tail)
        stages.append(f"atrim=start={_num(head)}:end={_num(end)}")
        stages.append("asetpts=N/SR/TB")

    stages.extend(_tone_stage(tones, config, head, select_works=select_works))

    if profile is not None:
        stages.extend(profile)
    else:
        stages.extend(_bandpass_stages(config))
        stages.extend(_noise_stage(config))
        if config.gate_db > -100.0:
            stages.append(
                f"agate=threshold={_num(_db_to_linear(gate_threshold_db(measurement, config)), 6)}:"
                f"ratio={_num(_GATE_RATIO, 1)}:attack={_GATE_ATTACK_MS}:release={_GATE_RELEASE_MS}"
            )

    target = _num(config.target_lufs, 2)
    # loudnorm's own true-peak target stays at the codec-agnostic figure: tying it to the
    # -4 dB limiter ceiling made the dynamic normalizer pull speech down while lifting an
    # attenuated tone (1.5 dB of tone separation lost for 0.2 dB of peak).  The alimiter
    # after it is what enforces the ceiling the encoder overshoot needs.
    peak = _num(LOUDNORM_TP_DB, 2)
    if loudness == "measure":
        stages.append(f"loudnorm=I={target}:TP={peak}:LRA={_num(_LRA, 1)}:print_format=json")
        # No limiter on the measuring pass: it would change the numbers the real pass is
        # about to be handed.
        return ",".join(stages)
    if isinstance(loudness, LoudnormStats):
        stages.append(
            f"loudnorm=I={target}:TP={peak}:LRA={_num(_LRA, 1)}"
            f":measured_I={_num(loudness.input_i, 2)}"
            f":measured_TP={_num(loudness.input_tp, 2)}"
            f":measured_LRA={_num(loudness.input_lra, 2)}"
            f":measured_thresh={_num(loudness.input_thresh, 2)}"
            f":offset={_num(loudness.target_offset, 2)}:linear=true"
        )
    else:
        gain = gain_for(measurement, config)
        if abs(gain) >= 0.1:
            stages.append(f"volume={_num(gain, 2)}dB")

    stages.append(f"alimiter=limit={_num(_db_to_linear(config.true_peak_db), 6)}:{_LIMITER_OPTIONS}")
    return ",".join(stages)

#: ``loudnorm``'s true-peak target, decoupled from ``audio_true_peak_db`` (see ``_loudness``).
LOUDNORM_TP_DB = -2.0
