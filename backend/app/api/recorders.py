"""Multi-tuner management: the recorder's desired-state poll and status push, and the
operator's view and edits (docs/ARCHITECTURE.md §4.8).

Two audiences, two authentications:

* the **recorder** polls ``GET /recorder/desired-state`` and posts ``POST /recorder/tuners/
  status`` with the same Bearer token carrying the ``upload`` scope that the call push and
  the observation push use — a recorder needs one credential, not several.  Both routes are
  keyed by the recorder's own ``instance_id`` and auto-create the ``recorders`` row on first
  contact, exactly as a site appears from its first observation;
* an **operator** reads ``GET /admin/recorders``, writes one tuner's desired state with
  ``PUT /admin/recorders/{id}/tuners/{key}``, and writes the recorder's own policy and
  desired state — hot/warm/cold, a preference number, its ``instance_id``, and the three
  instance-level settings (the upload hold ``spool_only``, the ``event_log_archive`` flag
  and the whole recorder-to-recorder ``link`` block) — with
  ``PUT /admin/recorders/{id}``.  All need the ``admin`` scope.  The tuner write is shared
  code: :func:`lock_recorder` + :func:`apply_tuner_desired` are what the operator's ``PUT``
  *and* :mod:`app.arbiter` both go through, so there is one implementation of the locked
  read-modify-write and not two.

``GET /admin/occupancy`` is read-only diagnostics folded out of ``recorder_observations``:
it needs no new recorder data.  Like the rest of ``admin_router`` it is admin-gated.

The contract with trunkd (the shapes below) is meant to stay stable: trunkd posts status
and polls desired-state in exactly these shapes.  Two properties of the contract worth
stating outright:

* **Tokens are mutually trusting.**  Any ``upload``-scoped token may poll or post the
  status of *any* recorder — a recorder is identified by the ``instance_id`` it names, not
  by which token it holds.  ``recorders.token_id`` records the token seen on first contact
  and is **reported, not enforced**: a later poll on a different token is served exactly as
  before and leaves the binding alone, but says so once an hour in the activity log
  (:func:`_note_token_mismatch`).  Refusing it would take a recorder off the air over a key
  rotation, which is a worse failure than the one it would catch; a token quietly moving
  between machines is still something an operator should be able to see afterwards.
* **Retunes are bounded by the reported span.**  ``PUT`` only accepts a
  ``center_frequency`` within half a span either side of the tuner's last *reported* center
  (``center_hz ± span_hz/2``): the recorder cannot tune outside the window it is on.  When
  the span is not known yet, the center is accepted as given.
* **The link's shared key leaves by exactly two doors.**  ``GET /recorder/desired-state``
  carries it in clear to the recorder that must hold it, and
  ``GET /admin/recorders/{id}/link/key`` hands it to an admin who asked for it and writes
  the read to the activity log.  Every other render of a recorder goes through
  :func:`app.schemas.public_link`, which replaces the key with ``key_set`` and an 8-hex
  fingerprint — one function, so the defence is one line of code rather than a rule every
  response model has to remember.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import activity
from app.api.observations import RecorderToken
from app.api.recorder_stream import announce as announce_desired_change
from app.api.recorder_stream import stream_info
from app.auth import AdminPrincipal, require_admin
from app.config import get_settings
from app.db import ReleasedSession, get_session, notify
from app.live import bus
from app.models import (
    RECORDER_STANDBY,
    Call,
    EventLogArchive,
    RadioEvent,
    Recorder,
    RecorderObservation,
    Site,
    Talkgroup,
    Tuner,
)
from app.runtime_settings import effective_settings
from app.schemas import (
    CHANNEL_STEP_HZ,
    HACKRF_LNA_MAX_DB,
    HACKRF_LNA_STEP_DB,
    HACKRF_ONLY_TUNER_FIELDS,
    HACKRF_VGA_MAX_DB,
    HACKRF_VGA_STEP_DB,
    MAX_INSTANCE_ID_LEN,
    MAX_PPM,
    MAX_TUNER_KEY_LEN,
    MIN_SAMPLE_RATE_CHANNELS,
    SOAPY_ONLY_TUNER_FIELDS,
    BroadcastifyClaimIn,
    BroadcastifyClaimOut,
    DesiredBroadcastify,
    DesiredRecorder,
    DesiredSkipTalkgroups,
    DesiredStateOut,
    DesiredTuner,
    RecorderLinkKeyGenerated,
    RecorderLinkKeyOut,
    RecorderOut,
    RecorderPolicyIn,
    RecorderStreamOut,
    SiteOccupancy,
    SpectrumAccepted,
    SpectrumBatchIn,
    SpectrumFrameIn,
    TunerDesiredIn,
    TunerOut,
    TunerStatusAccepted,
    TunerStatusIn,
    link_key_fingerprint,
    normalize_link,
    public_broadcastify,
    public_link,
)
from app.spectrum import hub as spectrum_hub

log = logging.getLogger("app.api.recorders")

router = APIRouter(tags=["recorder"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
DbSession = Annotated[Session, Depends(get_session)]

#: The roles an operator may assign a tuner.
TUNER_ROLES = ("auto", "control", "voice", "data")


def _scrub(value: Any) -> Any:
    """Strip NUL (``\\x00``) from strings, recursively — Postgres JSONB rejects it.

    A NUL in a reported string would blow up at flush time, *outside* the per-entry
    ``try`` in the status loop, and take the whole push down with a 500.  We drop it here
    so a rogue byte costs one character, not the report.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


#: At most one WARNING and one activity row per hour per ``(recorder, token)`` pair.  A
#: recorder polls every few seconds, so an entry per contact would bury the log in the one
#: message it exists to make findable.
TOKEN_MISMATCH_INTERVAL_S = 3600.0
#: ``{(recorder id, token id): monotonic of the last complaint}`` — module state, because a
#: rate limit that resets per request is not one.
_token_mismatch: dict[tuple[int, int], float] = {}


def _note_token_mismatch(db: Session, recorder: Recorder, token_id: int) -> None:
    """Say — at most once an hour — that a recorder turned up on a different token.

    The binding is left exactly as it was.  Re-binding would make the column follow
    whichever key was used last and record nothing; refusing would turn a routine key
    rotation into an outage.  So the poll is served, the first token keeps the row, and the
    activity log carries the one line that answers "when did this recorder change keys".
    """
    key = (recorder.id, token_id)
    now = time.monotonic()
    last = _token_mismatch.get(key)
    if last is not None and now - last < TOKEN_MISMATCH_INTERVAL_S:
        return
    _token_mismatch[key] = now
    log.warning(
        "recorder %s reported on token %s but is bound to token %s; serving it anyway",
        recorder.instance_id,
        token_id,
        recorder.token_id,
    )
    activity.record(
        db,
        source="system",
        level="warn",
        kind="recorder.token_mismatch",
        message=(
            f"Recorder {recorder.label or recorder.instance_id} is using a different API token "
            f"than the one it first reported on"
        ),
        meta={
            "recorder_id": recorder.id,
            "instance_id": recorder.instance_id,
            "bound_token_id": recorder.token_id,
            "seen_token_id": token_id,
        },
    )


def _get_or_create_recorder(db: Session, instance_id: str, *, token_id: int | None = None) -> Recorder:
    """The recorder for ``instance_id``, created on first contact like a site.

    Two recorders (or a recorder and an operator) reaching keyup at once must not 500 on
    ``uq_recorders_instance_id``: the insert runs in a SAVEPOINT and, if it loses the race,
    the row the winner committed is re-selected instead.
    """
    recorder = db.scalar(select(Recorder).where(Recorder.instance_id == instance_id))
    if recorder is None:
        try:
            with db.begin_nested():
                recorder = Recorder(instance_id=instance_id, token_id=token_id)
                db.add(recorder)
                db.flush()
        except IntegrityError:
            recorder = db.scalar(select(Recorder).where(Recorder.instance_id == instance_id))
        if recorder is None:  # pragma: no cover - the row exists after either branch
            raise HTTPException(status_code=500, detail="Could not create recorder")
    if token_id is not None:
        if recorder.token_id is None:
            recorder.token_id = token_id
        elif recorder.token_id != token_id:
            _note_token_mismatch(db, recorder, token_id)
    return recorder


def _get_or_create_tuner(db: Session, recorder_id: int, tuner_key: str) -> Tuner:
    """The tuner row for ``(recorder_id, tuner_key)``, created race-safely if missing.

    A status push and an operator ``PUT`` can both try to create the same tuner at once;
    the SAVEPOINT + re-select keeps ``uq_tuners_recorder_key`` from 500-ing either.
    """
    tuner = db.scalar(
        select(Tuner).where(Tuner.recorder_id == recorder_id, Tuner.tuner_key == tuner_key)
    )
    if tuner is None:
        try:
            with db.begin_nested():
                tuner = Tuner(recorder_id=recorder_id, tuner_key=tuner_key)
                db.add(tuner)
                db.flush()
        except IntegrityError:
            tuner = db.scalar(
                select(Tuner).where(Tuner.recorder_id == recorder_id, Tuner.tuner_key == tuner_key)
            )
        if tuner is None:  # pragma: no cover - the row exists after either branch
            raise HTTPException(status_code=500, detail="Could not create tuner")
    return tuner


