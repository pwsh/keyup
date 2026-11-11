#!/usr/bin/env python3
"""Regenerate the synthetic test fixtures under ``backend/tests/fixtures``.

Every audio file and every Broadcastify export in this tree is made here, from a fixed
seed, so nothing in it is a recording of anybody: no off-air speech, no real talkgroup or
radio ids, no real feed.  Run it from ``backend/``::

    .venv/bin/python tests/fixtures/gen_fixtures.py          # rewrite everything
    .venv/bin/python tests/fixtures/gen_fixtures.py --check  # regenerate into a temp dir
                                                             # and report what would change

It needs numpy, mutagen (both already backend dependencies) and an ``ffmpeg`` with
``libmp3lame`` on ``PATH``.  The PCM fixtures and the CSVs are bit-for-bit reproducible;
the MP3 bytes depend on the LAME build, so a different ffmpeg can produce different bytes
(and therefore different sha256 values in ``manifest.csv``) for the same samples.  The
committed files are the reference; regenerate only when a fixture has to change, and commit
the manifest with the audio.

What is generated, and the property each piece exists to exercise:

``tones/*.f32``
    8 kHz mono float32 excerpts for ``tests/test_audioproc.py``'s tone detector cases: two
    clean warbles, one with a frayed fading tail, one whose low halves are too weak to be
    seen, one long vocoder-like single tone that wanders, and one that is only a voice.
    Each is lead-in noise, the tone region, then synthetic speech — the speech is where a
    detector's false positives come from, so it is part of every case.
``speech/voiced.f32``
    Four seconds of synthetic voiced speech (a glottal-like harmonic series under moving
    formants, syllable envelopes and pauses) for the level and flatness tests, which need
    a signal that is speech-shaped and not a tone or noise.
``audio/<day>/<folder>/*.mp3``
    Thirteen sdrtrunk-style recordings of the same synthetic voice (or, for the shortest,
    a squelch-tail noise burst): 22.05 kHz mono 32 kbps CBR with sdrtrunk's ID3 frames and
    its filename grammar — plain calls, a bare and a fully-qualified radio, no FROM, a
    ``_V2`` collision pair, and four patch shapes.  The system is "Example System", the
    site "Central", the talkgroups 1xxx and the radios 9xxxxxx.
``manifest.csv``
    One row per MP3: what it is for, its ids, its duration and its sha256.
``cache/waveform/1.json``
    The waveform cache entry for call 1 (the plain call), which the API tests read back.
``bcfy/owner_hourly_sample.csv``, ``bcfy/owner_uptime_sample.csv``
    Invented dashboard exports in the shape Broadcastify's feed-owner pages download,
    including the structural quirks the importer tests assert: newest-first order, a UTC
    outage spread over two adjacent hours, and three incident days far above the rest.

The trunk-recorder fixtures (``trunk_recorder/EXAMPLE/``) and the small hand-written
Broadcastify response shapes in ``bcfy/`` are not generated here; see ``bcfy/README.md``.
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

SEED = 20260829
TONE_SR = 8000
MP3_SR = 22050
MP3_BITRATE = 32000
#: One MPEG-2 layer III frame: 576 samples.
MP3_FRAME = 576

SYSTEM = "Example System"
SITE = "Central"
CHANNEL_NAME = "T-Control"
DECODER = "P25 Phase 1"
RECORDER = "sdrtrunk nightly - 2026-02-19T09:01:39.551+0000"
WACN = 101010


# --------------------------------------------------------------------------------------
# signal building blocks
# --------------------------------------------------------------------------------------


def _envelope(n: int, sr: int, attack_s: float = 0.02, release_s: float = 0.03) -> np.ndarray:
    env = np.ones(n)
    a = min(n // 2, int(attack_s * sr))
    r = min(n // 2, int(release_s * sr))
    if a:
        env[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
    if r:
        env[n - r :] = 0.5 + 0.5 * np.cos(np.pi * np.arange(r) / r)
    return env


def noise(seconds: float, sr: int, rng: np.random.Generator, db: float) -> np.ndarray:
    """Gaussian noise at an RMS of ``db`` dBFS."""
    return rng.standard_normal(int(round(seconds * sr))) * 10.0 ** (db / 20.0)


def tone(hz: float, seconds: float, sr: int, *, amplitude: float, phase: float = 0.0) -> np.ndarray:
    t = np.arange(int(round(seconds * sr))) / sr
    return amplitude * np.sin(2.0 * np.pi * hz * t + phase)


def fm_tone(
    hz: float, seconds: float, sr: int, *, amplitude: float, depth_hz: float, rate_hz: float
) -> np.ndarray:
    """A tone whose frequency wanders ``±depth_hz`` at ``rate_hz`` — a vocoder's tone."""
    n = int(round(seconds * sr))
    t = np.arange(n) / sr
    inst = hz + depth_hz * np.sin(2.0 * np.pi * rate_hz * t)
    return amplitude * np.sin(2.0 * np.pi * np.cumsum(inst) / sr)


