"""Unit tests for the OpenAI-compatible backend, with respx standing in for Speaches."""

from __future__ import annotations

import re
import subprocess
import wave
from pathlib import Path

import httpx
import pytest
import respx

from app.stt.base import BackendUnavailableError, ModelNotInstalledError, TranscriptionError
from app.stt.openai_compatible import OpenAICompatibleBackend
from app.stt.types import TranscribeOptions

BASE = "http://speaches.test:8000"
MODEL = "Systran/faster-whisper-large-v3"
TRANSCRIBE = f"{BASE}/v1/audio/transcriptions"
MODELS = f"{BASE}/v1/models"


def make_backend(**kw) -> OpenAICompatibleBackend:
    kw.setdefault("name", "test")
    kw.setdefault("base_url", BASE)
    kw.setdefault("model", MODEL)
    kw.setdefault("backoff_base_s", 0.0)  # keep retry tests instant
    return OpenAICompatibleBackend(**kw)


def form_fields(request: httpx.Request) -> list[tuple[str, str]]:
    """Parse a multipart body into ``(name, value)`` pairs, ignoring the file part."""
    body = request.content.decode("utf-8", "replace")
    pairs: list[tuple[str, str]] = []
    for part in re.split(r"--[0-9a-f]{16,}", body):
        match = re.search(r'name="([^"]+)"(?:; filename="[^"]*")?\r\n(?:[^\r\n]*\r\n)*\r\n(.*?)\r\n?$', part, re.S)
        if match and "filename=" not in part.split("\r\n")[1]:
            pairs.append((match.group(1), match.group(2)))
    return pairs


# --------------------------------------------------------------------------- request shape


@respx.mock
def test_request_shape_defaults(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=verbose_json))
    make_backend().transcribe(mp3, TranscribeOptions(prompt="Example County radio traffic."))

    fields = form_fields(route.calls[0].request)
    names = [n for n, _ in fields]
    values = dict(fields)
    assert values["model"] == MODEL
    assert values["response_format"] == "verbose_json"
    assert values["language"] == "en"
    assert values["temperature"] == "0.0"
    assert values["vad_filter"] == "true"
    assert values["prompt"] == "Example County radio traffic."
    # Segment granularity always; word only on request.
    assert [v for n, v in fields if n == "timestamp_granularities[]"] == ["segment"]
    # hotwords is omitted entirely, not sent empty.
    assert "hotwords" not in names
    assert 'filename="' in route.calls[0].request.content.decode("utf-8", "replace")


@respx.mock
def test_request_shape_words_hotwords_and_vad_off(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=verbose_json))
    make_backend().transcribe(
        mp3,
        TranscribeOptions(
            word_timestamps=True, hotwords="Engine 1 Medic 21", vad=False, temperature=0.2, language=None
        ),
    )
    fields = form_fields(route.calls[0].request)
    values = dict(fields)
    assert [v for n, v in fields if n == "timestamp_granularities[]"] == ["segment", "word"]
    assert values["hotwords"] == "Engine 1 Medic 21"
    assert values["vad_filter"] == "false"
    assert values["temperature"] == "0.2"
    assert "language" not in values


@respx.mock
def test_bearer_token_sent(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=verbose_json))
    make_backend(api_key="s3cret").transcribe(mp3, TranscribeOptions())
    assert route.calls[0].request.headers["authorization"] == "Bearer s3cret"


@respx.mock
def test_no_auth_header_without_key(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=verbose_json))
    make_backend().transcribe(mp3, TranscribeOptions())
    assert "authorization" not in route.calls[0].request.headers


# --------------------------------------------------------------------------- normalization


@respx.mock
def test_file_is_uploaded_untouched_when_normalization_is_off(mp3):
    """The default (no filter) path uploads the recording as-is, unchanged from before."""
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json={"text": "x"}))
    make_backend().transcribe(mp3, TranscribeOptions())
    body = route.calls[0].request.content
    assert mp3.read_bytes() in body


