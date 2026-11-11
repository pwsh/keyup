"""Router selection, failover, concurrency and stats — all with fake backends."""

from __future__ import annotations

import threading
import time

import pytest

from app.stt.base import (
    BackendHealth,
    BackendUnavailableError,
    ModelNotInstalledError,
    RequestTimeoutError,
    TranscriptionError,
)
from app.stt.router import (
    SUCCESS_EVIDENCE_S,
    BackendRouter,
    NoBackendAvailableError,
    NoBackendEnabledError,
    RequestFailedEverywhereError,
    build_backends,
)
from app.stt.types import TranscribeOptions, TranscriptResult


class FakeBackend:
    """Minimal STTBackend implementation with scriptable behavior."""

    kind = "fake"

    def __init__(
        self,
        name: str,
        *,
        priority: int = 100,
        concurrency: int = 1,
        healthy: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
        duration: float = 10.0,
        processing_ms: int = 1000,
    ) -> None:
        self.name = name
        self.model = f"model-{name}"
        self.priority = priority
        self.concurrency = concurrency
        self.healthy = healthy
        self.error = error
        self.delay = delay
        self.duration = duration
        self.processing_ms = processing_ms
        self.calls = 0
        self.health_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def transcribe(self, path, options: TranscribeOptions) -> TranscriptResult:
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error:
                raise self.error
            return TranscriptResult(
                text=f"hello from {self.name}",
                language="en",
                duration=self.duration,
                engine=self.kind,
                backend_name=self.name,
                model=self.model,
                processing_ms=self.processing_ms,
            )
        finally:
            with self._lock:
                self.in_flight -= 1

    def health(self) -> BackendHealth:
        self.health_calls += 1
        return BackendHealth(ok=self.healthy, latency_ms=1.0, detail="fake")


# --------------------------------------------------------------------------- selection


def test_picks_lowest_priority_number_healthy_backend(tmp_path):
    gpu = FakeBackend("gpu", priority=10)
    cpu = FakeBackend("cpu", priority=90)
    result = BackendRouter([cpu, gpu]).transcribe(tmp_path / "a.mp3")
    assert result.backend_name == "gpu"
    assert cpu.calls == 0


def test_skips_unhealthy_backend(tmp_path):
    gpu = FakeBackend("gpu", priority=90, healthy=False)
    cpu = FakeBackend("cpu", priority=90)
    assert BackendRouter([gpu, cpu]).transcribe(tmp_path / "a.mp3").backend_name == "cpu"
    assert gpu.calls == 0


def test_prefer_overrides_priority(tmp_path):
    gpu = FakeBackend("gpu", priority=90)
    cpu = FakeBackend("cpu", priority=90)
    router = BackendRouter([gpu, cpu])
    assert router.transcribe(tmp_path / "a.mp3", prefer="cpu").backend_name == "cpu"


def test_all_unhealthy_still_attempts(tmp_path):
    """A cached 'down' verdict may be stale, so the router tries anyway rather than giving up."""
    only = FakeBackend("gpu", healthy=False)
    assert BackendRouter([only]).transcribe(tmp_path / "a.mp3").backend_name == "gpu"


def test_no_backends_configured(tmp_path):
    with pytest.raises(NoBackendAvailableError):
        BackendRouter([]).transcribe(tmp_path / "a.mp3")


# --------------------------------------------------------------------------- failover


def test_fails_over_on_error(tmp_path):
    gpu = FakeBackend("gpu", priority=10, error=TranscriptionError("cuda oom", backend="gpu"))
    cpu = FakeBackend("cpu", priority=90)
    result = BackendRouter([gpu, cpu]).transcribe(tmp_path / "a.mp3")
    assert result.backend_name == "cpu"
    assert gpu.calls == 1


def test_all_backends_failing_raises(tmp_path):
    a = FakeBackend("a", error=TranscriptionError("boom", backend="a"))
    b = FakeBackend("b", priority=500, error=TranscriptionError("bang", backend="b"))
    with pytest.raises(NoBackendAvailableError) as excinfo:
        BackendRouter([a, b]).transcribe(tmp_path / "a.mp3")
    assert "bang" in str(excinfo.value)
    assert a.calls == 1 and b.calls == 1


