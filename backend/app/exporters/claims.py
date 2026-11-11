"""The Broadcastify Calls claim registry: who is allowed to upload one call.

Only one thing may POST a given call to Broadcastify Calls, and on this deployment more
than one thing can: two trunkd recorders covering the same site both record every
transmission, and keyup's own exporter worker still uploads whatever no recorder answered
for (docs/EXPORTERS.md, "Recorder-direct upload").

Broadcastify's own duplicate detection is not enough to lean on.  It keys a call on
``(system, tg, ts)`` and ``ts`` is **whole seconds**, so two uploaders whose idea of when
the transmission started differs by one second do not look like a duplicate to them: they
look like two calls, and the archive keeps both.  Hence a registry on this side, asked
*before* anything is sent, with a tolerance wide enough to cover that rounding —
:data:`TOLERANCE_S`.

The grant is taken under a per-talkgroup ``pg_advisory_xact_lock``.  Two recorders finish
the same call within milliseconds of each other, so "look, then insert" without a lock is
exactly the race this exists to close; the lock is per *talkgroup* rather than global so
a busy dispatch channel never serializes the rest of the system behind it, and it is a
transaction-scoped lock so there is no unlock to forget on an error path.

Every holder of that lock keeps it for milliseconds — the claim is its own transaction,
committed before anything is sent — and every waiter gives up after
:data:`LOCK_TIMEOUT_MS` with :class:`ClaimBusy` rather than waiting on it indefinitely.

A claim does not last for ever against keyup.  keyup's worker is the retry path for a
recorder's failed upload, so a claim whose outcome is ``failed``, or which has had no
outcome at all for :data:`STALE_S` (the recorder claimed and never came back), is treated
as released when ``keyup`` asks — and keyup takes it over with a claim of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import BcfyClaim

log = logging.getLogger("app.exporters.claims")

#: The ``instance`` a claim taken by this host's own exporter worker carries.  A literal
#: rather than the hostname: what matters to a reader is "keyup uploaded it, not one of
#: the recorders", and a deployment that renames its host has not changed that answer.
KEYUP = "keyup"

#: How far apart two claims may be and still be the same call, in seconds.  It is the
#: rounding tolerance, not a guess at how long a call is: Broadcastify's ``ts`` is a whole
#: second and two recorders can disagree about which second a transmission began in, so
#: ±2 s covers the disagreement without being wide enough to swallow the next grant on a
#: fast-moving talkgroup.
TOLERANCE_S = 2

#: The ledger's prose for a call this end declined to send because somebody else had it.
#: A constant for the same reason :data:`app.exporters.broadcastify.DUPLICATE_REASON` is:
#: the worker writes it, the call push writes it, and the Exports card counts rows that
#: start with it, so the three must not drift apart.  The holder is appended.
CLAIMED_REASON = "claimed by recorder"

#: How long a claim is kept.  Nothing reads one after the call it was about has been
#: stored and answered for, and the window it is consulted in is seconds wide; two days is
#: long enough that an operator debugging yesterday's uploads can still see them.
KEEP_DAYS = 2

#: The first half of the advisory-lock key, so a claim never collides with another part of
#: the application that also locks on a talkgroup id.
LOCK_NAMESPACE = 0x6243_4659  # "bCFY"

#: How long a claimant waits for the per-talkgroup lock before it is told the registry is
#: busy.  Under the recorder's own 1.5 s claim timeout (trunkd ``CLAIM_TIMEOUT`` plus its
#: half-second extra), so the recorder hears a definite 503 rather than timing out, and so
#: a request thread and a pool connection are never tied up behind somebody else's lock.
LOCK_TIMEOUT_MS = 1000
#: …and the ceiling on any one statement of the claim, lock wait included.
STATEMENT_TIMEOUT_MS = 2000

#: Outcomes that release a claim for ``keyup``: the claimant tried and it did not land.
RELEASED_OUTCOMES = frozenset({"failed"})
#: How long a claim with *no* outcome holds against ``keyup``.  A recorder reports its
#: outcome on the call push a few seconds after claiming (register 4 s + PUT 6 s at the
#: very worst); one that has said nothing for this long crashed or lost the call, and the
#: call is keyup's to send.  Well past the skew window, so with the window on it never
#: decides anything a live recorder is still in the middle of.
STALE_S = 30


class ClaimBusy(RuntimeError):
    """The per-talkgroup lock could not be had within :data:`LOCK_TIMEOUT_MS`.

    The caller's transaction has failed when this is raised and must be rolled back.
    """


def _busy(exc: DBAPIError) -> bool:
    """Is this the lock or statement timeout :func:`acquire` set, rather than a real error?"""
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    # 55P03 lock_not_available, 57014 query_canceled (statement_timeout)
    return code in ("55P03", "57014")


def reason_for(holder: str | None) -> str:
    """The ledger skip reason naming who won the claim."""
    return f"{CLAIMED_REASON} {holder or 'unknown'}"


@dataclass(frozen=True)
class Claim:
    """The answer to "may I upload this call": may I, who holds it, and which row says so."""

    granted: bool
    holder: str
    claim_id: int
    #: the claimant whose released claim ``keyup`` took over, when that is how it was granted
    taken_over_from: str | None = None


def released(row: BcfyClaim, now: datetime) -> bool:
    """Does this claim no longer hold against ``keyup``?  See :data:`RELEASED_OUTCOMES`."""
    outcome = (row.outcome or "").strip().lower()
    if outcome in RELEASED_OUTCOMES:
        return True
    if outcome:
        return False
    created = row.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds() >= STALE_S


def _duration(duration_s: float | Decimal | None) -> Decimal | None:
    return Decimal(str(round(float(duration_s), 3))) if duration_s is not None else None


def _near(tgid: int, ts: int):  # noqa: ANN202 - a SQLAlchemy select
    """Every claim for this talkgroup within :data:`TOLERANCE_S` of this second."""
    return (
        select(BcfyClaim)
        .where(
            BcfyClaim.tgid == int(tgid),
            BcfyClaim.ts >= int(ts) - TOLERANCE_S,
            BcfyClaim.ts <= int(ts) + TOLERANCE_S,
        )
        .order_by(BcfyClaim.id)
    )


def acquire(
    session: Session,
    *,
    instance: str,
    tgid: int,
    ts: int,
    duration_s: float | Decimal | None = None,
) -> Claim:
    """Claim one call for ``instance``, or report who already holds it.

    Idempotent for the same claimant: a recorder that asks twice — a retry, a duplicate
    push — is granted the claim it already has rather than being told it lost to itself.

    The holder is the first claimant, unless ``keyup`` has since taken the claim over: a
    ``keyup`` claim only ever exists because it was granted, so when there is one it is
    the holder.  ``keyup`` takes over a claim that is :func:`released` — failed, or silent
    for :data:`STALE_S` — by adding its own row beside it, so the registry still shows who
    asked first.

    The caller owns the transaction and should commit it at once: the advisory lock is
    transaction-scoped, so the grant is only exclusive once committed, and every other
    claimant on this talkgroup waits until then.  Raises :class:`ClaimBusy` when the lock
    is not had within :data:`LOCK_TIMEOUT_MS` (the transaction is then failed and must be
    rolled back).
    """
    tgid = int(tgid)
    ts = int(ts)
    try:
        # Transaction-local, so they end with the claim's own commit or rollback.
        session.execute(
            text(
                "SELECT set_config('lock_timeout', :lock, true), "
                "set_config('statement_timeout', :stmt, true)"
            ),
            {"lock": f"{LOCK_TIMEOUT_MS}ms", "stmt": f"{STATEMENT_TIMEOUT_MS}ms"},
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :tg)"), {"ns": LOCK_NAMESPACE, "tg": tgid}
        )
        rows = list(session.scalars(_near(tgid, ts)))
    except DBAPIError as exc:
        if _busy(exc):
            raise ClaimBusy(f"the claim registry for talkgroup {tgid} is busy") from exc
        raise
    if rows:
        holder = next((r for r in rows if r.instance == KEYUP), rows[0])
        if holder.instance == instance:
            return Claim(granted=True, holder=instance, claim_id=int(holder.id))
        if instance == KEYUP and released(holder, session.scalar(select(func.now()))):
            row = BcfyClaim(
                tgid=tgid,
                ts=ts,
                duration_s=_duration(duration_s),
                instance=KEYUP,
            )
            session.add(row)
            session.flush()
            log.info(
                "claims: keyup takes over talkgroup %s at %s from %s (claim %s, outcome %s)",
                tgid,
                ts,
                holder.instance,
                holder.id,
                holder.outcome or "none",
            )
            return Claim(
                granted=True,
                holder=KEYUP,
                claim_id=int(row.id),
                taken_over_from=str(holder.instance),
            )
        return Claim(granted=False, holder=str(holder.instance), claim_id=int(holder.id))
    row = BcfyClaim(
        tgid=tgid,
        ts=ts,
        duration_s=_duration(duration_s),
        instance=instance,
    )
    session.add(row)
    session.flush()
    return Claim(granted=True, holder=instance, claim_id=int(row.id))


def finish(
    session: Session,
    claim_id: int,
    *,
    outcome: str | None = None,
    response: str | None = None,
    call_id: int | None = None,
) -> None:
    """Record what became of the upload a claim reserved.  Every field is optional."""
    values: dict[str, Any] = {}
    if outcome is not None:
        values["outcome"] = outcome[:100]
    if response is not None:
        values["response"] = response[:500]
    if call_id is not None:
        values["call_id"] = int(call_id)
    if not values:
        return
    session.execute(update(BcfyClaim).where(BcfyClaim.id == int(claim_id)).values(**values))


def stamp_call(
    session: Session,
    *,
    instance: str,
    tgid: int,
    ts: int,
    call_id: int,
    outcome: str | None = None,
    response: str | None = None,
) -> int | None:
    """Attach a stored call to the claim its recorder made, when there is one.

    Matched the way a claim is granted — same talkgroup, within :data:`TOLERANCE_S`, same
    claimant — because that is the only identity a claim has: it was made at the edge, one
    HTTP request before the audio existed here.  Returns the claim's id, or ``None`` when
    the recorder never claimed (the setting was off, or the claim went to somebody else).
    """
    row = session.scalars(_near(int(tgid), int(ts)).where(BcfyClaim.instance == instance)).first()
    if row is None:
        return None
    finish(session, int(row.id), outcome=outcome, response=response, call_id=call_id)
    return int(row.id)


def prune(session: Session, *, days: int = KEEP_DAYS) -> int:
    """Delete claims older than ``days``.  Returns how many went."""
    cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
    result = session.execute(delete(BcfyClaim).where(BcfyClaim.created_at < cutoff))
    return int(result.rowcount or 0)
