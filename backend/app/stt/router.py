"""Backend selection, failover and health/throughput bookkeeping (ARCHITECTURE §5.2).

Several backends can be configured with priorities; this router picks the lowest-numbered-priority
healthy one with a free concurrency slot, fails over to the next on error, and keeps the
per-backend counters the admin "backends" page reports.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import BackendError, BackendHealth, ModelNotInstalledError, STTBackend
from .types import TranscribeOptions, TranscriptResult

log = logging.getLogger(__name__)

__all__ = [
    "BackendRouter",
    "BackendStats",
    "NoBackendAvailableError",
    "NoBackendEnabledError",
    "RequestFailedEverywhereError",
    "build_backends",
]

_RTF_WINDOW = 50

#: width of the "what happened lately" windows the admin page reports (seconds)
RECENT_WINDOW_S = 60.0

#: how long a router trusts the operator's enable/disable overrides it last read (seconds).
#: Short, because the operator is watching the page they just clicked on; not zero, because
#: this is read on the selection path of every job.
OVERRIDE_TTL_S = 5.0

#: How recent a backend's last success (on any job) must be for its failure on *this* job to
#: count as evidence against the recording rather than against the server (seconds before
#: the request started; a success while it was in flight counts too).  Ten minutes is long
#: past the gap between two successes on a server doing real work — the three ports
#: of one GPU server each finish several calls a minute — and short enough that a server which has
#: quietly stopped answering everybody is not mistaken for a healthy one.
SUCCESS_EVIDENCE_S = 600.0


class NoBackendAvailableError(BackendError):
    """Every configured backend was unhealthy, disqualified or failed."""


class RequestFailedEverywhereError(BackendError):
    """Every backend that tried this request reached its server and failed *on it*.

    Deliberately **not** a :class:`NoBackendAvailableError`: the servers are up — at least
    one of them transcribed somebody else's call within :data:`SUCCESS_EVIDENCE_S` — and it
    is this recording they time out or error on.  The worker counts an attempt for it and
    parks the job for good after ``max_attempts``, where an outage would be parked as
    recoverable and requeued automatically.  Treating it as an outage is how the looping BOLO call
    (2026-09-23) went round for two hours, holding a slot on every server each round.
    """

    reached = True


class NoBackendEnabledError(NoBackendAvailableError):
    """Nothing is left to route to at all — every backend is disabled or disqualified.

    Separate from its parent because it means something different to the caller: no
    backend *failed*, there was nowhere to send the work.  The worker waits and retries on
    this without counting an attempt, so turning every backend off from the admin page
    pauses transcription instead of burning three attempts per job and parking the queue
    as ``failed``.
    """


class BackendStats:
    """Rolling per-backend counters.  Mutated under the router's lock."""

    __slots__ = (
        "attempts",
        "audio_s",
        "consecutive_errors",
        "errors",
        "in_flight",
        "last_error",
        "last_error_at",
        "last_success_at",
        "last_success_mono",
        "processing_s",
        "recent_errors",
        "recent_successes",
        "rtfs",
        "successes",
    )

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.errors = 0
        self.consecutive_errors = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_success_at: datetime | None = None
        #: the same instant on the monotonic clock, for "has this server worked lately"
        self.last_success_mono: float | None = None
        self.audio_s = 0.0
        self.processing_s = 0.0
        self.rtfs: deque[float] = deque(maxlen=_RTF_WINDOW)
        #: attempts currently inside ``backend.transcribe`` — the "is it working right now"
        #: number, which no counter of finished work can answer
        self.in_flight = 0
        #: monotonic stamps of the last minute's outcomes, for the per-minute rates
        self.recent_successes: deque[float] = deque()
        self.recent_errors: deque[float] = deque()

    @property
    def mean_rtf(self) -> float | None:
        """Mean of the last ``_RTF_WINDOW`` real-time factors (audio seconds per wall second)."""
        return sum(self.rtfs) / len(self.rtfs) if self.rtfs else None

    @property
    def mean_processing_s(self) -> float | None:
        """Wall seconds a transcription has cost on average, over every success so far."""
        return self.processing_s / self.successes if self.successes else None

    def _trim(self, now: float) -> None:
        """Drop outcomes that have fallen out of the rolling window."""
        cutoff = now - RECENT_WINDOW_S
        for window in (self.recent_successes, self.recent_errors):
            while window and window[0] < cutoff:
                window.popleft()

    def recent(self) -> tuple[int, int]:
        """(completed, failed) in the last :data:`RECENT_WINDOW_S` seconds."""
        self._trim(time.monotonic())
        return len(self.recent_successes), len(self.recent_errors)

    def as_dict(self) -> dict[str, Any]:
        completed_1m, failed_1m = self.recent()
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "in_flight": self.in_flight,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_success_at": self.last_success_at,
            "audio_s": round(self.audio_s, 3),
            "processing_s": round(self.processing_s, 3),
            "mean_rtf": round(self.mean_rtf, 2) if self.mean_rtf is not None else None,
            "mean_processing_s": (
                round(self.mean_processing_s, 3) if self.mean_processing_s is not None else None
            ),
            "recent_completed_1m": completed_1m,
            "recent_failed_1m": failed_1m,
            "samples": len(self.rtfs),
        }


