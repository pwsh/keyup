"""In-process CPU transcription with faster-whisper (CTranslate2).

Lets the Keyup host transcribe on its own when the GPU box is down (ARCHITECTURE §5.2).  The
``faster_whisper`` wheel is an optional extra (``pip install keyup[cpu]``) and is imported
lazily, so ``import app.stt`` works in the slim image with only httpx + pydantic installed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .base import BackendHealth, BackendUnavailableError, TranscriptionError
from .normalize import transcode_to_wav
from .types import TranscribeOptions, TranscriptResult, TranscriptSegment, TranscriptWord

log = logging.getLogger(__name__)

__all__ = ["FasterWhisperLocalBackend", "reset_model_cache"]

KIND = "faster_whisper_local"

# Future kinds slot in beside this one rather than inside it: an ``openvino_local`` backend
# (OpenVINO GenAI whisper on the Core Ultra iGPU/NPU) would implement the same STTBackend
# protocol with device="NPU", and the router would pick it by priority with no other change.

# Loading a model costs seconds and hundreds of MB, so one instance is shared per
# (model, device, compute_type, cpu_threads) across every worker thread in the process.
_MODELS: dict[tuple[str, str, str, int], Any] = {}
_MODELS_LOCK = threading.Lock()


def reset_model_cache() -> None:
    """Drop every cached model.  Test helper; also useful for an admin 'reload' action."""
    with _MODELS_LOCK:
        _MODELS.clear()


def _load_whisper_model(model: str, device: str, compute_type: str, cpu_threads: int) -> Any:
    key = (model, device, compute_type, cpu_threads)
    with _MODELS_LOCK:
        cached = _MODELS.get(key)
        if cached is not None:
            return cached
        try:
            from faster_whisper import WhisperModel  # noqa: PLC0415 - optional extra, imported lazily
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise BackendUnavailableError(
                "faster-whisper is not installed; install the 'cpu' extra "
                "(pip install 'keyup[cpu]') to use a local STT backend"
            ) from exc
        log.info("stt: loading %s on %s (%s, %d threads)", model, device, compute_type, cpu_threads)
        instance = WhisperModel(model, device=device, compute_type=compute_type, cpu_threads=cpu_threads)
        _MODELS[key] = instance
        return instance


class FasterWhisperLocalBackend:
    """Backend running CTranslate2 in this process."""

    kind = KIND

    def __init__(
        self,
        *,
        name: str = "local",
        model: str = "Systran/faster-whisper-small",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        concurrency: int = 1,
        priority: int = 10,
        beam_size: int = 5,
    ) -> None:
        self.name = name
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = max(1, int(cpu_threads))
        # CTranslate2 already saturates cpu_threads cores; running two transcriptions at once
        # on the same model just makes both slower, so the default is a single slot.
        self.concurrency = max(1, int(concurrency))
        self.priority = int(priority)
        self.beam_size = beam_size

    def _model(self) -> Any:
        return _load_whisper_model(self.model, self.device, self.compute_type, self.cpu_threads)

    def transcribe(self, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        audio = Path(path)
        if not audio.is_file():
            raise TranscriptionError(f"audio file not found: {audio}", backend=self.name)
        whisper = self._model()
        started = time.perf_counter()
        # faster-whisper decodes the file itself, so loudness normalization has to happen
        # first: transcode to a normalized 16 kHz mono wav and hand it that instead.  The
        # temp file is removed in the finally below; the stored recording is never touched.
        decode_path = audio
        normalized_tmp: str | None = None
        if options.normalize_filter:
            wav = transcode_to_wav(audio, af=options.normalize_filter, timeout_s=options.timeout_s)
            fd, normalized_tmp = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as handle:
                handle.write(wav)
            decode_path = Path(normalized_tmp)
        try:
            segments_iter, info = whisper.transcribe(
                str(decode_path),
                language=options.language,
                initial_prompt=options.prompt,
                hotwords=options.hotwords or None,
                vad_filter=options.vad,
                word_timestamps=options.word_timestamps,
                temperature=options.temperature,
                beam_size=self.beam_size,
            )
            # faster-whisper is lazy: the work happens while this generator is drained.
            raw_segments = list(segments_iter)
        except Exception as exc:  # noqa: BLE001 - normalize engine errors into ours
            raise TranscriptionError(f"{self.name}: {type(exc).__name__}: {exc}", backend=self.name) from exc
        finally:
            if normalized_tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(normalized_tmp)
        processing_ms = int((time.perf_counter() - started) * 1000)

        segments: list[TranscriptSegment] = []
        words: list[TranscriptWord] = []
        for seg in raw_segments:
            segments.append(
                TranscriptSegment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text,
                    avg_logprob=_opt_float(getattr(seg, "avg_logprob", None)),
                    no_speech_prob=_opt_float(getattr(seg, "no_speech_prob", None)),
                    compression_ratio=_opt_float(getattr(seg, "compression_ratio", None)),
                )
            )
            for word in getattr(seg, "words", None) or []:
                words.append(
                    TranscriptWord(
                        start=float(word.start),
                        end=float(word.end),
                        word=word.word,
                        probability=_opt_float(getattr(word, "probability", None)),
                    )
                )

        return TranscriptResult(
            text="".join(s.text for s in segments).strip(),
            language=getattr(info, "language", None),
            duration=_opt_float(getattr(info, "duration", None)),
            segments=segments,
            words=words or None,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=processing_ms,
            raw=None,
        )

    def health(self) -> BackendHealth:
        started = time.perf_counter()
        try:
            self._model()
        except Exception as exc:  # noqa: BLE001 - health() must never raise
            return BackendHealth(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return BackendHealth(
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=f"{self.model} loaded ({self.device}/{self.compute_type}, {self.cpu_threads} threads)",
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FasterWhisperLocalBackend {self.name} {self.model} {self.device}/{self.compute_type}>"


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)
