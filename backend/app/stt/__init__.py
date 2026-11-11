"""Keyup speech-to-text package (ARCHITECTURE §5.2).

Pluggable transcription backends behind one interface, a priority/health router, hallucination
post-processing and a DB-agnostic job loop.

Importing this package requires only ``httpx`` and ``pydantic``.  The local CPU backend needs the
optional ``faster-whisper`` extra and is therefore imported lazily by
:func:`app.stt.router.build_backends`; ``app.stt.faster_whisper_local`` may be imported directly
too — it only fails when a model is actually loaded.
"""

from __future__ import annotations

from .base import (
    BackendError,
    BackendHealth,
    BackendUnavailableError,
    ModelNotInstalledError,
    STTBackend,
    TranscriptionError,
)
from .openai_compatible import OpenAICompatibleBackend
from .ovms import OVMSBackend
from .postprocess import assess, collapse_repeats, normalize_text
from .prompt import build_hotwords, build_prompt
from .router import BackendRouter, NoBackendAvailableError, build_backends
from .runner import Job, JobRunner, run_jobs
from .types import (
    Quality,
    QualityAssessment,
    TranscribeOptions,
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
)

__all__ = [
    "BackendError",
    "BackendHealth",
    "BackendRouter",
    "BackendUnavailableError",
    "Job",
    "JobRunner",
    "ModelNotInstalledError",
    "NoBackendAvailableError",
    "OVMSBackend",
    "OpenAICompatibleBackend",
    "Quality",
    "QualityAssessment",
    "STTBackend",
    "TranscribeOptions",
    "TranscriptResult",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionError",
    "assess",
    "build_backends",
    "build_hotwords",
    "build_prompt",
    "collapse_repeats",
    "normalize_text",
    "run_jobs",
]
