"""Waveform peaks for the player (``GET /api/calls/{id}/waveform``).

The MP3s are 32 kbps mono, 2.2 s at the median — decoding one is a few milliseconds — but
the Calls page asks for a waveform every time a row is opened, so the peaks are computed
once and cached as a small JSON file next to the archive.

Decoding goes through ``ffmpeg`` rather than a Python MP3 decoder: it is already a
dependency of any box that stores audio, it handles the truncated/zero-length files a
recorder occasionally leaves behind without raising, and ``-f s16le -ac 1 -ar 8000`` gives
exactly the flat little-endian stream this needs with no resampling library in Python.
"""

from __future__ import annotations

import array
import json
import logging
import subprocess
from pathlib import Path

from app.config import Settings, get_settings

log = logging.getLogger("app.waveform")

#: 8 kHz mono is far more than 200 buckets need and keeps the decode cheap.
SAMPLE_RATE = 8000
FFMPEG_TIMEOUT_S = 30.0


class WaveformError(RuntimeError):
    """ffmpeg is missing, or the file could not be decoded."""


def cache_dir(settings: Settings | None = None) -> Path:
    """``WAVEFORM_CACHE_DIR`` when set, else ``<AUDIO_ROOT>/../cache/waveform``."""
    settings = settings or get_settings()
    if settings.waveform_cache_dir is not None:
        return Path(settings.waveform_cache_dir)
    return Path(settings.audio_root).parent / "cache" / "waveform"


def cache_path(call_id: int, settings: Settings | None = None) -> Path:
    return cache_dir(settings) / f"{call_id}.json"


def decode_pcm(path: str | Path) -> array.array:
    """Signed 16-bit mono PCM at :data:`SAMPLE_RATE`, decoded by ffmpeg."""
    command = [
        "ffmpeg",
        "-v",
        "quiet",
        "-nostdin",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:  # ffmpeg not installed
        raise WaveformError("ffmpeg is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise WaveformError(f"ffmpeg timed out decoding {Path(path).name}") from exc
    raw = completed.stdout
    if not raw:
        raise WaveformError(f"ffmpeg produced no audio for {Path(path).name}")
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    return samples


def peaks_from_samples(samples: array.array, buckets: int) -> list[float]:
    """Per-bucket peak amplitude, normalized so the loudest bucket is 1.0.

    Normalizing per call rather than to full scale is deliberate: these recordings are
    quiet and wildly uneven in level, and an absolute scale would draw most of them as a
    flat line.  The player only needs the *shape*.
    """
    if buckets <= 0 or not samples:
        return []
    size = max(1, len(samples) // buckets)
    out: list[float] = []
    for index in range(buckets):
        chunk = samples[index * size : (index + 1) * size]
        out.append(float(max(abs(int(s)) for s in chunk)) if chunk else 0.0)
    ceiling = max(out) or 1.0
    return [round(value / ceiling, 4) for value in out]


def _source_key(path: str | Path) -> str:
    """A cheap identity for the decoded file: ``<name>:<size>``, or ``""`` if it is gone.

    Name *and* size rather than mtime: the name is what distinguishes a recording from its
    ``.proc.mp3`` twin, which is the case this exists for, and the size catches a reprocess
    that rewrote the same name.  Deliberately not the mtime — that would make the cache file
    differ between two machines holding identical audio, which is a lot of churn to buy an
    edge case (a reprocess landing on the byte-identical length) that ``refresh=1`` fixes.
    """
    source = Path(path)
    try:
        return f"{source.name}:{source.stat().st_size}"
    except OSError:
        return ""


def compute(path: str | Path, buckets: int) -> list[float]:
    return peaks_from_samples(decode_pcm(path), buckets)


def waveform(
    call_id: int,
    path: str | Path,
    *,
    buckets: int | None = None,
    settings: Settings | None = None,
    refresh: bool = False,
) -> dict[str, object]:
    """Cached peaks for one call.  Returns ``{call_id, duration_s, peaks, buckets}``.

    A cache file whose bucket count no longer matches the setting is recomputed, so
    changing ``WAVEFORM_BUCKETS`` does not need a manual purge.  So is one computed from a
    *different file*: the cache is keyed by call id, but the file behind a call changes the
    moment the audio-processing pass (§5.5) writes its copy — and an envelope drawn from
    the untrimmed original under a player playing the trimmed one is worse than no envelope
    at all.  ``source`` is the name and size of whatever was decoded, which is cheap to
    take and changes on exactly the events that matter.
    """
    settings = settings or get_settings()
    buckets = buckets or settings.waveform_buckets
    target = cache_path(call_id, settings)
    source = _source_key(path)
    if not refresh and target.is_file():
        try:
            cached = json.loads(target.read_text())
            if len(cached.get("peaks", ())) == buckets and cached.get("source") == source:
                return cached
        except (OSError, ValueError):  # a corrupt cache file is simply rebuilt
            log.warning("waveform: rebuilding unreadable cache %s", target)

    samples = decode_pcm(path)
    payload = {
        "call_id": call_id,
        "duration_s": round(len(samples) / SAMPLE_RATE, 3),
        "peaks": peaks_from_samples(samples, buckets),
        "buckets": buckets,
        "source": source,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(target)
    except OSError as exc:  # a read-only cache directory must not break playback
        log.warning("waveform: could not cache %s: %s", target, exc)
    return payload


__all__ = ["WaveformError", "cache_dir", "cache_path", "compute", "decode_pcm", "waveform"]