def _drift(tuner: Tuner) -> bool:
    """True when the recorder has not applied the current desired state.

    Only a tuner that has actually been edited (``desired_version > 0``) *and* reports an
    ``applied_version`` that differs is drifted.  A never-edited tuner, or one whose
    recorder has not yet echoed an ``applied_version``, is "nothing desired / not reported
    yet" — not drift — so the "pending" badge does not sit on permanently.
    """
    if tuner.desired_version <= 0:
        return False
    applied = (tuner.reported or {}).get("applied_version")
    if applied is None:
        return False
    return applied != tuner.desired_version


def _recorder_pending(recorder: Recorder) -> bool:
    """True when keyup has an opinion on this recorder's uploads it has not taken up yet.

    The recorder-level twin of :func:`_drift`, with one deliberate difference.  A tuner that
    has never reported an ``applied_version`` is *not* called drifted, because most tuner
    fields have a sensible "nothing desired" state and a permanent badge would say nothing.
    Here the badge exists precisely to answer "has the hold I just set landed?", and a
    recorder that has said nothing back has demonstrably not applied it — so a missing
    ``applied_recorder_version`` reads as version 0, which differs from any version an
    operator write produced (writes start the counter at 1).  A recorder keyup has no
    opinion about (``desired_spool_only is None``) is never pending: there is nothing to
    apply.
    """
    if recorder.desired_spool_only is None:
        return False
    return (recorder.applied_recorder_version or 0) != (recorder.desired_recorder_version or 0)


def _recorder_block_pending(recorder: Recorder) -> bool:
    """:func:`_recorder_pending` widened to the whole recorder-level block.

    ``spool_only_pending`` stays scoped to the upload hold because the card's upload strip
    is about the hold and nothing else; this one answers "is *anything* keyup told this
    recorder still not taken up?", which is the badge the settings dialog wants.  A recorder
    keyup manages nothing about is never pending, for the same reason as before: there is
    nothing to apply.
    """
    managed = (
        recorder.desired_spool_only is not None
        or recorder.desired_event_log_archive is not None
        or recorder.desired_link is not None
    )
    if not managed:
        return False
    return (recorder.applied_recorder_version or 0) != (recorder.desired_recorder_version or 0)


def masked_broadcastify(db: Session) -> dict[str, Any] | None:
    """The Broadcastify block as an *operator* may see it — the API key taken out.

    One call per response rather than one per recorder: the block is derived from the
    export settings, so every recorder on the page is being sent the same one.
    """
    block = broadcastify_desired(db, effective_settings(db, base=get_settings()))
    return public_broadcastify(block.model_dump()) if block is not None else None


def skip_talkgroups_summary(db: Session) -> dict[str, Any] | None:
    """``{count, revision}`` for the skip list, or ``None`` when nothing is skipped.

    The count and not the list: a recorder row on the Admin page is answering "is keyup
    telling this box to drop anything, and has it changed", and the hundred tgids behind
    that answer belong on the Talkgroups page where they are edited.
    """
    block = skip_talkgroups_desired(db, effective_settings(db, base=get_settings()))
    if block is None:
        return None
    return {"count": len(block.tgids), "revision": block.revision}


#: Where a recorder posts its Broadcastify claim.  A constant because it is shipped *to*
#: the recorder in the desired-state block: the recorder does not build this path, it is
#: told it, so moving the route is one edit here and one poll on every recorder.
BROADCASTIFY_CLAIM_PATH = "/api/recorder/broadcastify/claim"