@respx.mock
def test_normalization_transcodes_to_normalized_wav_before_upload(tmp_path: Path, monkeypatch):
    """With the switch on, the clip is transcoded to wav with the -af filter first (whisper)."""
    wav = tmp_path / "clip.wav"
    with wave.open(str(wav), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16_000)
        out.writeframes(b"\x10\x00" * 32_000)

    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr("app.stt.normalize.subprocess.run", spy)
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json={"text": "x"}))

    af = "speechnorm=e=12.5:r=0.0001:l=1"
    make_backend().transcribe(wav, TranscribeOptions(normalize_filter=af))

    assert seen and any("-af" in c and af in c for c in seen)
    body = route.calls[0].request.content
    assert wav.read_bytes() not in body, "the un-normalized wav must not reach the server"
    assert f'filename="{wav.stem}.wav"'.encode() in body
    assert b"RIFF" in body and b"WAVE" in body


# --------------------------------------------------------------------------- response mapping


@respx.mock
def test_maps_verbose_json(mp3, verbose_json):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(200, json=verbose_json))
    result = make_backend(name="whisper").transcribe(mp3, TranscribeOptions())

    assert result.text.startswith("419 on 23")
    assert result.language == "en"
    assert result.duration == pytest.approx(14.9943125)
    assert result.engine == "openai_compatible"
    assert result.backend_name == "whisper"
    assert result.model == MODEL
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert (seg.start, seg.end) == (0.0, 14.8)
    assert seg.avg_logprob == pytest.approx(-0.24023437669331377)
    assert seg.no_speech_prob == pytest.approx(0.1282958984375)
    assert seg.compression_ratio == pytest.approx(1.2213740458015268)
    assert result.words is not None and result.words[0].word == " 419"
    assert result.processing_ms >= 0
    assert result.raw is not None and result.raw["task"] == "transcribe"


@respx.mock
def test_empty_vad_filtered_response(mp3):
    """A 0.42 s squelch tail with vad_filter=true: empty text, junk language, no segments."""
    respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(
            200, json={"task": "transcribe", "language": "nn", "duration": 0.418, "text": "", "segments": []}
        )
    )
    result = make_backend().transcribe(mp3, TranscribeOptions())
    assert result.text == ""
    assert result.segments == []
    assert result.words is None


@respx.mock
def test_words_nested_in_segments_are_lifted(mp3):
    respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(
            200,
            json={
                "language": "en",
                "duration": 2.0,
                "text": "Copy that.",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": " Copy that.",
                        "words": [{"start": 0.1, "end": 0.5, "word": " Copy", "probability": 0.9}],
                    }
                ],
            },
        )
    )
    result = make_backend().transcribe(mp3, TranscribeOptions(word_timestamps=True))
    assert result.words is not None and [w.word for w in result.words] == [" Copy"]


# --------------------------------------------------------------------------- errors & retries


@respx.mock
def test_404_model_not_installed(mp3):
    detail = "Model 'Systran/faster-whisper-medium' is not installed locally. You can download the model using `POST /v1/models`"
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(404, json={"detail": detail}))
    with pytest.raises(ModelNotInstalledError) as excinfo:
        make_backend(model="Systran/faster-whisper-medium").transcribe(mp3, TranscribeOptions())
    assert "not installed locally" in str(excinfo.value)
    assert excinfo.value.model == "Systran/faster-whisper-medium"
    assert route.call_count == 1, "a missing model is permanent; it must not be retried"


