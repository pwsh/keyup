"""The OpenVINO GenAI backend and the micro-server that fronts it.

Nothing here needs an Intel accelerator.  The fast tests replace ``WhisperPipeline`` with a
stub — the interesting logic is chunk mapping, the silence guard that stands in for the VAD
openvino-genai does not have, model resolution and the generation config — and the one test
marked ``slow`` really does compile ``OpenVINO/whisper-base-fp16-ov`` for the CPU device and
decode a 4.9 s corpus clip (~150 MB download the first time).  Deselect it with
``pytest -m "not slow"``.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.stt import openvino_local as ov
from app.stt.base import BackendUnavailableError, TranscriptionError
from app.stt.openvino_local import OpenVINOLocalBackend
from app.stt.postprocess import assess
from app.stt.router import build_backends
from app.stt.types import Quality, TranscribeOptions

from .conftest import SAMPLE_MEDIUM, SAMPLE_SHORT, audio

# --------------------------------------------------------------------------- stubs


@dataclass
class Chunk:
    start_ts: float
    end_ts: float
    text: str


class FakeDecoded:
    def __init__(self, chunks, language="en"):
        self.chunks = chunks
        self.texts = ["".join(c.text for c in chunks)]
        self.scores = [1.0]
        self.language = language


class FakePipeline:
    """Records the generation kwargs and returns canned chunks."""

    def __init__(self, chunks=None, error: Exception | None = None):
        self.chunks = chunks if chunks is not None else [Chunk(0.0, 4.3, " Engine 12 is on scene.")]
        self.error = error
        self.calls: list[dict] = []

    def generate(self, samples, **kwargs):
        self.calls.append({"samples": len(samples), **kwargs})
        if self.error:
            raise self.error
        return FakeDecoded(self.chunks)


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """A directory that looks like a pre-exported OpenVINO whisper model."""
    (tmp_path / "openvino_encoder_model.xml").write_text("<net/>")
    (tmp_path / "openvino_decoder_model.xml").write_text("<net/>")
    return tmp_path


@pytest.fixture
def stub(monkeypatch, model_dir: Path):
    """A backend whose pipeline is a :class:`FakePipeline`; returns ``(backend, pipeline)``."""

    def make(chunks=None, error: Exception | None = None, **kwargs):
        pipeline = FakePipeline(chunks, error)
        monkeypatch.setattr(ov, "_load_pipeline", lambda *a, **k: pipeline)
        backend = OpenVINOLocalBackend(model=str(model_dir), **kwargs)
        return backend, pipeline

    return make


def silence(seconds: float) -> array.array:
    return array.array("f", [0.0] * int(seconds * ov.SAMPLE_RATE))


def tone(seconds: float, amplitude: float = 0.2) -> array.array:
    n = int(seconds * ov.SAMPLE_RATE)
    return array.array("f", [amplitude * math.sin(i / 20.0) for i in range(n)])


# --------------------------------------------------------------------------- devices


def test_device_is_normalized_and_validated():
    assert OpenVINOLocalBackend(model="x", device="gpu").device == "GPU"
    assert OpenVINOLocalBackend(model="x", device=" npu ").device == "NPU"
    # OpenVINO's compound forms survive: AUTO with a fallback list, and a second GPU.
    assert OpenVINOLocalBackend(model="x", device="AUTO:GPU,CPU").device == "AUTO:GPU,CPU"
    assert OpenVINOLocalBackend(model="x", device="GPU.1").device == "GPU.1"
    with pytest.raises(ValueError, match="unsupported OpenVINO device"):
        OpenVINOLocalBackend(model="x", device="TPU")


def test_available_devices_never_raises(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "openvino", None)
    assert ov.available_devices() == []


def test_defaults_sit_between_the_gpu_server_and_the_cpu_fallback():
    backend = OpenVINOLocalBackend(model="x")
    assert backend.kind == "openvino_genai"
    assert backend.concurrency == 1, "one accelerator, one decode at a time"
    assert 10 < backend.priority < 100


# --------------------------------------------------------------------------- model resolution


def test_resolves_a_local_export(model_dir: Path):
    assert ov.resolve_model_dir(str(model_dir)) == model_dir


def test_a_directory_without_ir_explains_how_to_export(tmp_path: Path):
    (tmp_path / "pytorch_model.bin").write_bytes(b"")
    with pytest.raises(BackendUnavailableError, match="optimum-cli export openvino"):
        ov.resolve_model_dir(str(tmp_path))


def test_a_path_that_is_not_a_directory_is_not_treated_as_a_repo_id(tmp_path: Path):
    target = tmp_path / "model.xml"
    target.write_text("")
    with pytest.raises(BackendUnavailableError, match="not a directory"):
        ov.resolve_model_dir(str(target))


def test_a_repo_id_is_downloaded_into_the_cache_dir(monkeypatch, model_dir: Path, tmp_path: Path):
    seen: dict = {}

    def fake_snapshot(repo_id: str, cache_dir: str):
        seen.update(repo_id=repo_id, cache_dir=cache_dir)
        return str(model_dir)

    monkeypatch.setitem(
        __import__("sys").modules,
        "huggingface_hub",
        type("hub", (), {"snapshot_download": staticmethod(fake_snapshot)}),
    )
    cache = tmp_path / "cache"
    assert ov.resolve_model_dir("OpenVINO/whisper-base-fp16-ov", cache) == model_dir
    assert seen == {"repo_id": "OpenVINO/whisper-base-fp16-ov", "cache_dir": str(cache)}
    assert cache.is_dir()


def test_a_download_failure_is_a_backend_error(monkeypatch, tmp_path: Path):
    def boom(repo_id: str, cache_dir: str):
        raise OSError("no route to host")

    monkeypatch.setitem(
        __import__("sys").modules,
        "huggingface_hub",
        type("hub", (), {"snapshot_download": staticmethod(boom)}),
    )
    with pytest.raises(BackendUnavailableError, match="no route to host"):
        ov.resolve_model_dir("OpenVINO/whisper-base-fp16-ov", tmp_path)


# --------------------------------------------------------------------------- transcription


def test_chunks_become_segments(monkeypatch, stub):
    chunks = [Chunk(0.0, 3.2, " Engine 12 on scene."), Chunk(3.2, 7.0, " Nothing showing.")]
    backend, pipeline = stub(chunks)
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(7.0))

    result = backend.transcribe(__file__, TranscribeOptions())

    assert result.engine == "openvino_genai" and result.backend_name == "openvino"
    assert result.text == "Engine 12 on scene. Nothing showing."
    assert result.duration == pytest.approx(7.0, abs=0.01)
    assert result.processing_ms >= 0 and result.language == "en"
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 3.2), (3.2, 7.0)]
    # openvino-genai reports neither of these; postprocess skips the checks that need them.
    assert all(s.avg_logprob is None and s.no_speech_prob is None for s in result.segments)
    assert result.words is None and result.raw is None


def test_an_open_ended_final_chunk_is_clamped_to_the_clip(monkeypatch, stub):
    backend, _ = stub([Chunk(0.0, -1.0, " ...")])
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    result = backend.transcribe(__file__, TranscribeOptions())
    assert result.segments[0].end == pytest.approx(5.0, abs=0.01)


def test_a_repeat_loop_is_still_caught_without_engine_scores(monkeypatch, stub):
    """The recomputed compression ratio is what keeps ``assess`` check #4 alive here."""
    looped = " ".join(["10-4 copy that"] * 40)
    backend, _ = stub([Chunk(0.0, 8.0, looped)])
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(8.0))

    result = backend.transcribe(__file__, TranscribeOptions())
    assert result.segments[0].compression_ratio > 2.4, "the recomputed ratio must see the loop"
    outcome = assess(result, result.duration, TranscribeOptions())
    assert outcome.quality is Quality.SUSPECT and "repeat_loop" in outcome.reasons


