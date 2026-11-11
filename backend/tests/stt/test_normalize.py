"""Unit and behavioral tests for speech-volume normalization (app/stt/normalize.py).

The unit tests are hermetic.  The behavioral ones need a real ffmpeg (``which ffmpeg``) and
assert the filter actually *raises* a quiet clip's level without clipping a loud one.
"""

from __future__ import annotations

import array
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from app.config import Settings
from app.stt import normalize as n

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


# --------------------------------------------------------------------------- ffmpeg_audio_filter


def test_filter_is_none_when_the_switch_is_off():
    assert n.ffmpeg_audio_filter(Settings(stt_normalize=False)) is None


def test_filter_is_the_builtin_default_when_on_and_unconfigured():
    chosen = n.ffmpeg_audio_filter(Settings(stt_normalize=True, stt_normalize_filter=""))
    # Whatever the installed ffmpeg supports; on any modern build this is speechnorm.
    assert chosen == n.default_filter()
    if HAS_FFMPEG:
        assert chosen and ("speechnorm" in chosen or "dynaudnorm" in chosen or "loudnorm" in chosen)


def test_an_explicit_filter_is_used_verbatim():
    s = Settings(stt_normalize=True, stt_normalize_filter="loudnorm=I=-16:TP=-1.5:LRA=11")
    assert n.ffmpeg_audio_filter(s) == "loudnorm=I=-16:TP=-1.5:LRA=11"


def test_af_flags():
    assert n.af_flags(None) == []
    assert n.af_flags("") == []
    assert n.af_flags("speechnorm") == ["-af", "speechnorm"]


@needs_ffmpeg
def test_default_filter_prefers_speechnorm():
    # This build (and the deployed image) carries speechnorm.
    assert "speechnorm" in n.available_filters()
    assert n.default_filter().startswith("speechnorm")


# --------------------------------------------------------------------------- transcode argv


def test_transcode_argv_contains_af_when_a_filter_is_set(monkeypatch, tmp_path):
    """The filter must reach ffmpeg's argv as ``-af <filter>`` — and be absent otherwise."""
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr(n.subprocess, "run", spy)

    src = _sine_wav(tmp_path / "src.wav", seconds=1.0, amplitude=0.3)

    n.transcode_to_wav(src, af="speechnorm=e=12.5:r=0.0001:l=1")
    assert seen and "-af" in seen[-1]
    assert seen[-1][seen[-1].index("-af") + 1] == "speechnorm=e=12.5:r=0.0001:l=1"

    seen.clear()
    n.transcode_to_wav(src, af=None)
    assert seen and "-af" not in seen[-1]


@needs_ffmpeg
def test_a_bad_filter_falls_back_to_an_unnormalized_transcode(tmp_path):
    """A filter ffmpeg cannot run must not drop the job — the clip still decodes."""
    src = _sine_wav(tmp_path / "src.wav", seconds=1.0, amplitude=0.3)
    out = n.transcode_to_wav(src, af="not_a_real_filter=nope")
    assert out[:4] == b"RIFF" and b"WAVE" in out[:16]


# --------------------------------------------------------------------------- behavioral


@needs_ffmpeg
def test_a_quiet_clip_is_raised_and_a_loud_one_is_not_clipped(tmp_path):
    """The whole point: a soft talker comes out louder, a loud tone stays under full scale."""
    quiet = _sine_wav(tmp_path / "quiet.wav", seconds=2.0, amplitude=0.03)
    loud = _sine_wav(tmp_path / "loud.wav", seconds=2.0, amplitude=0.95)

    af = n.default_filter()
    before = _rms(quiet)
    after = _rms_bytes(n.transcode_to_wav(quiet, af=af))
    assert after > before * 2, f"quiet clip not meaningfully raised: {before:.5f} -> {after:.5f}"

    loud_peak = _peak_bytes(n.transcode_to_wav(loud, af=af))
    assert loud_peak <= 1.0, f"loud clip clipped past full scale: peak {loud_peak:.4f}"


# --------------------------------------------------------------------------- helpers


def _sine_wav(path: Path, *, seconds: float, amplitude: float, rate: int = 16_000) -> Path:
    n_samples = int(seconds * rate)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for i in range(n_samples):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<h", value)
        out.writeframes(bytes(frames))
    return path


def _samples_from_bytes(wav_bytes: bytes) -> array.array:
    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def _rms(path: Path) -> float:
    return _rms_bytes(path.read_bytes())


def _rms_bytes(wav_bytes: bytes) -> float:
    samples = _samples_from_bytes(wav_bytes)
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


def _peak_bytes(wav_bytes: bytes) -> float:
    samples = _samples_from_bytes(wav_bytes)
    return (max(abs(s) for s in samples) / 32768.0) if samples else 0.0
