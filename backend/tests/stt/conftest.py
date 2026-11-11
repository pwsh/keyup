"""Fixtures for the STT tests.

Unit tests are hermetic (respx mocks the HTTP server, no model is loaded).  Three modules step
outside that: ``test_live_speaches.py`` (marker ``live``, needs ``STT_LIVE=1``),
``test_live_ovms.py`` (marker ``live``, needs ``STT_LIVE_OVMS=1`` and an OVMS container) and
``test_faster_whisper_local.py`` (marker ``slow``, needs the optional faster-whisper extra).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIO_ROOT = Path(os.environ.get("KEYUP_AUDIO_ROOT", REPO_ROOT / "audio"))

# Real recordings from your own archive, relative to AUDIO_ROOT: a 0.42 s squelch tail, a
# 4.91 s clip of plain speech and a 14.99 s dispatch (the durations the live tests assert).
# None ships with the repository, so the tests that read them skip unless these point at
# files that exist.  Read-only: the tests never write anything under AUDIO_ROOT.
SAMPLE_SHORT = os.environ.get("KEYUP_STT_SAMPLE_SHORT", "samples/short_squelch_tail.mp3")  # 0.42 s
SAMPLE_MEDIUM = os.environ.get("KEYUP_STT_SAMPLE_MEDIUM", "samples/medium_speech.mp3")  # 4.91 s
SAMPLE_LONG = os.environ.get("KEYUP_STT_SAMPLE_LONG", "samples/long_dispatch.mp3")  # 14.99 s
#: a word the long sample's transcript must contain (blank: only the length is checked)
SAMPLE_LONG_WORD = os.environ.get("KEYUP_STT_SAMPLE_LONG_WORD", "")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: hits a real STT server; needs STT_LIVE=1 (Speaches) or STT_LIVE_OVMS=1 (OVMS)",
    )
    config.addinivalue_line("markers", "slow: loads a real whisper model on CPU")


def audio(relative: str) -> Path:
    """Absolute path to a corpus file, skipping the test when the corpus is not present."""
    path = AUDIO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"corpus file missing: {path}")
    return path


@pytest.fixture
def mp3(tmp_path: Path) -> Path:
    """A tiny stand-in file for tests that only care about the request, not the audio."""
    path = tmp_path / "20260828_062253_Example-System_Central_T-Control__TO_1701_FROM_9200005.mp3"
    path.write_bytes(b"ID3\x03\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64)
    return path


@pytest.fixture
def verbose_json() -> dict:
    """A real Speaches ``verbose_json`` body, copied from whisper_results.json (14.99 s sample)."""
    return {
        "task": "transcribe",
        "language": "en",
        "duration": 14.9943125,
        "text": "419 on 23, this is going to be at 784 Maple Drive, will be for a 59-year-old male subject with back and neck pain and chest pain, is conscious and breathing.",
        "words": [
            {"start": 1.32, "end": 1.8, "word": " 419", "probability": 0.78564453125},
            {"start": 1.8, "end": 2.1, "word": " on", "probability": 0.51513671875},
            {"start": 2.1, "end": 2.38, "word": " 23,", "probability": 0.99267578125},
        ],
        "segments": [
            {
                "id": 1,
                "seek": 0,
                "start": 0.0,
                "end": 14.8,
                "text": " 419 on 23, this is going to be at 784 Maple Drive, will be for a 59-year-old male subject with back and neck pain and chest pain, is conscious and breathing.",
                "tokens": [50365, 19048],
                "temperature": 0.0,
                "avg_logprob": -0.24023437669331377,
                "compression_ratio": 1.2213740458015268,
                "no_speech_prob": 0.1282958984375,
                "words": None,
            }
        ],
    }
