"""The transcription worker against a real PostgreSQL.

Claiming is the part that has to be exactly right — two workers must never be handed the
same row — so it is tested with two concurrent claims rather than by reading the SQL.  The
router is a stub: what is under test is the job bookkeeping, not whisper.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Call, System, Talkgroup, Transcript, TranscriptionJob
from app.stt.types import (
    Quality,
    QualityAssessment,
    TranscribeOptions,
    TranscriptResult,
    TranscriptSegment,
)
from app.stt.worker import (
    _LOOP_RE,
    AUTO_REQUEUE_PREFIX,
    BACKOFF_BASE_S,
    MIN_HALLUCINATION_CHARS,
    PARKED_BACKEND_UNAVAILABLE,
    SttWorker,
    hallucination_reason,
    requeue_failed,
    requeue_stale,
    unpark_backend_outage,
)
from tests.conftest import requires_db

pytestmark = requires_db


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


class FakeRouter:
    """Stands in for :class:`app.stt.router.BackendRouter`."""

    def __init__(self, result: TranscriptResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[Path, TranscribeOptions]] = []
        self.backends: list[object] = []

    def transcribe(self, path: Path, options: TranscribeOptions):  # noqa: ANN201
        self.calls.append((Path(path), options))
        if self.error is not None:
            raise self.error
        return self.result


def make_result(text: str = "Engine 1 responding to Riverton.", **kw) -> TranscriptResult:  # noqa: ANN003
    return TranscriptResult(
        text=text,
        language="en",
        duration=kw.pop("duration", 3.0),
        segments=[
            TranscriptSegment(
                start=0.0,
                end=3.0,
                text=text,
                avg_logprob=kw.pop("avg_logprob", -0.3),
                no_speech_prob=kw.pop("no_speech_prob", 0.1),
                compression_ratio=1.2,
            )
        ],
        engine="openai_compatible",
        backend_name="whisper",
        model="Systran/faster-whisper-large-v3",
        processing_ms=kw.pop("processing_ms", 250),
        **kw,
    )


def seed_call(
    session,  # noqa: ANN001
    *,
    root: Path | None = None,
    tgid: int = 1101,
    duration: float = 3.0,
    priority: int = 100,
    scheduled_at: datetime | None = None,
    file_path: str = "20260829/EC EVENT 6/call.mp3",
) -> tuple[Call, TranscriptionJob]:
    """One system + talkgroup + call + queued job, the shape ingest leaves behind.

    ``root`` writes the recording as well: claiming now refuses a job whose audio is not
    on disk, so a job that is meant to be claimable needs a file behind it.
    """
    system = session.execute(select(System).where(System.name == "Example System")).scalar_one_or_none()
    if system is None:
        system = System(name="Example System", protocol="P25 Phase 1")
        session.add(system)
        session.flush()
    talkgroup = session.execute(
        select(Talkgroup).where(Talkgroup.system_id == system.id, Talkgroup.tgid == tgid)
    ).scalar_one_or_none()
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=system.id, tgid=tgid, alias="EC EVENT 6", priority=priority)
        session.add(talkgroup)
        session.flush()

    started = datetime.now(UTC) - timedelta(minutes=5)
    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[tgid],
        recorded_at=started + timedelta(seconds=duration),
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=duration),
        duration_s=Decimal(str(duration)),
        file_path=file_path,
        file_name=Path(file_path).name,
        talkgroup_alias_at_record="EC EVENT 6",
        transcript_status="queued",
    )
    session.add(call)
    session.flush()
    job = TranscriptionJob(
        call_id=call.id,
        status="queued",
        priority=priority,
        max_attempts=3,
        scheduled_at=scheduled_at or datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(job)
    session.commit()
    if root is not None:
        target = Path(root) / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xfb\x90d" + b"\x00" * 64)
    return call, job


@pytest.fixture
def worker(session, settings, tmp_path):  # noqa: ANN001, ANN201
    from app.db import SessionLocal

    return SttWorker(
        router=FakeRouter(make_result()),
        settings=settings,
        session_factory=SessionLocal,
        worker_name="test-worker",
        audio_root=tmp_path,
    )


# --------------------------------------------------------------------------------------
# claim
# --------------------------------------------------------------------------------------


def test_claim_marks_the_job_running_and_resolves_the_audio_path(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job = seed_call(session, root=tmp_path)

    claimed = worker.claim()

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.call_id == call.id
    assert claimed.path == str(tmp_path / call.file_path)
    assert claimed.duration_s == pytest.approx(3.0)
    assert claimed.talkgroup_alias == "EC EVENT 6"

    session.expire_all()
    row = session.get(TranscriptionJob, job.id)
    assert (row.status, row.locked_by) == ("running", "test-worker")
    assert row.locked_at is not None and row.started_at is not None
    assert session.get(Call, call.id).transcript_status == "running"


def test_a_call_with_no_speech_in_it_never_reaches_whisper(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The re-check on claim, and why it is here rather than at enqueue time.

    Transcription is enqueued by ingest; the audio pass runs on its own queue and is what
    measures whether anybody spoke (``audio_meta.content``, §5.5).  So the job usually exists
    *before* the verdict does, and the only place that can act on it is the moment the job is
    claimed.  Production 2395971/2395972 — 0.648 s, one click, no words — were both sent to
    Whisper and both came back empty.
    """
    call, job = seed_call(session, root=tmp_path)
    call.audio_meta = {"content": "none", "speech_s": 0.04, "speech_floor_db": -40.0}
    session.commit()

    assert worker.claim() is None

    session.expire_all()
    row = session.get(TranscriptionJob, job.id)
    # ``cancelled`` rather than a new job status: this is the same "there is nothing to
    # transcribe" the empty-clip path already cancels for.
    assert row.status == "cancelled"
    assert row.last_error == "no speech"
    assert row.finished_at is not None
    assert row.locked_by is None
    assert session.get(Call, call.id).transcript_status == "skipped"


def test_a_call_the_audio_pass_has_not_reached_is_claimed_as_before(session, worker, tmp_path) -> None:  # noqa: ANN001
    """No verdict is not a verdict of "none": the queue must not stall on a missing pass."""
    call, _job = seed_call(session, root=tmp_path)
    call.audio_meta = None
    session.commit()
    assert worker.claim() is not None

    session.rollback()


