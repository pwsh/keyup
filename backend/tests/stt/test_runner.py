"""The job loop, driven entirely by fake claim/complete/fail callables (no database)."""

from __future__ import annotations

import threading

import pytest

from app.stt.base import BackendHealth, TranscriptionError
from app.stt.router import BackendRouter
from app.stt.runner import Job, JobRunner, run_jobs
from app.stt.types import Quality, TranscribeOptions, TranscriptResult


class StubBackend:
    kind = "fake"
    name = "stub"
    model = "stub-model"
    concurrency = 4
    priority = 100

    def __init__(self, *, fail_paths: set[str] | None = None, text: str = "Engine 1 responding.") -> None:
        self.fail_paths = fail_paths or set()
        self.text = text
        self.seen: list[str] = []
        self.options: list[TranscribeOptions] = []
        self._lock = threading.Lock()

    def transcribe(self, path, options):
        with self._lock:
            self.seen.append(str(path))
            self.options.append(options)
        if str(path) in self.fail_paths or path.name in self.fail_paths:
            raise TranscriptionError("engine exploded", backend=self.name)
        return TranscriptResult(
            text=self.text,
            language="en",
            duration=5.0,
            engine=self.kind,
            backend_name=self.name,
            model=self.model,
            processing_ms=250,
        )

    def health(self):
        return BackendHealth(ok=True, latency_ms=1.0)


class Harness:
    """Hands out a fixed list of jobs, then ``None``, and records completions/failures."""

    def __init__(self, jobs: list[Job], *, stop_when_drained: bool = True) -> None:
        self.pending = list(jobs)
        self.completed: list[tuple[Job, TranscriptResult, object]] = []
        self.failures: list[tuple[Job, Exception]] = []
        self.stop_event = threading.Event()
        self.stop_when_drained = stop_when_drained
        self.claim_calls = 0
        self._lock = threading.Lock()

    def claim(self) -> Job | None:
        self.claim_calls += 1
        if self.pending:
            return self.pending.pop(0)
        if self.stop_when_drained:
            self.stop_event.set()
        return None

    def complete(self, job, result, quality) -> None:
        with self._lock:
            self.completed.append((job, result, quality))

    def fail(self, job, exc) -> None:
        with self._lock:
            self.failures.append((job, exc))


def options_for(job: Job) -> TranscribeOptions:
    return TranscribeOptions(prompt=f"Talkgroup {job.talkgroup_alias}." if job.talkgroup_alias else None)


def make_jobs(n: int, tmp_path) -> list[Job]:
    jobs = []
    for i in range(n):
        path = tmp_path / f"call{i}.mp3"
        path.write_bytes(b"\xff\xfb")
        jobs.append(
            Job(id=i, call_id=1000 + i, path=str(path), priority=5, talkgroup_alias="EC FIRE DISPATCH", duration_s=5.0)
        )
    return jobs


def test_job_dataclass_defaults():
    job = Job(id=1, call_id=2, path="/x.mp3")
    assert job.priority == 0 and job.talkgroup_alias is None and job.duration_s is None


def test_processes_every_claimed_job(tmp_path):
    backend = StubBackend()
    harness = Harness(make_jobs(5, tmp_path))
    runner = run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([backend]),
        options_for,
        workers=2,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert len(harness.completed) == 5
    assert harness.failures == []
    assert runner.processed == 5 and runner.failed == 0
    assert sorted(j.id for j, _, _ in harness.completed) == [0, 1, 2, 3, 4]


def test_quality_assessment_is_passed_to_complete(tmp_path):
    harness = Harness(make_jobs(1, tmp_path))
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    _job, result, quality = harness.completed[0]
    assert result.text == "Engine 1 responding."
    assert quality.quality is Quality.GOOD
    assert quality.cleaned_text == "Engine 1 responding."


