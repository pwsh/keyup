"""In-process transcription on Intel accelerators with OpenVINO GenAI (ARCHITECTURE §5.2).

The production host is a Core Ultra 7 258V (Lunar Lake): an Arc 140V iGPU on
``/dev/dri/renderD128`` and an "AI Boost" NPU on ``/dev/accel/accel0``.  ``openvino_genai``'s
``WhisperPipeline`` runs whisper on any of them — ``device`` is just a string, ``CPU``, ``GPU``,
``NPU`` or ``AUTO`` — so one backend class covers all three and the router picks by priority.

Two things differ from :mod:`app.stt.faster_whisper_local`, and both matter:

**No VAD.**  openvino-genai has no ``vad_filter``; it hands the whole clip to the decoder.  On
this corpus that is the "Thanks for watching!" failure mode (§1.5, 14 % of calls are under a
second of squelch tail).  The protection here is two-sided and neither half is whisper's own
VAD: a clip shorter than :data:`MIN_AUDIO_S` or quieter than :data:`SILENCE_RMS` never reaches
the decoder at all (empty text → ``postprocess.assess`` labels it ``noise``), and whatever does
reach it is still judged by the hallucination heuristics afterwards.  ``options.vad`` is
therefore honoured in spirit, not literally, and is never forwarded.

**Fewer confidence signals.**  ``WhisperDecodedResults`` carries chunk timings and text but no
per-segment ``avg_logprob`` or ``no_speech_prob``, so those stay ``None`` on every segment —
:func:`app.stt.postprocess.assess` skips the checks whose inputs are missing (it filters
``None`` out before comparing) and keeps the rest.  ``compression_ratio`` is computed here from
the segment text, so the repeat-loop check still fires.

Models are pre-exported OpenVINO IR, not HF transformers checkpoints.  ``model`` may be:

* an OpenVINO-format HF repo id, e.g. ``OpenVINO/whisper-large-v3-turbo-int8-ov`` or
  ``OpenVINO/whisper-base-fp16-ov`` — downloaded with ``huggingface_hub`` into ``cache_dir``;
* a local directory holding ``openvino_encoder_model.xml`` + friends.

To export a checkpoint that has no ready-made OpenVINO repo (optimum is a build-time tool and
is deliberately *not* a runtime dependency)::

    pip install 'optimum-intel[openvino]'
    optimum-cli export openvino --model openai/whisper-large-v3-turbo \\
        --weight-format int8 /srv/keyup/models/whisper-large-v3-turbo-int8-ov

then set ``"model": "/srv/keyup/models/whisper-large-v3-turbo-int8-ov"``.

The wheels are an optional extra (``pip install 'keyup[openvino]'``) and are imported
lazily, so the slim API image — which has no Intel GPU/NPU userspace at all — still imports
``app.stt``.  For that image the same class is served over HTTP by :mod:`app.stt.ov_server`
behind the ``openai_compatible`` kind; see ``docs/STT.md``.
"""

from __future__ import annotations

import array
import logging
import math
import os
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
import threading
import time
import zlib
from pathlib import Path
from typing import Any

from .base import BackendHealth, BackendUnavailableError, TranscriptionError
from .normalize import af_flags, note_filter_failure
from .types import TranscribeOptions, TranscriptResult, TranscriptSegment

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CACHE_DIR",
    "MIN_AUDIO_S",
    "SILENCE_RMS",
    "SUPPORTED_DEVICES",
    "OpenVINOLocalBackend",
    "available_devices",
    "decode_audio",
    "reset_pipeline_cache",
    "resolve_model_dir",
]

KIND = "openvino_genai"

SUPPORTED_DEVICES: tuple[str, ...] = ("CPU", "GPU", "NPU", "AUTO")
"""What ``device`` may be.  OpenVINO also accepts forms like ``AUTO:GPU,CPU`` and ``GPU.1``;
those pass through when the prefix before ``:`` or ``.`` is one of these."""

DEFAULT_CACHE_DIR = Path(
    os.environ.get("KEYUP_OPENVINO_CACHE", Path.home() / ".cache/keyup/openvino")
)
"""Where downloaded IR models and compiled-blob caches live.  A compiled blob cache is what
turns the NPU's ~30-60 s first-run compile into a sub-second load on every later start."""

SAMPLE_RATE = 16_000
"""Whisper's feature extractor is fixed at 16 kHz mono; ffmpeg resamples to it."""

