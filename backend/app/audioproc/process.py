"""Running the audio-processing chain on one recording (ARCHITECTURE §5.5).

:mod:`app.audioproc.chain` decides *what* to do; this module decodes the file, measures it,
runs ffmpeg and writes the processed copy.  Two rules govern everything here:

**The original is never touched.**  The processed copy is written beside it as
``<stem>.proc.mp3`` — same directory, so retention, refiling and the archive layout keep
working unchanged — and ``calls.file_path``, ``calls.sha256`` and the dedupe keep pointing
at the bytes the recorder wrote.  The output goes to a temporary file in the same directory
and is renamed into place, so a reader never sees a half-written ``.proc.mp3`` and a crashed
pass leaves nothing behind but a stray temp file.

**A pass that cannot improve the call must not lose it.**  Every failure mode — ffmpeg
missing, a filter this build does not carry, an unreadable file, a first loudnorm pass that
measured nothing — degrades to "store the original and say so in the meta", never to a
dropped call.  The processed copy is an optimization; ``file_path`` is the archive.

The decode is one 8 kHz mono pass shared by everything that needs samples: the trim
boundaries, the level measurements and the tone detector.  These files are 32 kbit/s mono
and a couple of seconds long, so that decode is a few milliseconds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.audio import NO_CONTENT, PROCESSED_SUFFIX, processed_path_for
from app.audioproc.chain import (
    LOUDNORM_MIN_S,
    PIPELINE_VERSION,
    AudioConfig,
    LoudnormStats,
    Measurement,
    depth_for,
    filter_chain,
)
from app.audioproc.tones import ToneReport, ToneSegment, detect_tones

log = logging.getLogger("app.audioproc")

__all__ = [
    "PROCESSED_SUFFIX",
    "AudioProcessError",
    "ProcessResult",
    "SourceFormat",
    "EMPTY_CLIP_MAX_BYTES",
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_S",
    "decode_pcm",
    "empty_clip_reason",
    "CONTENT_NONE",
    "CONTENT_PAGE",
    "CONTENT_SPEECH",
    "CONTENT_STATIC",
    "NO_CONTENT",
    "STATIC_MAX_LEVEL_DB",
    "flat_fraction",
    "measure",
    "probe",
    "speech_seconds",
    "process_file",
    "processed_path_for",
    "render_to_mp3",
]

#: The decode every measurement is made on.  8 kHz is the source rate of most of this
#: archive and is twice the top of the vocoder's usable band, so nothing a tone detector or
#: a level meter cares about is lost.
ANALYSIS_RATE = 8000

#: Floor for a level in dB: what an all-zero clip measures.  Real audio never approaches it.
SILENCE_DB = -100.0

#: Window the trim scans with.  10 ms is short enough to land the cut inside a syllable's
#: onset and long enough that one stray sample of noise does not defeat the threshold.
_TRIM_WINDOW_MS = 10.0

#: Encoding floor.  A recording stored below this is a recorder oddity, and re-encoding a
#: processed copy at 8 kbit/s would undo the point of processing it.
MIN_BITRATE_BPS = 16_000

FFMPEG_TIMEOUT_S = 180.0

#: loudnorm prints its JSON to stderr after everything else it has to say.
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)

#: ``silencedetect`` announces each quiet stretch as a pair of lines on stderr.
_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")

#: The shortest gap ``silencedetect`` is asked to report.  It has to be well under a
#: syllable, because a stretch of quiet it does not report is counted as speech; 50 ms is
#: five of the trim's own windows and short enough that the pause inside a word is seen.
SPEECH_MIN_SILENCE_S = 0.05

#: What ``audio_meta.content`` can say.  ``speech`` is the ordinary answer.  ``page`` is a
#: call that is *only* a pager tone-out and is content all the same — production calls
#: the tone-out pair are 4.25 s fire dispatch pages, an A tone of about a second then a B tone
#: of about 2.75, and not one word: they are the most important audio on the system.  ``none``
#: is a recording with nothing in it worth transcribing, exporting or keeping — a mic key-up
#: with no words behind it ("kerchunk"), which trunkd records as faithfully as a sentence.
#: **Only ``none`` costs a call anything**; ``page`` is treated exactly like ``speech``
#: everywhere except that nobody expects a transcript of it.
#: ``static`` is the fourth: broadband vocoder noise across the whole speech band, a
#: squelch tail or a failed decode rather than a quiet talker.  It has to be its own value
#: because the measure that finds the others cannot see it — :func:`speech_seconds` counts
#: seconds above a floor on the *processed* copy, and static lifted 25 dB by the gain stage
#: clears that floor for its whole length, so the three static calls
#: were all stored ``speech`` with 0.4-1.3 s of "speech" in them.  What separates it is
#: :func:`flat_fraction`.  It is handled exactly like ``none`` — never transcribed, never
#: exported, discarded with the rest — and is a separate name only so the archive can say
#: *why*, the same reason ``page`` is not ``speech``.
CONTENT_SPEECH = "speech"
CONTENT_PAGE = "page"
CONTENT_NONE = "none"
CONTENT_STATIC = "static"

#: The contents with nothing in them worth keeping (:data:`app.audio.NO_CONTENT`, defined
#: there so the exporters can read it without importing numpy).  Every gate that refused a
#: ``none`` call reads this, so adding a value is all it takes to have the exporters, the
#: transcriber and the discard pass agree about it.
assert set(NO_CONTENT) == {CONTENT_NONE, CONTENT_STATIC}

#: The band a spectral-flatness measurement is taken over: what the vocoder actually
#: carries.  Below 300 Hz is rumble and above 3400 Hz is the top of its range, and both are
#: flat on *every* call, static or not.
FLATNESS_LO_HZ = 300.0
FLATNESS_HI_HZ = 3400.0

#: Frame the flatness is measured on: 256 samples at 8 kHz, 50 % overlap — the same 32 ms
#: frame :mod:`app.audioproc.tones` analyses, for the same reason.
FLATNESS_FRAME = 256

#: A frame under this is not part of the recording, only the space around it, and its
#: spectrum is whatever noise happened to be in the channel.  Measured over the review
#: corpus this floor reproduces the analysis document's flat-frac figures within 0.02.
FLATNESS_LIVE_DB = -60.0

#: …and the level half of the static rule.  A clip louder than this is somebody talking,
#: whatever its spectrum looks like: the two known-good calls whose flatness comes closest
#: to a static one (the quiet duplicate pair, the same transmission recorded twice, flat-frac 0.74)
#: sit at -19 dBFS and are excluded on level alone.  The level term is what carries the
#: rule — the flatness margin on its own is only about 0.05.
STATIC_MAX_LEVEL_DB = -35.0


class AudioProcessError(RuntimeError):
    """The recording could not be decoded or encoded at all."""


@dataclass(frozen=True)
class SourceFormat:
    """The stored file's own format, so the processed copy matches it."""

    sample_rate: int = ANALYSIS_RATE
    channels: int = 1
    bitrate_bps: int = MIN_BITRATE_BPS
    duration_s: float = 0.0

    @property
    def out_bitrate_bps(self) -> int:
        return max(MIN_BITRATE_BPS, int(self.bitrate_bps or 0))