def test_missing_model_disqualifies_backend_permanently(tmp_path):
    gpu = FakeBackend("gpu", priority=10, error=ModelNotInstalledError("no such model", backend="gpu"))
    cpu = FakeBackend("cpu", priority=90)
    router = BackendRouter([gpu, cpu])
    assert router.transcribe(tmp_path / "a.mp3").backend_name == "cpu"
    assert router.transcribe(tmp_path / "b.mp3").backend_name == "cpu"
    assert gpu.calls == 1, "a disqualified backend must not be retried"
    assert router.stats()["gpu"]["disqualified"]


def test_broken_health_probe_does_not_crash_the_router(tmp_path):
    class Exploding(FakeBackend):
        def health(self):
            raise RuntimeError("socket exploded")

    bad = Exploding("bad", priority=10)
    good = FakeBackend("good", priority=90)
    assert BackendRouter([bad, good]).transcribe(tmp_path / "a.mp3").backend_name == "good"


# --------------------------------------------------------------------------- health caching


def test_health_is_cached_for_the_ttl(tmp_path):
    backend = FakeBackend("gpu")
    router = BackendRouter([backend], health_ttl_s=60)
    for _ in range(5):
        router.transcribe(tmp_path / "a.mp3")
    assert backend.health_calls == 1


def test_health_recheck_after_ttl(tmp_path):
    backend = FakeBackend("gpu")
    router = BackendRouter([backend], health_ttl_s=0.0)
    router.transcribe(tmp_path / "a.mp3")
    router.transcribe(tmp_path / "b.mp3")
    assert backend.health_calls >= 2


def test_failure_invalidates_cached_health(tmp_path):
    gpu = FakeBackend("gpu", priority=10, error=TranscriptionError("boom", backend="gpu"))
    cpu = FakeBackend("cpu", priority=90)
    router = BackendRouter([gpu, cpu], health_ttl_s=300)
    router.transcribe(tmp_path / "a.mp3")
    router.transcribe(tmp_path / "b.mp3")
    assert gpu.health_calls >= 2


def test_health_report_and_force(tmp_path):
    backend = FakeBackend("gpu")
    router = BackendRouter([backend], health_ttl_s=300)
    assert router.health_report()["gpu"].ok
    router.health_report(force=True)
    assert backend.health_calls == 2
    router.invalidate_health()
    router.health_report()
    assert backend.health_calls == 3


# --------------------------------------------------------------------------- concurrency


