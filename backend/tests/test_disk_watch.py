"""The disk-free alert: thresholds, one event per crossing, re-arm, days to full.

A fake ``disk_usage`` stands in for the disk, so each test says exactly how full it is.
"""

from __future__ import annotations

from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from tests.conftest import TEST_DATABASE_URL, requires_db

GIB = 1024**3
Usage = namedtuple("Usage", "total used free")


def make_settings(root: Path, **kw) -> Settings:  # noqa: ANN003
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, audio_root=root, tz="America/Winnipeg", **kw)


class FakeDisk:
    """A 1000 GiB disk whose free space the test sets."""

    def __init__(self, free_gib: float, total_gib: float = 1000) -> None:
        self.free = int(free_gib * GIB)
        self.total = int(total_gib * GIB)

    def __call__(self, path: str) -> Usage:  # noqa: ARG002
        return Usage(self.total, self.total - self.free, self.free)


# --------------------------------------------------------------------------------------
# the arithmetic (no database)
# --------------------------------------------------------------------------------------


def test_either_threshold_trips_the_level(tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import level_for

    settings = make_settings(tmp_path)  # 15 % / 30 GB warn, 5 % / 10 GB critical
    total = 1000 * GIB
    assert level_for(200 * GIB, total, settings) == "ok"
    assert level_for(149 * GIB, total, settings) == "warn"  # under 15 %
    assert level_for(49 * GIB, total, settings) == "critical"  # under 5 %
    small = 100 * GIB
    assert level_for(25 * GIB, small, settings) == "warn"  # 25 % free, but under 30 GB
    assert level_for(9 * GIB, small, settings) == "critical"  # under 10 GB


def test_a_level_is_left_only_clear_of_its_threshold(tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import level_for

    settings = make_settings(tmp_path)
    total = 1000 * GIB
    # 15.5 % is above the 15 % line but inside the 1-point margin: still warn once warned
    assert level_for(155 * GIB, total, settings, previous="warn") == "warn"
    assert level_for(155 * GIB, total, settings, previous="ok") == "ok"
    assert level_for(165 * GIB, total, settings, previous="warn") == "ok"


def test_days_to_full_from_a_linear_decline() -> None:
    from app.disk_watch import days_to_full, growth_per_day

    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    # 10 GiB a day, sampled every 5 minutes over 24 h, ending at 50 GiB free
    samples = [
        (now - timedelta(minutes=5 * i), int(50 * GIB + 10 * GIB * (5 * i) / 1440)) for i in range(289)
    ]
    growth = growth_per_day(samples)
    assert growth == pytest.approx(10 * GIB, rel=1e-6)
    assert days_to_full(50 * GIB, growth) == pytest.approx(5.0, abs=0.01)


def test_no_estimate_without_history_or_while_space_is_growing() -> None:
    from app.disk_watch import days_to_full, growth_per_day

    now = datetime(2026, 9, 23, 12, tzinfo=UTC)
    assert growth_per_day([(now, 1)]) is None
    # under an hour of samples is noise
    assert growth_per_day([(now - timedelta(minutes=30), 2 * GIB), (now, GIB)]) is None
    freeing = growth_per_day([(now - timedelta(hours=6), GIB), (now, 2 * GIB)])
    assert freeing is not None and freeing < 0
    assert days_to_full(GIB, freeing) is None
    assert days_to_full(GIB, None) is None


# --------------------------------------------------------------------------------------
# the worker's check
# --------------------------------------------------------------------------------------


def _events(session):  # noqa: ANN001, ANN202
    from app.models import ActivityEvent

    return list(
        session.scalars(
            select(ActivityEvent).where(ActivityEvent.kind == "system.disk_low").order_by(ActivityEvent.id)
        )
    )


@requires_db
def test_a_crossing_raises_one_event_and_re_arms(session, tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import check

    settings = make_settings(tmp_path)
    disk = FakeDisk(200)
    now = datetime.now(UTC)

    def tick(minutes: int) -> None:
        check(session, settings, usage=disk, now=now + timedelta(minutes=minutes))

    tick(0)
    assert _events(session) == []

    disk.free = 140 * GIB  # under 15 %
    tick(5)
    tick(10)  # still under: no second event
    events = _events(session)
    assert [(e.level, e.meta["level"]) for e in events] == [("warn", "warn")]
    assert events[0].meta["path"] == str(tmp_path)

    disk.free = 40 * GIB  # under 5 %: the critical threshold fires on its own
    tick(15)
    tick(20)
    assert [e.level for e in _events(session)] == ["warn", "error"]

    disk.free = 300 * GIB  # well clear: re-armed
    tick(25)
    assert len(_events(session)) == 2
    disk.free = 140 * GIB
    tick(30)
    assert [e.level for e in _events(session)] == ["warn", "error", "warn"]


@requires_db
def test_samples_carry_the_rate_and_old_ones_are_pruned(session, tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import check
    from app.models import DiskSample

    settings = make_settings(tmp_path)
    disk = FakeDisk(500)
    now = datetime.now(UTC)
    session.add(DiskSample(path=str(tmp_path), at=now - timedelta(days=3), free_bytes=1, total_bytes=2))
    session.commit()
    # 24 hours ago there was 510 GiB, twelve hours ago 505: 10 GiB a day
    for hours, free in ((24, 510), (12, 505)):
        disk.free = free * GIB
        check(session, settings, usage=disk, now=now - timedelta(hours=hours))
    disk.free = 500 * GIB
    readings = check(session, settings, usage=disk, now=now)

    assert readings[0].growth_bytes_per_day == pytest.approx(10 * GIB, rel=1e-6)
    assert readings[0].days_to_full == pytest.approx(50.0, abs=0.01)
    ages = [s.at for s in session.scalars(select(DiskSample))]
    assert len(ages) == 3 and min(ages) >= now - timedelta(days=2)


@requires_db
def test_a_crossing_is_delivered_on_disk_low_rules(session, tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import check
    from app.models import AlertRule, Notification

    rule = AlertRule(
        name="disk",
        kind="disk_low",
        channels=["webhook"],
        channel_config={"webhook": {"url": "http://hooks.test/disk"}},
    )
    session.add(rule)
    session.commit()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = make_settings(tmp_path)
    check(session, settings, usage=FakeDisk(20), client=client)  # ok -> critical
    check(session, settings, usage=FakeDisk(20), client=client)  # same level: nothing

    assert len(sent) == 1
    assert sent[0]["kind"] == "disk_low" and sent[0]["level"] == "critical"
    assert sent[0]["days_to_full"] is None
    rows = list(session.scalars(select(Notification)))
    assert [(n.ok, n.severity, n.rule_id) for n in rows] == [(True, "error", rule.id)]


@requires_db
def test_a_filesystem_is_sampled_once_however_many_paths_share_it(session, tmp_path) -> None:  # noqa: ANN001
    from app.disk_watch import check

    extra = tmp_path / "db"
    extra.mkdir()
    settings = make_settings(tmp_path, disk_watch_paths=f"{extra}, /nonexistent/path")
    readings = check(session, settings, usage=FakeDisk(500))
    assert [r.path for r in readings] == [str(tmp_path)]


# --------------------------------------------------------------------------------------
# GET /api/status
# --------------------------------------------------------------------------------------


@requires_db
def test_status_reports_the_disk(session, monkeypatch) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app import disk_watch
    from app.config import get_settings
    from app.main import app
    from app.models import DiskSample

    root = str(get_settings().audio_root)
    monkeypatch.setattr(disk_watch, "_disk_usage", FakeDisk(100))
    now = datetime.now(UTC)
    # 105 GiB twelve hours ago, 100 now: 10 GiB/day, ten days to full
    session.add(DiskSample(path=root, at=now - timedelta(hours=12), free_bytes=105 * GIB, total_bytes=1000 * GIB))
    session.commit()

    from app.auth import create_api_token

    # Signed in: a guest sees the path as a label (tests/api/test_admin_read_lock.py).
    _, token = create_api_token(session, "tests-read", ["read"], created_by="tests")
    with TestClient(app) as client:
        body = client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).json()

    disk = body["disk"]
    assert disk["path"] == root
    assert disk["free_bytes"] == 100 * GIB
    assert disk["total_bytes"] == 1000 * GIB
    assert disk["free_percent"] == 10.0
    assert disk["level"] == "warn"
    assert disk["days_to_full"] == pytest.approx(10.0, abs=0.05)
    assert body["disk_volumes"][0]["path"] == root
