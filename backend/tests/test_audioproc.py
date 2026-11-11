"""The audio-processing stage: tone detection, the decisions, and one real ffmpeg pass (§5.5).

Everything except the last class runs without ffmpeg and without a database — the tone
detector and the chain builder are pure by construction, which is the point of the split.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.audioproc.chain import (
    GATE_FLOOR_DB,
    LOUDNORM_MIN_S,
    PIPELINE_VERSION,
    VOLUME_BRANCH_OFFSET_DB,
    AudioConfig,
    LoudnormStats,
    Measurement,
    _bandpass_stages,
    _noise_stage,
    filter_chain,
    gain_for,
    gate_threshold_db,
)
from app.audioproc.process import (
    PROCESSED_SUFFIX,
    STATIC_MAX_LEVEL_DB,
    AudioProcessError,
    decode_pcm,
    empty_clip_reason,
    flat_fraction,
    measure,
    process_file,
    processed_path_for,
    speech_seconds,
)
from app.audioproc.tones import MAX_FREQ_SPREAD_HZ, detect_tones
from tests.conftest import requires_db

SR = 8000
FIXTURES = Path(__file__).parent / "fixtures" / "audio"
#: Synthetic 8 kHz PCM excerpts (`fixtures/gen_fixtures.py`), each modelled on a production
#: call the tone rules were measured on: lead-in, the tone region, then synthetic speech.
TONE_FIXTURES = Path(__file__).parent / "fixtures" / "tones"
#: …and one excerpt that is only speech, for the level tests: 4 s of synthetic voiced speech
#: (harmonics under moving formants, syllables and pauses), which has a transmission's
#: RMS-against-LUFS behaviour where a noise generator does not.
SPEECH_FIXTURES = Path(__file__).parent / "fixtures" / "speech"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def tone(hz: float, seconds: float, *, amplitude: float = 0.5, sample_rate: int = SR) -> np.ndarray:
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * hz * t)


def silence(seconds: float, sample_rate: int = SR) -> np.ndarray:
    return np.zeros(int(sample_rate * seconds))


def warble(
    lo_hz: float, hi_hz: float, half_s: float, halves: int, *, amplitude: float = 0.5
) -> np.ndarray:
    """Two frequencies alternating, the shape of calls 1170192 and 1170733."""
    return np.concatenate(
        [tone(hi_hz if n % 2 == 0 else lo_hz, half_s, amplitude=amplitude) for n in range(halves)]
    )


def vibrato(hz: float, seconds: float, *, depth_hz: float, rate_hz: float = 5.0) -> np.ndarray:
    """A tone whose frequency wobbles — tonal by every frame test, unstable across the run."""
    t = np.arange(int(SR * seconds)) / SR
    phase = 2.0 * np.pi * (hz * t - (depth_hz / (2.0 * np.pi * rate_hz)) * np.cos(2.0 * np.pi * rate_hz * t))
    return 0.5 * np.sin(phase)


def vowel(seconds: float, *, f0_hz: float = 140.0, drift_hz: float = 25.0) -> np.ndarray:
    """A harmonic stack over a slowly drifting pitch: what the 291 false positives were."""
    t = np.arange(int(SR * seconds)) / SR
    f0 = f0_hz + drift_hz * t / max(seconds, 1e-9)
    phase = 2.0 * np.pi * np.cumsum(f0) / SR
    stack = sum(a * np.sin(k * phase) for k, a in ((3, 0.6), (4, 0.3), (5, 0.15), (6, 0.08)))
    return 0.5 * stack / float(np.max(np.abs(stack)))


def formant_vowel(
    seconds: float = 0.8, *, jitter_hz: float = 60.0, rate_hz: float = 6.0, seed: int = 3
) -> np.ndarray:
    """A loud open vowel: three formants, each wandering ±60 Hz.

    The shape the isolated-tone rules have to keep rejecting.  A vowel's first formant is
    dominant enough to be a "tonal" frame, so what disqualifies it is that nothing about it
    holds still — which is the measurement the second `single` clause trades duration for.
    """
    t = np.arange(int(SR * seconds)) / SR
    rng = np.random.RandomState(seed)
    tracks = [
        amp * np.sin(2.0 * np.pi * np.cumsum(hz + jitter_hz * np.sin(2.0 * np.pi * rate_hz * t + rng.rand() * 6.0)) / SR)
        for hz, amp in ((500.0, 1.0), (1500.0, 0.6), (2500.0, 0.3))
    ]
    stack = sum(tracks)
    return 0.6 * stack / float(np.max(np.abs(stack)))


def fixture(name: str) -> np.ndarray:
    """A synthetic tone-region excerpt, 8 kHz mono PCM (`TONE_FIXTURES`)."""
    return np.fromfile(TONE_FIXTURES / f"{name}.f32", dtype=np.float32).astype(np.float64)


def voiced_speech() -> np.ndarray:
    """Four seconds of synthetic voiced speech (`SPEECH_FIXTURES`)."""
    return np.fromfile(SPEECH_FIXTURES / "voiced.f32", dtype=np.float32).astype(np.float64)


def at_level(signal: np.ndarray, db: float) -> np.ndarray:
    """``signal`` rescaled to an RMS of ``db`` dBFS."""
    rms = float(np.sqrt(np.mean(np.asarray(signal, dtype=np.float64) ** 2)))
    return np.asarray(signal, dtype=np.float64) * (10.0 ** (db / 20.0) / max(rms, 1e-12))


def speechish(seconds: float, *, seed: int = 0, sample_rate: int = SR) -> np.ndarray:
    """Broadband noise shaped into syllables — tonal in no single bin, which is the point."""
    rng = np.random.RandomState(seed)
    n = int(sample_rate * seconds)
    noise = rng.randn(n)
    envelope = 0.5 + 0.5 * np.sin(2.0 * np.pi * 4.0 * np.arange(n) / sample_rate)
    return 0.2 * noise * envelope


# --------------------------------------------------------------------------------------
# tone detection
# --------------------------------------------------------------------------------------


class TestToneDetection:
    def test_two_tone_page_is_found_and_flagged_as_an_alert(self) -> None:
        """Call 1103864's shape: 625 Hz then 750 Hz, then voice (ARCHITECTURE §5.5)."""
        signal = np.concatenate(
            [silence(1.15), tone(625, 0.96), silence(0.07), tone(750, 2.91), speechish(5.0)]
        )
        report = detect_tones(signal, SR)

        assert report.pattern == "two_tone"
        assert report.alert is True
        assert report.detected is True
        assert len(report.segments) == 2
        first, second = report.segments
        assert first.hz == pytest.approx(625, abs=8)
        assert second.hz == pytest.approx(750, abs=8)
        # The measured span from production was 1.15-2.11 and 2.18-5.09; the detector's
        # frame grid puts it within one 16 ms hop of that.
        assert first.start_s == pytest.approx(1.15, abs=0.05)
        assert second.end_s == pytest.approx(5.09, abs=0.05)
        # One pattern, one attenuation span, margins included.
        assert len(report.spans) == 1
        assert report.spans[0].start_s == pytest.approx(1.11, abs=0.05)
        assert report.spans[0].end_s == pytest.approx(5.13, abs=0.05)

    def test_a_pair_of_blips_is_not_a_two_tone(self) -> None:
        """Each tone of a pair has to hold 0.4 s; Quik-Call II's shortest is about 1 s."""
        signal = np.concatenate([tone(625, 0.25), silence(0.05), tone(750, 0.25)])
        assert detect_tones(signal, SR).pattern == "none"

    def test_speech_like_noise_has_no_tone(self) -> None:
        report = detect_tones(speechish(4.0, seed=7), SR)
        assert report.segments == ()
        assert report.pattern == "none"
        assert report.alert is False

    def test_a_short_beep_does_not_hold_long_enough(self) -> None:
        """0.2 s of a clean 1 kHz tone has no pattern behind it, so it is not a tone."""
        signal = np.concatenate([silence(0.2), tone(1000, 0.2), silence(0.2)])
        report = detect_tones(signal, SR)
        assert report.segments == ()
        assert report.pattern == "none"

    def test_one_long_beep_is_a_single_not_a_page(self) -> None:
        report = detect_tones(np.concatenate([silence(0.2), tone(1000, 1.5)]), SR)
        assert report.pattern == "single"
        assert report.detected is True
        assert report.alert is False

    def test_a_stable_half_second_tone_is_a_single(self) -> None:
        report = detect_tones(np.concatenate([silence(0.2), tone(1000, 0.6), silence(0.2)]), SR)
        assert report.pattern == "single"
        assert len(report.spans) == 1

    def test_a_tone_just_under_the_isolated_floor_is_nothing(self) -> None:
        """0.35 s at 492 Hz is call 1170568's shape — a vowel, and 291 of these in a week."""
        report = detect_tones(np.concatenate([silence(0.2), tone(492, 0.35), silence(0.2)]), SR)
        assert report.pattern == "none"

    def test_a_vibrato_tone_is_not_stable_enough(self) -> None:
        """±30 Hz of wobble is tonal every frame and an encoder in none of them."""
        signal = np.concatenate([silence(0.2), vibrato(1000, 0.6, depth_hz=30.0), silence(0.2)])
        assert detect_tones(signal, SR).pattern == "none"

    def test_a_wobbling_tone_held_a_full_second_is_a_single(self) -> None:
        """The second `single` clause: call 2394781's shape, made synthetic.

        ±10 Hz of wobble measures past `MAX_FREQ_SPREAD_HZ`, so this run is not
        encoder-grade and can be no part of a pattern.  Held for 1.2 s it is still a tone —
        no voice keeps one pitch inside ±20 Hz that long — and at 0.6 s the same signal is
        nothing, which is the duration doing the deciding.
        """
        held = np.concatenate([silence(0.2), vibrato(1000, 1.2, depth_hz=10.0), silence(0.2)])
        brief = np.concatenate([silence(0.2), vibrato(1000, 0.6, depth_hz=10.0), silence(0.2)])
        assert detect_tones(held, SR).pattern == "single"
        assert detect_tones(brief, SR).pattern == "none"

    def test_a_loud_vowel_is_not_a_tone_however_long_it_is_held(self) -> None:
        """0.8 s of 500/1500/2500 Hz formants jittering ±60 Hz: dominant, never stable."""
        signal = np.concatenate([silence(0.2), formant_vowel(0.8), silence(0.2)])
        assert detect_tones(signal, SR).pattern == "none"
        # …and it is not a near miss on duration either.
        assert detect_tones(
            np.concatenate([silence(0.2), formant_vowel(2.0), silence(0.2)]), SR
        ).pattern == "none"

    def test_a_drifting_vowel_is_not_a_tone(self) -> None:
        signal = np.concatenate([silence(0.2), vowel(0.8), silence(0.2)])
        assert detect_tones(signal, SR).pattern == "none"

    def test_alternating_frequencies_are_a_warble(self) -> None:
        """Renamed from `three tones are an alert`: 700-900-700 is two frequencies
        alternating, which the pattern vocabulary now names `warble` rather than folding
        into `alert`.  `alert` is kept for one frequency repeated or three distinct ones."""
        signal = np.concatenate(
            [tone(700, 0.5), silence(0.05), tone(900, 0.5), silence(0.05), tone(700, 0.5)]
        )
        report = detect_tones(signal, SR)
        assert report.pattern == "warble"
        assert report.alert is True
        assert len(report.spans) == 1

    def test_short_halves_count_when_they_repeat(self) -> None:
        """A quarter-second half is under every isolated rule and is still a warble.

        This is the false negative the rework was for: twelve halves of 0.25 s alternating
        between two frequencies used to produce nothing at all.
        """
        signal = np.concatenate([silence(0.5), warble(468, 878, 0.25, 12), speechish(3.0)])
        report = detect_tones(signal, SR)
        assert report.pattern == "warble"
        assert report.alert is True
        assert len(report.segments) >= 10
        assert len(report.spans) == 1
        assert report.spans[0].start_s == pytest.approx(0.46, abs=0.06)
        assert report.spans[0].end_s == pytest.approx(3.54, abs=0.08)

    def test_one_frequency_struck_three_times_is_an_alert(self) -> None:
        signal = np.concatenate(
            [tone(880, 0.4), silence(0.2), tone(880, 0.4), silence(0.2), tone(880, 0.4)]
        )
        report = detect_tones(signal, SR)
        assert report.pattern == "alert"
        assert report.alert is True
        assert len(report.spans) == 1

    def test_a_pair_late_in_the_call_is_a_tone_but_not_a_page(self) -> None:
        """A page leads the transmission; two tones ten seconds in are somebody keying over."""
        signal = np.concatenate([speechish(10.0), tone(625, 0.5), silence(0.05), tone(750, 0.5)])
        report = detect_tones(signal, SR)
        assert report.pattern == "two_tone"
        assert report.alert is False

    def test_a_quiet_tone_is_below_the_level_floor(self) -> None:
        report = detect_tones(tone(1000, 1.0, amplitude=0.002), SR)
        assert report.segments == ()

    def test_out_of_band_tones_are_ignored(self) -> None:
        for hz in (120.0, 3800.0):
            assert detect_tones(tone(hz, 1.0), SR).segments == (), hz

    def test_empty_audio_is_not_an_error(self) -> None:
        assert detect_tones(np.zeros(0), SR).pattern == "none"
        assert detect_tones(silence(0.01), SR).pattern == "none"

    def test_the_stability_threshold_is_an_order_of_magnitude_clear(self) -> None:
        """The constant is a decision, not a tuning: state what it is between."""
        assert 1.0 < MAX_FREQ_SPREAD_HZ < 18.0