def broadcastify_desired(db: Session, settings: Any) -> DesiredBroadcastify | None:
    """The recorder's Broadcastify Calls block, or ``None`` — which is the usual answer.

    Three conditions, all of them "is this actually on": the operator has chosen
    ``broadcastify_upload_at = recorder``, the Calls exporter is enabled, and the selected
    environment's credentials are present.  Any one of them false and the block is absent,
    which is the instruction to keep uploading through keyup.

    The talkgroup list is *resolved* here rather than shipped as a rule.  A recorder has no
    talkgroup table and no idea which systems this archive calls trunked, so ``allow`` and
    ``deny`` both come down the wire as one explicit list of tgids and ``all`` comes down
    as ``null``.  One consequence is worth stating: in ``deny`` mode a talkgroup keyup has
    never catalogued is *not* in the list, so the recorder will not upload it, where
    keyup's own uploader would have (:func:`app.exporters.gate.selected` fails closed on a
    talkgroup it cannot read, but discovery would have created the row first).  A list is
    the only shape a recorder can act on at call end, and erring towards not publishing a
    channel nobody has described is the right direction to err in.
    """
    from app.exporters import gate  # noqa: PLC0415 - the exporters package imports httpx
    from app.exporters.broadcastify import SERVICE as BROADCASTIFY_SERVICE  # noqa: PLC0415
    from app.exporters.broadcastify import endpoint  # noqa: PLC0415

    if str(getattr(settings, "broadcastify_upload_at", "keyup") or "keyup") != "recorder":
        return None
    if not bool(getattr(settings, "export_broadcastify_enabled", False)):
        return None
    where = endpoint(settings)
    if not where.configured():
        return None

    talkgroups: list[int] | None = None
    if gate.selection_mode(settings, BROADCASTIFY_SERVICE) != "all":
        flag = gate.export_flag_column(Talkgroup, BROADCASTIFY_SERVICE)
        trunked = {row.id for row in gate.trunked_systems(db)}
        wanted = gate.systems_for(settings, BROADCASTIFY_SERVICE)
        if wanted:
            trunked &= wanted
        statement = select(Talkgroup.tgid).where(Talkgroup.system_id.in_(sorted(trunked) or [0]))
        if flag is not None:
            statement = statement.where(flag.is_(True))
        talkgroups = sorted({int(tgid) for tgid in db.scalars(statement)})

    block: dict[str, Any] = {
        "enabled": True,
        "endpoint_url": where.url,
        "api_key": where.api_key,
        "system_id": where.system_id,
        # mp3 and not the m4a the worker prefers: the recorder encodes at call end on a
        # box that may have no libfdk_aac at all, and an encoder that is not there is a
        # call that is never uploaded.  Broadcastify accepts both.
        "encoding": "mp3",
        "talkgroups": talkgroups,
        "skew_limit_s": float(getattr(settings, "broadcastify_skew_limit_s", 15.0) or 0.0),
        "claim": True,
        "claim_url": BROADCASTIFY_CLAIM_PATH,
    }
    # A hash of the block rather than a counter, because what it is derived from — runtime
    # settings and the talkgroup selection — is not versioned per recorder and cannot be.
    # 12 hex characters: enough that two different blocks will not collide, short enough to
    # read in a log line.
    revision = hashlib.sha256(orjson.dumps(block, option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]
    return DesiredBroadcastify(**block, revision=revision)


#: The three outbound services whose selection is consulted before a talkgroup may be
#: called unwanted.  Named here rather than derived from the exporters' registry because
#: this is a *safety* list: a service that is added and forgotten here would let keyup tell
#: a recorder to stop uploading a channel that service publishes.
SKIP_EXPORT_SERVICES = ("broadcastify", "broadcastify_feed", "openmhz")


def skip_talkgroups_desired(db: Session, settings: Any) -> DesiredSkipTalkgroups | None:
    """The tgids a recorder need not upload at all, or ``None`` when there are none.

    A talkgroup qualifies only when every use its audio could be put to is off:

    * ``record`` is false — the recording is not being kept;
    * ``transcribe`` is false — the words are not being kept either;
    * and no *enabled* export service selects it, judged by :mod:`app.exporters.gate`'s
      own selection rule rather than by a re-reading of the flag columns here.  A service
      that is switched off keeps nothing; an enabled one in ``all`` mode selects every
      talkgroup, so nothing is skippable while it is on.

    Keyed by **tgid**, because that is all a recorder has: it decoded a number off a
    control channel and has no idea which of keyup's systems this deployment filed it
    under.  So a tgid is listed only when *every* talkgroup row carrying that number, on
    every trunked system, is skippable — one system still wanting 1055 keeps 1055 coming.

    ``None`` for an empty list, so the recorder's "is there anything here" test is the
    presence of the block, the same shape ``broadcastify`` uses.
    """
    from app.exporters import gate  # noqa: PLC0415 - the exporters package imports httpx

    trunked = {row.id for row in gate.trunked_systems(db)}
    if not trunked:
        return None

    from app.exporters.worker import enabled_for  # noqa: PLC0415 - same package, same reason

    # Only a service that is switched on can put audio to use: a flag on a talkgroup for
    # an exporter nobody enabled keeps nothing.  Of those, a service in ``all`` mode
    # selects every talkgroup, so nothing is skippable.  Asked once rather than per row.
    services = [service for service in SKIP_EXPORT_SERVICES if enabled_for(settings, service)]
    if any(gate.selection_mode(settings, service) == "all" for service in services):
        return None
    flags = [
        column
        for column in (gate.export_flag_column(Talkgroup, service) for service in services)
        if column is not None
    ]

    statement = select(Talkgroup.tgid, Talkgroup.record, Talkgroup.transcribe, *flags).where(
        Talkgroup.system_id.in_(sorted(trunked))
    )
    wanted: set[int] = set()
    skippable: set[int] = set()
    for row in db.execute(statement):
        tgid = int(row[0])
        unwanted = not row[1] and not row[2] and not any(bool(flag) for flag in row[3:])
        (skippable if unwanted else wanted).add(tgid)

    tgids = sorted(skippable - wanted)
    if not tgids:
        return None
    # The same 12 hex characters over the same shape as the Broadcastify block, and for the
    # same reason: nothing versions the talkgroup table per recorder, so the recorder
    # compares a hash of what it was last sent.
    revision = hashlib.sha256(orjson.dumps(tgids)).hexdigest()[:12]
    return DesiredSkipTalkgroups(tgids=tgids, revision=revision)


# ------------------------------------------------------------------ recorder-facing ---


@router.get(
    "/recorder/desired-state",
    response_model=DesiredStateOut,
    summary="The desired state for a recorder's tuners",
    description=(
        "What keyup wants each of a recorder's tuners to be doing, keyed by the recorder's own "
        "tuner key. Each tuner carries its own `version` (its `desired_version`) alongside the "
        "`config` to apply — every field an operator wrote, the front-end settings included, "
        "passed through verbatim; the recorder echoes that version back as `applied_version` in its "
        "status push, and keyup calls the tuner drifted while the two differ. The recorder-level "
        "`version` changes whenever any tuner does, so an unchanged value means nothing to do. "
        "The `recorder` block is what keyup wants of the *instance* — currently `spool_only`, "
        "the upload hold — on its own `version`, which the recorder echoes back as "
        "`recorder_applied.version` in its status push; a null `spool_only` means keyup has no "
        "opinion and the recorder keeps its own config/panel value. "
        "The recorder is created on first contact. Needs the `upload` scope."
    ),
)
def get_desired_state(
    db: DbSession,
    token: RecorderToken,
    instance: Annotated[str, Query(description="The recorder's instance_id", max_length=128)],
) -> DesiredStateOut:
    recorder = _get_or_create_recorder(db, instance, token_id=token.id)
    settings = effective_settings(db, base=get_settings())
    tuners = {
        tuner.tuner_key: DesiredTuner(version=tuner.desired_version, config=dict(tuner.desired))
        for tuner in recorder.tuners
        if tuner.desired
    }
    db.commit()
    return DesiredStateOut(
        version=recorder.version,
        tuners=tuners,
        # The recorder's own desired state, on its own counter. `spool_only` is None on
        # every recorder no operator has touched, and None means "no opinion" — the
        # recorder keeps its config/panel value rather than being switched by our silence.
        recorder=DesiredRecorder(
            version=recorder.desired_recorder_version,
            spool_only=recorder.desired_spool_only,
            event_log_archive=recorder.desired_event_log_archive,
            # The one response that carries the shared link key in clear, and it goes to the
            # recorder that has to hold it.  Everything an operator reads goes through
            # `public_link`, which takes the key out; this deliberately does not.
            link=recorder.desired_link if isinstance(recorder.desired_link, dict) else None,
            # Derived from runtime settings, not from anything stored on this recorder, so
            # it is built per poll and carries its own `revision` instead of moving
            # `version` — a settings change is not a recorder edit and must not read as
            # one.  The second response that carries a credential in clear, and it goes to
            # the recorder that has to register with it.
            broadcastify=broadcastify_desired(db, settings),
            # Derived from the talkgroup table for the same reason and on the same terms:
            # its own `revision`, and `version` is left alone.  Null — the shipped answer —
            # means "upload everything", which is also what an older recorder that has
            # never heard of the field does.
            skip_talkgroups=skip_talkgroups_desired(db, settings),
        ),
    )


@router.post(
    BROADCASTIFY_CLAIM_PATH.removeprefix("/api"),
    response_model=BroadcastifyClaimOut,
    summary="Claim one call for upload to Broadcastify Calls",
    description=(
        "Ask before uploading: **may I** POST this call to Broadcastify Calls? Answered "
        "`granted: true` for the first claimant of a talkgroup and second (±2 s), and "
        "`granted: false` with the `holder` for everyone after — a recorder told no skips "
        "the upload and reports `broadcastifyOutcome=claim_denied` on its call push. "
        "Asking twice with the same `instance_id` is granted the same claim, so a retry is "
        "safe. This exists because two recorders hear every call and Broadcastify keys a "
        "duplicate on `(system, tg, ts)` with a **whole-second** `ts`: two uploaders that "
        "rounded the same transmission a second apart do not look like a duplicate there, "
        "they look like two calls (docs/EXPORTERS.md). keyup's own exporter claims here "
        "too, as `keyup`. The claim never waits more than about a second on another "
        "claimant of the same talkgroup: past that it answers **503** with `Retry-After`, "
        "which a recorder treats like any unanswered claim — it uploads anyway, because a "
        "duplicate Broadcastify can often catch is cheaper than a lost call. Needs the "
        "`upload` scope."
    ),
    responses={503: {"description": "The claim registry for this talkgroup is busy."}},
)
def claim_broadcastify(
    db: DbSession,
    token: RecorderToken,  # noqa: ARG001 - the scope check is the dependency's whole job
    body: BroadcastifyClaimIn,
) -> BroadcastifyClaimOut:
    from app.exporters import claims  # noqa: PLC0415 - the exporters package imports httpx

    try:
        held = claims.acquire(
            db,
            instance=body.instance_id,
            tgid=body.tgid,
            ts=body.ts,
            duration_s=body.duration_s,
        )
    except claims.ClaimBusy:
        # Not a denial.  "Denied" makes the recorder drop its upload, and the lock holder
        # may be claiming a *different* call on this talkgroup — so a denial here could
        # lose a call nobody else sends.  A 503 is what the recorder already treats as
        # "keyup could not answer" (trunkd `Claim::Unreachable`): it uploads and notes it.
        db.rollback()
        log.warning(
            "broadcastify claim: talkgroup %s busy for %s; answered 503",
            body.tgid,
            body.instance_id,
        )
        raise HTTPException(
            status_code=503,
            detail="the claim registry for this talkgroup is busy",
            headers={"Retry-After": "1"},
        ) from None
    # The commit is what makes the grant exclusive: the advisory lock the claim was taken
    # under is transaction-scoped, so the next claimant waits exactly this long.
    db.commit()
    return BroadcastifyClaimOut(
        granted=held.granted, holder=held.holder, claim_id=held.claim_id
    )


@router.post(
    "/recorder/tuners/status",
    response_model=TunerStatusAccepted,
    summary="A recorder reports what its tuners are doing",
    description=(
        "One report of every tuner's live status — serial, backend, center/span, role, lock "
        "state, control-channel health, pool usage, the hardware the operator says is on it, its "
        "front-end settings (`gain`/`gains`/`agc`, `ppm` with `auto_ppm_applied`, "
        "`bandwidth_hz`, `rx_port`, `settings`, `lna_db`/`vga_db`/`amp`, `sample_rate`, "
        "`data_follow`, `control_frequencies`), the `gain_elements` it has and the "
        "`capabilities` saying which of them change without a restart, any desired fields it "
        "`rejected`, its wideband level and frequency error, and the desired version it has "
        "applied. The body may also carry the recorder's own `station` (what and where the site "
        "is) and `counters` (instance-level lifetime totals); each of those is written only when "
        "present — omitting one leaves the stored value alone rather than clearing it, and the "
        "same goes for `uplink` (what the recorder's upload path is doing: holding or not, where "
        "that came from, what is waiting in the spool), `link` (the peers it is linked to), "
        "`event_log` (its own archive backlog) and `recorder_applied` (the recorder-level "
        "desired version it has applied). `monitor` — where the recorder serves its own live "
        "audio panel — is the one block that does **not** follow that rule: omitting it clears "
        "the stored one, because it becomes a link an operator clicks. Unknown tuner keys "
        "create rows; a malformed entry is skipped, never fatal. Bumps the recorder's "
        "last-seen and announces `tuner.status` on the live bus. Needs the `upload` scope."
    ),
)
def post_tuner_status(
    body: TunerStatusIn,
    db: DbSession,
    token: RecorderToken,
) -> TunerStatusAccepted:
    recorder = _get_or_create_recorder(db, body.instance_id, token_id=token.id)
    now = datetime.now(UTC)
    recorder.last_seen_at = now
    if body.recorder_version:
        recorder.recorder_version = body.recorder_version

    # Update-only, never clear: a push that omits `station` or `counters` says "the recorder
    # did not tell us", not "the recorder has none".  Only a body that actually carries the
    # key rewrites it.  Without that rule a recorder rolled back to an older binary — or one
    # whose station block the operator has not filled in yet — would silently wipe the
    # description a newer one reported, and the operator would have no way to tell the loss
    # from a station that was never described.  `exclude_none` keeps the stored object to the
    # fields the recorder actually sent, so an absent field reads as absent in the JSONB too.
    if body.station is not None:
        recorder.station = _scrub(body.station.model_dump(mode="json", exclude_none=True))
        recorder.reported_at = now
    if body.counters is not None:
        recorder.counters = body.counters.model_dump(mode="json", exclude_none=True)
        recorder.reported_at = now
    if body.uplink is not None:
        recorder.uplink = _scrub(body.uplink.model_dump(mode="json", exclude_none=True))
        recorder.reported_at = now
    # `link` and `event_log` are opaque objects rather than models, so they are stored as
    # they arrived — no `exclude_none`, because keyup is not the one deciding which of the
    # recorder's keys are meaningful.  Same update-only rule as everything above it: a push
    # that omits one says "no change", never "clear it".
    if body.link is not None:
        recorder.link = _scrub(body.link)
        recorder.reported_at = now
    if body.event_log is not None:
        recorder.event_log = _scrub(body.event_log)
        recorder.reported_at = now
    # The live-monitor block is the one exception to the update-only rule above, and it is
    # deliberate.  What keyup does with it is put an anchor on the status page; a link to a
    # panel the recorder has since stopped serving — or to a port it no longer listens on —
    # sends an operator to a dead page and tells them nothing about why.  So a push that
    # carries no `monitor` clears the stored one: the recorder that serves a panel says so
    # on every report, and silence is the honest answer for one that does not.  A recorder
    # on a binary that predates the panel therefore keeps a NULL here and simply gets no
    # link, which is exactly the page's behaviour before this existed.
    monitor = (
        _scrub(body.monitor.model_dump(mode="json", exclude_none=True))
        if body.monitor is not None
        else None
    )
    if monitor != recorder.monitor:
        recorder.monitor = monitor
    if body.monitor is not None:
        recorder.reported_at = now
    # The echo of the recorder-level desired version, which is what turns the operator's
    # "pending" badge off.  Stored even when it lags: the point of the number is the lag.
    # The whole block is kept beside the number so the dialog can show applied against
    # pending *per field* rather than one badge for three settings; `exclude_none` keeps it
    # to what the recorder actually said, so an older binary that echoes only the version
    # does not read as "it applied a null link".
    if body.recorder_applied is not None:
        recorder.applied_recorder_version = body.recorder_applied.version
        recorder.recorder_applied = _scrub(
            body.recorder_applied.model_dump(mode="json", exclude_none=True)
        )
        recorder.reported_at = now
    # Recorder-level refusals and capabilities, the instance-level twins of the per-tuner
    # maps.  Same update-only rule as everything above — but `{}` is a real value here and
    # not an omission: it is how a recorder says the last apply refused nothing, which is
    # what clears a stale warning off the page.
    if body.rejected is not None:
        recorder.recorder_rejected = _scrub(dict(body.rejected)) or None
        recorder.reported_at = now
    if body.capabilities is not None:
        # Only the `recorder` half is ours; the tuners carry their own in their entries.
        block = body.capabilities.get("recorder")
        recorder.recorder_capabilities = _scrub(block) if isinstance(block, dict) else None
        recorder.reported_at = now
    db.flush()

    upserted = 0
    skipped = 0
    for entry in body.tuners:
        try:
            key = entry.tuner_key
            if not key:
                skipped += 1
                continue
            reported = _scrub(entry.model_dump(mode="json"))
            # ON CONFLICT DO UPDATE: race-free against a concurrent status or PUT creating
            # the same tuner_key, and it never touches desired/desired_version.
            stmt = pg_insert(Tuner).values(
                recorder_id=recorder.id, tuner_key=key, reported=reported, reported_at=now
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_tuners_recorder_key",
                set_={"reported": stmt.excluded.reported, "reported_at": stmt.excluded.reported_at},
            )
            db.execute(stmt)
            upserted += 1
        except Exception:  # noqa: BLE001 - a bad tuner entry must not fail the whole push
            log.warning("tuner status: skipping malformed entry", exc_info=True)
            skipped += 1

    db.flush()
    # Identifiers only, per the 8 KB pg_notify rule: the operator page re-reads the recorder.
    notify(db, "tuner.status", {"recorder_id": recorder.id})
    db.commit()
    # This is the one thing that changes a recorder's tuner-key set, so the spectrum path's
    # cache of it is dropped here rather than waited out: a tuner that has just announced
    # itself must not have its first seconds of frames rejected as unknown.
    forget_tuner_keys(recorder.instance_id)
    return TunerStatusAccepted(
        ok=True, recorder_id=recorder.id, tuners_upserted=upserted, tuners_skipped=skipped
    )


#: How long a recorder's tuner-key set is reused for.  The set changes only when a status
#: push creates a tuner, which is a once-per-recorder-lifetime event; a new tuner starts
#: being accepted within this window.  Short enough that nobody debugging a fresh tuner
#: notices, long enough that a recorder streaming spectrum at 10 frames a second asks the
#: database twice a minute instead of ten times a second.
_TUNER_KEYS_TTL_S = 5.0

_tuner_keys_lock = threading.Lock()
_tuner_keys_cache: dict[str, tuple[float, set[str]]] = {}


def forget_tuner_keys(instance_id: str | None = None) -> None:
    """Drop the cached tuner keys for one recorder, or for all of them."""
    with _tuner_keys_lock:
        if instance_id is None:
            _tuner_keys_cache.clear()
        else:
            _tuner_keys_cache.pop(instance_id, None)


def _known_tuner_keys(db: Session, instance_id: str) -> set[str]:
    """The tuner keys keyup knows for a recorder — empty if the recorder is unknown.

    A spectrum frame naming a tuner outside this set is ignored: the frame path never
    creates rows (that is the status push's job), so an unknown instance/tuner simply has
    nowhere to belong and is dropped rather than allowed to grow the in-process cache.

    Cached, because this is the *only* database work anywhere on the spectrum path and it
    was being done per POST — which, for a recorder that posts one frame at a time, is per
    frame.  The point of :mod:`app.spectrum` is that a live spectrum costs the database
    nothing (the frames are ephemeral, never stored, never routed through NOTIFY); a
    join per frame quietly undid that, and during the incident it is what made the
    recorder's POSTs average 70 ms and peak at 10.5 s while Postgres was saturated.
    """
    now = time.monotonic()
    with _tuner_keys_lock:
        entry = _tuner_keys_cache.get(instance_id)
    if entry is not None and now - entry[0] < _TUNER_KEYS_TTL_S:
        return entry[1]
    keys = set(
        db.execute(
            select(Tuner.tuner_key)
            .join(Recorder, Tuner.recorder_id == Recorder.id)
            .where(Recorder.instance_id == instance_id)
        ).scalars()
    )
    with _tuner_keys_lock:
        _tuner_keys_cache[instance_id] = (time.monotonic(), keys)
    return keys


@router.post(
    "/recorder/spectrum",
    response_model=SpectrumAccepted,
    summary="A recorder streams a tuner's live spectrum",
    description=(
        "One or more ephemeral FFT frames for a tuner: a center/span and `bins` signed-byte "
        "dBFS values. Post either a single frame object or `{\"frames\": [...]}` to batch. Frames "
        "are never stored and never routed through the database — keyup caches only the latest "
        "per tuner and fans it out to any live viewer over `GET /admin/recorders/spectrum/stream`. "
        "A frame for a tuner keyup does not yet know (no status push has created it) is ignored. "
        "Needs the `upload` scope."
    ),
)
def post_spectrum(
    body: SpectrumFrameIn | SpectrumBatchIn,
    db: DbSession,
    token: RecorderToken,
) -> SpectrumAccepted:
    frames = body.frames if isinstance(body, SpectrumBatchIn) else [body]
    accepted = 0
    skipped = 0
    # One tuner-key lookup per distinct instance in the batch, reused across its frames.
    known: dict[str, set[str]] = {}
    for frame in frames:
        keys = known.get(frame.instance_id)
        if keys is None:
            keys = _known_tuner_keys(db, frame.instance_id)
            known[frame.instance_id] = keys
        if frame.tuner_key not in keys:
            skipped += 1
            continue
        spectrum_hub.ingest(frame.model_dump())
        accepted += 1
    return SpectrumAccepted(ok=True, accepted=accepted, skipped=skipped)


def _reject(field: str, reason: str) -> HTTPException:
    """A 422 that names the field, in the shape the validation handler already produces.

    ``app.main``'s ``RequestValidationError`` handler flattens pydantic's errors to
    ``"field: message"``; a hand-written check that reads differently would make one form
    report two dialects of the same refusal.  So these read the same way, and the dialog can
    attach a message to the input it came from by splitting on the first colon.
    """
    return HTTPException(status_code=422, detail=f"{field}: {reason}")


def _number(value: Any) -> float | None:
    """``value`` as a float when it is a real number — bools excluded, NaN excluded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None  # noqa: PLR0124 - NaN != NaN


def _gain_elements(reported: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The reported gain stages, keyed by name — empty when the tuner has not named any."""
    raw = reported.get("gain_elements")
    if not isinstance(raw, list):
        return {}
    elements: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            elements[entry["name"]] = entry
    return elements


def _check_backend(changes: dict[str, Any], reported: dict[str, Any]) -> None:
    """Refuse a field the reported backend does not have.

    Only when the backend is actually known: a tuner that has never reported is not going to
    be told what it does not support by a page that has no idea what it is.  Everything an
    unknown backend is sent goes through, and trunkd answers with ``rejected`` if it was
    wrong — which is precisely what ``rejected`` is for.
    """
    backend = reported.get("backend")
    if not isinstance(backend, str) or not backend.strip():
        return
    is_hackrf = backend.strip().lower() == "hackrf"
    wrong = SOAPY_ONLY_TUNER_FIELDS if is_hackrf else HACKRF_ONLY_TUNER_FIELDS
    other = "a Soapy device" if is_hackrf else "a HackRF"
    for field in wrong:
        if changes.get(field) is not None:
            raise _reject(field, f"only {other} has this; this tuner reports backend {backend!r}")


def _check_gains(changes: dict[str, Any], reported: dict[str, Any]) -> None:
    """Per-element gain against the elements the tuner named, and overall gain against their sum.

    A tuner that has not reported its ``gain_elements`` cannot have per-element gain written
    at all, and that is the honest answer rather than an unhelpful one: the element names are
    the driver's, keyup has no list of its own to guess from, and a key the device does not
    have is a setting that silently does nothing.
    """
    gains = changes.get("gains")
    elements = _gain_elements(reported)
    if isinstance(gains, dict) and gains:
        if not elements:
            raise _reject(
                "gains",
                "this tuner has not reported which gain elements it has; set the overall gain instead",
            )
        for name, value in gains.items():
            element = elements.get(name)
            if element is None:
                known = ", ".join(sorted(elements)) or "none"
                raise _reject("gains", f"{name!r} is not one of this tuner's elements ({known})")
            number = _number(value)
            if number is None:
                raise _reject("gains", f"{name!r}: the gain must be a number of dB")
            low, high = _number(element.get("min")), _number(element.get("max"))
            if low is not None and high is not None and not (low <= number <= high):
                raise _reject("gains", f"{name!r}: {number:g} dB is outside {low:g}…{high:g} dB")

    gain = _number(changes.get("gain"))
    # The overall range is the sum of the element ranges — which is what an overall gain
    # *is* on a Soapy device: the driver distributes it across the same stages.  Checked only
    # when every element named both ends, because a partial sum is a wrong sum.
    if gain is not None and elements:
        lows = [_number(e.get("min")) for e in elements.values()]
        highs = [_number(e.get("max")) for e in elements.values()]
        if all(v is not None for v in lows) and all(v is not None for v in highs):
            low, high = sum(lows), sum(highs)  # type: ignore[arg-type]
            if not (low <= gain <= high):
                raise _reject("gain", f"{gain:g} dB is outside this tuner's {low:g}…{high:g} dB")


def _check_hackrf_steps(changes: dict[str, Any]) -> None:
    """LNA in 8 dB steps to 40, VGA in 2 dB steps to 62 — libhackrf's own granularity.

    Off-step values are not refused by the hardware; they are silently rounded, which is the
    worst of both worlds: the page says 25 and the front end is at 24 for ever.
    """
    for field, ceiling, step in (
        ("lna_db", HACKRF_LNA_MAX_DB, HACKRF_LNA_STEP_DB),
        ("vga_db", HACKRF_VGA_MAX_DB, HACKRF_VGA_STEP_DB),
    ):
        value = changes.get(field)
        if value is None:
            continue
        number = _number(value)
        if number is None or number != int(number):
            raise _reject(field, "must be a whole number of dB")
        whole = int(number)
        if not (0 <= whole <= ceiling):
            raise _reject(field, f"must be between 0 and {ceiling} dB")
        if whole % step:
            raise _reject(field, f"must be a multiple of {step} dB")


def _check_control_frequencies(changes: dict[str, Any], reported: dict[str, Any]) -> None:
    """Candidates on the channel grid, and inside the span when the tuner has reported one."""
    values = changes.get("control_frequencies")
    if not isinstance(values, list) or not values:
        return
    center = _number(reported.get("center_hz"))
    span = _number(reported.get("span_hz"))
    low = high = None
    if center is not None and span is not None and span > 0:
        low, high = center - span / 2, center + span / 2
    for index, raw in enumerate(values):
        number = _number(raw)
        if number is None or number != int(number) or number <= 0:
            raise _reject(f"control_frequencies[{index}]", "must be a frequency in whole hertz")
        hz = int(number)
        if hz % CHANNEL_STEP_HZ:
            raise _reject(
                f"control_frequencies[{index}]",
                f"{hz / 1e6:.4f} MHz is not a multiple of {CHANNEL_STEP_HZ} Hz",
            )
        if low is not None and high is not None and not (low <= hz <= high):
            raise _reject(
                f"control_frequencies[{index}]",
                f"{hz / 1e6:.4f} MHz is outside the tuner's span "
                f"({low / 1e6:.4f}–{high / 1e6:.4f} MHz)",
            )


def _check_sample_rate(changes: dict[str, Any]) -> None:
    """``sample_rate / 12500`` an even whole number of at least four channels."""
    value = changes.get("sample_rate")
    if value is None:
        return
    number = _number(value)
    if number is None or number != int(number) or number <= 0:
        raise _reject("sample_rate", "must be a rate in whole samples per second")
    rate = int(number)
    if rate % CHANNEL_STEP_HZ:
        raise _reject("sample_rate", f"must be a multiple of {CHANNEL_STEP_HZ} Hz")
    channels = rate // CHANNEL_STEP_HZ
    if channels % 2:
        raise _reject(
            "sample_rate",
            f"{rate} Hz is {channels} channels of {CHANNEL_STEP_HZ} Hz; the count must be even",
        )
    if channels < MIN_SAMPLE_RATE_CHANNELS:
        raise _reject(
            "sample_rate",
            f"must cover at least {MIN_SAMPLE_RATE_CHANNELS} channels "
            f"({CHANNEL_STEP_HZ * MIN_SAMPLE_RATE_CHANNELS} Hz)",
        )


def validate_tuner_settings(changes: dict[str, Any], reported: dict[str, Any]) -> None:
    """Refuse a desired write the tuner's last *reported* state says cannot work.

    Every rule here is trunkd's own, restated against what this particular tuner said it is.
    keyup checks them at the edit for the reason it checks ``bands`` there: trunkd drops a
    field it cannot use and carries on, so the operator's only evidence of a bad value is a
    setting that never takes — and by then they have closed the dialog.

    Deliberately permissive where the tuner has said nothing.  No reported backend means no
    backend rule; no reported ``gain_elements`` means no element check; no reported span
    means control candidates are only checked against the channel grid.  A tuner that has
    never pushed a status is configurable, not frozen, and whatever keyup could not check
    comes back as ``rejected`` from the recorder.

    A cleared field (an explicit ``null``) is never refused: withdrawing an opinion is always
    allowed, whatever the tuner turns out to be.
    """
    _check_backend(changes, reported)
    _check_gains(changes, reported)
    _check_hackrf_steps(changes)
    _check_control_frequencies(changes, reported)
    _check_sample_rate(changes)

    for field in ("ppm", "auto_ppm_limit"):
        value = changes.get(field)
        if value is None:
            continue
        number = _number(value)
        if number is None:
            raise _reject(field, "must be a number of ppm")
        low = -MAX_PPM if field == "ppm" else 0.0
        if not (low <= number <= MAX_PPM):
            raise _reject(field, f"must be between {low:g} and {MAX_PPM:g} ppm")

    bandwidth = changes.get("bandwidth_hz")
    if bandwidth is not None:
        number = _number(bandwidth)
        if number is None or number != int(number) or number < 0:
            raise _reject("bandwidth_hz", "must be a bandwidth in whole hertz, or 0 for the driver's own")


def lock_recorder(db: Session, recorder_id: int) -> Recorder | None:
    """The recorder row, locked ``FOR UPDATE`` — or ``None`` when there is no such row.

    Every writer of a tuner's desired state takes this first.  It serializes the Python
    read-modify-write of the two version counters below, so two writes to *different*
    tuners of one recorder cannot both read ``recorder.version = 4`` and both store 5.  The
    arbiter takes exactly the same lock as the operator's ``PUT``, which is the point: a
    background job and a person editing the same recorder are simply two writers queueing.

    The lock alone is not enough, which is why the read is ``populate_existing`` and the
    tuners are re-read behind it.  A caller may have loaded these rows *before* taking the
    lock — the arbiter loads every recorder and every tuner at the top of its pass and locks
    one of them several statements later — and a plain ``SELECT … FOR UPDATE`` hands back
    the objects already in the identity map, attributes and all, without looking at what it
    just read.  The read-modify-write would then merge over a snapshot from before the lock
    and quietly undo whatever an operator committed in between.  Refreshing here means every
    writer merges over the committed state, which is the guarantee the lock is taken for.
    """
    recorder = db.scalar(
        select(Recorder)
        .where(Recorder.id == recorder_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if recorder is None:
        return None
    # The collection is expired so a tuner some other transaction added is seen at all, and
    # the rows themselves are re-read so the ones already loaded carry committed values.
    db.expire(recorder, ["tuners"])
    db.scalars(
        select(Tuner)
        .where(Tuner.recorder_id == recorder.id)
        .execution_options(populate_existing=True)
    ).all()
    return recorder


def apply_tuner_desired(
    db: Session,
    recorder: Recorder,
    tuner_key: str,
    changes: dict[str, Any],
    *,
    updated_by: str,
) -> Tuner:
    """Merge ``changes`` onto one tuner's desired state and bump both version counters.

    The one code path that writes ``tuners.desired``: the operator's ``PUT`` and the
    arbiter both come through here, so there is exactly one implementation of the merge
    rule (a value sets, ``None`` clears, an absent key is left alone), of the
    ``desired_version`` / ``recorder.version`` bump, and of the bus announcement.  The
    caller must already hold ``recorder`` from :func:`lock_recorder`, and owns the commit.
    """
    tuner = _get_or_create_tuner(db, recorder.id, tuner_key)
    desired: dict[str, Any] = dict(tuner.desired or {})
    for field, value in changes.items():
        if value is None:
            desired.pop(field, None)
        else:
            desired[field] = value
    tuner.desired = _scrub(desired)
    tuner.desired_version = (tuner.desired_version or 0) + 1
    tuner.updated_by = updated_by

    recorder.version = (recorder.version or 0) + 1
    db.flush()
    notify(db, "tuner.status", {"recorder_id": recorder.id})
    return tuner


def _rejected(tuner: Tuner) -> dict[str, Any] | None:
    """The fields this tuner's recorder refused, lifted out of ``reported``.

    Surfaced as a field of its own rather than left for the client to dig out of the
    reported blob, because it is the *only* trace a refusal leaves: trunkd acknowledges the
    desired version whether or not it could use every field in it, so ``drift`` clears and
    the operator would otherwise see a clean save that did nothing.
    """
    value = (tuner.reported or {}).get("rejected")
    return value if isinstance(value, dict) and value else None


def tuner_out(tuner: Tuner) -> TunerOut:
    """One tuner as the admin API renders it."""
    return TunerOut(
        id=tuner.id,
        tuner_key=tuner.tuner_key,
        reported=tuner.reported,
        reported_at=tuner.reported_at,
        desired=tuner.desired,
        desired_version=tuner.desired_version,
        updated_by=tuner.updated_by,
        updated_at=tuner.updated_at,
        drift=_drift(tuner),
        rejected=_rejected(tuner),
    )


def recorder_out(
    recorder: Recorder,
    *,
    broadcastify: dict[str, Any] | None = None,
    skip_talkgroups: dict[str, Any] | None = None,
) -> RecorderOut:
    """One recorder and its tuners as the admin API renders it.

    ``broadcastify`` is the masked Broadcastify block — the same one for every recorder,
    because it is derived from the export settings — passed in rather than built here so a
    list of recorders costs one talkgroup query and not one per row.  ``skip_talkgroups``
    is passed the same way and for the same reason, and is a count rather than the list:
    the list is the Talkgroups page's, and a recorder row wants to say how big it is.
    """
    return RecorderOut(
        id=recorder.id,
        instance_id=recorder.instance_id,
        label=recorder.label,
        version=recorder.version,
        recorder_version=recorder.recorder_version,
        last_seen_at=recorder.last_seen_at,
        reported_at=recorder.reported_at,
        station=recorder.station,
        counters=recorder.counters,
        priority=recorder.priority,
        standby=recorder.standby,
        promoted_at=recorder.promoted_at,
        created_at=recorder.created_at,
        desired_spool_only=recorder.desired_spool_only,
        desired_recorder_version=recorder.desired_recorder_version,
        applied_recorder_version=recorder.applied_recorder_version,
        uplink=recorder.uplink,
        link=recorder.link,
        event_log=recorder.event_log,
        spool_only_pending=_recorder_pending(recorder),
        desired_event_log_archive=recorder.desired_event_log_archive,
        # `public_link` is the whole leak defence: one function takes the shared key out,
        # and every operator-facing render of a recorder goes through this one call.
        desired_link=public_link(recorder.desired_link),
        # Same defence, same shape: the block an operator reads is the block the recorder
        # is sent with the credential taken out of it.  Built here rather than stored, so
        # it is what *this* recorder would receive if it polled now.
        desired_broadcastify=broadcastify,
        desired_skip_talkgroups=skip_talkgroups,
        recorder_pending=_recorder_block_pending(recorder),
        recorder_applied=recorder.recorder_applied,
        recorder_rejected=recorder.recorder_rejected,
        recorder_capabilities=recorder.recorder_capabilities,
        # Per worker: the push-channel registry is in this process's memory.
        stream=RecorderStreamOut(**stream_info(recorder.instance_id)),
        tuners=[tuner_out(t) for t in sorted(recorder.tuners, key=lambda t: t.tuner_key)],
    )


# ---------------------------------------------------------------------- operator ---


@admin_router.get(
    "/recorders",
    response_model=list[RecorderOut],
    summary="Recorders and their tuners (admin)",
    description=(
        "Every recorder keyup manages, each with its tuners: what the recorder last reported, "
        "what an operator wants (`desired`), and a `drift` flag that is set when the tuner has "
        "not yet applied the desired version. Each recorder also carries its upload hold — "
        "`desired_spool_only`, `desired_recorder_version`, `applied_recorder_version`, the "
        "`uplink` it reports and a `spool_only_pending` flag that is the recorder-level twin of "
        "a tuner's `drift`; the `link` it reports about its peers and the `event_log` it "
        "reports about its own archive backlog; the `station` its operator described, the "
        "`counters` it has run up, and its standby policy — "
        "`standby` (hot/warm/cold), `priority` (lower is preferred) and `promoted_at`, which "
        "is set only while the arbiter is the one holding this recorder on. Admin only."
    ),
)
def list_recorders(db: DbSession, principal: AdminPrincipal) -> list[RecorderOut]:
    recorders = db.scalars(select(Recorder).order_by(Recorder.instance_id)).all()
    masked = masked_broadcastify(db)
    skipped = skip_talkgroups_summary(db)
    return [
        recorder_out(recorder, broadcastify=masked, skip_talkgroups=skipped)
        for recorder in recorders
    ]


@admin_router.get(
    "/recorders/stream",
    summary="Recorder/tuner changes as Server-Sent Events (admin)",
    description=(
        "Tails the live bus for tuner status and desired-state changes. Each one is emitted as "
        "an `event: tuner.status` frame carrying only the `recorder_id`; the client re-reads "
        "`GET /admin/recorders`. A comment heartbeat is sent when the feed is quiet. Admin only; "
        "authenticates with the session cookie."
    ),
    responses={200: {"content": {"text/event-stream": {}}}},
    dependencies=[Depends(require_admin)],
)
async def stream_recorders(request: Request, _released: ReleasedSession = None) -> StreamingResponse:
    heartbeat = float(get_settings().live_status_seconds)

    async def events() -> AsyncIterator[bytes]:
        yield b": connected\n\n"
        subscriber = bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if event.get("event") != "tuner.status":
                    continue
                frame = {"recorder_id": event.get("recorder_id")}
                yield b"event: tuner.status\ndata: " + orjson.dumps(frame) + b"\n\n"
        finally:
            bus.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@admin_router.get(
    "/recorders/spectrum/stream",
    summary="A tuner's live spectrum as Server-Sent Events (admin)",
    description=(
        "Streams the FFT frames a recorder posts for one tuner, selected by `instance` (the "
        "recorder's instance_id) and `tuner` (its tuner key). The last cached frame is sent "
        "immediately on connect so the view is never blank, then each new frame arrives as an "
        "`event: spectrum` frame; a comment heartbeat is sent when the tuner is quiet. Frames are "
        "never persisted. Admin only; authenticates with the session cookie."
    ),
    responses={200: {"content": {"text/event-stream": {}}}},
    dependencies=[Depends(require_admin)],
)
async def stream_spectrum(
    request: Request,
    instance: Annotated[str, Query(description="The recorder's instance_id", max_length=MAX_INSTANCE_ID_LEN)],
    tuner: Annotated[str, Query(description="The recorder's tuner key", max_length=MAX_TUNER_KEY_LEN)],
    _released: ReleasedSession = None,
) -> StreamingResponse:
    heartbeat = float(get_settings().live_status_seconds)

    async def events() -> AsyncIterator[bytes]:
        yield b": connected\n\n"
        subscriber = spectrum_hub.subscribe(instance, tuner)
        try:
            cached = spectrum_hub.latest(instance, tuner)
            if cached is not None:
                yield b"event: spectrum\ndata: " + orjson.dumps(cached) + b"\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(subscriber.queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                yield b"event: spectrum\ndata: " + orjson.dumps(frame) + b"\n\n"
        finally:
            spectrum_hub.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@admin_router.put(
    "/recorders/{recorder_id}/tuners/{tuner_key}",
    response_model=TunerOut,
    summary="Set a tuner's desired state (admin)",
    description=(
        "Writes what keyup wants one tuner to do — role, focus site, center frequency, enabled, "
        "spectrum, bands — and how its front end is set up: `gain`/`gains`/`agc`, `ppm` with "
        "`auto_ppm`/`auto_ppm_limit`, `bandwidth_hz`, `rx_port`, raw Soapy `settings`, the "
        "HackRF `lna_db`/`vga_db`/`amp`, `sample_rate`, `data_follow` and the "
        "`control_frequencies` to hunt. The given fields are merged onto whatever was there "
        "(omitted leaves it, a value sets it, an explicit null clears it). Bumps the tuner's "
        "`desired_version` and the recorder's `version`, and announces `tuner.status` on the bus. "
        "Refused with a 422 naming the field: a role outside auto|control|voice|data; a center "
        "frequency outside the reported span; a `bands` entry that is neither a known label nor "
        "an explicit `lo-hi` range in Hz; `ppm` beyond ±200; a `gains` key the tuner did not "
        "report in `gain_elements`, or a value outside that element's range; an overall `gain` "
        "outside the sum of those ranges; `lna_db` off its 8 dB step to 40 or `vga_db` off its "
        "2 dB step to 62; a control candidate that is not a multiple of 12 500 Hz or falls "
        "outside the span; a `sample_rate` that is not an even whole number of at least four "
        "12.5 kHz channels; and a Soapy-only field on a tuner reporting the `hackrf` backend or "
        "a HackRF-only field on one that is not. Everything is checked against what the tuner "
        "last *reported*, so a tuner that has said nothing is checked only where it can be. "
        "Admin only; creates the tuner if it does not exist."
    ),
)
def put_tuner_desired(
    recorder_id: int,
    tuner_key: str,
    body: TunerDesiredIn,
    db: DbSession,
    principal: AdminPrincipal,
) -> TunerOut:
    if len(tuner_key) > 64:
        raise HTTPException(status_code=422, detail="tuner_key is too long")
    if body.role is not None and body.role not in TUNER_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {', '.join(TUNER_ROLES)}")

    # Lock the recorder row for the duration of the write: it serializes concurrent writes
    # to this recorder's tuners — an operator's and the arbiter's alike — so the Python
    # read-modify-write of the version counters cannot lose a bump.
    recorder = lock_recorder(db, recorder_id)
    if recorder is None:
        raise HTTPException(status_code=404, detail="No such recorder")

    tuner = _get_or_create_tuner(db, recorder_id, tuner_key)
    reported = tuner.reported or {}

    changes = body.model_dump(exclude_unset=True)
    # A new center frequency is a retune, and a retune to another band is a legitimate
    # thing to ask of a spare tuner — an idle RSPdx parked on VHF pointed at a neighbour
    # site's 800 MHz control channel is exactly the case.  It used to be refused whenever
    # the new center fell outside the *current* span, which made every band change
    # impossible from here.  The candidates and bands are instead checked against the span
    # the tuner will have: the requested center, and the requested or reported width.
    if body.center_frequency is not None:
        width = _number(changes.get("sample_rate")) or _number(reported.get("span_hz"))
        reported = {**reported, "center_hz": body.center_frequency}
        if width:
            reported["span_hz"] = width
    # Everything the tuner's own reported state can rule out — the wrong backend's fields,
    # a gain element it does not have, an off-step HackRF gain, a control candidate outside
    # the span, a sample rate the channelizer cannot use.  Before the write, so a refused
    # edit leaves the desired state exactly as it was rather than half applied.
    validate_tuner_settings(changes, reported)

    # Merge: an omitted field is left alone, a value sets it, an explicit null clears it.
    tuner = apply_tuner_desired(db, recorder, tuner_key, changes, updated_by=principal.label)
    instance_id = recorder.instance_id
    db.commit()
    # After the commit, never before: a woken connection re-reads the versions immediately
    # and must see the ones this write stored.  Only this worker's sockets hear it; every
    # other worker's poll picks the same change up within a second.
    announce_desired_change(instance_id)
    return tuner_out(tuner)


#: Every table that keys a row by the recorder's *instance text* rather than by its row id.
#: A rename has to rewrite all of them or the archive splits in two at the moment of the
#: rename — the old name's days on one side, the new name's on the other, and no query that
#: joins them.  The list is deliberately explicit rather than derived: a new table keyed by
#: instance text is a decision someone should have to come here and record.
INSTANCE_TEXT_COLUMNS: tuple[tuple[type[Any], Any], ...] = (
    (RadioEvent, RadioEvent.instance),
    (RecorderObservation, RecorderObservation.instance),
    (EventLogArchive, EventLogArchive.instance),
    (Call, Call.source_instance),
)


def _rename_instance(db: Session, recorder: Recorder, wanted: str, *, by: str) -> None:
    """Rename a recorder's ``instance_id`` and carry its archive across with it.

    The rename and the rewrites are one transaction on purpose.  A half-done rename is the
    worst outcome available here: the roster would show the new name and every event, every
    observation and every archived day would still be filed under the old one, with nothing
    to say they belong together.

    A name another recorder already holds is a 409 rather than a merge.  Merging two
    recorders is a different operation with real questions in it (which label wins, which
    standby class, what happens to two rows of tuners), and the common case by far is a
    stale empty row left by a box that was renamed on its own side first — so the message
    says to delete that one and points at the route that does it.
    """
    wanted = wanted.strip()
    if not wanted:
        raise _reject("instance_id", "must not be empty")
    if wanted == recorder.instance_id:
        return
    clash = db.scalar(select(Recorder).where(Recorder.instance_id == wanted))
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"another recorder (#{clash.id}) is already called {wanted!r}. That is usually "
                "an empty row left behind by a box that was renamed on its own side first — "
                f"delete it with DELETE /api/admin/recorders/{clash.id} and try again."
            ),
        )
    was = recorder.instance_id
    recorder.instance_id = wanted
    try:
        # Flushed before the rewrites so the unique constraint — not the SELECT above, which
        # another transaction could have raced — is what decides. A clash caught here is the
        # same 409 rather than a 500 from a constraint nobody was expecting to fire.
        db.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail=f"another recorder is already called {wanted!r}"
        ) from None
    moved: dict[str, int] = {}
    for model, column in INSTANCE_TEXT_COLUMNS:
        result = db.execute(update(model).where(column == was).values({column: wanted}))
        if result.rowcount:
            moved[model.__tablename__] = result.rowcount
    activity.record(
        db,
        source="system",
        level="info",
        kind="recorder_renamed",
        message=f"Renamed recorder {was} to {wanted}",
        meta={"recorder_id": recorder.id, "from": was, "to": wanted, "moved": moved, "by": by},
    )
    log.info("recorder %s renamed to %s, moved %s (%s)", was, wanted, moved, by)


