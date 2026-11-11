"""Health alerts: each condition raised once, cleared once after it has recovered.

The clock is the ``now`` every pass is handed, so a test walks minutes forward without
sleeping; the deliveries go to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

HOOK = "http://hooks.test/health"
#: every check off; each test switches on the one it is about
OFF = {
    "health_recorder_silent_minutes": 0,
    "health_ingest_lag_minutes": 0,
    "health_stt_backlog_minutes": 0,
    "health_backup_max_age_hours": 0,
}


def make_settings(tmp_path: Path, **kw) -> Settings:  # noqa: ANN003
    values = {**OFF, **kw}
    values.setdefault("backup_dir", str(tmp_path / "backups"))
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        public_base_url="http://keyup.test",
        **values,
    )


class Hooks:
    """A webhook receiver: every payload it was sent, in order."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(204)


def health_rule(session, *, pattern: str | None = None, name: str = "health"):  # noqa: ANN001, ANN201
    from app.models import AlertRule

    row = AlertRule(
        name=name,
        kind="health",
        pattern=pattern,
        channels=["webhook"],
        channel_config={"webhook": {"url": HOOK}},
    )
    session.add(row)
    session.commit()
    return row


def activity_rows(session):  # noqa: ANN001, ANN201
    from app.models import ActivityEvent

    return list(
        session.scalars(
            select(ActivityEvent).where(ActivityEvent.kind == "system.health").order_by(ActivityEvent.id)
        )
    )


def seed_call(session, recorded_at: datetime):  # noqa: ANN001, ANN201
    from app.models import Call, System, Talkgroup

    system = session.scalar(select(System).where(System.name == "Example System"))
    if system is None:
        system = System(name="Example System")
        session.add(system)
        session.flush()
    talkgroup = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1011))
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=system.id, tgid=1011, alias="EC LAW 1", category="law")
        session.add(talkgroup)
        session.flush()
    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[1011],
        recorded_at=recorded_at,
        started_at=recorded_at - timedelta(seconds=4),
        started_local=(recorded_at - timedelta(seconds=4)).replace(tzinfo=None),
        ended_at=recorded_at,
        duration_s=Decimal("4.000"),
        file_path=f"1011/{recorded_at.timestamp()}.mp3",
        file_name="call.mp3",
    )
    session.add(call)
    session.commit()
    return call


# --------------------------------------------------------------------------------------
# recorder silent
# --------------------------------------------------------------------------------------