class TestToneDetectionOnCallShapes:
    """The shapes of the calls the tone rules were measured on (8 kHz mono f32 excerpts).

    Each fixture is synthetic (``fixtures/gen_fixtures.py``) and reproduces one production
    call's tone region plus a couple of seconds of speech after it, which is what makes them
    regression tests rather than tone generators: the speech is where the old detector's
    false positives came from.
    """

    def test_a_quiet_warble_is_attenuated_whole(self) -> None:
        """The false negative (call 1170192's shape): 878/468 Hz every ~0.25 s, every half
        under the old floor."""
        report = detect_tones(fixture("warble_quiet"), SR)
        assert report.pattern == "warble"
        assert report.alert is True
        assert len(report.segments) == 12
        assert {round(s.hz) for s in report.segments} == {878, 468}
        assert len(report.spans) == 1
        span = report.spans[0]
        assert span.start_s == pytest.approx(1.22, abs=0.1)
        assert span.end_s == pytest.approx(4.31, abs=0.1)

    def test_a_warble_includes_its_fading_tail(self) -> None:
        """Call 1170733's shape: its last halves fade and measure 7-10 Hz of spread, past
        `MAX_FREQ_SPREAD_HZ`; the alternation carries them."""
        report = detect_tones(fixture("warble_fading_tail"), SR)
        assert report.pattern == "warble"
        assert report.alert is True
        assert len(report.spans) == 1
        span = report.spans[0]
        assert span.start_s == pytest.approx(1.05, abs=0.1)
        assert span.end_s == pytest.approx(4.14, abs=0.1)

    def test_a_held_vowel_is_speech_and_nothing_else(self) -> None:
        """The false positive (call 1170568's shape): a voiced vowel that used to be flagged
        492 Hz for 0.35 s.

        Attenuating it 24 dB punched a hole in the speech, which is what the operator heard
        as the audio being dropped.
        """
        report = detect_tones(fixture("vowel_only"), SR)
        assert report.pattern == "none"
        assert report.segments == ()
        assert report.spans == ()
        assert report.alert is False

    def test_a_warble_is_covered_end_to_end_despite_its_missing_halves(self) -> None:
        """The partial detection (call 1169412's shape): only the 878 Hz halves clear the
        level and dominance tests.

        Four of them 0.2 s apart is one frequency repeating, which is an `alert`, and the
        span has to cover the 467.9 Hz halves the detector cannot see — attenuating only
        the halves it could is exactly what this call sounded like before.
        """
        report = detect_tones(fixture("warble_hidden_halves"), SR)
        assert report.pattern in {"warble", "alert"}
        assert report.alert is True
        assert len(report.spans) == 1
        span = report.spans[0]
        assert span.start_s == pytest.approx(0.66, abs=0.1)
        assert span.end_s == pytest.approx(3.43, abs=0.1)


    def test_a_wandering_single_the_tight_stability_limit_rejected(self) -> None:
        """The second false negative (call 2394781's shape): 1.4 s of one vocoded 990 Hz
        tone, wandering about 18 Hz.

        Every frame is well over 0.55 dominant, so the old `>= 55 %` clause was never the
        problem here — the peak wandering 18 Hz (p90) was, an order of magnitude past
        `MAX_FREQ_SPREAD_HZ`, because a vocoder does not hold a frequency the way an encoder
        generates it.  A second inside ±20 Hz is what says
        tone, and it went out at full level (18 dB louder than the speech) until it did.
        """
        report = detect_tones(fixture("single_wandering"), SR)
        assert report.pattern == "single"
        assert report.alert is False
        assert len(report.segments) == 1
        assert report.segments[0].hz == pytest.approx(990, abs=15)
        assert len(report.spans) == 1
        span = report.spans[0]
        assert span.start_s == pytest.approx(0.15, abs=0.05)
        assert span.end_s == pytest.approx(1.6, abs=0.05)

    def test_a_warble_is_absorbed_across_its_one_hop_overlaps(self) -> None:
        """The truncated span (call 2403028's shape): 878/468 Hz alternating from 1.224 s to
        4.312 s.

        Twelve runs, consecutive in frame numbering — and because a run's end is the end of
        its last *frame* rather than of its last hop, each pair overlaps by exactly one
        16 ms hop.  `_absorb` tested the gap against zero and so could not pull them in, and
        the halves failing `MAX_FREQ_SPREAD_HZ` (the last four here) ended the chain early.
        In production the warble past that point went out un-attenuated, 4.6 dB *louder*
        than the speech that followed.
        """
        report = detect_tones(fixture("warble_overlapping_runs"), SR)
        assert report.pattern == "warble"
        assert report.alert is True
        assert len(report.segments) == 12
        assert {round(s.hz) for s in report.segments} == {878, 468}
        assert len(report.spans) == 1
        span = report.spans[0]
        assert span.start_s == pytest.approx(1.224, abs=0.02)
        assert span.end_s == pytest.approx(4.312, abs=0.02)


# --------------------------------------------------------------------------------------
# measurement: what the trim and the gain are decided from
# --------------------------------------------------------------------------------------


class TestMeasurement:
    def test_leading_dead_air_is_measured_and_padded(self) -> None:
        signal = np.concatenate([silence(0.8), tone(1000, 1.0), silence(0.5)])
        m = measure(signal, SR, AudioConfig())

        # 0.8 s of silence less the 150 ms of padding that is deliberately kept.
        assert m.head_trim_s == pytest.approx(0.65, abs=0.02)
        assert m.tail_trim_s == pytest.approx(0.35, abs=0.02)
        assert m.kept_s == pytest.approx(1.3, abs=0.05)

    def test_the_trimmed_mean_is_louder_than_the_whole_clip(self) -> None:
        """Which is the whole reason the gain is computed from it and not from `mean_db`."""
        signal = np.concatenate([silence(3.0), tone(1000, 1.0, amplitude=0.2)])
        m = measure(signal, SR, AudioConfig())
        assert m.trimmed_mean_db is not None
        assert m.trimmed_mean_db > m.mean_db + 3.0
        assert m.level_db == m.trimmed_mean_db

    def test_trimming_off_measures_no_trim(self) -> None:
        signal = np.concatenate([silence(0.8), tone(1000, 1.0)])
        m = measure(signal, SR, AudioConfig(trim_silence=False))
        assert (m.head_trim_s, m.tail_trim_s) == (0.0, 0.0)
        assert m.level_db == m.mean_db

    def test_an_all_silent_clip_keeps_its_whole_self(self) -> None:
        """Nothing clears the threshold, so there is no "first loud sample" to trim to."""
        m = measure(silence(0.5), SR, AudioConfig())
        assert (m.head_trim_s, m.tail_trim_s) == (0.0, 0.0)
        assert m.mean_db <= -99.0
        assert m.duration_s == pytest.approx(0.5, abs=0.01)

    def test_an_empty_decode_measures_silence(self) -> None:
        m = measure(np.zeros(0), SR, AudioConfig())
        assert m.duration_s == 0.0
        assert m.mean_db <= -99.0