def test_concurrency_limit_is_enforced(tmp_path):
    backend = FakeBackend("gpu", concurrency=2, delay=0.08)
    router = BackendRouter([backend])
    threads = [threading.Thread(target=router.transcribe, args=(tmp_path / f"{i}.mp3",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert backend.calls == 6
    assert backend.max_in_flight <= 2


def test_overflow_spills_to_the_lower_priority_backend(tmp_path):
    gpu = FakeBackend("gpu", priority=10, concurrency=1, delay=0.15)
    cpu = FakeBackend("cpu", priority=90, concurrency=4)
    router = BackendRouter([gpu, cpu])
    threads = [threading.Thread(target=router.transcribe, args=(tmp_path / f"{i}.mp3",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert gpu.calls >= 1
    assert cpu.calls >= 1, "work should spill over rather than queue behind a full GPU"
    assert gpu.calls + cpu.calls == 4


def test_single_busy_backend_blocks_rather_than_failing(tmp_path):
    backend = FakeBackend("only", concurrency=1, delay=0.1)
    router = BackendRouter([backend], acquire_timeout_s=10)
    threads = [threading.Thread(target=router.transcribe, args=(tmp_path / f"{i}.mp3",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert backend.calls == 3 and backend.max_in_flight == 1


# --------------------------------------------------------------------------- stats


def test_stats_track_rtf_and_errors(tmp_path):
    gpu = FakeBackend("gpu", priority=10, duration=20.0, processing_ms=1000)  # 20x real time
    cpu = FakeBackend("cpu", priority=90, error=TranscriptionError("boom", backend="cpu"))
    router = BackendRouter([gpu, cpu])
    for i in range(3):
        router.transcribe(tmp_path / f"{i}.mp3")
    # prefer the broken backend: it is tried first, errors, and the router falls over to the GPU.
    assert router.transcribe(tmp_path / "x.mp3", prefer="cpu").backend_name == "gpu"

    stats = router.stats()
    assert stats["gpu"]["successes"] == 4
    assert stats["gpu"]["errors"] == 0
    assert stats["gpu"]["mean_rtf"] == pytest.approx(20.0)
    assert stats["gpu"]["audio_s"] == pytest.approx(80.0)
    assert stats["gpu"]["samples"] == 4
    assert stats["cpu"]["errors"] == 1
    assert stats["cpu"]["consecutive_errors"] == 1
    assert "boom" in stats["cpu"]["last_error"]
    assert stats["gpu"]["kind"] == "fake" and stats["gpu"]["priority"] == 10


def test_stats_reset_consecutive_errors_on_success(tmp_path):
    class Flaky(FakeBackend):
        def transcribe(self, path, options):
            self.calls += 1
            if self.calls == 1:
                raise TranscriptionError("first one fails", backend=self.name)
            return TranscriptResult(
                text="ok", duration=5.0, engine="fake", backend_name=self.name, model="m", processing_ms=500
            )

    backend = Flaky("flaky")
    router = BackendRouter([backend])
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")
    router.transcribe(tmp_path / "b.mp3")
    stats = router.stats()["flaky"]
    assert stats["errors"] == 1 and stats["successes"] == 1 and stats["consecutive_errors"] == 0


def test_stats_include_every_backend_before_any_work():
    stats = BackendRouter([FakeBackend("gpu")]).stats()
    assert stats["gpu"]["attempts"] == 0 and stats["gpu"]["mean_rtf"] is None
    assert stats["gpu"]["healthy"] is None


# --------------------------------------------------------------------------- build_backends


def test_build_backends_from_config():
    backends = build_backends(
        [
            {
                "name": "whisper",
                "kind": "openai_compatible",
                "base_url": "http://whisper:8000/",
                "model": "Systran/faster-whisper-large-v3",
                "concurrency": 4,
                "priority": 100,
            },
            {
                "name": "local",
                "kind": "faster_whisper_local",
                "model": "Systran/faster-whisper-small",
                "cpu_threads": 4,
                "priority": 10,
            },
        ]
    )
    assert [b.name for b in backends] == ["whisper", "local"]
    assert backends[0].kind == "openai_compatible"
    assert backends[0].base_url == "http://whisper:8000"
    assert backends[0].concurrency == 4
    assert backends[1].kind == "faster_whisper_local"
    assert backends[1].cpu_threads == 4
    assert backends[1].concurrency == 1


def test_build_backends_defaults_to_openai_compatible():
    backends = build_backends([{"name": "x", "base_url": "http://h:8000", "model": "m"}])
    assert backends[0].kind == "openai_compatible" and backends[0].priority == 10


def test_build_backends_skips_disabled():
    assert build_backends([{"name": "x", "base_url": "http://h:8000", "model": "m", "enabled": False}]) == []


def test_build_backends_ignores_unknown_keys():
    backends = build_backends([{"name": "x", "base_url": "http://h:8000", "model": "m", "color": "red"}])
    assert len(backends) == 1


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "x", "kind": "openai_compatible", "base_url": "http://h:8000"},  # no model
        {"name": "x", "kind": "openai_compatible", "model": "m"},  # no base_url
        {"name": "x", "kind": "telepathy", "model": "m"},  # unknown kind
    ],
)
def test_build_backends_rejects_bad_config(entry):
    with pytest.raises(ValueError):
        build_backends([entry])


# ------------------------------------------------------------------ in-flight accounting


def test_in_flight_counts_attempts_inside_the_backend(tmp_path):
    """The one number no counter of *finished* work can give: what is happening now."""
    backend = FakeBackend("gpu", concurrency=4, delay=0.2)
    router = BackendRouter([backend])
    seen: list[int] = []

    def run(index: int) -> None:
        router.transcribe(tmp_path / f"{index}.mp3")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    seen.append(router.stats()["gpu"]["in_flight"])
    for thread in threads:
        thread.join()

    assert seen == [3]
    # And it comes back down — including after a failure, which is why the decrement is in
    # a ``finally`` rather than on each of the two exits.
    assert router.stats()["gpu"]["in_flight"] == 0


def test_in_flight_returns_to_zero_after_a_failure(tmp_path):
    backend = FakeBackend("gpu", error=TranscriptionError("boom", backend="gpu"))
    router = BackendRouter([backend])
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")
    assert router.stats()["gpu"]["in_flight"] == 0


def test_stats_carry_the_last_minute_and_the_mean_processing_time(tmp_path):
    backend = FakeBackend("gpu", processing_ms=2000, duration=20.0)
    router = BackendRouter([backend])
    router.transcribe(tmp_path / "a.mp3")
    router.transcribe(tmp_path / "b.mp3")
    entry = router.stats()["gpu"]
    assert entry["recent_completed_1m"] == 2
    assert entry["recent_failed_1m"] == 0
    assert entry["mean_processing_s"] == pytest.approx(2.0)
    assert entry["last_success_at"] is not None


# ------------------------------------------------------------------ the operator's switch


def test_a_backend_the_operator_turned_off_is_not_picked(tmp_path):
    """The override wins over the configuration, and clearing it puts the backend back."""
    gpu = FakeBackend("gpu", priority=10)
    cpu = FakeBackend("cpu", priority=90)
    overrides: dict[str, bool] = {"gpu": False}
    router = BackendRouter([gpu, cpu], override_provider=lambda: dict(overrides), override_ttl_s=0)
    assert router.transcribe(tmp_path / "a.mp3").backend_name == "cpu"
    assert gpu.calls == 0

    overrides.clear()
    assert router.transcribe(tmp_path / "b.mp3").backend_name == "gpu"


def test_an_override_is_re_read_within_the_cache_window(tmp_path):
    gpu = FakeBackend("gpu", priority=10)
    cpu = FakeBackend("cpu", priority=90)
    reads = {"count": 0}
    overrides: dict[str, bool] = {}

    def provider() -> dict[str, bool]:
        reads["count"] += 1
        return dict(overrides)

    router = BackendRouter([gpu, cpu], override_provider=provider, override_ttl_s=0.05)
    router.transcribe(tmp_path / "a.mp3")
    before = reads["count"]
    router.transcribe(tmp_path / "b.mp3")
    assert reads["count"] == before, "inside the window the router must not re-query"

    overrides["gpu"] = False
    time.sleep(0.06)
    assert router.transcribe(tmp_path / "c.mp3").backend_name == "cpu"


def test_config_disabled_backends_are_built_but_not_routed_to(tmp_path):
    """The worker builds every backend so the operator can switch one on without a restart."""
    gpu = FakeBackend("gpu", priority=10)
    cpu = FakeBackend("cpu", priority=90)
    overrides: dict[str, bool] = {}
    router = BackendRouter(
        [gpu, cpu],
        config_disabled={"gpu"},
        override_provider=lambda: dict(overrides),
        override_ttl_s=0,
    )
    assert router.transcribe(tmp_path / "a.mp3").backend_name == "cpu"
    overrides["gpu"] = True
    assert router.transcribe(tmp_path / "b.mp3").backend_name == "gpu"
    assert router.stats()["gpu"]["config_enabled"] is False
    assert router.stats()["gpu"]["enabled"] is True


def test_every_backend_off_is_its_own_error(tmp_path):
    """Distinct from a failure, because the worker must wait rather than fail the job."""
    gpu = FakeBackend("gpu")
    router = BackendRouter([gpu], config_disabled={"gpu"})
    with pytest.raises(NoBackendEnabledError):
        router.transcribe(tmp_path / "a.mp3")
    assert gpu.calls == 0


def test_an_unreadable_override_table_does_not_stop_transcription(tmp_path):
    def provider() -> dict[str, bool]:
        raise RuntimeError("database is down")

    router = BackendRouter([FakeBackend("gpu")], override_provider=provider, override_ttl_s=0)
    assert router.transcribe(tmp_path / "a.mp3").backend_name == "gpu"


def test_a_health_flip_notifies_the_publisher(tmp_path):  # noqa: ARG001
    backend = FakeBackend("gpu")
    flips: list[int] = []
    router = BackendRouter([backend], on_health_change=lambda: flips.append(1), health_ttl_s=0)
    router.health(backend)
    assert len(flips) == 1, "the first verdict is news too"
    router.health(backend)
    assert len(flips) == 1, "an unchanged verdict is not"
    backend.healthy = False
    router.health(backend)
    assert len(flips) == 2


# --------------------------------------------------------------------------- outage or bad recording


def served_others(router: BackendRouter, *backends: FakeBackend, tmp_path) -> None:  # noqa: ANN001
    """Every backend transcribes somebody else's call first: the evidence it is working."""
    for backend in backends:
        router.transcribe(tmp_path / "other.mp3", prefer=backend.name)


def test_every_serving_backend_timing_out_on_one_file_is_not_an_outage(tmp_path):
    """Call 5237413: three servers, each busily transcribing others, each timing out on it."""
    backends = [FakeBackend(n, priority=p) for n, p in (("p0", 10), ("p1", 11), ("p2", 12))]
    router = BackendRouter(backends)
    served_others(router, *backends, tmp_path=tmp_path)
    for b in backends:
        b.error = RequestTimeoutError(f"{b.name}: no answer within 110s", backend=b.name)

    with pytest.raises(RequestFailedEverywhereError) as excinfo:
        router.transcribe(tmp_path / "poison.mp3")

    assert not isinstance(excinfo.value, NoBackendAvailableError)
    assert all(b.calls == 2 for b in backends)  # one success each, then one try at the file
    assert "p2: no answer within 110s" in str(excinfo.value)


def test_an_http_error_on_every_serving_backend_is_the_recordings_too(tmp_path):
    a, b = FakeBackend("a"), FakeBackend("b", priority=500)
    router = BackendRouter([a, b])
    served_others(router, a, b, tmp_path=tmp_path)
    a.error = TranscriptionError("HTTP 422 from a: bad audio", backend="a")
    b.error = BackendUnavailableError("b: giving up after 3 attempt(s): 500", backend="b", reached=True)
    with pytest.raises(RequestFailedEverywhereError):
        router.transcribe(tmp_path / "poison.mp3")


def test_refused_connections_everywhere_are_an_outage(tmp_path):
    a, b = FakeBackend("a"), FakeBackend("b", priority=500)
    router = BackendRouter([a, b])
    served_others(router, a, b, tmp_path=tmp_path)
    for x in (a, b):
        x.error = BackendUnavailableError(f"{x.name}: connection refused", backend=x.name)
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")


def test_one_unreachable_backend_makes_it_an_outage(tmp_path):
    """Partly down is still down: the backend that never saw the file might have managed it."""
    a, b = FakeBackend("a"), FakeBackend("b", priority=500)
    router = BackendRouter([a, b])
    served_others(router, a, b, tmp_path=tmp_path)
    a.error = RequestTimeoutError("a: no answer within 110s", backend="a")
    b.error = BackendUnavailableError("b: connection refused", backend="b")
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")


def test_timeouts_with_no_recent_success_are_an_outage(tmp_path):
    """A wedged server answers nobody; read timeouts alone cannot tell that from a bad file."""
    a, b = FakeBackend("a"), FakeBackend("b", priority=500)
    for x in (a, b):
        x.error = RequestTimeoutError(f"{x.name}: no answer within 110s", backend=x.name)
    router = BackendRouter([a, b])
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")  # a fresh router has seen no success at all

    served_others_long_ago = time.monotonic() - SUCCESS_EVIDENCE_S - 60
    for x in (a, b):
        router._stats[x.name].last_success_mono = served_others_long_ago
    with pytest.raises(NoBackendAvailableError):
        router.transcribe(tmp_path / "a.mp3")
