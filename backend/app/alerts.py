"""Alert rules and their delivery channels (ARCHITECTURE §3.4, §7).

``evaluate(session, call_id, event=…)`` is called by the worker's LISTEN loop for every
live-bus event:

* ``call.new`` runs the ``talkgroup``, ``radio`` and ``emergency`` rules — they only need
  the metadata, so the alert fires within a second of the recording landing;
* ``call.transcribed`` runs the ``keyword`` and ``regex`` rules, which need the text.

A rule that matches and is out of its cooldown is delivered to each of its ``channels``
and logged in ``notifications`` — one row per channel, carrying ``ok``/``error``, so a
webhook that has been 500ing for a week is visible in the admin UI instead of silent.

Nothing in here raises into the worker: a broken rule, an unreachable webhook and an
invalid regex are all recorded and stepped over.
"""

from __future__ import annotations

import json
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import httpx
import regex
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AlertRule, Call, Notification, Radio, Talkgroup
from app.runtime_settings import effective_settings

log = logging.getLogger("app.alerts")

__all__ = [
    "MAX_PATTERN_LENGTH",
    "REDACTED",
    "REGEX_TIMEOUT_S",
    "SELF_RAISED_EVENTS",
    "SKIPPED_SEVERITY",
    "CallSummary",
    "deliver",
    "emergency_scope",
    "evaluate",
    "redact_channel_config",
    "scrub_secrets",
    "summarize",
    "unredact",
    "validate_channel_config",
    "validate_pattern",
    "validate_rule",
]

#: which rule kinds each live-bus event can fire
#:
#: ``site.message`` is not a call event: it is raised at observation ingest by
#: :mod:`app.rr.message_kinds` when a site transmits a ``mfid:opcode`` it never has before,
#: or when a beacon's undecoded bytes change.  It is listed here because this table is what
#: says which rule kinds exist and what fires them — but its findings are about a *site*,
#: so :func:`evaluate`, which loads a call, steps over it and the module delivers its own.
KINDS_BY_EVENT: dict[str, tuple[str, ...]] = {
    "call.new": ("talkgroup", "radio", "emergency"),
    "call.transcribed": ("keyword", "regex"),
    "site.message": ("new_kind", "exemplar_changed"),
    # the worker's five-minute disk check (:mod:`app.disk_watch`): free space crossed the
    # warning or the critical threshold
    "system.disk_low": ("disk_low",),
    # the worker's once-a-minute health check (:mod:`app.health`): a condition raised or
    # cleared
    "system.health": ("health",),
}

#: Events whose alerts are raised by the code that found them rather than by
#: :func:`evaluate` — there is no call to summarize.  ``channel_audit`` has always worked
#: this way; ``site.message`` is the second, ``system.disk_low`` the third and
#: ``system.health`` the fourth.
SELF_RAISED_EVENTS = frozenset({"site.message", "system.disk_low", "system.health"})

CHANNELS = ("webhook", "discord", "ntfy", "email")


@dataclass
class CallSummary:
    """The payload every channel renders — one call, flattened and link-ready."""

    call_id: int
    started_at: datetime
    duration_s: float
    system: str | None
    talkgroup_id: int | None
    talkgroup_tgid: int | None
    talkgroup_alias: str | None
    talkgroup_ids: list[int]
    radio_id: int | None
    radio_label: str | None
    transcript: str | None
    quality: str | None
    url: str
    audio_url: str
    #: The on-air radio number (``radios.radio_id``); ``radio_id`` above is the row id.
    radio_number: int | None = None
    #: The P25 emergency flag the recorder reported, and the talkgroup's category
    #: (``law``, ``fire``, ``ems`` …) — what an ``emergency`` rule narrows on.
    emergency: bool = False
    talkgroup_category: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "started_at": self.started_at.isoformat(),
            "duration_s": self.duration_s,
            "system": self.system,
            "talkgroup": {
                "id": self.talkgroup_id,
                "tgid": self.talkgroup_tgid,
                "alias": self.talkgroup_alias,
            },
            "talkgroup_ids": self.talkgroup_ids,
            "radio": {"id": self.radio_id, "number": self.radio_number, "label": self.radio_label},
            "emergency": self.emergency,
            "category": self.talkgroup_category,
            "transcript": self.transcript,
            "quality": self.quality,
            "url": self.url,
            "audio_url": self.audio_url,
        }

    @property
    def title(self) -> str:
        where = self.talkgroup_alias or (str(self.talkgroup_tgid) if self.talkgroup_tgid else "unknown")
        return f"Keyup: EMERGENCY on {where}" if self.emergency else f"Keyup: {where}"

    @property
    def message(self) -> str:
        when = self.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        body = self.transcript or "(no transcript)"
        unit = f" [{self.radio_label}]" if self.radio_label else ""
        return f"{when}{unit} ({self.duration_s:.1f}s)\n{body}"