def test_a_short_clip_never_reaches_the_decoder(monkeypatch, stub):
    """0.42 s of squelch tail is what whisper turns into "Thanks for watching!"."""
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(0.42))

    result = backend.transcribe(__file__, TranscribeOptions())

    assert pipeline.calls == [], "the VAD stand-in must skip the decode entirely"
    assert result.text == "" and result.segments == []
    assert "too short" in result.raw["skipped"]
    assert assess(result, result.duration, TranscribeOptions()).quality is Quality.NOISE


def test_a_silent_clip_never_reaches_the_decoder(monkeypatch, stub):
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: silence(4.0))

    result = backend.transcribe(__file__, TranscribeOptions())

    assert pipeline.calls == []
    assert "silent" in result.raw["skipped"]
    assert result.duration == pytest.approx(4.0, abs=0.01)


def test_normalize_redecodes_with_the_filter_after_the_guard(monkeypatch, stub):
    """The guard reads the un-normalized level; a clip that passes is decoded again with -af."""
    backend, pipeline = stub()
    seen: list[str | None] = []

    def fake_decode(path, timeout_s=120.0, af=None):
        seen.append(af)
        return tone(5.0)

    monkeypatch.setattr(ov, "decode_audio", fake_decode)
    af = "speechnorm=e=12.5:r=0.0001:l=1"
    backend.transcribe(__file__, TranscribeOptions(normalize_filter=af))

    assert seen == [None, af], "first decode feeds the guard un-normalized, the second is normalized"
    assert len(pipeline.calls) == 1