class TestGainDecision:
    """Every number here carries `VOLUME_BRANCH_OFFSET_DB`: the branch aims a LUFS target
    at an RMS measurement, and the offset is the measured distance between the two scales."""

    def test_a_quiet_call_is_lifted_toward_the_target(self) -> None:
        m = Measurement(duration_s=2.0, mean_db=-42.0, peak_db=-20.0)
        assert gain_for(m, AudioConfig(target_lufs=-16.0)) == pytest.approx(
            26.0 + VOLUME_BRANCH_OFFSET_DB
        )

    def test_a_hot_call_is_brought_down(self) -> None:
        m = Measurement(duration_s=2.0, mean_db=-6.0, peak_db=-0.5)
        assert gain_for(m, AudioConfig(target_lufs=-16.0)) == pytest.approx(
            -10.0 + VOLUME_BRANCH_OFFSET_DB
        )

    def test_the_cap_bounds_a_near_silent_clip(self) -> None:
        m = Measurement(duration_s=2.0, mean_db=-80.0, peak_db=-70.0)
        assert gain_for(m, AudioConfig(max_gain_db=30.0)) == 30.0

    def test_silence_is_never_amplified(self) -> None:
        m = Measurement(duration_s=0.5, mean_db=-100.0, peak_db=-100.0)
        assert gain_for(m, AudioConfig()) == 0.0

    def test_the_gain_is_measured_from_the_trimmed_level(self) -> None:
        m = Measurement(
            duration_s=4.0, mean_db=-42.0, peak_db=-10.0, head_trim_s=2.0, trimmed_mean_db=-24.0
        )
        assert gain_for(m, AudioConfig(target_lufs=-16.0)) == pytest.approx(
            8.0 + VOLUME_BRANCH_OFFSET_DB
        )


# --------------------------------------------------------------------------------------
# the filter string
# --------------------------------------------------------------------------------------


def stages(chain: str) -> list[str]:
    """Split a filter chain on the commas that separate stages, not those inside quotes."""
    out, current, quoted = [], "", False
    for char in chain:
        if char == "'":
            quoted = not quoted
        if char == "," and not quoted:
            out.append(current)
            current = ""
            continue
        current += char
    if current:
        out.append(current)
    return out


class TestFilterChain:
    plain = Measurement(duration_s=2.0, mean_db=-30.0, peak_db=-10.0)

    def test_the_default_chain_for_a_short_untrimmed_call(self) -> None:
        chain = filter_chain(self.plain, (), AudioConfig())
        assert stages(chain) == [
            # The speech band a P25 vocoder actually carries, then the denoiser measured
            # behind it.  The high edge is 3800 and the assumed floor -35 because 3400 and
            # -30 between them took 3.35 dB out of the 2.5-3.8 kHz band, where the
            # consonants are (the 2026-09-14 band-pass measurement).
            "highpass=f=300",
            "lowpass=f=3800",
            "afftdn=nf=-35:nr=18",
            # -30 dBFS in, so the gate follows it down to -60 rather than sitting at the
            # -50 default, and the lift carries the branch offset.
            "agate=threshold=0.001:ratio=4:attack=5:release=250",
            "volume=15dB",
            "alimiter=limit=0.630957:level=disabled:attack=5:release=50",
        ]

    def test_every_chain_ends_on_the_limiter(self) -> None:
        """Whichever branch ran: the peak ceiling is the one promise the file makes."""
        stats = LoudnormStats(-24.0, -3.0, 7.0, -34.0, 0.5)
        for loudness in (None, stats):
            chain = filter_chain(self.plain, (), AudioConfig(), loudness=loudness)
            assert stages(chain)[-1].startswith("alimiter=limit=")

    def test_the_measuring_pass_has_no_limiter(self) -> None:
        """It would change the numbers the real pass is about to be handed."""
        chain = filter_chain(self.plain, (), AudioConfig(), loudness="measure")
        assert "alimiter" not in chain
        assert chain.endswith("print_format=json")

    def test_the_second_pass_carries_every_measured_value(self) -> None:
        stats = LoudnormStats(
            input_i=-24.42, input_tp=-3.1, input_lra=7.2, input_thresh=-34.5, target_offset=0.42
        )
        chain = filter_chain(self.plain, (), AudioConfig(), loudness=stats)
        for token in ("measured_I=-24.42", "measured_TP=-3.1", "measured_LRA=7.2",
                      "measured_thresh=-34.5", "offset=0.42", "linear=true"):
            assert token in chain, token

    def test_trimming_leads_the_chain_and_renumbers(self) -> None:
        m = Measurement(
            duration_s=10.0, mean_db=-30.0, peak_db=-10.0, head_trim_s=0.5, tail_trim_s=1.0
        )
        first, second = stages(filter_chain(m, (), AudioConfig()))[:2]
        assert first == "atrim=start=0.5:end=9"
        assert second == "asetpts=N/SR/TB"

    def test_trimming_off_leaves_the_measurement_unused(self) -> None:
        m = Measurement(duration_s=10.0, mean_db=-30.0, peak_db=-10.0, head_trim_s=0.5)
        assert "atrim" not in filter_chain(m, (), AudioConfig(trim_silence=False))

    def test_tone_spans_are_attenuated_in_the_post_trim_timebase(self) -> None:
        """The chain is built from the merged *spans*, so one page is one `volume` stage.

        Changed with the stability rework: a two-tone page used to arrive here as two
        segments and leave as two stages, and the 70 ms between its tones went out at full
        level.  The pair is one pattern, so it is now one span (§5.5).
        """
        m = Measurement(duration_s=10.0, mean_db=-30.0, peak_db=-10.0, head_trim_s=1.0)
        tones = detect_tones(
            np.concatenate([silence(1.15), tone(625, 0.96), silence(0.07), tone(750, 2.91)]), SR
        ).spans
        chain = filter_chain(m, tones, AudioConfig(page_tone_attenuate_db=24.0))
        volumes = [s for s in stages(chain) if s.startswith("volume=volume=")]
        assert len(volumes) == 1
        # A page, so it is the *page* depth that applies — see the by-pattern test below.
        assert "-24dB" in volumes[0]
        # 1.15 measured, less the 1.0 s the trim took off the head, less the span margin.
        assert "between(t,0.09" in volumes[0] or "between(t,0.1" in volumes[0]

    def test_a_single_tone_span_is_attenuated_like_a_pair(self) -> None:
        """A lone held tone reaches the same kind of `volume` stage a page does.

        A wandering single (call 2394781's shape) is one `single` span, and it has to be
        turned down like any other — the loudness stage that follows is the reason: it
        would otherwise set the whole clip's gain from the tone.  Only the *depth* differs, and that is
        ``test_tones_are_turned_down_by_what_they_are``'s business.
        """
        report = detect_tones(fixture("single_wandering"), SR)
        assert report.pattern == "single"
        chain = filter_chain(self.plain, report.spans, AudioConfig(tone_attenuate_db=18.0))
        volumes = [s for s in stages(chain) if s.startswith("volume=volume=")]
        assert len(volumes) == 1
        assert "-18dB" in volumes[0]
        assert "between(t," in volumes[0]
        # …and before the loudness/limiter stages, never after.
        names = stages(chain)
        assert names.index(volumes[0]) < len(names) - 1

    def test_tones_are_turned_down_by_what_they_are(self) -> None:
        """A page stays audible; the console's attention tone does not have to.

        One chain, two spans, two depths.  A two-tone page is the *message* — somebody was
        toned out, and a listener wants to hear it under the dispatch that follows — where a
        `single` is the held attention tone in front of the dispatch (call 2396065: 1.95 s of
        993 Hz at -10.5 dBFS before 17 s of speech), which carries nothing and is only loud.
        """
        m = Measurement(duration_s=30.0, mean_db=-30.0, peak_db=-10.0)
        signal = np.concatenate(
            [
                tone(993, 1.95),  # the console tone: one `single` span
                silence(0.6),
                tone(625, 1.0),  # …and a two-tone page after it
                silence(0.05),
                tone(750, 1.0),
            ]
        )
        report = detect_tones(signal, SR)
        assert [span.pattern for span in report.spans] == ["single", "two_tone"]

        chain = filter_chain(
            m, report.spans, AudioConfig(tone_attenuate_db=30.0, page_tone_attenuate_db=18.0)
        )
        volumes = [s for s in stages(chain) if s.startswith("volume=volume=")]
        assert len(volumes) == 2
        assert "-30dB" in volumes[0], volumes[0]
        assert "-18dB" in volumes[1], volumes[1]

    def test_the_span_a_pattern_produced_says_which_pattern_it_was(self) -> None:
        """The depth is chosen per span, so the span has to carry the name."""
        page = detect_tones(
            np.concatenate([tone(625, 1.0), silence(0.05), tone(750, 1.0)]), SR
        )
        assert page.pattern == "two_tone"
        assert [span.pattern for span in page.spans] == ["two_tone"]
        assert page.spans[0].is_page is True
        # …and it reaches ``audio_meta.tone_spans`` with it.
        assert page.spans_as_dicts()[0]["pattern"] == "two_tone"
        # The per-run detail is not a pattern and does not claim to be one.
        assert all("pattern" not in seg for seg in page.as_dicts())

    def test_tone_removal_cuts_and_renumbers(self) -> None:
        tones = detect_tones(np.concatenate([tone(625, 0.5), silence(0.05), tone(750, 0.5)]), SR).spans
        chain = filter_chain(self.plain, tones, AudioConfig(tone_handling="remove"))
        assert "aselect='not(" in chain
        assert chain.count("between(t,") == 1
        assert "asetpts=N/SR/TB" in stages(chain)

    def test_tone_removal_without_aselect_trims_and_concats(self) -> None:
        """The cut for an ffmpeg whose ``aselect`` drops nothing (8.0.x): same spans, no aselect."""
        tones = detect_tones(np.concatenate([tone(625, 0.5), silence(0.05), tone(750, 0.5)]), SR).spans
        chain = filter_chain(self.plain, tones, AudioConfig(tone_handling="remove"), select_works=False)
        assert "aselect" not in chain
        assert "atrim=start=" in chain
        assert "asetpts=N/SR/TB" in stages(chain)

    def test_the_trim_cut_keeps_the_pieces_between_the_spans(self) -> None:
        from app.audioproc.chain import _trim_cut

        assert _trim_cut([(0.0, 1.5)]) == "atrim=start=1.5"
        assert _trim_cut([(2.0, 3.0), (0.5, 1.0), (2.5, 3.25)]) == (
            "asplit=3[tcut0][tcut1][tcut2]"
            ";[tcut0]atrim=end=0.5[tkeep0]"
            ";[tcut1]atrim=start=1:end=2[tkeep1]"
            ";[tcut2]atrim=start=3.25[tkeep2]"
            ";[tkeep0][tkeep1][tkeep2]concat=n=3:v=0:a=1"
        )

    def test_keeping_tones_puts_nothing_in_the_chain(self) -> None:
        tones = detect_tones(tone(1000, 1.0), SR).spans
        chain = filter_chain(self.plain, tones, AudioConfig(tone_handling="keep"))
        assert "volume=volume=" not in chain
        assert "aselect" not in chain

    def test_the_band_pass_sits_between_the_tones_and_the_denoiser(self) -> None:
        """Order is the point: cut the out-of-band energy before anything measures it.

        After the trim and the tone envelope (which are expressed in a timebase the band
        edges must not disturb) and before the denoiser, so ``afftdn`` spends its budget on
        the noise inside the speech band rather than the hiss above it.
        """
        tones = detect_tones(tone(1000, 1.0), SR).spans
        chain = filter_chain(self.plain, tones, AudioConfig())
        rendered = stages(chain)
        assert rendered[rendered.index("highpass=f=300") - 1].startswith("volume=volume=")
        assert rendered[rendered.index("highpass=f=300") : rendered.index("highpass=f=300") + 3] == [
            "highpass=f=300",
            "lowpass=f=3800",
            "afftdn=nf=-35:nr=18",
        ]

    @pytest.mark.parametrize(
        ("low", "high", "expected"),
        [
            (0.0, 3800.0, ["lowpass=f=3800"]),
            (300.0, 0.0, ["highpass=f=300"]),
            (0.0, 0.0, []),
        ],
    )
    def test_a_zero_edge_drops_that_half_of_the_band_pass(
        self, low: float, high: float, expected: list[str]
    ) -> None:
        chain = filter_chain(
            self.plain, (), AudioConfig(bandpass_low_hz=low, bandpass_high_hz=high)
        )
        assert [s for s in stages(chain) if s.split("=")[0] in {"highpass", "lowpass"}] == expected

    def test_a_profile_replaces_the_global_band_pass_too(self) -> None:
        """A profile is the whole noise section, band edges included — it declares its own."""
        chain = filter_chain(self.plain, (), AudioConfig(), profile=["afftdn=nf=-28"])
        assert "highpass" not in chain
        assert "lowpass" not in chain
        assert "afftdn=nf=-28" in chain

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("off", None),
            ("afftdn", "afftdn=nf=-35:nr=18"),
            ("anlmdn", "anlmdn"),
        ],
    )
    def test_each_denoiser_choice(self, kind: str, expected: str | None) -> None:
        chain = filter_chain(self.plain, (), AudioConfig(noise_reduction=kind))
        present = [s for s in stages(chain) if s.split("=")[0] in {"afftdn", "anlmdn", "arnndn"}]
        assert present == ([] if expected is None else [expected])

    def test_a_zero_reduction_leaves_nr_off_the_command_line(self) -> None:
        """Which is not "no reduction": with no ``nr`` ffmpeg uses its own default of 12."""
        chain = filter_chain(self.plain, (), AudioConfig(noise_reduction_nr=0.0))
        assert "afftdn=nf=-35" in stages(chain)

    def test_arnndn_without_a_model_falls_back_to_afftdn(self) -> None:
        chain = filter_chain(self.plain, (), AudioConfig(noise_reduction="arnndn"))
        assert "arnndn" not in chain
        assert "afftdn=nf=-35:nr=18" in chain

    def test_arnndn_with_a_model_uses_it(self) -> None:
        chain = filter_chain(
            self.plain, (), AudioConfig(noise_reduction="arnndn", rnnoise_model="/models/bd.rnnn")
        )
        assert "arnndn=m='/models/bd.rnnn'" in chain

    def test_the_gate_can_be_switched_off(self) -> None:
        assert "agate" not in filter_chain(self.plain, (), AudioConfig(gate_db=-100.0))

    def test_the_gate_threshold_is_linear(self) -> None:
        """A -10 dBFS clip asks for -40, which is also the ceiling, so the number is -40."""
        loud = Measurement(duration_s=2.0, mean_db=-10.0, peak_db=-1.0)
        chain = filter_chain(loud, (), AudioConfig(gate_db=-40.0))
        gate = next(s for s in stages(chain) if s.startswith("agate"))
        assert gate.startswith("agate=threshold=0.01:")

    def test_a_zero_gain_stage_is_omitted(self) -> None:
        m = Measurement(duration_s=2.0, mean_db=-16.0, peak_db=-3.0)
        target = -16.0 - VOLUME_BRANCH_OFFSET_DB
        assert "volume=" not in filter_chain(m, (), AudioConfig(target_lufs=target))

    def test_an_unknown_setting_value_falls_back_rather_than_failing(self) -> None:
        config = AudioConfig.from_settings(
            type("S", (), {"audio_noise_reduction": "wat", "audio_tone_handling": "nope"})()
        )
        assert config.noise_reduction == "afftdn"
        assert config.tone_handling == "attenuate"