#: Vowel targets (F1, F2, F3) in Hz, roughly an adult voice's /a e i o u/ and a schwa.
_VOWELS = (
    (730.0, 1090.0, 2440.0),
    (530.0, 1840.0, 2480.0),
    (300.0, 2200.0, 2900.0),
    (570.0, 840.0, 2410.0),
    (320.0, 900.0, 2400.0),
    (500.0, 1500.0, 2500.0),
)
_FORMANT_BW = (90.0, 120.0, 160.0)
_FORMANT_GAIN = (1.0, 0.55, 0.25)


def _syllable(seconds: float, sr: int, rng: np.random.Generator, f0_start: float) -> np.ndarray:
    """One voiced syllable: a harmonic series on a moving pitch, shaped by moving formants."""
    n = max(8, int(round(seconds * sr)))
    t = np.arange(n) / sr
    # Pitch: a glide plus a slow wobble and a little jitter — a voice never holds still.
    glide = rng.uniform(-25.0, 15.0)
    f0 = f0_start + glide * (t / max(seconds, 1e-6))
    f0 = f0 + rng.uniform(3.0, 7.0) * np.sin(2.0 * np.pi * rng.uniform(4.0, 7.0) * t + rng.uniform(0, 6.28))
    f0 = f0 + np.convolve(rng.standard_normal(n), np.ones(64) / 64, mode="same") * 4.0
    phase = 2.0 * np.pi * np.cumsum(f0) / sr
    a = _VOWELS[rng.integers(len(_VOWELS))]
    b = _VOWELS[rng.integers(len(_VOWELS))]
    mix = np.clip(t / max(seconds, 1e-6), 0.0, 1.0)
    formants = [a[j] + (b[j] - a[j]) * mix for j in range(3)]
    out = np.zeros(n)
    nyquist_limit = min(3600.0, 0.45 * sr)
    k = 1
    while k * float(np.min(f0)) < nyquist_limit:
        hk = k * f0
        gain = np.zeros(n)
        for fj, bw, g in zip(formants, _FORMANT_BW, _FORMANT_GAIN, strict=True):
            gain += g / (1.0 + ((hk - fj) / bw) ** 2)
        # a radio voice channel's pass band: rolled off under ~300 Hz, cut at the top
        gain *= (hk < nyquist_limit) / np.sqrt(k) * (hk / 300.0) ** 4 / (1.0 + (hk / 300.0) ** 4)
        out += gain * np.sin(k * phase)
        k += 1
    return out * _envelope(n, sr, 0.03, 0.05)


def _fricative(seconds: float, sr: int, rng: np.random.Generator) -> np.ndarray:
    """A short unvoiced consonant: high-passed noise."""
    n = max(8, int(round(seconds * sr)))
    raw = rng.standard_normal(n + 1)
    return np.diff(raw) * 0.06 * _envelope(n, sr, 0.01, 0.02)


