"""Backend protocol and shared errors.

A backend is a small synchronous object.  Synchronous on purpose: both implementations are
blocking (an httpx request, a CTranslate2 call), so the router runs them in a thread pool and
the async API layer stays free of engine-specific plumbing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .types import TranscribeOptions, TranscriptResult

__all__ = [
    "BackendError",
    "BackendHealth",
    "BackendUnavailableError",
    "ModelNotInstalledError",
    "RequestTimeoutError",
    "STTBackend",
    "TranscriptionError",
]


class BackendHealth(BaseModel):
    """Outcome of a cheap liveness probe."""

    ok: bool
    latency_ms: float | None = None
    detail: str = ""


class BackendError(RuntimeError):
    """Base class for every failure raised by an STT backend.

    ``reached`` says whether the server *answered this request* (or accepted it and then sat
    on it past the deadline), as opposed to never being reachable at all.  The router needs
    the difference to tell an outage from a recording every backend chokes on: a refused
    connection says nothing about the audio, a read timeout on a server that is busily
    transcribing everybody else's calls says a great deal (``app.stt.router``).
    """

    #: the class default; an instance may say otherwise (see ``BackendUnavailableError``)
    reached: bool = False

    def __init__(self, message: str, *, backend: str = "", reached: bool | None = None) -> None:
        super().__init__(message)
        self.backend = backend
        if reached is not None:
            self.reached = reached


class BackendUnavailableError(BackendError):
    """The backend could not be reached, or gave up after exhausting its retries.

    ``reached=True`` when every one of those retries got as far as the server (HTTP 5xx,
    a dropped response) rather than failing to connect.
    """


class ModelNotInstalledError(BackendError):
    """The server is reachable but does not have the requested model.

    Speaches answers with HTTP 404 and a body like::

        {"detail": "Model 'Systran/faster-whisper-medium' is not installed locally.
                    You can download the model using `POST /v1/models`"}

    This is a configuration error, never a transient one, so the router must not retry or fail
    over on the assumption the next attempt will work — it disqualifies the backend instead.
    """

    def __init__(self, message: str, *, backend: str = "", model: str = "") -> None:
        super().__init__(message, backend=backend)
        self.model = model


class TranscriptionError(BackendError):
    """The backend was reachable but the transcription itself failed."""

    reached = True


class RequestTimeoutError(TranscriptionError):
    """The server accepted the request and did not answer within the client's deadline.

    Raised at once rather than retried on the same server: the deadline is already scaled
    to the clip (``app.stt.worker.request_timeout_s``), so a server that used all of it on
    this file will very likely use all of it again, and each retry holds one of its slots
    for the whole deadline.  The router fails over to the next backend instead.
    """


@runtime_checkable
class STTBackend(Protocol):
    """What the router needs from any speech-to-text engine."""

    name: str
    """Configured instance name, unique within a deployment (e.g. ``whisper``)."""

    kind: str
    """Implementation family: ``openai_compatible`` | ``faster_whisper_local`` | ``openvino_genai``."""

    model: str
    """Model identifier as the engine knows it."""

    concurrency: int
    """Maximum simultaneous in-flight transcriptions for this backend."""

    priority: int
    """Higher wins.  The router prefers the highest-priority healthy backend with a free slot."""

    def transcribe(self, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        """Transcribe one audio file.  Blocking; raises a :class:`BackendError` subclass."""
        ...

    def health(self) -> BackendHealth:
        """Cheap liveness + model-availability probe.  Must not raise."""
        ...
