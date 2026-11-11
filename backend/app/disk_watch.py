"""The disk-free alert: sample free space, warn before the disk fills.

Production reached 98 % on 2026-09-22 with nothing saying so; the only guard was the upload
path's 507 at ``UPLOAD_MIN_DISK_FREE_BYTES``, which is the moment recording stops, not a
warning.  This module is the warning.

Every five minutes the worker calls :func:`check`, which reads ``shutil.disk_usage`` for
every filesystem the app knows — ``AUDIO_ROOT``, every retention archive path, and
``DISK_WATCH_PATHS`` (e.g. the database volume mounted read-only into the worker; it is
otherwise invisible from here) — one reading per filesystem, however many of those paths
share it.  Each reading is a ``disk_samples`` row, kept for 48 hours:

* the **level** is ``warn`` below *either* ``disk_warn_free_percent`` or
  ``disk_warn_free_gb``, ``critical`` below either critical threshold;
* a level **worse than the last sample's** is a crossing: one ``system.disk_low`` activity
  row (``warn``, or ``error`` for critical) and one delivery on every enabled alert rule of
  kind ``disk_low``.  The same level again is not;
* the alert **re-arms** once a reading is back above the threshold — with a small margin
  (:data:`HYSTERESIS_PERCENT` / :data:`HYSTERESIS_GB`), so a disk hovering on the line
  does not alert every time a recording lands and the discard pass removes one;
* the **growth rate** is the least-squares slope of free space over the last 24 hours of
  samples, and **days to full** is free space divided by it — ``None`` while free space is
  not shrinking or there is less than an hour of history.

``GET /api/status`` reads the same numbers (:func:`status`), live for free space and from
the samples for the rate.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import activity
from app.alerts import CallSummary, deliver, scrub_secrets
from app.audio import archive_roots
from app.config import Settings
from app.models import AlertRule, DiskSample, Notification
from app.runtime_settings import effective_settings

log = logging.getLogger("app.disk_watch")

__all__ = [
    "ALERT_EVENT",
    "ALERT_KIND",
    "DiskReading",
    "check",
    "days_to_full",
    "growth_per_day",
    "level_for",
    "status",
    "watch_paths",
]

#: (How often the worker samples is ``app.worker.DISK_WATCH_INTERVAL_MINUTES``: five.)
#: The window the growth rate is fitted over, and how long samples are kept.
RATE_WINDOW = timedelta(hours=24)
KEEP_SAMPLES = timedelta(hours=48)
#: Less history than this and the rate is noise — a single recording landing is "growth".
MIN_SPAN = timedelta(hours=1)
#: How far back above a threshold a disk must climb before that threshold re-arms.
HYSTERESIS_PERCENT = 1.0
HYSTERESIS_GB = 1.0

ALERT_KIND = "disk_low"
ALERT_EVENT = "system.disk_low"
ACTIVITY_KIND = "system.disk_low"

GIB = 1024**3
LEVELS = ("ok", "warn", "critical")
_RANK = {level: rank for rank, level in enumerate(LEVELS)}

Usage = Callable[[str], Any]

#: Looked up at call time, so a test can stand a fake disk in for the real one.
_disk_usage: Usage = shutil.disk_usage


@dataclass
class DiskReading:
    """One filesystem, now."""

    path: str
    free_bytes: int
    total_bytes: int
    level: str
    growth_bytes_per_day: float | None = None
    days_to_full: float | None = None
    #: the level before this reading — ``crossed`` is whether it got worse
    previous: str = "ok"

    @property
    def free_percent(self) -> float:
        return round(self.free_bytes / self.total_bytes * 100, 2) if self.total_bytes else 0.0

    @property
    def crossed(self) -> bool:
        return _RANK[self.level] > _RANK[self.previous]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "free_percent": self.free_percent,
            "level": self.level,
            "growth_bytes_per_day": self.growth_bytes_per_day,
            "days_to_full": self.days_to_full,
        }

    @property
    def sentence(self) -> str:
        free_gb = self.free_bytes / GIB
        text = f"{self.path} has {free_gb:.1f} GB free ({self.free_percent:.1f} %)"
        if self.growth_bytes_per_day and self.growth_bytes_per_day > 0:
            text += f", shrinking {self.growth_bytes_per_day / GIB:.1f} GB/day"
        if self.days_to_full is not None:
            text += f" — full in about {self.days_to_full:.1f} days"
        return text


# --------------------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------------------


def level_for(free_bytes: int, total_bytes: int, settings: Settings, previous: str = "ok") -> str:
    """``ok`` / ``warn`` / ``critical`` for one reading.

    Either threshold of a pair trips it — a small disk runs out of gigabytes before it runs
    out of percent and a big one the other way round.  A level the disk is already at is
    left only once free space is :data:`HYSTERESIS_PERCENT` / :data:`HYSTERESIS_GB` clear
    of its thresholds.
    """
    if total_bytes <= 0:
        return "ok"
    percent = free_bytes / total_bytes * 100
    gb = free_bytes / GIB

    def under(pct_threshold: float, gb_threshold: float, holding: bool) -> bool:
        pct_margin = HYSTERESIS_PERCENT if holding and pct_threshold > 0 else 0.0
        gb_margin = HYSTERESIS_GB if holding and gb_threshold > 0 else 0.0
        return percent < pct_threshold + pct_margin or gb < gb_threshold + gb_margin

    rank = _RANK.get(previous, 0)
    if under(float(settings.disk_critical_free_percent), float(settings.disk_critical_free_gb), rank >= 2):
        return "critical"
    if under(float(settings.disk_warn_free_percent), float(settings.disk_warn_free_gb), rank >= 1):
        return "warn"
    return "ok"


def growth_per_day(samples: list[tuple[datetime, int]]) -> float | None:
    """Bytes of free space lost per day — the least-squares slope, sign flipped.

    ``None`` with fewer than two samples or under :data:`MIN_SPAN` of history.  Negative
    when the disk is gaining space.  A fit rather than first-minus-last because the audio
    tree is a sawtooth — recordings land continuously and the discard pass removes a batch
    a minute — and the two end points can sit on opposite teeth.
    """
    if len(samples) < 2:
        return None
    ordered = sorted(samples)
    span = ordered[-1][0] - ordered[0][0]
    if span < MIN_SPAN:
        return None
    t0 = ordered[0][0]
    xs = [(at - t0).total_seconds() for at, _ in ordered]
    ys = [float(free) for _, free in ordered]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denominator
    return -slope * 86_400


def days_to_full(free_bytes: int, growth: float | None) -> float | None:
    """How long until nothing is free at the current rate; ``None`` when it is not shrinking."""
    if growth is None or growth <= 0:
        return None
    return round(free_bytes / growth, 2)


# --------------------------------------------------------------------------------------
# what to watch
# --------------------------------------------------------------------------------------


def watch_paths(settings: Settings) -> list[Path]:
    """Every directory whose filesystem is worth watching, most important first."""
    paths = [Path(settings.audio_root), *archive_roots(settings)]
    for part in str(getattr(settings, "disk_watch_paths", "") or "").split(","):
        if part.strip():
            paths.append(Path(part.strip()))
    return paths


def _filesystems(settings: Settings, usage: Usage) -> list[tuple[str, int, int]]:
    """``(label path, free, total)`` per filesystem — the first watched path names it."""
    seen: set[int] = set()
    out: list[tuple[str, int, int]] = []
    for path in watch_paths(settings):
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            measured = usage(str(path))
        except OSError:
            continue  # not mounted here, or not visible from this container
        seen.add(device)
        out.append((str(path), int(measured.free), int(measured.total)))
    return out


def _history(session: Session, path: str, since: datetime) -> list[tuple[datetime, int]]:
    return [
        (at, free)
        for at, free in session.execute(
            select(DiskSample.at, DiskSample.free_bytes)
            .where(DiskSample.path == path, DiskSample.at >= since)
            .order_by(DiskSample.at)
        ).all()
    ]


def _last_level(session: Session, path: str) -> str:
    level = session.scalar(
        select(DiskSample.level).where(DiskSample.path == path).order_by(DiskSample.at.desc()).limit(1)
    )
    return level if level in _RANK else "ok"


# --------------------------------------------------------------------------------------
# the worker's check
# --------------------------------------------------------------------------------------


def check(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    usage: Usage | None = None,
    client: httpx.Client | None = None,
) -> list[DiskReading]:
    """Sample every watched filesystem, raise what crossed a threshold, commit."""
    usage = usage or _disk_usage
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    readings: list[DiskReading] = []
    for path, free, total in _filesystems(settings, usage):
        previous = _last_level(session, path)
        reading = DiskReading(
            path=path,
            free_bytes=free,
            total_bytes=total,
            level=level_for(free, total, settings, previous),
            previous=previous,
        )
        history = [*_history(session, path, now - RATE_WINDOW), (now, free)]
        reading.growth_bytes_per_day = growth_per_day(history)
        reading.days_to_full = days_to_full(free, reading.growth_bytes_per_day)
        session.add(DiskSample(path=path, at=now, free_bytes=free, total_bytes=total, level=reading.level))
        readings.append(reading)

    session.execute(delete(DiskSample).where(DiskSample.at < now - KEEP_SAMPLES))

    crossed = [reading for reading in readings if reading.crossed]
    for reading in crossed:
        critical = reading.level == "critical"
        activity.record(
            session,
            source="system",
            level="error" if critical else "warn",
            kind=ACTIVITY_KIND,
            message=f"disk space {'critical' if critical else 'low'}: {reading.sentence}",
            meta={
                **reading.as_dict(),
                "previous": reading.previous,
                "thresholds": {
                    "warn_percent": settings.disk_warn_free_percent,
                    "warn_gb": settings.disk_warn_free_gb,
                    "critical_percent": settings.disk_critical_free_percent,
                    "critical_gb": settings.disk_critical_free_gb,
                },
            },
        )
        log.warning("disk: %s is now %s — %s", reading.path, reading.level, reading.sentence)
    session.commit()

    if crossed:
        _raise_alerts(session, crossed, settings, client=client, now=now)
        session.commit()
    return readings


class _DiskSummary(CallSummary):
    """A reading wearing the shape :func:`app.alerts.deliver` renders (see message_kinds)."""

    def __init__(self, reading: DiskReading, *, url: str, now: datetime) -> None:
        super().__init__(
            call_id=0,
            started_at=now,
            duration_s=0.0,
            system=None,
            talkgroup_id=None,
            talkgroup_tgid=None,
            talkgroup_alias=None,
            talkgroup_ids=[],
            radio_id=None,
            radio_label=None,
            transcript=None,
            quality=None,
            url=url,
            audio_url=url,
        )
        self.reading = reading

    @property
    def title(self) -> str:
        word = "critical" if self.reading.level == "critical" else "low"
        return f"Keyup: disk space {word} on {self.reading.path}"

    @property
    def message(self) -> str:
        return self.reading.sentence

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": ALERT_KIND,
            "event": ALERT_EVENT,
            "title": self.title,
            "message": self.message,
            "url": self.url,
            **self.reading.as_dict(),
        }


def _raise_alerts(
    session: Session,
    readings: list[DiskReading],
    settings: Settings,
    *,
    client: httpx.Client | None,
    now: datetime,
) -> list[Notification]:
    """Deliver each crossing on every enabled ``disk_low`` rule's channels.

    No cooldown: a crossing is already once per threshold, and a warn followed an hour
    later by a critical is two things an operator needs to hear.
    """
    rules = list(
        session.scalars(
            select(AlertRule)
            .where(AlertRule.enabled.is_(True), AlertRule.kind == ALERT_KIND)
            .order_by(AlertRule.id)
        )
    )
    if not rules:
        return []
    base = settings.public_base_url.rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=settings.alert_timeout_s)
    written: list[Notification] = []
    try:
        for reading in readings:
            summary = _DiskSummary(reading, url=f"{base}/admin", now=now)
            for rule in rules:
                for channel in rule.channels or ():
                    notification = Notification(
                        rule_id=rule.id,
                        call_id=None,
                        channel=channel,
                        severity="error" if reading.level == "critical" else "warn",
                        subject=summary.title,
                        body=summary.message[:4000],
                        ok=True,
                    )
                    try:
                        deliver(rule, channel, summary, settings, http)
                    except Exception as exc:  # noqa: BLE001 - delivery failures are data
                        notification.ok = False
                        notification.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:2000]
                        log.warning("disk: rule %s channel %s failed: %s", rule.id, channel, exc)
                    session.add(notification)
                    written.append(notification)
                rule.last_fired_at = now
    finally:
        if owned:
            http.close()
    return written


# --------------------------------------------------------------------------------------
# GET /api/status
# --------------------------------------------------------------------------------------


def status(
    session: Session | None,
    settings: Settings,
    *,
    now: datetime | None = None,
    usage: Usage | None = None,
) -> list[DiskReading]:
    """Every watched filesystem as it stands now, the audio root's first.

    Free space is measured live; the rate comes from the worker's samples, so it is
    ``None`` until the worker has an hour of them (and when ``session`` is ``None``, which
    is the status route with its database down).
    """
    now = now or datetime.now(UTC)
    usage = usage or _disk_usage
    out: list[DiskReading] = []
    for path, free, total in _filesystems(settings, usage):
        history: list[tuple[datetime, int]] = []
        if session is not None:
            try:
                history = _history(session, path, now - RATE_WINDOW)
            except Exception:  # noqa: BLE001 - the status route must answer regardless
                session.rollback()
                history = []
        history.append((now, free))
        reading = DiskReading(
            path=path, free_bytes=free, total_bytes=total, level=level_for(free, total, settings)
        )
        reading.growth_bytes_per_day = growth_per_day(history)
        reading.days_to_full = days_to_full(free, reading.growth_bytes_per_day)
        out.append(reading)
    return out