def voice(seconds: float, sr: int, rng: np.random.Generator, *, rms_db: float = -24.0) -> np.ndarray:
    """``seconds`` of speech-shaped sound: words of voiced syllables, consonants, pauses."""
    total = int(round(seconds * sr))
    out = np.zeros(total)
    pos = int(rng.uniform(0.0, 0.08) * sr)
    f0_base = rng.uniform(105.0, 150.0)
    while pos < total:
        # a word: one to three syllables, sometimes led by a consonant
        for _ in range(int(rng.integers(1, 4))):
            if rng.random() < 0.2:
                fric = _fricative(rng.uniform(0.04, 0.09), sr, rng)
                end = min(total, pos + fric.size)
                out[pos:end] += fric[: end - pos]
                pos = end
            syl = _syllable(rng.uniform(0.12, 0.30), sr, rng, f0_base + rng.uniform(-12.0, 12.0))
            end = min(total, pos + syl.size)
            out[pos:end] += syl[: end - pos]
            pos = end + int(rng.uniform(0.0, 0.04) * sr)
            if pos >= total:
                break
        # the gap between words, and now and then the longer one between phrases
        pos += int((rng.uniform(0.25, 0.45) if rng.random() < 0.2 else rng.uniform(0.05, 0.16)) * sr)
    live = out[np.abs(out) > 0]
    rms = float(np.sqrt(np.mean(live**2))) if live.size else 1.0
    return out * (10.0 ** (rms_db / 20.0) / max(rms, 1e-12))


def squelch_tail(seconds: float, sr: int, rng: np.random.Generator) -> np.ndarray:
    """A burst of channel noise: what a transmission that ends as it starts sounds like."""
    burst = rng.standard_normal(int(round(seconds * sr))) * 10.0 ** (-22.0 / 20.0)
    return burst * _envelope(burst.size, sr, 0.005, 0.03)


# --------------------------------------------------------------------------------------
# tones/*.f32 and speech/voiced.f32 (8 kHz)
# --------------------------------------------------------------------------------------


#: Generated a little off 878.1/467.9 Hz so that the detector's parabolic interpolation
#: reads them back as 878 and 468, which is what the tests assert.
WARBLE_HI = 880.4
WARBLE_LO = 467.2


def _warble(halves: int, half_s: float, *, amplitude: float, start_hi: bool = True) -> list[np.ndarray]:
    out = []
    for n in range(halves):
        hz = WARBLE_HI if (n % 2 == 0) == start_hi else WARBLE_LO
        out.append(tone(hz, half_s, TONE_SR, amplitude=amplitude))
    return out


def _excerpt(parts: list[np.ndarray], rng: np.random.Generator, floor_db: float = -52.0) -> np.ndarray:
    """Concatenate ``parts`` over a constant channel-noise floor."""
    signal = np.concatenate(parts)
    return signal + rng.standard_normal(signal.size) * 10.0 ** (floor_db / 20.0)


