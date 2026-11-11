"""Unit tests for the OVMS backend, with respx standing in for openvino/model_server.

The request shape is the whole point of this backend: OVMS *accepts* `prompt`,
`response_format`, `hotwords` and `vad_filter` and then ignores them (measured), so the tests
below assert those fields are absent rather than merely harmless.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import httpx
import pytest
import respx

from app.stt.base import BackendUnavailableError, ModelNotInstalledError, TranscriptionError
from app.stt.ovms import OVMSBackend, compression_ratio, probe_duration, transcode_to_wav
from app.stt.postprocess import assess
from app.stt.router import build_backends
from app.stt.types import Quality, TranscribeOptions

from .test_openai_compatible import form_fields

BASE = "http://stt-ovms.test:8000"
MODEL = "whisper-medium-fp16-ov"
TRANSCRIBE = f"{BASE}/v3/audio/transcriptions"
MODELS = f"{BASE}/v1/models"

#: What OVMS actually answers.  There is nothing else in it: no segments, no logprobs, no
#: duration, no language.
TEXT_ONLY = {"text": " Driveway to The Shed is on Gable Avenue."}


def make_backend(**kw) -> OVMSBackend:
    kw.setdefault("name", "stt-ovms")
    kw.setdefault("base_url", BASE)
    kw.setdefault("model", MODEL)
    kw.setdefault("backoff_base_s", 0.0)  # keep retry tests instant
    return OVMSBackend(**kw)


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    """A real 2 s 16 kHz mono wav — long enough to clear the sub-second squelch guard."""
    path = tmp_path / "20260828_062253_Example-System_Central_T-Control__TO_1101_FROM_5200005.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"\x10\x00" * 32_000)
    return path


@pytest.fixture
def m4a(wav: Path) -> Path:
    """The same audio as trunk-recorder writes it (TR_PREFER_AUDIO=m4a), or skip."""
    path = wav.with_suffix(".m4a")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", str(path)],
            capture_output=True, timeout=60, check=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"ffmpeg cannot write m4a here: {exc}")
    return path


# --------------------------------------------------------------------------- request shape


@respx.mock
def test_it_posts_to_v3_with_only_the_fields_ovms_reads(wav: Path):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    make_backend().transcribe(
        wav,
        TranscribeOptions(prompt="Example County radio traffic.", hotwords="Engine 1", vad=True),
    )

    request = route.calls[0].request
    assert request.url.path == "/v3/audio/transcriptions"
    fields = form_fields(request)
    values = dict(fields)
    assert values["model"] == MODEL
    assert values["language"] == "en"
    # Never omitted: OVMS defaults temperature to 1.0 (sampling) when the field is absent.
    assert values["temperature"] == "0.0"
    # The four fields OVMS accepts and then silently ignores.  Sending them would put a domain
    # prompt in the request log that never reached the decoder.
    names = {n for n, _ in fields}
    assert names == {"model", "language", "temperature"}
    for ignored in ("prompt", "response_format", "hotwords", "vad_filter"):
        assert ignored not in names


@respx.mock
def test_word_granularity_is_sent_only_when_asked_for(wav: Path):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    backend = make_backend()

    backend.transcribe(wav, TranscribeOptions())
    assert "timestamp_granularities[]" not in {n for n, _ in form_fields(route.calls[0].request)}

    backend.transcribe(wav, TranscribeOptions(word_timestamps=True))
    fields = form_fields(route.calls[1].request)
    # 'segment' is never asked for: OVMS changes the decode when it is, and word granularity
    # 400s outright on a model exported without enable_word_timestamps.
    assert [v for n, v in fields if n == "timestamp_granularities[]"] == ["word"]


@respx.mock
def test_a_wav_is_uploaded_untouched(wav: Path):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    make_backend().transcribe(wav, TranscribeOptions())
    body = route.calls[0].request.content
    assert wav.read_bytes() in body
    assert f'filename="{wav.name}"'.encode() in body


# --------------------------------------------------------------------------- transcode


@respx.mock
def test_an_m4a_is_transcoded_to_wav_before_upload(m4a: Path):
    """OVMS answers 400 'not valid wav nor mp3' for anything else, so ffmpeg runs first."""
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    make_backend().transcribe(m4a, TranscribeOptions())

    body = route.calls[0].request.content
    assert f'filename="{m4a.stem}.wav"'.encode() in body
    assert b"audio/wav" in body
    assert m4a.read_bytes() not in body, "the m4a itself must not reach OVMS"
    assert b"RIFF" in body and b"WAVE" in body


@respx.mock
def test_normalize_forces_a_transcode_of_a_native_wav_with_af(wav: Path, monkeypatch):
    """OVMS would upload a wav untouched; with the switch on it must be normalized first."""
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr("app.stt.normalize.subprocess.run", spy)
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))

    af = "speechnorm=e=12.5:r=0.0001:l=1"
    make_backend().transcribe(wav, TranscribeOptions(normalize_filter=af))

    # The filter reached ffmpeg's argv, even though the input was already a native wav.
    assert seen and any("-af" in c and af in c for c in seen)
    body = route.calls[0].request.content
    assert wav.read_bytes() not in body, "the un-normalized wav must not reach OVMS"
    assert f'filename="{wav.stem}.wav"'.encode() in body
    assert b"RIFF" in body and b"WAVE" in body


def test_transcode_produces_16k_mono_wav(m4a: Path, tmp_path: Path):
    out = tmp_path / "out.wav"
    out.write_bytes(transcode_to_wav(m4a))
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16_000
        assert handle.getsampwidth() == 2


def test_transcode_reports_a_file_ffmpeg_cannot_read(tmp_path: Path):
    junk = tmp_path / "truncated.m4a"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(TranscriptionError, match="ffmpeg failed"):
        transcode_to_wav(junk)


# --------------------------------------------------------------------------- mapping


@respx.mock
def test_text_only_becomes_one_whole_call_segment(wav: Path):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    result = make_backend().transcribe(wav, TranscribeOptions())

    assert result.text == "Driveway to The Shed is on Gable Avenue."
    assert result.engine == "ovms" and result.backend_name == "stt-ovms" and result.model == MODEL
    # OVMS reports no duration; ffprobe supplies it, and the single segment spans the call.
    assert result.duration == pytest.approx(2.0, abs=0.05)
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.start == 0.0 and segment.end == pytest.approx(result.duration)
    assert segment.text == result.text
    assert segment.avg_logprob is None and segment.no_speech_prob is None
    assert segment.compression_ratio == pytest.approx(compression_ratio(result.text))
    assert result.words is None
    assert result.raw == TEXT_ONLY


@respx.mock
def test_empty_text_maps_to_no_segments_and_is_labeled_noise(wav: Path):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json={"text": ""}))
    result = make_backend().transcribe(wav, TranscribeOptions())
    assert result.text == "" and result.segments == []
    assert assess(result, result.duration).quality is Quality.NOISE


@respx.mock
def test_words_are_kept_when_the_model_returns_them(wav: Path):
    respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(
            200,
            json={
                "text": "Engine 12 on scene.",
                "words": [
                    {"word": " Engine", "start": 0.1, "end": 0.5},
                    {"word": " 12", "start": 0.5, "end": 0.8},
                ],
            },
        )
    )
    result = make_backend().transcribe(wav, TranscribeOptions(word_timestamps=True))
    assert result.words and len(result.words) == 2
    assert result.words[0].word == " Engine" and result.words[0].probability is None


@respx.mock
def test_segments_are_used_when_a_future_build_sends_them(wav: Path):
    respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(
            200,
            json={
                "text": "Driveway to The Shed is on Gable Avenue.",
                "segments": [{"text": "Driveway to The Shed", "start": 0.0, "end": 1.2},
                             {"text": "is on Gable Avenue.", "start": 1.2, "end": 2.0}],
            },
        )  # fmt: skip
    )
    result = make_backend().transcribe(wav, TranscribeOptions())
    assert [s.end for s in result.segments] == [1.2, 2.0]
    assert all(s.no_speech_prob is None for s in result.segments)


# --------------------------------------------------------------------------- quality


@respx.mock
def test_quality_still_labels_sensibly_without_model_metrics(wav: Path):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    result = make_backend().transcribe(wav, TranscribeOptions())
    outcome = assess(result, result.duration)
    assert outcome.quality is Quality.GOOD
    assert outcome.cleaned_text == "Driveway to The Shed is on Gable Avenue."
    # The row says out loud that two of the checks had no input.
    assert "no_model_metrics" in outcome.reasons


@respx.mock
def test_a_caption_hallucination_is_still_caught_by_the_text_heuristics(wav: Path):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json={"text": " Thanks for watching!"}))
    result = make_backend().transcribe(wav, TranscribeOptions())
    outcome = assess(result, result.duration)
    assert outcome.quality is Quality.NOISE
    assert any(r.startswith("hallucination_phrase") for r in outcome.reasons)


@respx.mock
def test_a_decoder_loop_is_still_caught_by_the_recomputed_compression_ratio(wav: Path):
    loop = "Lakeside Riverton " * 20
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json={"text": loop}))
    result = make_backend().transcribe(wav, TranscribeOptions())
    outcome = assess(result, result.duration)
    assert "repeat_loop" in outcome.reasons
    assert outcome.quality is Quality.SUSPECT


# --------------------------------------------------------------------------- vad substitute


@respx.mock
def test_a_sub_second_squelch_tail_never_reaches_ovms(tmp_path: Path):
    """OVMS has no vad_filter; the duration guard is the whole of the substitute."""
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=TEXT_ONLY))
    tail = tmp_path / "tail.wav"
    with wave.open(str(tail), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"\x01\x00" * 6_720)  # 0.42 s, the corpus squelch tail

    result = make_backend().transcribe(tail, TranscribeOptions(vad=True))
    assert not route.called
    assert result.text == "" and result.duration == pytest.approx(0.42, abs=0.01)
    assert assess(result, result.duration).quality is Quality.NOISE

    make_backend().transcribe(tail, TranscribeOptions(vad=False))
    assert route.called, "vad=false sends it anyway"


# --------------------------------------------------------------------------- errors


@respx.mock
def test_an_unknown_model_name_disqualifies_the_backend(wav: Path):
    """OVMS answers 404 for a --model_name it does not serve."""
    respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(404, json={"error": "Mediapipe graph definition with requested name is not found"})
    )
    with pytest.raises(ModelNotInstalledError, match="Mediapipe graph definition"):
        make_backend().transcribe(wav, TranscribeOptions())


@respx.mock
def test_a_400_is_not_retried(wav: Path):
    """Word timestamps on a model exported without them, for instance."""
    route = respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(400, json={"error": "Word timestamps not supported for this model"})
    )
    with pytest.raises(TranscriptionError, match="Word timestamps not supported"):
        make_backend().transcribe(wav, TranscribeOptions(word_timestamps=True))
    assert route.call_count == 1


@respx.mock
def test_5xx_and_transport_errors_are_retried(wav: Path):
    route = respx.post(TRANSCRIBE).mock(
        side_effect=[
            httpx.Response(503, text="model loading"),
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=TEXT_ONLY),
        ]
    )
    result = make_backend(attempts=3).transcribe(wav, TranscribeOptions())
    assert result.text and route.call_count == 3


@respx.mock
def test_it_gives_up_after_the_configured_attempts(wav: Path):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(BackendUnavailableError):
        make_backend(attempts=2).transcribe(wav, TranscribeOptions())
    assert route.call_count == 2


def test_a_missing_file_is_an_error_not_a_request(tmp_path: Path):
    with pytest.raises(TranscriptionError, match="audio file not found"):
        make_backend().transcribe(tmp_path / "gone.wav", TranscribeOptions())


# --------------------------------------------------------------------------- health


@respx.mock
def test_health_finds_the_model_in_v1_models():
    """OVMS answers /v1/models in the OpenAI shape, so the inherited probe works as-is."""
    respx.get(MODELS).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": MODEL, "object": "model", "owned_by": "OVMS"}], "object": "list"}
        )
    )
    health = make_backend().health()
    assert health.ok and health.latency_ms is not None


@respx.mock
def test_health_rejects_a_server_serving_a_different_model():
    respx.get(MODELS).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whisper-base-fp16-ov"}], "object": "list"})
    )
    health = make_backend().health()
    assert not health.ok and "not installed" in health.detail


@respx.mock
def test_health_never_raises_when_the_server_is_down():
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("connection refused"))
    health = make_backend().health()
    assert not health.ok and "ConnectError" in health.detail


# --------------------------------------------------------------------------- factory


def test_build_backends_makes_an_ovms_backend():
    (backend,) = build_backends(
        [{"name": "stt-ovms", "kind": "ovms", "base_url": BASE, "model": MODEL, "priority": 60}]
    )
    assert isinstance(backend, OVMSBackend)
    assert backend.kind == "ovms" and backend.priority == 60 and backend.concurrency == 1
    assert backend.base_url == BASE


def test_build_backends_requires_a_base_url():
    with pytest.raises(ValueError, match="base_url"):
        build_backends([{"name": "stt-ovms", "kind": "ovms", "model": MODEL}])


# --------------------------------------------------------------------------- helpers


def test_probe_duration_reads_the_file(wav: Path):
    assert probe_duration(wav) == pytest.approx(2.0, abs=0.05)


def test_probe_duration_answers_none_for_a_file_ffprobe_cannot_read(tmp_path: Path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"nope")
    assert probe_duration(junk) is None


def test_compression_ratio_ignores_text_too_short_to_mean_anything():
    assert compression_ratio("10-4") is None
    assert compression_ratio("Lakeside Riverton " * 20) > 2.4
