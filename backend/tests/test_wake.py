"""The queue workers' doorbells (app/wake.py): idle threads wake on ``call.new`` instead of
waiting out their poll, and nothing about *which* job they take changes."""

from __future__ import annotations

import threading
import time

import pytest

from app import wake
from app.stt.router import BackendRouter
from app.stt.runner import Job, JobRunner
from app.wake import Doorbell
from tests.conftest import requires_db
from tests.stt.test_runner import StubBackend, options_for


def _wait_for(predicate, timeout: float = 5.0) -> bool:  # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- the bell


class TestDoorbell:
    def test_a_ring_ends_the_wait(self) -> None:
        bell = Doorbell()
        seen = bell.seq
        threading.Timer(0.05, bell.ring).start()
        started = time.monotonic()
        assert bell.wait(seen, 10.0) is True
        assert time.monotonic() - started < 2.0

    def test_a_ring_before_the_wait_is_not_lost(self) -> None:
        """The snapshot is taken before the claim; a ring in between must still count."""
        bell = Doorbell()
        seen = bell.seq
        bell.ring()
        started = time.monotonic()
        assert bell.wait(seen, 10.0) is True
        assert time.monotonic() - started < 0.1

    def test_no_ring_is_the_fallback_timeout(self) -> None:
        bell = Doorbell()
        started = time.monotonic()
        assert bell.wait(bell.seq, 0.2) is False
        assert time.monotonic() - started >= 0.19

    def test_stop_ends_the_wait_without_a_ring(self) -> None:
        bell = Doorbell()
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        started = time.monotonic()
        assert bell.wait(bell.seq, 30.0, stop) is False
        assert time.monotonic() - started < wake.STOP_CHECK_S + 0.5

    def test_a_delayed_ring_waits_its_delay(self) -> None:
        bell = Doorbell()
        seen = bell.seq
        started = time.monotonic()
        bell.ring(0.3)
        assert bell.wait(seen, 10.0) is True
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 2.0

    def test_an_immediate_ring_overtakes_a_delayed_one(self) -> None:
        bell = Doorbell()
        seen = bell.seq
        bell.ring(5.0)
        threading.Timer(0.05, bell.ring).start()
        started = time.monotonic()
        assert bell.wait(seen, 10.0) is True
        assert time.monotonic() - started < 1.0

    def test_one_ring_wakes_every_waiter(self) -> None:
        bell = Doorbell()
        seen = bell.seq
        woke: list[bool] = []
        threads = [
            threading.Thread(target=lambda: woke.append(bell.wait(seen, 10.0))) for _ in range(3)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        bell.ring()
        for thread in threads:
            thread.join(timeout=2)
        assert woke == [True, True, True]


# --------------------------------------------------------------------------- the runner


def test_an_idle_runner_thread_claims_on_the_ring_not_the_poll(tmp_path) -> None:  # noqa: ANN001
    """With a 30 s fallback poll, a job that appears and rings is still taken at once."""
    bell = Doorbell()
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    queue: list[Job] = []
    done = threading.Event()
    claims = 0

    def claim() -> Job | None:
        nonlocal claims
        claims += 1
        return queue.pop() if queue else None

    runner = JobRunner(
        claim=claim,
        complete=lambda *_: done.set(),
        fail=lambda *_: done.set(),
        router=BackendRouter([StubBackend()]),
        options_for=options_for,
        workers=1,
        poll_s=30.0,
        wake=bell,
    )
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: claims >= 1)
        queue.append(Job(id=1, call_id=1, path=str(audio)))
        started = time.monotonic()
        bell.ring()
        assert done.wait(5.0)
        assert time.monotonic() - started < 2.0
    finally:
        runner.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_stopped_runner_does_not_wait_out_its_fallback_poll() -> None:
    bell = Doorbell()
    runner = JobRunner(
        claim=lambda: None,
        complete=lambda *_: None,
        fail=lambda *_: None,
        router=BackendRouter([StubBackend()]),
        options_for=options_for,
        workers=2,
        poll_s=60.0,
        wake=bell,
    )
    threading.Timer(0.1, runner.stop).start()
    started = time.monotonic()
    runner.run()
    assert time.monotonic() - started < 5.0


# --------------------------------------------------------------------------- the listener


@pytest.fixture
def bells(monkeypatch):  # noqa: ANN001, ANN201
    """Fresh process bells, so one test's rings never wake another's waits."""
    stt, audio, first = Doorbell(), Doorbell(), threading.Event()
    monkeypatch.setattr(wake, "STT", stt)
    monkeypatch.setattr(wake, "AUDIO", audio)
    monkeypatch.setattr(wake, "AUDIO_FIRST", first)
    return stt, audio, first


