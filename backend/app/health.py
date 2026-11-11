"""Health alerts: the things an operator needs to hear about before a listener does.

Every minute the worker calls :func:`check`, which measures four things and remembers, per
subject, whether each is wrong (``health_conditions``):

``recorder_silent:<instance_id>``
    a **hot** recorder (docs/RECORDERS.md) has not reported for
    ``health_recorder_silent_minutes``.  Warm spares and cold bench boxes are not watched:
    a laptop that is closed is not an outage.  The arbiter's own staleness is a different,
    shorter clock — it decides a failover, this decides whether to wake somebody;
``ingest_lag``
    no call at all has been recorded for ``health_ingest_lag_minutes`` — the same figure
    as the header's lag badge (``GET /api/status/lag``), with a recorder clock running
    ahead of real time ignored;
``stt_backlog``
    the oldest **live-band** transcription job has been due for
    ``health_stt_backlog_minutes`` — counting the jobs an STT outage parked, which leave
    the queue while every backend is failing (:mod:`app.stt.worker`).  Backlog-band work
    (a migration, a repair) is never counted: a big backlog is the normal state;
``backup_stale``
    the newest database-backup manifest ``deploy/backup/keyup-db-backup.sh`` wrote into
    ``BACKUP_DIR`` is older than ``health_backup_max_age_hours``.  Skipped when the
    directory is not mounted into the worker.

Disk space is not here: :mod:`app.disk_watch` has watched it (alert kind
``disk_low``), with thresholds of its own.

**Raised once, cleared once.**  A subject going bad is a *raise*: one ``system.health``
activity row and one delivery on every enabled alert rule of kind ``health``.  Staying bad
is nothing.  Coming back is a *clear* — the same again, saying it recovered — but only
after it has stayed good for :data:`RECOVERY`, so a recorder that drops out twice in a
minute is one alert and one recovery, not four.  A threshold set to 0 switches its check
off; a subject nobody watches any more (the check switched off, a recorder demoted to warm
or deleted) is retired quietly, with an activity line and no delivery.

A ``health`` rule may narrow what it hears: ``pattern`` (or ``filters.checks``) lists the
checks it wants, e.g. ``recorder_silent, ingest_lag``; empty means all four.  Rule
cooldowns do not apply — the raise/clear pairing already is the rate limit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import activity
from app.alerts import CallSummary, deliver, scrub_secrets
from app.config import Settings
from app.ingest.flow import BACKLOG_BAND
from app.models import AlertRule, Call, HealthCondition, Notification, Recorder, TranscriptionJob
from app.runtime_settings import effective_settings

log = logging.getLogger("app.health")

__all__ = [
    "ALERT_EVENT",
    "ALERT_KIND",
    "CHECKS",
    "RECOVERY",
    "Observation",
    "Transition",
    "active",
    "check",
    "observe",
]

ALERT_KIND = "health"
ALERT_EVENT = "system.health"
ACTIVITY_KIND = "system.health"
CHECKS = ("recorder_silent", "ingest_lag", "stt_backlog", "backup_stale")

#: How long a raised subject must stay good before it is cleared.  Five minutes: longer
#: than a recorder restart, short enough that "recovered" arrives while it is still news.
RECOVERY = timedelta(minutes=5)

#: A recorder clock this far ahead of ours is ignored by the ingest-lag check, so one box
#: stamping calls in the future cannot hide an outage of every other.
FUTURE_SKEW = timedelta(minutes=5)

#: The activity level a raise is logged at: recording stopping loses data, the other two
#: only delay it.
RAISE_LEVEL = {
    "recorder_silent": "error",
    "ingest_lag": "error",
    "stt_backlog": "warn",
    "backup_stale": "warn",
}

TITLES = {
    "recorder_silent": "recorder silent",
    "ingest_lag": "no new calls",
    "stt_backlog": "transcription stalled",
    "backup_stale": "database backup overdue",
}


@dataclass
class Observation:
    """One subject, measured now."""

    key: str
    check: str
    bad: bool
    #: minutes (hours for ``backup_stale``); ``None`` when there is nothing to measure
    value: float | None
    message: str


@dataclass
class Transition:
    """A subject that was raised or cleared by this pass."""

    observation: Observation
    state: str  # "raised" | "cleared"
    raised_at: datetime | None

    @property
    def title(self) -> str:
        what = TITLES.get(self.observation.check, self.observation.check)
        subject = self.observation.key.partition(":")[2]
        where = f" ({subject})" if subject else ""
        if self.state == "cleared":
            return f"Keyup: recovered — {what}{where}"
        return f"Keyup: {what}{where}"

    @property
    def message(self) -> str:
        if self.state == "cleared" and self.raised_at is not None:
            return f"{self.observation.message} (raised {self.raised_at:%Y-%m-%d %H:%M} UTC)"
        return self.observation.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": ALERT_KIND,
            "event": ALERT_EVENT,
            "state": self.state,
            "check": self.observation.check,
            "key": self.observation.key,
            "value": self.observation.value,
            "title": self.title,
            "message": self.message,
            "raised_at": self.raised_at.isoformat() if self.raised_at else None,
        }


# --------------------------------------------------------------------------------------
# the measurements
# --------------------------------------------------------------------------------------


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60, 1)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _recorders(session: Session, threshold: int, now: datetime) -> list[Observation]:
    out: list[Observation] = []
    for recorder in session.scalars(select(Recorder).where(Recorder.standby == "hot").order_by(Recorder.id)):
        last = recorder.reported_at or recorder.last_seen_at
        if last is None:
            continue  # never heard from: there is nothing to have lost
        age = _minutes(now - _aware(last))
        name = recorder.label or recorder.instance_id
        bad = age > threshold
        out.append(
            Observation(
                key=f"recorder_silent:{recorder.instance_id}",
                check="recorder_silent",
                bad=bad,
                value=age,
                message=(
                    f"recorder {name} has not reported for {age:.0f} min (threshold {threshold} min)"
                    if bad
                    else f"recorder {name} is reporting (last {age:.1f} min ago)"
                ),
            )
        )
    return out


def _ingest(session: Session, threshold: int, now: datetime) -> list[Observation]:
    last = session.scalar(select(func.max(Call.recorded_at)).where(Call.recorded_at <= now + FUTURE_SKEW))
    if last is None:
        return []  # an empty archive has not stopped, it has not started
    age = max(0.0, _minutes(now - _aware(last)))
    bad = age > threshold
    return [
        Observation(
            key="ingest_lag",
            check="ingest_lag",
            bad=bad,
            value=age,
            message=(
                f"no call recorded for {age:.0f} min (threshold {threshold} min); "
                f"the last was at {_aware(last):%H:%M} UTC"
                if bad
                else f"calls are arriving (newest {age:.1f} min ago)"
            ),
        )
    ]


def _stt(session: Session, threshold: int, now: datetime) -> list[Observation]:
    from app.stt.worker import PARKED_BACKEND_UNAVAILABLE  # noqa: PLC0415 - heavy module

    oldest = session.scalar(
        select(func.min(TranscriptionJob.scheduled_at)).where(
            TranscriptionJob.priority < BACKLOG_BAND,
            or_(
                (TranscriptionJob.status == "queued") & (TranscriptionJob.scheduled_at <= now),
                (TranscriptionJob.status == "failed")
                & (TranscriptionJob.parked_reason == PARKED_BACKEND_UNAVAILABLE),
            ),
        )
    )
    age = max(0.0, _minutes(now - _aware(oldest))) if oldest is not None else 0.0
    bad = age > threshold
    return [
        Observation(
            key="stt_backlog",
            check="stt_backlog",
            bad=bad,
            value=age,
            message=(
                f"the oldest live transcription job has waited {age:.0f} min (threshold {threshold} min)"
                if bad
                else "live transcription is keeping up"
                + (f" (oldest job {age:.1f} min)" if oldest is not None else "")
            ),
        )
    ]


def newest_backup(directory: Path) -> datetime | None:
    """When the newest backup manifest was written, or ``None`` when there is none."""
    newest: float | None = None
    with os.scandir(directory) as entries:
        for entry in entries:
            name = entry.name
            if not (name.startswith("keyup-") and name.endswith(".json")) or name.endswith(".drill.json"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return datetime.fromtimestamp(newest, UTC) if newest is not None else None


def _backup(directory: Path, threshold: int, now: datetime) -> list[Observation]:
    if not directory.is_dir():
        return []  # not mounted here: this deployment does not back up through the script
    try:
        newest = newest_backup(directory)
    except OSError as exc:
        # Mounted but unreadable (wrong owner or mode) is a broken check, and a broken
        # check must say so rather than go quiet.
        log.warning("health: cannot read %s: %s", directory, exc)
        return [
            Observation(
                key="backup_stale",
                check="backup_stale",
                bad=True,
                value=None,
                message=f"cannot read the backup directory {directory}: {exc.strerror or exc}",
            )
        ]
    if newest is None:
        return [
            Observation(
                key="backup_stale",
                check="backup_stale",
                bad=True,
                value=None,
                message=f"no database backup in {directory} yet",
            )
        ]
    age = round((now - newest).total_seconds() / 3600, 1)
    bad = age > threshold
    return [
        Observation(
            key="backup_stale",
            check="backup_stale",
            bad=bad,
            value=age,
            message=(
                f"the newest database backup is {age:.0f} h old (threshold {threshold} h)"
                if bad
                else f"the newest database backup is {age:.1f} h old"
            ),
        )
    ]


def observe(session: Session, settings: Settings, now: datetime) -> list[Observation]:
    """Every subject every switched-on check watches, measured now."""
    out: list[Observation] = []
    if settings.health_recorder_silent_minutes > 0:
        out += _recorders(session, settings.health_recorder_silent_minutes, now)
    if settings.health_ingest_lag_minutes > 0:
        out += _ingest(session, settings.health_ingest_lag_minutes, now)
    if settings.health_stt_backlog_minutes > 0:
        out += _stt(session, settings.health_stt_backlog_minutes, now)
    if settings.health_backup_max_age_hours > 0:
        out += _backup(Path(settings.backup_dir), settings.health_backup_max_age_hours, now)
    return out


# --------------------------------------------------------------------------------------
# the worker's pass
# --------------------------------------------------------------------------------------


def check(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> list[Transition]:
    """Measure, update ``health_conditions``, log and deliver what changed; commit."""
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    observations = observe(session, settings, now)
    rows = {row.key: row for row in session.scalars(select(HealthCondition).with_for_update())}
    seen: set[str] = set()
    transitions: list[Transition] = []

    for obs in observations:
        seen.add(obs.key)
        row = rows.get(obs.key)
        if row is None:
            row = HealthCondition(key=obs.key, check=obs.check, active=False)
            session.add(row)
        row.checked_at = now
        row.value = obs.value
        row.message = obs.message
        if obs.bad:
            row.good_since = None
            if not row.active:
                row.active = True
                row.raised_at = now
                row.cleared_at = None
                transitions.append(Transition(obs, "raised", now))
        elif row.active:
            if row.good_since is None:
                row.good_since = now
            if now - _aware(row.good_since) >= RECOVERY:
                raised_at = row.raised_at
                row.active = False
                row.cleared_at = now
                row.good_since = None
                transitions.append(Transition(obs, "cleared", raised_at))

    for key, row in rows.items():
        if key in seen:
            continue
        # Nobody watches this any more: the check was switched off, the recorder was
        # demoted or deleted, the backup directory unmounted.  Retire it without a
        # delivery — nothing recovered, somebody changed what is watched.
        if row.active:
            activity.record(
                session,
                source="system",
                level="info",
                kind=ACTIVITY_KIND,
                message=f"health: {key} is no longer watched; its alert is withdrawn",
                meta={"key": key, "check": row.check, "state": "retired"},
            )
        session.delete(row)

    for transition in transitions:
        obs = transition.observation
        raised = transition.state == "raised"
        activity.record(
            session,
            source="system",
            level=RAISE_LEVEL.get(obs.check, "warn") if raised else "info",
            kind=ACTIVITY_KIND,
            message=f"{transition.title.removeprefix('Keyup: ')}: {transition.message}",
            meta=transition.as_dict(),
        )
        (log.warning if raised else log.info)("health: %s %s — %s", obs.key, transition.state, obs.message)
    session.commit()

    if transitions:
        _deliver(session, transitions, settings, client=client, now=now)
        session.commit()
    return transitions


def _wanted(rule: AlertRule) -> set[str]:
    """The checks a ``health`` rule listens to; empty = all."""
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    raw: list[Any] = []
    if rule.pattern:
        raw += rule.pattern.replace(",", " ").split()
    listed = filters.get("checks")
    if isinstance(listed, list | tuple):
        raw += list(listed)
    elif isinstance(listed, str):
        raw += listed.replace(",", " ").split()
    return {str(item).strip().casefold() for item in raw if str(item).strip()}


class _HealthSummary(CallSummary):
    """A transition wearing the shape :func:`app.alerts.deliver` renders."""

    def __init__(self, transition: Transition, *, url: str, now: datetime) -> None:
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
        self.transition = transition

    @property
    def title(self) -> str:
        return self.transition.title

    @property
    def message(self) -> str:
        return self.transition.message

    def as_dict(self) -> dict[str, Any]:
        return {**self.transition.as_dict(), "url": self.url}


def _deliver(
    session: Session,
    transitions: list[Transition],
    settings: Settings,
    *,
    client: httpx.Client | None,
    now: datetime,
) -> list[Notification]:
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
        for transition in transitions:
            summary = _HealthSummary(transition, url=f"{base}/admin", now=now)
            raised = transition.state == "raised"
            for rule in rules:
                wanted = _wanted(rule)
                if wanted and transition.observation.check not in wanted:
                    continue
                for channel in rule.channels or ():
                    notification = Notification(
                        rule_id=rule.id,
                        call_id=None,
                        channel=channel,
                        severity=RAISE_LEVEL.get(transition.observation.check, "warn") if raised else "info",
                        subject=summary.title,
                        body=summary.message[:4000],
                        ok=True,
                    )
                    try:
                        deliver(rule, channel, summary, settings, http)
                    except Exception as exc:  # noqa: BLE001 - delivery failures are data
                        notification.ok = False
                        notification.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:2000]
                        log.warning("health: rule %s channel %s failed: %s", rule.id, channel, exc)
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


def active(session: Session) -> list[HealthCondition]:
    """The conditions raised right now, oldest first."""
    return list(
        session.scalars(
            select(HealthCondition)
            .where(HealthCondition.active.is_(True))
            .order_by(HealthCondition.raised_at)
        )
    )