@dataclass(frozen=True)
class ProcessResult:
    """One pass: the meta to store, the tones found, and whether a file was written."""

    meta: dict[str, Any]
    tones: ToneReport
    wrote: bool
    path: Path | None = None

    @property
    def tone_detected(self) -> bool:
        return self.tones.detected

    @property
    def alert(self) -> bool:
        return self.tones.alert


def _run(cmd: list[str], *, timeout_s: float = FFMPEG_TIMEOUT_S) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise AudioProcessError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessError(f"{cmd[0]} timed out after {timeout_s:.0f}s") from exc


def _stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]


@lru_cache(maxsize=1)
def aselect_works() -> bool:
    """Whether this ffmpeg's ``aselect`` actually drops the frames its expression rejects.

    ffmpeg 8.0.x's does not: every frame passes whatever the expression says, even
    ``aselect=0`` (an upstream regression, fixed in their master but not in 8.0.1).  Tone
    removal would then encode the tones untouched while the meta said ``tone_removed``, so
    :func:`app.audioproc.chain.filter_chain` cuts with ``atrim``/``concat`` instead when this
    is false.  Probed once per process: half a second of generated silence through
    ``aselect=0`` must come out empty.  ``True`` when ffmpeg cannot be run at all — the
    encode fails on its own then, and the chain stays what it always was.
    """
    try:
        probe_cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "anullsrc=r=8000:cl=mono", "-t", "0.5",
            "-af", "aselect=0", "-f", "s16le", "-",
        ]  # fmt: skip
        proc = _run(probe_cmd, timeout_s=30)
    except AudioProcessError:
        return True
    if proc.returncode != 0:
        return True
    if proc.stdout:
        log.warning("audioproc: this ffmpeg's aselect drops nothing; tone removal uses atrim")
        return False
    return True