MIN_AUDIO_S = 0.75
"""Shorter than this is a squelch tail, not speech — the same threshold ingest uses to decide
whether to enqueue a job at all (``TRANSCRIBE_MIN_SECONDS``).  Never decoded."""

SILENCE_RMS = 0.0015
"""RMS below this is an open squelch with no modulation.  Measured on this corpus: real traffic
sits at 0.02-0.20, the 0.42 s squelch tail that decodes as "Thanks for watching!" at ~0.0008."""

DEFAULT_MAX_NEW_TOKENS = 440
"""Whisper's decoder context is 448 tokens per 30 s window; this caps a runaway loop just under
it.  Long-form audio is processed window by window, so this is not a cap on the whole file."""

#: The marker every pre-exported OpenVINO whisper repo carries.
_IR_MARKER = "openvino_encoder_model.xml"

# Compiling a model for GPU/NPU costs seconds to a minute; one pipeline is shared per
# (model_dir, device, cpu_threads) across every worker thread, exactly as the CTranslate2
# backend shares its WhisperModel.
_PIPELINES: dict[tuple[str, str, int], Any] = {}
#: how long each of them took to compile, so health() can report the real number rather than
#: the near-zero cost of the cache hit it is actually measuring.
_LOAD_SECONDS: dict[tuple[str, str, int], float] = {}
_PIPELINES_LOCK = threading.Lock()


def reset_pipeline_cache() -> None:
    """Drop every compiled pipeline.  Test helper; also useful for an admin 'reload' action."""
    with _PIPELINES_LOCK:
        _PIPELINES.clear()
        _LOAD_SECONDS.clear()


# --------------------------------------------------------------------------- devices


def available_devices() -> list[str]:
    """Devices this OpenVINO install can see, e.g. ``['CPU', 'GPU', 'NPU']``.

    Never raises: on a host with no Intel userspace (or no openvino wheel) the answer is simply
    unknown, and the admin backends page would rather show that than a stack trace.
    """
    try:
        import openvino  # noqa: PLC0415 - optional extra, imported lazily

        return list(openvino.Core().available_devices)
    except Exception as exc:  # noqa: BLE001 - introspection helper, must never raise
        log.debug("openvino: cannot enumerate devices: %s", exc)
        return []


def _normalize_device(device: str) -> str:
    """Upper-case and validate a device string, keeping OpenVINO's ``AUTO:GPU,CPU`` forms."""
    value = (device or "CPU").strip().upper()
    head = value.split(":", 1)[0].split(".", 1)[0]
    if head not in SUPPORTED_DEVICES:
        raise ValueError(f"unsupported OpenVINO device {device!r}; expected one of {list(SUPPORTED_DEVICES)}")
    return value


# --------------------------------------------------------------------------- model resolution