@admin_router.put(
    "/recorders/{recorder_id}",
    response_model=RecorderOut,
    summary="Set a recorder's policy and desired state (admin)",
    description=(
        "Writes what this recorder is *for*: its `label`, its `priority` (lower is preferred "
        "when several recorders could cover one site) and its `standby` class — `hot` records "
        "whenever it is healthy, `warm` waits with its tuners disabled until the arbiter "
        "promotes it, `cold` is never touched automatically.\n\n"
        "It also writes the recorder's own **desired state**, the three settings that are the "
        "instance's business rather than a tuner's: `spool_only` (the upload hold), "
        "`event_log_archive` (ship the daily raw event log here) and `link` (the whole "
        "recorder-to-recorder block). All three are three-valued — a value, or an explicit "
        "`null` that withdraws keyup's opinion and leaves the recorder on its own config, or "
        "the key left out of the body entirely, which changes nothing. Every write that "
        "actually changes one of them bumps `desired_recorder_version`, which the recorder "
        "echoes back once it has applied it; a write of the value already stored bumps "
        "nothing.\n\n"
        "`link` is written as a unit and refused with a 422 naming the field: `bind` and each "
        "peer must be `host:port` (a hostname, an IPv4 address, or an IPv6 one in brackets, "
        "port 1…65535), peers must not repeat, an enabled link needs at least one peer, the "
        "shared key must be at least 16 characters and `hello_interval_s` must be 1…60. "
        "Omitting `key` from an update keeps the stored one. The key never comes back here — "
        "`desired_link` renders `key_set` and `key_fingerprint` instead.\n\n"
        "`instance_id` renames the recorder and rewrites the instance text on everything it "
        "has already filed; a name another recorder holds is a 409. Omitted fields are left "
        "alone. This never touches a tuner's desired state: only the arbiter turns policy "
        "into tuner writes, and only by flipping `enabled`. Admin only."
    ),
)
def put_recorder_policy(
    recorder_id: int,
    body: RecorderPolicyIn,
    db: DbSession,
    principal: AdminPrincipal,
) -> RecorderOut:
    recorder = lock_recorder(db, recorder_id)
    if recorder is None:
        raise HTTPException(status_code=404, detail="No such recorder")

    changes = body.model_dump(exclude_unset=True)
    was_called = recorder.instance_id
    if changes.get("instance_id") is not None:
        _rename_instance(db, recorder, changes["instance_id"], by=principal.label)
    if "label" in changes:
        recorder.label = (changes["label"] or "").strip() or None
    if changes.get("priority") is not None:
        recorder.priority = int(changes["priority"])
    if changes.get("standby") is not None:
        standby = changes["standby"]
        if standby not in RECORDER_STANDBY:
            raise HTTPException(
                status_code=422, detail=f"standby must be one of {', '.join(RECORDER_STANDBY)}"
            )
        # Leaving the `hot`/`warm` pair for `cold` means "hands off from now on", so any
        # promotion the arbiter is still holding is disowned rather than left to be undone
        # by a job that will never look at this recorder again.  The tuners are left exactly
        # as they are: an operator who wants them off says so on the tuner.
        if standby == "cold":
            recorder.promoted_at = None
        recorder.standby = standby
    # `spool_only` is the one field whose explicit null is a write rather than "unset": it
    # is the third state, "keyup has no opinion".  So the key's *presence* decides, not the
    # value, and a write that changes nothing does not bump the version the recorder polls.
    #
    # The three of them share one counter and one rule: the *presence* of the key decides,
    # the value may be null, and only a real change bumps.  `bumped` collects what actually
    # moved so the version rises once for a write that changed two of them, not twice — the
    # recorder applies the block as a whole and a counter that outran it would leave the
    # page pending against a state the recorder had already reached.
    bumped: list[str] = []
    if "spool_only" in changes:
        wanted = changes["spool_only"]
        if wanted is not None and not isinstance(wanted, bool):  # pragma: no cover - pydantic
            raise HTTPException(status_code=422, detail="spool_only must be true, false or null")
        if wanted != recorder.desired_spool_only:
            recorder.desired_spool_only = wanted
            bumped.append(f"spool_only={wanted}")
    if "event_log_archive" in changes:
        wanted = changes["event_log_archive"]
        if wanted != recorder.desired_event_log_archive:
            recorder.desired_event_log_archive = wanted
            bumped.append(f"event_log_archive={wanted}")
    if "link" in changes:
        raw = changes["link"]
        if raw is None:
            if recorder.desired_link is not None:
                recorder.desired_link = None
                bumped.append("link=unmanaged")
        else:
            stored = recorder.desired_link if isinstance(recorder.desired_link, dict) else {}
            existing_key = stored.get("key") if isinstance(stored.get("key"), str) else None
            try:
                # Normalized before it is compared, so "192.0.2.30:9000 " and "192.0.2.30:9000"
                # are one value and a resubmitted form does not bump the version for a space.
                block = normalize_link(raw, existing_key=existing_key)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            if block != stored:
                recorder.desired_link = block
                bumped.append(
                    f"link={'on' if block['enabled'] else 'off'} "
                    f"bind={block['bind']} peers={len(block['peers'])} "
                    f"key={link_key_fingerprint(block['key'])}"
                )
    if bumped:
        recorder.desired_recorder_version = (recorder.desired_recorder_version or 0) + 1
        log.info(
            "recorder %s desired: %s v%s (%s)",
            recorder.instance_id,
            ", ".join(bumped),
            recorder.desired_recorder_version,
            principal.label,
        )

    db.flush()
    notify(db, "tuner.status", {"recorder_id": recorder.id})
    instance_id = recorder.instance_id
    db.commit()
    # A rename changes the key the recorder connects under, so the socket still registered
    # under the old name is nudged too — it reads the row back by id and sees the new name.
    announce_desired_change(was_called)
    announce_desired_change(instance_id)
    log.info(
        "recorder %s policy: standby=%s priority=%s (%s)",
        recorder.instance_id,
        recorder.standby,
        recorder.priority,
        principal.label,
    )
    return recorder_out(
        recorder,
        broadcastify=masked_broadcastify(db),
        skip_talkgroups=skip_talkgroups_summary(db),
    )