def test_a_guarded_out_clip_is_never_redecoded_with_the_filter(monkeypatch, stub):
    """A squelch tail is rejected before any normalized decode — normalizing it would waste ffmpeg."""
    backend, pipeline = stub()
    seen: list[str | None] = []

    def fake_decode(path, timeout_s=120.0, af=None):
        seen.append(af)
        return tone(0.42)

    monkeypatch.setattr(ov, "decode_audio", fake_decode)
    backend.transcribe(__file__, TranscribeOptions(normalize_filter="speechnorm=e=12.5:r=0.0001:l=1"))

    assert seen == [None], "the guard rejected it, so no normalized re-decode happened"
    assert pipeline.calls == []


def test_the_guard_thresholds_are_configurable(monkeypatch, stub):
    backend, pipeline = stub(min_audio_s=0.1, silence_rms=0.0)
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(0.42))
    backend.transcribe(__file__, TranscribeOptions())
    assert len(pipeline.calls) == 1


def test_generation_config_carries_language_task_and_prompt(monkeypatch, stub):
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))

    backend.transcribe(
        __file__,
        TranscribeOptions(language="en", prompt="Example System", hotwords="Riverton", vad=True),
    )

    call = pipeline.calls[0]
    assert call["language"] == "<|en|>", "openvino-genai wants the token form"
    assert call["task"] == "transcribe" and call["return_timestamps"] is True
    assert call["max_new_tokens"] == ov.DEFAULT_MAX_NEW_TOKENS
    assert call["initial_prompt"] == "Example System"
    assert "vad_filter" not in call and "vad" not in call


def test_hotwords_are_off_by_default_and_opt_in(monkeypatch, stub):
    """Measured, not cautious: hotwords repeat into every window and the decoder follows them."""
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    options = TranscribeOptions(prompt="Example System", hotwords="Lakeside Riverton")

    backend.transcribe(__file__, options)
    assert "hotwords" not in pipeline.calls[0]

    opted_in, pipeline = stub(use_hotwords=True)
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    opted_in.transcribe(__file__, options)
    assert pipeline.calls[0]["hotwords"] == "Lakeside Riverton"


def test_the_prompt_can_be_switched_off(monkeypatch, stub):
    backend, pipeline = stub(use_prompt=False)
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    backend.transcribe(__file__, TranscribeOptions(prompt="Example System"))
    assert "initial_prompt" not in pipeline.calls[0]