def resolve_model_dir(model: str, cache_dir: Path | str | None = None) -> Path:
    """Return a local directory holding an OpenVINO IR whisper model.

    A path that exists is used as-is; anything else is treated as a Hugging Face repo id and
    downloaded into ``cache_dir``.  Either way the directory must contain OpenVINO IR — a
    transformers checkpoint has to be exported first (see the module docstring).
    """
    candidate = Path(model).expanduser()
    if candidate.is_dir():
        return _check_ir(candidate, model)
    if candidate.exists() or model.startswith((".", "/", "~")):
        raise BackendUnavailableError(f"OpenVINO model path {model!r} is not a directory")

    root = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415 - optional extra
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise BackendUnavailableError(
            "huggingface_hub is not installed; install the 'openvino' extra "
            "(pip install 'keyup[openvino]') or point 'model' at a local exported directory"
        ) from exc
    root.mkdir(parents=True, exist_ok=True)
    log.info("openvino: resolving model %s into %s", model, root)
    try:
        path = Path(snapshot_download(repo_id=model, cache_dir=str(root)))
    except Exception as exc:  # noqa: BLE001 - network / 404 / auth all mean "no model"
        raise BackendUnavailableError(
            f"could not download OpenVINO model {model!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return _check_ir(path, model)


def _check_ir(path: Path, model: str) -> Path:
    if (path / _IR_MARKER).is_file():
        return path
    raise BackendUnavailableError(
        f"{model!r} resolved to {path}, which holds no {_IR_MARKER}: it is not a pre-exported "
        "OpenVINO model.  Export one with: optimum-cli export openvino --model "
        "openai/whisper-large-v3-turbo --weight-format int8 <dir>, then set 'model' to <dir>."
    )


# --------------------------------------------------------------------------- pipeline cache


def _load_pipeline(model_dir: Path, device: str, cpu_threads: int, cache_dir: Path) -> Any:
    key = (str(model_dir), device, cpu_threads)
    with _PIPELINES_LOCK:
        cached = _PIPELINES.get(key)
        if cached is not None:
            return cached
        try:
            import openvino_genai  # noqa: PLC0415 - optional extra, imported lazily
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise BackendUnavailableError(
                "openvino-genai is not installed; install the 'openvino' extra "
                "(pip install 'keyup[openvino]') to transcribe on an Intel GPU/NPU"
            ) from exc

        # CACHE_DIR persists the compiled blob, which is the difference between a minute and a
        # second of start-up on NPU/GPU.  (OpenVINO warns that a cached blob is not guaranteed
        # to be readable by a different runtime version, which is why the image pins the
        # runtime and the wheel together; a stale blob is recompiled, not mis-executed.)
        properties: dict[str, Any] = {"CACHE_DIR": str(cache_dir / "compiled")}
        # CPU-plugin property only: AUTO forwards what it is given to whichever plugin it
        # picks, and the GPU plugin rejects it.
        if device.startswith("CPU") and cpu_threads:
            properties["INFERENCE_NUM_THREADS"] = int(cpu_threads)
        (cache_dir / "compiled").mkdir(parents=True, exist_ok=True)
        log.info("openvino: compiling %s for %s", model_dir.name, device)
        started = time.perf_counter()
        try:
            pipeline = openvino_genai.WhisperPipeline(str(model_dir), device, **properties)
        except Exception as exc:  # noqa: BLE001 - a missing driver surfaces here
            raise BackendUnavailableError(
                f"OpenVINO could not compile {model_dir.name} for device {device!r}: "
                f"{type(exc).__name__}: {exc} (devices seen: {available_devices() or 'none'})"
            ) from exc
        elapsed = time.perf_counter() - started
        log.info("openvino: %s ready on %s in %.1fs", model_dir.name, device, elapsed)
        _PIPELINES[key] = pipeline
        _LOAD_SECONDS[key] = elapsed
        return pipeline


def _supported_config_keys() -> set[str]:
    """Generation-config fields this openvino-genai build actually has.

    ``initial_prompt`` and ``hotwords`` arrived in 2025.x and unknown keys raise, so the
    request is filtered against the installed class rather than assumed.
    """
    try:
        import openvino_genai  # noqa: PLC0415 - optional extra

        return {name for name in dir(openvino_genai.WhisperGenerationConfig) if not name.startswith("_")}
    except Exception:  # noqa: BLE001 - filtering is best-effort
        return set()


# --------------------------------------------------------------------------- audio


def decode_audio(path: Path, timeout_s: float = 120.0, af: str | None = None) -> array.array:
    """Decode any ffmpeg-readable file to 16 kHz mono float32 samples.

    openvino-genai wants raw samples, not a filename, and ffmpeg is already a system
    dependency of this image (waveform peaks, the ingest header probe) — which is why there is
    no librosa/soundfile here.  ``array('f')`` is a buffer the pipeline accepts directly, so
    nothing is copied into a Python list.

    ``af`` is the loudness-normalization ``-af`` filter (:func:`app.stt.normalize.
    ffmpeg_audio_filter`); when set it is applied in this decode so the samples handed to the
    pipeline are normalized.  If ffmpeg fails *with* the filter the decode is retried once
    without it (logged once) rather than failing the job.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-i", str(path), *af_flags(af),
        "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise TranscriptionError("ffmpeg is not installed; it is required to decode audio") from exc
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError(f"ffmpeg timed out after {timeout_s:.0f}s on {path.name}") from exc
    if proc.returncode != 0:
        if af:  # the filter is the likely culprit — decode again clean before giving up
            note_filter_failure(af, proc.stderr.decode("utf-8", "replace").strip()[:300])
            return decode_audio(path, timeout_s=timeout_s, af=None)
        detail = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise TranscriptionError(f"ffmpeg failed on {path.name}: {detail}")
    samples = array.array("f")
    samples.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % samples.itemsize])
    return samples


def _rms(samples: array.array, budget: int = 20_000) -> float:
    """Root-mean-square amplitude, sub-sampled so the cost does not grow with clip length.

    A silence check does not need every sample: a stride over ~20 000 of them answers "is
    anything modulating this carrier" identically and in microseconds.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    stride = max(1, n // budget)
    total = 0.0
    count = 0
    for i in range(0, n, stride):
        value = samples[i]
        total += value * value
        count += 1
    return math.sqrt(total / count) if count else 0.0


def _compression_ratio(text: str) -> float | None:
    """gzip ratio of a segment, the signal whisper itself uses to spot a decoder loop.

    faster-whisper reports this per segment; openvino-genai does not, so it is recomputed the
    same way (raw bytes / deflated bytes) to keep ``postprocess.assess`` check #4 alive.
    """
    payload = text.strip().encode("utf-8")
    if len(payload) < 16:  # too short for the ratio to mean anything
        return None
    return len(payload) / max(1, len(zlib.compress(payload, 6)))


# --------------------------------------------------------------------------- backend


class OpenVINOLocalBackend:
    """Backend running OpenVINO GenAI whisper in this process, on CPU, iGPU or NPU."""

    kind = KIND

    def __init__(
        self,
        *,
        name: str = "openvino",
        model: str = "OpenVINO/whisper-base-fp16-ov",
        device: str = "CPU",
        cache_dir: Path | str | None = None,
        cpu_threads: int = 4,
        concurrency: int = 1,
        priority: int = 20,
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        min_audio_s: float = MIN_AUDIO_S,
        silence_rms: float = SILENCE_RMS,
        use_prompt: bool = True,
        use_hotwords: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.device = _normalize_device(device)
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
        self.cpu_threads = max(1, int(cpu_threads))
        # One accelerator, one job: a second concurrent generate() on the same compiled model
        # does not add throughput, it just splits the same silicon two ways.
        self.concurrency = max(1, int(concurrency))
        self.priority = int(priority)
        self.max_new_tokens = int(max_new_tokens)
        self.min_audio_s = float(min_audio_s)
        self.silence_rms = float(silence_rms)
        self.use_prompt = bool(use_prompt)
        self.use_hotwords = bool(use_hotwords)
        self._model_dir: Path | None = None

    # ------------------------------------------------------------------ plumbing

    def model_dir(self) -> Path:
        if self._model_dir is None:
            self._model_dir = resolve_model_dir(self.model, self.cache_dir)
        return self._model_dir

    def _cache_key(self) -> tuple[str, str, int]:
        return (str(self.model_dir()), self.device, self.cpu_threads)

    def _pipeline(self) -> Any:
        return _load_pipeline(self.model_dir(), self.device, self.cpu_threads, self.cache_dir)

    def _generation_kwargs(self, options: TranscribeOptions) -> dict[str, Any]:
        """Build the ``WhisperGenerationConfig`` fields for one request.

        ``options.vad`` has no counterpart here and is handled by the silence guard instead;
        ``word_timestamps`` needs the pipeline to be *constructed* with it, so a per-request
        ask cannot be honoured and is ignored rather than silently changing the shared pipeline.

        ``hotwords`` is off by default, and that default is measured, not cautious.
        openvino-genai injects ``initial_prompt`` into the *first* 30 s window only but
        ``hotwords`` into **every** window, so on short clips with little decodable speech the
        decoder simply continues the hotword list: over 15 stratified corpus files on
        whisper-large-v3-turbo-int8, hotwords turned 6 of them into "Riverton Lakeside North
        Fork Eastside Fairview Fork Eastside Fairview Fork…" loops and cut throughput from
        2.8x to 1.2x real time.  ``initial_prompt`` alone cost one file and ~30 % of the
        throughput.  Set ``use_hotwords`` if a future model tolerates them; ``postprocess``
        catches the failure either way (``prompt_echo`` / ``repeat_loop``), but catching it
        means the transcript is already lost.
        """
        kwargs: dict[str, Any] = {
            "task": "transcribe",
            "return_timestamps": True,
            "max_new_tokens": self.max_new_tokens,
        }
        if options.language:
            lang = options.language.strip()
            kwargs["language"] = lang if lang.startswith("<|") else f"<|{lang}|>"
        if options.temperature:
            kwargs["temperature"] = float(options.temperature)
            kwargs["do_sample"] = True
        supported = _supported_config_keys()
        if self.use_prompt and options.prompt and (not supported or "initial_prompt" in supported):
            kwargs["initial_prompt"] = options.prompt
        if self.use_hotwords and options.hotwords and (not supported or "hotwords" in supported):
            kwargs["hotwords"] = options.hotwords
        return kwargs

    # ------------------------------------------------------------------ transcription

    def transcribe(self, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        audio = Path(path)
        if not audio.is_file():
            raise TranscriptionError(f"audio file not found: {audio}", backend=self.name)

        started = time.perf_counter()
        samples = decode_audio(audio, timeout_s=options.timeout_s)
        duration = len(samples) / SAMPLE_RATE

        # The silence/duration guard reads the *un-normalized* level on purpose: normalization
        # would boost a squelch tail's noise floor and defeat the RMS check.  Only a clip that
        # is going to be decoded is decoded a second time with the loudness filter applied.
        guard = self._silence_guard(samples, duration)
        if guard is not None:
            return self._empty(duration, guard, started)
        if options.normalize_filter:
            samples = decode_audio(audio, timeout_s=options.timeout_s, af=options.normalize_filter)

        pipeline = self._pipeline()
        try:
            decoded = pipeline.generate(samples, **self._generation_kwargs(options))
        except Exception as exc:  # noqa: BLE001 - normalize engine errors into ours
            raise TranscriptionError(f"{self.name}: {type(exc).__name__}: {exc}", backend=self.name) from exc
        processing_ms = int((time.perf_counter() - started) * 1000)

        segments = [
            TranscriptSegment(
                start=float(chunk.start_ts),
                # A final chunk whose end is still open comes back as -1; clamp it to the clip.
                end=float(chunk.end_ts) if float(chunk.end_ts) >= 0 else duration,
                text=chunk.text,
                avg_logprob=None,
                no_speech_prob=None,
                compression_ratio=_compression_ratio(chunk.text),
            )
            for chunk in (getattr(decoded, "chunks", None) or [])
        ]
        texts = list(getattr(decoded, "texts", None) or [])
        text = (texts[0] if texts else "").strip()
        if not text and segments:
            text = "".join(s.text for s in segments).strip()

        return TranscriptResult(
            text=text,
            language=getattr(decoded, "language", None) or options.language,
            duration=duration,
            segments=segments,
            words=None,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=processing_ms,
            raw=None,
        )

    def _silence_guard(self, samples: array.array, duration: float) -> str | None:
        """Why this clip must not be decoded, or ``None`` to go ahead.

        This is the whole of the VAD substitute: openvino-genai has no ``vad_filter``, and a
        sub-second squelch tail fed to whisper comes back as "Thanks for watching!".
        """
        if duration < self.min_audio_s:
            return f"too short: {duration:.2f}s < {self.min_audio_s:.2f}s"
        level = _rms(samples)
        if level < self.silence_rms:
            return f"silent: rms {level:.5f} < {self.silence_rms:.5f}"
        return None

    def _empty(self, duration: float, reason: str, started: float) -> TranscriptResult:
        """An empty transcript, which ``postprocess.assess`` labels ``noise`` (``empty_text``)."""
        log.debug("openvino %s: skipping decode (%s)", self.name, reason)
        return TranscriptResult(
            text="",
            language=None,
            duration=duration,
            segments=[],
            words=None,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=int((time.perf_counter() - started) * 1000),
            raw={"skipped": reason},
        )

    # ------------------------------------------------------------------ health

    def health(self) -> BackendHealth:
        """Compile the pipeline (once) and report the device it landed on.

        Deliberately not cheap the first time: a model that cannot compile for this device is
        exactly what the router needs to learn before it sends the device a job.
        """
        started = time.perf_counter()
        try:
            self._pipeline()
        except Exception as exc:  # noqa: BLE001 - health() must never raise
            return BackendHealth(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        devices = available_devices()
        # After the first probe this method is a dictionary lookup, so the compile time it
        # reports has to come from when the compile actually happened.
        load_s = _LOAD_SECONDS.get(self._cache_key())
        return BackendHealth(
            ok=True,
            latency_ms=elapsed_ms,
            detail=(
                f"{self.model} ready on {self.device}"
                + (f" (compiled in {load_s:.1f}s)" if load_s is not None else "")
                + f"; available devices: {', '.join(devices) if devices else 'unknown'}"
            ),
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpenVINOLocalBackend {self.name} {self.model} {self.device}>"