#: The generated shared key's length in bytes, rendered as twice as many hex characters.
#: 32 bytes because the key is an HMAC secret and there is no reason to be stingy with one
#: nobody has to type: the button exists precisely so that nobody types it.
LINK_KEY_BYTES = 32


@admin_router.post(
    "/recorders/{recorder_id}/link/generate-key",
    response_model=RecorderLinkKeyGenerated,
    summary="Generate a shared link key (admin)",
    description=(
        "Returns 32 random bytes as 64 hex characters, for the operator to paste into the "
        "link settings of **every** recorder in the mesh — they authenticate to each other "
        "with one shared secret, so a key that is not identical on both ends is a link that "
        "never comes up.\n\n"
        "Generating is deliberately **not** saving: nothing is written, and the key becomes "
        "this recorder's only when the settings are saved with it. A route that stored it "
        "would break a working link the moment someone pressed the button and then thought "
        "better of it. Admin only."
    ),
)
def generate_link_key(
    recorder_id: int,
    db: DbSession,
    principal: AdminPrincipal,
) -> RecorderLinkKeyGenerated:
    if db.get(Recorder, recorder_id) is None:
        raise HTTPException(status_code=404, detail="No such recorder")
    return RecorderLinkKeyGenerated(key=secrets.token_hex(LINK_KEY_BYTES))