def summarize(session: Session, call_id: int, settings: Settings | None = None) -> CallSummary | None:
    """Load one call into the shape the channels want, or ``None`` when it is gone."""
    settings = settings or get_settings()
    row = session.execute(
        select(Call, Talkgroup, Radio)
        .outerjoin(Talkgroup, Call.talkgroup_id == Talkgroup.id)
        .outerjoin(Radio, Call.radio_id == Radio.id)
        .where(Call.id == call_id)
    ).first()
    if row is None:
        return None
    call, talkgroup, radio = row
    base = settings.public_base_url.rstrip("/")
    return CallSummary(
        call_id=call.id,
        started_at=call.started_at,
        duration_s=float(call.duration_s),
        system=call.system.name if call.system is not None else None,
        talkgroup_id=call.talkgroup_id,
        talkgroup_tgid=talkgroup.tgid if talkgroup is not None else None,
        talkgroup_alias=(talkgroup.alias if talkgroup is not None else None)
        or call.talkgroup_alias_at_record,
        talkgroup_ids=list(call.talkgroup_ids or []),
        radio_id=call.radio_id,
        radio_label=(radio.unit_id or radio.alias or str(radio.radio_id)) if radio is not None else None,
        radio_number=radio.radio_id if radio is not None else None,
        emergency=bool(call.emergency),
        talkgroup_category=talkgroup.category if talkgroup is not None else None,
        transcript=call.transcript_text,
        quality=call.transcript_quality,
        url=f"{base}/calls/{call.id}",
        audio_url=f"{base}/api/calls/{call.id}/audio",
    )


# --------------------------------------------------------------------------------------
# secrets, and what a rule is allowed to say
# --------------------------------------------------------------------------------------

#: Channel-config keys that are credentials in everything but name: a Discord webhook URL
#: *is* the credential, an ntfy URL names a topic anybody may then publish to, and custom
#: headers are where an ``Authorization`` ends up.  They never leave this process.
SECRET_CONFIG_KEYS = frozenset(
    {"url", "webhook_url", "headers", "token", "api_key", "key", "password", "auth", "target"}
)

#: What a redacted value reads as on the wire.  It is also accepted *back*: the Alerts
#: form round-trips the whole rule, and a field that came back unchanged must not
#: overwrite the stored secret with its own mask.
REDACTED = "__redacted__"

#: Schemes a rule may deliver to.  Private addresses stay allowed on purpose — webhooks on
#: this LAN are the normal case — which is why rule editing is an admin action (H2).
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Longest ``pattern`` a rule may carry.  A regex is run against every transcript, so the
#: cost of a bad one is paid forever; a short one is easier to reason about, too.
MAX_PATTERN_LENGTH = 500

#: Wall clock one regex match may take before the rule is skipped for this call (M2).
REGEX_TIMEOUT_S = 0.1

#: ``notifications.severity`` for a rule that was skipped rather than delivered.
SKIPPED_SEVERITY = "skipped"

_URL_RE = re.compile(r"\b(?:https?|ftp)://\S+", re.IGNORECASE)


def scrub_secrets(text: str | None) -> str | None:
    """Replace every URL in a message with ``<url>``.

    Delivery failures are stored on ``notifications.error`` and read back by anybody who
    can see the archive, and an httpx error quotes the URL it failed to reach — which for
    a Discord webhook is the credential itself.
    """
    if not text:
        return text
    return _URL_RE.sub("<url>", text)


def redact_channel_config(config: Any) -> dict[str, Any]:
    """A channel config safe to hand to a reader: the shape, never the secrets."""
    if not isinstance(config, dict):
        return {}
    out: dict[str, Any] = {}
    for channel, value in config.items():
        if isinstance(value, dict):
            out[channel] = {
                key: (REDACTED if key in SECRET_CONFIG_KEYS and item else item)
                for key, item in value.items()
            }
        elif channel in SECRET_CONFIG_KEYS and value:
            out[channel] = REDACTED
        else:
            out[channel] = value
    return out


def unredact(new: Any, previous: Any) -> Any:
    """Merge an inbound config over the stored one, keeping masked values as they were."""
    if not isinstance(new, dict):
        return previous if new == REDACTED else new
    old = previous if isinstance(previous, dict) else {}
    merged = dict(old)
    for key, value in new.items():
        if value == REDACTED:
            continue  # the caller sent our own mask back: keep what is stored
        merged[key] = unredact(value, old.get(key))
    return merged