def test_an_already_tokenized_language_is_passed_through(monkeypatch, stub):
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    backend.transcribe(__file__, TranscribeOptions(language="<|es|>"))
    assert pipeline.calls[0]["language"] == "<|es|>"


def test_unsupported_generation_keys_are_dropped(monkeypatch, stub):
    """An older openvino-genai has no ``hotwords``; sending it would raise."""
    backend, pipeline = stub()
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    backend.use_hotwords = True
    monkeypatch.setattr(ov, "_supported_config_keys", lambda: {"language", "task", "return_timestamps"})
    backend.transcribe(__file__, TranscribeOptions(prompt="p", hotwords="h"))
    assert "hotwords" not in pipeline.calls[0] and "initial_prompt" not in pipeline.calls[0]


def test_a_missing_file_raises_without_compiling_anything(tmp_path: Path):
    backend = OpenVINOLocalBackend(model="does-not-exist")
    with pytest.raises(TranscriptionError, match="audio file not found"):
        backend.transcribe(tmp_path / "nope.mp3", TranscribeOptions())


def test_engine_errors_are_normalized(monkeypatch, stub):
    backend, _ = stub(error=RuntimeError("NPU: infer request failed"))
    monkeypatch.setattr(ov, "decode_audio", lambda path, timeout_s=120.0: tone(5.0))
    with pytest.raises(TranscriptionError, match="NPU: infer request failed"):
        backend.transcribe(__file__, TranscribeOptions())


# --------------------------------------------------------------------------- audio decode


def test_decode_audio_gives_16k_mono_float32():
    samples = ov.decode_audio(audio(SAMPLE_MEDIUM))
    assert samples.typecode == "f"
    assert len(samples) / ov.SAMPLE_RATE == pytest.approx(4.91, abs=0.05)
    assert max(abs(s) for s in samples) <= 1.0


def test_decode_audio_reports_a_file_ffmpeg_cannot_read(tmp_path: Path):
    junk = tmp_path / "not-audio.mp3"
    junk.write_bytes(b"nonsense")
    with pytest.raises(TranscriptionError, match="ffmpeg failed"):
        ov.decode_audio(junk)


def test_the_real_squelch_tail_is_rejected_by_the_guard(monkeypatch, stub):
    """End to end on the actual 0.42 s file from the corpus, with only the model faked."""
    backend, pipeline = stub()
    result = backend.transcribe(audio(SAMPLE_SHORT), TranscribeOptions())
    assert pipeline.calls == [] and result.text == ""


# --------------------------------------------------------------------------- health


def test_health_reports_device_and_available_devices(monkeypatch, stub):
    backend, _ = stub(device="GPU")
    monkeypatch.setattr(ov, "available_devices", lambda: ["CPU", "GPU", "NPU"])
    health = backend.health()
    assert health.ok and health.latency_ms is not None
    assert "GPU" in health.detail and "CPU, GPU, NPU" in health.detail


def test_health_reports_the_compile_time_not_the_cache_hit(monkeypatch, model_dir: Path):
    """The second probe measures a dict lookup, so the number it quotes must be remembered."""
    ov.reset_pipeline_cache()
    monkeypatch.setattr(ov, "available_devices", lambda: ["CPU"])
    monkeypatch.setitem(
        __import__("sys").modules,
        "openvino_genai",
        type("m", (), {"WhisperPipeline": lambda *a, **k: FakePipeline()}),
    )
    backend = OpenVINOLocalBackend(model=str(model_dir), cache_dir=model_dir)
    assert backend.health().ok
    assert "compiled in" in backend.health().detail


def test_health_reports_a_device_that_cannot_compile(monkeypatch, model_dir: Path):
    def boom(*a, **k):
        raise BackendUnavailableError("device NPU not found")

    monkeypatch.setattr(ov, "_load_pipeline", boom)
    health = OpenVINOLocalBackend(model=str(model_dir), device="NPU").health()
    assert not health.ok and "device NPU not found" in health.detail