def tone_fixtures() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)
    sr = TONE_SR
    out: dict[str, np.ndarray] = {}

    # A clean warble, quiet throughout: twelve 0.248 s halves from 1.28 s.
    out["warble_quiet"] = _excerpt(
        [
            np.zeros(int(1.28 * sr)),
            *_warble(12, 0.248, amplitude=0.08),
            voice(2.2, sr, rng, rms_db=-26.0),
        ],
        rng,
    )

    # A warble whose last two halves fade and wobble (spread past MAX_FREQ_SPREAD_HZ):
    # only the pattern around them says they belong to it.
    halves = _warble(10, 0.2507, amplitude=0.3)
    for k, hz in ((10, WARBLE_HI), (11, WARBLE_LO + 0.3)):
        fade = np.linspace(1.0, 0.45, int(round(0.2507 * sr))) * (0.3 if k == 10 else 0.2)
        wobble = fm_tone(hz, 0.2507, sr, amplitude=1.0, depth_hz=11.0, rate_hz=7.98)
        halves.append(wobble * fade)
    out["warble_fading_tail"] = _excerpt(
        [np.zeros(int(1.09 * sr)), *halves, voice(2.0, sr, rng, rms_db=-24.0)], rng
    )

    # A warble seen only through its high halves: the 467.9 Hz halves sit under the level
    # floor, so the detector sees one frequency struck four times 0.2 s apart.
    parts: list[np.ndarray] = [np.zeros(int(0.70 * sr))]
    for n in range(4):
        parts.append(tone(WARBLE_HI, 0.5225, sr, amplitude=0.3))
        if n < 3:
            parts.append(tone(WARBLE_LO, 0.2, sr, amplitude=0.012))
    parts.append(voice(2.2, sr, rng, rms_db=-24.0))
    out["warble_hidden_halves"] = _excerpt(parts, rng)

    # Speech and nothing else, with one held open vowel near 492 Hz: tonal frame by frame,
    # but its pitch moves, so it must not come out as a tone.
    t = np.arange(int(0.38 * sr)) / sr
    f0 = 164.0 + 22.0 * t / t[-1] + 4.0 * np.sin(2.0 * np.pi * 5.5 * t)
    phase = 2.0 * np.pi * np.cumsum(f0) / sr
    held = (0.25 * np.sin(phase) + 0.6 * np.sin(3 * phase) + 0.2 * np.sin(2 * phase) + 0.12 * np.sin(5 * phase))
    held *= _envelope(held.size, sr, 0.03, 0.05) * 0.35
    out["vowel_only"] = _excerpt(
        [voice(1.4, sr, rng, rms_db=-22.0), held, voice(2.4, sr, rng, rms_db=-22.0)], rng
    )

    # One long tone that a vocoder has made wander: ±11 Hz around 990 Hz for 1.37 s, which
    # the detector measures as about 17 Hz of spread — far past MAX_FREQ_SPREAD_HZ.
    lone = fm_tone(990.0, 1.37, sr, amplitude=0.4, depth_hz=11.0, rate_hz=3.1)
    lone *= _envelope(lone.size, sr, 0.01, 0.01)
    out["single_wandering"] = _excerpt(
        [np.zeros(int(0.19 * sr)), lone, np.zeros(int(0.25 * sr)), voice(2.4, sr, rng, rms_db=-26.0)],
        rng,
    )

    # A warble from 1.28 s to 4.256 s whose last four halves wobble past the tight limit
    # and run straight on from one another — consecutive frames, so each pair of runs
    # overlaps by one hop and only an overlap-aware absorb carries the span to the end.
    halves = _warble(8, 0.248, amplitude=0.3)
    # Each wobbling half's centre is nudged so that the median the detector reads off it
    # still rounds to 878/468: a wandering tone's median depends on where the frame grid
    # falls on its wander, and these halves sit half a hop apart on that grid.
    for hz, depth in ((WARBLE_HI + 1.0, 10.0), (WARBLE_LO - 0.75, 12.0), (WARBLE_HI + 1.0, 10.0), (WARBLE_LO + 0.5, 12.0)):
        halves.append(fm_tone(hz, 0.248, sr, amplitude=0.3, depth_hz=depth, rate_hz=8.06))
    out["warble_overlapping_runs"] = _excerpt(
        [np.zeros(int(1.28 * sr)), *halves, voice(2.0, sr, rng, rms_db=-24.0)], rng
    )
    return {name: signal.astype(np.float32) for name, signal in out.items()}


def speech_fixture() -> np.ndarray:
    rng = np.random.default_rng(SEED + 1)
    signal = voice(4.0, TONE_SR, rng, rms_db=-24.0)
    signal = signal + rng.standard_normal(signal.size) * 10.0 ** (-70.0 / 20.0)
    return signal.astype(np.float32)


# --------------------------------------------------------------------------------------
# audio/**.mp3 — sdrtrunk-shaped recordings
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recording:
    """One sdrtrunk recording: what its filename, ID3 frames and audio say."""

    stamp: str  # file write instant, "YYYYmmdd_HHMMSS"
    millis: int  # the COMM date's milliseconds
    folder: str  # sdrtrunk's per-alias directory
    to: str  # the filename's TO field, verbatim
    from_: str | None  # the filename's FROM field, verbatim (underscores)
    tit2: str  # the ID3 title, verbatim
    tpe1: str | None  # the ID3 artist: the radio, dotted
    channel: str
    frequency_hz: int
    duration_s: float
    why: str
    call_type: str
    tgid: int
    talkgroup_ids: tuple[int, ...]
    version: int | None = None
    content: str = "voice"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def day(self) -> str:
        return self.stamp[:8]

    @property
    def name(self) -> str:
        system = SYSTEM.replace(" ", "-")
        name = f"{self.stamp}_{system}_{SITE}_{CHANNEL_NAME}__TO_{self.to}"
        if self.from_:
            name += f"_FROM_{self.from_}"
        if self.version:
            name += f"_V{self.version}"
        return name + ".mp3"

    @property
    def relative(self) -> str:
        return f"{self.day}/{self.folder}/{self.name}"