@admin_router.get(
    "/recorders/{recorder_id}/link/key",
    response_model=RecorderLinkKeyOut,
    summary="Reveal a recorder's shared link key (admin)",
    description=(
        "The **only** route that returns the link's shared secret in clear to an operator. "
        "Every other read of a recorder renders `key_set` and `key_fingerprint` (the first 8 "
        "hex of its sha256) instead, which is enough to see that two peers hold the same key "
        "and nothing more — so the secret is fetched deliberately, by a dialog the operator "
        "opened, rather than carried in every list of recorders a page happens to load.\n\n"
        "The read is recorded in the activity log, as a read of a secret should be. Admin only."
    ),
)
def get_link_key(
    recorder_id: int,
    db: DbSession,
    principal: AdminPrincipal,
) -> RecorderLinkKeyOut:
    recorder = db.get(Recorder, recorder_id)
    if recorder is None:
        raise HTTPException(status_code=404, detail="No such recorder")
    block = recorder.desired_link if isinstance(recorder.desired_link, dict) else {}
    key = block.get("key") if isinstance(block.get("key"), str) else None
    activity.record(
        db,
        source="system",
        level="info",
        kind="recorder.link_key_read",
        message=f"Read the link key for {recorder.label or recorder.instance_id}",
        meta={
            "recorder_id": recorder_id,
            "instance_id": recorder.instance_id,
            # The fingerprint, never the key: an audit trail that quoted the secret would be
            # a second place it lives, and one nobody thinks of as sensitive.
            "key_fingerprint": link_key_fingerprint(key),
            "by": principal.label,
        },
    )
    db.commit()
    return RecorderLinkKeyOut(
        key=key, key_set=bool(key), key_fingerprint=link_key_fingerprint(key)
    )