def probe(path: Path | str) -> SourceFormat:
    """Sample rate, channels, bitrate and duration of the stored file.

    Falls back to the defaults rather than raising: a file ffprobe cannot describe may
    still decode, and a processed copy at 8 kHz mono 16 kbit/s is a reasonable answer for
    anything this archive holds.
    """
    if shutil.which("ffprobe") is None:
        return SourceFormat()
    proc = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,bit_rate:format=bit_rate,duration",
            "-of", "json", str(path),
        ],  # fmt: skip
        timeout_s=30.0,
    )
    if proc.returncode != 0:
        return SourceFormat()
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return SourceFormat()
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}

    def _int(*values: Any) -> int:
        for value in values:
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return 0

    return SourceFormat(
        sample_rate=_int(stream.get("sample_rate")) or ANALYSIS_RATE,
        channels=_int(stream.get("channels")) or 1,
        bitrate_bps=_int(stream.get("bit_rate"), fmt.get("bit_rate")) or MIN_BITRATE_BPS,
        duration_s=float(fmt.get("duration") or 0.0) if fmt.get("duration") else 0.0,
    )


#: A recording at most this large cannot hold a frame of audio.  trunkd emitted about
#: forty of these a day: a 288-byte MP3 that is a LAME/Xing header and nothing else, which
#: ffmpeg answers with "could not find codec parameters".  A real clip of this archive's
#: shortest transmission at its lowest bitrate is several kilobytes.
EMPTY_CLIP_MAX_BYTES = 1024