class TestLoudnormBranch:
    """Which loudness stage a clip gets is decided by its *post-trim* length."""

    @staticmethod
    def kept(seconds: float) -> Measurement:
        return Measurement(duration_s=seconds + 1.0, mean_db=-30.0, peak_db=-10.0, tail_trim_s=1.0)

    def test_a_median_length_call_is_below_the_loudnorm_floor(self) -> None:
        assert self.kept(2.2).kept_s < LOUDNORM_MIN_S

    def test_a_long_call_clears_it(self) -> None:
        assert self.kept(25.9).kept_s >= LOUDNORM_MIN_S

    def test_trimming_can_push_a_clip_under_the_floor(self) -> None:
        """A four-second call that is three seconds of dead air is a one-second call."""
        m = Measurement(duration_s=4.0, mean_db=-30.0, peak_db=-10.0, head_trim_s=3.0)
        assert m.kept_s < LOUDNORM_MIN_S

    def test_an_unmeasurable_first_pass_is_refused(self) -> None:
        assert LoudnormStats.from_json({"input_i": "-inf", "input_tp": "-120",
                                        "input_lra": "0", "input_thresh": "-inf",
                                        "target_offset": "0"}) is None
        assert LoudnormStats.from_json({}) is None

    def test_a_good_first_pass_is_parsed(self) -> None:
        stats = LoudnormStats.from_json(
            {"input_i": "-24.42", "input_tp": "-3.10", "input_lra": "7.20",
             "input_thresh": "-34.50", "target_offset": "0.42"}
        )
        assert stats is not None
        assert stats.input_i == pytest.approx(-24.42)


