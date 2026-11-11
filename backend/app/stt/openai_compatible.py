"""OpenAI-compatible speech-to-text backend.

Talks to any server implementing the OpenAI Audio API — Speaches on a GPU host, a
CPU container, or a dedicated box (ARCHITECTURE §5.2).  Only ``httpx`` is required, so this backend works
in the slim container image with no CTranslate2 wheel.
"""

from __future__ import annotations

import logging
import mimetypes
import random
import time
from pathlib import Path
from typing import Any

import httpx

from .base import (
    BackendHealth,
    BackendUnavailableError,
    ModelNotInstalledError,
    RequestTimeoutError,
    TranscriptionError,
)
from .types import TranscribeOptions, TranscriptResult, TranscriptSegment, TranscriptWord

log = logging.getLogger(__name__)

__all__ = ["OpenAICompatibleBackend"]

KIND = "openai_compatible"

_DEFAULT_MIME = "audio/mpeg"

#: transport failures that mean the request never reached a server: refused, unresolvable,
#: unroutable, or no pooled connection to be had.  Everything else — a read or write that
#: stalled, a connection dropped mid-response, an HTTP status — happened *at* the server.
_UNREACHABLE: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or _DEFAULT_MIME


class OpenAICompatibleBackend:
    """Backend speaking ``POST {base_url}/v1/audio/transcriptions`` (multipart)."""

    kind = KIND

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        concurrency: int = 4,
        priority: int = 100,
        attempts: int = 3,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 8.0,
        health_timeout_s: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.concurrency = max(1, int(concurrency))
        self.priority = int(priority)
        self.attempts = max(1, int(attempts))
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.health_timeout_s = health_timeout_s
        self._client = client

    # ------------------------------------------------------------------ plumbing

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _http(self, timeout: float) -> tuple[httpx.Client, bool]:
        """Return ``(client, owned)``; the caller closes it only when ``owned``."""
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=timeout), True

    def _sleep_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter, so parallel workers do not retry in lockstep."""
        window = min(self.backoff_max_s, self.backoff_base_s * (2**attempt))
        return random.uniform(0, window)  # noqa: S311 - jitter, not cryptography

    # ------------------------------------------------------------------ request

    def _build_form(self, options: TranscribeOptions) -> dict[str, str | list[str]]:
        """Build the multipart text fields.

        ``timestamp_granularities[]`` is a repeated field (httpx encodes a list value as one
        part per item), spelled with the OpenAI ``[]`` suffix that Speaches also accepts.
        """
        granularities = ["segment"]
        if options.word_timestamps:
            granularities.append("word")
        form: dict[str, str | list[str]] = {
            "model": self.model,
            "response_format": "verbose_json",
            "temperature": str(options.temperature),
            "vad_filter": "true" if options.vad else "false",
            "timestamp_granularities[]": granularities,
        }
        if options.language:
            form["language"] = options.language
        if options.prompt:
            form["prompt"] = options.prompt
        # Omitted entirely when unset: Speaches treats an empty hotwords string as a real
        # (empty) bias list rather than "no hotwords".
        if options.hotwords:
            form["hotwords"] = options.hotwords
        return form

    def _transcribe_url(self) -> str:
        """The endpoint one clip is POSTed to.  Overridden by dialects on a different path."""
        return f"{self.base_url}/v1/audio/transcriptions"

    def _prepare_upload(
        self,
        audio: Path,
        options: TranscribeOptions,
    ) -> tuple[str, bytes, str]:
        """Return the ``(filename, bytes, content-type)`` of the file part.

        A hook so a dialect whose server accepts fewer container formats can transcode first
        (see :mod:`app.stt.ovms`).  The OpenAI shape uploads the recording untouched — unless
        loudness normalization is on, in which case the clip is transcoded to 16 kHz mono wav
        with the ``-af`` filter applied first, so the server (Speaches, say) receives
        normalized audio like every other backend (§5.2).
        """
        if options.normalize_filter:
            from .normalize import transcode_to_wav  # noqa: PLC0415 - avoid an import at module load

            payload = transcode_to_wav(audio, af=options.normalize_filter, timeout_s=options.timeout_s)
            return f"{audio.stem}.wav", payload, "audio/wav"
        return audio.name, audio.read_bytes(), _guess_mime(audio)

    def transcribe(self, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        audio = Path(path)
        if not audio.is_file():
            raise TranscriptionError(f"audio file not found: {audio}", backend=self.name)
        filename, payload, mime = self._prepare_upload(audio, options)
        form = self._build_form(options)
        url = self._transcribe_url()
        client, owned = self._http(options.timeout_s)
        started = time.perf_counter()
        last_error: Exception | None = None
        # Did every try get as far as the server?  One connect failure among them and the
        # give-up below is about reachability, not about this recording (BackendError.reached).
        reached = True
        try:
            for attempt in range(self.attempts):
                files = {"file": (filename, payload, mime)}
                try:
                    resp = client.post(
                        url, data=form, files=files, headers=self._headers, timeout=options.timeout_s
                    )
                except httpx.ReadTimeout as exc:
                    # The server took the upload and then sat on it for the whole deadline.
                    # Not retried here: see RequestTimeoutError.  (2026-09-23: call 5237413
                    # held a slot for 3 x 120 s on each of three servers, every round.)
                    log.warning(
                        "stt %s: no answer within %.0fs (attempt %d); not retrying on this server",
                        self.name,
                        options.timeout_s,
                        attempt + 1,
                    )
                    raise RequestTimeoutError(
                        f"{self.name}: no answer within {options.timeout_s:.0f}s: {exc or 'timed out'}",
                        backend=self.name,
                    ) from exc
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    last_error = exc
                    if isinstance(exc, _UNREACHABLE):
                        reached = False
                    log.warning("stt %s: transport error (attempt %d): %s", self.name, attempt + 1, exc)
                else:
                    if resp.status_code == 404:
                        raise ModelNotInstalledError(
                            self._detail(resp) or f"model {self.model!r} is not installed on {self.base_url}",
                            backend=self.name,
                            model=self.model,
                        )
                    if resp.status_code >= 500:
                        last_error = TranscriptionError(
                            f"HTTP {resp.status_code} from {self.name}: {self._detail(resp)}",
                            backend=self.name,
                        )
                        log.warning("stt %s: HTTP %s (attempt %d)", self.name, resp.status_code, attempt + 1)
                    elif resp.status_code >= 400:
                        # 4xx other than 404 is a bad request: retrying cannot help.
                        raise TranscriptionError(
                            f"HTTP {resp.status_code} from {self.name}: {self._detail(resp)}",
                            backend=self.name,
                        )
                    else:
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        return self._to_result(resp.json(), elapsed_ms, audio)
                if attempt + 1 < self.attempts:
                    time.sleep(self._sleep_for(attempt))
        finally:
            if owned:
                client.close()
        raise BackendUnavailableError(
            f"{self.name}: giving up after {self.attempts} attempt(s): {last_error}",
            backend=self.name,
            reached=reached,
        ) from last_error

    @staticmethod
    def _detail(resp: httpx.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:500]
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error") or body
            return detail if isinstance(detail, str) else str(detail)[:500]
        return str(body)[:500]

    # ------------------------------------------------------------------ mapping

    def _to_result(
        self,
        body: dict[str, Any],
        processing_ms: int,
        audio: Path,  # noqa: ARG002 - dialects that get no duration back read it off the file
    ) -> TranscriptResult:
        segments = [TranscriptSegment.model_validate(s) for s in (body.get("segments") or [])]
        raw_words = body.get("words")
        if not raw_words:
            # Some servers only nest words inside segments when word granularity is asked for.
            raw_words = [w for s in (body.get("segments") or []) for w in (s.get("words") or [])]
        words = [TranscriptWord.model_validate(w) for w in raw_words] or None
        # Note: when vad_filter empties a clip entirely the server reports a junk language
        # ('nn' was observed on a 0.42 s squelch tail).  Kept verbatim; postprocess.assess()
        # labels the empty text as noise, which is the signal that actually matters.
        return TranscriptResult(
            text=(body.get("text") or "").strip(),
            language=body.get("language"),
            duration=body.get("duration"),
            segments=segments,
            words=words,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=processing_ms,
            raw=body,
        )

    # ------------------------------------------------------------------ health

    def health(self) -> BackendHealth:
        client, owned = self._http(self.health_timeout_s)
        started = time.perf_counter()
        try:
            resp = client.get(
                f"{self.base_url}/v1/models", headers=self._headers, timeout=self.health_timeout_s
            )
            latency_ms = (time.perf_counter() - started) * 1000
            if resp.status_code != 200:
                return BackendHealth(
                    ok=False, latency_ms=latency_ms, detail=f"HTTP {resp.status_code} from /v1/models"
                )
            data = resp.json().get("data") or []
            ids = {m.get("id") for m in data if isinstance(m, dict)}
            if self.model not in ids:
                return BackendHealth(
                    ok=False,
                    latency_ms=latency_ms,
                    detail=f"model {self.model!r} not installed (have: {sorted(i for i in ids if i)})",
                )
            return BackendHealth(ok=True, latency_ms=latency_ms, detail=f"{len(ids)} model(s) installed")
        except Exception as exc:  # noqa: BLE001 - health() must never raise
            return BackendHealth(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if owned:
                client.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpenAICompatibleBackend {self.name} {self.base_url} {self.model}>"