def test_health_never_raises_on_an_unresolvable_model():
    health = OpenVINOLocalBackend(model="/nonexistent/model/dir").health()
    assert not health.ok and health.detail


# --------------------------------------------------------------------------- registration


def test_the_router_builds_the_kind_from_config():
    (backend,) = build_backends(
        [
            {
                "name": "npu",
                "kind": "openvino_genai",
                "model": "OpenVINO/whisper-large-v3-turbo-int8-ov",
                "device": "NPU",
                "concurrency": 1,
                "priority": 50,
            }
        ]
    )
    assert isinstance(backend, OpenVINOLocalBackend)
    assert backend.name == "npu" and backend.device == "NPU" and backend.priority == 50


def test_an_openvino_entry_survives_the_settings_model():
    from app.config import SttBackend

    entry = SttBackend(
        name="npu", kind="openvino_genai", model="OpenVINO/whisper-base-fp16-ov", device="GPU"
    )
    (backend,) = build_backends([entry.model_dump()])
    assert backend.device == "GPU"


# --------------------------------------------------------------------------- the micro-server


class StubBackend:
    """The smallest thing ``create_app`` needs: it never loads a model."""

    kind = "openvino_genai"

    def __init__(self, result=None, health=None):
        from app.stt.base import BackendHealth
        from app.stt.types import TranscriptResult, TranscriptSegment

        self.name = "openvino"
        self.model = "OpenVINO/whisper-base-fp16-ov"
        self.device = "NPU"
        self.concurrency = 1
        self.seen: list = []
        self._health = health or BackendHealth(ok=True, latency_ms=12.0, detail="compiled for NPU")
        self._result = result or TranscriptResult(
            text="Engine 12 on scene.",
            language="en",
            duration=4.91,
            segments=[
                TranscriptSegment(start=0.0, end=4.3, text=" Engine 12 on scene.", compression_ratio=1.1)
            ],
            engine="openvino_genai",
            backend_name="openvino",
            model="OpenVINO/whisper-base-fp16-ov",
            processing_ms=700,
        )

    def transcribe(self, path, options):
        self.seen.append((Path(path).name, Path(path).read_bytes(), options))
        return self._result

    def health(self):
        return self._health


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.stt.ov_server import create_app

    def make(backend=None):
        engine = backend or StubBackend()
        return TestClient(create_app(engine)), engine

    return make


def test_server_lists_the_one_model_it_serves(client):
    http, engine = client()
    body = http.get("/v1/models").json()
    assert [m["id"] for m in body["data"]] == [engine.model]
    assert body["data"][0]["device"] == "NPU"


def test_server_health_carries_the_device_list(client, monkeypatch):
    import app.stt.ov_server as server

    monkeypatch.setattr(server, "available_devices", lambda: ["CPU", "GPU", "NPU"])
    http, _ = client()
    body = http.get("/health").json()
    assert body["ok"] and body["device"] == "NPU" and body["devices"] == ["CPU", "GPU", "NPU"]


def test_server_health_is_503_when_the_model_will_not_compile(client):
    from app.stt.base import BackendHealth

    http, _ = client(StubBackend(health=BackendHealth(ok=False, detail="no NPU")))
    response = http.get("/health")
    assert response.status_code == 503 and response.json()["detail"] == "no NPU"