# --------------------------------------------------------------------------------------
# the real thing, against ffmpeg and a fixture recording
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestProcessFile:
    @staticmethod
    def a_fixture() -> Path:
        files = sorted(FIXTURES.rglob("*.mp3"))
        if not files:
            pytest.skip("no audio fixtures on this checkout")
        return files[0]

    def test_the_processed_copy_sits_beside_the_original(self) -> None:
        assert processed_path_for(Path("/a/b/c.mp3")) == Path("/a/b/c" + PROCESSED_SUFFIX)
        assert processed_path_for(Path("/a/b/c.wav")).name == "c" + PROCESSED_SUFFIX

    def test_a_fixture_recording_is_levelled_toward_the_target(self, tmp_path: Path) -> None:
        source = self.a_fixture()
        result = process_file(source, AudioConfig(), dst=tmp_path / "out.mp3")

        assert result.wrote is True
        assert (tmp_path / "out.mp3").stat().st_size > 0
        assert source.stat().st_size == source.stat().st_size  # the original is untouched
        meta = result.meta
        # Whatever it came in at, it leaves within a few dB of the -16 target.
        assert meta["mean_db_after"] == pytest.approx(-16.0, abs=6.0)
        # And it never clips: the limiter is the one promise the file makes.
        assert meta["peak_db_after"] <= 0.0
        assert meta["version"] == PIPELINE_VERSION
        assert meta["silent"] is False
        assert set(meta) >= {"filter", "gain_db", "head_trim_s", "tail_trim_s", "tones",
                             "tone_spans", "noise_reduced", "tone_removed", "silent", "version"}

    def test_a_synthesized_page_is_detected_and_turned_down(self, tmp_path: Path) -> None:
        """End to end: a two-tone page in, `alert` out and the tones 24 dB quieter."""
        signal = np.concatenate(
            [silence(0.6), tone(625, 1.0), silence(0.05), tone(750, 2.9), speechish(4.0)]
        )
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        source = tmp_path / "page.mp3"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(source)],
            input=raw, check=True, capture_output=True,
        )

        result = process_file(source, AudioConfig(), dst=tmp_path / "page.proc.mp3")

        assert result.tone_detected is True
        assert result.alert is True
        assert result.meta["tone_pattern"] == "two_tone"
        assert len(result.meta["tones"]) == 2
        # …and one merged span over the pair, which is what was actually turned down.
        assert len(result.meta["tone_spans"]) == 1
        assert result.meta["tone_removed"] is False

        # …and the span really is turned down, measured against the same call processed
        # with `keep`.
        #
        # Against the *speech* is the wrong comparison and used to be the one made here.
        # The envelope goes on before `loudnorm` (§5.5 stage 2, and for a good reason: a
        # page tone is the loudest thing in the archive and normalizing first would set the
        # whole clip's gain from it), and on a clip that is half tone `loudnorm` runs
        # dynamic and hands most of the attenuation straight back — which is exactly why
        # both the export path and the player re-apply the envelope *after* their own lift
        # (:func:`app.exporters.loudness.tone_envelope_for`).  The old assertion held by
        # 0.85 dB on this fixture, so it was really measuring how hard `loudnorm` happened
        # to be working, and it moved the day the true-peak ceiling did.  This measures the
        # attenuation itself, which is what the pass actually controls: about 9 dB here.
        def rms_db(block: np.ndarray) -> float:
            return 20 * math.log10(max(float(np.sqrt(np.mean(block**2))), 1e-12))

        def tone_against_speech(path: Path) -> float:
            after = decode_pcm(path)
            head = after[int(0.6 * SR) : int(1.4 * SR)]
            speech = after[int(5.2 * SR) :]
            assert head.size and speech.size
            return rms_db(head) - rms_db(speech)

        process_file(source, AudioConfig(tone_handling="keep"), dst=tmp_path / "kept.mp3")
        assert tone_against_speech(tmp_path / "page.proc.mp3") < (
            tone_against_speech(tmp_path / "kept.mp3") - 6.0
        )

    def test_an_empty_call_is_stored_and_marked_silent(self, tmp_path: Path) -> None:
        source = tmp_path / "quiet.mp3"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=8000:cl=mono", "-t", "0.5",
             "-c:a", "libmp3lame", "-b:a", "16k", str(source)],
            check=True, capture_output=True,
        )
        result = process_file(source, AudioConfig(), dst=tmp_path / "quiet.proc.mp3")
        assert result.meta["silent"] is True
        assert result.meta["gain_db"] == 0.0
        assert (tmp_path / "quiet.proc.mp3").is_file()

    def test_removing_tones_shortens_the_file(self, tmp_path: Path) -> None:
        signal = np.concatenate([tone(625, 1.0), silence(0.05), tone(750, 1.0), speechish(2.0)])
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        source = tmp_path / "cut.mp3"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(source)],
            input=raw, check=True, capture_output=True,
        )
        result = process_file(
            source, AudioConfig(tone_handling="remove"), dst=tmp_path / "cut.proc.mp3"
        )
        assert result.meta["tone_removed"] is True
        assert decode_pcm(tmp_path / "cut.proc.mp3").size < decode_pcm(source).size

    @pytest.mark.parametrize("select_works", [True, False])
    def test_removing_tones_cuts_them_on_either_cut(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, select_works: bool
    ) -> None:
        """Both cuts take out the ~2.1 s page, whichever ffmpeg runs them.

        ``True`` forces ``aselect`` — on an ffmpeg whose aselect drops nothing (8.0.x) that
        path cannot cut, so it is skipped there; ``False`` forces the ``atrim``/``concat``
        fallback, which must work on every ffmpeg, the production image's included.
        """
        import app.audioproc.process as proc

        if select_works and not proc.aselect_works():
            pytest.skip("this ffmpeg's aselect drops nothing (ffmpeg 8.0.x regression)")
        monkeypatch.setattr(proc, "aselect_works", lambda: select_works)
        signal = np.concatenate([tone(625, 1.0), silence(0.05), tone(750, 1.0), speechish(2.0)])
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        source = tmp_path / "cut.mp3"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(source)],
            input=raw, check=True, capture_output=True,
        )  # fmt: skip
        result = process_file(
            source, AudioConfig(tone_handling="remove"), dst=tmp_path / "cut.proc.mp3"
        )
        assert result.meta["tone_removed"] is True
        assert ("aselect" in result.meta["filter"]) is select_works
        # 4.05 s in, the page is 2.1 of it: what is left is the speech, give or take a frame.
        seconds = decode_pcm(tmp_path / "cut.proc.mp3").size / SR
        assert 1.7 < seconds < 2.3, seconds


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestNothingClipsAfterTheCodec:
    """The ceiling has to survive the encode, which is the part that used to be missed.

    Production, one afternoon: 804 of about 930 processed calls stored
    ``peak_db_after = 0.0``.  Call 2402902 came in peaking at -8.4 dBFS and its processed
    MP3 decoded at +0.01 dBFS with 153 samples over the limiter's ceiling.  Two causes,
    and either one alone is enough:

    1. ``alimiter`` defaults to ``level=1`` — "auto level" — which *raises* the output until
       the loudest sample sits exactly on ``limit``.  Every file left the pass pinned to the
       ceiling no matter how quiet it arrived, which is what put the peak at the ceiling
       *by construction* rather than as a cap.
    2. The ceiling is enforced on PCM, and the archive is 16 kbps MP3 at 8 kHz.  A lossy
       decoder does not reproduce the samples it was given; it reconstructs a waveform that
       overshoots them, by 1.5-2 dB on this material.  A chain that stops at -1.5 dBFS
       decodes over full scale, and the decoder clips.

    So these tests measure the thing the listener actually gets: encode, *decode*, and look
    at the peak.  Nothing else in this file does that — the chain-string tests cannot, and
    the ffmpeg tests above assert on what the pass reported about itself.
    """

    @staticmethod
    def hot_source(path: Path, *, bitrate: str = "16k") -> Path:
        """A call that peaks near full scale — a close-in unit, or a page.

        The bug does not show on a quiet clip: it needs something the limiter engages on,
        which is exactly the traffic that was clipping.
        """
        signal = np.concatenate(
            [silence(0.6), tone(625, 1.0, amplitude=0.8), silence(0.05),
             tone(750, 2.9, amplitude=0.8), speechish(4.0) * 1.6]
        )  # fmt: skip
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", bitrate, str(path)],
            input=raw, check=True, capture_output=True,
        )
        return path

    @staticmethod
    def decoded_peak_db(path: Path) -> float:
        samples = decode_pcm(path)
        return 20 * math.log10(max(float(np.max(np.abs(samples))), 1e-12))

    def test_the_stored_chain_survives_its_own_mp3_encode(self, tmp_path: Path) -> None:
        """-4 dBFS of PCM ceiling is what -1 dBFS of *decoded* peak costs at 16 kbps."""
        source = self.hot_source(tmp_path / "hot.mp3")
        assert self.decoded_peak_db(source) > -3.0, "the fixture has to be hot to prove anything"

        result = process_file(source, AudioConfig(), dst=tmp_path / "hot.proc.mp3")

        # The chain says what it does, and both halves of the fix are in it.
        chain = result.meta["filter"]
        assert "alimiter=limit=0.630957:level=disabled:attack=5:release=50" in chain
        assert "TP=-2" in chain
        # …and the file it produced decodes clear of full scale rather than on it.
        peak = self.decoded_peak_db(tmp_path / "hot.proc.mp3")
        assert peak <= -1.0, f"decoded peak {peak:.2f} dBFS is not clear of full scale"

        # The overshoot is real and is the whole reason the ceiling moved: the limiter was
        # told -4 dBFS and what comes back off the decoder is a couple of decibels above it.
        assert peak > -4.0

        # ``peak_db_after`` is measured by decoding the file that was just written, so it is
        # this same number — which is why 804 calls reported exactly 0.0 rather than the
        # -1.5 the chain claimed.  It is the production symptom, so it is asserted directly.
        assert result.meta["peak_db_after"] == pytest.approx(peak, abs=0.05)
        assert result.meta["peak_db_after"] <= -1.0

    def test_the_old_ceiling_is_what_clipped(self, tmp_path: Path) -> None:
        """The same clip through the shipped-before chain, to show the test can fail.

        A regression test for a ceiling is worthless if it would pass at any ceiling, so
        this pins the counterfactual: -1.5 dBFS with auto-level on is what production ran,
        and it decodes at or above full scale.
        """
        source = self.hot_source(tmp_path / "hot.mp3")
        result = process_file(
            source, AudioConfig(true_peak_db=-1.5), dst=tmp_path / "old.proc.mp3"
        )
        old_chain = result.meta["filter"].replace(
            "alimiter=limit=0.841395:level=disabled", "alimiter=limit=0.841395:level=1"
        )
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-af", old_chain,
             "-ac", "1", "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", "16k",
             str(tmp_path / "as-shipped.mp3")],
            check=True, capture_output=True,
        )
        as_shipped = self.decoded_peak_db(tmp_path / "as-shipped.mp3")
        assert as_shipped >= -0.5, f"{as_shipped:.2f} dBFS — this fixture no longer clips"
        # …and the fix is worth at least a decibel and a half of headroom on it.
        process_file(source, AudioConfig(), dst=tmp_path / "fixed.proc.mp3")
        assert self.decoded_peak_db(tmp_path / "fixed.proc.mp3") <= as_shipped - 1.5

    def test_the_export_chain_survives_its_own_encode(self, tmp_path: Path) -> None:
        """Same measurement on the outbound copy, at the export's own -3 dB ceiling."""
        from app.config import Settings
        from app.exporters.loudness import export_filter

        source = self.hot_source(tmp_path / "hot.mp3", bitrate="32k")
        process_file(source, AudioConfig(), dst=tmp_path / "hot.proc.mp3")

        chain = export_filter(None, Settings(_env_file=None, export_target_lufs=-11.0))
        assert chain is not None
        assert "alimiter=limit=0.707946:level=disabled:attack=5:release=50" in chain
        assert "TP=-3.0" in chain

        # The Feed's own encode: MP3, and the higher rate an outbound service gets.
        out = tmp_path / "export.mp3"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(tmp_path / "hot.proc.mp3"),
             "-af", chain, "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "32k",
             str(out)],
            check=True, capture_output=True,
        )
        peak = self.decoded_peak_db(out)
        assert peak <= -1.0, f"decoded export peak {peak:.2f} dBFS is not clear of full scale"


