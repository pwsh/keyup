"""``call_exports`` as a queue: discovery, claiming, and the three ways a row finishes.

The exporter worker never walks ``calls`` looking for work twice.  :func:`discover` turns
eligible calls into ledger rows once — with ``ON CONFLICT (call_id, service) DO NOTHING``,
so two workers, a restart mid-cycle, and a re-run over an overlapping window all converge
on exactly one row per (call, service) — and everything after that is a queue over this
table.  That is the whole dedupe story: **a call cannot be uploaded twice because it cannot
have two rows.**

A row is born ``pending`` or ``skipped``.  ``skipped`` is not a failure; it is the record
that we deliberately did not publish this call (encrypted, no audio, no talkgroup), written
so the same call is not re-examined every cycle for ever.

:func:`claim` takes one due ``pending`` row with ``FOR UPDATE SKIP LOCKED`` — the same
pattern as ``transcription_jobs`` (``app/stt/worker.py``) and for the same reason: it is the
only way two workers can share a queue without a chance of handing both the same row.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.orm import Session, aliased

from app.audio import NO_CONTENT, PRESENT_STATES
from app.config import Settings
from app.models import Call, CallExport

from .gate import (
    content_clause,
    is_unit_call,
    live_band_clause,
    selection_clause,
    systems_clause,
    trunked_clause,
    unit_calls_clause,
)

log = logging.getLogger("app.exporters.ledger")

__all__ = [
    "AMBIGUOUS_PREFIX",
    "BACKOFF_BASE_S",
    "DUPLICATE_UPLOAD_REASON",
    "EXPORTED_STATUSES",
    "NO_SPEECH_REASON",
    "STATIC_REASON",
    "MAX_BACKOFF_S",
    "UNCERTAIN_REASON",
    "VERIFY_DELAY_S",
    "Claimed",
    "backoff_seconds",
    "bump_attempt",
    "claim",
    "discover",
    "duplicate_upload_reason",
    "mark_failed",
    "mark_skipped",
    "mark_uploaded",
    "no_speech_reason",
    "recent",
    "repoint_exports",
    "requeue",
    "skip_reason_for",
    "summary",
    "window_summary",
]

#: ``30 s * 2**(attempts - 1)``: the first retry lands 30 s after the first failure, then
#: 60 s, 120 s, 240 s…  Same constant as the transcription worker (``app/stt/worker.py``),
#: so the curve an operator already knows is the curve they get here.
BACKOFF_BASE_S = 30
#: …but capped: somebody else's service being down for a day should not push the next try
#: a week out.
MAX_BACKOFF_S = 3600

#: ``last_error`` of a row whose last attempt sent the whole request and heard nothing back
#: (:class:`~app.exporters.base.AmbiguousExportError`).  It is a marker as well as prose:
#: :attr:`Claimed.verify_first` reads it, and the worker then asks the service whether it
#: already has the call before sending it again.  Kept on every later failure of the same
#: row, because once one attempt might have landed, every re-post has to ask first.
AMBIGUOUS_PREFIX = "ambiguous:"

#: The skip reason for a row that may have been published and could not be checked — an
#: ambiguous attempt on a client with no way to look the call up.  Never re-posted by the
#: worker; the operator's requeue is the only way back into the queue (and a requeued row
#: keeps its ``last_error``, so it is still checked first when the client can).
UNCERTAIN_REASON = "uncertain"

#: How long after an ambiguous attempt the lookup runs.  OpenMHz answers ``200`` before
#: its S3 put and Mongo save, so a call it did take is not listed for a few seconds; asking
#: at once would find nothing and re-post exactly the duplicate the lookup exists to stop.
VERIFY_DELAY_S = 60.0


#: One transmission, two recorders, one upload.  Reconciliation can demote a copy that has
#: already been published (:func:`~app.ingest.reconcile._swap` makes the newcomer primary),
#: and the promoted copy is a *different row* with no ledger row of its own — so discovery
#: would find it, and the archive would get the same transmission twice.  Both sides of that
#: race write this reason: :func:`discover` when it finds a primary whose duplicate group is
#: already spoken for, and the swap itself when it demotes a copy that was uploaded.  The
#: id of the copy that carries the upload is appended, so the ledger says *which* row the
#: audio went out on; the constant is the prefix the status endpoint counts on
#: (``duplicate_uploads_24h``), exactly as ``DUPLICATE_REASON`` is for Broadcastify's own
#: "already received" answer.  The two are different facts and are counted separately: that
#: one is somebody else publishing first, this one is us not publishing twice.
DUPLICATE_UPLOAD_REASON = "already exported as call"

#: The statuses of a sibling copy's row that mean this transmission is already accounted
#: for on this service: ``uploaded`` (it went out), ``pending`` (it is in the queue) and
#: ``failed`` (it was attempted to exhaustion — a second copy is not a retry, and re-queuing
#: is the operator's decision).  ``skipped`` is deliberately absent: a copy refused for
#: being encrypted or audio-less says nothing about the copy that replaced it.
EXPORTED_STATUSES = ("uploaded", "pending", "failed")

#: Rows per multi-row INSERT in :func:`discover`.  Five bind parameters a row, so a chunk is
#: 2 500 parameters — a pass at the default ``limit`` is one statement, and a caller that
#: asks for more stays far under PostgreSQL's 65 535-parameter ceiling.
INSERT_CHUNK = 500

#: Nobody said anything.  A mic key-up with no words behind it is a real transmission and
#: the archive keeps it — the radio did key up on that talkgroup — but publishing it wastes
#: a slot on both services and gives a listener half a second of a click.  The audio pass
#: decides (``audio_meta.content``, :func:`app.audioproc.process.speech_seconds`); this is
#: the prefix the ledger and the status endpoint count on, exactly as
#: :data:`DUPLICATE_UPLOAD_REASON` is.  The **live Feed** does not use it: a feed is the
#: channel as it sounded, key-ups included.
NO_SPEECH_REASON = "no speech"

#: Nobody said anything *and* the channel was noise: broadband static across the whole
#: vocoder band, a squelch tail or a failed decode (``audio_meta.content = "static"``,
#: :func:`app.audioproc.process.flat_fraction`).  Refused for the same reason and by the
#: same gate as :data:`NO_SPEECH_REASON` — it is named apart so a ledger row says which of
#: the two it was, because a mic key-up and a radio delivering noise are different faults.
STATIC_REASON = "static"


def no_speech_reason(meta: dict[str, Any] | None) -> str:
    """Why this call carries nothing, with the measurement that produced the verdict.

    :data:`STATIC_REASON` and the flat-frame share for a ``static`` call,
    :data:`NO_SPEECH_REASON` and the seconds above the floor for a ``none`` one.
    """
    meta = meta or {}
    if meta.get("content") == "static":
        flat = meta.get("flat_frac")
        if flat is None:
            return STATIC_REASON
        return f"{STATIC_REASON} ({float(flat):.2f} of frames flat)"
    speech = meta.get("speech_s")
    floor = meta.get("speech_floor_db")
    if speech is None or floor is None:
        return NO_SPEECH_REASON
    return f"{NO_SPEECH_REASON} ({float(speech):.2f} s above {float(floor):g} dB)"


def duplicate_upload_reason(call_id: int) -> str:
    """:data:`DUPLICATE_UPLOAD_REASON` naming the copy that carries the upload."""
    return f"{DUPLICATE_UPLOAD_REASON} {call_id}"


def backoff_seconds(attempts: int) -> float:
    """``30 s × 2**attempts``, capped at :data:`MAX_BACKOFF_S`.

    :func:`mark_failed` passes the count of attempts made *before* this one, so the first
    failure waits ``backoff_seconds(0)`` = 30 s.
    """
    return float(min(MAX_BACKOFF_S, BACKOFF_BASE_S * (2 ** max(0, attempts))))


def skip_reason_for(call: Any, settings: Any | None = None, *, no_speech: bool = True) -> str | None:
    """Why this call must never be uploaded, or ``None`` when it may be.

    Four refusals, all of them about the recording rather than about us:

    ``encrypted``
        there is nothing intelligible in it and both services would be published a minute
        of digital noise;
    ``no talkgroup``
        neither service has anywhere to put a call that is not on a talkgroup — OpenMHz
        keys its archive on ``talkgroup_num`` and Broadcastify Calls on ``tg``.  A
        **unit-to-unit call** is exempt: it has no talkgroup *by construction* rather than
        by accident, which is a different fact with a different answer
        (:data:`~app.exporters.gate.UNIT_CALL`, decided per service by
        :func:`~app.exporters.gate.excluded_reason`) — and the live Feed publishes audio
        with no talkgroup attached to it at all, so for the Feed it is not a refusal;
    ``no audio``
        retention purged the MP3, or it never landed;
    :data:`NO_SPEECH_REASON`
        the audio pass measured no speech in it at all — a mic key-up.  ``no_speech=False``
        turns this one off, which is what the **live Feed** passes: a feed is the channel as
        it sounded, and a key-up is part of that.  It is asked last because it is the only
        one that depends on a pass having *run*: a call whose audio job has not landed yet
        carries no ``content`` and is exported exactly as before.

    ``encrypted`` is the one an operator can overrule: ``export_include_encrypted`` (off)
    lets an encrypted call through for a consumer that genuinely wants it.  The other two
    are facts about the row and no setting softens them.
    """
    if getattr(call, "encrypted", None) is True and not (
        settings is not None and bool(getattr(settings, "export_include_encrypted", False))
    ):
        return "encrypted"
    if getattr(call, "talkgroup_id", None) is None and not is_unit_call(call):
        return "no talkgroup"
    state = getattr(call, "audio_state", "present") or "present"
    if state not in PRESENT_STATES:
        return "no audio"
    if not getattr(call, "file_path", None):
        return "no audio"
    meta = getattr(call, "audio_meta", None) or {}
    if no_speech and meta.get("content") in NO_CONTENT:
        return no_speech_reason(meta)
    return None


def discover(
    session: Session,
    *,
    service: str,
    watermark: datetime | None,
    settings: Settings,
    limit: int = 500,
) -> int:
    """Create ledger rows for calls this service may publish.  Returns rows created.

    A ``None`` watermark means the service is off and **nothing** is discovered — the first
    of the two guards, applied here so a disabled service cannot even accumulate a queue to
    be flushed the moment somebody turns it on.  The band filter
    (:func:`~app.exporters.gate.live_band_clause`) is the second, and
    ``EXPORT_MAX_AGE_MINUTES`` is a third, softer one: a service that was unreachable for
    an afternoon comes back to the last hour of traffic, not the whole afternoon.

    ``duplicate_of IS NULL`` keeps reconciliation's losing copy out: when two recorders
    caught the same transmission we publish the copy that won, once.

    That clause alone is not enough, because *which* copy won can change after the fact.
    Copy A lands, gets a ledger row and is uploaded three seconds later; copy B lands, wins
    the ruling, and A is demoted to point at B — leaving B a primary with no ledger row and
    the transmission already published.  ``exported_as`` is the second half of the guard: a
    correlated subquery for a demoted copy of this call (``duplicate_of = calls.id``) that
    already has a row for this service in one of :data:`EXPORTED_STATUSES`.  When it finds
    one the call still gets a row — a ``skipped`` one, reasoned
    :data:`DUPLICATE_UPLOAD_REASON` — because a row is what stops it being re-examined for
    ever, and the reason is what tells an operator why the gap in the archive is not a bug.
    """
    if watermark is None:
        return 0
    now = datetime.now(UTC)
    # In SQL rather than per row: discovery reads up to ``limit`` calls a cycle, every three
    # seconds, and this must not become a query per call.
    copy = aliased(Call)
    exported_as = (
        select(copy.id)
        .where(
            copy.duplicate_of == Call.id,
            select(CallExport.id)
            .where(
                CallExport.call_id == copy.id,
                CallExport.service == service,
                CallExport.status.in_(EXPORTED_STATUSES),
            )
            .exists(),
        )
        .order_by(copy.id)
        .limit(1)
        .correlate(Call)
        .scalar_subquery()
    )
    stmt = (
        select(Call, exported_as.label("exported_as"))
        .where(
            Call.started_at > watermark,
            Call.duplicate_of.is_(None),
            live_band_clause(),
            ~select(CallExport.id)
            .where(CallExport.call_id == Call.id, CallExport.service == service)
            .exists(),
        )
        .order_by(Call.started_at)
        .limit(max(1, limit))
    )
    max_age = int(getattr(settings, "export_max_age_minutes", 0) or 0)
    if max_age > 0:
        stmt = stmt.where(Call.started_at >= now - timedelta(minutes=max_age))
    # The operator's selection, as SQL.  Filtering here rather than writing a `skipped` row
    # per unwanted call keeps the ledger a record of what we *meant* to publish: a talkgroup
    # the operator never selected is not a call we decided against, it is one we were never
    # asked about, and re-selecting it later should not have to step over a skipped row.
    for clause in content_clause(settings):
        stmt = stmt.where(clause)
    selection = selection_clause(settings, service)
    if selection is not None:
        stmt = stmt.where(selection)
    # Private calls, for the services that never take one — the same "never asked about it"
    # filtering the selection above is, and for the same reason.
    private = unit_calls_clause(settings, service)
    if private is not None:
        stmt = stmt.where(private)
    systems = systems_clause(settings, service)
    if systems is not None:
        stmt = stmt.where(systems)
    # Broadcastify Calls takes trunked systems only, and that is a rule rather than a
    # selection: a conventional system never gets a ledger row for it.
    trunked = trunked_clause(service)
    if trunked is not None:
        stmt = stmt.where(trunked)

    # ``.all()`` rather than streaming the cursor: the INSERT below runs on this same
    # connection, and an open server-side cursor is not something to interleave writes with.
    rows: list[dict[str, Any]] = []
    for call, exported_as_id in session.execute(stmt).all():
        # The duplicate answer is checked first: "already exported as call 41" is the
        # reason this row exists at all, and it is the one an operator is looking for.
        reason = (
            duplicate_upload_reason(int(exported_as_id))
            if exported_as_id is not None
            else skip_reason_for(call, settings)
        )
        rows.append(
            {
                "call_id": call.id,
                "service": service,
                "status": "skipped" if reason else "pending",
                "skip_reason": reason,
                "next_attempt_at": now,
            }
        )
    created = 0
    # One multi-row INSERT per chunk rather than one round trip per call (up to 500 a
    # pass).  ON CONFLICT DO NOTHING is what makes a second worker (or a re-run over an
    # overlapping window) harmless: the loser writes nothing rather than a duplicate.
    # RETURNING rather than rowcount: a suppressed conflict returns no row, so the rows
    # that come back are exactly the ones this pass created.  The rows go in in the order
    # the calls were read, as the one-at-a-time loop did, so two workers racing over the
    # same window take the unique-index entries in the same order.
    for start in range(0, len(rows), INSERT_CHUNK):
        inserted = session.execute(
            insert(CallExport)
            .values(rows[start : start + INSERT_CHUNK])
            .on_conflict_do_nothing(constraint="uq_call_exports_call_service")
            .returning(CallExport.id)
        ).all()
        created += len(inserted)
    session.commit()
    return created


def repoint_exports(session: Session, *, winner_id: int, loser_id: int) -> None:
    """Move the demoted copy's ledger rows to the copy that just became primary.

    Called from the swap (:func:`~app.ingest.reconcile._swap` and the batch pass's demotion
    of a row that was a primary), where one transmission has just changed which row
    represents it.  Per service, the loser's row decides:

    ``uploaded``
        the audio is already in their archive and nothing here can un-send it — the row
        stays where it is, on the copy that was actually published, and the *winner* gets a
        ``skipped`` row reasoned :data:`DUPLICATE_UPLOAD_REASON` so discovery never picks
        it up.  A recorder-direct Broadcastify upload is this case too: the recorder
        registered and PUT the call itself and the row carries its ``uploaded_by``.
    ``pending``
        nothing has been sent yet, and the winner is by definition the better recording —
        so the row itself moves (``call_id = winner``), keeping its attempt count and its
        place in the queue.  ``UNIQUE (call_id, service)`` makes that a safe write; when
        the winner already has a row for the service the move would collide, so the
        loser's row is skipped instead — either way exactly one row can be uploaded.
    ``failed`` / ``skipped``
        left alone.  Both are decisions already taken about a specific recording, and a
        change of primary is not new information about them.
    """
    # Locked, and re-read once the lock is ours.  The worker holds a claimed row's lock
    # across the whole upload and marks it ``uploaded`` on commit; a plain read here would
    # see ``pending``, wait at the UPDATE below, and then move (or skip) a row that had
    # just been published — so the winner would be offered to the service a second time.
    # ``populate_existing`` matters as much as the lock: the session may already hold
    # these objects from an earlier read, and without it the stale status would be kept.
    rows = list(
        session.scalars(
            select(CallExport)
            .where(CallExport.call_id == loser_id)
            .order_by(CallExport.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    rows.sort(key=lambda row: str(row.service))
    if not rows:
        return
    taken = {
        str(service)
        for service in session.scalars(
            select(CallExport.service).where(CallExport.call_id == winner_id)
        )
    }
    now = datetime.now(UTC)
    for row in rows:
        service = str(row.service)
        if row.status == "uploaded":
            if service in taken:
                continue
            session.execute(
                insert(CallExport)
                .values(
                    call_id=winner_id,
                    service=service,
                    status="skipped",
                    skip_reason=duplicate_upload_reason(loser_id),
                    next_attempt_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_call_exports_call_service")
            )
            taken.add(service)
        elif row.status == "pending":
            if service in taken:
                row.status = "skipped"
                row.skip_reason = duplicate_upload_reason(winner_id)
            else:
                row.call_id = winner_id
                taken.add(service)
            row.updated_at = now
    session.flush()


@dataclass(frozen=True, slots=True)
class Claimed:
    """One ledger row and the call behind it, read together so the worker needs no join."""

    export_id: int
    call_id: int
    attempts: int
    call: Call
    #: the row's ``last_error`` as it was when claimed — read for :attr:`verify_first`
    last_error: str | None = None

    @property
    def verify_first(self) -> bool:
        """Did an earlier attempt end ambiguously?  Then ask the service before re-posting."""
        return (self.last_error or "").startswith(AMBIGUOUS_PREFIX)


def claim(
    session: Session,
    *,
    service: str,
    newest_first: bool = False,
    exclude: Collection[int] = (),
) -> Claimed | None:
    """Take the next due ``pending`` row for ``service``, or ``None``.

    ``FOR UPDATE SKIP LOCKED`` on the ledger row, held until the caller's session commits
    or rolls back, so a second worker moves on to the next row instead of waiting for a
    30-second upload.

    ``newest_first`` inverts the order, and exists for Broadcastify Calls alone.  Their
    archive refuses a call that reaches it more than a few seconds after the transmission
    ended (``REJECTED-CALL-SKEW-TOO-LONG``), so a backlog worked oldest-first spends every
    attempt on the calls that are already too late while the fresh ones behind them go
    stale in turn — the queue converts a momentary delay into a permanent one.  Newest
    first inverts that: the calls that can still be published are attempted while they can
    be, and the ones behind them are refused by the cheap pre-check in the worker
    (:data:`~app.exporters.broadcastify.LATE_REASON`) rather than by a round trip.  OpenMHz
    has no such window and stays oldest-first, which is the fairer order when it is free.

    ``exclude`` is the ids the caller already handled in this pass.  A row whose outcome
    could not be written is still ``pending`` and still due, and without this the batch loop
    would take it straight back — twenty uploads of one call in one pass.
    """
    order = "next_attempt_at DESC, id DESC" if newest_first else "next_attempt_at, id"
    skip = sorted({int(i) for i in exclude})
    stmt = text(
        f"""
        SELECT id, call_id, attempts, last_error
        FROM call_exports
        WHERE service = :service AND status = 'pending' AND next_attempt_at <= now()
          {"AND id <> ALL(:exclude)" if skip else ""}
        ORDER BY {order}
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """  # noqa: S608 - ``order`` and the exclusion are literals chosen above
    )
    params: dict[str, Any] = {"service": service}
    if skip:
        stmt = stmt.bindparams(bindparam("exclude", type_=ARRAY(BigInteger)))
        params["exclude"] = skip
    row = session.execute(stmt, params).first()
    if row is None:
        session.rollback()
        return None
    call = session.get(Call, row.call_id)
    if call is None:  # pragma: no cover - the FK cascade makes this unreachable
        session.execute(
            update(CallExport)
            .where(CallExport.id == row.id)
            .values(status="skipped", skip_reason="the call row is gone")
        )
        session.commit()
        return None
    return Claimed(
        export_id=row.id,
        call_id=row.call_id,
        attempts=int(row.attempts),
        call=call,
        last_error=row.last_error,
    )


#: ``call_exports.response`` is 500 characters wide; the writers below cap rather than
#: refuse, because a ledger row is bookkeeping and a long line is not worth losing it over.
RESPONSE_CHARS = 500


def _said(response: str | None, response_code: int | None) -> dict[str, Any]:
    """The two "what did they answer" columns, or nothing at all.

    Returns an empty dict when there is no response, so a caller that never heard one does
    not overwrite a line an earlier attempt did hear with ``NULL`` — and so every writer
    below can splat this into its ``values()`` unconditionally.
    """
    if response is None and response_code is None:
        return {}
    return {"response": (response or "")[:RESPONSE_CHARS] or None, "response_code": response_code}


def mark_uploaded(
    session: Session,
    export_id: int,
    *,
    remote_id: str | None = None,
    response: str | None = None,
    response_code: int | None = None,
    uploaded_by: str | None = None,
) -> None:
    """The service accepted it.  Terminal: nothing re-reads an ``uploaded`` row.

    ``uploaded_by`` is which uploader sent it — ``keyup`` for this host's worker, a
    recorder's ``instance_id`` when a recorder uploaded it and reported the outcome on the
    call push.  ``None`` leaves the column alone rather than blanking it.
    """
    session.execute(
        update(CallExport)
        .where(CallExport.id == export_id)
        .values(
            status="uploaded",
            remote_id=remote_id,
            **({"uploaded_by": uploaded_by} if uploaded_by else {}),
            uploaded_at=datetime.now(UTC),
            last_error=None,
            updated_at=datetime.now(UTC),
            **_said(response, response_code),
        )
    )


def mark_skipped(
    session: Session,
    export_id: int,
    reason: str,
    *,
    response: str | None = None,
    response_code: int | None = None,
    attempts: int | None = None,
    error: str | None = None,
) -> None:
    """Deliberately not published, and not to be reconsidered.

    ``reason`` is our own prose and ``response`` is theirs.  The two are kept apart on
    purpose: "encrypted" and "outside the live export window" are decisions taken here with
    nothing sent, while ``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL`` is an answer, and only
    the second is a duplicate.

    ``attempts``/``error`` are for the skips that end a row which *was* attempted — a
    Broadcastify retry that ran out of skew window, an ambiguous upload that cannot be
    checked — so the count and the last failure are not lost with the status change.
    ``None`` leaves each column alone.
    """
    extra: dict[str, Any] = {}
    if attempts is not None:
        extra["attempts"] = int(attempts)
    if error is not None:
        extra["last_error"] = error[:2000]
    session.execute(
        update(CallExport)
        .where(CallExport.id == export_id)
        .values(
            status="skipped",
            skip_reason=reason[:500],
            updated_at=datetime.now(UTC),
            **extra,
            **_said(response, response_code),
        )
    )


def mark_failed(
    session: Session,
    export_id: int,
    *,
    attempts: int,
    error: str,
    max_attempts: int,
    response: str | None = None,
    response_code: int | None = None,
    retry_in_s: float | None = None,
) -> bool:
    """Count the attempt and either schedule a retry or park the row.  ``True`` = parked.

    Parking is the point: a call that has failed ``max_attempts`` times is almost always
    failing for a reason a retry cannot fix (a rejected key, a talkgroup the service does
    not know), and a queue that retries it for ever is a queue that never drains.

    ``retry_in_s`` replaces the backoff curve for one retry — ``0`` is "at once", which is
    what a Broadcastify retry inside the skew window needs, and the ambiguous-upload path
    passes :data:`VERIFY_DELAY_S`.
    """
    used = attempts + 1
    parked = used >= max(1, max_attempts)
    values: dict[str, Any] = {
        "attempts": used,
        "last_error": (error or "")[:2000],
        "updated_at": datetime.now(UTC),
        **_said(response, response_code),
    }
    if parked:
        values["status"] = "failed"
    else:
        delay = backoff_seconds(used - 1) if retry_in_s is None else max(0.0, float(retry_in_s))
        values["next_attempt_at"] = datetime.now(UTC) + timedelta(seconds=delay)
    session.execute(update(CallExport).where(CallExport.id == export_id).values(**values))
    return parked


def bump_attempt(session: Session, export_id: int, *, attempts: int, error: str) -> bool:
    """Count one attempt and push the row back by the backoff — nothing else.  ``True`` if bumped.

    The fallback for when the real outcome could not be written (the database refused the
    ledger write, or the transaction it was in had already failed): without it the row is
    rolled back to ``pending`` with its old ``next_attempt_at`` and is due again at once, so
    the next pass sends the call again with no backoff at all.  Guarded on ``pending`` so it
    never undoes an outcome another writer did manage to record.  The caller commits.
    """
    result = session.execute(
        update(CallExport)
        .where(CallExport.id == export_id, CallExport.status == "pending")
        .values(
            attempts=CallExport.attempts + 1,
            last_error=(error or "")[:2000],
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts)),
            updated_at=datetime.now(UTC),
        )
    )
    return bool(result.rowcount)


def summary(session: Session) -> dict[str, dict[str, int]]:
    """``{service: {status: count}}`` over the whole ledger — the observability read."""
    out: dict[str, dict[str, int]] = {}
    for service, status, count in session.execute(
        select(CallExport.service, CallExport.status, func.count()).group_by(
            CallExport.service, CallExport.status
        )
    ):
        out.setdefault(str(service), {})[str(status)] = int(count)
    return out


def window_summary(session: Session, *, hours: int = 24) -> dict[str, dict[str, int]]:
    """:func:`summary`, restricted to rows touched in the last ``hours``.

    ``updated_at`` rather than ``created_at``: the question the status panel asks is "how
    is it doing *now*", and a row discovered yesterday that failed ten minutes ago is part
    of today's answer.
    """
    since = datetime.now(UTC) - timedelta(hours=max(1, hours))
    out: dict[str, dict[str, int]] = {}
    for service, status, count in session.execute(
        select(CallExport.service, CallExport.status, func.count())
        .where(CallExport.updated_at >= since)
        .group_by(CallExport.service, CallExport.status)
    ):
        out.setdefault(str(service), {})[str(status)] = int(count)
    return out


def recent(session: Session, *, service: str | None = None, limit: int = 50) -> list[CallExport]:
    """The most recently touched ledger rows, newest first — the "Recent uploads" table."""
    stmt = select(CallExport).order_by(CallExport.updated_at.desc(), CallExport.id.desc())
    if service is not None:
        stmt = stmt.where(CallExport.service == service)
    return list(session.scalars(stmt.limit(max(1, min(500, limit)))))


def requeue(session: Session, export_id: int) -> CallExport | None:
    """Put one parked row back in the queue, due now.  ``None`` when there is no such row.

    Only ``failed`` and ``skipped`` rows are re-queued: an ``uploaded`` row is the dedupe
    guarantee itself and re-sending it would publish the call twice, which is the whole
    thing this module exists to prevent.  The attempt count is reset so the operator gets a
    full set of tries rather than one, and the last error is kept until the retry writes
    its own outcome.
    """
    row = session.get(CallExport, export_id)
    if row is None or row.status not in ("failed", "skipped"):
        return None
    row.status = "pending"
    row.attempts = 0
    row.skip_reason = None
    row.next_attempt_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    session.flush()
    return row