@respx.mock
def test_retries_on_5xx_then_succeeds(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(
        side_effect=[
            httpx.Response(503, text="overloaded"),
            httpx.Response(500, json={"detail": "cuda oom"}),
            httpx.Response(200, json=verbose_json),
        ]
    )
    result = make_backend(attempts=3).transcribe(mp3, TranscribeOptions())
    assert route.call_count == 3
    assert result.text.startswith("419 on 23")


@respx.mock
def test_retries_exhausted_raises_unavailable(mp3):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(BackendUnavailableError):
        make_backend(attempts=2).transcribe(mp3, TranscribeOptions())
    assert route.call_count == 2


@respx.mock
def test_retries_on_connection_error(mp3, verbose_json):
    route = respx.post(TRANSCRIBE).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=verbose_json)]
    )
    make_backend(attempts=3).transcribe(mp3, TranscribeOptions())
    assert route.call_count == 2


@respx.mock
def test_4xx_is_not_retried(mp3):
    route = respx.post(TRANSCRIBE).mock(return_value=httpx.Response(422, json={"detail": "bad form"}))
    with pytest.raises(TranscriptionError):
        make_backend(attempts=3).transcribe(mp3, TranscribeOptions())
    assert route.call_count == 1


def test_missing_file_raises(tmp_path):
    with pytest.raises(TranscriptionError):
        make_backend().transcribe(tmp_path / "nope.mp3", TranscribeOptions())


# --------------------------------------------------------------------------- health


@respx.mock
def test_health_ok_when_model_listed():
    respx.get(MODELS).mock(return_value=httpx.Response(200, json={"data": [{"id": MODEL}], "object": "list"}))
    health = make_backend().health()
    assert health.ok and health.latency_ms is not None


@respx.mock
def test_health_reports_missing_model():
    respx.get(MODELS).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "Systran/faster-whisper-tiny"}]})
    )
    health = make_backend().health()
    assert not health.ok
    assert "not installed" in health.detail


@respx.mock
def test_health_survives_connection_error():
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("no route to host"))
    health = make_backend().health()
    assert not health.ok
    assert "ConnectError" in health.detail


@respx.mock
def test_health_reports_http_error():
    respx.get(MODELS).mock(return_value=httpx.Response(502, text="bad gateway"))
    assert make_backend().health().ok is False


# --------------------------------------------------------------------------------------
# telling "the server choked on this file" from "the server is not there"
# --------------------------------------------------------------------------------------


@respx.mock
def test_a_read_timeout_is_not_retried_on_the_same_server(mp3):
    """The server took the upload and used the whole deadline on it; it would do so again.

    The looping BOLO call held a slot for 3 x 120 s on each server, every round.
    """
    from app.stt.base import RequestTimeoutError

    route = respx.post(TRANSCRIBE).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(RequestTimeoutError) as excinfo:
        make_backend(attempts=3).transcribe(mp3, TranscribeOptions(timeout_s=50.0))
    assert route.call_count == 1
    assert excinfo.value.reached is True
    assert "no answer within 50s" in str(excinfo.value)


@respx.mock
def test_giving_up_on_5xx_says_the_server_was_reached(mp3):
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(500, json={"detail": "boom"}))
    with pytest.raises(BackendUnavailableError) as excinfo:
        make_backend(attempts=2).transcribe(mp3, TranscribeOptions())
    assert excinfo.value.reached is True


@respx.mock
def test_giving_up_on_a_refused_connection_says_it_was_not(mp3):
    respx.post(TRANSCRIBE).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(BackendUnavailableError) as excinfo:
        make_backend(attempts=2).transcribe(mp3, TranscribeOptions())
    assert excinfo.value.reached is False


@respx.mock
def test_one_refused_connection_among_the_retries_is_enough_to_call_it_unreachable(mp3):
    """A server restarting mid-request: dropped response, then refused.  Not the file's fault."""
    respx.post(TRANSCRIBE).mock(
        side_effect=[httpx.RemoteProtocolError("server disconnected"), httpx.ConnectError("refused")]
    )
    with pytest.raises(BackendUnavailableError) as excinfo:
        make_backend(attempts=2).transcribe(mp3, TranscribeOptions())
    assert excinfo.value.reached is False
