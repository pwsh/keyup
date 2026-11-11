"""Outbound webhooks: telling one consumer what happened, without ever waiting on them.

The API answers "what was on the radio around this incident" *when asked*.  A CAD
system's actual shape is the other way round — a call for service is written, and the
radio traffic for it arrives over the following minutes — so without this the consumer's
only options are to poll ``/calls`` on a timer or to hold a WebSocket
open against ``/api/live``.  Polling is a query a minute for the times when nothing
happened; the socket means the consumer's own uptime decides whether it hears about a call
at all.  A webhook is the third answer: keyup remembers what it owes and keeps trying.

The design is one page of it:

**Off unless somebody turns it on.**  ``webhook_url`` blank (the shipped default) means
nothing is enqueued, the table stays empty and the worker's tick returns immediately.
``webhook_events`` empty means the same even with a URL — half a filled-in form is not
consent.  Both are runtime settings, so a consumer's receiver can be switched off during
their maintenance window without a keyup deploy.

**Enqueue in the caller's transaction, deliver somewhere else.**  :func:`enqueue` inserts
a row in the same session as the work that caused the event, exactly as
:func:`app.activity.record` does: an ingest that rolls back leaves no delivery behind, and
a stored call always has its event.  What it does *not* do is make an HTTP request —
putting somebody else's server in the critical path of storing a recording is precisely
the failure this archive must not have.

**The payload is frozen at enqueue.**  It is built once, from the rows the event is about,
and stored as it will be sent.  A retry three hours later re-sends what was true when the
event happened rather than what is true now, which is what an event log means by an event;
it also means a delivery survives the call being deleted by retention.

**Never raises.**  Every entry point here swallows its own failures into the log, because
losing a webhook must never fail an ingest, a transcription or an API request.

Signing: ``X-Keyup-Signature: sha256=<hex>`` is HMAC-SHA256 of the exact bytes of the
request body under ``webhook_secret``, so a receiver can prove the call came from this
deployment rather than from whoever found the URL.  A blank secret sends no header at all
rather than a signature under the empty key — a signature nobody can trust is worse than
an honest absence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.alerts import scrub_secrets
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import WEBHOOK_EVENTS, Call, ExternalLink, Radio, Talkgroup, WebhookDelivery
from app.runtime_settings import effective_settings

log = logging.getLogger("app.webhooks")

#: ``30 s × 2**attempts``, capped — the same curve as the export ledger and the
#: transcription worker, so the backoff an operator already knows is the one they get.
BACKOFF_BASE_S = 30
MAX_BACKOFF_S = 3600

#: The header naming the event, so a receiver can route without parsing the body.
EVENT_HEADER = "X-Keyup-Event"
#: The HMAC header.  ``sha256=`` prefixed, GitHub-style, so the algorithm is on the wire
#: and a future second one does not need a second header.
SIGNATURE_HEADER = "X-Keyup-Signature"
#: The delivery's own id, so a receiver can dedupe without reading the body either.
DELIVERY_HEADER = "X-Keyup-Delivery"


def backoff_seconds(attempts: int) -> float:
    """``30 s × 2**attempts``, capped at :data:`MAX_BACKOFF_S`."""
    return float(min(MAX_BACKOFF_S, BACKOFF_BASE_S * (2 ** max(0, attempts))))


#: Event names already complained about, so a misconfigured subscription is one log line
#: rather than one per ingested call — this is read on the hot path, four thousand times a
#: day, and a warning that repeats that often is a warning nobody reads.
_warned_unknown: set[str] = set()


def subscribed_events(settings: Settings) -> frozenset[str]:
    """The events this deployment is actually sending, honouring the URL switch.

    A name that is not one of :data:`app.models.WEBHOOK_EVENTS` is dropped with a warning
    rather than failing the read: one typo in a comma-separated field should not silence
    the other three events.
    """
    if not str(getattr(settings, "webhook_url", "") or "").strip():
        return frozenset()
    wanted = [
        name.strip() for name in str(getattr(settings, "webhook_events", "") or "").split(",")
    ]
    known = {name for name in wanted if name in WEBHOOK_EVENTS}
    unknown = {name for name in wanted if name and name not in WEBHOOK_EVENTS} - _warned_unknown
    if unknown:
        _warned_unknown.update(unknown)
        log.warning(
            "webhooks: ignoring unknown event name(s) %s — known names are %s",
            ", ".join(sorted(unknown)),
            ", ".join(WEBHOOK_EVENTS),
        )
    return frozenset(known)


#: What ``webhook_call_scope`` may be.  ``all`` is the shipped default and today's
#: behaviour; the others exist because a deployment replaying an archive sends a consumer
#: hundreds of thousands of ``call.transcribed`` events a day about calls from 2016.
CALL_SCOPES = ("all", "live", "linked", "live+linked")

#: Scope values already complained about — the same one-line-per-mistake rule as
#: :data:`_warned_unknown`, and for the same reason: this is the ingest hot path.
_warned_scopes: set[str] = set()


def call_scope(settings: Settings) -> str:
    """``webhook_call_scope``, normalized.  An unrecognized value reads as ``all``.

    Deliberately permissive: the Admin form validates this against
    :data:`app.runtime_settings.SETTINGS_OPTIONS` before it is ever stored, so a value that
    gets here anyway came from a hand-edited row or an older deployment — and the safe
    reading of "I do not understand this filter" is to send the events, not to go quiet in
    a way nobody would notice until the consumer asked where their data went.
    """
    value = str(getattr(settings, "webhook_call_scope", "") or "all").strip().lower()
    if value in CALL_SCOPES:
        return value
    if value not in _warned_scopes:
        _warned_scopes.add(value)
        log.warning(
            "webhooks: unknown webhook_call_scope %r — sending every call; known values are %s",
            value,
            ", ".join(CALL_SCOPES),
        )
    return "all"


def _has_link(session: Session, call_id: int) -> bool:
    """Does this call carry an external link?  One indexed existence check, no rows read."""
    return (
        session.execute(
            select(ExternalLink.id).where(ExternalLink.call_id == call_id).limit(1)
        ).first()
        is not None
    )


def call_in_scope(
    session: Session, *, call_id: int, priority: str | None, scope: str
) -> bool:
    """Whether one call passes ``webhook_call_scope``.

    ``priority`` comes from the payload that has already been built, so ``live`` costs no
    query at all; ``linked`` is the only branch that reaches the database, and
    ``live+linked`` short-circuits on the free half first.
    """
    if scope == "all":
        return True
    if scope == "live":
        return priority == "live"
    if scope == "live+linked" and priority == "live":
        return True
    return _has_link(session, call_id)


def sign(body: bytes, secret: str) -> str | None:
    """``sha256=<hex>`` over ``body``, or ``None`` when there is no secret to sign with."""
    if not secret:
        return None
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------------------
# payloads
# --------------------------------------------------------------------------------------


def _call_payload(session: Session, call_id: int, settings: Settings) -> dict[str, Any] | None:
    """One call as a consumer wants it: enough to act on, without a second request.

    The fields are the ones a consumer matches on (time, talkgroup, agency, unit, text)
    plus the two links, because a consumer's next move after "a call was stored" is either
    to match it against an incident or to play it.  ``ingest_priority`` is here for the same reason it is on
    ``CallOut``: a ``backlog`` event is an archive migration replaying 2016, not news.
    """
    row = session.execute(
        select(Call, Talkgroup, Radio)
        .outerjoin(Talkgroup, Call.talkgroup_id == Talkgroup.id)
        .outerjoin(Radio, Call.radio_id == Radio.id)
        .where(Call.id == call_id)
    ).first()
    if row is None:
        return None
    call, talkgroup, radio = row
    base = str(settings.public_base_url).rstrip("/")
    return {
        "id": call.id,
        "system": call.system.name if call.system is not None else None,
        "site": call.site.name if call.site is not None else None,
        "tgid": talkgroup.tgid if talkgroup is not None else None,
        "talkgroup_id": call.talkgroup_id,
        "talkgroup_alias": (talkgroup.alias if talkgroup is not None else None)
        or call.talkgroup_alias_at_record,
        "talkgroup_ids": list(call.talkgroup_ids or ()),
        "category": talkgroup.category if talkgroup is not None else None,
        "service_tag": talkgroup.service_tag if talkgroup is not None else None,
        "agency_code": talkgroup.agency_code if talkgroup is not None else None,
        "radio_id": call.radio_id,
        "unit_id": radio.unit_id if radio is not None else None,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "started_local": call.started_local.isoformat() if call.started_local else None,
        "duration_s": float(call.duration_s or 0),
        "call_type": call.call_type,
        "emergency": call.emergency,
        "encrypted": call.encrypted,
        # What the audio *sounds* like, alongside what the radio asserted (§5.5).  A
        # consumer matching a fire page to an incident wants `alert`, not `emergency`.
        "alert": call.alert,
        "tone_detected": call.tone_detected,
        "ingest_source": call.ingest_source,
        "ingest_priority": call.ingest_priority,
        "transcript_status": call.transcript_status,
        "transcript_text": call.transcript_text,
        "transcript_quality": call.transcript_quality,
        "url": f"{base}/calls/{call.id}",
        "audio_url": f"{base}/api/calls/{call.id}/audio",
    }


def _link_payload(link: ExternalLink, settings: Settings) -> dict[str, Any]:
    """One external link — the record that somebody confirmed a call belongs to an incident."""
    base = str(settings.public_base_url).rstrip("/")
    return {
        "id": link.id,
        "source": link.source,
        "external_id": link.external_id,
        "call_id": link.call_id,
        "conversation_id": link.conversation_id,
        "link_confidence": link.link_confidence,
        "link_method": link.link_method,
        "link_note": link.link_note,
        "created_by": link.created_by,
        "url": f"{base}/calls/{link.call_id}" if link.call_id is not None else None,
    }


# --------------------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------------------


def enqueue(
    session: Session,
    *,
    event: str,
    subject_id: int | None,
    body: dict[str, Any],
    settings: Settings | None = None,
) -> WebhookDelivery | None:
    """Queue one delivery in the caller's transaction, or do nothing.  Never raises.

    ``None`` back means "nothing to send", which is the normal case: the feature is off,
    or this event is not subscribed.  It does **not** commit — the row lands with the work
    that caused it, so a rolled-back ingest leaves no promise behind.

    ``settings`` is for a caller that has already resolved them (the helpers below, the
    worker, a test).  Left out, this reads :func:`app.runtime_settings.effective_settings`
    with no base, which is the memoized merge and therefore free once warm — which matters,
    because this is called once per ingested recording whether or not anybody is listening.

    "Never raises" is a SAVEPOINT here and in the two helpers below, not only a
    ``try``: on PostgreSQL a failed statement aborts the caller's whole transaction, and
    the ingest's COMMIT then rolls the call back without raising.  Rolling back to
    the savepoint loses only the webhook.
    """
    try:
        with session.begin_nested():
            resolved = settings or effective_settings(session)
            if event not in subscribed_events(resolved):
                return None
            row = WebhookDelivery(
                event=event,
                subject_id=subject_id,
                payload={
                    "event": event,
                    "at": datetime.now(UTC).isoformat(),
                    **body,
                },
                next_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()  # assign the id without ending the caller's transaction
            return row
    except Exception:  # noqa: BLE001 - a webhook is never worth a failed ingest
        log.warning("webhooks: could not queue %s for %s", event, subject_id, exc_info=True)
        return None


def enqueue_call_event(
    session: Session, event: str, call_id: int, *, settings: Settings | None = None
) -> WebhookDelivery | None:
    """``call.stored`` / ``call.transcribed`` for one call.

    The subscription is checked *before* the call is loaded, so a deployment with webhooks
    off — the default — pays one cached settings read on the ingest path and no query.

    ``webhook_call_scope`` is checked *after*, because the field it filters on
    (``ingest_priority``) is one the payload has already loaded: asking the question first
    would mean a second query for every call to answer what the row in hand already says.
    Only these two call events are scoped; :func:`enqueue_link_event` is untouched.
    """
    try:
        with session.begin_nested():
            resolved = settings or effective_settings(session)
            if event not in subscribed_events(resolved):
                return None
            payload = _call_payload(session, call_id, resolved)
            if payload is None:  # pragma: no cover - the caller just wrote this row
                return None
            if not call_in_scope(
                session,
                call_id=call_id,
                priority=payload.get("ingest_priority"),
                scope=call_scope(resolved),
            ):
                return None
            return enqueue(
                session, event=event, subject_id=call_id, body={"call": payload}, settings=resolved
            )
    except Exception:  # noqa: BLE001
        log.warning("webhooks: could not queue %s for call %s", event, call_id, exc_info=True)
        return None


def enqueue_link_event(
    session: Session, event: str, link: ExternalLink, *, settings: Settings | None = None
) -> WebhookDelivery | None:
    """``link.created`` / ``link.deleted`` for one external link.

    The link row is passed in rather than looked up because ``link.deleted`` fires on a row
    that is about to stop existing.
    """
    try:
        with session.begin_nested():
            resolved = settings or effective_settings(session)
            if event not in subscribed_events(resolved):
                return None
            return enqueue(
                session,
                event=event,
                subject_id=link.call_id,
                body={"link": _link_payload(link, resolved)},
                settings=resolved,
            )
    except Exception:  # noqa: BLE001
        log.warning("webhooks: could not queue %s", event, exc_info=True)
        return None


# --------------------------------------------------------------------------------------
# the delivery loop
# --------------------------------------------------------------------------------------


def claim(session: Session) -> WebhookDelivery | None:
    """Take the next due ``pending`` delivery, or ``None``.

    ``FOR UPDATE SKIP LOCKED``, held until the caller's transaction ends — the same
    pattern as ``transcription_jobs`` and ``call_exports``, and for the same reason: it is
    the only way two worker processes can share a queue without both being handed the same
    row and POSTing it twice.
    """
    row = session.execute(
        text(
            """
            SELECT id
            FROM webhook_deliveries
            WHERE status = 'pending' AND next_at <= now()
            ORDER BY next_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
    ).first()
    if row is None:
        session.rollback()
        return None
    return session.get(WebhookDelivery, row.id)