def _fq(home: int, radio: int) -> tuple[str, str]:
    """A fully-qualified radio as the filename and as ID3 spell it."""
    return f"{WACN}_{home}_{radio}", f"{WACN}.{home}.{radio}"


RECORDINGS: tuple[Recording, ...] = (
    Recording(
        stamp="20260828_055219", millis=894, folder="EXAMPLE EMS OPS", to="1421",
        from_=_fq(57, 9831120)[0], tit2='1421"EXAMPLE EMS OPS"', tpe1=_fq(57, 9831120)[1],
        channel="1-90", frequency_hz=852137500, duration_s=2.377,
        why="plain call, fully-qualified radio (WACN.sys.id)", call_type="group",
        tgid=1421, talkgroup_ids=(1421,),
    ),
    Recording(
        stamp="20260829_015213", millis=979, folder="EXAMPLE TRANSIT", to="1753",
        from_="9250041", tit2='1753"EXAMPLE TRANSIT"', tpe1="9250041",
        channel="1-718", frequency_hz=859987500, duration_s=1.672,
        why="plain call, bare local radio id", call_type="group", tgid=1753, talkgroup_ids=(1753,),
    ),
    Recording(
        stamp="20260829_030253", millis=689, folder="EXAMPLE EVENT 1", to="1701", from_=None,
        tit2='1701"EXAMPLE EVENT 1"', tpe1=None, channel="1-90", frequency_hz=852137500,
        duration_s=0.601, why="no FROM: the recorder never resolved the radio",
        call_type="group", tgid=1701, talkgroup_ids=(1701,),
    ),
    Recording(
        stamp="20260829_171137", millis=460, folder="EXAMPLE EVENT 1", to="1701", from_=None,
        tit2='1701"EXAMPLE EVENT 1"', tpe1=None, channel="1-718", frequency_hz=859987500,
        duration_s=0.941, why="no FROM, second example", call_type="group", tgid=1701,
        talkgroup_ids=(1701,),
    ),
    Recording(
        stamp="20260829_135155", millis=101, folder="EXAMPLE MED 2", to="1634", from_="9200114",
        tit2='1634"EXAMPLE MED 2"', tpe1="9200114", channel="1-62", frequency_hz=851787500,
        duration_s=16.614, why="the call the _V2 file below collides with", call_type="group",
        tgid=1634, talkgroup_ids=(1634,),
    ),
    Recording(
        stamp="20260829_135155", millis=193, folder="EXAMPLE MED 2", to="1634", from_="9200114",
        tit2='1634"EXAMPLE MED 2"', tpe1="9200114", channel="1-718", frequency_hz=859987500,
        duration_s=0.784, why="_V2 collision suffix", call_type="group", tgid=1634,
        talkgroup_ids=(1634,), version=2,
    ),
    Recording(
        stamp="20260828_082515", millis=185, folder="EXAMPLE EVENT 1", to="1701", from_="9200007",
        tit2='1701"EXAMPLE EVENT 1"', tpe1="9200007", channel="1-168", frequency_hz=853112500,
        duration_s=0.601, why="_V2 collision suffix, second example", call_type="group",
        tgid=1701, talkgroup_ids=(1701,), version=2,
    ),
    Recording(
        stamp="20260829_193221", millis=720, folder="PATCH 1899", to="P1899-[1561--1310]",
        from_=_fq(7, 900004)[0],
        tit2='P:1899 [1561, 1310]"PATCH 1899","EXAMPLE INTEROP 1","EXAMPLE SHERIFF"',
        tpe1=_fq(7, 900004)[1], channel="1-258", frequency_hz=854237500, duration_s=27.768,
        why="patch with 2 members, in a PATCH folder", call_type="patch", tgid=1561,
        talkgroup_ids=(1561, 1310),
    ),
    Recording(
        stamp="20260829_165914", millis=435, folder="PATCH 1157", to="P1157-[1812--1561--1811]",
        from_=_fq(57, 9700005)[0],
        tit2='P:1157 [1812, 1561, 1811]"PATCH 1157","EXAMPLE LAW 3","EXAMPLE INTEROP 1","EXAMPLE LAW 4"',
        tpe1=_fq(57, 9700005)[1], channel="1-638", frequency_hz=858987500, duration_s=26.175,
        why="patch with 3 members", call_type="patch", tgid=1812, talkgroup_ids=(1812, 1561, 1811),
    ),
    Recording(
        stamp="20260829_085845", millis=587, folder="PATCH 1038", to="1038",
        from_=_fq(92, 9299201)[0],
        tit2=(
            'P:1038 [1551, 1561, 1501, 1282]"PATCH 1038","EXAMPLE INTEROP 2",'
            '"EXAMPLE INTEROP 1","EXAMPLE LAW 5","EXAMPLE LAW 5 PATCH"'
        ),
        tpe1=_fq(92, 9299201)[1], channel="1-62", frequency_hz=851787500, duration_s=30.642,
        why="patch with 4 members whose filename TO is the bare patch id (ID3 is authoritative)",
        call_type="patch", tgid=1551, talkgroup_ids=(1551, 1561, 1501, 1282),
    ),
    Recording(
        stamp="20260828_232527", millis=758, folder="EXAMPLE INTEROP 1", to="P1097-[1561--1612]",
        from_="9200006", tit2='P:1097 [1561, 1612]"EXAMPLE INTEROP 1","EXAMPLE LAW 2"',
        tpe1="9200006", channel="1-62", frequency_hz=851787500, duration_s=18.233,
        why="patch whose TIT2 prints no patch name, only the members'", call_type="patch",
        tgid=1561, talkgroup_ids=(1561, 1612),
    ),
    Recording(
        stamp="20260829_141703", millis=144, folder="EXAMPLE EVENT 1", to="1701", from_="9200006",
        tit2='1701"EXAMPLE EVENT 1"', tpe1="9200006", channel="1-274", frequency_hz=854437500,
        duration_s=0.235, why="shortest call in the set (0.235 s squelch tail)", call_type="group",
        tgid=1701, talkgroup_ids=(1701,), content="squelch",
    ),
    Recording(
        stamp="20260829_200945", millis=788, folder="EXAMPLE LAW 1", to="1601", from_="9200102",
        tit2='1601"EXAMPLE LAW 1"', tpe1="9200102", channel="1-456", frequency_hz=856712500,
        duration_s=44.513, why="longest call in the set (44.5 s)", call_type="group", tgid=1601,
        talkgroup_ids=(1601,),
    ),
)