#: A recorder heard from within this window is live, and forgetting it needs ``force``.
FORGET_GRACE_S = 3600


@admin_router.delete(
    "/recorders/{recorder_id}",
    status_code=204,
    summary="Forget a recorder (admin)",
    description=(
        "Removes a recorder and its tuners from the roster. Nothing it recorded is touched — "
        "calls carry the recorder's *name*, not a foreign key onto this row — so this forgets "
        "a machine that has been retired, not its archive. A recorder that has reported within "
        "the last hour is a 409 unless `force=true`: a live recorder is re-created by its very "
        "next poll, so deleting one only loses the label and standby policy an operator set on "
        "it. Admin only."
    ),
)
def forget_recorder(
    recorder_id: int,
    db: DbSession,
    principal: AdminPrincipal,
    force: Annotated[
        bool, Query(description="Forget it even though it is still reporting")
    ] = False,
) -> Response:
    recorder = db.get(Recorder, recorder_id)
    if recorder is None:
        raise HTTPException(status_code=404, detail="No such recorder")
    seen = recorder.last_seen_at or recorder.reported_at
    age = (datetime.now(UTC) - seen).total_seconds() if seen is not None else None
    if not force and age is not None and age < FORGET_GRACE_S:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{recorder.instance_id} reported {int(age)}s ago and would come straight back "
                "on its next poll; pass force=true to forget it anyway"
            ),
        )
    activity.record(
        db,
        source="system",
        level="warn",
        kind="recorder_deleted",
        message=f"Forgot recorder {recorder.label or recorder.instance_id}",
        meta={
            "recorder_id": recorder_id,
            "instance_id": recorder.instance_id,
            "last_seen_at": seen.isoformat() if seen is not None else None,
            "forced": force,
            "by": principal.label,
        },
    )
    db.delete(recorder)
    db.commit()
    log.info("recorder %s forgotten (%s)", recorder.instance_id, principal.label)
    return Response(status_code=204)