def mark_delivered(session: Session, delivery: WebhookDelivery, *, status_code: int) -> None:
    """A 2xx.  Terminal: nothing re-reads a ``delivered`` row."""
    now = datetime.now(UTC)
    session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery.id)
        .values(
            status="delivered",
            attempts=delivery.attempts + 1,
            response_status=status_code,
            last_error=None,
            delivered_at=now,
            updated_at=now,
        )
    )


def mark_failed(
    session: Session,
    delivery: WebhookDelivery,
    *,
    error: str,
    status_code: int | None,
    max_attempts: int,
) -> bool:
    """Count the attempt and either schedule a retry or park it.  ``True`` = parked.

    The error is stored with every URL scrubbed: an httpx failure quotes the URL it could
    not reach, that URL can carry a token in its path, and this column is read back by
    anybody who can see the archive.
    """
    used = delivery.attempts + 1
    parked = used >= max(1, max_attempts)
    values: dict[str, Any] = {
        "attempts": used,
        "response_status": status_code,
        "last_error": (scrub_secrets(error) or "")[:2000],
        "updated_at": datetime.now(UTC),
    }
    if parked:
        values["status"] = "failed"
    else:
        values["next_at"] = datetime.now(UTC) + timedelta(seconds=backoff_seconds(used - 1))
    session.execute(
        update(WebhookDelivery).where(WebhookDelivery.id == delivery.id).values(**values)
    )
    return parked


