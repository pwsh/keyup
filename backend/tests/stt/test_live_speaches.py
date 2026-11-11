"""Live end-to-end test against the real Speaches server.

Skipped unless ``STT_LIVE=1``.  Point it elsewhere with ``STT_LIVE_BASE_URL`` /
``STT_LIVE_MODEL``::

    STT_LIVE=1 pytest tests/stt/test_live_speaches.py -m live -v
"""

from __future__ import annotations

import os

import pytest

from app.stt import BackendRouter, OpenAICompatibleBackend, TranscribeOptions, assess
from app.stt.base import ModelNotInstalledError
from app.stt.prompt import build_hotwords, build_prompt
from app.stt.types import Quality

from .conftest import SAMPLE_LONG, SAMPLE_LONG_WORD, SAMPLE_MEDIUM, SAMPLE_SHORT, audio

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("STT_LIVE") != "1", reason="set STT_LIVE=1 to run live STT tests"),
]

BASE_URL = os.environ.get("STT_LIVE_BASE_URL", "http://whisper:8000")
MODEL = os.environ.get("STT_LIVE_MODEL", "Systran/faster-whisper-large-v3")


@pytest.fixture(scope="module")
def backend() -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(name="whisper", base_url=BASE_URL, model=MODEL, concurrency=4)


@pytest.fixture(scope="module")
def options() -> TranscribeOptions:
    return TranscribeOptions(
        prompt=build_prompt(
            system_name="Example System Example County",
            talkgroup_aliases=["EC FIRE DISPATCH", "EC EMS OPS", "EC V-LAW 31+"],
            unit_aliases=["Engine 1", "Medic 21", "EFD 2"],
        ),
        hotwords=build_hotwords(unit_aliases=["Engine 1", "Medic 21", "EFD 2"]),
        timeout_s=120,
    )


def test_health_reports_the_installed_model(backend):
    health = backend.health()
    assert health.ok, health.detail
    assert health.latency_ms is not None and health.latency_ms < 5000


def test_health_rejects_a_model_that_is_not_installed():
    probe = OpenAICompatibleBackend(
        name="whisper", base_url=BASE_URL, model="Systran/faster-whisper-medium"
    )
    health = probe.health()
    assert not health.ok
    assert "not installed" in health.detail
    with pytest.raises(ModelNotInstalledError):
        probe.transcribe(audio(SAMPLE_MEDIUM), TranscribeOptions())


def test_medium_clip_transcribes_to_real_words(backend, options):
    """4.91 s of plain speech."""
    result = backend.transcribe(audio(SAMPLE_MEDIUM), options)
    assert result.duration == pytest.approx(4.91, abs=0.1)
    assert result.language == "en"
    assert result.engine == "openai_compatible" and result.backend_name == "whisper"
    assert result.model == MODEL
    assert len(result.text.split()) >= 3
    assert result.segments and result.segments[0].no_speech_prob is not None
    assert result.rtf is not None and result.rtf > 1.0, "GPU should beat real time"
    outcome = assess(result, result.duration, options)
    assert outcome.quality in {Quality.GOOD, Quality.SUSPECT}
    assert outcome.cleaned_text


def test_long_clip_with_word_timestamps(backend, options):
    """14.99 s EMS dispatch; the address and complaint should survive."""
    opts = options.model_copy(update={"word_timestamps": True})
    result = backend.transcribe(audio(SAMPLE_LONG), opts)
    assert result.duration == pytest.approx(14.99, abs=0.1)
    if SAMPLE_LONG_WORD:
        assert SAMPLE_LONG_WORD.lower() in result.text.lower()
    assert result.words and len(result.words) > 10
    first = result.words[0]
    assert 0.0 <= first.start <= first.end <= result.duration + 1
    assert all(w.probability is not None for w in result.words)
    assert assess(result, result.duration, opts).quality is Quality.GOOD


def test_short_squelch_tail_is_labeled_noise(backend, options):
    """0.42 s tail: with vad_filter=true the server returns nothing, and we call it noise."""
    result = backend.transcribe(audio(SAMPLE_SHORT), options)
    outcome = assess(result, result.duration, options)
    assert outcome.quality is Quality.NOISE, f"got {outcome.quality} for {result.text!r}"


def test_router_end_to_end(backend, options):
    router = BackendRouter([backend])
    result = router.transcribe(audio(SAMPLE_MEDIUM), options)
    assert result.text
    stats = router.stats()["whisper"]
    assert stats["successes"] == 1 and stats["errors"] == 0
    assert stats["mean_rtf"] and stats["mean_rtf"] > 1.0
    assert stats["healthy"] is True