def test_server_transcribes_multipart_into_verbose_json(client, mp3: Path):
    http, engine = client()
    response = http.post(
        "/v1/audio/transcriptions",
        files={"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")},
        data={
            "model": engine.model,
            "language": "en",
            "prompt": "Example System",
            "hotwords": "Riverton",
            "response_format": "verbose_json",
            "vad_filter": "true",
            "temperature": "0.0",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "Engine 12 on scene." and body["language"] == "en"
    assert body["duration"] == pytest.approx(4.91)
    assert body["segments"][0]["start"] == 0.0 and body["segments"][0]["compression_ratio"] == 1.1

    name, payload, options = engine.seen[0]
    assert name == mp3.name and payload == mp3.read_bytes(), "the upload reaches the backend intact"
    assert options.language == "en" and options.prompt == "Example System"
    assert options.hotwords == "Riverton"


def test_the_client_backend_parses_what_the_server_returns(client, mp3: Path):
    """The two halves of the compose path, joined: server response -> TranscriptResult."""
    from app.stt.openai_compatible import OpenAICompatibleBackend

    http, engine = client()
    body = http.post(
        "/v1/audio/transcriptions",
        files={"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")},
        data={"model": engine.model, "response_format": "verbose_json"},
    ).json()

    remote = OpenAICompatibleBackend(name="stt-openvino", base_url="http://x", model=engine.model)
    result = remote._to_result(body, 700, mp3)
    assert result.text == "Engine 12 on scene." and result.duration == pytest.approx(4.91)
    assert len(result.segments) == 1 and result.segments[0].no_speech_prob is None


def test_server_404s_an_unknown_model_so_the_router_disqualifies_it(client, mp3: Path):
    http, _ = client()
    response = http.post(
        "/v1/audio/transcriptions",
        files={"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")},
        data={"model": "Systran/faster-whisper-large-v3"},
    )
    assert response.status_code == 404 and "not installed" in response.json()["detail"]


def test_server_honours_the_plain_response_formats(client, mp3: Path):
    http, _ = client()
    files = {"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")}
    assert http.post("/v1/audio/transcriptions", files=files, data={"response_format": "text"}).text == (
        "Engine 12 on scene."
    )
    files = {"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")}
    body = http.post("/v1/audio/transcriptions", files=files, data={"response_format": "json"}).json()
    assert body == {"text": "Engine 12 on scene."}


def test_server_reports_a_failed_decode_as_500(client, mp3: Path):
    engine = StubBackend()

    def boom(path, options):
        raise TranscriptionError("ffmpeg failed")

    engine.transcribe = boom
    http, _ = client(engine)
    response = http.post(
        "/v1/audio/transcriptions", files={"file": (mp3.name, mp3.read_bytes(), "audio/mpeg")}
    )
    assert response.status_code == 500 and "ffmpeg failed" in response.json()["detail"]


def test_server_reads_its_configuration_from_the_environment(monkeypatch):
    from app.stt.ov_server import backend_from_env

    monkeypatch.setenv("OV_MODEL", "OpenVINO/whisper-base-fp16-ov")
    monkeypatch.setenv("OV_DEVICE", "gpu")
    monkeypatch.setenv("OV_CPU_THREADS", "8")
    backend = backend_from_env()
    assert backend.model == "OpenVINO/whisper-base-fp16-ov"
    assert backend.device == "GPU" and backend.cpu_threads == 8


# --------------------------------------------------------------------------- the real thing


@pytest.mark.slow
def test_transcribes_a_real_clip_on_the_cpu_device():
    pytest.importorskip("openvino_genai", reason="optional 'openvino' extra not installed")
    ov.reset_pipeline_cache()
    backend = OpenVINOLocalBackend(model="OpenVINO/whisper-base-fp16-ov", device="CPU", cpu_threads=2)

    health = backend.health()
    assert health.ok, health.detail
    assert "CPU" in health.detail

    result = backend.transcribe(audio(SAMPLE_MEDIUM), TranscribeOptions(language="en"))
    assert result.engine == "openvino_genai"
    assert result.duration == pytest.approx(4.91, abs=0.05)
    assert result.processing_ms > 0 and result.rtf is not None
    assert result.text.strip(), "whisper-base should find speech in a clean 5 s clip"
    assert result.segments and result.segments[0].end > result.segments[0].start
    assert assess(result, result.duration, TranscribeOptions()).quality in set(Quality)