def validate_pattern(kind: str, pattern: str | None) -> None:
    """Reject a pattern that is too long, or a regex that does not compile (M2).

    Compiling here rather than at match time means a rule that cannot work is refused at
    the moment somebody writes it, instead of failing silently on every transcript.
    """
    if pattern is None:
        return
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"pattern is longer than {MAX_PATTERN_LENGTH} characters")
    if kind == "regex" and pattern.strip():
        try:
            regex.compile(pattern, regex.IGNORECASE)
        except regex.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc


def _check_url(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    scheme = (urlsplit(value.strip()).scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"{where}: {scheme or 'no'} scheme is not deliverable — use http:// or https://"
        )


def validate_channel_config(config: Any) -> None:
    """Every URL a rule may be asked to fetch must be plain http or https (H2)."""
    if not isinstance(config, dict):
        return
    for channel, value in config.items():
        if isinstance(value, dict):
            for key in ("url", "webhook_url"):
                _check_url(value.get(key), f"{channel}.{key}")
        elif channel == "target":
            _check_url(value, "target")


def validate_rule(kind: str, pattern: str | None, channel_config: Any) -> None:
    """Everything a rule must satisfy before it is stored.  Raises ``ValueError``."""
    validate_pattern(kind, pattern)
    validate_channel_config(channel_config)


# --------------------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------------------


def _ids(value: Any) -> set[int]:
    """Coerce a rule's id list — JSON array, comma string or single number — to ints."""
    out: set[int] = set()
    if value is None:
        return out
    items = value if isinstance(value, list | tuple | set) else str(value).replace(",", " ").split()
    for item in items:
        try:
            out.add(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _rule_ids(rule: AlertRule, *keys: str) -> set[int]:
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    ids: set[int] = _ids(rule.pattern)
    for key in keys:
        ids |= _ids(filters.get(key))
    return ids


def _system_matches(rule: AlertRule, call: Call, summary: CallSummary) -> bool:
    """True unless the rule names systems and this call is on none of them.

    ``filters.system`` follows ``GET /calls?system=``: a system id or a system name (case
    folded), one value, a list or a comma string.  Absent means every system.
    """
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    raw = filters.get("system")
    if raw is None or raw == "" or raw == []:
        return True
    values = raw if isinstance(raw, list | tuple | set) else str(raw).split(",")
    ids: set[int] = set()
    names: set[str] = set()
    for value in values:
        text_value = str(value).strip()
        if not text_value:
            continue
        if not isinstance(value, bool) and text_value.lstrip("-").isdigit():
            ids.add(int(text_value))
        else:
            names.add(text_value.upper())
    if not ids and not names:
        return True
    if call.system_id is not None and call.system_id in ids:
        return True
    return bool(summary.system and summary.system.upper() in names)


def emergency_scope(rule: AlertRule) -> dict[str, set[Any]]:
    """What an ``emergency`` rule is narrowed to — every set empty means every emergency.

    ``pattern`` is a comma/space list where a number is an on-air tgid, a word is a
    talkgroup category (``law``, ``fire``, ``ems`` …, case folded) and a leading ``-``
    excludes either — ``law, fire, -1507`` is "law and fire talkgroups, but never 1507"
    (the radio whose emergency button got stuck, 2026-08-31).  ``filters`` may say the same
    as ``talkgroup_ids`` / ``exclude_talkgroup_ids`` / ``categories`` /
    ``exclude_categories``, and ``system`` as for every other kind.
    """
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    scope: dict[str, set[Any]] = {
        "tgids": _ids(filters.get("talkgroup_ids")),
        "exclude_tgids": _ids(filters.get("exclude_talkgroup_ids")),
        "categories": set(),
        "exclude_categories": set(),
    }
    for key in ("categories", "exclude_categories"):
        value = filters.get(key)
        items = value if isinstance(value, list | tuple | set) else str(value or "").replace(",", " ").split()
        scope[key] |= {str(item).strip().casefold() for item in items if str(item).strip()}
    for token in (rule.pattern or "").replace(",", " ").split():
        negate = token.startswith("-") and len(token) > 1
        word = token[1:] if negate else token
        if word.isdigit():
            scope["exclude_tgids" if negate else "tgids"].add(int(word))
        else:
            scope["exclude_categories" if negate else "categories"].add(word.casefold())
    return scope


def _emergency_matches(rule: AlertRule, call: Call, summary: CallSummary) -> bool:
    if not call.emergency or not _system_matches(rule, call, summary):
        return False
    scope = emergency_scope(rule)
    tgids = set(summary.talkgroup_ids)
    if summary.talkgroup_tgid is not None:
        tgids.add(summary.talkgroup_tgid)
    category = (summary.talkgroup_category or "").casefold()
    if tgids & scope["exclude_tgids"] or (category and category in scope["exclude_categories"]):
        return False
    if not scope["tgids"] and not scope["categories"]:
        return True
    return bool(tgids & scope["tgids"]) or bool(category and category in scope["categories"])


def matches(rule: AlertRule, call: Call, summary: CallSummary) -> bool:
    """Does this rule fire on this call?  Pure, so the tests can be exhaustive."""
    if rule.kind == "talkgroup":
        # Ids are on-air tgids — what the radio network calls them, and what
        # ``calls.talkgroup_ids`` holds — and nothing else.  They used to match the
        # ``talkgroups.id`` primary key as well, and the two number spaces overlap (dozens
        # of collisions on a real archive when the same bug was fixed in ``?talkgroup=``), so
        # a rule for one channel also fired on another.  Migration 0ebc81310604 rewrote the
        # stored pks.  A tgid two systems share is told apart by the rule's ``system``.
        wanted = _rule_ids(rule, "talkgroup_ids", "talkgroups", "tgids")
        if not wanted or not _system_matches(rule, call, summary):
            return False
        candidates = set(summary.talkgroup_ids)
        if summary.talkgroup_tgid is not None:
            candidates.add(summary.talkgroup_tgid)
        return bool(wanted & candidates)

    if rule.kind == "radio":
        # Ids are on-air radio numbers — what the radio sends and what a person types —
        # and nothing else.  ``calls.radio_id`` is a ``radios.id`` row id, a different
        # number space that overlaps it, so matching it as well made a rule for one radio
        # fire on whichever radio happened to be that row (the talkgroup fix above
        # applied to radios; migration 3216a949614d rewrote the stored row ids).  A number
        # two systems both use is told apart by the rule's ``system``.
        wanted = _rule_ids(rule, "radio_ids", "radios")
        if not wanted or summary.radio_number is None or not _system_matches(rule, call, summary):
            return False
        return summary.radio_number in wanted

    if rule.kind == "emergency":
        return _emergency_matches(rule, call, summary)

    text_ = summary.transcript or ""
    if not text_ or not rule.pattern:
        return False

    if rule.kind == "keyword":
        return rule.pattern.strip().casefold() in text_.casefold()

    if rule.kind == "regex":
        # `regex`, not `re`: it is the only engine in the stdlib's neighborhood that can
        # be interrupted.  A rule is written by an admin but runs against every transcript
        # for ever, and a catastrophically backtracking pattern would otherwise pin a
        # worker thread on one call.
        try:
            return regex.search(rule.pattern, text_, regex.IGNORECASE, timeout=REGEX_TIMEOUT_S) is not None
        except TimeoutError:
            log.warning("alert rule %s: regex timed out after %.2fs", rule.id, REGEX_TIMEOUT_S)
            raise
        except regex.error as exc:
            log.warning("alert rule %s has an invalid regex: %s", rule.id, exc)
            return False

    return False  # pragma: no cover - the enum has no other member


def _in_cooldown(rule: AlertRule, now: datetime) -> bool:
    if not rule.last_fired_at or rule.cooldown_seconds <= 0:
        return False
    return now - rule.last_fired_at < timedelta(seconds=rule.cooldown_seconds)


# --------------------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------------------


def _config(rule: AlertRule, channel: str) -> dict[str, Any]:
    config = rule.channel_config if isinstance(rule.channel_config, dict) else {}
    value = config.get(channel)
    return value if isinstance(value, dict) else {}


def deliver(
    rule: AlertRule,
    channel: str,
    summary: CallSummary,
    settings: Settings,
    client: httpx.Client,
) -> None:
    """Send one alert on one channel.  Raises on failure; the caller logs it."""
    config = _config(rule, channel)

    if channel == "webhook":
        url = config.get("url") or config.get("webhook_url")
        if not url:
            raise ValueError("webhook channel has no url")
        payload = {"rule": {"id": rule.id, "name": rule.name, "kind": rule.kind}, **summary.as_dict()}
        headers = {"Content-Type": "application/json"}
        headers.update(config.get("headers") or {})
        client.post(str(url), content=json.dumps(payload), headers=headers).raise_for_status()
        return

    if channel == "discord":
        url = config.get("webhook_url") or config.get("url") or settings.discord_webhook_url
        if not url:
            raise ValueError("discord channel has no webhook_url")
        embed = {
            "title": summary.title,
            "description": summary.message[:4000],
            "url": summary.url,
            "timestamp": summary.started_at.isoformat(),
            "fields": [
                {"name": "Rule", "value": rule.name, "inline": True},
                {"name": "Audio", "value": summary.audio_url, "inline": False},
            ],
        }
        client.post(str(url), json={"embeds": [embed]}).raise_for_status()
        return

    if channel == "ntfy":
        base = (config.get("url") or settings.ntfy_url or "").rstrip("/")
        topic = config.get("topic") or ""
        if not base or not topic:
            raise ValueError("ntfy channel needs a server url and a topic")
        headers = {
            "Title": summary.title,
            "Click": summary.url,
            "Tags": config.get("tags") or "radio",
            "Priority": str(config.get("priority") or 3),
            "Actions": f"view, Play audio, {summary.audio_url}",
        }
        client.post(f"{base}/{topic}", content=summary.message.encode(), headers=headers).raise_for_status()
        return

    if channel == "email":
        if not settings.smtp_host:
            raise ValueError("email channel needs SMTP_HOST")
        recipients = config.get("to") or config.get("recipients")
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        if not recipients:
            raise ValueError("email channel has no recipients")
        message = EmailMessage()
        message["Subject"] = f"[{rule.name}] {summary.title}"
        message["From"] = config.get("from") or settings.smtp_from
        message["To"] = ", ".join(recipients)
        message.set_content(f"{summary.message}\n\n{summary.url}\n{summary.audio_url}\n")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.alert_timeout_s) as smtp:
            if settings.smtp_user:
                smtp.starttls()
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        return

    raise ValueError(f"unknown alert channel {channel!r}")


# --------------------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------------------


def evaluate(
    session: Session,
    call_id: int,
    *,
    event: str = "call.new",
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> list[Notification]:
    """Run every rule this event can fire against one call; returns the log rows written."""
    # The channel fallbacks (the Discord webhook, the ntfy server, the SMTP relay) are
    # deployment settings a rule may leave unset, so they are read through the settings
    # table rather than from the environment the worker started with.
    settings = effective_settings(session, base=settings)
    now = now or datetime.now(UTC)
    kinds = KINDS_BY_EVENT.get(event)
    if not kinds or event in SELF_RAISED_EVENTS:
        return []

    rules = (
        session.execute(
            select(AlertRule)
            .where(AlertRule.enabled.is_(True), AlertRule.kind.in_(kinds))
            .order_by(AlertRule.id)
        )
        .scalars()
        .all()
    )
    if not rules:
        return []

    call = session.get(Call, call_id)
    summary = summarize(session, call_id, settings) if call is not None else None
    if call is None or summary is None:
        log.debug("alerts: call %s is gone, nothing to evaluate", call_id)
        return []

    owned = client is None
    http = client or httpx.Client(timeout=settings.alert_timeout_s)
    written: list[Notification] = []
    try:
        for rule in rules:
            try:
                if not matches(rule, call, summary):
                    continue
            except TimeoutError:
                # The rule's regex ran out of its budget on this transcript.  Skipping it
                # silently would make an alert that never fires indistinguishable from one
                # nobody triggered, so the skip is a row in the delivery log like any other.
                skipped = Notification(
                    rule_id=rule.id,
                    call_id=call.id,
                    channel=next(iter(rule.channels or ()), "none"),
                    severity=SKIPPED_SEVERITY,
                    subject=summary.title,
                    body=None,
                    ok=False,
                    error=f"rule skipped: its regex did not finish within {REGEX_TIMEOUT_S:.2f}s",
                )
                session.add(skipped)
                written.append(skipped)
                continue
            except Exception:  # noqa: BLE001 - a broken rule must not stop the others
                log.exception("alerts: rule %s failed to match", rule.id)
                continue
            if _in_cooldown(rule, now):
                log.debug("alerts: rule %s is in cooldown", rule.id)
                continue

            rule.last_fired_at = now
            for channel in rule.channels or ():
                notification = Notification(
                    rule_id=rule.id,
                    call_id=call.id,
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
                    # The message quotes the URL it could not reach, and for a Discord or
                    # ntfy endpoint that URL *is* the credential; the log is readable by
                    # anybody who can read the archive.
                    notification.error = scrub_secrets(f"{type(exc).__name__}: {exc}")[:2000]
                    log.warning("alerts: rule %s channel %s failed: %s", rule.id, channel, exc)
                session.add(notification)
                written.append(notification)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("alerts: evaluation of call %s failed", call_id)
        return []
    finally:
        if owned:
            http.close()
    return written
