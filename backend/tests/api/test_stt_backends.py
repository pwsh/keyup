"""The STT backends page: the worker's published counters, the queue, and the switches.

The bug this suite exists for: the counters live in the worker process' memory, the page is
served by the API process, and the endpoint used to build its own router — so it reported
zeros forever while the worker was busy.  Everything here therefore asserts against rows
that crossed the process boundary (``stt_backend_stats``) or against SQL over
``transcription_jobs``, never against something one process remembers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ActivityEvent, SttBackendStat, TranscriptionJob
from app.stt import stats_store
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

WORKER = "worker-1:4242"
STARTED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def snapshot(**overrides) -> dict[str, dict]:  # noqa: ANN003
    """One router ``stats()`` entry for the configured backend."""
    entry = {
        "kind": "openai_compatible",
        "model": "Systran/faster-whisper-large-v3",
        "base_url": "http://whisper:8000",
        "priority": 10,
        "concurrency": 2,
        "enabled": True,
        "config_enabled": True,
        "healthy": True,
        "health_detail": "ok",
        "in_flight": 1,
        "attempts": 12,
        "successes": 11,
        "errors": 1,
        "consecutive_errors": 0,
        "last_error": "TranscriptionError: 502 from whisper",
        "last_error_at": STARTED,
        "last_success_at": STARTED + timedelta(minutes=5),
        "audio_s": 480.0,
        "processing_s": 60.0,
        "mean_rtf": 8.0,
        "mean_processing_s": 5.45,
        "recent_completed_1m": 3,
        "recent_failed_1m": 0,
    }
    entry.update(overrides)
    return {"whisper": entry}


def publish(session, **overrides) -> None:  # noqa: ANN001, ANN003
    stats_store.publish(
        session,
        snapshot(**overrides),
        worker_instance=WORKER,
        started_at=STARTED,
        now=overrides.pop("now", None) or datetime.now(UTC),
    )


# --------------------------------------------------------------------------------------
# the publisher
# --------------------------------------------------------------------------------------


def test_the_upsert_sets_only_non_key_columns(session) -> None:  # noqa: ANN001, ARG001
    """The house rule from the upsert-locks fix, applied to this table.

    A SET that names ``name`` — the primary key — asks Postgres for ``FOR UPDATE`` on the
    row from the column list alone, which conflicts with the ``FOR KEY SHARE`` every
    foreign-key check takes.  This upsert runs every ten seconds forever, so getting it
    wrong here would be a permanent, low-grade version of the 79-second stall.
    """
    values = stats_store._row_values(  # noqa: SLF001 - the shape is the thing under test
        "whisper",
        snapshot()["whisper"],
        worker_instance=WORKER,
        started_at=STARTED,
        now=datetime.now(UTC),
    )
    update_set = stats_store.upsert_statement(values)._post_values_clause.update_values_to_set  # noqa: SLF001
    assigned = set(dict(update_set))  # a list of pairs before SQLAlchemy 2.1, a dict since
    assert "name" not in assigned
    # The operator's columns are not the worker's to write, or a click would be undone on
    # the next publish.
    assert not assigned & {"admin_enabled", "admin_changed_at", "admin_changed_by"}
    assert {"attempts", "successes", "in_flight", "updated_at"} <= assigned


def test_publishing_twice_updates_the_same_row(session) -> None:  # noqa: ANN001
    publish(session)
    publish(session, successes=25, attempts=26)
    rows = list(session.scalars(select(SttBackendStat)))
    assert len(rows) == 1
    assert rows[0].successes == 25
    assert rows[0].worker_instance == WORKER
    assert rows[0].started_at == STARTED


def test_a_publish_leaves_the_operator_override_alone(session) -> None:  # noqa: ANN001
    stats_store.set_override(session, "whisper", False, changed_by="tests")
    session.commit()
    publish(session)
    row = session.scalars(select(SttBackendStat)).one()
    assert row.admin_enabled is False
    assert row.admin_changed_by == "tests"


def test_staleness_is_measured_from_the_last_publish() -> None:
    now = datetime.now(UTC)
    assert stats_store.is_stale(None, now=now) is True
    assert stats_store.is_stale(now - timedelta(seconds=5), now=now) is False
    assert stats_store.is_stale(now - timedelta(seconds=stats_store.STALE_AFTER_S + 1), now=now) is True


# --------------------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------------------


def test_backends_page_needs_an_admin(client, archive) -> None:  # noqa: ANN001, ARG001
    assert client.get("/api/admin/stt/backends").status_code == 401


def test_a_configured_backend_appears_before_any_worker_has_run(client, archive, admin_token) -> None:  # noqa: ANN001, ARG001
    body = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()
    entry = next(b for b in body["backends"] if b["name"] == "whisper")
    assert entry["stale"] is True
    assert entry["attempts"] == 0
    assert body["reporting"] is False
    assert body["last_report_at"] is None


def test_a_fresh_row_is_reported_as_the_worker_published_it(
    client, archive, admin_token, session
) -> None:  # noqa: ANN001, ARG001
    publish(session)
    body = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()
    entry = next(b for b in body["backends"] if b["name"] == "whisper")
    assert entry["stale"] is False
    assert entry["successes"] == 11
    assert entry["in_flight"] == 1
    assert entry["mean_rtf"] == 8.0
    assert entry["last_error"].startswith("TranscriptionError")
    assert body["reporting"] is True
    assert body["worker_instance"] == WORKER


def test_a_row_the_worker_stopped_updating_reads_stale(
    client, archive, admin_token, session
) -> None:  # noqa: ANN001, ARG001
    publish(session, now=datetime.now(UTC) - timedelta(seconds=stats_store.STALE_AFTER_S + 30))
    body = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()
    entry = next(b for b in body["backends"] if b["name"] == "whisper")
    assert entry["stale"] is True
    # The row is kept, not deleted: "since when" is the useful half of "not reporting".
    assert entry["successes"] == 11
    assert body["reporting"] is False
    assert body["last_report_at"] is not None


# --------------------------------------------------------------------------------------
# the queue block
# --------------------------------------------------------------------------------------


@pytest.fixture
def queue(session, archive):  # noqa: ANN001, ANN201
    """One job in each state the block counts, one per ingested call.

    Written out rather than reusing whatever ingest enqueued, because the point of the
    block is that it reads *the table* — so the table is what the test sets up.
    """
    from app.ingest.flow import BACKLOG_BAND

    now = datetime.now(UTC)
    session.execute(TranscriptionJob.__table__.delete())
    states = [
        {"status": "queued", "priority": 100},
        {"status": "queued", "priority": BACKLOG_BAND + 10},
        {"status": "running", "started_at": now - timedelta(seconds=30)},
        {
            "status": "done",
            "started_at": now - timedelta(minutes=2),
            "finished_at": now - timedelta(minutes=1),
        },
        {
            "status": "failed",
            "started_at": now - timedelta(minutes=4),
            "finished_at": now - timedelta(minutes=3),
        },
        {"status": "cancelled"},
    ]
    for call_id, state in zip(archive.call_ids, states, strict=False):
        session.add(TranscriptionJob(call_id=call_id, **state))
    session.commit()
    return states


def test_the_queue_block_counts_every_state(client, admin_token, queue) -> None:  # noqa: ANN001, ARG001
    block = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()["queue"]
    assert block["queued_live"] == 1
    assert block["queued_backlog"] == 1
    assert block["running"] == 1
    assert block["completed_1h"] == 1
    assert block["failed_1h"] == 1
    # 60 s of wall time for the one completion the last hour has.
    assert block["mean_processing_s"] == pytest.approx(60.0, abs=1.0)
    # One job done in the last ten minutes.
    assert block["throughput_per_min"] == pytest.approx(0.1)
    assert block["audio_seconds_queued"] > 0
    # Three jobs outstanding at a tenth of a job a minute.
    assert block["eta_seconds"] == pytest.approx(1800.0, rel=0.01)


def test_an_empty_queue_has_no_eta(client, archive, admin_token, session) -> None:  # noqa: ANN001, ARG001
    session.execute(TranscriptionJob.__table__.delete())
    session.commit()
    block = client.get("/api/admin/stt/backends", headers=bearer(admin_token)).json()["queue"]
    assert block["queued_live"] == 0
    assert block["eta_seconds"] is None


# --------------------------------------------------------------------------------------
# the operator's switch
# --------------------------------------------------------------------------------------


def test_disabling_a_backend_records_the_override_and_an_activity_row(
    client, archive, admin_token, session
) -> None:  # noqa: ANN001, ARG001
    response = client.put(
        "/api/admin/stt/backends/whisper/enabled",
        json={"enabled": False},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["admin_enabled"] is False
    assert body["config_enabled"] is True  # the distinction the UI shows
    assert body["admin_changed_by"]

    event = session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "stt.backend_disabled")
    ).one()
    assert event.source == "stt"
    assert event.meta["backend"] == "whisper"


def test_clearing_the_override_goes_back_to_the_configuration(
    client, archive, admin_token, session
) -> None:  # noqa: ANN001, ARG001
    headers = {**bearer(admin_token), **CSRF_HEADERS}
    client.put("/api/admin/stt/backends/whisper/enabled", json={"enabled": False}, headers=headers)
    body = client.put(
        "/api/admin/stt/backends/whisper/enabled", json={"enabled": None}, headers=headers
    ).json()
    assert body["admin_enabled"] is None
    assert body["enabled"] is True
    session.expire_all()
    assert stats_store.override_map(session) == {}
    assert session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "stt.backend_override_cleared")
    ).one()


def test_the_override_is_what_the_worker_reads_back(session) -> None:  # noqa: ANN001
    """The router's side of the switch: a plain map, keyed by name, overrides only."""
    stats_store.set_override(session, "whisper", False, changed_by="tests")
    session.commit()
    assert stats_store.override_map(session) == {"whisper": False}
    read = stats_store.override_provider(lambda: session)
    assert read() == {"whisper": False}


def test_an_unknown_backend_is_a_404(client, archive, admin_token) -> None:  # noqa: ANN001, ARG001
    response = client.put(
        "/api/admin/stt/backends/nope/enabled",
        json={"enabled": False},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert response.status_code == 404


def test_switching_a_backend_needs_an_admin(client, archive, write_token) -> None:  # noqa: ANN001, ARG001
    anonymous = client.put(
        "/api/admin/stt/backends/whisper/enabled", json={"enabled": False}, headers=CSRF_HEADERS
    )
    assert anonymous.status_code == 401
    denied = client.put(
        "/api/admin/stt/backends/whisper/enabled",
        json={"enabled": False},
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert denied.status_code == 403
