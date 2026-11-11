"""Local CPU backend.

The fast tests never load a model; the one marked ``slow`` really does run
``Systran/faster-whisper-tiny`` (int8, 2 threads) over a 4.9 s corpus clip, which takes a few
seconds and downloads ~75 MB into ``~/.cache/huggingface`` the first time.  Deselect it with
``pytest -m "not slow"``.
"""

from __future__ import annotations

import pytest

from app.stt.faster_whisper_local import FasterWhisperLocalBackend, reset_model_cache
from app.stt.types import Quality, TranscribeOptions

from .conftest import SAMPLE_MEDIUM, audio

faster_whisper = pytest.importorskip("faster_whisper", reason="optional 'cpu' extra not installed")


def test_defaults_match_the_documented_cpu_profile():
    backend = FasterWhisperLocalBackend()
    assert backend.kind == "faster_whisper_local"
    assert backend.device == "cpu" and backend.compute_type == "int8"
    assert backend.concurrency == 1, "CTranslate2 already saturates cpu_threads cores"
    assert backend.priority < 100, "local CPU must sit below the GPU server"


def test_missing_file_raises_without_loading_a_model(tmp_path):
    from app.stt.base import TranscriptionError

    backend = FasterWhisperLocalBackend(model="does-not-exist")
    with pytest.raises(TranscriptionError):
        backend.transcribe(tmp_path / "nope.mp3", TranscribeOptions())


def test_health_reports_a_model_that_cannot_load():
    reset_model_cache()
    health = FasterWhisperLocalBackend(model="Systran/definitely-not-a-model").health()
    assert not health.ok and health.detail


@pytest.mark.slow
def test_transcribes_a_real_clip_on_cpu():
    path = audio(SAMPLE_MEDIUM)  # 4.91 s of plain speech
    backend = FasterWhisperLocalBackend(model="Systran/faster-whisper-tiny", cpu_threads=2)

    health = backend.health()
    assert health.ok, health.detail

    result = backend.transcribe(path, TranscribeOptions(language="en"))
    assert result.engine == "faster_whisper_local" and result.backend_name == "local"
    assert result.model == "Systran/faster-whisper-tiny"
    assert result.duration == pytest.approx(4.91, abs=0.3)
    assert result.processing_ms > 0 and result.rtf is not None
    assert result.text.strip(), "tiny should still find speech in a clean 5 s clip"
    assert result.segments and result.segments[0].no_speech_prob is not None
    assert result.raw is None, "local backend keeps no raw payload"

    from app.stt.postprocess import assess

    outcome = assess(result, result.duration, TranscribeOptions())
    assert outcome.quality in set(Quality)


@pytest.mark.slow
def test_model_instance_is_shared_across_backends():
    reset_model_cache()
    a = FasterWhisperLocalBackend(model="Systran/faster-whisper-tiny", cpu_threads=2)
    b = FasterWhisperLocalBackend(name="local2", model="Systran/faster-whisper-tiny", cpu_threads=2)
    assert a._model() is b._model()
    c = FasterWhisperLocalBackend(model="Systran/faster-whisper-tiny", cpu_threads=1)
    assert c._model() is not a._model()


@pytest.mark.slow
def test_word_timestamps_on_cpu():
    backend = FasterWhisperLocalBackend(model="Systran/faster-whisper-tiny", cpu_threads=2)
    result = backend.transcribe(audio(SAMPLE_MEDIUM), TranscribeOptions(word_timestamps=True))
    assert result.words, "word_timestamps=True must populate words"
    assert all(w.start <= w.end for w in result.words)