def test_a_call_with_speech_is_claimed(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, _job = seed_call(session, root=tmp_path)
    call.audio_meta = {"content": "speech", "speech_s": 1.4}
    session.commit()
    assert worker.claim() is not None


def test_a_page_is_claimed_even_though_nobody_spoke(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Only `none` skips.

    Calls 2396355/2396357 are 4.25 s of pager tone-out with no dispatch behind them: no
    speech at all, and still content.  Whisper will make little of it, which is fine — the
    empty result is a fact about the call and not a job we should never have run.
    """
    call, _job = seed_call(session, root=tmp_path)
    call.audio_meta = {"content": "page", "speech_s": 0.0}
    session.commit()
    assert worker.claim() is not None


def test_claim_returns_none_when_nothing_is_due(session, worker, tmp_path) -> None:  # noqa: ANN001
    seed_call(session, root=tmp_path, scheduled_at=datetime.now(UTC) + timedelta(minutes=10))
    assert worker.claim() is None


def test_claim_orders_by_priority_then_schedule(session, worker, tmp_path) -> None:  # noqa: ANN001
    """M6: a lower `priority` runs first — 1 is "do this now", which is what a forced
    re-transcribe asks for, and 100 is the default ingest enqueues at."""
    _, urgent = seed_call(session, root=tmp_path, tgid=1101, priority=10, file_path="a.mp3")
    _, ordinary = seed_call(session, root=tmp_path, tgid=1102, priority=900, file_path="b.mp3")

    first, second = worker.claim(), worker.claim()

    assert [first.id, second.id] == [urgent.id, ordinary.id]


def test_two_workers_never_claim_the_same_job(session, settings, tmp_path) -> None:  # noqa: ANN001
    """``FOR UPDATE SKIP LOCKED`` under real concurrency, not in theory."""
    from app.db import SessionLocal

    for n in range(4):
        seed_call(session, root=tmp_path, tgid=1100 + n, file_path=f"c{n}.mp3")

    claimed: list[int] = []
    lock = threading.Lock()

    def drain(name: str) -> None:
        worker = SttWorker(
            router=FakeRouter(),
            settings=settings,
            session_factory=SessionLocal,
            worker_name=name,
            audio_root=tmp_path,
        )
        while (job := worker.claim()) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 4
    assert len(set(claimed)) == 4


# --------------------------------------------------------------------------------------
# options / prompt
# --------------------------------------------------------------------------------------


def test_options_carry_the_domain_prompt_and_the_configured_knobs(session, worker, tmp_path) -> None:  # noqa: ANN001
    seed_call(session, root=tmp_path)
    job = worker.claim()

    options = worker.options_for(job)

    assert options.vad is worker.settings.stt_vad
    assert options.language == "en"
    assert options.word_timestamps is False
    assert "EC EVENT 6" in options.prompt  # the talkgroup, straight from the directory
    assert "Places:" not in options.prompt  # STT_PLACES is blank by default


def test_stt_places_join_the_prompt_in_order(session, worker, tmp_path) -> None:  # noqa: ANN001
    """``STT_PLACES`` is a comma list; blanks are dropped and duplicates folded."""
    worker.settings = worker.settings.model_copy(
        update={"stt_places": " Lakeside, Riverton ,, lakeside, State University "}
    )
    seed_call(session, root=tmp_path)
    job = worker.claim()

    options = worker.options_for(job)

    assert "Places: Lakeside, Riverton, State University." in options.prompt


def test_prompt_places_is_defaults_then_the_setting() -> None:
    from app.config import Settings
    from app.stt.prompt import DEFAULT_PLACES
    from app.stt.worker import prompt_places

    assert prompt_places(Settings(_env_file=None)) == DEFAULT_PLACES
    assert prompt_places(Settings(_env_file=None, stt_places="A, ,B")) == (*DEFAULT_PLACES, "A", "B")


def test_the_prompt_carries_the_roster(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Curated callsigns and the names of people on the air are the proper nouns to bias for.

    An inactive person is left out on purpose: the row is kept because the archive is
    historical, but biasing the decoder toward a name that will not be said again only costs
    accuracy on the ones that will.
    """
    from app.models import Person, Unit

    seed_call(session, root=tmp_path)
    session.add_all(
        [
            Unit(callsign="E7", name="Engine 7"),
            Person(name="Halvorsen", rank="Captain"),
            Person(name="Retiredovsky", active=False),
        ]
    )
    session.commit()

    prompt = worker.prompt()

    assert "E7" in prompt
    assert "Engine 7" in prompt
    assert "Halvorsen" in prompt
    assert "Retiredovsky" not in prompt


def test_options_carry_the_normalize_filter_when_the_switch_is_on(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The worker fills TranscribeOptions.normalize_filter so every backend prep normalizes."""
    from app.stt.normalize import ffmpeg_audio_filter

    seed_call(session, root=tmp_path)
    job = worker.claim()

    options = worker.options_for(job)
    assert options.normalize_filter == ffmpeg_audio_filter(worker.settings)


def test_options_leave_the_normalize_filter_unset_when_off(session, settings, tmp_path) -> None:  # noqa: ANN001
    from app.db import SessionLocal

    worker = SttWorker(
        router=FakeRouter(make_result()),
        settings=settings.model_copy(update={"stt_normalize": False}),
        session_factory=SessionLocal,
        worker_name="test-worker",
        audio_root=tmp_path,
    )
    seed_call(session, root=tmp_path)
    job = worker.claim()

    assert worker.options_for(job).normalize_filter is None


def test_the_prompt_is_cached(session, worker, tmp_path) -> None:  # noqa: ANN001
    seed_call(session, root=tmp_path)
    first = worker.prompt()
    session.add(Talkgroup(system_id=1, tgid=99999, alias="BRAND NEW CHANNEL"))
    session.commit()
    assert worker.prompt() == first  # not re-read until the TTL expires


# --------------------------------------------------------------------------------------
# complete
# --------------------------------------------------------------------------------------


def test_complete_writes_the_transcript_and_denormalizes_it(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    job = worker.claim()
    result = make_result()
    quality = QualityAssessment(
        quality=Quality.GOOD, reasons=[], cleaned_text="Engine 1 responding to Riverton."
    )

    worker.complete(job, result, quality)

    session.expire_all()
    transcript = session.execute(select(Transcript).where(Transcript.call_id == call.id)).scalar_one()
    assert transcript.is_primary is True
    assert transcript.engine == "openai_compatible"
    assert transcript.backend_name == "whisper"
    assert transcript.quality == "good"
    assert transcript.processing_ms == 250
    assert transcript.avg_logprob == pytest.approx(-0.3)
    assert transcript.no_speech_prob == pytest.approx(0.1)
    assert transcript.vad_used is True
    assert transcript.prompt_used

    stored = session.get(Call, call.id)
    assert stored.transcript_text == "Engine 1 responding to Riverton."
    assert stored.transcript_status == "done"
    assert stored.transcript_quality == "good"
    assert session.get(TranscriptionJob, job_row.id).status == "done"


def test_a_second_transcript_demotes_the_first(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, _ = seed_call(session, root=tmp_path)
    quality = QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text="first")
    worker.complete(worker.claim(), make_result("first"), quality)

    session.add(TranscriptionJob(call_id=call.id, status="queued", max_attempts=3))
    session.commit()
    worker.complete(
        worker.claim(),
        make_result("second"),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text="second"),
    )

    session.expire_all()
    rows = session.execute(
        select(Transcript).where(Transcript.call_id == call.id).order_by(Transcript.id)
    ).scalars().all()
    assert [r.is_primary for r in rows] == [False, True]
    assert session.get(Call, call.id).transcript_text == "second"


def test_noise_becomes_transcript_status_empty(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, _ = seed_call(session, root=tmp_path)
    worker.complete(
        worker.claim(),
        make_result("Thanks for watching!"),
        QualityAssessment(quality=Quality.NOISE, reasons=["empty_text"], cleaned_text=""),
    )

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.transcript_status == "empty"
    assert stored.transcript_text is None
    assert stored.transcript_quality == "noise"


# --------------------------------------------------------------------------------------
# fail / backoff
# --------------------------------------------------------------------------------------


def test_failure_requeues_with_exponential_backoff(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    job = worker.claim()
    before = datetime.now(UTC)

    worker.fail(job, RuntimeError("connection reset"))

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert row.status == "queued"
    assert row.attempts == 1
    assert "connection reset" in row.last_error
    assert row.locked_by is None
    delay = (row.scheduled_at - before).total_seconds()
    assert BACKOFF_BASE_S * 2 - 5 < delay < BACKOFF_BASE_S * 2 + 5  # 30 s * 2**1
    assert session.get(Call, call.id).transcript_status == "queued"


def test_the_last_attempt_parks_the_job_as_failed(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    for _ in range(3):
        job = worker.claim()
        assert job is not None
        worker.fail(job, RuntimeError("backend is down"))
        session.expire_all()
        row = session.get(TranscriptionJob, job_row.id)
        if row.status == "queued":  # let the next claim see it
            row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.finished_at is not None
    assert session.get(Call, call.id).transcript_status == "failed"


def test_no_backend_enabled_makes_the_job_wait_rather_than_fail(session, worker, tmp_path) -> None:  # noqa: ANN001
    """An operator switching the last backend off pauses the queue; it does not empty it.

    Nothing failed — there was nowhere to send the work — so the attempt is not counted.
    Counting it would park every job in the queue as ``failed`` within three polls, and the
    operator would come back to a backlog that has to be requeued by hand.
    """
    from app.stt.router import NoBackendEnabledError
    from app.stt.worker import NO_BACKEND_RETRY_S

    call, job_row = seed_call(session, root=tmp_path)
    before = datetime.now(UTC)

    for _ in range(5):  # well past max_attempts
        job = worker.claim()
        assert job is not None, "the job has to stay claimable"
        worker.fail(job, NoBackendEnabledError("no STT backend is enabled and usable"))
        session.expire_all()
        row = session.get(TranscriptionJob, job_row.id)
        assert (row.status, row.attempts) == ("queued", 0)
        row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert row.finished_at is None
    assert session.get(Call, call.id).transcript_status == "queued"

    # …and the wait is flat and short, not the failure path's doubling backoff.
    job = worker.claim()
    worker.fail(job, NoBackendEnabledError("still nothing enabled"))
    session.expire_all()
    delay = (session.get(TranscriptionJob, job_row.id).scheduled_at - before).total_seconds()
    assert 0 < delay < NO_BACKEND_RETRY_S + 5


def test_build_router_builds_a_config_disabled_backend_so_it_can_be_switched_on(settings) -> None:  # noqa: ANN001
    """``enabled: false`` must not mean "absent", or the admin switch would need a restart."""
    from app.config import SttBackend
    from app.stt.worker import build_router

    config = settings.model_copy(
        update={
            "stt_backends": [
                SttBackend(name="on", kind="openai_compatible", base_url="http://a:8000", model="m"),
                SttBackend(
                    name="off",
                    kind="openai_compatible",
                    base_url="http://b:8000",
                    model="m",
                    enabled=False,
                ),
            ]
        }
    )
    router = build_router(config, session_factory=None)
    assert {b.name for b in router.backends} == {"on", "off"}
    assert router.config_disabled == {"off"}
    assert router.is_enabled("off") is False
    assert router.is_enabled("on") is True


# --------------------------------------------------------------------------------------
# parking a job, and getting it back after an outage
# --------------------------------------------------------------------------------------


def park(session, worker, tmp_path, exc: Exception, **kw):  # noqa: ANN001, ANN201
    """Burn one call's three attempts against ``exc`` and hand back its parked row."""
    call, job_row = seed_call(session, root=tmp_path, **kw)
    for _ in range(3):
        job = worker.claim()
        assert job is not None
        worker.fail(job, exc)
        session.expire_all()
        row = session.get(TranscriptionJob, job_row.id)
        if row.status == "queued":
            row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    session.expire_all()
    return call, session.get(TranscriptionJob, job_row.id)


def publish_backend(session, *, name: str = "whisper", healthy: bool | None = True, **kw):  # noqa: ANN001, ANN201
    """A configured backend with the stats row the worker would have published for it.

    This is the state ``GET /api/admin/stt/backends`` reads, and therefore the state the
    unpark pass reads: a configuration row so the backend is *configured* at all, and a
    stats row carrying the worker's health verdict and the timestamp that says whether
    anybody is still reporting it.
    """
    from app.models import SttBackendConfig, SttBackendStat

    session.merge(
        SttBackendConfig(
            name=name,
            kind="openai_compatible",
            base_url="http://whisper.local:8000/v1",
            model="Systran/faster-whisper-large-v3",
            enabled=kw.pop("config_enabled", True),
        )
    )
    session.merge(
        SttBackendStat(
            name=name,
            healthy=healthy,
            enabled=kw.pop("enabled", True),
            updated_at=kw.pop("updated_at", datetime.now(UTC)),
            **kw,
        )
    )
    session.commit()


def test_a_backend_outage_parks_the_job_with_a_reason(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Parked, so the operator sees it — and annotated, so a later pass can find it."""
    from app.stt.router import NoBackendAvailableError

    call, row = park(
        session,
        worker,
        tmp_path,
        NoBackendAvailableError("every STT backend failed; last error: whisper: giving up"),
    )
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.parked_reason == PARKED_BACKEND_UNAVAILABLE
    assert session.get(Call, call.id).transcript_status == "failed"


def test_a_content_failure_parks_with_no_reason(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Anything that is not the router giving up is a verdict on this recording.

    Requeuing it automatically would retry it for ever, which is a loop rather than a
    recovery, so it parks with ``parked_reason`` NULL and only a person moves it.
    """
    _, row = park(session, worker, tmp_path, RuntimeError("ffmpeg: invalid data found"))
    assert (row.status, row.parked_reason) == ("failed", None)


def test_a_disabled_backend_still_never_counts_an_attempt(session, worker, tmp_path) -> None:  # noqa: ANN001
    """``NoBackendEnabledError`` subclasses the outage error; the wait branch still wins."""
    from app.stt.router import NoBackendEnabledError

    _, job_row = seed_call(session, root=tmp_path)
    worker.fail(worker.claim(), NoBackendEnabledError("no STT backend is enabled and usable"))
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts, row.parked_reason) == ("queued", 0, None)


def test_the_unpark_pass_requeues_the_outage_jobs_once_a_backend_is_healthy(  # noqa: ANN001
    session, worker, tmp_path
) -> None:
    from app.stt.router import NoBackendAvailableError

    outage_call, outage = park(
        session, worker, tmp_path, NoBackendAvailableError("every STT backend failed"), tgid=1101
    )
    content_call, content = park(
        session,
        worker,
        tmp_path,
        RuntimeError("ffmpeg: invalid data found"),
        tgid=1102,
        file_path="20260829/EC EVENT 7/call.mp3",
    )
    outage_error = outage.last_error
    publish_backend(session)

    assert unpark_backend_outage(session) == 1

    session.expire_all()
    row = session.get(TranscriptionJob, outage.id)
    assert (row.status, row.attempts, row.parked_reason) == ("queued", 0, None)
    assert (row.locked_by, row.locked_at, row.started_at, row.finished_at) == (None, None, None, None)
    assert row.scheduled_at <= datetime.now(UTC) + timedelta(seconds=1)
    # The error the outage left is kept, prefixed: the row still says what happened, and
    # also says that nobody had to come and rescue it.
    assert row.last_error == AUTO_REQUEUE_PREFIX + outage_error
    assert session.get(Call, outage_call.id).transcript_status == "queued"

    # …and the content failure is left exactly where it was.
    still = session.get(TranscriptionJob, content.id)
    assert (still.status, still.parked_reason) == ("failed", None)
    assert session.get(Call, content_call.id).transcript_status == "failed"

    assert unpark_backend_outage(session) == 0  # nothing parked, nothing to do


@pytest.mark.parametrize(
    ("healthy", "config_enabled", "age_s", "why"),
    [
        (False, True, 0, "the backend is answering, badly"),
        (None, True, 0, "nothing has probed it yet"),
        (True, False, 0, "the operator has switched it off"),
        (True, True, 600, "the worker stopped publishing, so the row is stale"),
    ],
)
def test_the_unpark_pass_waits_while_no_backend_is_usable(  # noqa: ANN001
    session, worker, tmp_path, healthy, config_enabled, age_s, why
) -> None:
    """Requeuing into an outage would just park the queue again three attempts later."""
    from app.stt.router import NoBackendAvailableError

    _, outage = park(session, worker, tmp_path, NoBackendAvailableError("every STT backend failed"))
    publish_backend(
        session,
        healthy=healthy,
        config_enabled=config_enabled,
        updated_at=datetime.now(UTC) - timedelta(seconds=age_s),
    )

    assert unpark_backend_outage(session) == 0, why
    session.expire_all()
    assert session.get(TranscriptionJob, outage.id).status == "failed"


def test_the_unpark_pass_writes_one_activity_record(session, worker, tmp_path) -> None:  # noqa: ANN001
    """One line in the operator's log per recovery, and silence when there was nothing to do."""
    from app.models import ActivityEvent
    from app.stt.router import NoBackendAvailableError

    park(session, worker, tmp_path, NoBackendAvailableError("every STT backend failed"))
    publish_backend(session)

    assert unpark_backend_outage(session) == 1
    events = list(
        session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "transcribe.unparked"))
    )
    assert len(events) == 1
    assert events[0].message == "1 jobs requeued after the STT outage ended"
    assert (events[0].source, events[0].level) == ("transcribe", "info")

    assert unpark_backend_outage(session) == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(ActivityEvent)
            .where(ActivityEvent.kind == "transcribe.unparked")
        )
        == 1
    )


