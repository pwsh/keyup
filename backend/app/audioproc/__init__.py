"""Stored-audio processing: measure a recording, and write a listenable copy of it (§5.5).

Calls arrive as the recorder wrote them, and on a live P25 system that is not a consistent
product.  Measured across six hours of this archive: the mean level spans 20 dB between
the tenth percentile (-42 dBFS) and the loudest call (-21), a third of calls carry more
than half a second of dead air before anybody speaks, some "calls" are 0.5 s of nothing at
all, and a fire page opens with two tones that are the loudest thing in the day.  Playing
that back raw means riding the volume knob.

So every call gets a **processed copy** — ``<stem>.proc.mp3``, beside the original, at the
original's format and bitrate — trimmed, denoised, gated, loudness-normalized and
peak-limited, with its page tones turned down.  The original is never modified: it is what
``calls.file_path`` names, what ``calls.sha256`` hashes and what the dedupe compares, and
``?raw=1`` on the audio route still serves it.

Three modules, split by what they need:

:mod:`app.audioproc.tones`
    pure numpy — find the sustained tones and say whether the sequence is a page.
:mod:`app.audioproc.chain`
    pure decisions — measurements and settings in, an ffmpeg ``-af`` string out.
:mod:`app.audioproc.process`
    the half that touches the disk: decode, measure, run ffmpeg, rename into place.

:mod:`app.audioproc.jobs` is the queue around them (``audio_jobs``), and
:mod:`app.audioproc.worker` is the thread the worker process runs.
"""

from __future__ import annotations

from app.audioproc.chain import (
    LOUDNORM_MIN_S,
    NOISE_REDUCERS,
    PIPELINE_VERSION,
    TONE_HANDLINGS,
    AudioConfig,
    LoudnormStats,
    Measurement,
    filter_chain,
    gain_for,
)
from app.audioproc.process import (
    PROCESSED_SUFFIX,
    AudioProcessError,
    ProcessResult,
    SourceFormat,
    decode_pcm,
    measure,
    probe,
    process_file,
    processed_path_for,
)
from app.audioproc.tones import ToneReport, ToneSegment, detect_tones

__all__ = [
    "LOUDNORM_MIN_S",
    "NOISE_REDUCERS",
    "PIPELINE_VERSION",
    "PROCESSED_SUFFIX",
    "TONE_HANDLINGS",
    "AudioConfig",
    "AudioProcessError",
    "LoudnormStats",
    "Measurement",
    "ProcessResult",
    "SourceFormat",
    "ToneReport",
    "ToneSegment",
    "decode_pcm",
    "detect_tones",
    "filter_chain",
    "gain_for",
    "measure",
    "probe",
    "process_file",
    "processed_path_for",
]
