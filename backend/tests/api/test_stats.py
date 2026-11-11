"""Statistics endpoints (§4.5)."""

from __future__ import annotations

import re

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def test_overview_counts_volume_coverage_and_lag(client, archive) -> None:
    body = client.get("/api/stats/overview").json()
    assert body["calls_total"] == len(archive.call_ids)
    assert body["audio_hours_total"] > 0
    # One of six is transcribed; the skipped squelch tails are out of the denominator.
    assert 0 < body["transcript_coverage"] <= 1
    assert body["transcripts_total"] == 1
    assert body["stt_rtf"] is not None and body["stt_rtf"] > 0
    assert body["queue_depth"] >= 0
    assert body["ingest_lag_s"] > 0  # the fixtures are historical
    assert body["last_call_at"] is not None


def test_coverage_ignores_calls_the_policy_skipped(client, archive, session) -> None:
    from app.models import Call

    for call in session.query(Call).all():
        if call.transcript_status not in ("done",):
            call.transcript_status = "skipped"
    session.commit()
    assert client.get("/api/stats/overview").json()["transcript_coverage"] == 1.0


def test_activity_returns_cells_and_a_matrix(client, archive) -> None:
    body = client.get(
        "/api/stats/activity",
        params={"from": "2026-08-28", "to": "2026-08-30", "bucket": "day"},
    ).json()
    assert body["bucket"] == "day"
    assert sum(c["calls"] for c in body["cells"]) == len(archive.call_ids)
    assert all(c["seconds"] > 0 for c in body["cells"])
    assert len(body["matrix"]) == len(body["rows"])
    assert all(len(row) == len(body["buckets"]) for row in body["matrix"])
    assert body["max"] == max(max(row) for row in body["matrix"])


def test_activity_can_ignore_the_talkgroup_dimension(client, archive) -> None:
    body = client.get(
        "/api/stats/activity",
        params={"from": "2026-08-28", "to": "2026-08-30", "bucket": "day", "group_by": "none"},
    ).json()
    assert body["rows"] == ["all"]
    assert sum(body["matrix"][0]) == len(archive.call_ids)


def test_activity_can_row_the_matrix_by_local_day(client, archive) -> None:
    """The Statistics page's default grouping: rows are local days, columns hours.

    It used to 422 — the endpoint only knew `talkgroup` and `none` while the page had
    been asking for `day` since it was written.
    """
    body = client.get(
        "/api/stats/activity",
        params={"from": "2026-08-28", "to": "2026-08-30", "group_by": "day"},
    ).json()

    assert body["bucket"] == "hour"
    # Every row label is a local date, every column an hour of the day, bare.
    assert body["rows"] == sorted(body["rows"])
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", row) for row in body["rows"])
    assert all(re.fullmatch(r"\d{2}", bucket) for bucket in body["buckets"])
    assert len(body["matrix"]) == len(body["rows"])
    assert all(len(row) == len(body["buckets"]) for row in body["matrix"])
    # Nothing is dropped: a day grid keeps every call, unlike the talkgroup grid's top-N.
    assert sum(sum(row) for row in body["matrix"]) == len(archive.call_ids)
    assert sum(c["calls"] for c in body["cells"]) == len(archive.call_ids)
    # A day cell names its day, since it has no talkgroup to be named after.
    assert {c["alias"] for c in body["cells"]} == set(body["rows"])
    assert body["max"] == max(max(row) for row in body["matrix"])


def test_activity_by_day_and_by_talkgroup_count_the_same_calls(client, archive) -> None:
    params = {"from": "2026-08-28", "to": "2026-08-30"}
    by_day = client.get("/api/stats/activity", params={**params, "group_by": "day"}).json()
    by_tg = client.get("/api/stats/activity", params={**params, "group_by": "talkgroup"}).json()

    assert sum(c["calls"] for c in by_day["cells"]) == sum(c["calls"] for c in by_tg["cells"])
    assert by_day["rows"] != by_tg["rows"]


def test_activity_rejects_a_grouping_it_does_not_have(client) -> None:
    assert client.get("/api/stats/activity", params={"group_by": "radio"}).status_code == 422


def test_activity_honours_the_call_filters(client, archive) -> None:
    body = client.get(
        "/api/stats/activity",
        params={"from": "2026-08-28", "to": "2026-08-30", "bucket": "day", "talkgroup": 1701},
    ).json()
    assert sum(c["calls"] for c in body["cells"]) == 3