class BackendRouter:
    """Routes transcription requests across configured backends."""

    def __init__(
        self,
        backends: Sequence[STTBackend],
        *,
        health_ttl_s: float = 60.0,
        acquire_timeout_s: float = 300.0,
        config_disabled: Iterable[str] = (),
        override_provider: Callable[[], dict[str, bool]] | None = None,
        override_ttl_s: float = OVERRIDE_TTL_S,
        on_health_change: Callable[[], None] | None = None,
    ) -> None:
        self.backends: list[STTBackend] = list(backends)
        self.health_ttl_s = health_ttl_s
        self.acquire_timeout_s = acquire_timeout_s
        #: backends ``STT_BACKENDS`` marks ``enabled: false``.  They are still *built*, so the
        #: operator can turn one on from the admin page without a restart; they are simply
        #: not routed to until an override says otherwise.
        self.config_disabled = set(config_disabled)
        #: reads the operator's overrides (``{name: enabled}``, absent = follow config).  The
        #: worker passes a database reader; a bare router has none and follows config alone.
        self.override_provider = override_provider
        self.override_ttl_s = override_ttl_s
        #: called when a backend's health verdict flips, so the worker can republish at once
        #: rather than at the end of its cadence
        self.on_health_change = on_health_change
        self._overrides: dict[str, bool] = {}
        self._overrides_at = 0.0
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {
            b.name: threading.Semaphore(max(1, int(b.concurrency))) for b in self.backends
        }
        self._stats: dict[str, BackendStats] = {b.name: BackendStats() for b in self.backends}
        self._health: dict[str, tuple[float, BackendHealth]] = {}
        # A model the server does not have will never appear on its own; disqualify permanently
        # (until the process restarts or health is forced) instead of re-probing every TTL.
        self._disqualified: dict[str, str] = {}

    # ------------------------------------------------------------------ health

    def health(self, backend: STTBackend, *, force: bool = False) -> BackendHealth:
        """Cached health probe.  Re-probes at most once per ``health_ttl_s`` per backend."""
        now = time.monotonic()
        if not force:
            cached = self._health.get(backend.name)
            if cached and now - cached[0] < self.health_ttl_s:
                return cached[1]
        try:
            result = backend.health()
        except Exception as exc:  # noqa: BLE001 - a broken health() must not take the router down
            result = BackendHealth(ok=False, detail=f"{type(exc).__name__}: {exc}")
        previous = self._health.get(backend.name)
        self._health[backend.name] = (now, result)
        if self.on_health_change is not None and (previous is None or previous[1].ok != result.ok):
            try:
                self.on_health_change()
            except Exception:  # noqa: BLE001 - a broken listener must not fail the probe
                log.exception("stt: health-change listener raised for %s", backend.name)
        return result

    def health_report(self, *, force: bool = False) -> dict[str, BackendHealth]:
        return {b.name: self.health(b, force=force) for b in self.backends}

    def invalidate_health(self, name: str | None = None) -> None:
        if name is None:
            self._health.clear()
        else:
            self._health.pop(name, None)

    # ------------------------------------------------------------------ selection

    def overrides(self, *, force: bool = False) -> dict[str, bool]:
        """The operator's enable/disable overrides, re-read at most every ``override_ttl_s``.

        Cached because this is on the selection path of every job and the answer changes at
        human speed; the TTL is short enough that a click on the admin page takes effect
        while the operator is still looking at it.
        """
        if self.override_provider is None:
            return {}
        now = time.monotonic()
        if not force and self._overrides_at and now - self._overrides_at < self.override_ttl_s:
            return self._overrides
        try:
            self._overrides = dict(self.override_provider())
        except Exception:  # noqa: BLE001 - an unreadable override table must not stop transcription
            log.exception("stt: could not read the backend overrides; keeping the last ones")
        self._overrides_at = now
        return self._overrides

    def is_enabled(self, name: str, *, force: bool = False) -> bool:
        """The effective state: the operator's override if there is one, else the config."""
        override = self.overrides(force=force).get(name)
        if override is not None:
            return override
        return name not in self.config_disabled

    def _candidates(self, prefer: str | None) -> list[STTBackend]:
        """Backends in the order to try: preferred first, then priority ascending (lower number wins)."""
        usable = [
            b for b in self.backends if b.name not in self._disqualified and self.is_enabled(b.name)
        ]
        usable.sort(key=lambda b: (b.priority, b.name))
        if prefer:
            usable.sort(key=lambda b: b.name != prefer)
        return usable

    # ------------------------------------------------------------------ transcription

    def transcribe(
        self,
        path: str | Path,
        options: TranscribeOptions | None = None,
        prefer: str | None = None,
    ) -> TranscriptResult:
        """Transcribe ``path``, failing over across backends.  Blocking (run me in a thread)."""
        options = options or TranscribeOptions()
        candidates = self._candidates(prefer)
        if not candidates:
            raise NoBackendEnabledError(
                "no STT backend is enabled and usable "
                f"(disqualified: {self._disqualified or 'none'}, "
                f"disabled: {sorted(self._disabled_names()) or 'none'})"
            )

        healthy = [b for b in candidates if self.health(b).ok]
        # Everything looks down: the cached verdict may be stale (a server that just came back),
        # so try the whole list anyway rather than failing without touching the network.
        order = healthy or candidates

        started = time.monotonic()
        last_error: Exception | None = None
        #: (backend, error) for every backend that actually tried this request
        failures: list[tuple[STTBackend, BackendError]] = []
        busy = False
        for backend in order:
            free = self._sems[backend.name].acquire(blocking=False)
            if not free:
                if backend is not order[-1]:
                    continue  # someone else has every slot; try a lower-priority backend
                if not self._sems[backend.name].acquire(timeout=self.acquire_timeout_s):
                    last_error = NoBackendAvailableError(
                        f"all backends busy for {self.acquire_timeout_s:.0f}s"
                    )
                    busy = True
                    break
            try:
                return self._run(backend, path, options)
            except ModelNotInstalledError as exc:
                self._disqualified[backend.name] = str(exc)
                self._health[backend.name] = (time.monotonic(), BackendHealth(ok=False, detail=str(exc)))
                log.error("stt: disqualifying backend %s: %s", backend.name, exc)
                last_error = exc
                failures.append((backend, exc))
            except BackendError as exc:
                self.invalidate_health(backend.name)
                log.warning("stt: backend %s failed, failing over: %s", backend.name, exc)
                last_error = exc
                failures.append((backend, exc))
            finally:
                self._sems[backend.name].release()

        if not busy and self._request_specific(failures, started):
            detail = "; ".join(f"{b.name}: {e}" for b, e in failures)
            raise RequestFailedEverywhereError(
                f"every STT backend that tried failed on this recording while serving others: {detail}"
            ) from last_error
        raise NoBackendAvailableError(f"every STT backend failed; last error: {last_error}") from last_error

    def _request_specific(
        self, failures: Sequence[tuple[STTBackend, BackendError]], started: float
    ) -> bool:
        """Is this a recording every backend chokes on, rather than an outage?

        Two things have to be true, and both are needed:

        * **every** backend that tried reached its server and failed there
          (:attr:`~app.stt.base.BackendError.reached`: a read timeout, an HTTP error) — one
          refused connection, one unresolvable host, one disqualified model, and it is at
          least partly an outage, so it is treated as one;
        * **at least one** of them has transcribed something else within
          :data:`SUCCESS_EVIDENCE_S` of this request starting.  A server that accepts
          connections and then answers nobody (a wedged GPU) also produces read timeouts;
          what separates "this file" from "this server" is that the server is still
          finishing other people's calls.

        A freshly started router has no successes to point to, so its first verdicts are
        "outage"; the worker's cap on automatic requeues (``OUTAGE_REQUEUE_CAP``) is the
        backstop for that and for any other misreading.
        """
        if not failures or not all(exc.reached for _, exc in failures):
            return False
        horizon = started - SUCCESS_EVIDENCE_S
        with self._lock:
            return any(
                (seen := self._stats[b.name].last_success_mono) is not None and seen >= horizon
                for b, _ in failures
            )

    def _disabled_names(self) -> set[str]:
        """Every backend that is off right now, by config or by the operator."""
        return {b.name for b in self.backends if not self.is_enabled(b.name)}

    def _run(self, backend: STTBackend, path: str | Path, options: TranscribeOptions) -> TranscriptResult:
        stats = self._stats[backend.name]
        with self._lock:
            stats.attempts += 1
            # Incremented here and decremented in the ``finally`` below, so ``in_flight`` is
            # exactly "attempts inside backend.transcribe" however the attempt ends.
            stats.in_flight += 1
        try:
            result = backend.transcribe(path, options)
        except Exception as exc:
            with self._lock:
                stats.errors += 1
                stats.consecutive_errors += 1
                stats.last_error = f"{type(exc).__name__}: {exc}"
                stats.last_error_at = datetime.now(UTC)
                stats.recent_errors.append(time.monotonic())
            raise
        else:
            with self._lock:
                stats.successes += 1
                stats.consecutive_errors = 0
                stats.last_success_at = datetime.now(UTC)
                stats.last_success_mono = time.monotonic()
                stats.recent_successes.append(stats.last_success_mono)
                processing_s = result.processing_ms / 1000.0
                stats.processing_s += processing_s
                if result.duration:
                    stats.audio_s += result.duration
                rtf = result.rtf
                if rtf is not None:
                    stats.rtfs.append(rtf)
            return result
        finally:
            with self._lock:
                stats.in_flight -= 1

    # ------------------------------------------------------------------ introspection

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per-backend counters for the admin page / ``/api/ops`` endpoint."""
        with self._lock:
            snapshot = {name: st.as_dict() for name, st in self._stats.items()}
        for backend in self.backends:
            entry = snapshot.setdefault(backend.name, BackendStats().as_dict())
            entry.update(
                kind=backend.kind,
                model=backend.model,
                base_url=getattr(backend, "base_url", None),
                priority=backend.priority,
                concurrency=backend.concurrency,
                disqualified=self._disqualified.get(backend.name),
                config_enabled=backend.name not in self.config_disabled,
                admin_enabled=self.overrides().get(backend.name),
                enabled=self.is_enabled(backend.name),
            )
            cached = self._health.get(backend.name)
            entry["healthy"] = cached[1].ok if cached else None
            entry["health_detail"] = cached[1].detail if cached else ""
        return snapshot


# --------------------------------------------------------------------------- factory


def build_backends(
    config: Iterable[dict[str, Any]], *, include_disabled: bool = False
) -> list[STTBackend]:
    """Instantiate backends from plain dicts (settings / DB rows / env JSON).

    Shape::

        {"name": "whisper", "kind": "openai_compatible",
         "base_url": "http://whisper:8000", "api_key": null,
         "model": "Systran/faster-whisper-large-v3",
         "concurrency": 4, "priority": 10}
        {"name": "local", "kind": "faster_whisper_local",
         "model": "Systran/faster-whisper-small", "cpu_threads": 4,
         "concurrency": 1, "priority": 90}
        {"name": "stt-ovms", "kind": "ovms",
         "base_url": "http://stt-ovms:8000", "model": "whisper-medium-fp16-ov",
         "concurrency": 1, "priority": 60}
        {"name": "npu", "kind": "openvino_genai",
         "model": "OpenVINO/whisper-large-v3-turbo-int8-ov", "device": "NPU",
         "cache_dir": null, "cpu_threads": 4, "concurrency": 1, "priority": 50}

    Unknown keys are ignored so config can carry UI-only fields; ``enabled: false`` skips a row
    unless ``include_disabled`` is set, which the worker does: a backend the operator may
    switch on from the admin page has to exist as an object before they click, and the
    router's own ``config_disabled`` set is what keeps it out of rotation until then.
    """
    backends: list[STTBackend] = []
    for entry in config:
        if entry.get("enabled") is False and not include_disabled:
            continue
        kind = (entry.get("kind") or "openai_compatible").strip()
        name = entry.get("name") or kind
        model = entry.get("model")
        if not model:
            raise ValueError(f"STT backend {name!r}: 'model' is required")
        if kind == "openai_compatible":
            base_url = entry.get("base_url")
            if not base_url:
                raise ValueError(f"STT backend {name!r}: 'base_url' is required for openai_compatible")
            from .openai_compatible import OpenAICompatibleBackend  # noqa: PLC0415 - avoid cycles

            backends.append(
                OpenAICompatibleBackend(
                    name=name,
                    base_url=base_url,
                    model=model,
                    api_key=entry.get("api_key") or None,
                    concurrency=int(entry.get("concurrency", 4)),
                    priority=int(entry.get("priority", 10)),
                    attempts=int(entry.get("attempts", 3)),
                )
            )
        elif kind == "ovms":
            base_url = entry.get("base_url")
            if not base_url:
                raise ValueError(f"STT backend {name!r}: 'base_url' is required for ovms")
            from .ovms import OVMSBackend  # noqa: PLC0415 - avoid cycles

            backends.append(
                OVMSBackend(
                    name=name,
                    base_url=base_url,
                    model=model,
                    api_key=entry.get("api_key") or None,
                    concurrency=int(entry.get("concurrency", 1)),
                    priority=int(entry.get("priority", 60)),
                    attempts=int(entry.get("attempts", 3)),
                )
            )
        elif kind == "faster_whisper_local":
            from .faster_whisper_local import FasterWhisperLocalBackend  # noqa: PLC0415 - optional extra

            backends.append(
                FasterWhisperLocalBackend(
                    name=name,
                    model=model,
                    # An entry built from config.SttBackend carries device="" (the field is
                    # shared with the openvino kind, whose devices are spelled in upper case).
                    device=entry.get("device") or "cpu",
                    compute_type=entry.get("compute_type") or "int8",
                    cpu_threads=int(entry.get("cpu_threads", 4)),
                    concurrency=int(entry.get("concurrency", 1)),
                    priority=int(entry.get("priority", 90)),
                )
            )
        elif kind == "openvino_genai":
            from .openvino_local import OpenVINOLocalBackend  # noqa: PLC0415 - optional extra

            backends.append(
                OpenVINOLocalBackend(
                    name=name,
                    model=model,
                    device=entry.get("device") or "CPU",
                    cache_dir=entry.get("cache_dir") or None,
                    cpu_threads=int(entry.get("cpu_threads", 4)),
                    concurrency=int(entry.get("concurrency", 1)),
                    priority=int(entry.get("priority", 50)),
                    use_prompt=bool(entry.get("use_prompt", True)),
                    use_hotwords=bool(entry.get("use_hotwords", False)),
                )
            )
        else:
            raise ValueError(f"STT backend {name!r}: unknown kind {kind!r}")
    return backends