class TestWhatRings:
    @staticmethod
    def ring(event: str, **payload: object) -> None:
        from app.worker import Worker

        Worker._ring(event, {"event": event, "id": 1, **payload})

    def test_a_live_call_rings_both_queues(self, bells) -> None:  # noqa: ANN001
        stt, audio, _ = bells
        self.ring("call.new", duplicate=False, ingest_priority=None)
        assert stt.seq == 1
        assert audio.seq == 1

    @pytest.mark.parametrize("priority", ["backlog", "bulk", "600"])
    def test_a_backlog_upload_rings_nothing(self, bells, priority) -> None:  # noqa: ANN001
        """Those jobs wait behind live traffic by design; the fallback poll takes them."""
        stt, audio, _ = bells
        self.ring("call.new", duplicate=False, ingest_priority=priority)
        assert (stt.seq, audio.seq) == (0, 0)

    def test_an_explicit_live_or_urgent_upload_rings(self, bells) -> None:  # noqa: ANN001
        stt, _, _ = bells
        self.ring("call.new", duplicate=False, ingest_priority="live")
        self.ring("call.new", duplicate=False, ingest_priority="urgent")
        assert stt.seq == 2

    def test_a_duplicate_rings_nothing(self, bells) -> None:  # noqa: ANN001
        stt, audio, _ = bells
        self.ring("call.new", duplicate=True)
        assert (stt.seq, audio.seq) == (0, 0)

    def test_audio_first_delays_the_transcription_ring(self, bells) -> None:  # noqa: ANN001
        """With audio processing on, Whisper should get the processed copy: the audio bell
        rings now and the transcription bell only after the grace (or the pass's own ring)."""
        from app.worker import AUDIO_GRACE_S

        stt, audio, first = bells
        first.set()
        seen = stt.seq
        started = time.monotonic()
        self.ring("call.new", duplicate=False)
        assert audio.seq == 1
        assert stt.seq == seen
        assert stt.wait(seen, AUDIO_GRACE_S + 2.0) is True
        assert time.monotonic() - started >= AUDIO_GRACE_S - 0.1

    def test_a_forced_retranscribe_rings_transcription(self, bells) -> None:  # noqa: ANN001
        stt, audio, _ = bells
        self.ring("call.updated")
        assert (stt.seq, audio.seq) == (1, 0)

    def test_other_events_ring_nothing(self, bells) -> None:  # noqa: ANN001
        stt, audio, _ = bells
        self.ring("call.transcribed")
        self.ring("tuner.status")
        assert (stt.seq, audio.seq) == (0, 0)


@requires_db
def test_a_notify_on_the_bus_rings_the_bell(session, settings, bells) -> None:  # noqa: ANN001
    """End to end over a real LISTEN connection: ``pg_notify`` in a committed transaction
    wakes the transcription bell, and the listener still queues the alert evaluation."""
    from app.db import notify
    from app.worker import Worker

    stt, audio, _ = bells
    worker = Worker(settings)
    thread = threading.Thread(target=worker._listen, daemon=True)
    thread.start()
    try:
        # The listener rings once as it (re)connects — whatever was announced while it was
        # away is lost, so the idle workers get one look.
        assert stt.wait(0, 10.0) is True
        seen = stt.seq
        notify(session, "call.new", {"id": 42, "duplicate": False, "ingest_priority": None})
        session.commit()
        assert stt.wait(seen, 10.0) is True
        assert audio.seq >= 2
        assert worker.alerts.get(timeout=5.0) == ("call.new", 42)
    finally:
        worker.stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


@requires_db
def test_a_listener_without_alerts_only_rings(session, settings, bells) -> None:  # noqa: ANN001
    """``--stt-only`` starts the listener for the doorbell and must not evaluate alerts."""
    from app.db import notify
    from app.worker import Worker

    stt, _, _ = bells
    worker = Worker(settings)
    worker._alerts_enabled = False
    thread = threading.Thread(target=worker._listen, daemon=True)
    thread.start()
    try:
        assert stt.wait(0, 10.0) is True
        seen = stt.seq
        notify(session, "call.new", {"id": 7, "duplicate": False})
        session.commit()
        assert stt.wait(seen, 10.0) is True
        assert worker.alerts.empty()
    finally:
        worker.stop.set()
        thread.join(timeout=10)


# --------------------------------------------------------------------------- the audio worker


@requires_db
def test_the_audio_worker_wakes_on_its_bell_with_the_stage_off(session) -> None:  # noqa: ANN001
    """Off, a new call's audio job is cancelled as soon as the bell rings — not a fallback
    poll later — because the syndication feeds hold a call back while it has an open job."""
    from sqlalchemy import select

    from app.audioproc.jobs import AudioProcessWorker, enqueue
    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import AudioJob
    from tests.test_reconcile import _call

    bell, stt_bell = Doorbell(), Doorbell()
    off = get_settings().model_copy(update={"audio_process_enabled": False})
    worker = AudioProcessWorker(
        settings=off, session_factory=SessionLocal, worker_name="t", bell=bell, stt_bell=stt_bell
    )
    stop = threading.Event()
    first = threading.Event()
    first.set()
    original = wake.AUDIO_FIRST
    wake.AUDIO_FIRST = first
    thread = threading.Thread(target=worker.run, args=(stop,), daemon=True)
    thread.start()
    try:
        # An idle cycle with the stage off says so: transcription must not wait for it.
        assert _wait_for(lambda: not first.is_set())
        call = _call(session, path="synthetic/wake-off.mp3")
        on = get_settings().model_copy(update={"audio_process_enabled": True})
        assert enqueue(session, call, on, force=True) is not None
        session.commit()
        started = time.monotonic()
        bell.ring()

        def cancelled() -> bool:
            session.expire_all()
            job = session.scalars(select(AudioJob).where(AudioJob.call_id == call.id)).one()
            return job.status == "cancelled"

        assert _wait_for(cancelled, timeout=5.0)
        assert time.monotonic() - started < 5.0
    finally:
        stop.set()
        thread.join(timeout=5)
        wake.AUDIO_FIRST = original
    assert not thread.is_alive()
