"""Rebuilding the router under running worker threads without dropping work.

The thing under test is not "does the reload happen" — that is one comparison — but the
part that is easy to get wrong: a swap must not reach an attempt that is already inside a
backend, and it must not silently stop the health-change wake-up that keeps the admin page
current.  Both are asserted here against a real router with a blocking fake backend, so the
in-flight attempt is genuinely in flight when the swap lands.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.stt.base import BackendHealth, STTBackend
from app.stt.hotswap import RouterHandle, RouterReloader
from app.stt.router import BackendRouter
from app.stt.types import TranscribeOptions, TranscriptResult
from app.stt.worker import build_router
from tests.conftest import requires_db


class SlowBackend(STTBackend):
    """A backend that blocks inside ``transcribe`` until the test lets it out."""

    def __init__(self, name: str, *, gate: threading.Event | None = None) -> None:
        self.name = name
        self.kind = "openai_compatible"
        self.model = "fake"
        self.priority = 10
        self.concurrency = 1
        self.gate = gate
        self.entered = threading.Event()
        self.healthy = True

    def health(self) -> BackendHealth:
        return BackendHealth(ok=self.healthy, detail="ok" if self.healthy else "down")

    def transcribe(self, path: Path, options: TranscribeOptions) -> TranscriptResult:  # noqa: ARG002
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the backend"
        return TranscriptResult(
            text=f"from {self.name}",
            model=self.model,
            engine="openai_compatible",
            backend_name=self.name,
        )


def router_for(*backends: SlowBackend) -> BackendRouter:
    return BackendRouter(list(backends), health_ttl_s=0.0)


def test_the_handle_forwards_to_whichever_router_is_current(tmp_path: Path) -> None:
    first, second = SlowBackend("first"), SlowBackend("second")
    handle = RouterHandle(router_for(first))
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"x")

    assert handle.transcribe(clip, TranscribeOptions()).backend_name == "first"
    handle.swap(router_for(second), config_version=7)
    assert handle.transcribe(clip, TranscribeOptions()).backend_name == "second"
    assert handle.config_version == 7
    assert [b.name for b in handle.backends] == ["second"]


def test_an_attempt_in_flight_finishes_on_the_router_it_started_on(tmp_path: Path) -> None:
    """The property the whole design exists for: a swap never reaches running work."""
    gate = threading.Event()
    old = SlowBackend("old", gate=gate)
    new = SlowBackend("new")
    old_router = router_for(old)
    handle = RouterHandle(old_router)
    clip = tmp_path / "a.mp3"
    clip.write_bytes(b"x")

    result: dict[str, TranscriptResult] = {}
    worker = threading.Thread(
        target=lambda: result.update(one=handle.transcribe(clip, TranscribeOptions()))
    )
    worker.start()
    assert old.entered.wait(10), "the attempt never reached the backend"

    # Swap while the attempt is inside the old backend, holding its permit.
    handle.swap(router_for(new), config_version=2)
    gate.set()
    worker.join(timeout=10)

    assert result["one"].backend_name == "old", "the in-flight attempt was rerouted"
    # The old router's permit came back to the old router, not to the new one.
    assert old_router.stats()["old"]["in_flight"] == 0
    assert old_router.stats()["old"]["successes"] == 1
    assert handle.stats()["new"]["successes"] == 0


def test_the_health_listener_survives_a_swap() -> None:
    """A backend going down must still wake the publisher after a reload."""
    woken: list[int] = []
    first, second = SlowBackend("first"), SlowBackend("second")
    handle = RouterHandle(router_for(first))
    handle.on_health_change = lambda: woken.append(1)

    second.healthy = False
    handle.swap(router_for(second), config_version=2)
    handle.health_report(force=True)
    assert woken, "the swap dropped the health-change hook"


# --------------------------------------------------------------------------------------
# the reloader
# --------------------------------------------------------------------------------------


class FakeSession:
    """Just enough of a session for :func:`app.stt.config_store.version`."""

    def __init__(self, versions: list[int]) -> None:
        self.versions = versions

    def scalar(self, _stmt):  # noqa: ANN001, ANN202
        return self.versions[-1]

    def close(self) -> None:
        pass


def test_the_reloader_rebuilds_only_when_the_version_moves() -> None:
    versions = [0]
    built: list[str] = []

    def build() -> BackendRouter:
        built.append("x")
        return router_for(SlowBackend(f"gen{len(built)}"))

    handle = RouterHandle(router_for(SlowBackend("gen0")), config_version=0)
    reloader = RouterReloader(handle, lambda: FakeSession(versions), build)

    assert reloader.poll() is False, "no change, no rebuild"
    assert built == []

    versions.append(3)
    assert reloader.poll() is True
    assert handle.config_version == 3
    assert [b.name for b in handle.backends] == ["gen1"]

    assert reloader.poll() is False, "the same version must not rebuild again"
    assert reloader.swaps == 1


def test_a_build_that_fails_keeps_the_working_router() -> None:
    """A backend the operator saved that this host cannot construct must not stop the work."""
    versions = [0]
    handle = RouterHandle(router_for(SlowBackend("working")), config_version=0)

    def build() -> BackendRouter:
        raise RuntimeError("openvino-genai is not installed")

    reloader = RouterReloader(handle, lambda: FakeSession(versions), build)
    versions.append(5)
    assert reloader.poll() is False
    assert [b.name for b in handle.backends] == ["working"]
    assert handle.config_version == 0, "the version must stay behind, so the next tick retries"
    versions.append(5)
    assert reloader.poll() is False


def test_a_reload_never_falls_back_to_the_environment() -> None:
    """A database that cannot be read is not the operator saying "use STT_BACKENDS again".

    At start-up the env fallback is right: a router on stale backends beats no worker.  On a
    reload it is a silent downgrade — the running router already came from the operator's
    rows, so swapping in whatever is left in the host's ``.env`` *and* stamping it with the
    new version would report the edit applied and leave no tick to correct it.  The read is
    allowed to raise instead, which ``poll`` already knows how to survive.
    """
    from app.stt import config_store

    def unreadable():  # noqa: ANN202
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        config_store.entries_for(unreadable, fallback_to_env=False)

    versions = [0]
    handle = RouterHandle(router_for(SlowBackend("from-the-rows")), config_version=0)
    reloader = RouterReloader(
        handle,
        lambda: FakeSession(versions),
        lambda: build_router(session_factory=unreadable, fallback_to_env=False),
    )
    versions.append(7)
    assert reloader.poll() is False, "an unreadable table must not swap anything in"
    assert [b.name for b in handle.backends] == ["from-the-rows"]
    assert handle.config_version == 0, "and must not claim v7 was applied"


def test_start_up_still_falls_back_to_the_environment(monkeypatch) -> None:  # noqa: ANN001
    """The other half of the rule: a worker that cannot read the table still starts."""
    from app.stt import config_store

    def unreadable():  # noqa: ANN202
        raise RuntimeError("connection refused")

    monkeypatch.setattr(config_store, "entries_from_env", lambda *a, **k: [{"name": "env"}])  # noqa: ARG005
    assert config_store.entries_for(unreadable) == [{"name": "env"}]


def test_the_publisher_runs_the_reload_before_it_publishes() -> None:
    """Ordering, so the row the page reads already carries the version it just applied."""
    from app.stt.stats_store import StatsPublisher

    order: list[str] = []
    handle = RouterHandle(router_for(SlowBackend("one")), config_version=4)

    class Recorder(StatsPublisher):
        def publish_once(self) -> int:
            order.append("publish")
            return 0

    publisher = Recorder(
        handle,
        lambda: None,
        worker_instance="test",
        on_tick=lambda: order.append("reload"),
    )
    publisher._tick()  # noqa: SLF001 - the hook is the unit under test
    publisher.publish_once()
    assert order == ["reload", "publish"]


def test_a_broken_tick_hook_does_not_stop_the_reporting() -> None:
    from app.stt.stats_store import StatsPublisher

    published: list[int] = []
    handle = RouterHandle(router_for(SlowBackend("one")))

    def explode() -> None:
        raise RuntimeError("the settings table is on fire")

    class Recorder(StatsPublisher):
        def publish_once(self) -> int:
            published.append(1)
            return 0

    publisher = Recorder(handle, lambda: None, worker_instance="test", on_tick=explode)
    publisher._tick()  # noqa: SLF001
    publisher.publish_once()
    assert published == [1]


@pytest.mark.parametrize("version", [0, 11])
def test_the_publisher_stamps_the_handles_version(version: int) -> None:
    """``stt_backend_stats.config_version`` is read off the handle, not guessed."""
    handle = RouterHandle(router_for(SlowBackend("one")), config_version=version)
    assert getattr(handle, "config_version", None) == version


# --------------------------------------------------------------------------------------
# end to end, against the real table
# --------------------------------------------------------------------------------------


@requires_db
def test_a_saved_backend_reaches_a_running_worker(session, settings) -> None:  # noqa: ANN001
    """The whole point, in one test: edit the table, tick the worker, new router.

    Exercised through :func:`app.stt.worker.build_router` rather than a stub, so the path
    the worker really takes — read the rows, build the engines, count the disabled ones —
    is the path that is proved to reload.
    """
    from app.db import SessionLocal
    from app.models import SttBackendConfig
    from app.stt import config_store
    from app.stt.worker import build_router, build_router_handle

    session.add(
        SttBackendConfig(name="first", kind="faster_whisper_local", model="tiny", priority=1)
    )
    config_store.bump(session)
    session.commit()

    handle = build_router_handle(settings, session_factory=SessionLocal)
    assert [b.name for b in handle.backends] == ["first"]
    applied = handle.config_version

    reloader = RouterReloader(
        handle, SessionLocal, lambda: build_router(settings, session_factory=SessionLocal)
    )
    assert reloader.poll() is False, "nothing has changed since the handle was built"

    session.add(
        SttBackendConfig(name="second", kind="faster_whisper_local", model="tiny", priority=2)
    )
    config_store.bump(session)
    session.commit()

    assert reloader.poll() is True
    assert sorted(b.name for b in handle.backends) == ["first", "second"]
    assert handle.config_version > applied
