"""Speech-volume normalization for the STT audio-prep path (ARCHITECTURE §5.2).

Scanner traffic is wildly uneven in level: a paging tone or an alert beep pins the meter
while the dispatcher two seconds later is barely above the noise floor, and a soft talker on
a distant repeater can sit 20 dB under the loud units on the same talkgroup.  Whisper's
feature extractor is not loudness-invariant — a clip that never leaves the bottom of the
range decodes worse than the same words at a healthy level — so **every clip is loudness-
normalized before it reaches an engine.**

This is a *preprocessing* step in the audio-prep every backend already runs (they all
transcode to 16 kHz mono before the request, or decode the file to samples): the
normalization is an ffmpeg ``-af`` filter inserted into that same pass.  It therefore never
touches the stored recording, its ``sha256`` or the dedupe — only the bytes handed to the
decoder.

The filter is chosen for speech, best-first against what the installed ffmpeg actually
carries (``ffmpeg -hide_banner -filters``, probed once and cached):

* **``speechnorm``** — purpose-built to *raise quiet speech* toward a target while leaving
  already-loud passages alone; its ``p`` peak (0.95 by default) is the anti-clipping limiter,
  so no separate one is chained.  This is the default (``speechnorm=e=12.5:r=0.0001:l=1``):
  ``e`` caps the expansion so a near-silent squelch tail is not amplified into a roar, ``r``
  keeps the level from pumping between words, ``l`` links channels (a no-op on our mono).
* **``dynaudnorm``** — evens loud-vs-soft *within* a clip; a gentle ``alimiter=limit=0.95``
  is chained because, unlike speechnorm, it has no built-in peak guard.
* **``loudnorm``** — EBU R128 to a consistent integrated loudness; ``TP=-1.5`` is its true-peak
  ceiling.

If ffmpeg fails on the filter (a build without it, a bad override), the transcode is retried
once **without** the filter and the job proceeds un-normalized rather than being dropped;
the failure is logged once per distinct filter string.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
import tempfile
from functools import lru_cache
from pathlib import Path

from app.config import Settings

from .base import TranscriptionError

log = logging.getLogger(__name__)

__all__ = [
    "af_flags",
    "available_filters",
    "default_filter",
    "ffmpeg_audio_filter",
    "note_filter_failure",
    "transcode_to_wav",
]

SAMPLE_RATE = 16_000
"""Whisper's feature extractor is fixed at 16 kHz mono."""

#: Candidate default chains, best-first for scanner voice.  The first whose lead filter the
#: installed ffmpeg carries wins.
_SPEECHNORM = "speechnorm=e=12.5:r=0.0001:l=1"
_DYNAUDNORM = "dynaudnorm=f=150:g=15:p=0.9:m=10,alimiter=limit=0.95"
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"

#: A filter row from ``ffmpeg -filters``: the flag chars, the name, then the ``A->A`` arrows.
#: ffmpeg 7 prints three flag columns (``TSC``); ffmpeg 8 dropped the command column and
#: prints two (``TS``).  Matching exactly three made every row miss on ffmpeg 8, so the probe
#: came back empty and normalization silently switched itself off.
_FILTER_ROW = re.compile(r"^\s*[A-Z.]{2,3}\s+(\S+)\s+\S+->\S+")

#: Distinct filter strings already logged as failing, so the fallback logs once, not per clip.
_warned: set[str] = set()


@lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    """The filter names this ffmpeg build carries, per ``ffmpeg -hide_banner -filters``.

    Empty when ffmpeg is absent or the probe fails — callers then fall back to no filter.
    Probed once and cached for the life of the process.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        return frozenset()
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = {m.group(1) for line in out.stdout.splitlines() if (m := _FILTER_ROW.match(line))}
    return frozenset(names)


@lru_cache(maxsize=1)
def default_filter() -> str:
    """The built-in default ``-af`` chain for the installed ffmpeg, or ``""`` if it has none."""
    avail = available_filters()
    if "speechnorm" in avail:
        return _SPEECHNORM
    if "dynaudnorm" in avail:
        return _DYNAUDNORM
    if "loudnorm" in avail:
        return _LOUDNORM
    return ""


def ffmpeg_audio_filter(settings: Settings) -> str | None:
    """The ffmpeg ``-af`` string to apply before transcription, or ``None`` when off.

    ``STT_NORMALIZE`` is the master switch; ``STT_NORMALIZE_FILTER`` overrides the chain and,
    when blank, the built-in default for the detected ffmpeg is used.  ``None`` (switch off,
    or nothing available/configured) means "behave exactly as before".
    """
    if not getattr(settings, "stt_normalize", False):
        return None
    configured = (getattr(settings, "stt_normalize_filter", "") or "").strip()
    chosen = configured or default_filter()
    return chosen or None


def af_flags(filter_str: str | None) -> list[str]:
    """``["-af", filter]`` when a filter is set, else ``[]`` — spliced into an ffmpeg argv."""
    return ["-af", filter_str] if filter_str else []


def note_filter_failure(filter_str: str, detail: str) -> None:
    if filter_str not in _warned:
        _warned.add(filter_str)
        log.warning(
            "stt: normalize filter %r failed (%s); falling back to un-normalized transcode",
            filter_str, detail,
        )


def _stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]


def transcode_to_wav(
    path: Path, *, sample_rate: int = SAMPLE_RATE, af: str | None = None, timeout_s: float = 120.0
) -> bytes:
    """Decode any ffmpeg-readable file to mono 16-bit wav bytes, optionally normalized.

    ``af`` is the ``-af`` filter from :func:`ffmpeg_audio_filter`; when it is set the filter is
    applied in this same decode pass so the request carries loudness-normalized audio.  If
    ffmpeg fails *with* the filter the decode is retried once without it (logged once) rather
    than failing the job; a genuinely unreadable file still raises :class:`TranscriptionError`.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", str(path), *af_flags(af),
            "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", "-f", "wav", tmp.name,
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
        except FileNotFoundError as exc:
            raise TranscriptionError("ffmpeg is not installed; it is required to decode audio") from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionError(f"ffmpeg timed out after {timeout_s:.0f}s on {path.name}") from exc
        if proc.returncode != 0:
            if af:  # the filter is the likely culprit — try again clean before giving up
                note_filter_failure(af, _stderr(proc))
                return transcode_to_wav(path, sample_rate=sample_rate, af=None, timeout_s=timeout_s)
            raise TranscriptionError(f"ffmpeg failed on {path.name}: {_stderr(proc)}")
        return Path(tmp.name).read_bytes()