# ---------------------------------------------------------------- band occupancy ---


def _occupancy_for(observation: RecorderObservation, site: Site | None) -> SiteOccupancy:
    """Fold one observation's band plan and traffic into a band strip."""
    payload = observation.payload or {}
    bands = [
        {
            "id": band.get("id"),
            "base_hz": band.get("base_hz"),
            "spacing_hz": band.get("spacing_hz"),
            "bandwidth_hz": band.get("bandwidth_hz"),
        }
        for band in payload.get("bands", [])
        if isinstance(band, dict)
    ]

    channels: list[dict[str, Any]] = []
    seen: set[int] = set()

    control = payload.get("control")
    if isinstance(control, dict) and isinstance(control.get("frequency_hz"), int):
        seen.add(control["frequency_hz"])
        channels.append(
            {
                "frequency_hz": control["frequency_hz"],
                "channel": control.get("channel"),
                "role": "control",
                "grants": 0,
                "calls": 0,
                "last_seen": observation.observed_at,
            }
        )

    data = payload.get("data_channel")
    data_freqs = set()
    if isinstance(data, dict):
        for freq in data.get("channels", []) or []:
            if isinstance(freq, int):
                data_freqs.add(freq)

    for entry in payload.get("traffic", []):
        if not isinstance(entry, dict):
            continue
        freq = entry.get("frequency_hz")
        if not isinstance(freq, int) or freq in seen:
            continue
        seen.add(freq)
        channels.append(
            {
                "frequency_hz": freq,
                "channel": entry.get("channel"),
                "role": "data" if freq in data_freqs else "voice",
                "grants": entry.get("grants") or 0,
                "calls": entry.get("calls") or 0,
                "last_seen": entry.get("last_seen"),
            }
        )

    channels.sort(key=lambda c: c["frequency_hz"])
    return SiteOccupancy(
        site_id=observation.site_id,
        site_name=site.name if site is not None else None,
        system_id=observation.system_id,
        observed_at=observation.observed_at,
        bands=bands,
        channels=channels,
    )


@admin_router.get(
    "/occupancy",
    response_model=list[SiteOccupancy],
    summary="Per-site band occupancy",
    description=(
        "A band strip for every site, folded out of each site's most recent observation: the "
        "band plan, and one entry per known channel with its role, grants, calls and last-seen. "
        "No new recorder data — this is a read over what the observation push already stored. "
        "Admin only."
    ),
)
def occupancy(
    db: DbSession,
    principal: AdminPrincipal,
    site: Annotated[int | None, Query(description="Only this site id")] = None,
) -> list[SiteOccupancy]:
    # The latest observation per site (DISTINCT ON, ordered so the newest wins).
    stmt = (
        select(RecorderObservation)
        .where(RecorderObservation.site_id.is_not(None))
        .ext(distinct_on(RecorderObservation.site_id))
        .order_by(RecorderObservation.site_id, RecorderObservation.observed_at.desc())
    )
    if site is not None:
        stmt = stmt.where(RecorderObservation.site_id == site)
    observations = db.scalars(stmt).all()

    site_ids = [o.site_id for o in observations]
    sites = {
        s.id: s
        for s in db.scalars(select(Site).where(Site.id.in_(site_ids))).all()
    } if site_ids else {}

    result = [_occupancy_for(o, sites.get(o.site_id)) for o in observations]
    result.sort(key=lambda s: (s.site_name or "", s.site_id))
    return result


__all__ = [
    "admin_router",
    "apply_tuner_desired",
    "lock_recorder",
    "recorder_out",
    "router",
    "tuner_out",
    "validate_tuner_settings",
]