def test_requeue_failed_moves_everything_when_nothing_is_asked_of_it(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The shape the admin route calls: every failed job, whatever parked it, error cleared."""
    from app.stt.router import NoBackendAvailableError

    _, outage = park(
        session, worker, tmp_path, NoBackendAvailableError("every STT backend failed"), tgid=1101
    )
    _, content = park(
        session,
        worker,
        tmp_path,
        RuntimeError("ffmpeg: invalid data found"),
        tgid=1102,
        file_path="20260829/EC EVENT 7/call.mp3",
    )

    assert requeue_failed(session) == 2
    session.commit()
    session.expire_all()
    for job_id in (outage.id, content.id):
        row = session.get(TranscriptionJob, job_id)
        assert (row.status, row.attempts, row.last_error, row.parked_reason) == (
            "queued",
            0,
            None,
            None,
        )


def test_requeue_failed_takes_at_most_one_batch(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The unpark pass is bounded, so one outage cannot rewrite the archive in a transaction."""
    from app.stt.router import NoBackendAvailableError

    for n in range(3):
        park(
            session,
            worker,
            tmp_path,
            NoBackendAvailableError("every STT backend failed"),
            tgid=1200 + n,
            file_path=f"20260829/EC EVENT {n}/call.mp3",
        )

    assert requeue_failed(session, parked_reason=PARKED_BACKEND_UNAVAILABLE, limit=2) == 2
    session.commit()
    assert (
        session.scalar(
            select(func.count())
            .select_from(TranscriptionJob)
            .where(TranscriptionJob.status == "failed")
        )
        == 1
    )


# --------------------------------------------------------------------------------------
# stale reaper
# --------------------------------------------------------------------------------------


def test_requeue_stale_reclaims_a_dead_workers_job(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    worker.claim()
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    row.locked_at = datetime.now(UTC) - timedelta(minutes=45)
    session.commit()

    assert requeue_stale(session, 10) == 1

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.locked_by, row.started_at) == ("queued", None, None)
    assert session.get(Call, call.id).transcript_status == "queued"
    assert requeue_stale(session, 10) == 0  # nothing left to reap


def test_requeue_stale_leaves_a_live_job_alone(session, worker, tmp_path) -> None:  # noqa: ANN001
    _, job_row = seed_call(session, root=tmp_path)
    worker.claim()

    assert requeue_stale(session, 10) == 0
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "running"


def test_claim_skips_a_job_whose_audio_is_gone(session, worker, tmp_path) -> None:  # noqa: ANN001
    """No file, no request to a backend — the job is parked with the reason."""
    call, job_row = seed_call(session, root=tmp_path)
    (tmp_path / call.file_path).unlink()

    assert worker.claim() is None

    session.expire_all()
    parked = session.get(TranscriptionJob, job_row.id)
    assert parked.status == "failed"
    assert "no audio" in parked.last_error
    assert session.get(Call, call.id).transcript_status == "skipped"


def test_claim_refuses_a_file_path_that_escapes_the_archive(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The row is the only thing that says where the audio is; it is not trusted."""
    call, job_row = seed_call(session, root=tmp_path)
    call.file_path = "../../../etc/hostname"
    session.commit()

    assert worker.claim() is None
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "failed"


def test_a_human_correction_survives_a_re_transcribe(session, worker, tmp_path) -> None:  # noqa: ANN001
    """M7: the engine's fresh guess is stored, but it does not displace an edited primary."""
    call, job_row = seed_call(session, root=tmp_path)
    corrected = Transcript(
        call_id=call.id,
        engine="openai_compatible",
        text_="engine one responding",
        edited_text="Engine 1 responding to Riverton.",
        edited_by_user=True,
        is_primary=True,
    )
    session.add(corrected)
    call.transcript_text = corrected.edited_text
    session.commit()

    job = worker.claim()
    worker.complete(
        job,
        make_result("engine won responding two riverton"),
        QualityAssessment(
            quality=Quality.GOOD, cleaned_text="engine won responding two riverton", reasons=[]
        ),
    )

    session.expire_all()
    rows = list(session.scalars(select(Transcript).where(Transcript.call_id == call.id)))
    assert len(rows) == 2
    primary = next(r for r in rows if r.is_primary)
    assert primary.id == corrected.id
    assert session.get(Call, call.id).transcript_text == "Engine 1 responding to Riverton."
    assert session.get(TranscriptionJob, job_row.id).status == "done"


# --------------------------------------------------------------------------------------
# a caption typed before the queued job ran
# --------------------------------------------------------------------------------------

CAPTION = "Engine 1 responding to Riverton."


def caption(session, call: Call) -> None:  # noqa: ANN001
    """What ``PATCH /api/calls/{id}`` writes for a caption on an untranscribed call."""
    session.add(
        Transcript(
            call_id=call.id,
            engine="manual",
            text_=CAPTION,
            edited_text=CAPTION,
            edited_by_user=True,
            is_primary=True,
        )
    )
    call.transcript_text = CAPTION
    call.transcript_status = "done"
    session.commit()


def test_a_captioned_call_stays_done_while_its_job_is_claimed(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    caption(session, call)

    job = worker.claim()

    assert job is not None  # the job still runs: its transcript is kept beside the caption
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "running"
    stored = session.get(Call, call.id)
    assert (stored.transcript_status, stored.transcript_text) == ("done", CAPTION)


def test_a_captioned_call_stays_done_when_its_job_fails(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    caption(session, call)
    for attempt in range(3):
        job = worker.claim()
        assert job is not None
        worker.fail(job, RuntimeError("backend is down"))
        session.expire_all()
        row = session.get(TranscriptionJob, job_row.id)
        # requeued with backoff, then parked — the call says done throughout
        assert row.status == ("failed" if attempt == 2 else "queued")
        assert session.get(Call, call.id).transcript_status == "done"
        if row.status == "queued":
            row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

    stored = session.get(Call, call.id)
    assert (stored.transcript_status, stored.transcript_text) == ("done", CAPTION)
    primary = session.scalars(
        select(Transcript).where(Transcript.call_id == call.id, Transcript.is_primary.is_(True))
    ).one()
    assert primary.engine == "manual"

    # …and the operator's requeue of the parked job does not put it back to queued either
    assert requeue_failed(session) == 1
    session.commit()
    session.expire_all()
    assert session.get(Call, call.id).transcript_status == "done"


def test_a_captioned_call_stays_done_when_its_job_goes_stale(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    caption(session, call)
    worker.claim()
    session.expire_all()
    session.get(TranscriptionJob, job_row.id).locked_at = datetime.now(UTC) - timedelta(minutes=45)
    session.commit()

    assert requeue_stale(session, 10) == 1

    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "queued"
    assert session.get(Call, call.id).transcript_status == "done"


def test_an_uncaptioned_call_still_follows_its_job(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The control: an engine transcript, even an edited one, is not a caption."""
    call, _ = seed_call(session, root=tmp_path)
    session.add(
        Transcript(
            call_id=call.id,
            engine="openai_compatible",
            text_="engine one",
            edited_text="Engine 1",
            edited_by_user=True,
            is_primary=True,
        )
    )
    session.commit()

    job = worker.claim()
    session.expire_all()
    assert session.get(Call, call.id).transcript_status == "running"
    worker.fail(job, RuntimeError("connection reset"))
    session.expire_all()
    assert session.get(Call, call.id).transcript_status == "queued"


# --------------------------------------------------------------------------------------
# backlog fairness (§5.2)
# --------------------------------------------------------------------------------------


def backlog_worker(session, settings, tmp_path, *, workers: int = 2, share: float = 0.5):  # noqa: ANN001, ANN201, ARG001
    from app.db import SessionLocal

    return SttWorker(
        router=FakeRouter(make_result()),
        settings=settings.model_copy(update={"stt_backlog_worker_share": share}),
        session_factory=SessionLocal,
        worker_name="fairness",
        audio_root=tmp_path,
        worker_count=workers,
    )


@pytest.mark.parametrize(
    ("workers", "share", "expected"),
    [(2, 0.5, 1), (4, 0.5, 2), (4, 0.25, 1), (1, 0.5, 1), (8, 1.0, 8), (4, 0.0, 1)],
)
def test_the_backlog_limit_is_a_share_of_the_threads_floored_at_one(
    session, settings, tmp_path, workers, share, expected  # noqa: ANN001
) -> None:
    worker = backlog_worker(session, settings, tmp_path, workers=workers, share=share)
    assert worker.backlog_limit == expected


def test_only_the_share_of_threads_may_hold_backlog_jobs(session, settings, tmp_path) -> None:  # noqa: ANN001
    """With two threads and a 0.5 share, one backlog job at a time — the rest wait.

    ``priority ASC`` alone hands out every backlog job the moment the live queue empties,
    and a thread already inside a forty-second recording cannot be preempted when the next
    live call lands.  The guard is what keeps a thread free for it.
    """
    worker = backlog_worker(session, settings, tmp_path, workers=2, share=0.5)
    _, live_job = seed_call(session, root=tmp_path, tgid=1103, priority=100, file_path="live.mp3")
    seed_call(session, root=tmp_path, tgid=1101, priority=600, file_path="backlog-a.mp3")
    seed_call(session, root=tmp_path, tgid=1102, priority=600, file_path="backlog-b.mp3")

    first = worker.claim()
    assert first is not None and first.id == live_job.id  # priority ASC: live first

    second = worker.claim()
    assert second is not None and second.priority == 600  # the one backlog slot

    # Only backlog is left and the slot is still held, so the queue reads empty from here.
    assert worker.claim() is None


def test_finishing_a_backlog_job_gives_the_slot_back(session, settings, tmp_path) -> None:  # noqa: ANN001
    worker = backlog_worker(session, settings, tmp_path, workers=2, share=0.5)
    seed_call(session, root=tmp_path, tgid=1101, priority=600, file_path="backlog-a.mp3")
    seed_call(session, root=tmp_path, tgid=1102, priority=600, file_path="backlog-b.mp3")

    first = worker.claim()
    assert first is not None
    assert worker.claim() is None  # the only backlog slot is taken

    worker.complete(
        first,
        make_result(),
        QualityAssessment(quality=Quality.GOOD, cleaned_text="ok", reasons=[]),
    )

    second = worker.claim()
    assert second is not None and second.priority == 600


def test_a_failed_backlog_job_gives_the_slot_back_too(session, settings, tmp_path) -> None:  # noqa: ANN001
    worker = backlog_worker(session, settings, tmp_path, workers=2, share=0.5)
    seed_call(session, root=tmp_path, tgid=1101, priority=600, file_path="backlog-a.mp3")
    seed_call(session, root=tmp_path, tgid=1102, priority=600, file_path="backlog-b.mp3")

    first = worker.claim()
    assert first is not None
    worker.fail(first, RuntimeError("backend down"))

    assert worker.claim() is not None


def test_a_live_job_takes_no_backlog_slot(session, settings, tmp_path) -> None:  # noqa: ANN001
    worker = backlog_worker(session, settings, tmp_path, workers=2, share=0.5)
    seed_call(session, root=tmp_path, tgid=1101, priority=100, file_path="live-a.mp3")
    seed_call(session, root=tmp_path, tgid=1102, priority=100, file_path="live-b.mp3")
    seed_call(session, root=tmp_path, tgid=1103, priority=600, file_path="backlog.mp3")

    assert worker.claim() is not None
    assert worker.claim() is not None
    third = worker.claim()
    assert third is not None and third.priority == 600  # the slot was never spent


# --------------------------------------------------------------------------------------
# the hallucination sanity guard
# --------------------------------------------------------------------------------------

#: The shape of a prompt-echo transcript on a 1.512 s call: the system's own STT
#: vocabulary, continued by the decoder and looped eight times.
PROMPT_ECHO = ", ".join(["LKPD DISP, NF PW 1507, Millbrook Road, EC LAW 4, Riverton Fire"] * 8)


def test_the_live_prompt_echo_loop_is_caught() -> None:
    assert len(PROMPT_ECHO) > 500
    reason = hallucination_reason(PROMPT_ECHO, 1.512)
    assert reason is not None
    # 533-odd characters over 1.5 s is the first thing that condemns it…
    assert reason.startswith("chars_per_second=")
    # …and the loop test alone would have caught it even with no duration to go on.
    assert (hallucination_reason(PROMPT_ECHO, None) or "").startswith("repeat_loop:")


def test_normal_speech_of_the_same_length_over_thirty_seconds_passes() -> None:
    """Same character count, a plausible amount of airtime: nothing is wrong with it."""
    real = (
        "Lakeside, EC Law 4, I'm out with a silver Malibu at the gas station on North "
        "Fork Road, state registration 3 Charlie Bravo 519. Start me a second unit when "
        "one clears, I've got three occupants and only one of them is talking. Advise the "
        "shift sergeant we may need a tow for this one, it is blocking the north lane. "
        "Also be advised the driver says the registration belongs to his brother, so run "
        "it both ways before you clear me, and let Eastside know we have their vehicle."
    )
    assert abs(len(real) - len(PROMPT_ECHO)) < 60
    assert hallucination_reason(real, 30.0) is None


def test_a_short_call_with_short_text_passes() -> None:
    assert hallucination_reason("Copy that, 10-4, en route.", 1.4) is None
    assert hallucination_reason("", 1.4) is None


def test_a_phrase_said_twice_is_not_a_loop() -> None:
    """Real dispatch repeats itself; the guard must leave a rebroadcast alone."""
    bolo = (
        "Attention all units, be on the lookout for a dark in color, Buick four-door sedan, "
        "dark in color, Buick four-door sedan, last seen westbound on Highway 9."
    )
    assert hallucination_reason(bolo, 12.0) is None


@pytest.mark.parametrize(
    ("text", "duration"),
    [
        ("10-4, 10-4.", 1.6),
        ("Copy, copy.", 1.4),
        ("Roger. Roger. Roger.", 2.8),
        (" ".join(["Unit 12 copy."] * 4), 6.0),
    ],
)
def test_an_acknowledgment_said_over_and_over_is_kept(text: str, duration: float) -> None:
    """A short call whose whole content is one acknowledgment repeated is not a loop.

    Every one of these collapses to a fraction of itself — "10-4, 10-4." loses half — so
    the shrink test alone condemned them all, and discarding them threw away the only
    words the call had.  The character floor and the three-phrase minimum are what tell a
    dispatcher repeating herself from a decoder going round.
    """
    assert hallucination_reason(text, duration) is None
    # …and with no duration to go on either, since the repeat rules stand alone there.
    assert hallucination_reason(text, None) is None


def test_a_radio_check_repeated_four_times_is_not_a_loop() -> None:
    """The ×4 regex rule fires on this — the 80-character floor is what saves it."""
    text = "Unit 12 copy. " * 4
    assert len(text.strip()) < MIN_HALLUCINATION_CHARS
    assert _LOOP_RE.search(text) is not None  # the loop rule, left to itself, condemns it
    assert hallucination_reason(text, 6.0) is None


def test_the_phrase_rule_still_catches_a_long_echo_without_a_verbatim_run() -> None:
    """Above the floor, with four phrases dropped, the shrink test still condemns text.

    The unit drifts by a digit each time, so no substring repeats verbatim and the ×4
    regex sees nothing; what remains is the collapse, which is the case that rule exists
    for.
    """
    unit = "Riverton Fire North Fork EC LAW {n}"
    text = ", ".join(unit.format(n=n) for n in range(5) for _ in range(3))
    assert _LOOP_RE.search(text) is None
    assert hallucination_reason(text, 300.0) == "repeat_loop:phrases"


def test_complete_discards_a_hallucination_and_logs_it(session, worker, tmp_path) -> None:  # noqa: ANN001
    from app.models import ActivityEvent

    call, _ = seed_call(session, root=tmp_path, duration=1.512)
    worker.complete(
        worker.claim(),
        make_result(PROMPT_ECHO, duration=1.512),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text=PROMPT_ECHO),
    )

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.transcript_status == "empty"
    assert stored.transcript_text is None
    assert stored.transcript_quality == "noise"

    # The job still finished — this is the engine failing, not the worker.
    job_row = session.execute(
        select(TranscriptionJob).where(TranscriptionJob.call_id == call.id)
    ).scalar_one()
    assert job_row.status == "done"

    # The raw text is kept on the transcript row so an operator can see what happened.
    transcript = session.execute(
        select(Transcript).where(Transcript.call_id == call.id)
    ).scalar_one()
    assert transcript.text_ == PROMPT_ECHO
    assert any(r.startswith("hallucination:") for r in transcript.quality_reasons)

    event = session.execute(
        select(ActivityEvent).where(ActivityEvent.kind == "stt.hallucination")
    ).scalar_one()
    assert event.source == "stt"
    assert event.level == "warn"
    assert event.call_id == call.id
    assert event.meta["chars"] == len(PROMPT_ECHO)


def test_hallucination_reason_catches_a_caption_credit() -> None:
    """The phrase families reach the worker's guard, so the call keeps no text (§5.2)."""
    reason = hallucination_reason("Closed captioning provided by WKNO-Memphis.", 2.1)
    assert reason == "hallucination_phrase:credit:closed caption"
    assert hallucination_reason("Engine 1 responding to Riverton.", 2.1) is None


def test_hallucination_reason_gates_the_signoff_family_on_duration() -> None:
    """A 4.9 s "116, goodbye." is a unit clearing the air; two seconds of "Goodbye." is not."""
    assert hallucination_reason("116, goodbye.", 4.9) is None
    assert hallucination_reason("Goodbye, everyone.", 9.0) is None
    assert hallucination_reason("Goodbye, everyone.", 2.0) == "hallucination_phrase:signoff:goodbye"


def test_hallucination_reason_catches_a_chained_signoff() -> None:
    """Two sign-offs chained ("…, and I'll see you next time") are still the whole utterance."""
    text = "Thank you for joining us, and we'll see you next time."
    assert hallucination_reason(text, 1.7) is not None
    assert hallucination_reason(text, 1.7).startswith("hallucination_phrase:signoff:")
    # a pure outro is noise on a long call too; one word beside it ("today") keeps it
    assert hallucination_reason(text, 9.0) is not None
    assert hallucination_reason("Thank you for joining us today, and we'll see you next time.", 9.0) is None
    assert hallucination_reason("10-4, thanks for listening.", 1.5) is None


def test_hallucination_reason_merges_operator_patterns() -> None:
    from app.stt.postprocess import compile_patterns

    # not opening with a broadcast-credit frame ("This program was …"), so only the
    # operator's pattern can condemn it
    text = "Underwriting was brought to you by viewers like you."
    assert hallucination_reason(text, 3.0) is None
    patterns = compile_patterns("brought to you by")
    assert hallucination_reason(text, 3.0, patterns) == "hallucination_phrase:custom:brought to you by"


def test_complete_discards_a_caption_credit_phrase(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A credit line assessed as noise leaves the call empty and text-free, not 'done'."""
    text = "Closed captioning provided by WKNO-Memphis."
    call, _ = seed_call(session, root=tmp_path, duration=2.0)
    worker.complete(
        worker.claim(),
        make_result(text, duration=2.0),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text=text),
    )

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.transcript_status == "empty"
    assert stored.transcript_text is None
    assert stored.transcript_quality == "noise"
    transcript = session.execute(
        select(Transcript).where(Transcript.call_id == call.id)
    ).scalar_one()
    assert transcript.text_ == text  # nothing is destroyed
    assert "hallucination:hallucination_phrase:credit:closed caption" in transcript.quality_reasons


def test_complete_discards_a_broadcast_credit_frame(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The broadcast-credit family takes the same path: noise, and the call keeps no text."""
    from app.stt.postprocess import CREDIT_FRAME_PATTERNS

    text = "This is a presentation of the U.S. Department of Defense."
    call, _ = seed_call(session, root=tmp_path, duration=2.0)
    worker.complete(
        worker.claim(),
        make_result(text, duration=2.0),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text=text),
    )

    session.expire_all()
    stored = session.get(Call, call.id)
    assert (stored.transcript_status, stored.transcript_text, stored.transcript_quality) == (
        "empty",
        None,
        "noise",
    )
    transcript = session.execute(
        select(Transcript).where(Transcript.call_id == call.id)
    ).scalar_one()
    assert transcript.text_ == text
    assert transcript.quality == "noise"
    frame = CREDIT_FRAME_PATTERNS[0].pattern[:80]
    assert f"hallucination:hallucination_phrase:credit_frame:{frame}" in transcript.quality_reasons


# --------------------------------------------------------------------------------------
# job ownership
# --------------------------------------------------------------------------------------


GOOD = QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text="Engine 1 responding to Riverton.")


def cancel_like_a_forced_retranscribe(session, call_id: int) -> TranscriptionJob:  # noqa: ANN001
    """What ``POST /calls/{id}/transcribe {"force": true}`` does: cancel, queue a new job."""
    from app.db import lock_calls

    lock_calls(session, call_id)
    call = session.get(Call, call_id)
    session.refresh(call)
    call.transcript_status = "queued"
    for open_job in session.scalars(
        select(TranscriptionJob).where(
            TranscriptionJob.call_id == call_id, TranscriptionJob.status.in_(("queued", "running"))
        )
    ):
        open_job.status = "cancelled"
        open_job.finished_at = datetime.now(UTC)
    session.flush()
    replacement = TranscriptionJob(
        call_id=call_id,
        status="queued",
        priority=1,
        max_attempts=3,
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(replacement)
    session.commit()
    return replacement


def transcribed_events(session, call_id: int) -> int:  # noqa: ANN001
    from app.models import ActivityEvent

    return session.scalar(
        select(func.count())
        .select_from(ActivityEvent)
        .where(ActivityEvent.kind == "transcribe.done", ActivityEvent.call_id == call_id)
    )


def test_a_cancelled_job_cannot_be_completed_by_its_old_claimant(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, old_row = seed_call(session, root=tmp_path)
    old = worker.claim()
    assert old is not None and old.claimed_at is not None
    replacement = cancel_like_a_forced_retranscribe(session, call.id)

    new = worker.claim()
    assert new is not None and new.id == replacement.id
    worker.complete(
        new,
        make_result("the forced one"),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text="the forced one"),
    )
    # the old thread finishes afterwards — its result must not land anywhere
    worker.complete(old, make_result("the stale one"), GOOD)

    session.expire_all()
    assert session.get(TranscriptionJob, old_row.id).status == "cancelled"
    assert session.get(TranscriptionJob, replacement.id).status == "done"
    rows = session.scalars(select(Transcript).where(Transcript.call_id == call.id)).all()
    assert [(r.text_, r.is_primary) for r in rows] == [("the forced one", True)]
    assert session.get(Call, call.id).transcript_text == "the forced one"
    assert transcribed_events(session, call.id) == 1


def test_a_cancelled_job_cannot_be_failed_by_its_old_claimant(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, old_row = seed_call(session, root=tmp_path)
    old = worker.claim()
    cancel_like_a_forced_retranscribe(session, call.id)

    worker.fail(old, RuntimeError("the old request finally timed out"))

    session.expire_all()
    row = session.get(TranscriptionJob, old_row.id)
    assert (row.status, row.attempts, row.last_error) == ("cancelled", 0, None)
    assert session.get(Call, call.id).transcript_status == "queued"  # the new job's


def test_a_job_the_audio_pass_cancelled_mid_flight_is_not_completed(session, worker, tmp_path) -> None:  # noqa: ANN001
    """``empty_clip_meta`` cancels a *running* job too; the late result is dropped and the
    call stays ``empty`` rather than ``running`` for ever."""
    from app.audioproc.jobs import empty_clip_meta

    call, job_row = seed_call(session, root=tmp_path)
    job = worker.claim()
    stored = session.get(Call, call.id)
    session.refresh(stored)
    assert stored.transcript_status == "running"
    empty_clip_meta(session, stored, "no frames")
    session.commit()

    worker.complete(job, make_result("Thank you."), GOOD)

    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "cancelled"
    assert session.get(Call, call.id).transcript_status == "empty"
    assert session.scalar(select(func.count()).select_from(Transcript).where(Transcript.call_id == call.id)) == 0


def test_a_job_requeued_as_stale_and_reclaimed_belongs_to_the_second_claim(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, job_row = seed_call(session, root=tmp_path)
    first = worker.claim()
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    row.locked_at = datetime.now(UTC) - timedelta(minutes=45)
    session.commit()
    assert requeue_stale(session, 10) == 1

    second = worker.claim()
    assert second is not None and second.id == first.id
    assert second.claimed_at != first.claimed_at

    worker.complete(first, make_result("first claimant"), GOOD)  # discarded
    worker.fail(first, RuntimeError("first claimant gave up"))  # discarded too
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts) == ("running", 0)
    assert session.scalar(select(func.count()).select_from(Transcript).where(Transcript.call_id == call.id)) == 0

    worker.complete(
        second,
        make_result("second claimant"),
        QualityAssessment(quality=Quality.GOOD, reasons=[], cleaned_text="second claimant"),
    )
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "done"
    assert session.get(Call, call.id).transcript_text == "second claimant"
    assert transcribed_events(session, call.id) == 1


def test_the_heartbeat_keeps_a_slow_job_from_being_reaped(session, worker, tmp_path) -> None:  # noqa: ANN001
    _, job_row = seed_call(session, root=tmp_path)
    job = worker.claim()
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    row.locked_at = datetime.now(UTC) - timedelta(minutes=45)  # as if the request hung
    session.commit()

    assert worker.heartbeat() == 1

    assert requeue_stale(session, 10) == 0
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert row.status == "running"
    assert row.locked_at > datetime.now(UTC) - timedelta(minutes=1)

    worker.complete(job, make_result(), GOOD)
    assert worker.heartbeat() == 0  # nothing left to beat for


def test_the_heartbeat_does_not_refresh_somebody_elses_claim(session, worker, settings, tmp_path) -> None:  # noqa: ANN001
    from app.db import SessionLocal

    _, job_row = seed_call(session, root=tmp_path)
    worker.claim()
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    row.locked_at = datetime.now(UTC) - timedelta(minutes=45)
    session.commit()
    assert requeue_stale(session, 10) == 1
    other = SttWorker(
        router=FakeRouter(make_result()),
        settings=settings,
        session_factory=SessionLocal,
        worker_name="other-process",
        audio_root=tmp_path,
    )
    assert other.claim() is not None

    assert worker.heartbeat() == 0  # the first process's claim is gone
    assert other.heartbeat() == 1


def test_the_heartbeat_gives_up_on_a_claim_past_its_maximum_age(session, worker, tmp_path) -> None:  # noqa: ANN001
    from app.stt.worker import HEARTBEAT_MAX_AGE_S

    _, job_row = seed_call(session, root=tmp_path)
    job = worker.claim()
    worker._active[job.id] = job.claimed_at - timedelta(seconds=HEARTBEAT_MAX_AGE_S + 60)  # noqa: SLF001
    assert worker.heartbeat() == 0


def test_two_failed_rows_for_one_call_requeue_once_and_the_batch_commits(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A forced re-transcribe during an outage leaves two failed rows for one call; both
    used to be requeued, the open-job unique index aborted the batch, and the automatic
    unpark retried the same rows for ever."""
    from app.stt.router import NoBackendAvailableError

    outage = NoBackendAvailableError("every STT backend failed")
    call, older = park(session, worker, tmp_path, outage, tgid=1101)
    newer = TranscriptionJob(
        call_id=call.id,
        status="failed",
        max_attempts=3,
        attempts=3,
        parked_reason=PARKED_BACKEND_UNAVAILABLE,
        last_error="NoBackendAvailableError: every STT backend failed",
    )
    session.add(newer)
    session.commit()
    other_call, other = park(
        session, worker, tmp_path, outage, tgid=1102, file_path="20260829/EC EVENT 7/call.mp3"
    )

    assert requeue_failed(session, parked_reason=PARKED_BACKEND_UNAVAILABLE) == 2
    session.commit()

    session.expire_all()
    assert session.get(TranscriptionJob, newer.id).status == "queued"  # the newest one
    assert session.get(TranscriptionJob, older.id).status == "failed"
    assert session.get(TranscriptionJob, other.id).status == "queued"
    assert session.get(Call, call.id).transcript_status == "queued"
    assert session.get(Call, other_call.id).transcript_status == "queued"
    # and a second pass has nothing left it may move: the call already has an open job
    assert requeue_failed(session, parked_reason=PARKED_BACKEND_UNAVAILABLE) == 0
    session.commit()


def test_requeue_failed_skips_a_call_that_already_has_an_open_job(session, worker, tmp_path) -> None:  # noqa: ANN001
    call, parked = park(session, worker, tmp_path, RuntimeError("bad audio"))
    session.add(TranscriptionJob(call_id=call.id, status="queued", max_attempts=3))
    session.commit()

    assert requeue_failed(session) == 0
    session.commit()
    session.expire_all()
    assert session.get(TranscriptionJob, parked.id).status == "failed"


def test_an_exception_in_options_for_fails_the_job_and_releases_the_slot(session, settings, tmp_path) -> None:  # noqa: ANN001
    from app.stt.runner import JobRunner

    worker = backlog_worker(session, settings, tmp_path, workers=2, share=0.5)
    _, job_row = seed_call(session, root=tmp_path, priority=600, file_path="backlog-a.mp3")

    def broken_options(job):  # noqa: ANN001, ANN202, ARG001
        raise RuntimeError("settings table unreadable")

    runner = JobRunner(
        claim=worker.claim,
        complete=worker.complete,
        fail=worker.fail,
        router=worker.router,
        options_for=broken_options,
        workers=1,
    )
    job = worker.claim()
    assert job is not None and worker._backlog_held == {job.id}  # noqa: SLF001

    runner._handle(job)  # noqa: SLF001 - must not raise, must not leak the slot

    assert worker._backlog_held == set()  # noqa: SLF001
    assert runner.failed == 1
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts) == ("queued", 1)
    assert "settings table unreadable" in row.last_error


# --------------------------------------------------------------------------------------
# conversational words
# --------------------------------------------------------------------------------------


def test_a_real_one_and_a_half_second_okay_keeps_its_text(session, worker, tmp_path) -> None:  # noqa: ANN001
    from app.stt.postprocess import assess

    call, _ = seed_call(session, root=tmp_path, duration=1.5)
    result = make_result("Okay.", duration=1.5, no_speech_prob=0.05)
    assert hallucination_reason("Okay.", 1.5, no_speech_prob=0.05) is None

    worker.complete(worker.claim(), result, assess(result, 1.5))

    session.expire_all()
    stored = session.get(Call, call.id)
    assert (stored.transcript_text, stored.transcript_status) == ("Okay.", "done")
    assert stored.transcript_quality == "suspect"


def test_the_thirty_second_thank_you_is_still_discarded(session, worker, tmp_path) -> None:  # noqa: ANN001
    from app.stt.postprocess import assess

    call, _ = seed_call(session, root=tmp_path, duration=30.0)
    result = make_result("Thank you.", duration=30.0, no_speech_prob=0.1)
    assert hallucination_reason("Thank you.", 30.0, no_speech_prob=0.1) == (
        "hallucination_phrase:phrase:thank you"
    )

    worker.complete(worker.claim(), result, assess(result, 30.0))

    session.expire_all()
    stored = session.get(Call, call.id)
    assert (stored.transcript_text, stored.transcript_status) == (None, "empty")


# --------------------------------------------------------------------------------------
# a recording every backend chokes on is not an outage (call 5237413, 2026-09-23)
# --------------------------------------------------------------------------------------
#
# These run the real router over real OpenAI-compatible clients, with respx standing in
# for the three Speaches servers, so what is under test is the whole chain the production
# job went round: the client's error, the router's verdict and the worker's bookkeeping.

WHISPER_MODEL = "Systran/faster-whisper-large-v3"
WHISPER_BASES = ("http://p0.test:8000", "http://p1.test:8001", "http://p2.test:8002")


def whisper_router():  # noqa: ANN201
    from app.stt.openai_compatible import OpenAICompatibleBackend
    from app.stt.router import BackendRouter

    return BackendRouter(
        [
            OpenAICompatibleBackend(
                name=f"p{i}",
                base_url=base,
                model=WHISPER_MODEL,
                priority=10 + i,
                attempts=3,
                backoff_base_s=0.0,
            )
            for i, base in enumerate(WHISPER_BASES)
        ]
    )


def mock_whisper(respx_mock, *, transcribe, health_ok: bool = True) -> None:  # noqa: ANN001
    """Point every server's health probe and transcription endpoint at the given behavior."""
    import httpx

    for base in WHISPER_BASES:
        if health_ok:
            respx_mock.get(f"{base}/v1/models").mock(
                return_value=httpx.Response(200, json={"data": [{"id": WHISPER_MODEL}]})
            )
        else:
            respx_mock.get(f"{base}/v1/models").mock(side_effect=httpx.ConnectError("refused"))
        respx_mock.post(f"{base}/v1/audio/transcriptions").mock(side_effect=transcribe)


def run_once(worker, router):  # noqa: ANN001, ANN201
    """What JobRunner._handle does with one claimed job, minus the threads."""
    job = worker.claim()
    assert job is not None, "the job has to be claimable"
    options = worker.options_for(job).model_copy(update={"normalize_filter": None})
    try:
        router.transcribe(Path(job.path), options, prefer=job.prefer)
    except Exception as exc:  # noqa: BLE001 - the runner hands every failure to fail()
        worker.fail(job, exc)
        return exc
    raise AssertionError("the transcription was expected to fail")


def make_due(session, job_id: int) -> TranscriptionJob:  # noqa: ANN001
    session.expire_all()
    row = session.get(TranscriptionJob, job_id)
    if row.status == "queued":
        row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    return row


def serve_other_calls(router, tmp_path) -> None:  # noqa: ANN001
    """Each server transcribes somebody else's call — the evidence that it is up."""
    other = tmp_path / "other.mp3"
    other.write_bytes(b"\xff\xfb\x90d" + b"\x00" * 64)
    for backend in router.backends:
        router.transcribe(other, TranscribeOptions(), prefer=backend.name)


def test_a_file_every_backend_times_out_on_uses_its_attempts_and_parks_for_good(
    session, worker, tmp_path  # noqa: ANN001
) -> None:
    import httpx
    import respx

    from app.stt.router import RequestFailedEverywhereError

    call, job_row = seed_call(session, root=tmp_path, duration=43.259, priority=600)
    router = whisper_router()
    ok = httpx.Response(200, json={"text": "Engine 1 en route.", "segments": [], "duration": 2.0})
    with respx.mock(assert_all_called=False) as mock:
        mock_whisper(mock, transcribe=[ok])  # one success per server, for the other call
        serve_other_calls(router, tmp_path)
        mock_whisper(mock, transcribe=httpx.ReadTimeout("timed out"))

        for attempt in range(1, 4):
            exc = run_once(worker, router)
            assert isinstance(exc, RequestFailedEverywhereError), exc
            row = make_due(session, job_row.id)
            assert row.attempts == attempt
        # one request per server per attempt: a read timeout is not retried on the same one
        posts = [c.request for c in mock.calls if c.request.method == "POST"][3:]
        assert len(posts) == 3 * 3
        # …each asked for the clip-scaled deadline, not the flat 120 s…
        assert {r.extensions["timeout"]["read"] for r in posts} == {45 + 1.5 * 43.259}
        # …and only the first attempt carried the domain prompt
        assert [b'name="prompt"' in r.read() for r in posts] == [True] * 3 + [False] * 6

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts, row.parked_reason) == ("failed", 3, None)
    assert "RequestFailedEverywhereError" in row.last_error
    assert session.get(Call, call.id).transcript_status == "failed"

    # The unpark pass never sees it, however healthy the backends are.
    publish_backend(session)
    assert unpark_backend_outage(session) == 0
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "failed"


def test_a_refused_connection_on_every_backend_is_still_an_outage(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Parked as recoverable, and requeued with its attempts given back once a backend is up."""
    import httpx
    import respx

    from app.stt.router import NoBackendAvailableError, RequestFailedEverywhereError

    call, job_row = seed_call(session, root=tmp_path)
    router = whisper_router()
    with respx.mock(assert_all_called=False) as mock:
        mock_whisper(mock, transcribe=httpx.ConnectError("connection refused"), health_ok=False)
        for _ in range(3):
            exc = run_once(worker, router)
            assert isinstance(exc, NoBackendAvailableError)
            assert not isinstance(exc, RequestFailedEverywhereError)
            make_due(session, job_row.id)

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.parked_reason) == ("failed", PARKED_BACKEND_UNAVAILABLE)

    publish_backend(session)
    assert unpark_backend_outage(session) == 1
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts, row.outage_requeues) == ("queued", 0, 1)
    assert session.get(Call, call.id).transcript_status == "queued"


def test_the_outage_requeue_cap_parks_a_job_for_good(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The backstop: whatever the router says, an "outage" is requeued at most the cap times."""
    from app.stt.router import NoBackendAvailableError
    from app.stt.worker import OUTAGE_REQUEUE_CAP, PARKED_OUTAGE_REQUEUE_CAP

    call, job_row = seed_call(session, root=tmp_path)
    publish_backend(session)  # somebody is healthy the whole time: the misreading case
    worker.router = FakeRouter(error=NoBackendAvailableError("every STT backend failed"))

    def burn_three_attempts() -> TranscriptionJob:
        for _ in range(3):
            worker.fail(worker.claim(), worker.router.error)
            make_due(session, job_row.id)
        session.expire_all()
        return session.get(TranscriptionJob, job_row.id)

    for n in range(1, OUTAGE_REQUEUE_CAP + 1):
        row = burn_three_attempts()
        assert (row.status, row.parked_reason) == ("failed", PARKED_BACKEND_UNAVAILABLE)
        assert unpark_backend_outage(session) == 1
        session.expire_all()
        assert session.get(TranscriptionJob, job_row.id).outage_requeues == n
        make_due(session, job_row.id)

    row = burn_three_attempts()
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.parked_reason == PARKED_OUTAGE_REQUEUE_CAP
    assert row.outage_requeues == OUTAGE_REQUEUE_CAP
    assert session.get(Call, call.id).transcript_status == "failed"
    assert unpark_backend_outage(session) == 0  # the loop is over
    session.expire_all()
    assert session.get(TranscriptionJob, job_row.id).status == "failed"

    # An operator's requeue is a fresh decision: the job gets a whole new allowance.
    assert requeue_failed(session, job_ids=[job_row.id]) == 1
    session.commit()
    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts, row.outage_requeues, row.parked_reason) == ("queued", 0, 0, None)


def test_the_unpark_pass_skips_a_row_already_at_the_cap(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Belt and braces: a ``backend_unavailable`` row at the cap stays where it is."""
    from app.stt.router import NoBackendAvailableError
    from app.stt.worker import OUTAGE_REQUEUE_CAP

    _, row = park(session, worker, tmp_path, NoBackendAvailableError("every STT backend failed"))
    row.outage_requeues = OUTAGE_REQUEUE_CAP
    session.commit()
    publish_backend(session)
    assert unpark_backend_outage(session) == 0


@pytest.mark.parametrize("cap", [False, True], ids=["request-failed", "outage-cap"])
def test_a_captioned_call_stays_done_when_its_job_parks_for_good(  # noqa: ANN001
    session, worker, tmp_path, cap
) -> None:
    """The two new ways to park: a captioned call keeps the status its caption earned."""
    from app.stt.router import NoBackendAvailableError, RequestFailedEverywhereError
    from app.stt.worker import OUTAGE_REQUEUE_CAP, PARKED_OUTAGE_REQUEUE_CAP

    call, job_row = seed_call(session, root=tmp_path)
    caption(session, call)
    if cap:
        session.get(TranscriptionJob, job_row.id).outage_requeues = OUTAGE_REQUEUE_CAP
        session.commit()
        exc: Exception = NoBackendAvailableError("every STT backend failed")
    else:
        exc = RequestFailedEverywhereError("every STT backend that tried failed on this recording")
    for _ in range(3):
        worker.fail(worker.claim(), exc)
        make_due(session, job_row.id)
        assert session.get(Call, call.id).transcript_status == "done"

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.parked_reason) == ("failed", PARKED_OUTAGE_REQUEUE_CAP if cap else None)
    stored = session.get(Call, call.id)
    assert (stored.transcript_status, stored.transcript_text) == ("done", CAPTION)


def test_the_request_deadline_scales_with_the_clip(session, worker, tmp_path) -> None:  # noqa: ANN001
    from app.stt.worker import REQUEST_TIMEOUT_MAX_S, request_timeout_s

    assert request_timeout_s(2.0) == 48.0
    assert request_timeout_s(43.259) == pytest.approx(109.9, abs=0.1)
    assert request_timeout_s(90.0) == REQUEST_TIMEOUT_MAX_S  # never longer than the old flat one
    assert request_timeout_s(None) == REQUEST_TIMEOUT_MAX_S

    seed_call(session, root=tmp_path, duration=10.0)
    assert worker.options_for(worker.claim()).timeout_s == 60.0


def test_the_retry_after_a_poison_failure_goes_without_the_prompt_and_can_succeed(
    session, worker, tmp_path  # noqa: ANN001
) -> None:
    """What call 5237413 does on Whisper: loops with the prompt, transcribes without it."""
    import httpx
    import respx

    call, job_row = seed_call(session, root=tmp_path, duration=43.259)
    router = whisper_router()
    bolo = {
        "text": "Attention all units, special attention to the eastbound from the 274.",
        "segments": [],
        "duration": 43.26,
    }

    def whisper(request: httpx.Request) -> httpx.Response:
        if b'name="prompt"' in request.read():
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=bolo)

    with respx.mock(assert_all_called=False) as mock:
        mock_whisper(mock, transcribe=whisper)
        # the other call, sent without a prompt, is what shows the servers are up
        serve_other_calls(router, tmp_path)
        run_once(worker, router)
        make_due(session, job_row.id)

        job = worker.claim()
        assert job.without_prompt is True
        options = worker.options_for(job).model_copy(update={"normalize_filter": None})
        assert options.prompt is None
        result = router.transcribe(Path(job.path), options)
        worker.complete(
            job, result, QualityAssessment(quality=Quality.GOOD, cleaned_text=result.text)
        )

    session.expire_all()
    row = session.get(TranscriptionJob, job_row.id)
    assert (row.status, row.attempts) == ("done", 1)
    transcript = session.scalars(select(Transcript).where(Transcript.call_id == call.id)).one()
    assert transcript.prompt_used is None  # the row says what was actually sent
    assert session.get(Call, call.id).transcript_text == bolo["text"]

    # A person's requeue clears the error, and with it the fallback.
    row.status, row.last_error, row.attempts = "failed", "RequestFailedEverywhereError: x", 3
    session.commit()
    assert requeue_failed(session, job_ids=[row.id]) == 1
    session.commit()
    assert worker.claim().without_prompt is False