def test_a_silent_hot_recorder_is_raised_once_and_cleared_after_recovery(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import RECOVERY, check
    from app.models import HealthCondition, Recorder

    now = datetime.now(UTC)
    hot = Recorder(
        instance_id="recorder-a", label="recorder-a", standby="hot", reported_at=now - timedelta(minutes=9)
    )
    # a warm spare (the laptop) and a cold bench box that are just as quiet: never alerted
    session.add_all(
        [
            hot,
            Recorder(instance_id="laptop", standby="warm", reported_at=now - timedelta(hours=3)),
            Recorder(instance_id="bench", standby="cold", reported_at=now - timedelta(days=3)),
        ]
    )
    session.commit()
    settings = make_settings(tmp_path, health_recorder_silent_minutes=5)
    hooks = Hooks()
    health_rule(session)

    first = check(session, settings, now=now, client=hooks.client)
    assert [(t.observation.key, t.state) for t in first] == [("recorder_silent:recorder-a", "raised")]
    # still silent a minute later, and two minutes after that: nothing new
    assert check(session, settings, now=now + timedelta(minutes=1), client=hooks.client) == []
    assert check(session, settings, now=now + timedelta(minutes=3), client=hooks.client) == []

    # it reports again — but a clear waits for RECOVERY of steady health
    hot.reported_at = now + timedelta(minutes=4)
    session.commit()
    assert check(session, settings, now=now + timedelta(minutes=4), client=hooks.client) == []
    row = session.get(HealthCondition, "recorder_silent:recorder-a")
    assert row.active and row.good_since is not None

    hot.reported_at = now + RECOVERY + timedelta(minutes=4)
    session.commit()
    cleared = check(session, settings, now=now + RECOVERY + timedelta(minutes=4), client=hooks.client)
    assert [(t.observation.key, t.state) for t in cleared] == [("recorder_silent:recorder-a", "cleared")]
    session.refresh(row)
    assert not row.active and row.cleared_at is not None

    assert [(p["state"], p["check"], p["key"]) for p in hooks.sent] == [
        ("raised", "recorder_silent", "recorder_silent:recorder-a"),
        ("cleared", "recorder_silent", "recorder_silent:recorder-a"),
    ]
    assert hooks.sent[0]["title"] == "Keyup: recorder silent (recorder-a)"
    assert hooks.sent[1]["title"].startswith("Keyup: recovered")
    assert [(e.level, e.meta["state"]) for e in activity_rows(session)] == [
        ("error", "raised"),
        ("info", "cleared"),
    ]


def test_a_relapse_inside_the_recovery_window_is_not_a_second_alert(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import Recorder

    now = datetime.now(UTC)
    hot = Recorder(instance_id="recorder-a", standby="hot", reported_at=now - timedelta(minutes=9))
    session.add(hot)
    session.commit()
    settings = make_settings(tmp_path, health_recorder_silent_minutes=5)

    assert len(check(session, settings, now=now)) == 1
    hot.reported_at = now + timedelta(minutes=1)  # back …
    session.commit()
    assert check(session, settings, now=now + timedelta(minutes=1)) == []
    # … and gone again before RECOVERY elapsed: still the one alert, nothing new
    assert check(session, settings, now=now + timedelta(minutes=8)) == []
    assert [e.meta["state"] for e in activity_rows(session)] == ["raised"]


def test_the_state_survives_a_worker_restart(session, tmp_path) -> None:  # noqa: ANN001
    """The memory is the table, not the process: a fresh pass does not raise again."""
    from app.db import SessionLocal
    from app.health import check
    from app.models import Recorder

    now = datetime.now(UTC)
    session.add(Recorder(instance_id="recorder-a", standby="hot", reported_at=now - timedelta(minutes=30)))
    session.commit()
    settings = make_settings(tmp_path, health_recorder_silent_minutes=5)
    assert len(check(session, settings, now=now)) == 1

    other = SessionLocal()
    try:
        assert check(other, settings, now=now + timedelta(minutes=1)) == []
    finally:
        other.close()


# --------------------------------------------------------------------------------------
# ingest lag, transcription backlog, backups
# --------------------------------------------------------------------------------------


def test_ingest_lag_ignores_a_clock_running_ahead(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check

    now = datetime.now(UTC)
    seed_call(session, now - timedelta(minutes=45))
    seed_call(session, now + timedelta(hours=2))  # a recorder whose clock is two hours fast
    settings = make_settings(tmp_path, health_ingest_lag_minutes=20)

    raised = check(session, settings, now=now)
    assert [(t.observation.key, t.state) for t in raised] == [("ingest_lag", "raised")]
    assert raised[0].observation.value == pytest.approx(45.0, abs=0.2)

    seed_call(session, now + timedelta(minutes=1))
    assert check(session, settings, now=now + timedelta(minutes=1)) == []  # recovering
    cleared = check(session, settings, now=now + timedelta(minutes=7))
    assert [t.state for t in cleared] == ["cleared"]


def test_an_empty_archive_is_not_an_ingest_outage(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import HealthCondition

    assert check(session, make_settings(tmp_path, health_ingest_lag_minutes=20)) == []
    assert session.scalars(select(HealthCondition)).all() == []


def test_stt_backlog_counts_live_and_parked_jobs_and_never_the_backlog_band(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import TranscriptionJob
    from app.stt.worker import PARKED_BACKEND_UNAVAILABLE

    now = datetime.now(UTC)
    settings = make_settings(tmp_path, health_stt_backlog_minutes=30)
    old = seed_call(session, now - timedelta(hours=3))
    # a migration's worth of backlog, hours old: the normal state, not an alert
    session.add(TranscriptionJob(call_id=old.id, priority=600, scheduled_at=now - timedelta(hours=3)))
    # a live job retried into the future is not due yet
    retry = seed_call(session, now - timedelta(hours=2))
    session.add(TranscriptionJob(call_id=retry.id, priority=100, scheduled_at=now + timedelta(minutes=5)))
    session.commit()
    assert check(session, settings, now=now) == []

    # an STT outage parks the live jobs instead of leaving them queued
    parked = seed_call(session, now - timedelta(minutes=50))
    session.add(
        TranscriptionJob(
            call_id=parked.id,
            priority=100,
            status="failed",
            parked_reason=PARKED_BACKEND_UNAVAILABLE,
            scheduled_at=now - timedelta(minutes=45),
        )
    )
    session.commit()
    raised = check(session, settings, now=now)
    assert [(t.observation.key, t.state) for t in raised] == [("stt_backlog", "raised")]
    assert raised[0].observation.value == pytest.approx(45.0, abs=0.2)


def test_backup_age_comes_from_the_newest_manifest(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import HealthCondition

    now = datetime.now(UTC)
    settings = make_settings(tmp_path, health_backup_max_age_hours=30)
    # not mounted here: the check is skipped, not failed
    assert check(session, settings, now=now) == []
    assert session.get(HealthCondition, "backup_stale") is None

    backups = tmp_path / "backups"
    backups.mkdir()
    raised = check(session, settings, now=now)
    assert [t.state for t in raised] == ["raised"]
    assert "no database backup" in raised[0].observation.message

    manifest = backups / "keyup-20260923T021500Z.json"
    manifest.write_text("{}")
    stamp = (now - timedelta(hours=2)).timestamp()
    os.utime(manifest, (stamp, stamp))
    # a drill report is newer, but it is not a backup
    drill = backups / "keyup-20260923T021500Z.drill.json"
    drill.write_text("{}")
    assert check(session, settings, now=now) == []
    row = session.get(HealthCondition, "backup_stale")
    assert row.active and row.value == pytest.approx(2.0, abs=0.1)
    assert [t.state for t in check(session, settings, now=now + timedelta(minutes=6))] == ["cleared"]

    old = (now - timedelta(hours=40)).timestamp()
    os.utime(manifest, (old, old))
    again = check(session, settings, now=now + timedelta(minutes=7))
    assert [t.state for t in again] == ["raised"]


# --------------------------------------------------------------------------------------
# delivery and bookkeeping
# --------------------------------------------------------------------------------------


def test_a_rule_hears_only_the_checks_it_lists(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import Notification

    now = datetime.now(UTC)
    seed_call(session, now - timedelta(minutes=45))
    everything = health_rule(session, name="everything")
    recorders_only = health_rule(session, name="recorders", pattern="recorder_silent")
    hooks = Hooks()
    check(session, make_settings(tmp_path, health_ingest_lag_minutes=20), now=now, client=hooks.client)

    assert [p["key"] for p in hooks.sent] == ["ingest_lag"]
    rows = list(session.scalars(select(Notification)))
    assert [(n.rule_id, n.ok, n.call_id, n.severity) for n in rows] == [(everything.id, True, None, "error")]
    assert recorders_only.last_fired_at is None


def test_switching_a_check_off_retires_its_alert_without_a_delivery(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import HealthCondition

    now = datetime.now(UTC)
    seed_call(session, now - timedelta(minutes=45))
    hooks = Hooks()
    health_rule(session)
    check(session, make_settings(tmp_path, health_ingest_lag_minutes=20), now=now, client=hooks.client)
    assert len(hooks.sent) == 1

    assert check(session, make_settings(tmp_path), now=now + timedelta(minutes=1), client=hooks.client) == []
    assert len(hooks.sent) == 1
    assert session.get(HealthCondition, "ingest_lag") is None
    assert [e.meta["state"] for e in activity_rows(session)] == ["raised", "retired"]


def test_a_failed_delivery_is_logged_and_the_state_still_moves(session, tmp_path) -> None:  # noqa: ANN001
    from app.health import check
    from app.models import HealthCondition, Notification

    now = datetime.now(UTC)
    seed_call(session, now - timedelta(minutes=45))
    health_rule(session)
    broken = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    check(session, make_settings(tmp_path, health_ingest_lag_minutes=20), now=now, client=broken)

    assert session.get(HealthCondition, "ingest_lag").active
    (row,) = session.scalars(select(Notification)).all()
    assert row.ok is False and "500" in row.error
    # …and it is not retried as a fresh raise next minute
    assert (
        check(session, make_settings(tmp_path, health_ingest_lag_minutes=20), now=now + timedelta(minutes=1))
        == []
    )


def test_an_unreadable_backup_directory_is_raised_not_skipped(session, tmp_path) -> None:  # noqa: ANN001
    """Mounted with the wrong owner or mode, the check must say so rather than go quiet."""
    from app.health import check

    backups = tmp_path / "backups"
    backups.mkdir()
    backups.chmod(0o000)
    try:
        if os.access(backups, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("root reads any directory")
        raised = check(session, make_settings(tmp_path, health_backup_max_age_hours=30))
    finally:
        backups.chmod(0o755)
    assert [t.state for t in raised] == ["raised"]
    assert "cannot read the backup directory" in raised[0].observation.message