class TestTheGateFollowsTheClip:
    """`gate_threshold_db`: a threshold read off this clip rather than off the archive.

    Eighteen of the eighteen calls more than 6 dB below the archive's median level are on
    the `volume` branch, and production 2405543 — asked for +30 dB — came out +16.1.  A
    threshold fixed at -50 dBFS and applied *before* the gain sits at or above the content
    of a call that arrived at -46, so the gate ducks the speech instead of the hiss.
    """

    def test_a_normally_levelled_call_gets_exactly_the_configured_number(self) -> None:
        """The change has to be invisible on the calls the chain already gets right."""
        normal = Measurement(duration_s=2.0, mean_db=-20.0, peak_db=-6.0)
        assert gate_threshold_db(normal, AudioConfig(gate_db=-50.0)) == pytest.approx(-50.0)

    def test_a_quiet_call_takes_the_gate_down_with_it(self) -> None:
        quiet = Measurement(duration_s=2.0, mean_db=-46.28, peak_db=-23.62)
        # -46.28 - 30 is -76.28, held at the floor rather than chasing it down.
        assert gate_threshold_db(quiet, AudioConfig()) == pytest.approx(GATE_FLOOR_DB)

    def test_it_never_rises_above_the_setting(self) -> None:
        """`audio_gate_db` keeps its meaning: the operator's ceiling, not a suggestion."""
        loud = Measurement(duration_s=2.0, mean_db=-3.0, peak_db=-0.5)
        assert gate_threshold_db(loud, AudioConfig(gate_db=-55.0)) == pytest.approx(-55.0)

    def test_the_trimmed_level_is_what_it_reads(self) -> None:
        """The same number the gain is computed from — dead air must not lower the gate."""
        m = Measurement(
            duration_s=4.0, mean_db=-60.0, peak_db=-10.0, head_trim_s=2.0, trimmed_mean_db=-20.0
        )
        assert gate_threshold_db(m, AudioConfig()) == pytest.approx(-50.0)

    def test_a_clip_with_no_level_at_all_keeps_the_setting(self) -> None:
        empty = Measurement(duration_s=0.5, mean_db=-100.0, peak_db=-100.0)
        assert gate_threshold_db(empty, AudioConfig(gate_db=-50.0)) == pytest.approx(-50.0)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestTheGateDoesNotEatQuietCalls:
    """The same decision, measured through ffmpeg rather than asserted about the string."""

    @staticmethod
    def encode(signal: np.ndarray, path: Path) -> Path:
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
            input=raw, check=True, capture_output=True,
        )
        return path

    def test_a_60_db_clip_on_the_volume_branch_keeps_its_content(self, tmp_path: Path) -> None:
        """-60 dBFS speech, two seconds — the short branch, and the shape of 2405543.

        With the gate fixed at -50 dBFS ahead of the gain this clip is 10 dB under the
        threshold for its whole length: measured, the old order came out **6.8 dB quieter
        than it went in** having been asked for +30.  The clip has to survive the pass with
        its speech still in it.
        """
        clip = at_level(voiced_speech()[: int(2.0 * SR)], -60.0)
        source = self.encode(clip, tmp_path / "faint.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "faint.proc.mp3")

        assert result.meta["gain_mode"] == "volume"
        # It came out louder than it went in, which under the old order it did not.
        assert result.meta["mean_db_after"] > result.meta["mean_db"] + 10.0
        assert result.meta["content"] == "speech"
        assert result.meta["speech_s"] > 0.25

    def test_the_old_fixed_gate_is_what_took_it_away(self, tmp_path: Path) -> None:
        """The counter-measurement: the same clip through a gate pinned at the setting."""
        clip = at_level(voiced_speech()[: int(2.0 * SR)], -55.0)
        source = self.encode(clip, tmp_path / "faint2.mp3")
        config = AudioConfig()

        m = measure(decode_pcm(source), SR, config)
        # Everything ahead of the gate is the chain's own — the band-pass and denoiser are
        # held constant so what this measures is the gate and nothing else.
        head = ",".join([*_bandpass_stages(config), *_noise_stage(config)])
        old = (
            f"{head},agate=threshold={10.0 ** (config.gate_db / 20.0):.6f}"
            f":ratio=4:attack=5:release=250,volume={gain_for(m, config):.2f}dB"
        )
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-af", old,
             "-ac", "1", "-ar", str(SR), "-c:a", "libmp3lame", "-b:a", "32k",
             str(tmp_path / "old.mp3")],
            check=True, capture_output=True,
        )
        before = decode_pcm(source)
        after = decode_pcm(tmp_path / "old.mp3")
        old_gain = 20.0 * np.log10(
            np.sqrt(np.mean(after**2)) / np.sqrt(np.mean(before**2))
        )
        new_gain = process_file(
            source, config, dst=tmp_path / "faint2.proc.mp3"
        ).meta["gain_db"]

        # It asked for the cap either way; only one of the two delivered anything.  Measured:
        # +0.33 dB through the fixed gate against +17.50 dB through the relative one.
        assert gain_for(m, config) == pytest.approx(config.max_gain_db)
        assert old_gain < 2.0
        assert new_gain > old_gain + 15.0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestTheTwoGainBranchesLandTogether:
    """`VOLUME_BRANCH_OFFSET_DB`: no step for a listener at `LOUDNORM_MIN_S`."""

    @staticmethod
    def encode(signal: np.ndarray, path: Path) -> Path:
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
            input=raw, check=True, capture_output=True,
        )
        return path

    def test_two_and_four_seconds_of_the_same_speech_land_together(self, tmp_path: Path) -> None:
        """The same recording, cut either side of the three-second boundary.

        One goes down the `loudnorm` branch and one down the `volume` branch, and a
        listener scrubbing a day of traffic hears them one after another: they have to be
        the same loudness, because nothing about the audio changed but where it was cut.
        """
        speech = voiced_speech()
        long_result = process_file(
            self.encode(speech, tmp_path / "long.mp3"),
            AudioConfig(),
            dst=tmp_path / "long.proc.mp3",
        )
        short_result = process_file(
            self.encode(speech[: int(2.0 * SR)], tmp_path / "short.mp3"),
            AudioConfig(),
            dst=tmp_path / "short.proc.mp3",
        )

        assert long_result.meta["gain_mode"] == "loudnorm"
        assert short_result.meta["gain_mode"] == "volume"
        assert short_result.meta["mean_db_after"] == pytest.approx(
            long_result.meta["mean_db_after"], abs=1.0
        )

    def test_the_offset_is_what_the_short_branch_carries(self) -> None:
        """Stated as an identity so the constant cannot be edited away by accident."""
        m = Measurement(duration_s=2.0, mean_db=-30.0, peak_db=-10.0)
        config = AudioConfig(target_lufs=-16.0)
        assert gain_for(m, config) == pytest.approx(14.0 + VOLUME_BRANCH_OFFSET_DB)