def _encode_mp3(samples: np.ndarray, path: Path) -> None:
    """32 kbps CBR, no Xing/LAME header and no tags, the way sdrtrunk's encoder writes."""
    raw = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-f", "s16le", "-ar", str(MP3_SR), "-ac", "1", "-i", "-",
            "-c:a", "libmp3lame", "-b:a", "32k", "-write_xing", "0",
            "-id3v2_version", "0", "-map_metadata", "-1",
            "-fflags", "+bitexact", "-flags:a", "+bitexact",
            "-f", "mp3", str(path),
        ],
        input=raw, check=True, capture_output=True,
    )


def _mp3_length(path: Path) -> float:
    from mutagen.mp3 import MP3

    return float(MP3(str(path)).info.length)


def _tag(path: Path, rec: Recording) -> None:
    from mutagen.id3 import COMM, ID3, TALB, TCOM, TCON, TDRC, TIT1, TIT2, TPE1

    stamp = datetime.strptime(rec.stamp, "%Y%m%d_%H%M%S")
    comment = (
        f"Date:{stamp:%Y-%m-%d %H:%M:%S}.{rec.millis:03d};System:{SYSTEM};Site:{SITE};"
        f"Name:{CHANNEL_NAME};Decoder:{DECODER};Channel:{rec.channel};"
        f"Frequency:{rec.frequency_hz};"
    )
    tags = ID3()
    tags.add(COMM(encoding=0, lang="eng", desc="", text=[comment]))
    tags.add(TALB(encoding=0, text=[CHANNEL_NAME]))
    tags.add(TCOM(encoding=0, text=[RECORDER]))
    tags.add(TCON(encoding=0, text=["Scanner Audio"]))
    tags.add(TIT1(encoding=0, text=[SYSTEM]))
    tags.add(TIT2(encoding=0, text=[rec.tit2]))
    if rec.tpe1:
        tags.add(TPE1(encoding=0, text=[rec.tpe1]))
    tags.add(TDRC(encoding=0, text=[stamp.strftime("%Y")]))
    tags.save(str(path), v2_version=4, padding=lambda info: 0)


