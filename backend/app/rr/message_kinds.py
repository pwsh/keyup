"""What the control channel has *ever* said, and the alert when that changes.

docs/UNKNOWN-DATA-PLAN.md phase 5 — the ``new_unknown`` hook.  The message census
(``GET /api/sites/{id}/message-mix``) folds ``message_mix`` out of the last *n* days of
stored observation payloads: it says what a site is transmitting now, in what proportion,
and it is recomputed on every read.  What it cannot say is whether a ``mfid:opcode`` is
*new* — a rolling window has no memory, and "97 kinds" looks exactly the same whether the
98th appeared this morning or a year ago.

``site_message_kinds`` is that memory: one row per ``(site, mfid, opcode)``, updated in the
same transaction as the observation window that carried it.  Two things about a row are
worth waking an operator for:

``new_kind``
    a pair this site has never transmitted before.  Either the network turned a feature on,
    or the recorder started decoding far enough to see it — both are worth a look.
``exemplar_changed``
    a kind the recorder cannot decode whose hex body is no longer the body it was.  The
    Harris beacons are the reason: a beacon that repeats one payload for months and then
    does not has just told us which bits are a field.  Only kinds seen more than
    :data:`EXEMPLAR_MIN_COUNT` times qualify, because a message seen twice changing its
    bytes is not a beacon, it is a message with an argument in it.

Both raise an activity row (``site.unknown_new`` / ``site.unknown_changed``) and, when an
alert rule of the matching kind is enabled, deliver on that rule's channels.  ``alerted_at``
on the row is the rate limiter: one event per ``(site, kind)`` per
:data:`ALERT_INTERVAL`, so a beacon that flaps every five minutes is one alert a day.

**Nothing alerts on a site whose row set is empty.**  The first window into an empty table
seeds it silently — otherwise the first deploy would report all ~97 kinds the site has been
transmitting all along as news.  ``keyup sites seed-message-kinds`` does the same job
deliberately, replaying every stored window, and is what a deployment should run once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import activity
from app.alerts import CallSummary, deliver, scrub_secrets
from app.config import Settings
from app.models import AlertRule, Notification, RecorderObservation, Site, SiteMessageKind
from app.p25.message_names import label_for, load_curated
from app.runtime_settings import effective_settings

log = logging.getLogger("app.rr.message_kinds")

__all__ = [
    "ALERT_INTERVAL",
    "ALERT_KINDS",
    "EXEMPLAR_MIN_COUNT",
    "MessageKindFinding",
    "NamingResult",
    "name_message_kinds",
    "raise_message_kind_alerts",
    "record_message_kinds",
    "seed_message_kinds",
]

#: the ``alert_rules.kind`` values this module fires, one per finding kind
ALERT_KINDS = ("new_kind", "exemplar_changed")

#: the live-bus event name these rules hang off in :data:`app.alerts.KINDS_BY_EVENT`
ALERT_EVENT = "site.message"

#: How often one ``(site, mfid, opcode)`` may raise.  A beacon that alternates between two
#: payloads would otherwise alert on every window for ever.
ALERT_INTERVAL = timedelta(hours=24)

#: How many times a kind must have been counted before a change in its bytes is a beacon
#: changing rather than an argument varying.
EXEMPLAR_MIN_COUNT = 1_000

#: How much of an exemplar a one-line message shows.
EXEMPLAR_PREVIEW = 8


def _short(exemplar: str | None) -> str:
    if not exemplar:
        return "—"
    return exemplar if len(exemplar) <= EXEMPLAR_PREVIEW else f"{exemplar[:EXEMPLAR_PREVIEW]}…"


def _mix_key(raw: Any) -> tuple[int, int] | None:
    """``"164:15"`` / ``"164/15"`` / ``"15"`` → ``(mfid, opcode)``.

    The same reading :func:`app.api.radio_events._mix_key` does, kept here rather than
    imported so the ingest does not depend on an API module (and so a census key is never
    dropped for punctuation on one side and kept on the other).
    """
    text = str(raw).strip()
    for separator in (":", "/", "-"):
        if separator in text:
            left, _, right = text.partition(separator)
            try:
                return int(left), int(right)
            except ValueError:
                return None
    try:
        return 0, int(text)
    except ValueError:
        return None


@dataclass
class MessageKindFinding:
    """One thing worth telling the operator about one message kind."""

    kind: str  # new_kind | exemplar_changed
    site_id: int
    site_name: str
    mfid: int
    opcode: int
    count: int
    exemplar: str | None = None
    exemplar_previous: str | None = None

    @property
    def key(self) -> str:
        return f"{self.mfid}:{self.opcode}"

    @property
    def heading(self) -> str:
        if self.kind == "new_kind":
            return f"Keyup: new message kind {self.key} at {self.site_name}"
        return f"Keyup: message kind {self.key} changed at {self.site_name}"

    @property
    def detail(self) -> str:
        if self.kind == "new_kind":
            return (
                f"new control-channel message kind {self.key} on {self.site_name} "
                f"(first exemplar {_short(self.exemplar)})"
            )
        return (
            f"control-channel message kind {self.key} on {self.site_name} changed its bytes "
            f"({_short(self.exemplar_previous)} → {_short(self.exemplar)}, {self.count:,} seen)"
        )

    @property
    def activity_kind(self) -> str:
        return "site.unknown_new" if self.kind == "new_kind" else "site.unknown_changed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "site": self.site_name,
            "key": self.key,
            "mfid": self.mfid,
            "opcode": self.opcode,
            "count": self.count,
            "exemplar": self.exemplar,
            "exemplar_previous": self.exemplar_previous,
        }


# --------------------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------------------


def _counts(payload: dict[str, Any]) -> dict[tuple[int, int], int]:
    """``message_mix`` as ``(mfid, opcode) → count``, ignoring what will not parse."""
    mix = payload.get("message_mix")
    if not isinstance(mix, dict):
        return {}
    out: dict[tuple[int, int], int] = {}
    for raw_key, raw_count in mix.items():
        key = _mix_key(raw_key)
        if key is None:
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        out[key] = out.get(key, 0) + count
    return out


def _exemplars(payload: dict[str, Any]) -> dict[tuple[int, int], str]:
    """``unknown_exemplars`` as ``(mfid, opcode) → hex``; the last entry in a window wins."""
    out: dict[tuple[int, int], str] = {}
    for entry in payload.get("unknown_exemplars") or ():
        if not isinstance(entry, dict) or not entry.get("exemplar"):
            continue
        try:
            key = (int(entry.get("mfid") or 0), int(entry["opcode"]))
        except (TypeError, ValueError, KeyError):
            continue
        out[key] = str(entry["exemplar"])
    return out


def _rows(session: Session, site_id: int) -> dict[tuple[int, int], SiteMessageKind]:
    return {
        (row.mfid, row.opcode): row
        for row in session.scalars(select(SiteMessageKind).where(SiteMessageKind.site_id == site_id))
    }


def record_message_kinds(
    session: Session,
    *,
    site_id: int,
    payload: dict[str, Any],
    observed_at: datetime,
    now: datetime | None = None,
    silent: bool = False,
    rows: dict[tuple[int, int], SiteMessageKind] | None = None,
) -> list[MessageKindFinding]:
    """Fold one window's census into ``site_message_kinds``; return what is worth saying.

    Runs in the caller's transaction and does not commit — the row and the observation that
    produced it land together, so a rolled-back window never leaves a kind marked as seen.

    ``silent`` (or an empty table for this site) folds without producing findings: that is
    the implicit baseline, and it is why a fresh deployment does not announce the 97 kinds
    the site has always transmitted.  Findings are also suppressed per row by
    :data:`ALERT_INTERVAL`, stamped on ``alerted_at`` here so the rate limit holds whether or
    not any alert rule is enabled.
    """
    now = now or datetime.now(UTC)
    site = session.get(Site, site_id)
    if site is None:  # pragma: no cover - the caller resolved it a moment ago
        return []

    # ``rows`` lets a replay hold one site's rows across thousands of windows instead of
    # re-reading them per window; the ingest path passes none and reads them once.
    if rows is None:
        rows = _rows(session, site_id)
    # An empty table for this site is a site nobody has ever folded: seed it, say nothing.
    quiet = silent or not rows
    counts = _counts(payload)
    exemplars = _exemplars(payload)
    findings: list[MessageKindFinding] = []

    def announce(finding: MessageKindFinding, row: SiteMessageKind) -> None:
        if quiet:
            return
        if row.alerted_at is not None and now - row.alerted_at < ALERT_INTERVAL:
            return
        row.alerted_at = now
        findings.append(finding)

    # Every key the window counted, plus any exemplar for a kind the mix did not mention:
    # an exemplar is evidence the message was heard, so it must not be able to arrive for a
    # kind that has no row.
    for key in sorted(set(counts) | set(exemplars)):
        mfid, opcode = key
        count = counts.get(key, 0)
        row = rows.get(key)
        if row is None:
            # A kind arrives named when anything names it: the standard opcodes have had
            # names since TIA-102.AABC and there is no sense in a site appearing with 267
            # anonymous rows when two hundred of them are GRP_VCH_GRNT_UPD.  Only what
            # nothing names is left unknown — which is what makes the count mean something.
            label, named = label_for(mfid, opcode)
            row = SiteMessageKind(
                site_id=site_id,
                mfid=mfid,
                opcode=opcode,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                count=count,
                exemplar=exemplars.get(key),
                known=named,
                label=label,
            )
            session.add(row)
            rows[key] = row
            announce(
                MessageKindFinding(
                    kind="new_kind",
                    site_id=site_id,
                    site_name=site.name,
                    mfid=mfid,
                    opcode=opcode,
                    count=count,
                    exemplar=exemplars.get(key),
                ),
                row,
            )
            continue

        row.count = int(row.count or 0) + count
        if row.first_seen_at is None or observed_at < row.first_seen_at:
            row.first_seen_at = observed_at
        if row.last_seen_at is None or observed_at > row.last_seen_at:
            row.last_seen_at = observed_at

        exemplar = exemplars.get(key)
        if exemplar is None or exemplar == row.exemplar:
            continue
        if row.exemplar is None:
            # First bytes for a kind we had only counted: not a change, so not news.
            row.exemplar = exemplar
            continue
        previous, row.exemplar = row.exemplar, exemplar
        row.exemplar_previous = previous
        row.exemplar_changed_at = observed_at
        if int(row.count or 0) < EXEMPLAR_MIN_COUNT:
            continue  # an argument varying, not a beacon changing
        announce(
            MessageKindFinding(
                kind="exemplar_changed",
                site_id=site_id,
                site_name=site.name,
                mfid=mfid,
                opcode=opcode,
                count=int(row.count or 0),
                exemplar=exemplar,
                exemplar_previous=previous,
            ),
            row,
        )

    for finding in findings:
        activity.record(
            session,
            source="site",
            level="warn",
            kind=finding.activity_kind,
            message=finding.detail,
            meta=finding.as_dict(),
        )
    return findings


# --------------------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------------------


class _MessageKindSummary(CallSummary):
    """A finding wearing the shape :func:`app.alerts.deliver` renders.

    Subclassed rather than duck-typed for the same reason the channel audit does it: every
    channel — webhook, Discord, ntfy, email — keeps working unchanged, and only the three
    things a channel actually reads are overridden.
    """

    def __init__(self, finding: MessageKindFinding, *, url: str) -> None:
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
            "kind": self.finding.kind,
            "event": ALERT_EVENT,
            "title": self.title,
            "message": self.message,
            "url": self.url,
            **self.finding.as_dict(),
        }


def raise_message_kind_alerts(
    session: Session,
    findings: list[MessageKindFinding],
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> list[Notification]:
    """Deliver each finding on every enabled rule of its own kind.

    The activity row was written by :func:`record_message_kinds` whether or not a rule
    exists — the log is the archive's memory and does not depend on anybody having
    configured a webhook.  This is only the outbound half.
    """
    if not findings:
        return []
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)

    rules_by_kind: dict[str, list[AlertRule]] = {}
    for rule in session.scalars(
        select(AlertRule)
        .where(AlertRule.enabled.is_(True), AlertRule.kind.in_(ALERT_KINDS))
        .order_by(AlertRule.id)
    ):
        rules_by_kind.setdefault(rule.kind, []).append(rule)
    if not rules_by_kind:
        return []

    base = settings.public_base_url.rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=settings.alert_timeout_s)
    written: list[Notification] = []
    try:
        for finding in findings:
            rules = rules_by_kind.get(finding.kind) or []
            if not rules:
                continue
            summary = _MessageKindSummary(finding, url=f"{base}/sites?site={finding.site_id}")
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
                        log.warning("message kinds: rule %s channel %s failed: %s", rule.id, channel, exc)
                    session.add(notification)
                    written.append(notification)
                rule.last_fired_at = now
    finally:
        if owned:
            http.close()
    return written


# --------------------------------------------------------------------------------------
# the baseline
# --------------------------------------------------------------------------------------


@dataclass
class SeedResult:
    """What one ``keyup sites seed-message-kinds`` run did."""

    sites: int = 0
    windows: int = 0
    kinds: int = 0
    per_site: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sites": self.sites,
            "windows": self.windows,
            "kinds": self.kinds,
            "per_site": self.per_site,
        }


def seed_message_kinds(session: Session, *, site_id: int | None = None, commit: bool = True) -> SeedResult:
    """Replay the stored observation windows into ``site_message_kinds``, silently.

    The baseline every deployment wants before the first alert can mean anything: without
    it the next window a site posts would report every kind it has ever transmitted as new.
    (The ingest hook makes that safe on its own — the first window into an empty table
    seeds it — but this replays the *history*, so first/last seen and the counts are the
    ones the archive already has rather than the ones from tonight.)

    The aggregation is a plain dict per site, folded in ``observed_at`` order so
    ``first_seen_at`` is the first window that carried the kind; roughly 3 200 windows per
    site per week makes it a few seconds of Python and no query per key.
    """
    result = SeedResult()
    stmt = (
        select(RecorderObservation.site_id, RecorderObservation.observed_at, RecorderObservation.payload)
        .where(RecorderObservation.site_id.is_not(None))
        .order_by(RecorderObservation.observed_at)
    )
    if site_id is not None:
        stmt = stmt.where(RecorderObservation.site_id == site_id)

    cache: dict[int, dict[tuple[int, int], SiteMessageKind]] = {}
    for row_site_id, observed_at, payload in session.execute(stmt).yield_per(500):
        if not isinstance(payload, dict):
            continue
        result.windows += 1
        rows = cache.get(row_site_id)
        if rows is None:
            rows = cache[row_site_id] = _rows(session, row_site_id)
        record_message_kinds(
            session,
            site_id=row_site_id,
            payload=payload,
            observed_at=observed_at,
            silent=True,
            rows=rows,
        )

    session.flush()
    for one, rows in sorted(cache.items()):
        site = session.get(Site, one)
        result.per_site[site.name if site is not None else str(one)] = len(rows)
        result.kinds += len(rows)
    result.sites = len(cache)
    if commit:
        session.commit()
    return result


# --------------------------------------------------------------------------------------
# naming what is already stored
# --------------------------------------------------------------------------------------


@dataclass
class NamingResult:
    """What one ``keyup sites name-message-kinds`` run did."""

    rows: int = 0
    #: rows that carry a label after the run
    named: int = 0
    #: rows that count as known after the run (a noise label is not knowledge)
    known: int = 0
    #: rows this run actually wrote
    changed: int = 0
    dry_run: bool = False
    per_site: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "named": self.named,
            "known": self.known,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "per_site": self.per_site,
        }


def name_message_kinds(
    session: Session,
    *,
    toml_path: str | None = None,
    site_id: int | None = None,
    dry_run: bool = False,
    commit: bool = True,
) -> NamingResult:
    """Name every stored ``site_message_kinds`` row the tables know a name for.

    The ingest names a kind as it is first inserted, so this is the retrospective half:
    the rows that were written before there was a table to name them from.  It is
    idempotent — a row already carrying the name it would be given is not written — so it
    is safe to re-run after every registry update, which is the point of ``--toml``.

    A row an operator has named by hand is **not** overwritten: a hand-written label is a
    finding about this site, and the built-in table only knows about the protocol.
    """
    curated = load_curated(toml_path) if toml_path else {}
    stmt = select(SiteMessageKind)
    if site_id is not None:
        stmt = stmt.where(SiteMessageKind.site_id == site_id)
    result = NamingResult(dry_run=dry_run)
    sites: dict[int, str] = {}

    for row in session.scalars(stmt.order_by(SiteMessageKind.site_id, SiteMessageKind.mfid)):
        result.rows += 1
        name = sites.get(row.site_id)
        if name is None:
            site = session.get(Site, row.site_id)
            name = sites[row.site_id] = site.name if site is not None else str(row.site_id)
        counts = result.per_site.setdefault(name, {"rows": 0, "named": 0, "known": 0, "changed": 0})
        counts["rows"] += 1

        label, known = label_for(row.mfid, row.opcode, curated)
        if label is not None and not (row.label and row.label != label and row.known):
            if row.label != label or row.known != known:
                result.changed += 1
                counts["changed"] += 1
                if not dry_run:
                    row.label = label
                    row.known = known
            # Report the state the run leaves the row in, dry or not.
            row_label, row_known = label, known
        else:
            row_label, row_known = row.label, bool(row.known)

        if row_label:
            result.named += 1
            counts["named"] += 1
        if row_known:
            result.known += 1
            counts["known"] += 1

    if dry_run:
        session.rollback()
    elif commit:
        session.commit()
    return result