def empty_clip_reason(path: Path | str) -> str | None:
    """Why this file holds no audio at all, or ``None`` when it holds some.

    Asked only after a decode has already failed, to tell the two failures apart: a
    recording ffmpeg could not read (a real fault — retry it, park it, look at it) from a
    container with nothing in it (nothing was recorded — there is no work to do and no
    error to report).  Cheap first: a file under
    :data:`EMPTY_CLIP_MAX_BYTES` is answered without running anything.  Otherwise ffprobe
    decides, and a probe that cannot answer says ``None`` — the failure stands.
    """
    file = Path(path)
    try:
        size = file.stat().st_size
    except OSError:
        return None
    if size == 0:
        return "empty file"
    if size < EMPTY_CLIP_MAX_BYTES:
        return "no frames"
    if shutil.which("ffprobe") is None:
        return None
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "json", str(file),
        ],  # fmt: skip
        timeout_s=30.0,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not [s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"]:
        return "no audio stream"
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return None
    if duration <= 0.0:
        return "zero duration"
    return None


def decode_pcm(path: Path | str, *, sample_rate: int = ANALYSIS_RATE, af: str | None = None) -> np.ndarray:
    """Mono float samples in ±1.0, decoded by ffmpeg.  ``af`` applies a chain first."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        *(["-af", af] if af else []),
        "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-",
    ]  # fmt: skip
    proc = _run(cmd)
    if proc.returncode != 0 and not proc.stdout:
        raise AudioProcessError(f"ffmpeg could not decode {Path(path).name}: {_stderr(proc)}")
    raw = proc.stdout
    usable = len(raw) - (len(raw) % 2)
    if usable <= 0:
        return np.zeros(0, dtype=np.float64)
    return np.frombuffer(raw[:usable], dtype="<i2").astype(np.float64) / 32768.0


def _db(value: float) -> float:
    return float(max(SILENCE_DB, 20.0 * np.log10(max(float(value), 1e-12))))


def measure(samples: np.ndarray, sample_rate: int, config: AudioConfig) -> Measurement:
    """Level, peak and the dead air at each end — everything the chain decides from."""
    duration = samples.size / sample_rate if sample_rate else 0.0
    if samples.size == 0:
        return Measurement(0.0, SILENCE_DB, SILENCE_DB, sample_rate=sample_rate)
    mean_db = _db(np.sqrt(np.mean(samples**2)))
    peak_db = _db(np.max(np.abs(samples)))

    head = tail = 0.0
    trimmed = None
    window = max(1, int(sample_rate * _TRIM_WINDOW_MS / 1000.0))
    count = samples.size // window
    if config.trim_silence and count >= 2:
        blocks = samples[: count * window].reshape(count, window)
        loud = np.max(np.abs(blocks), axis=1) > 10.0 ** (config.trim_threshold_db / 20.0)
        if loud.any():
            first = int(np.argmax(loud))
            last = count - 1 - int(np.argmax(loud[::-1]))
            keep = config.trim_keep_ms / 1000.0
            head = max(0.0, first * window / sample_rate - keep)
            tail = max(0.0, duration - ((last + 1) * window / sample_rate + keep))
            # Never trim so hard there is nothing left; a clip that is all dead air keeps
            # its whole self and is marked `silent` instead.
            if duration - head - tail < 0.05:
                head = tail = 0.0
        else:
            head = tail = 0.0
    if head > 0.0 or tail > 0.0:
        lo = int(head * sample_rate)
        hi = samples.size - int(tail * sample_rate)
        kept = samples[lo:hi]
        trimmed = _db(np.sqrt(np.mean(kept**2))) if kept.size else mean_db
    return Measurement(
        duration_s=duration,
        mean_db=mean_db,
        peak_db=peak_db,
        head_trim_s=round(head, 3),
        tail_trim_s=round(tail, 3),
        trimmed_mean_db=trimmed,
        sample_rate=sample_rate,
    )


def _complement(spans: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    """What ``[0, total]`` has left once ``spans`` are taken out of it."""
    out: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(spans):
        if start > cursor:
            out.append((cursor, min(start, total)))
        cursor = max(cursor, end)
        if cursor >= total:
            break
    if cursor < total:
        out.append((cursor, total))
    return [(lo, hi) for lo, hi in out if hi > lo]


def _subtract(spans: list[tuple[float, float]], cuts: Sequence[tuple[float, float]]) -> float:
    """Total length of ``spans`` once every ``cut`` has been taken out of them."""
    total = 0.0
    for start, end in spans:
        remaining = [(start, end)]
        for cut_start, cut_end in cuts:
            nxt: list[tuple[float, float]] = []
            for lo, hi in remaining:
                if cut_end <= lo or cut_start >= hi:
                    nxt.append((lo, hi))
                    continue
                if cut_start > lo:
                    nxt.append((lo, cut_start))
                if cut_end < hi:
                    nxt.append((cut_end, hi))
            remaining = nxt
        total += sum(hi - lo for lo, hi in remaining)
    return total


def speech_seconds(
    path: Path | str,
    *,
    floor_db: float,
    duration_s: float,
    samples: np.ndarray | None = None,
    sample_rate: int = ANALYSIS_RATE,
    exclude: Sequence[tuple[float, float]] = (),
) -> float:
    """How many seconds of ``path`` are above ``floor_db`` — the *speech content* measure.

    ``silent`` (``audio_silent_below_db``, -60 dBFS) asks whether the whole clip is quiet,
    and a mic key-up answers no: the key-up pair are 0.35 s at -80 dB
    followed by one 30 ms click at -22 dB, which averages -50 dB and clears the silence
    floor comfortably.  Nobody said anything.  The question that separates them is not how
    loud the clip is but *how much of it* is loud, which is what this counts.

    Measured with ``silencedetect``, which is the one filter that reports stretches rather
    than an average, over the **processed** copy — after the trim, the gain and the tone
    handling, so the floor means the same thing for a quiet talker as for a loud one.  A
    run ffmpeg cannot complete falls back to counting windows over ``samples`` (the decode
    the caller already has), because a measurement that fails must not turn every call into
    a no-speech call.

    ``exclude`` is stretches that are loud but are **not speech** — the tone spans, in this
    same processed timebase.  A page or a console attention tone is above the floor by a wide
    margin, and without this a tone-only call (2.0 s of 993 Hz and
    nothing else) measures two seconds of "speech" and is transcribed, exported and kept.
    """
    floor = float(floor_db)
    total = max(0.0, float(duration_s))
    if total <= 0.0:
        return 0.0
    proc = _run(
        [
            "ffmpeg", "-nostdin", "-v", "info", "-i", str(path),
            "-af", f"silencedetect=n={floor:.1f}dB:d={SPEECH_MIN_SILENCE_S:.3f}",
            "-f", "null", "-",
        ]  # fmt: skip
    )
    cuts = [
        (max(0.0, float(lo)), min(total, float(hi)))
        for lo, hi in exclude
        if min(total, float(hi)) > max(0.0, float(lo))
    ]
    if proc.returncode == 0:
        text = (proc.stderr or b"").decode("utf-8", "replace")
        starts = [float(value) for value in _SILENCE_START.findall(text)]
        ends = [float(value) for value in _SILENCE_END.findall(text)]
        quiet: list[tuple[float, float]] = []
        for index, start in enumerate(starts):
            # A clip that ends inside a silence gets no closing line from some builds; the
            # stretch runs to the end of the audio, which is what ``total`` says.
            end = ends[index] if index < len(ends) else total
            lo, hi = max(0.0, start), min(end, total)
            if hi > lo:
                quiet.append((lo, hi))
        # What is left when the quiet stretches are taken out of the clip: the loud ones.
        loud_spans = _complement(quiet, total)
        return round(_subtract(loud_spans, cuts), 3)

    log.info("audioproc: silencedetect failed on %s; counting windows instead", Path(path).name)
    if samples is None or samples.size == 0 or sample_rate <= 0:
        return 0.0
    window = max(1, int(sample_rate * _TRIM_WINDOW_MS / 1000.0))
    count = samples.size // window
    if count == 0:
        return 0.0
    blocks = samples[: count * window].reshape(count, window)
    loud = np.sqrt(np.mean(blocks**2, axis=1)) > 10.0 ** (floor / 20.0)
    if cuts:
        centres = (np.arange(count) + 0.5) * window / sample_rate
        for lo, hi in cuts:
            loud &= ~((centres >= lo) & (centres < hi))
    return round(float(np.count_nonzero(loud)) * window / sample_rate, 3)


def flat_fraction(
    samples: np.ndarray,
    sample_rate: int = ANALYSIS_RATE,
    *,
    threshold: float = 0.30,
) -> float:
    """The share of live frames whose in-band spectrum is flatter than ``threshold``.

    Spectral flatness is the geometric mean of a frame's power spectrum over its arithmetic
    mean: 1.0 for white noise, near zero for a tone or a voiced vowel, because a vowel puts
    its energy in formants and their harmonics and leaves deep nulls between them.  Taken
    over :data:`FLATNESS_LO_HZ`-:data:`FLATNESS_HI_HZ` only, and only over frames above
    :data:`FLATNESS_LIVE_DB`, so neither the rumble under the band nor the silence around
    the transmission votes.

    **Measured on the recorder's own file, not on the processed copy.**  That is not a
    detail: measured on the processed copy the rule stops working entirely.  The chain
    lifts a static clip 24-30 dB, so the three static calls come off it at -28.1 to
    -29.0 dBFS — above :data:`STATIC_MAX_LEVEL_DB`, which flags **none** of the three —
    while a real quiet call rises to 0.72 flat and starts looking like one.  The
    raw decode is also the one :func:`measure` already has in hand, so this costs an FFT
    over a couple of seconds of audio and no extra decode.

    Returns 0.0 for a clip with no live frames at all, which is what silence is: a call
    with nothing in it is ``none``, and it must not become ``static`` for want of evidence.
    """
    audio = np.asarray(samples, dtype=np.float64).reshape(-1)
    frame = FLATNESS_FRAME
    if sample_rate <= 0 or audio.size < frame:
        return 0.0
    hop = frame // 2
    count = 1 + (audio.size - frame) // hop
    stride = audio.strides[0]
    blocks = np.lib.stride_tricks.as_strided(
        audio, shape=(count, frame), strides=(hop * stride, stride), writeable=False
    )
    live = np.sqrt(np.mean(blocks**2, axis=1)) > 10.0 ** (FLATNESS_LIVE_DB / 20.0)
    if not live.any():
        return 0.0
    spectra = np.abs(np.fft.rfft(blocks[live] * np.hanning(frame), axis=1)) ** 2
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
    band = spectra[:, (freqs >= FLATNESS_LO_HZ) & (freqs <= FLATNESS_HI_HZ)] + 1e-20
    # exp(mean(log)) rather than prod()**(1/n): the product of a hundred power values
    # underflows to zero long before it means anything.
    flatness = np.exp(np.mean(np.log(band), axis=1)) / np.mean(band, axis=1)
    return round(float(np.count_nonzero(flatness > float(threshold)) / flatness.size), 3)


def _loudnorm_stats(path: Path, chain: str) -> LoudnormStats | None:
    """Run the measuring pass and parse its JSON, or ``None`` when it says nothing usable."""
    proc = _run(
        ["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-af", chain, "-f", "null", "-"]
    )
    match = _LOUDNORM_JSON.search((proc.stderr or b"").decode("utf-8", "replace"))
    if match is None:
        return None
    try:
        return LoudnormStats.from_json(json.loads(match.group(0)))
    except ValueError:
        return None


def _encode(src: Path, dst: Path, chain: str, fmt: SourceFormat) -> None:
    """Encode ``src`` through ``chain`` into ``dst``, atomically.

    Same container, channel count and sample rate as the source — a processed copy that
    resampled would be a different recording, and every consumer (the player, the export
    transcoder, the transcriber's own decode) already handles what is stored.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(dst.parent), prefix=".proc-", suffix=".mp3")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        proc = _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
                *(["-af", chain] if chain else []),
                "-map_metadata", "-1",
                "-ac", "1", "-ar", str(fmt.sample_rate),
                "-c:a", "libmp3lame", "-b:a", str(fmt.out_bitrate_bps),
                "-f", "mp3", str(tmp),
            ]  # fmt: skip
        )
        if proc.returncode != 0:
            raise AudioProcessError(f"ffmpeg failed on {src.name}: {_stderr(proc)}")
        if tmp.stat().st_size == 0:
            raise AudioProcessError(f"ffmpeg produced an empty file for {src.name}")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