def requeue(session: Session, delivery_id: int) -> bool:
    """Put a parked delivery back at the front of the queue.  ``True`` when one moved.

    The attempt count is reset with it: a retry an operator asked for is a fresh start,
    not the sixth of five, and leaving the count would park it again on the next failure.
    """
    result = session.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status != "delivered")
        .values(
            status="pending",
            attempts=0,
            next_at=datetime.now(UTC),
            last_error=None,
            updated_at=datetime.now(UTC),
        )
    )
    return bool(result.rowcount)


def summary(session: Session) -> dict[str, int]:
    """``{status: count}`` over the whole queue — the observability read."""
    return {
        str(status): int(count)
        for status, count in session.execute(
            select(WebhookDelivery.status, func.count()).group_by(WebhookDelivery.status)
        )
    }


class WebhookWorker:
    """Owns one delivery cycle.  Construct with a session factory; call :meth:`tick`.

    Deliberately serial — one delivery in flight at a time, process-wide.  The receiver is
    somebody else's server, the ordering of ``call.stored`` before ``call.transcribed`` for
    the same call is worth keeping, and ten sockets against a small consumer is how a
    helpful integration becomes a denial of service.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: Callable[[], Session] = SessionLocal,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self._client = client

    def runtime(self, session: Session) -> Settings:
        return effective_settings(session, base=self.settings)

    def tick(self) -> dict[str, int]:
        """Send up to ``webhook_batch_size`` due deliveries.  Returns counts; never raises."""
        counts = {"delivered": 0, "failed": 0}
        session = self.session_factory()
        try:
            try:
                settings = self.runtime(session)
            except Exception:  # noqa: BLE001 - the database is somebody else's problem
                log.exception("webhooks: could not read the settings table; skipping this tick")
                session.rollback()
                return counts
            url = str(getattr(settings, "webhook_url", "") or "").strip()
            if not url or not subscribed_events(settings):
                return counts

            batch = max(1, int(getattr(settings, "webhook_batch_size", 20) or 20))
            client = self._client or httpx.Client(
                timeout=float(getattr(settings, "webhook_timeout_s", 10.0) or 10.0)
            )
            try:
                for _ in range(batch):
                    delivery = claim(session)
                    if delivery is None:
                        break
                    ok = self._send(session, client, delivery, settings=settings, url=url)
                    counts["delivered" if ok else "failed"] += 1
            finally:
                if self._client is None:
                    client.close()
        except Exception:  # noqa: BLE001 - the loop outlives any one tick
            log.exception("webhooks: tick failed")
            session.rollback()
        finally:
            session.close()
        return counts

    def _send(
        self,
        session: Session,
        client: httpx.Client,
        delivery: WebhookDelivery,
        *,
        settings: Settings,
        url: str,
    ) -> bool:
        """POST one claimed delivery and record the outcome.  Commits its own work.

        No rollback on the failure path: the claim holds a ``FOR UPDATE`` lock on the row,
        and releasing it before the attempt has been counted would let a second worker take
        the same delivery mid-flight.  The request raised from httpx, not the database, so
        the session is clean and the outcome is written inside the claim's transaction —
        exactly as :mod:`app.exporters.worker` does it.
        """
        max_attempts = max(1, int(getattr(settings, "webhook_max_attempts", 5) or 5))
        body = json.dumps(
            {"delivery_id": delivery.id, **(delivery.payload or {})}, default=str
        ).encode()
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: delivery.event,
            DELIVERY_HEADER: str(delivery.id),
            "User-Agent": "keyup-webhooks/1",
        }
        signature = sign(body, str(getattr(settings, "webhook_secret", "") or ""))
        if signature is not None:
            headers[SIGNATURE_HEADER] = signature

        try:
            response = client.post(url, content=body, headers=headers)
        except Exception as exc:  # noqa: BLE001 - any transport failure is just a failure
            mark_failed(
                session, delivery, error=str(exc), status_code=None, max_attempts=max_attempts
            )
            session.commit()
            return False

        if 200 <= response.status_code < 300:
            mark_delivered(session, delivery, status_code=response.status_code)
            session.commit()
            return True

        # A 4xx that is not 408/429 is the receiver saying "not this, ever": retrying a
        # rejected body four more times only fills their log and ours.  Park it now by
        # capping the allowance at the attempt just made.
        permanent = 400 <= response.status_code < 500 and response.status_code not in (408, 429)
        mark_failed(
            session,
            delivery,
            error=f"HTTP {response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
            max_attempts=delivery.attempts + 1 if permanent else max_attempts,
        )
        session.commit()
        return False

    def run(self, stop: threading.Event) -> None:  # pragma: no cover - the worker's loop
        """Tick until ``stop`` is set.  Only used where no scheduler is available."""
        while not stop.is_set():
            self.tick()
            stop.wait(max(1.0, float(getattr(self.settings, "webhook_poll_seconds", 5.0) or 5.0)))


#: How long a *succeeded* delivery is kept.  It is a receipt, not a record: the consumer
#: has the event, and the only question anybody asks of this table afterwards is "did it
#: go out", which a week answers.  ``failed`` and ``pending`` rows are never pruned by age
#: — a parked delivery is precisely the one an operator comes back to retry — so the table
#: stays bounded by what succeeded and by nothing else.  Not a setting: a knob whose only
#: effect is how much of a receipt log survives is a knob nobody would tune correctly.
DELIVERED_KEEP_DAYS = 7


def prune(session: Session, *, keep_days: int = DELIVERED_KEEP_DAYS) -> int:
    """Delete ``delivered`` rows older than ``keep_days``; return how many went.

    ``keep_days <= 0`` keeps them for ever.  Commits its own work — it is a maintenance
    pass, not part of anybody's transaction.
    """
    from sqlalchemy import delete  # noqa: PLC0415 - only this function needs it

    if keep_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    result = session.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.status == "delivered", WebhookDelivery.created_at < cutoff
        )
    )
    session.commit()
    return int(result.rowcount or 0)


def recent(
    session: Session,
    *,
    limit: int = 50,
    status: str | None = None,
    event: str | None = None,
) -> Sequence[WebhookDelivery]:
    """The newest deliveries, newest first — what the admin list reads."""
    stmt = select(WebhookDelivery)
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    if event is not None:
        stmt = stmt.where(WebhookDelivery.event == event)
    return list(session.scalars(stmt.order_by(WebhookDelivery.id.desc()).limit(max(1, limit))))


__all__ = [
    "BACKOFF_BASE_S",
    "CALL_SCOPES",
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "MAX_BACKOFF_S",
    "SIGNATURE_HEADER",
    "WebhookWorker",
    "backoff_seconds",
    "call_in_scope",
    "call_scope",
    "claim",
    "enqueue",
    "enqueue_call_event",
    "enqueue_link_event",
    "DELIVERED_KEEP_DAYS",
    "mark_delivered",
    "mark_failed",
    "prune",
    "recent",
    "requeue",
    "sign",
    "subscribed_events",
    "summary",
]