def _recording_samples(rec: Recording, n: int, index: int) -> np.ndarray:
    rng = np.random.default_rng(SEED + 100 + index)
    if rec.content == "squelch":
        body = squelch_tail(n / MP3_SR, MP3_SR, rng)
    else:
        # about the level of a dispatch on the air: loud enough that the audio chain's
        # relative gate sits at its cap (GATE_MAX_DB) rather than under it
        body = voice(n / MP3_SR, MP3_SR, rng, rms_db=-16.0)
        body = body + rng.standard_normal(body.size) * 10.0 ** (-68.0 / 20.0)
    body = body[:n]
    if body.size < n:
        body = np.pad(body, (0, n - body.size))
    return body


def write_recording(rec: Recording, root: Path, index: int) -> Path:
    """Encode ``rec`` under ``root`` so that mutagen reports its duration to within 5 ms."""
    path = root / rec.relative
    path.parent.mkdir(parents=True, exist_ok=True)
    # Without a Xing header the length is read from the byte count, so it moves in whole
    # frames; aim for the frame count and correct by the frames the encoder adds.
    frames = max(1, round(rec.duration_s * MP3_SR / MP3_FRAME))
    n = frames * MP3_FRAME
    for _ in range(6):
        _encode_mp3(_recording_samples(rec, n, index), path)
        error = rec.duration_s - _mp3_length(path)
        if abs(error) <= 0.0131:  # half a frame
            break
        n = max(MP3_FRAME, n + int(round(error * MP3_SR / MP3_FRAME)) * MP3_FRAME)
    _tag(path, rec)
    return path


# --------------------------------------------------------------------------------------
# manifest, waveform cache
# --------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root: Path) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["path", "why", "call_type", "tgid", "talkgroup_ids", "radio_raw", "duration_s", "sha256"])
    for rec in RECORDINGS:
        path = root / "audio" / rec.relative
        writer.writerow(
            [
                rec.relative, rec.why, rec.call_type, rec.tgid,
                " ".join(str(t) for t in rec.talkgroup_ids), rec.tpe1 or "",
                f"{_mp3_length(path):.3f}", sha256(path),
            ]
        )
    return buffer.getvalue()