#: The longest stretch of a call the profile editor's preview will render.  A preview is
#: something an operator listens to while tuning a filter, and the judgement is made in the
#: first few seconds; the cap is what stops "preview this chain" from being a way to make
#: the API transcode an hour of audio per click.
PREVIEW_MAX_S = 60.0

#: And the cap on what comes back, in case a profile does something surprising to the
#: encoder.  A minute of this archive's audio is about 120 kB at 16 kbit/s; two megabytes
#: is room for a 256 kbit/s source and still small enough to hold in memory without care.
PREVIEW_MAX_BYTES = 2 * 1024 * 1024


def render_to_mp3(
    src: Path | str,
    chain: str,
    *,
    seconds: float | None = None,
    bitrate_bps: int | None = None,
) -> bytes:
    """Encode ``src`` through ``chain`` and hand back the MP3 bytes.  Nothing is stored.

    The profile editor's A/B: the *original* recording rendered through a chain that has
    not been saved, so an operator can hear a filter before a talkgroup is committed to it.
    Deliberately not a variant of :func:`process_file` — there is no measurement, no tone
    detection, no trim, and above all no file written beside the call — and deliberately
    capped, at :data:`PREVIEW_MAX_S` of audio and :data:`PREVIEW_MAX_BYTES` of output.

    ``chain`` must already have come out of :func:`app.audioproc.profiles.render_chain`.
    Nothing here inspects it: this is the function that hands a string to ffmpeg, so the
    string reaching it has to have been built from the whitelist, not checked here after
    the fact.
    """
    source = Path(src)
    limit = min(float(seconds or PREVIEW_MAX_S), PREVIEW_MAX_S)
    fmt = probe(source)
    handle, tmp_name = tempfile.mkstemp(prefix="keyup-preview-", suffix=".mp3")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        proc = _run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-t", f"{limit:.3f}", "-i", str(source),
                *(["-af", chain] if chain else []),
                "-map_metadata", "-1",
                "-ac", "1", "-ar", str(fmt.sample_rate),
                "-c:a", "libmp3lame", "-b:a", str(fmt.out_bitrate_bps),
                "-f", "mp3", str(tmp),
            ]  # fmt: skip
        )
        if proc.returncode != 0:
            raise AudioProcessError(f"ffmpeg failed on {source.name}: {_stderr(proc)}")
        size = tmp.stat().st_size
        if size == 0:
            raise AudioProcessError(f"ffmpeg produced an empty preview for {source.name}")
        if size > PREVIEW_MAX_BYTES:
            raise AudioProcessError(
                f"the preview came to {size} bytes, over the {PREVIEW_MAX_BYTES}-byte cap"
            )
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


