"""``GET /api/status/activity`` — the Live activity panel's one request.

Every assertion here is about the **window boundary**: the point of the endpoint is that
the number on the tile is what happened in the last N seconds and the number behind the
trend arrow is what happened in the N seconds before that, so the tests place rows on both
sides of the line and on the far side of both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

WINDOW = 300


@pytest.fixture
def timeline(session, archive):  # noqa: ANN001, ANN201
    """The six ingested calls, dated: three inside the window, two before it, one long ago.

    Returns ``(inside, previous, ancient)`` as lists of call ids.
    """
    from app.models import Call

    now = datetime.now(UTC)
    calls = list(session.scalars(select(Call).where(Call.id.in_(archive.call_ids)).order_by(Call.id)))
    inside, previous, ancient = calls[:3], calls[3:5], calls[5:]
    for call in inside:
        call.ingested_at = now - timedelta(seconds=30)
    for call in previous:
        call.ingested_at = now - timedelta(seconds=WINDOW + 30)
    for call in ancient:
        call.ingested_at = now - timedelta(seconds=3 * WINDOW)
    session.commit()
    return [c.id for c in inside], [c.id for c in previous], [c.id for c in ancient]


def snapshot(client, window: int = WINDOW, token: str | None = None):  # noqa: ANN001, ANN201
    response = client.get(
        "/api/status/activity",
        params={"window": window},
        headers=bearer(token) if token else {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ingest_counts_split_at_the_window_boundary(client, timeline) -> None:  # noqa: ANN001
    inside, previous, _ancient = timeline
    body = snapshot(client)
    assert body["window_s"] == WINDOW
    assert body["counters"]["ingested"] == {"current": len(inside), "previous": len(previous)}


def test_a_longer_window_swallows_both_halves(client, timeline) -> None:  # noqa: ANN001
    inside, previous, _ancient = timeline
    body = snapshot(client, window=2 * WINDOW)
    assert body["counters"]["ingested"]["current"] == len(inside) + len(previous)


def test_the_audio_verdict_is_broken_out_by_content(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import Call

    inside, previous, _ancient = timeline
    for call_id, content in zip(inside, ("speech", "page", "static"), strict=False):
        session.get(Call, call_id).audio_meta = {"content": content}
    session.get(Call, previous[0]).audio_meta = {"content": "none"}
    session.commit()

    counters = snapshot(client)["counters"]
    assert counters["speech"]["current"] == 1
    assert counters["pages"]["current"] == 1
    assert counters["static"]["current"] == 1
    assert counters["no_speech"] == {"current": 0, "previous": 1}


def test_duplicates_are_dated_by_the_ruling_not_the_call(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import Call

    inside, previous, ancient = timeline
    # An old call reconciled a moment ago still counts as work done a moment ago.
    loser = session.get(Call, ancient[0])
    loser.duplicate_of = inside[0]
    loser.reconciled_at = datetime.now(UTC) - timedelta(seconds=10)
    older = session.get(Call, previous[0])
    older.duplicate_of = inside[0]
    older.reconciled_at = datetime.now(UTC) - timedelta(seconds=WINDOW + 10)
    session.commit()

    assert snapshot(client)["counters"]["duplicates_merged"] == {"current": 1, "previous": 1}


def test_discarded_audio_is_dated_by_when_it_went(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import Call

    inside, _previous, _ancient = timeline
    call = session.get(Call, inside[0])
    call.audio_state = "discarded"
    call.audio_archived_at = datetime.now(UTC) - timedelta(seconds=5)
    session.commit()

    assert snapshot(client)["counters"]["audio_discarded"]["current"] == 1


def test_finished_jobs_count_and_queued_ones_are_the_depth(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import AudioJob, TranscriptionJob

    inside, previous, _ancient = timeline
    now = datetime.now(UTC)
    # Ingest already queued one audio and one transcription job per call, so the work is
    # *finished* here rather than invented: a partial index forbids a second open job.
    queued_before = len(session.scalars(select(AudioJob).where(AudioJob.status == "queued")).all())

    def finish(model, call_id, at):  # noqa: ANN001, ANN202
        job = session.scalars(select(model).where(model.call_id == call_id)).one()
        job.status = "done"
        job.finished_at = at

    finish(AudioJob, inside[0], now - timedelta(seconds=20))
    finish(AudioJob, previous[0], now - timedelta(seconds=WINDOW + 20))
    finish(TranscriptionJob, inside[0], now - timedelta(seconds=15))
    session.commit()

    body = snapshot(client)
    assert body["counters"]["audio_processed"] == {"current": 1, "previous": 1}
    assert body["counters"]["transcribed"]["current"] == 1
    assert body["queues"]["audio_queued"] == queued_before - 2
    assert body["queues"]["transcription_queued"] >= 1


def test_exports_are_bucketed_by_service_and_skip_reason(client, session, timeline) -> None:  # noqa: ANN001
    from app.exporters.broadcastify import LATE_REASON
    from app.exporters.claims import CLAIMED_REASON
    from app.exporters.ledger import DUPLICATE_UPLOAD_REASON, NO_SPEECH_REASON, STATIC_REASON
    from app.models import CallExport

    inside, previous, ancient = timeline
    now = datetime.now(UTC)
    fresh, stale = now - timedelta(seconds=10), now - timedelta(seconds=WINDOW + 10)
    # One row per (call, service): that pair is unique in the ledger, so the six calls
    # carry the six Broadcastify outcomes and OpenMHz reuses them for its own three.
    rows = [
        ("broadcastify", "uploaded", None, fresh),
        ("broadcastify", "skipped", f"{DUPLICATE_UPLOAD_REASON} 7", fresh),
        ("broadcastify", "skipped", f"{NO_SPEECH_REASON} (0.04 s above -40 dB)", fresh),
        ("broadcastify", "skipped", f"{STATIC_REASON} (0.91 of frames flat)", fresh),
        ("broadcastify", "skipped", f"{LATE_REASON} (22.0 s after call end)", fresh),
        ("broadcastify", "skipped", CLAIMED_REASON, fresh),
        ("openmhz", "uploaded", None, stale),
        ("openmhz", "pending", None, fresh),
        ("openmhz", "failed", None, fresh),
    ]
    call_ids = [*inside, *previous, *ancient]
    seen: dict[str, int] = {}
    for service, status, reason, at in rows:
        index = seen.get(service, 0)
        seen[service] = index + 1
        session.add(
            CallExport(
                call_id=call_ids[index],
                service=service,
                status=status,
                skip_reason=reason,
                created_at=at,
                updated_at=at,
            )
        )
    session.commit()

    body = snapshot(client)
    bcfy = body["exports"]["broadcastify"]
    assert bcfy["uploaded"]["current"] == 1
    assert bcfy["skipped_duplicate"]["current"] == 1
    assert bcfy["skipped_no_speech"]["current"] == 1
    assert bcfy["skipped_static"]["current"] == 1
    assert bcfy["skipped_late"]["current"] == 1
    assert bcfy["claimed_by_recorder"]["current"] == 1
    openmhz = body["exports"]["openmhz"]
    assert openmhz["failed"]["current"] == 1
    assert openmhz["uploaded"] == {"current": 0, "previous": 1}
    assert openmhz["pending"]["current"] == 1
    assert body["queues"]["exports_pending"] == 1


def test_every_recorder_gets_a_heartbeat_row(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import Call, Recorder

    inside, _previous, _ancient = timeline
    session.add(Recorder(instance_id="recorder-a", label="Recorder A", last_seen_at=datetime.now(UTC)))
    session.add(Recorder(instance_id="spare", label="Cold spare"))
    for call_id in inside:
        session.get(Call, call_id).source_instance = "recorder-a"
    session.commit()

    rows = {row["instance"]: row for row in snapshot(client)["recorders"]}
    assert rows["recorder-a"]["calls_in_window"] == len(inside)
    assert rows["recorder-a"]["seconds_since_last_call"] is not None
    assert rows["recorder-a"]["seconds_since_last_call"] < WINDOW
    assert rows["spare"]["calls_in_window"] == 0
    assert rows["spare"]["seconds_since_last_call"] is None
    assert rows["spare"]["registered"] is True


def test_a_recorder_row_carries_the_live_monitor_it_reported(client, session, timeline) -> None:  # noqa: ANN001
    """The link the status page offers, and the two warnings that decide how it reads."""
    from app.models import Recorder

    session.add(
        Recorder(
            instance_id="loopback",
            monitor={
                "enabled": True,
                "url": "http://127.0.0.1:8095",
                "stream_url": "http://127.0.0.1:8095/api/monitor/stream",
                "token_required": False,
                "streams": 0,
                "max_streams": 4,
            },
        )
    )
    session.add(
        Recorder(
            instance_id="routable",
            monitor={"enabled": True, "url": "http://192.0.2.30:8095", "token_required": True},
        )
    )
    session.add(Recorder(instance_id="six", monitor={"enabled": True, "url": "http://[::1]:8095"}))
    session.add(Recorder(instance_id="named", monitor={"enabled": True, "url": "http://localhost:8095"}))
    session.add(Recorder(instance_id="off", monitor={"enabled": False}))
    session.add(Recorder(instance_id="silent"))
    session.commit()

    rows = {row["instance"]: row for row in snapshot(client)["recorders"]}
    assert rows["loopback"]["monitor"] == {
        "enabled": True,
        "url": "http://127.0.0.1:8095",
        "stream_url": "http://127.0.0.1:8095/api/monitor/stream",
        "token_required": False,
        # Computed here, not in the browser: the default bind only works on that box.
        "local_only": True,
    }
    assert rows["routable"]["monitor"]["local_only"] is False
    assert rows["routable"]["monitor"]["token_required"] is True
    assert rows["routable"]["monitor"]["stream_url"] is None
    assert rows["six"]["monitor"]["local_only"] is True
    assert rows["named"]["monitor"]["local_only"] is True
    assert rows["off"]["monitor"] == {
        "enabled": False,
        "url": None,
        "stream_url": None,
        "token_required": False,
        "local_only": False,
    }
    # A recorder that has never reported one gets null, not an empty block.
    assert rows["silent"]["monitor"] is None


def test_an_unregistered_instance_has_no_monitor_at_all(client, session, timeline) -> None:  # noqa: ANN001
    """There is no row to have reported one, so the key is present and null."""
    from app.models import Call

    inside, _previous, _ancient = timeline
    session.get(Call, inside[0]).source_instance = "stranger"
    session.commit()

    rows = {row["instance"]: row for row in snapshot(client)["recorders"]}
    assert rows["stranger"]["monitor"] is None


def test_an_unregistered_instance_still_shows_up(client, session, timeline) -> None:  # noqa: ANN001
    from app.models import Call

    inside, _previous, _ancient = timeline
    session.get(Call, inside[0]).source_instance = "stranger"
    session.commit()

    rows = {row["instance"]: row for row in snapshot(client)["recorders"]}
    assert rows["stranger"]["registered"] is False
    assert rows["stranger"]["calls_in_window"] == 1


def test_rejected_transcripts_come_from_the_activity_log(client, session, timeline) -> None:  # noqa: ANN001
    from app import activity
    from app.models import ActivityEvent

    inside, previous, _ancient = timeline
    for call_id in (inside[0], previous[0]):
        activity.record(
            session,
            source="stt",
            level="warn",
            kind="stt.hallucination",
            message="discarded 'Thank you for watching'",
            call_id=call_id,
        )
    session.commit()
    # The second one belongs to the previous window; `at` defaults to now, so move it.
    rows = list(session.scalars(select(ActivityEvent).order_by(ActivityEvent.id)))
    rows[-1].at = datetime.now(UTC) - timedelta(seconds=WINDOW + 20)
    session.commit()

    assert snapshot(client)["counters"]["transcripts_rejected"] == {"current": 1, "previous": 1}


def test_the_ticker_is_for_administrators_only(client, session, admin_token, timeline) -> None:  # noqa: ANN001, ARG001
    from app import activity

    activity.record(
        session,
        source="export",
        level="info",
        kind="export.uploaded",
        message="broadcastify accepted call 1",
    )
    activity.record(
        session,
        source="system",
        level="info",
        kind="retention.pass",
        message="a kind the ticker does not carry",
    )
    session.commit()

    guest = snapshot(client)
    assert guest["recent"] == []
    assert guest["recent_visible"] is False

    admin = snapshot(client, token=admin_token)
    assert admin["recent_visible"] is True
    kinds = [row["kind"] for row in admin["recent"]]
    assert "export.uploaded" in kinds
    assert "retention.pass" not in kinds
    assert len(admin["recent"]) <= 30


def test_the_window_is_bounded(client) -> None:  # noqa: ANN001
    assert client.get("/api/status/activity", params={"window": 99_999}).status_code == 422
    assert client.get("/api/status/activity", params={"window": 1}).status_code == 422


def test_the_route_is_documented(client) -> None:  # noqa: ANN001
    schema = client.get("/api/openapi.json").json()
    operation = schema["paths"]["/api/status/activity"]["get"]
    assert operation["summary"]
    assert operation["description"]