def test_talkgroup_and_radio_tables(client, archive) -> None:
    talkgroups = client.get("/api/stats/talkgroups").json()
    assert talkgroups
    assert sum(t["call_count"] for t in talkgroups) >= len(archive.call_ids) - 1
    assert all(len(t["spark"]) == 24 for t in talkgroups)
    assert talkgroups == sorted(talkgroups, key=lambda t: -t["call_count"])

    radios = client.get("/api/stats/radios").json()
    assert radios
    assert all(len(r["spark"]) == 24 for r in radios)
    assert any(r["top_talkgroup"] for r in radios)


def test_transcription_stats_report_the_backend_rtf(client, archive, read_token) -> None:
    body = client.get("/api/stats/transcription", headers=bearer(read_token)).json()
    assert {s["status"] for s in body["status"]}
    assert body["quality"] == [{"quality": "good", "count": 1}]
    assert body["backends"]
    backend = body["backends"][0]
    assert backend["backend_name"] == "whisper"
    assert backend["calls"] == 1
    assert backend["processing_seconds"] == 0.9
    assert backend["rtf"] > 0


def test_transcription_stats_keep_the_backends_from_a_guest(client, archive, api_user, user_password) -> None:
    """Which STT servers the operator runs is operator business: a guest gets the rest of
    the response but an empty `backends`.  The archive half is cached for five minutes, so
    both orders are checked against one warm cache — the operator's table must not reach a
    guest from it, and a guest's request must not blank the operator's."""
    guest = client.get("/api/stats/transcription")
    assert guest.status_code == 200
    body = guest.json()
    assert body["backends"] == []
    assert "whisper" not in guest.text
    assert body["quality"] == [{"quality": "good", "count": 1}]  # the guest-visible chart
    assert {s["status"] for s in body["status"]}

    login = client.post(
        "/api/auth/login",
        json={"username": api_user.username, "password": user_password},
        headers=CSRF_HEADERS,
    )
    assert login.status_code == 200
    signed_in = client.get("/api/stats/transcription").json()
    assert [b["backend_name"] for b in signed_in["backends"]] == ["whisper"]
    assert signed_in["quality"] == body["quality"]

    client.cookies.clear()
    again = client.get("/api/stats/transcription")
    assert again.json()["backends"] == []
    assert "whisper" not in again.text


def test_transcription_stats_cache_the_archive_scans_but_not_the_queue(client, archive, session) -> None:
    """The distributions are archive-wide scans (~10 s on production) and are cached; the
    queue figures are index-range counts and must move the moment a job does."""
    from sqlalchemy import select, update

    from app.models import Call, TranscriptionJob
    from app.services import counts, stats

    first = client.get("/api/stats/transcription").json()
    job_id = session.scalar(select(TranscriptionJob.id).order_by(TranscriptionJob.id).limit(1))
    assert job_id is not None
    session.execute(update(TranscriptionJob).where(TranscriptionJob.id == job_id).values(status="running"))
    session.execute(update(Call).values(transcript_quality="suspect"))
    session.commit()

    second = client.get("/api/stats/transcription").json()
    assert second["running"] == first["running"] + 1  # live
    assert second["quality"] == first["quality"]  # cached

    counts.invalidate(stats.TRANSCRIPTION_KEY)
    third = client.get("/api/stats/transcription").json()
    assert {q["quality"] for q in third["quality"]} == {"suspect"}


def test_gaps_endpoint_is_empty_without_a_detected_outage(client, archive) -> None:
    assert client.get("/api/stats/gaps").json() == []


def test_gaps_endpoint_reports_a_detected_outage(client, archive, session) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models import Call, RecorderGap

    call = session.query(Call).first()
    session.add(
        RecorderGap(
            system_id=call.system_id,
            started_at=datetime.now(UTC) - timedelta(hours=3),
            ended_at=datetime.now(UTC) - timedelta(hours=1),
            duration_s=7200,
            note="recorder restarted",
        )
    )
    session.commit()
    gaps = client.get("/api/stats/gaps").json()
    assert len(gaps) == 1
    assert gaps[0]["minutes"] == 120.0
    assert gaps[0]["system_name"] == "Example System"
    assert client.get("/api/stats/gaps", params={"hours": 1}).json() == []
    assert client.get("/api/stats/overview").json()["recorder_gaps_24h"] == 1
