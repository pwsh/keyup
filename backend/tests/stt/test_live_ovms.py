"""Live end-to-end test against a real OpenVINO Model Server.

Skipped unless ``STT_LIVE_OVMS=1``.  Any device works — the server decides that, not us — so
this runs against a CPU container on a laptop and against the iGPU/NPU container on an
Intel Core Ultra host unchanged::

    docker run --rm -p 127.0.0.1:8097:8000 -v /tmp/ovms-models:/models:rw \\
        openvino/model_server:weekly --rest_port 8000 \\
        --source_model OpenVINO/whisper-base-fp16-ov --model_name whisper-base-fp16-ov \\
        --model_repository_path /models --target_device CPU

    STT_LIVE_OVMS=1 STT_LIVE_OVMS_MODEL=whisper-base-fp16-ov \\
        pytest tests/stt/test_live_ovms.py -m live -v

Point it elsewhere with ``STT_LIVE_OVMS_BASE_URL`` / ``STT_LIVE_OVMS_MODEL``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.stt import BackendRouter, TranscribeOptions, assess
from app.stt.base import ModelNotInstalledError
from app.stt.ovms import OVMSBackend
from app.stt.prompt import build_prompt
from app.stt.types import Quality

from .conftest import SAMPLE_LONG, SAMPLE_MEDIUM, SAMPLE_SHORT, audio

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STT_LIVE_OVMS") != "1", reason="set STT_LIVE_OVMS=1 to run live OVMS tests"
    ),
]

BASE_URL = os.environ.get("STT_LIVE_OVMS_BASE_URL", "http://localhost:8097")
MODEL = os.environ.get("STT_LIVE_OVMS_MODEL", "whisper-base-fp16-ov")


@pytest.fixture(scope="module")
def backend() -> OVMSBackend:
    return OVMSBackend(name="stt-ovms", base_url=BASE_URL, model=MODEL, concurrency=1)


@pytest.fixture(scope="module")
def options() -> TranscribeOptions:
    # The prompt is built and passed exactly as the worker builds it, to prove the backend
    # drops it: OVMS accepts the field and ignores it, so sending it would be theatre.
    return TranscribeOptions(
        prompt=build_prompt(
            system_name="Example System Example County",
            talkgroup_aliases=["EC FIRE DISPATCH", "EC EMS OPS"],
            unit_aliases=["Engine 1", "Medic 21"],
        ),
        hotwords="Engine 1, Medic 21",
        timeout_s=300,
    )


def test_health_reports_the_served_model(backend):
    health = backend.health()
    assert health.ok, health.detail


def test_health_rejects_a_model_the_server_does_not_serve():
    probe = OVMSBackend(name="stt-ovms", base_url=BASE_URL, model="whisper-large-v3-turbo-fp16-ov")
    health = probe.health()
    assert not health.ok
    assert "not installed" in health.detail
    # OVMS answers 404 "Mediapipe graph definition with requested name is not found", which
    # is what makes the router disqualify the backend rather than retry it forever.
    with pytest.raises(ModelNotInstalledError):
        probe.transcribe(audio(SAMPLE_MEDIUM), TranscribeOptions())


def test_medium_clip_transcribes_to_real_words(backend, options):
    """4.91 s: 'Driveway to The Shed is on Gable Avenue.'"""
    result = backend.transcribe(audio(SAMPLE_MEDIUM), options)
    assert result.duration == pytest.approx(4.91, abs=0.1)
    assert result.engine == "ovms" and result.backend_name == "stt-ovms"
    assert result.model == MODEL
    assert len(result.text.split()) >= 3
    assert "gable" in result.text.lower() or "avenue" in result.text.lower()

    # The whole point of this backend's mapping: text only, one segment, no model metrics.
    assert len(result.segments) == 1
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == pytest.approx(result.duration)
    assert result.segments[0].avg_logprob is None
    assert result.segments[0].no_speech_prob is None
    assert result.segments[0].compression_ratio is not None

    outcome = assess(result, result.duration, options)
    assert outcome.quality in {Quality.GOOD, Quality.SUSPECT}
    assert outcome.cleaned_text
    assert "no_model_metrics" in outcome.reasons


def test_long_clip_keeps_the_address(backend, options):
    """14.99 s EMS dispatch.  whisper-base mangles digits; the street name should survive."""
    result = backend.transcribe(audio(SAMPLE_LONG), options)
    assert result.duration == pytest.approx(14.99, abs=0.1)
    assert len(result.text.split()) >= 10
    assert result.rtf is not None and result.rtf > 0


def test_a_trunk_recorder_m4a_is_transcoded_and_accepted(backend, options, tmp_path: Path):
    """OVMS rejects m4a outright (400); the backend transcodes, so the caller never sees it."""
    source = audio(SAMPLE_MEDIUM)
    m4a = tmp_path / "call.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-c:a", "aac", str(m4a)],
        capture_output=True, timeout=60, check=True,
    )  # fmt: skip
    result = backend.transcribe(m4a, options)
    assert len(result.text.split()) >= 3
    assert result.duration == pytest.approx(4.91, abs=0.2)


def test_short_squelch_tail_is_labeled_noise(backend, options):
    """0.42 s tail.  OVMS has no VAD, so the duration guard has to keep it out of the decoder."""
    result = backend.transcribe(audio(SAMPLE_SHORT), options)
    assert result.text == "", f"the guard let a squelch tail through: {result.text!r}"
    assert assess(result, result.duration, options).quality is Quality.NOISE


def test_router_end_to_end(backend, options):
    router = BackendRouter([backend])
    result = router.transcribe(audio(SAMPLE_MEDIUM), options)
    assert result.text
    stats = router.stats()["stt-ovms"]
    assert stats["successes"] == 1 and stats["errors"] == 0
    assert stats["kind"] == "ovms" and stats["healthy"] is True
