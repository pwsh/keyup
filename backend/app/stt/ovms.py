"""OpenVINO Model Server (OVMS) speech-to-text backend (ARCHITECTURE §5.2).

Intel's own inference server — ``openvino/model_server`` — serves whisper on the production
host's Arc 140V iGPU *and* on its AI Boost NPU, which is the one thing our own
``stt-openvino`` sidecar cannot do (openvino-genai's NPU whisper path throws on every clip on
this SKU; ``docs/STT.md``).  It speaks a *subset* of the OpenAI Audio API on a different
path, so it gets its own kind rather than being configured as ``openai_compatible``:

``POST {base_url}/v3/audio/transcriptions``  (note: ``/v3``, not ``/v1``)

What OVMS accepts, measured against ``openvino/model_server:weekly`` (2026-08-21):

======================  =======================================================================
``model``               required — the server's ``--model_name``, *not* the HF repo id.
                        A name it does not serve answers **404** ("Mediapipe graph definition
                        with requested name is not found"), which is exactly the signal
                        :class:`~app.stt.base.ModelNotInstalledError` exists for.
``file``                **mp3 or wav only.**  Anything else — trunk-recorder's m4a included —
                        is a 400 ("Received input file is not valid wav nor mp3 audio file"),
                        so :meth:`OVMSBackend._prepare_upload` transcodes first.
``language``            forwarded.
``temperature``         **OVMS defaults it to 1.0**, i.e. sampling, when the field is absent.
                        This backend therefore never omits it; the option's own default is 0.
``timestamp_granularities[]``
                        only ``word``, and only on a model exported with
                        ``enable_word_timestamps`` — otherwise a 400.  Sent only when the
                        caller asked for word timestamps.
``prompt``, ``response_format``, ``hotwords``, ``vad_filter``
                        **silently ignored** (measured: no error, and the answer is unchanged).
                        Not sent, because a field the server drops on the floor is a lie in
                        the request log.
======================  =======================================================================

The answer is ``{"text": "..."}`` and nothing else: no ``segments``, no ``avg_logprob``, no
``no_speech_prob``, no ``duration``.  Three consequences, all of them handled here:

* the transcript becomes **one segment spanning the whole call**, whose length comes from
  ``ffprobe`` on the original recording (OVMS reports no duration, and the router's RTF
  bookkeeping and ``postprocess.assess`` both want one);
* ``avg_logprob`` and ``no_speech_prob`` stay ``None``, so the low-confidence and
  all-non-speech checks in :func:`app.stt.postprocess.assess` do not vote — it says so, on
  every such transcript, with the ``no_model_metrics`` reason;
* ``compression_ratio`` is recomputed from the text with zlib, exactly as
  :mod:`app.stt.openvino_local` does, so the repeat-loop check survives.

**There is no VAD anywhere in this path.**  OVMS has no ``vad_filter`` and whisper hallucinates
"Thanks for watching!" on a squelch tail, so ``options.vad`` is honoured by the same duration
guard :mod:`app.stt.openvino_local` uses: a clip shorter than :data:`MIN_AUDIO_S` is never sent.

Everything else — retries on 5xx and transport errors, backoff with jitter, the ``/v1/models``
health probe (OVMS answers that in the OpenAI shape) — is inherited unchanged from
:class:`~app.stt.openai_compatible.OpenAICompatibleBackend`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
import threading
import zlib
from pathlib import Path
from typing import Any

from .base import TranscriptionError
from .normalize import transcode_to_wav as normalize_transcode_to_wav
from .openai_compatible import OpenAICompatibleBackend
from .types import TranscribeOptions, TranscriptResult, TranscriptSegment, TranscriptWord

log = logging.getLogger(__name__)

__all__ = ["MIN_AUDIO_S", "NATIVE_SUFFIXES", "OVMSBackend", "compression_ratio", "probe_duration"]

KIND = "ovms"

NATIVE_SUFFIXES: frozenset[str] = frozenset({".mp3", ".wav"})
"""The only two containers OVMS decodes.  Everything else is transcoded to wav first."""

SAMPLE_RATE = 16_000
"""Whisper's feature extractor is fixed at 16 kHz mono; transcoding to anything else just
makes OVMS resample it again."""

MIN_AUDIO_S = 0.75
"""Shorter than this is a squelch tail, not speech — the same threshold ingest uses to decide
whether to enqueue a job (``TRANSCRIBE_MIN_SECONDS``).  Never sent: OVMS has no VAD, and
whisper answers a 0.42 s tail with "Thanks for watching!"."""


def probe_duration(path: Path, timeout_s: float = 30.0) -> float | None:
    """Length of ``path`` in seconds per ffprobe, or ``None`` when it cannot say.

    OVMS returns no duration, and the router's real-time-factor bookkeeping, the whole-call
    segment and ``assess``'s words-per-second check all want one.  ffprobe rather than a
    header read because trunk-recorder's m4a comes out of whatever AAC encoder the host had.
    """
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, the path is the only variable
            [binary, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        duration = float(out.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def compression_ratio(text: str) -> float | None:
    """gzip ratio of a transcript, the signal whisper itself uses to spot a decoder loop.

    OVMS reports none, so it is recomputed the same way faster-whisper computes it (raw bytes
    over deflated bytes) to keep ``postprocess.assess`` check #4 alive.
    """
    payload = text.strip().encode("utf-8")
    if len(payload) < 16:  # too short for the ratio to mean anything
        return None
    return len(payload) / max(1, len(zlib.compress(payload, 6)))


def transcode_to_wav(path: Path, timeout_s: float = 120.0, af: str | None = None) -> bytes:
    """Decode any ffmpeg-readable file to 16 kHz mono 16-bit wav bytes.

    OVMS rejects everything that is not mp3 or wav, and trunk-recorder writes m4a by default
    (``TR_PREFER_AUDIO=m4a``, §1.7).  ffmpeg is already a system dependency of this image.

    ``af`` is the loudness-normalization ``-af`` filter (:func:`app.stt.normalize.
    ffmpeg_audio_filter`); when set it is applied in this same decode so OVMS receives
    normalized audio.  The shared helper falls back to an un-normalized decode if ffmpeg
    cannot run the filter.
    """
    return normalize_transcode_to_wav(path, sample_rate=SAMPLE_RATE, af=af, timeout_s=timeout_s)


class OVMSBackend(OpenAICompatibleBackend):
    """Backend speaking OpenVINO Model Server's ``POST {base_url}/v3/audio/transcriptions``."""

    kind = KIND

    def __init__(self, *, min_audio_s: float = MIN_AUDIO_S, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.min_audio_s = float(min_audio_s)
        # ffprobe answers once per job, in transcribe(); _to_result() reads it back here.
        # Thread-local because the router runs several jobs against one backend instance.
        self._local = threading.local()

    # ------------------------------------------------------------------ request

    def _transcribe_url(self) -> str:
        return f"{self.base_url}/v3/audio/transcriptions"

    def _build_form(self, options: TranscribeOptions) -> dict[str, str | list[str]]:
        """Exactly the four fields OVMS reads, plus word granularity when it was asked for.

        ``prompt``/``hotwords``/``vad_filter``/``response_format`` are deliberately absent:
        OVMS accepts and then ignores them, which is worse than not sending them — it would
        read like the domain prompt were in play when it never is.
        """
        form: dict[str, str | list[str]] = {
            "model": self.model,
            # Never omitted: absent means 1.0 (sampling) to OVMS, and measurably worse text.
            "temperature": str(options.temperature),
        }
        if options.language:
            form["language"] = options.language
        if options.word_timestamps:
            # Only ``word`` is useful here — ``segment`` changes the decode on some models —
            # and it 400s unless the model was exported with ``enable_word_timestamps``.
            form["timestamp_granularities[]"] = ["word"]
        return form

    def _prepare_upload(self, audio: Path, options: TranscribeOptions) -> tuple[str, bytes, str]:
        suffix = audio.suffix.lower()
        # A native wav/mp3 is normally uploaded untouched, but normalization has to happen
        # *before* transcription for every format — so when a filter is set it forces a
        # transcode even for the containers OVMS would otherwise decode itself.
        if options.normalize_filter is None and suffix in NATIVE_SUFFIXES:
            mime = "audio/wav" if suffix == ".wav" else "audio/mpeg"
            return audio.name, audio.read_bytes(), mime
        log.debug("ovms %s: transcoding %s to 16 kHz mono wav", self.name, audio.name)
        payload = transcode_to_wav(audio, timeout_s=options.timeout_s, af=options.normalize_filter)
        return f"{audio.stem}.wav", payload, "audio/wav"

    # ------------------------------------------------------------------ transcription

    def transcribe(self, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        """Transcribe one clip, refusing sub-second squelch tails before they reach whisper."""
        audio = Path(path)
        if not audio.is_file():
            raise TranscriptionError(f"audio file not found: {audio}", backend=self.name)
        duration = probe_duration(audio)
        self._local.duration = duration
        if options.vad and duration is not None and duration < self.min_audio_s:
            return self._empty(audio, duration)
        return super().transcribe(audio, options)

    def _empty(self, audio: Path, duration: float) -> TranscriptResult:
        """An empty transcript, which ``postprocess.assess`` labels ``noise`` (``empty_text``)."""
        log.debug("ovms %s: skipping %s (%.2fs < %.2fs)", self.name, audio.name, duration, self.min_audio_s)
        return TranscriptResult(
            text="",
            language=None,
            duration=duration,
            segments=[],
            words=None,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=0,
            raw={"skipped": f"too short: {duration:.2f}s < {self.min_audio_s:.2f}s"},
        )

    # ------------------------------------------------------------------ mapping

    def _to_result(self, body: dict[str, Any], processing_ms: int, audio: Path) -> TranscriptResult:
        """Map ``{"text": ...}`` onto the shape every other backend returns.

        One segment spans the whole call because that is genuinely all OVMS knows: it does not
        segment, and inventing boundaries would be worse than admitting there are none.
        """
        text = (body.get("text") or "").strip()
        # transcribe() probed it on the way in; a direct _to_result() call (tests) probes now.
        duration = getattr(self._local, "duration", None)
        if duration is None:
            duration = probe_duration(audio)

        raw_segments = body.get("segments") or []
        if raw_segments:
            # Not asked for, but a future OVMS build (or a segment-capable graph) may send them.
            segments = [
                TranscriptSegment(
                    start=float(s.get("start") or 0.0),
                    end=float(s.get("end") or duration or 0.0),
                    text=s.get("text") or "",
                    avg_logprob=None,
                    no_speech_prob=None,
                    compression_ratio=compression_ratio(s.get("text") or ""),
                )
                for s in raw_segments
            ]
        elif text:
            segments = [
                TranscriptSegment(
                    start=0.0,
                    end=float(duration or 0.0),
                    text=text,
                    avg_logprob=None,
                    no_speech_prob=None,
                    compression_ratio=compression_ratio(text),
                )
            ]
        else:
            segments = []

        raw_words = body.get("words") or [w for s in raw_segments for w in (s.get("words") or [])]
        words = [TranscriptWord.model_validate(w) for w in raw_words] or None

        return TranscriptResult(
            text=text,
            language=body.get("language"),
            duration=duration,
            segments=segments,
            words=words,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=processing_ms,
            raw=body,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OVMSBackend {self.name} {self.base_url} {self.model}>"