class TestStaticFlatness:
    """`flat_fraction`: what separates broadband noise from a voice.

    Spectral flatness is the geometric mean of a frame's power spectrum over its arithmetic
    mean — 1.0 for white noise, near zero for a vowel, which puts its energy in formants
    and leaves nulls between them.  Production 2403008, 2402904 and 2402672 measure
    0.81-0.93 of their live frames flat; the twelve quietest real calls in the archive
    measure 0.02-0.20.
    """

    def test_white_noise_is_as_flat_as_it_gets(self) -> None:
        noise = at_level(np.random.RandomState(7).randn(int(1.5 * SR)), -40.0)
        assert flat_fraction(noise) >= 0.9

    def test_a_vowel_is_not_flat_at_all(self) -> None:
        assert flat_fraction(formant_vowel(1.5)) <= 0.1

    def test_voiced_speech_is_not_flat(self) -> None:
        assert flat_fraction(voiced_speech()) <= 0.3

    def test_silence_has_no_live_frames_and_so_is_not_static(self) -> None:
        """A call with nothing in it is `none`; it must not become `static` for want of
        evidence, which is what counting frames nobody spoke in would do."""
        assert flat_fraction(silence(1.0)) == 0.0

    def test_a_clip_shorter_than_one_frame_measures_nothing(self) -> None:
        assert flat_fraction(np.zeros(64)) == 0.0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestStaticContent:
    """`content = static` end to end (§5.5).

    Production 2403008 / 2402904 / 2402672 were all stored `speech` with 0.4-1.3 s of
    "speech" in them, because `speech_seconds` counts loud seconds on the **processed**
    copy and the gain stage had just lifted broadband noise 24-30 dB clear of the floor.
    The measure that sees it has to run on the recorder's own file, and has to be paired
    with a level test: the flatness margin alone is about 0.05, and the level term is what
    keeps loud speech out.
    """

    @staticmethod
    def encode(signal: np.ndarray, path: Path) -> Path:
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
            input=raw, check=True, capture_output=True,
        )
        return path

    def test_white_noise_at_40_db_is_static(self, tmp_path: Path) -> None:
        noise = at_level(np.random.RandomState(7).randn(int(1.5 * SR)), -40.0)
        source = self.encode(noise, tmp_path / "static.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "static.proc.mp3")

        assert result.meta["content"] == "static"
        assert result.meta["flat_frac"] >= 0.70
        # …and the measure the old classifier had says the opposite, which is the point:
        # lifted 15 dB, this noise is above the speech floor for its whole length.
        assert result.meta["speech_s"] > 0.25
        assert result.meta["silent"] is False

    def test_a_formant_signal_is_speech(self, tmp_path: Path) -> None:
        source = self.encode(formant_vowel(1.5), tmp_path / "vowel.mp3")
        result = process_file(source, AudioConfig(), dst=tmp_path / "vowel.proc.mp3")
        assert result.meta["content"] == "speech"
        assert result.meta["flat_frac"] < 0.70

    def test_silence_is_none_and_not_static(self, tmp_path: Path) -> None:
        source = self.encode(silence(1.0), tmp_path / "quiet3.mp3")
        result = process_file(source, AudioConfig(), dst=tmp_path / "quiet3.proc.mp3")
        assert result.meta["content"] == "none"
        assert result.meta["flat_frac"] == 0.0

    def test_the_level_test_is_what_carries_the_rule(self, tmp_path: Path) -> None:
        """Loud noise-shaped speech is speech.

        `speechish` — broadband noise cut into syllables — measures 1.00 on the flatness
        axis, exactly like static.  What keeps it out is that it is loud: production
        2405803/2405804 are the same transmission recorded twice, 0.74 flat and -19 dBFS,
        and they are excluded on level alone.
        """
        source = self.encode(speechish(2.0), tmp_path / "loudnoise.mp3")
        result = process_file(source, AudioConfig(), dst=tmp_path / "loudnoise.proc.mp3")
        assert result.meta["flat_frac"] >= 0.70
        assert result.meta["mean_db"] > STATIC_MAX_LEVEL_DB
        assert result.meta["content"] == "speech"

    def test_both_thresholds_are_settings(self, tmp_path: Path) -> None:
        """A deployment that disagrees with either number can move it."""
        noise = at_level(np.random.RandomState(7).randn(int(1.5 * SR)), -40.0)
        source = self.encode(noise, tmp_path / "static2.mp3")
        result = process_file(
            source,
            AudioConfig(static_frame_share=1.01),
            dst=tmp_path / "static2.proc.mp3",
        )
        assert result.meta["content"] != "static"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestSpeechContent:
    """Did anybody actually say anything? (``audio_meta.content``)

    Production calls 2395971 and 2395972 are one 0.648 s transmission on TRANSIT: 0.35 s at
    -80 dBFS and then a single click at -22 dBFS, peaking at -9.  A mic keyed and released.
    The whole-clip mean is about -50 dBFS, which sails past the -60 ``silent`` floor, so the
    call was stored as speech, transcribed (to nothing) and queued for export — twice, once
    per recorder.  ``silent`` asks how loud the clip is; this asks how much of it is loud.
    """

    @staticmethod
    def encode(signal: np.ndarray, path: Path) -> Path:
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(path)],
            input=raw, check=True, capture_output=True,
        )
        return path

    def test_a_key_up_with_nothing_behind_it_holds_no_speech(self, tmp_path: Path) -> None:
        """The shape of the production pair: dead air and one 30 ms click."""
        clip = np.concatenate([silence(0.35), 0.08 * np.ones(int(0.03 * SR)), silence(0.05)])
        source = self.encode(clip, tmp_path / "kerchunk.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "kerchunk.proc.mp3")

        assert result.meta["content"] == "none"
        assert result.meta["speech_s"] < 0.25
        assert result.meta["speech_floor_db"] == -40.0
        # …and the reason the old floor could not see it: the clip is not "silent" at all.
        assert result.meta["silent"] is False

    def test_a_call_somebody_talked_on_holds_speech(self, tmp_path: Path) -> None:
        source = self.encode(np.concatenate([silence(0.2), speechish(2.0)]), tmp_path / "talk.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "talk.proc.mp3")

        assert result.meta["content"] == "speech"
        assert result.meta["speech_s"] >= 0.25

    def test_the_minimum_is_a_setting_and_it_is_obeyed(self, tmp_path: Path) -> None:
        """A deployment that wants every click kept can have one."""
        clip = np.concatenate([silence(0.35), 0.08 * np.ones(int(0.03 * SR)), silence(0.05)])
        source = self.encode(clip, tmp_path / "kerchunk2.mp3")
        result = process_file(
            source, AudioConfig(min_speech_seconds=0.0), dst=tmp_path / "kerchunk2.proc.mp3"
        )
        assert result.meta["content"] == "speech"

    def test_an_all_silent_clip_has_no_speech_either(self, tmp_path: Path) -> None:
        source = self.encode(silence(0.5), tmp_path / "quiet2.mp3")
        result = process_file(source, AudioConfig(), dst=tmp_path / "quiet2.proc.mp3")
        assert result.meta["silent"] is True
        assert result.meta["content"] == "none"

    def test_a_call_that_is_nothing_but_a_tone_holds_no_speech(self, tmp_path: Path) -> None:
        """Production 2396243: 2.0 s of 993 Hz and nothing else.

        A tone clears the -40 dB floor by a mile, so counting loud seconds alone would call
        two of them speech — and then the call is transcribed (to a hallucination), exported,
        and kept.  The tone spans the pass already found are excluded from the count.
        """
        signal = np.concatenate([silence(0.2), tone(993, 2.0, amplitude=0.15), silence(0.3)])
        source = self.encode(signal, tmp_path / "tone-only.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "tone-only.proc.mp3")

        assert result.meta["tone_pattern"] == "single"
        assert result.meta["content"] == "none"
        assert result.meta["speech_s"] < 0.25
        # …and the tone really was found and turned down, which is what made it invisible.
        assert len(result.meta["tone_spans"]) == 1

    def test_a_page_is_content_even_with_nothing_said_after_it(self, tmp_path: Path) -> None:
        """Production 2396355/2396357: 4.25 s of fire dispatch page and not one word.

        An A tone of about a second then a B tone of about 2.75, and that is the whole call —
        so by seconds-of-speech alone it is indistinguishable from a mic click, and the
        discard pass would have taken the most important audio on the system.  A pager
        pattern is content by itself.
        """
        signal = np.concatenate([tone(625, 1.0), silence(0.05), tone(750, 2.75), silence(0.4)])
        source = self.encode(signal, tmp_path / "page-only.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "page-only.proc.mp3")

        assert result.meta["tone_pattern"] == "two_tone"
        assert result.meta["content"] == "page"
        # …and it really did measure as no speech; the pattern is what saved it.
        assert result.meta["speech_s"] < 0.25

    def test_a_lone_console_tone_is_not_a_page_and_saves_nothing(self, tmp_path: Path) -> None:
        """The distinction the rule turns on: `single` is the console, not a tone-out."""
        signal = np.concatenate([silence(0.2), tone(993, 2.0, amplitude=0.15), silence(0.3)])
        result = process_file(
            self.encode(signal, tmp_path / "console.mp3"),
            AudioConfig(),
            dst=tmp_path / "console.proc.mp3",
        )
        assert result.meta["tone_pattern"] == "single"
        assert result.meta["content"] == "none"

    def test_a_page_followed_by_speech_is_still_a_page(self, tmp_path: Path) -> None:
        """The dispatch after the tones is there — and the page is what the call *is*."""
        signal = np.concatenate(
            [tone(625, 1.0), silence(0.05), tone(750, 1.0), silence(0.1), speechish(3.0)]
        )
        source = self.encode(signal, tmp_path / "page.mp3")

        result = process_file(source, AudioConfig(), dst=tmp_path / "page.proc.mp3")

        assert result.meta["tone_pattern"] == "two_tone"
        assert result.meta["content"] == "page"
        # …and excluding the tone spans did not swallow the dispatch behind them.
        assert result.meta["speech_s"] >= 0.25

    def test_the_measurement_survives_ffmpeg_refusing_it(self, tmp_path: Path) -> None:
        """A measurement that cannot run must not turn every call into a no-speech call."""
        source = self.encode(np.concatenate([silence(0.2), speechish(2.0)]), tmp_path / "fb.mp3")
        samples = decode_pcm(source)
        assert speech_seconds(
            tmp_path / "does-not-exist.mp3",
            floor_db=-40.0,
            duration_s=samples.size / SR,
            samples=samples,
        ) >= 0.25


# --------------------------------------------------------------------------------------
# the empty recording (trunkd's 288-byte LAME header)
# --------------------------------------------------------------------------------------


def write_empty_mp3(path: Path) -> Path:
    """A real 288-byte MP3: a LAME/Xing header with not one frame behind it.

    Exactly what trunkd sent about forty times a day — encoded here rather than checked in
    so the bytes come from the same encoder the recorder used, not from a blob nobody can
    regenerate.  ffmpeg answers it with "could not find codec parameters".
    """
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", "0", "-c:a", "libmp3lame", "-b:a", "32k",
            "-id3v2_version", "0", "-write_id3v1", "0", "-y", str(path),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestEmptyRecording:
    def test_the_fixture_is_the_shape_production_saw(self, tmp_path: Path) -> None:
        clip = write_empty_mp3(tmp_path / "empty.mp3")
        assert clip.stat().st_size < 1024
        with pytest.raises(AudioProcessError):
            decode_pcm(clip)

    def test_an_empty_clip_is_named_as_one(self, tmp_path: Path) -> None:
        assert empty_clip_reason(write_empty_mp3(tmp_path / "empty.mp3")) == "no frames"

    def test_a_zero_byte_file_is_named_as_one(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.mp3"
        blank.write_bytes(b"")
        assert empty_clip_reason(blank) == "empty file"

    def test_a_fixture_recording_is_not_empty(self, tmp_path: Path) -> None:
        recordings = sorted(FIXTURES.rglob("*.mp3"))
        if not recordings:
            pytest.skip("no recordings in the fixture tree")
        assert empty_clip_reason(recordings[0]) is None

    def test_a_large_file_that_is_not_audio_is_named_by_the_probe(self, tmp_path: Path) -> None:
        """Over the size floor, so the answer has to come from ffprobe."""
        junk = tmp_path / "junk.mp3"
        junk.write_bytes(b"\x00" * 4096)
        assert empty_clip_reason(junk) in {"no audio stream", "zero duration", None}


# --------------------------------------------------------------------------------------
# the queue: enqueue_many (docs/ARCHITECTURE.md 5.5)
# --------------------------------------------------------------------------------------


@requires_db
class TestEnqueueMany:
    """A backfill set is written in chunks, because one statement cannot hold it.

    PostgreSQL allows 65,535 bind parameters per statement and each queued row spends
    four, so the single-statement insert this used to be died at 16,384 calls — which is
    what ``keyup process-audio`` over a 16,834-call backlog found: one ``OperationalError``
    and nothing queued at all.
    """

    def _calls(self, session, count: int) -> list[int]:  # noqa: ANN001
        from tests.test_reconcile import _call

        return [
            _call(session, path=f"synthetic/queue-{i}.mp3").id
            for i in range(count)
        ]

    def test_a_set_larger_than_one_statement_is_written_in_chunks(self, session) -> None:  # noqa: ANN001
        from app.audioproc.jobs import enqueue_many

        ids = self._calls(session, 7)
        chunks: list[tuple[int, int, int]] = []
        queued = enqueue_many(
            session, ids, chunk_size=3, on_chunk=lambda *args: chunks.append(args)
        )

        assert queued == 7
        # Three statements for seven rows, each reporting the running totals.
        assert chunks == [(3, 3, 7), (6, 6, 7), (7, 7, 7)]

    def test_re_running_it_queues_nothing_twice(self, session) -> None:  # noqa: ANN001
        """What makes committing each chunk safe: an interrupted run just resumes."""
        from app.audioproc.jobs import enqueue_many

        ids = self._calls(session, 5)
        assert enqueue_many(session, ids, chunk_size=2) == 5
        assert enqueue_many(session, ids, chunk_size=2) == 0

    def test_the_chunk_size_stays_clear_of_the_parameter_ceiling(self) -> None:
        from app.audioproc.jobs import ENQUEUE_CHUNK

        assert ENQUEUE_CHUNK * 4 < 65_535


# --------------------------------------------------------------------------------------
# what a no-speech call costs: the job's two side effects
# --------------------------------------------------------------------------------------


@requires_db
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestNoSpeechRetention:
    """A mic key-up keeps its row and loses its recording.

    The archive still knows radio 2087 keyed TRANSIT at 04:59:32 — that is the fact a
    pattern is read from, and it is metadata.  What nobody will ever want is the half
    second of click behind it, so the call joins the ones the discard pass collects after
    their grace (§5.6), which is a policy flag rather than a deletion here.
    """

    @staticmethod
    def seed(session, settings, signal: np.ndarray):  # noqa: ANN001, ANN205
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from app.models import Call, System, Talkgroup

        system = System(name="Example System", protocol="P25 Phase 1")
        session.add(system)
        session.flush()
        talkgroup = Talkgroup(system_id=system.id, tgid=1701, alias="EXAMPLE TRANSIT")
        session.add(talkgroup)
        session.flush()

        rel = "kerchunk/call.mp3"
        target = Path(settings.audio_root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR),
             "-ac", "1", "-i", "-", "-c:a", "libmp3lame", "-b:a", "32k", str(target)],
            input=raw, check=True, capture_output=True,
        )

        started = datetime.now(UTC) - timedelta(minutes=5)
        call = Call(
            system_id=system.id,
            talkgroup_id=talkgroup.id,
            talkgroup_ids=[1701],
            recorded_at=started,
            started_at=started,
            started_local=started.replace(tzinfo=None),
            ended_at=started + timedelta(seconds=0.648),
            duration_s=Decimal("0.648"),
            file_path=rel,
            file_name="call.mp3",
            audio_policy="keep",
        )
        session.add(call)
        session.flush()
        return call

    def clip(self) -> np.ndarray:
        """The production shape: 0.35 s of nothing, one 30 ms click, and out."""
        return np.concatenate([silence(0.35), 0.08 * np.ones(int(0.03 * SR)), silence(0.05)])

    def test_a_page_with_no_speech_keeps_its_audio(self, session, tmp_path) -> None:  # noqa: ANN001
        """Calls 2396355/2396357: a 4.25 s tone-out and nothing else.  It is not a click."""
        from app.audioproc.jobs import process_call
        from app.config import Settings
        from tests.conftest import TEST_DATABASE_URL

        settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, audio_root=tmp_path)
        page = np.concatenate([tone(625, 1.0), silence(0.05), tone(750, 2.75), silence(0.4)])
        call = self.seed(session, settings, page)

        assert process_call(session, call, settings)["content"] == "page"
        assert call.audio_policy == "keep"

    def test_a_no_speech_call_is_marked_for_discard(self, session, tmp_path) -> None:  # noqa: ANN001
        from app.audioproc.jobs import process_call
        from app.config import Settings
        from tests.conftest import TEST_DATABASE_URL

        settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, audio_root=tmp_path)
        call = self.seed(session, settings, self.clip())

        meta = process_call(session, call, settings)

        assert meta["content"] == "none"
        assert call.audio_policy == "discard"

    def test_the_discard_is_a_setting_an_operator_can_turn_off(self, session, tmp_path) -> None:  # noqa: ANN001
        from app.audioproc.jobs import process_call
        from app.config import Settings
        from tests.conftest import TEST_DATABASE_URL

        settings = Settings(
            _env_file=None,
            database_url=TEST_DATABASE_URL,
            audio_root=tmp_path,
            audio_discard_no_speech=False,
        )
        call = self.seed(session, settings, self.clip())

        assert process_call(session, call, settings)["content"] == "none"
        assert call.audio_policy == "keep"

    def test_a_call_somebody_talked_on_keeps_its_audio(self, session, tmp_path) -> None:  # noqa: ANN001
        from app.audioproc.jobs import process_call
        from app.config import Settings
        from tests.conftest import TEST_DATABASE_URL

        settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, audio_root=tmp_path)
        call = self.seed(session, settings, np.concatenate([silence(0.2), speechish(2.0)]))

        assert process_call(session, call, settings)["content"] == "speech"
        assert call.audio_policy == "keep"