def test_options_for_is_consulted_per_job(tmp_path):
    backend = StubBackend()
    harness = Harness(make_jobs(2, tmp_path))
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([backend]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert all(o.prompt == "Talkgroup EC FIRE DISPATCH." for o in backend.options)


def test_failed_job_calls_fail_and_the_loop_continues(tmp_path):
    jobs = make_jobs(3, tmp_path)
    backend = StubBackend(fail_paths={"call1.mp3"})
    harness = Harness(jobs)
    runner = run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([backend]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert len(harness.completed) == 2
    assert len(harness.failures) == 1
    assert harness.failures[0][0].id == 1
    assert runner.processed == 2 and runner.failed == 1


def test_a_raising_claim_does_not_kill_the_worker(tmp_path):
    jobs = make_jobs(2, tmp_path)

    class Flaky(Harness):
        def claim(self):
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise RuntimeError("connection reset by peer")
            return super().claim()

    harness = Flaky(jobs)
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert len(harness.completed) == 2


def test_a_raising_complete_does_not_kill_the_worker(tmp_path):
    harness = Harness(make_jobs(2, tmp_path))
    calls = {"n": 0}

    def bad_complete(job, result, quality):
        calls["n"] += 1
        raise RuntimeError("db is down")

    runner = run_jobs(
        harness.claim,
        bad_complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert calls["n"] == 2 and runner.processed == 2


def test_a_raising_fail_handler_does_not_kill_the_worker(tmp_path):
    harness = Harness(make_jobs(1, tmp_path))

    def bad_fail(job, exc):
        raise RuntimeError("db is down too")

    runner = run_jobs(
        harness.claim,
        harness.complete,
        bad_fail,
        BackendRouter([StubBackend(fail_paths={"call0.mp3"})]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert runner.failed == 1


def test_empty_queue_polls_then_stops(tmp_path):
    harness = Harness([])
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert harness.completed == [] and harness.claim_calls >= 1


def test_stop_event_set_up_front_does_no_work(tmp_path):
    harness = Harness(make_jobs(3, tmp_path))
    harness.stop_event.set()
    runner = run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=2,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert runner.processed == 0 and harness.completed == []


def test_claim_is_serialized_across_workers(tmp_path):
    """Two workers must never be handed the same row: claim() is called under a lock."""
    jobs = make_jobs(30, tmp_path)
    backend = StubBackend()
    harness = Harness(jobs)
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([backend]),
        options_for,
        workers=4,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    ids = sorted(j.id for j, _, _ in harness.completed)
    assert ids == list(range(30))
    assert len(backend.seen) == len(set(backend.seen))


def test_runner_stop_method(tmp_path):
    runner = JobRunner(
        claim=lambda: None,
        complete=lambda *a: None,
        fail=lambda *a: None,
        router=BackendRouter([StubBackend()]),
        options_for=options_for,
        workers=1,
        poll_s=5.0,
    )
    threading.Timer(0.05, runner.stop).start()
    runner.run()
    assert runner.stop_event.is_set()


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_worker_count_is_honoured(tmp_path, workers):
    harness = Harness(make_jobs(8, tmp_path))
    runner = run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([StubBackend()]),
        options_for,
        workers=workers,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert runner.workers == workers
    assert len(harness.completed) == 8



def test_runner_passes_job_preference_to_router(tmp_path):
    """A job created with an explicit backend (POST /calls/{id}/transcribe) reaches the router as ``prefer``."""
    primary, preferred = StubBackend(), StubBackend()
    primary.name, primary.priority = "whisper", 10
    preferred.name, preferred.priority = "stt-openvino", 50
    jobs = make_jobs(1, tmp_path)
    jobs[0].prefer = "stt-openvino"
    harness = Harness(jobs)
    run_jobs(
        harness.claim,
        harness.complete,
        harness.fail,
        BackendRouter([primary, preferred]),
        options_for,
        workers=1,
        poll_s=0.01,
        stop_event=harness.stop_event,
    )
    assert harness.failures == []
    assert [r.backend_name for _, r, _ in harness.completed] == ["stt-openvino"]