#: One warning per process for a denoiser that cannot run, not one per call.
_warned_model = False


def _warn_missing_model(config: AudioConfig) -> None:
    """Say once that ``arnndn`` is configured with no model and ``afftdn`` is running instead.

    The Admin page refuses to save that combination, so reaching here means the model file
    was removed after it was saved — worth a line in the log, and not worth failing a call
    over: :func:`app.audioproc.chain.filter_chain` falls back on its own.
    """
    global _warned_model
    if config.noise_reduction != "arnndn" or config.rnnoise_model or _warned_model:
        return
    _warned_model = True
    log.warning(
        "audioproc: audio_noise_reduction is 'arnndn' but audio_rnnoise_model names no "
        "readable file; falling back to afftdn"
    )


def process_file(
    src: Path | str,
    config: AudioConfig,
    *,
    dst: Path | str | None = None,
    profile: Sequence[str] | None = None,
    profile_name: str | None = None,
) -> ProcessResult:
    """Measure ``src``, write its processed copy and return the meta to store.

    Raises :class:`AudioProcessError` only when the source cannot be decoded or the encode
    fails outright — those are worth a retry.  Everything softer (an empty clip, a loudnorm
    pass that measured nothing) is recorded in the meta and the pass still succeeds.

    ``profile`` is a talkgroup's own filter stages, already rendered by
    :func:`app.audioproc.profiles.render_steps`; it stands in for the global denoiser and
    gate (see :func:`app.audioproc.chain.filter_chain`).  ``profile_name`` goes into the
    meta beside the literal filter string, because "which chain produced this file" is the
    first question asked of a call that sounds wrong and the string alone does not answer
    it once two profiles render alike.
    """
    _warn_missing_model(config)
    source = Path(src)
    target = Path(dst) if dst is not None else processed_path_for(source)
    fmt = probe(source)
    samples = decode_pcm(source, sample_rate=ANALYSIS_RATE)
    m = measure(samples, ANALYSIS_RATE, config)

    tones = detect_tones(samples, ANALYSIS_RATE) if config.tone_detect else ToneReport()
    # The *merged* spans, not the segments: one span per pattern, so a warble is turned
    # down whole rather than only on the halves the detector could measure.
    spans: tuple[ToneSegment, ...] = tones.spans if config.tone_handling != "keep" else ()
    # Only a removal needs aselect, so only a removal pays for the (cached) probe.
    select_works = aselect_works() if spans and config.tone_handling == "remove" else True

    duration_after_trim = m.kept_s
    loudness: LoudnormStats | str | None = None
    lufs: float | None = None
    if duration_after_trim >= LOUDNORM_MIN_S:
        measuring = filter_chain(
            m, spans, config, loudness="measure", profile=profile, select_works=select_works
        )
        stats = _loudnorm_stats(source, measuring)
        if stats is not None:
            loudness = stats
            lufs = round(stats.input_i, 2)
        else:
            log.info(
                "audioproc: loudnorm measured nothing usable for %s; using measured gain",
                source.name,
            )

    chain = filter_chain(m, spans, config, loudness=loudness, profile=profile, select_works=select_works)
    _encode(source, target, chain, fmt)

    after = decode_pcm(target, sample_rate=ANALYSIS_RATE)
    after_mean = _db(np.sqrt(np.mean(after**2))) if after.size else SILENCE_DB
    after_peak = _db(np.max(np.abs(after))) if after.size else SILENCE_DB

    silent = m.level_db < config.silent_below_db
    # …and the other question about an empty call: not "is all of it quiet" but "how much of
    # it is not".  Measured on the processed copy, so the floor is read against audio whose
    # level has already been evened out.
    # A tone is loud and is not speech.  The spans move with the trim into the processed
    # timebase, exactly as the filter chain moved them — except under ``remove``, where the
    # tone is already gone from the file and the timeline was renumbered under it, so there
    # is nothing to exclude and excluding anything would take real speech with it.
    tone_cuts: tuple[tuple[float, float], ...] = ()
    if config.tone_handling != "remove":
        tone_cuts = tuple(
            (max(0.0, span.start_s - m.head_trim_s), max(0.0, span.end_s - m.head_trim_s))
            for span in tones.spans
        )
    speech_s = speech_seconds(
        target,
        floor_db=config.speech_floor_db,
        duration_s=after.size / ANALYSIS_RATE if after.size else m.kept_s,
        samples=after,
        exclude=tone_cuts,
    )
    # A page is content whether or not anybody spoke after it: a tone-out with no dispatch
    # behind it is still the thing the channel exists to carry.  A *lone* held tone is not —
    # that is the console's attention tone, which is why the test is on the span's pattern
    # and not merely on "a tone was found".
    # …and static, which :func:`speech_seconds` cannot see at all — broadband noise lifted
    # 25 dB by the gain stage is above the speech floor for its whole length.  Measured on
    # the *source* decode (:func:`flat_fraction`), and only for a clip that is also quiet:
    # both halves are needed, and the level half is the one doing the work.
    flat_frac = flat_fraction(samples, ANALYSIS_RATE, threshold=config.static_flatness)
    is_static = (
        flat_frac >= config.static_frame_share and m.level_db <= STATIC_MAX_LEVEL_DB
    )
    if any(span.is_page for span in tones.spans):
        content = CONTENT_PAGE
    elif is_static:
        content = CONTENT_STATIC
    elif speech_s >= config.min_speech_seconds:
        content = CONTENT_SPEECH
    else:
        content = CONTENT_NONE
    meta: dict[str, Any] = {
        "version": PIPELINE_VERSION,
        "filter": chain,
        #: the profile that produced ``filter``, or None for the global chain
        "profile": profile_name,
        "duration_s": round(m.duration_s, 3),
        "mean_db": round(m.mean_db, 2),
        "peak_db": round(m.peak_db, 2),
        "trimmed_mean_db": None if m.trimmed_mean_db is None else round(m.trimmed_mean_db, 2),
        "mean_db_after": round(after_mean, 2),
        "peak_db_after": round(after_peak, 2),
        "lufs": lufs,
        "gain_mode": "loudnorm" if isinstance(loudness, LoudnormStats) else "volume",
        "gain_db": round(after_mean - m.level_db, 2) if not silent else 0.0,
        "head_trim_s": m.head_trim_s,
        "tail_trim_s": m.tail_trim_s,
        "noise_reduced": bool(profile) if profile is not None else config.noise_reduction != "off",
        "tones": tones.as_dicts(),
        #: the merged spans, each with the depth it was actually turned down by — the
        #: number the export chain and the in-app player both have to put back on when
        #: their own loudness stage lifts it again
        "tone_spans": [
            {
                **span.as_dict(),
                **(
                    {"attenuate_db": round(abs(depth_for(span.is_page, config)), 1)}
                    if config.tone_handling == "attenuate"
                    else {}
                ),
            }
            for span in spans
        ],
        "tone_pattern": tones.pattern,
        "tone_removed": bool(spans) and config.tone_handling == "remove",
        "alert": tones.alert,
        "silent": silent,
        #: ``speech``, ``page``, ``static`` or ``none`` — whether there is anything in this recording
        #: (:func:`speech_seconds`).  A ``none`` call is never transcribed, never exported,
        #: and its audio is discarded after the grace when ``audio_discard_no_speech`` is on;
        #: the row stays.  ``page`` is a tone-out with nothing said after it, and is kept and
        #: published exactly like ``speech``.
        "content": content,
        "speech_s": speech_s,
        "speech_floor_db": round(float(config.speech_floor_db), 1),
        #: the share of live frames whose in-band spectrum is flatter than
        #: ``audio_static_flatness`` — the measurement behind ``content=static``, stored on
        #: every call so the rule can be audited against the archive rather than argued about
        "flat_frac": flat_frac,
        "sample_rate": fmt.sample_rate,
        "bitrate": fmt.out_bitrate_bps,
    }
    return ProcessResult(meta=meta, tones=tones, wrote=True, path=target)