@requires_db
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
class TestTheCopyIsCheckedUnderTheCallLock:
    """Retention may move or purge the source while ffmpeg runs.

    The encode re-creates the pruned directory and renames a ``.proc.mp3`` into it; without
    a re-check under the call lock the row write would then name a copy of a recording
    that is gone, in a live-tree directory retention had just emptied.
    """

    @staticmethod
    def settings(tmp_path: Path):  # noqa: ANN205
        from app.config import Settings
        from tests.conftest import TEST_DATABASE_URL

        return Settings(_env_file=None, database_url=TEST_DATABASE_URL, audio_root=tmp_path)

    @staticmethod
    def meanwhile(monkeypatch, call_id: int, **row) -> None:  # noqa: ANN001, ANN003
        """Make ``process_file`` finish, then have "retention" rewrite the row and commit."""
        from sqlalchemy import update

        from app.audioproc import jobs
        from app.db import SessionLocal
        from app.models import Call

        real = jobs.process_file

        def encode_then_retire(source, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            result = real(source, *args, **kwargs)
            other = SessionLocal()
            try:
                other.execute(update(Call).where(Call.id == call_id).values(**row))
                other.commit()
            finally:
                other.close()
            if row.get("audio_state") in {"purged", "archived"}:
                Path(source).unlink()
            return result

        monkeypatch.setattr(jobs, "process_file", encode_then_retire)

    def test_a_copy_of_a_recording_purged_mid_encode_is_deleted(  # noqa: ANN001
        self, session, tmp_path, monkeypatch
    ) -> None:
        from app.audio import processed_path_for
        from app.audioproc.jobs import RecordingMoved, process_call

        settings = self.settings(tmp_path)
        call = TestNoSpeechRetention.seed(session, settings, speechish(1.0))
        session.commit()
        copy = processed_path_for(tmp_path / call.file_path)
        self.meanwhile(monkeypatch, call.id, audio_state="purged")

        with pytest.raises(RecordingMoved):
            process_call(session, call, settings)

        assert not copy.exists()
        # …and the directory the encode re-created is pruned again
        assert not copy.parent.exists()

    def test_a_recording_moved_but_still_present_is_retried(  # noqa: ANN001
        self, session, tmp_path, monkeypatch
    ) -> None:
        from app.audio import processed_path_for
        from app.audioproc.jobs import process_call
        from app.audioproc.process import AudioProcessError

        settings = self.settings(tmp_path)
        call = TestNoSpeechRetention.seed(session, settings, speechish(1.0))
        session.commit()
        copy = processed_path_for(tmp_path / call.file_path)
        self.meanwhile(monkeypatch, call.id, file_path="elsewhere/call.mp3")

        with pytest.raises(AudioProcessError, match="moved"):
            process_call(session, call, settings)
        assert not copy.exists()

    def test_the_worker_cancels_the_job_and_writes_nothing(  # noqa: ANN001
        self, session, tmp_path, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.audio import processed_path_for
        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.models import AudioJob, Call

        settings = self.settings(tmp_path).model_copy(update={"audio_process_enabled": True})
        call = TestNoSpeechRetention.seed(session, settings, speechish(1.0))
        assert enqueue(session, call, settings, force=True) is not None
        session.commit()
        copy = processed_path_for(tmp_path / call.file_path)
        self.meanwhile(monkeypatch, call.id, audio_state="purged")

        worker = AudioProcessWorker(settings=settings, session_factory=lambda: session, worker_name="t")
        assert worker.cycle() is True

        session.expire_all()
        job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
        assert job.status == "cancelled"
        assert "purged" in (job.last_error or "")
        row = session.get(Call, call.id)
        assert row.processed_path is None
        assert row.audio_meta is None or "version" not in row.audio_meta
        assert not copy.exists()


class TestQueueWithTheStageOff:
    def test_queued_jobs_are_cancelled_while_processing_is_off(self, session) -> None:  # noqa: ANN001
        """A job queued past the switch would otherwise sit for ever — and hold the
        syndication feeds' readiness gate for its call with it."""
        from sqlalchemy import select

        from app.audioproc.jobs import AudioProcessWorker, enqueue
        from app.config import get_settings
        from app.models import AudioJob
        from tests.test_reconcile import _call

        call = _call(session, path="synthetic/stage-off.mp3")
        on = get_settings().model_copy(update={"audio_process_enabled": True})
        assert enqueue(session, call, on, force=True) is not None
        session.commit()

        off = get_settings().model_copy(update={"audio_process_enabled": False})
        worker = AudioProcessWorker(settings=off, session_factory=lambda: session, worker_name="t")
        assert worker.cycle() is False
        session.expire_all()
        job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
        assert job.status == "cancelled"
        assert job.last_error == "audio processing is off"


# --------------------------------------------------------------------------------------
# the queue: a job is finished only by the claim that owns it
# --------------------------------------------------------------------------------------


@requires_db
class TestAudioJobOwnership:
    def running_job(self, session, *, minutes_ago: float = 0.0):  # noqa: ANN001, ANN201
        from datetime import UTC, datetime, timedelta

        from app.audioproc.jobs import enqueue
        from app.config import get_settings
        from app.models import AudioJob
        from tests.test_reconcile import _call

        call = _call(session, path=f"synthetic/own-{minutes_ago}.mp3")
        on = get_settings().model_copy(update={"audio_process_enabled": True})
        job_id = enqueue(session, call, on, force=True)
        job = session.get(AudioJob, job_id)
        claimed = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        job.status, job.started_at, job.locked_at, job.locked_by = "running", claimed, claimed, "t"
        session.commit()
        return job, claimed

    def worker(self, session):  # noqa: ANN001, ANN201
        from app.audioproc.jobs import AudioProcessWorker
        from app.config import get_settings

        return AudioProcessWorker(settings=get_settings(), session_factory=lambda: session, worker_name="t")

    def test_a_reclaimed_job_is_not_finished_by_the_first_claim(self, session) -> None:  # noqa: ANN001
        from datetime import timedelta

        job, claimed = self.running_job(session)
        worker = self.worker(session)

        worker._finish(session, job.id, "done", None, claimed - timedelta(seconds=5))  # noqa: SLF001
        worker._fail(session, job.id, 0, 3, "boom", claimed - timedelta(seconds=5))  # noqa: SLF001
        session.expire_all()
        assert (job.status, job.attempts) == ("running", 0)

        worker._finish(session, job.id, "done", None, claimed)  # noqa: SLF001
        session.expire_all()
        assert job.status == "done"

    def test_a_cancelled_job_is_not_failed_back_onto_the_queue(self, session) -> None:  # noqa: ANN001
        job, claimed = self.running_job(session)
        job.status = "cancelled"
        session.commit()

        self.worker(session)._fail(session, job.id, 0, 3, "boom", claimed)  # noqa: SLF001
        session.expire_all()
        assert (job.status, job.attempts) == ("cancelled", 0)

    def test_the_audio_reaper_has_its_own_half_hour_cutoff(self, session) -> None:  # noqa: ANN001
        from app.audioproc.jobs import STALE_MINUTES, requeue_stale

        assert STALE_MINUTES == 30
        slow, _ = self.running_job(session, minutes_ago=20)  # past the STT queue's ten minutes
        dead, _ = self.running_job(session, minutes_ago=45)

        assert requeue_stale(session) == 1
        session.expire_all()
        assert (slow.status, dead.status) == ("running", "queued")