def waveform_cache(path: Path, call_id: int = 1, buckets: int = 200) -> str:
    """The cache entry ``app.waveform.waveform`` writes for ``path`` (same decode, same maths)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-nostdin", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True, check=True,
    ).stdout
    samples = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2").astype(np.int64)
    size = max(1, samples.size // buckets)
    peaks = []
    for index in range(buckets):
        chunk = samples[index * size : (index + 1) * size]
        peaks.append(float(np.max(np.abs(chunk))) if chunk.size else 0.0)
    ceiling = max(peaks) or 1.0
    payload = {
        "call_id": call_id,
        "duration_s": round(samples.size / 8000, 3),
        "peaks": [round(value / ceiling, 4) for value in peaks],
        "buckets": buckets,
        "source": f"{path.name}:{path.stat().st_size}",
    }
    return json.dumps(payload)


# --------------------------------------------------------------------------------------
# bcfy/*.csv — invented feed-owner dashboard exports
# --------------------------------------------------------------------------------------

#: The hourly export's window: newest row first in the file, 179 rows.
HOURLY_FIRST = datetime(2026, 8, 28, 6)
HOURLY_LAST = datetime(2026, 9, 4, 16)
#: The outage: down 03:57-04:19 UTC on the last day, so 04:00 lost five of its fifteen
#: checks and 03:00 exactly one.
OUTAGE_HOUR = datetime(2026, 9, 4, 4)
#: The daily export's window, 179 rows.
DAILY_FIRST = date(2026, 3, 9)
DAILY_LAST = date(2026, 9, 3)
#: Three invented incident days, far above every ordinary one.
INCIDENT_DAYS = {date(2026, 5, 17): 412, date(2026, 6, 3): 171, date(2026, 7, 11): 118}


def hourly_csv() -> str:
    rng = np.random.default_rng(SEED + 7)
    rows = []
    hour = HOURLY_FIRST
    while hour <= HOURLY_LAST:
        # a listener curve that peaks in the evening, local time five hours behind UTC
        local = (hour.hour - 5) % 24
        base = 7.0 + 4.0 * np.cos(2.0 * np.pi * (local - 19) / 24.0)
        listeners = max(0, int(round(base + rng.normal(0.0, 1.5))))
        checks = 15
        reports = 15
        if hour == OUTAGE_HOUR:
            reports = 10
        elif hour == OUTAGE_HOUR - timedelta(hours=1):
            reports = 14
        rows.append((hour, checks, reports, listeners))
        hour += timedelta(hours=1)
    # The newest hour is still in progress when the export is taken: fewer checks so far.
    last = rows[-1]
    rows[-1] = (last[0], 12, 12, 7)
    lines = ["Hour,Online Checks,Online Reports,Max Listeners"]
    for hour, checks, reports, listeners in reversed(rows):
        lines.append(f"{hour:%Y-%m-%d %H:%M:%S},{checks},{reports},{listeners}")
    return "\n".join(lines) + "\n"


def uptime_csv() -> str:
    rng = np.random.default_rng(SEED + 8)
    rows = []
    day = DAILY_FIRST
    while day <= DAILY_LAST:
        listeners = int(round(26 + 6 * np.sin(day.toordinal() / 9.0) + rng.normal(0.0, 3.0)))
        listeners = INCIDENT_DAYS.get(day, min(max(listeners, 12), 45))
        uptime = 100.0 if rng.random() < 0.7 else round(float(rng.uniform(97.5, 99.95)), 2)
        rows.append((day, uptime, listeners))
        day += timedelta(days=1)
    rows[0] = (DAILY_FIRST, 100.0, 24)
    rows[-1] = (DAILY_LAST, 99.72, 31)
    lines = ["Day,Uptime %,Max Listeners"]
    for day, uptime, listeners in reversed(rows):
        lines.append(f"{day:%Y-%m-%d} 00:00:00,{uptime:.2f},{listeners}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------


def generate(root: Path) -> list[Path]:
    """Write every generated fixture under ``root``; return the paths written."""
    written: list[Path] = []
    for name, signal in tone_fixtures().items():
        path = root / "tones" / f"{name}.f32"
        path.parent.mkdir(parents=True, exist_ok=True)
        signal.astype("<f4").tofile(path)
        written.append(path)
    path = root / "speech" / "voiced.f32"
    path.parent.mkdir(parents=True, exist_ok=True)
    speech_fixture().astype("<f4").tofile(path)
    written.append(path)

    for index, rec in enumerate(RECORDINGS):
        written.append(write_recording(rec, root / "audio", index))
    path = root / "manifest.csv"
    path.write_text(manifest(root))
    written.append(path)

    path = root / "cache" / "waveform" / "1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(waveform_cache(root / "audio" / RECORDINGS[0].relative))
    written.append(path)

    for name, text in (("owner_hourly_sample.csv", hourly_csv()), ("owner_uptime_sample.csv", uptime_csv())):
        path = root / "bcfy" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="generate into a temp dir and diff")
    args = parser.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is not on PATH", file=sys.stderr)
        return 2
    if not args.check:
        shutil.rmtree(HERE / "audio", ignore_errors=True)
        for path in generate(HERE):
            print(path.relative_to(HERE))
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        written = generate(Path(tmp))
        changed = [
            p.relative_to(tmp)
            for p in written
            if not (HERE / p.relative_to(tmp)).is_file()
            or not filecmp.cmp(p, HERE / p.relative_to(tmp), shallow=False)
        ]
    for path in changed:
        print(f"differs: {path}")
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
