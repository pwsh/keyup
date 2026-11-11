"""The site channel audit: the difference between the air, RadioReference and the config.

Everything here is computed from ``site_frequencies`` and ``adjacent_sites`` at read time —
the report is never stored, so it cannot go stale, and the scheduled job
(:func:`run_channel_audit`) exists to *log* the summary and to raise the alerts, not to
cache the answer.

The five findings (docs/CHANNEL_AUDIT.md):

**undocumented**
    heard on the air, absent from RadioReference.  A new voice channel usually means the
    system grew; a new control channel means the audit is about to matter.
**missing**
    documented by RadioReference and either never heard or not heard within
    ``CHANNEL_AUDIT_STALE_DAYS``.
**control_drift**
    a frequency both sources know, disagreeing about whether it is a control channel —
    RR's primary that is now carrying voice, an alternate that has become the live control
    channel, and so on.
**recorder_config**
    control channels the site announces that the recorder is not configured to hunt, and
    configured frequencies the site no longer announces, plus the list to paste back into
    the recorder.
**adjacent_sites**
    the neighbors the control channel names, each resolved (or not) to a site Keyup knows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import CallSummary, deliver, scrub_secrets
from app.config import Settings
from app.models import AdjacentSite, AlertRule, Notification, RrSite, Site, SiteFrequency, System
from app.rr.observations import last_observed_at
from app.runtime_settings import effective_settings

log = logging.getLogger("app.rr.audit")

__all__ = [
    "ALERT_KIND",
    "AuditFinding",
    "audit_site",
    "audit_system",
    "raise_new_findings",
    "run_channel_audit",
]

#: the ``alert_rules.kind`` this module fires
ALERT_KIND = "channel_audit"

CONTROL_ROLES = ("primary_control", "alternate_control")


# --------------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------------


def _rows(session: Session, site_id: int) -> list[SiteFrequency]:
    return list(
        session.scalars(
            select(SiteFrequency)
            .where(SiteFrequency.site_id == site_id)
            .order_by(SiteFrequency.frequency_hz)
        )
    )


def _by_source(rows: list[SiteFrequency], source: str) -> dict[int, SiteFrequency]:
    return {row.frequency_hz: row for row in rows if row.source == source}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _known_sites(session: Session) -> dict[tuple[int | None, int, int], Site]:
    """``(system p25 id, rfss, site number) → site``, for resolving adjacent announcements."""
    index: dict[tuple[int | None, int, int], Site] = {}
    # How many sites share an ``(rfss, site_number)`` pair: on this network Ashby
    # (P25 system 92) and Millbrook (6) are both RFSS 10 site 10, so the system-less key
    # below is only safe when exactly one site owns the pair.
    owners: dict[tuple[int, int], int] = {}
    stmt = select(Site, System).join(System, Site.system_id == System.id)
    rows = session.execute(stmt).all()
    for site, _system in rows:
        if site.rfss is not None and site.site_number is not None:
            owners[(site.rfss, site.site_number)] = owners.get((site.rfss, site.site_number), 0) + 1
    for site, system in rows:
        if site.rfss is None or site.site_number is None:
            continue
        index.setdefault((system.system_id, site.rfss, site.site_number), site)
        # An announcement names the neighbour's P25 system id, which on a county-per-cell
        # network equals the site's NAC (say 0x293, 0x294, 0x295) while every
        # site hangs off one Keyup "Example System" system with an id of its own.  Index by
        # the NAC too, so an announced ``system_id`` finds the right tower.
        if site.nac is not None:
            index.setdefault((site.nac, site.rfss, site.site_number), site)
        if owners[(site.rfss, site.site_number)] == 1:
            index.setdefault((None, site.rfss, site.site_number), site)
    return index


def resolve_known_site(
    index: dict[tuple[int | None, int, int], Site],
    system_id_p25: int | None,
    rfss: int | None,
    site_number: int | None,
) -> Site | None:
    """The Keyup site an announcement names, or ``None``.

    The system-keyed entry first; the system-less fallback only when the announcement
    named no system, or the site it finds does not contradict it — a site whose NAC is
    known and differs from the announced system id is a different tower that happens to
    share the RFSS/site pair (Millbrook 6/10/10 against Ashby 92/10/10).
    """
    if rfss is None or site_number is None:
        return None
    site = index.get((system_id_p25, rfss, site_number))
    if site is not None:
        return site
    site = index.get((None, rfss, site_number))
    if site is None:
        return None
    if system_id_p25 is not None and site.nac is not None and site.nac != system_id_p25:
        return None
    return site


def _rr_sites(session: Session, system_id: int) -> list[RrSite]:
    """The RadioReference site cache for one system — the list neighbors resolve against."""
    return list(session.scalars(select(RrSite).where(RrSite.system_id == system_id)))


def _match_rr_site(row: AdjacentSite, rr_rows: list[RrSite]) -> tuple[RrSite | None, str | None]:
    """Resolve one announced neighbor to a cached RR site, and say which key matched.

    In order: the ``(rfss, site_number)`` pair, then the control frequency, then the P25
    system id (against RR's ``systemId`` or NAC).  On this network more than one tower can
    share an ``(rfss, site_number)`` — sites both numbered 10 on RFSS 10 — so a colliding
    pair is broken by the control frequency rather than guessed at.
    """
    by_pair = [r for r in rr_rows if r.rfss == row.rfss and r.site_number == row.site_number]
    if len(by_pair) == 1:
        return by_pair[0], "rfss+site_number"
    if len(by_pair) > 1 and row.system_id_p25 is not None:
        # West Co and East Co are both RFSS 20 site 20; the announced system id
        # (57 against 7) is the cleaner tiebreak, and it holds when RR's control
        # frequency is stale (RR lists West Co on 851.0875, the air says 852.0625).
        by_system = [r for r in by_pair if row.system_id_p25 in (r.rr_system_id, r.nac)]
        if len(by_system) == 1:
            return by_system[0], "rfss+site_number+system_id"
    if row.control_frequency_hz is not None:
        if len(by_pair) > 1:
            exact = [r for r in by_pair if r.control_frequency_hz == row.control_frequency_hz]
            if len(exact) == 1:
                return exact[0], "control_frequency"
        by_freq = [r for r in rr_rows if r.control_frequency_hz == row.control_frequency_hz]
        if len(by_freq) == 1:
            return by_freq[0], "control_frequency"
    if row.system_id_p25 is not None:
        by_system = [r for r in rr_rows if row.system_id_p25 in (r.rr_system_id, r.nac)]
        if len(by_system) == 1:
            return by_system[0], "system_id"
    return None, None


def audit_site(
    session: Session, site_id: int, *, settings: Settings | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """The whole report for one site, or ``None`` when there is no such site."""
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    site = session.get(Site, site_id)
    if site is None:
        return None
    system = session.get(System, site.system_id)

    rows = _rows(session, site_id)
    observed = _by_source(rows, "observed")
    documented = _by_source(rows, "radioreference")
    configured = _by_source(rows, "configured")
    stale_before = now - timedelta(days=max(0, settings.channel_audit_stale_days))

    live_control = next(
        (f for f, row in observed.items() if row.role == "primary_control"),
        None,
    )
    observed_alternate = sorted(f for f, row in observed.items() if row.role == "alternate_control")
    rr_primary = sorted(f for f, row in documented.items() if row.role == "primary_control")
    rr_alternate = sorted(f for f, row in documented.items() if row.role == "alternate_control")
    configured_hz = sorted(configured)

    undocumented = [
        {
            "frequency_hz": frequency,
            "role": row.role,
            "channel_ref": row.channel_ref,
            "first_seen_at": _aware(row.first_seen_at),
            "last_seen_at": _aware(row.last_seen_at),
            "grant_count": int(row.grant_count or 0),
            "observation_count": int(row.observation_count or 0),
        }
        for frequency, row in sorted(observed.items())
        if frequency not in documented
    ]

    missing = []
    for frequency, row in sorted(documented.items()):
        heard = observed.get(frequency)
        last_heard = _aware(heard.last_seen_at) if heard is not None else None
        if heard is not None and last_heard is not None and last_heard >= stale_before:
            continue
        missing.append(
            {
                "frequency_hz": frequency,
                "rr_role": row.role,
                "channel_ref": row.channel_ref,
                "last_seen_at": last_heard,
            }
        )

    drift = [
        {
            "frequency_hz": frequency,
            "rr_role": row.role,
            "observed_role": observed[frequency].role,
        }
        for frequency, row in sorted(documented.items())
        if frequency in observed
        and observed[frequency].role != row.role
        and (row.role in CONTROL_ROLES or observed[frequency].role in CONTROL_ROLES)
    ]

    announced_control = set(observed_alternate) | ({live_control} if live_control else set())
    announced_not_configured = sorted(announced_control - set(configured_hz))
    configured_not_announced = sorted(set(configured_hz) - announced_control)
    # Live control channel first, then everything else the site or RR calls a control
    # channel, ascending — the order to paste into the recorder's `control_frequencies`.
    live = {live_control} if live_control else set()
    suggested = sorted(live) + sorted(
        (announced_control | set(rr_primary) | set(rr_alternate)) - live
    )

    index = _known_sites(session)
    rr_rows = _rr_sites(session, site.system_id)
    adjacent = []
    for row in session.scalars(
        select(AdjacentSite)
        .where(AdjacentSite.site_id == site_id)
        .order_by(AdjacentSite.rfss, AdjacentSite.site_number)
    ):
        # A neighbor resolves two ways: to a Keyup ``Site`` the recorder also hears
        # (``known_site_id``), and — the point of this report — to a site RadioReference
        # documents (``known_in_rr``), which gives it a name even when Keyup never heard it.
        known = resolve_known_site(index, row.system_id_p25, row.rfss, row.site_number)
        rr_site, match = _match_rr_site(row, rr_rows)
        adjacent.append(
            {
                "system_id_p25": row.system_id_p25,
                "rfss": row.rfss,
                "site_number": row.site_number,
                "lra": row.lra,
                "control_frequency_hz": row.control_frequency_hz,
                "channel_ref": row.channel_ref,
                "flags": row.flags,
                "first_seen_at": _aware(row.first_seen_at),
                "last_seen_at": _aware(row.last_seen_at),
                # "known in RR" now means the neighbor resolves to a site in the
                # RadioReference cache; a neighbor that resolves to nothing is the
                # interesting case — a tower nobody has documented.
                "known_in_rr": rr_site is not None,
                "known_site_id": known.id if known is not None else None,
                "known_site_name": (
                    rr_site.name
                    if rr_site is not None and rr_site.name
                    else (known.name if known is not None else None)
                ),
                "known_county": rr_site.county if rr_site is not None else None,
                "match": match,
            }
        )

    return {
        "site": {
            "id": site.id,
            "system_id": site.system_id,
            "system": system.name if system is not None else None,
            "name": site.name,
            "site_number": site.site_number,
            "description": site.description,
            "nac": site.nac,
            "rfss": site.rfss,
            "lra": site.lra,
            "rr_site_id": site.rr_site_id,
        },
        "rr_synced_at": _aware(site.rr_synced_at),
        "last_observed_at": _aware(last_observed_at(session, site_id)),
        "stale_days": settings.channel_audit_stale_days,
        "control": {
            "observed": live_control,
            "rr_primary": rr_primary,
            "rr_alternate": rr_alternate,
            "observed_alternate": observed_alternate,
            "configured": configured_hz,
        },
        "undocumented": undocumented,
        "missing": missing,
        "control_drift": drift,
        "recorder_config": {
            "announced_not_configured": announced_not_configured,
            "configured_not_announced": configured_not_announced,
            "suggested_control_frequencies": suggested,
        },
        "adjacent_sites": adjacent,
    }


def audit_system(
    session: Session, system_id: int, *, settings: Settings | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """Per-site counts for one system — the overview that says which site to open."""
    system = session.get(System, system_id)
    if system is None:
        return None
    sites = []
    for site in session.scalars(
        select(Site).where(Site.system_id == system_id).order_by(Site.name)
    ):
        report = audit_site(session, site.id, settings=settings, now=now)
        if report is None:  # pragma: no cover - the site was just read
            continue
        sites.append(
            {
                "site_id": site.id,
                "site": site.name,
                "last_observed_at": report["last_observed_at"],
                "rr_synced_at": report["rr_synced_at"],
                "control_observed": report["control"]["observed"],
                "undocumented": len(report["undocumented"]),
                "missing": len(report["missing"]),
                "control_drift": len(report["control_drift"]),
                "announced_not_configured": len(report["recorder_config"]["announced_not_configured"]),
                "adjacent_unknown": sum(
                    1 for entry in report["adjacent_sites"] if entry["known_site_id"] is None
                ),
            }
        )
    return {
        "system": {
            "id": system.id,
            "name": system.name,
            "rr_system_id": system.rr_system_id,
            "rr_synced_at": _aware(system.rr_synced_at),
        },
        "sites": sites,
        "totals": {
            key: sum(int(row[key]) for row in sites)
            for key in (
                "undocumented",
                "missing",
                "control_drift",
                "announced_not_configured",
                "adjacent_unknown",
            )
        },
    }


# --------------------------------------------------------------------------------------
# alerts — once per frequency, once per adjacent site, ever
# --------------------------------------------------------------------------------------


@dataclass
class AuditFinding:
    """One thing worth telling somebody about, and the row that records it was told."""

    kind: str  # 'undocumented_frequency' | 'unknown_adjacent_site'
    site: Site
    heading: str
    detail: str
    payload: dict[str, Any]


class _AuditSummary(CallSummary):
    """A finding wearing the shape :func:`app.alerts.deliver` renders.

    Subclassing rather than duck-typing keeps every channel — webhook, Discord, ntfy,
    email — working unchanged; only the three things a channel actually reads (the title,
    the message and the payload) are overridden.
    """

    def __init__(self, finding: AuditFinding, *, url: str) -> None:
        super().__init__(
            call_id=0,
            started_at=datetime.now(UTC),
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
        self.finding = finding

    @property
    def title(self) -> str:
        return self.finding.heading

    @property
    def message(self) -> str:
        return self.finding.detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": ALERT_KIND,
            "finding": self.finding.kind,
            "site": {"id": self.finding.site.id, "name": self.finding.site.name},
            "title": self.title,
            "message": self.message,
            "url": self.url,
            **self.finding.payload,
        }


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware is not None else None


def _mhz(frequency_hz: int | None) -> str:
    return "—" if not frequency_hz else f"{frequency_hz / 1e6:.4f} MHz"


def _pending_findings(session: Session, site: Site, *, now: datetime) -> list[AuditFinding]:
    """Rows that have never been alerted on and are worth alerting on now.

    An observed frequency is only "undocumented" once the site has RadioReference rows to
    be absent from: before the first sync, everything is undocumented and none of it is
    news.  An adjacent site is only news when it resolves to no site Keyup knows.
    """
    findings: list[AuditFinding] = []
    documented = {
        row.frequency_hz
        for row in session.scalars(
            select(SiteFrequency).where(
                SiteFrequency.site_id == site.id, SiteFrequency.source == "radioreference"
            )
        )
    }
    if documented:
        for row in session.scalars(
            select(SiteFrequency)
            .where(
                SiteFrequency.site_id == site.id,
                SiteFrequency.source == "observed",
                SiteFrequency.alerted_at.is_(None),
            )
            .order_by(SiteFrequency.frequency_hz)
        ):
            if row.frequency_hz in documented:
                continue
            findings.append(
                AuditFinding(
                    kind="undocumented_frequency",
                    site=site,
                    heading=f"Keyup: undocumented channel at {site.name}",
                    detail=(
                        f"{_mhz(row.frequency_hz)} ({row.role.replace('_', ' ')}) is on the air at "
                        f"{site.name} and is not in RadioReference."
                    ),
                    payload={
                        "frequency_hz": row.frequency_hz,
                        "role": row.role,
                        "channel_ref": row.channel_ref,
                        # ISO text, not a datetime: the payload goes out as JSON on the
                        # webhook channel and is rendered into the others.
                        "first_seen_at": _iso(row.first_seen_at),
                        "grant_count": int(row.grant_count or 0),
                    },
                )
            )
            row.alerted_at = now

    index = _known_sites(session)
    for row in session.scalars(
        select(AdjacentSite).where(
            AdjacentSite.site_id == site.id, AdjacentSite.alerted_at.is_(None)
        )
    ):
        known = resolve_known_site(index, row.system_id_p25, row.rfss, row.site_number)
        row.alerted_at = now
        if known is not None:
            continue
        findings.append(
            AuditFinding(
                kind="unknown_adjacent_site",
                site=site,
                heading=f"Keyup: unknown neighbor of {site.name}",
                detail=(
                    f"{site.name} announces RFSS {row.rfss} site {row.site_number} "
                    f"(system {row.system_id_p25}) on {_mhz(row.control_frequency_hz)}; "
                    "Keyup has no such site."
                ),
                payload={
                    "system_id_p25": row.system_id_p25,
                    "rfss": row.rfss,
                    "site_number": row.site_number,
                    "control_frequency_hz": row.control_frequency_hz,
                },
            )
        )
    return findings


def raise_new_findings(
    session: Session,
    site_id: int,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> list[Notification]:
    """Alert once — ever — for each newly undocumented frequency or unknown neighbor.

    ``alerted_at`` on the row is the rate limiter, so a frequency that is heard every five
    minutes for a year is one alert.  When no ``channel_audit`` rule is enabled nothing is
    stamped, so enabling one later still reports what is already on the books.
    """
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    site = session.get(Site, site_id)
    if site is None:
        return []

    rules = list(
        session.scalars(
            select(AlertRule)
            .where(AlertRule.enabled.is_(True), AlertRule.kind == ALERT_KIND)
            .order_by(AlertRule.id)
        )
    )
    if not rules:
        return []

    findings = _pending_findings(session, site, now=now)
    if not findings:
        return []

    base = settings.public_base_url.rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=settings.alert_timeout_s)
    written: list[Notification] = []
    try:
        for finding in findings:
            summary = _AuditSummary(finding, url=f"{base}/sites?site={site.id}")
            for rule in rules:
                for channel in rule.channels or ():
                    notification = Notification(
                        rule_id=rule.id,
                        call_id=None,
                        channel=channel,
                        severity="info",
                        subject=summary.title,
                        body=summary.message[:4000],
                        ok=True,
                    )
                    try:
                        deliver(rule, channel, summary, settings, http)
                    except Exception as exc:  # noqa: BLE001 - delivery failures are data
                        notification.ok = False
                        notification.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:2000]
                        log.warning("channel audit: rule %s channel %s failed: %s", rule.id, channel, exc)
                    session.add(notification)
                    written.append(notification)
                rule.last_fired_at = now
    finally:
        if owned:
            http.close()
    return written


# --------------------------------------------------------------------------------------
# the scheduled job
# --------------------------------------------------------------------------------------


def run_channel_audit(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    rr_client: Any | None = None,
) -> list[dict[str, Any]]:
    """Recompute every site's audit, log a line each, and raise the new alerts.

    Also the place the RadioReference sync is *due*-checked from, so a deployment that
    configures RR gets its documentation refreshed without a second scheduler entry.
    """
    from app.rr.sync import sync_due  # noqa: PLC0415 - avoids a cycle at import time

    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)

    try:
        for summary in sync_due(session, settings=settings, client=rr_client, now=now):
            log.info("channel audit: rr sync %s", summary.as_dict())
    except Exception:  # noqa: BLE001 - RR is somebody else's server
        log.exception("channel audit: RadioReference sync failed")
        session.rollback()

    reports = []
    for site in session.scalars(select(Site).order_by(Site.system_id, Site.name)):
        report = audit_site(session, site.id, settings=settings, now=now)
        if report is None:  # pragma: no cover
            continue
        raise_new_findings(session, site.id, settings=settings, now=now)
        reports.append(report)
        log.info(
            "channel audit: %s — control %s, %d undocumented, %d missing, %d drift, "
            "%d announced-not-configured, %d unknown neighbors",
            site.name,
            _mhz(report["control"]["observed"]),
            len(report["undocumented"]),
            len(report["missing"]),
            len(report["control_drift"]),
            len(report["recorder_config"]["announced_not_configured"]),
            sum(1 for entry in report["adjacent_sites"] if entry["known_site_id"] is None),
        )
    return reports
