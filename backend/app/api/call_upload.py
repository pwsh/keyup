"""``POST /api/call-upload`` — the recorder push path (ARCHITECTURE §1.6, §5.1 step 0).

sdrtrunk's "Rdio Scanner" audio module posts one ``multipart/form-data`` request per
call and decides whether it worked by **string-matching the response body**, so the
contract here is literal:

===========================  ======  ==========================================
situation                    status  body
===========================  ======  ==========================================
``test=1`` probe             200     ``incomplete call data: no talkgroup``
stored                       200     ``Call imported successfully.``
dedupe                       200     ``duplicate call rejected``
bad/unknown ``key``          401     ``invalid key``
no audio, ``encrypted=0``    400     ``incomplete call data: no audio``
backlog push, no call time   400     ``{"error": "no_call_time", …}``
queue full                   429     ``queue full, retry later``
disk below the floor         507     ``insufficient storage``
===========================  ======  ==========================================

The last two are flow control (:mod:`app.ingest.flow`), and they are the *legacy* rendering
of it: a client that sends a ``priority`` form field has opted in to a JSON body instead —
``{"error": "queue_full", "class": …, "queue": {…}, "retry_after": N}`` — with the same
status and a ``Retry-After``.  A recorder that sends no ``priority`` keeps every body above
byte for byte and is never refused for a *backlog* reason, only when the live queue itself
is over ``UPLOAD_MAX_QUEUE_LIVE`` or the disk is full.  ``priority`` also decides the
``transcription_jobs.priority`` the call is enqueued at (``urgent`` → 10, ``live`` → the
talkgroup's own, ``backlog`` → that plus 500, ``bulk`` → 900, an integer as-is) and is kept
on the row as ``calls.ingest_priority``.  Every response past the key check carries the
queue gauge in ``X-Keyup-Queue-*`` / ``X-Keyup-Accepting-Backlog`` headers, and
``GET /upload/flow`` answers the same numbers on their own — docs/RECORDERS.md
"Priority and flow control", docs/MIGRATION.md.

Dedupe is only answerable *after* the whole multipart body has been read, which for an
archive whose calls Keyup mostly already holds is days of transfer spent to be told
``duplicate call rejected``.  ``POST /upload/exists`` is the way out: a batch of sha256
digests in, the subset ``calls.sha256`` already holds out, one indexed ``SELECT`` and
nothing queued — so a migration client hashes what it was going to send anyway and skips
the bytes.

Three recorders push here.  sdrtrunk and trunk-recorder's ``rdioscanner_uploader`` are told
apart by the ``sources``/``frequencies`` lists only the latter sends, which matters because
they spell ``patches`` differently; trunkd sends trunk-recorder's shape and names itself in
``recorder`` instead (``ingest_source='trunkd'``).

An upload with ``encrypted=1`` and **no audio part** is a call, not an error: P25 encrypts
the voice payload and nothing else, so trunkd knows the talkgroup, the radios, the timing,
the algorithm and the key id of an encrypted call and has no sample to send.  The row is
written with ``audio_state='purged'`` — the vocabulary the trunk-recorder sidecar path uses
for the same fact (§1.7) — so ``GET /calls/{id}/audio`` answers ``410 Gone`` and correlation
still sees that an agency held that talkgroup at that second.  A *clear* call with no audio
is still the recorder failing to send one.

Several recorders may hear one site, and an upload may say how well *this* one heard the
call — ``sourceQuality``, a JSON object with ``instance``, ``voice_frames``, ``bad_frames``
and a level.  It is kept on the row (``calls.source_instance`` / ``source_quality``), and on
the dedupe path it is what makes a second receiver's push more than a duplicate: when both
copies carry a report, name different instances, and the incoming one wins by a real margin,
the stored call takes the new audio and the push is answered ``Call imported successfully.``
instead of ``duplicate call rejected``.  Everything else about that answer is unchanged —
the same recorder re-sending, no report on either side, too small a margin, and it is the
rejection it always was (:func:`replace_with_better_source`, docs/RECORDERS.md).

The form's ``dateTime`` is the recorder's own **call start** epoch — better than anything
the file path can tell us, which is why an uploaded call is stored with
``time_source='upload'`` rather than the file-ingest ``file_write`` (which is call *end*
minus duration).  Without it the recording answers for itself, in this order: the filename
stamp (through :func:`app.ingest.parser.build_call_record`, which prefers the ID3 frame when
the name parses), then — for a name the parser cannot read at all — the ID3 ``COMM`` ``Date``
on its own.  With none of the three, **a push at ``backlog`` (or ``bulk``) priority is refused** — 400,
``{"error": "no_call_time"}`` — because a backlog upload is a file that still exists and
can simply be sent again once whatever hid its time is fixed, and filing it under its
arrival instead is how a million backlog calls came to be stamped with their upload date
(docs/MIGRATION.md, "Timestamps").  A *live* push has no second chance and keeps the
arrival fallback, with a rate-limited WARNING and a ``call.time_fallback`` activity row
saying so.  Everything else follows the file path: the MP3 is spooled to
``uploads/.incoming/`` and renamed into ``AUDIO_ROOT/uploads/YYYY/MM/DD/<ab>/`` — the day
the *call* happened, decided once the record has resolved its start (§1.1, "Storage
layout"; :func:`file_upload`) — parsed with :func:`app.ingest.parser.build_call_record`
when it carries sdrtrunk's ID3 frames, and written with the same directory upserts,
dedupe, conversation grouping and transcription enqueue that ``app.ingest.service`` uses —
so a pushed call and a watched file end up as the same row.

**Threading.**  ``call_upload`` is a plain ``def``, deliberately, and so is everything it
calls.  Nothing in this path is asynchronous: the session is sync SQLAlchemy, the audio is
hashed and probed with blocking reads, the body is moved with ``os.rename``, and the dedupe
takes Postgres locks that can wait.  As an ``async def`` all of that ran *on the event
loop*, which meant one slow upload stopped the whole process — on 2026-09-03 a backlog push
took 122,682 ms and for that whole window the API logged nothing, answered nothing, and
every recorder timed out.  FastAPI runs a ``def`` route in the threadpool (anyio's, 40
workers by default), so a slow upload now costs one worker; the flow-control semaphore that
bounds in-flight backlog uploads is a ``threading.BoundedSemaphore`` and was always written
for this.  There is no body streaming to give up by leaving the loop: Starlette parses the
multipart body and spools the audio part to a ``SpooledTemporaryFile`` *before* the route is
entered, so :func:`app.api.uploads.stream_to_path` is a file copy whichever colour it is
painted.  Two things follow from the incident and are enforced here: every upload is timed
per stage and one that outruns ``SLOW_UPLOAD_SECONDS`` says which stage was slow
(:class:`UploadStages`), and the write transaction sets a ``lock_timeout`` so a lock held by
another process — the watcher, ``keyup reconcile``'s batch pass — costs this push a retry
rather than a connection held for minutes (:func:`guard_lock_wait`).  docs/RECORDERS.md,
"Threading and slow uploads".
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import activity
from app.api.uploads import MAX_UPLOAD_BYTES, UploadTooLarge, stream_to_path
from app.audio import INCOMING_DIR, audio_path, prune_empty_dirs, upload_partition
from app.config import Settings, get_settings
from app.db import get_session, notify
from app.ingest import flow
from app.ingest.flow import Admission, BadPriority, PriorityChoice
from app.ingest.parser import (
    AUDIO_SUFFIXES,
    CallRecord,
    CallSourceRef,
    FilenameParts,
    Id3Meta,
    ParseError,
    RadioRef,
    _localize,
    build_call_record,
    parse_id3,
    probe_audio,
)

# ``quality_score`` lives with the rest of the scoring, in ``reconcile``: reconciliation
# ranks stored copies on the same number this endpoint ranks an arriving one on, and one
# definition is the only way those two can agree.  Re-exported here because it is part of
# this module's published surface.
from app.ingest.reconcile import (
    BAD_FRAME_PENALTY,  # noqa: F401 - re-exported; it documents this endpoint's own margin
    quality_score,
    reconcile_lookup,
    reconcile_upload,
)
from app.ingest.service import (
    UnknownSystem,
    assign_conversation,
    audio_policy_for,
    enqueue_audio_process,
    enqueue_transcription,
    record_ledger,
    resolve_legacy_sysite,
    resolve_system_id,
    touch_affiliation,
    upsert_call_radios,
    upsert_patch,
    upsert_site,
    upsert_talkgroup,
    write_call_sources,
)
from app.ingest.tr_parser import is_trunk_recorder_call
from app.models import CALL_KINDS as CALL_KIND_VALUES
from app.models import ApiToken, Call, CallExport, Talkgroup
from app.runtime_settings import effective_settings
from app.schemas import MAX_INSTANCE_ID_LEN

log = logging.getLogger("app.api.call_upload")

router = APIRouter(tags=["upload"])

# --- the literal bodies sdrtrunk matches on ------------------------------------------
OK_BODY = "Call imported successfully."
DUPLICATE_BODY = "duplicate call rejected"
NO_TALKGROUP_BODY = "incomplete call data: no talkgroup"
NO_AUDIO_BODY = "incomplete call data: no audio"
#: The push named no system and the archive holds none, or several — see
#: :func:`app.ingest.service.resolve_system_id`.  Answered rather than guessed at:
#: filing traffic under the wrong system is worse than refusing it, and inventing a
#: system to file it under is worse than both.
NO_SYSTEM_BODY = "incomplete call data: no system"
#: ``call_kind`` was neither ``group`` nor ``unit``.  Refused rather than defaulted: a
#: recorder that has learned a third word for "who this was addressed to" is telling us
#: something the archive cannot store, and filing it as a group call would lose it silently.
BAD_KIND_BODY = "incomplete call data: unknown call kind"
#: ``call_kind=unit`` **and** a ``talkgroup``.  A private call is not on a talkgroup, so a
#: push carrying both is a recorder bug, and the two readings of it (file it under the
#: talkgroup, or drop the talkgroup) are both wrong in ways nothing downstream could see.
UNIT_WITH_TALKGROUP_BODY = "incomplete call data: a unit call has no talkgroup"
#: ``call_kind=unit`` with no ``target_radio_id``: the other end is what makes it a unit
#: call at all, and without it the row is a source radio on no channel.
NO_TARGET_BODY = "incomplete call data: no target radio"
INVALID_KEY_BODY = "invalid key"
TOO_LARGE_BODY = "audio file too large"
#: Flow control refuses in the recorders' own idiom when the client did not opt in to the
#: JSON contract by sending ``priority``; a client that did gets the JSON body instead.
QUEUE_FULL_BODY = "queue full, retry later"
NO_STORAGE_BODY = "insufficient storage"
#: A *backlog* push whose call time nothing in the request could supply.  Plain text so a
#: legacy client still reads something sensible; a client that sent ``priority`` — which
#: every backlog pusher does — gets the JSON body :func:`no_call_time_response` builds.
NO_TIME_BODY = "incomplete call data: no call time"
#: ``dateTime`` outside :data:`MIN_CALL_EPOCH` … now + :data:`MAX_CALL_FUTURE_S`.  Almost
#: always milliseconds sent where seconds were promised, which ``datetime.fromtimestamp``
#: answers with a ``ValueError`` from deep in the request (a 500), and the ~1e11 range of
#: which it happily reads as the year 5138.
BAD_TIME_BODY = "incomplete call data: dateTime out of range"

#: the scope an ``api_tokens`` row needs to push calls (``admin`` implies it)
UPLOAD_SCOPES = ("upload", "admin")

# --- where a pushed call's ``started_at`` came from (``CallRecord.time_basis``) ---------
#: the form's ``dateTime`` — the recorder's own call start, and the point of the push path
TIME_BASIS_FORM = "form"
#: read out of the recording itself: sdrtrunk's ID3 ``COMM`` Date, or its filename stamp
TIME_BASIS_PARSED = "parsed"
#: **nothing said when the call happened.**  The row would be filed under the moment it
#: arrived, which is not a fact about the call at all — and, unlabeled, is indistinguishable
#: from one: the endpoint writes ``time_source='upload'`` either way, so a whole archive
#: can be stamped with its upload date and nothing in the schema will say so.  That is not
#: hypothetical (docs/MIGRATION.md, "Timestamps"): the backlog migration sends no
#: ``dateTime`` on purpose, a parser bug rejected every pre-2025 filename, and a million
#: calls were silently filed on the day they were uploaded.  So it is logged, counted, and
#: for a *backlog* push — one that can simply be sent again — refused.
TIME_BASIS_ARRIVAL = "arrival"

#: At most one WARNING and one activity row per minute for arrival-stamped live pushes.
#: The failure mode this exists to catch is a *bulk* one, and a log line per call would
#: bury the archive it is warning about.
TIME_FALLBACK_INTERVAL_S = 60.0
#: ``(monotonic of the last complaint, calls suppressed since)`` — module state, because a
#: rate limit that resets per request is not one.
_time_fallback: list[float] = [0.0, 0.0]

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: What ``call_kind`` may say.  A push that omits it is a group call, which is what every
#: recorder sent before private calls were recordable (:data:`app.models.CALL_KINDS`).
CALL_KINDS = frozenset(CALL_KIND_VALUES)
DEFAULT_CALL_KIND = "group"


def parse_call_kind(raw: str | None) -> str | None:
    """``call_kind`` -> ``'group'`` / ``'unit'``, or ``None`` when it is neither.

    Empty and absent both mean ``group``: the field is new, and a recorder that has never
    heard of it is sending exactly the traffic it always sent.
    """
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_CALL_KIND
    return text if text in CALL_KINDS else None


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    """``api_tokens.token_hash`` is a plain sha256 of the token — the same scheme
    ``app.auth`` uses, so either module can verify a key the other issued."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _lookup_token(session: Session, raw: str) -> ApiToken | None:
    """Local fallback for :func:`app.auth.verify_token`."""
    token = session.execute(
        select(ApiToken).where(ApiToken.token_hash == hash_token(raw))
    ).scalar_one_or_none()
    if token is None:
        return None
    now = datetime.now(UTC)
    if token.revoked_at is not None or (token.expires_at is not None and token.expires_at <= now):
        return None
    return token


def verify_upload_key(session: Session, raw: str | None) -> ApiToken | None:
    """Resolve the ``key`` form field to a token carrying the ``upload`` scope."""
    if not raw:
        return None
    token: ApiToken | None = None
    try:  # prefer app.auth's check; the local lookup below is the fallback
        from app.auth import verify_token  # noqa: PLC0415

        token = verify_token(session, raw)
    except Exception:  # noqa: BLE001 - missing module, or a different signature
        token = None
    if token is None:
        token = _lookup_token(session, raw)
    if token is None:
        return None
    if not set(token.scopes or ()) & set(UPLOAD_SCOPES):
        return None
    token.last_used_at = datetime.now(UTC)
    return token


# --------------------------------------------------------------------------------------
# form -> CallRecord
# --------------------------------------------------------------------------------------


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


#: How far ``startMs``/``endMs`` may sit from the form's own ``dateTime`` before they are
#: disbelieved.  A recorder that disagrees with itself by more than a day is not suffering
#: clock skew, it is sending a different unit (seconds where milliseconds were promised, a
#: monotonic counter, a nanosecond stamp truncated), and the whole-second field — which the
#: push path has always trusted — is the safer of the two answers.
MS_SANITY_WINDOW_S = 86400.0
#: Unix milliseconds are 13 digits from 2001 until 2286.  A 10-digit number here is seconds
#: mislabeled and a 16-digit one is microseconds; both are refused rather than divided.
MS_DIGITS = 13
#: How far ``endMs - startMs`` may sit from the length the recorder reported before the pair
#: is treated as saying nothing.  One second: the wall clock and the audio length differ by
#: a frame or two on a healthy call, and by the whole call when trunkd's end clock read
#: non-positive and the length fell back to the samples.
MS_END_TOLERANCE_S = 1.0


#: The earliest call time a push may name: 2000-01-01T00:00:00Z.  Nothing this archive holds
#: is older, and the recorders it takes pushes from did not exist.
MIN_CALL_EPOCH = 946_684_800
#: How far past *now* a call time may sit.  A day covers any clock skew a recorder is
#: honestly suffering; beyond that it is a unit error.
MAX_CALL_FUTURE_S = 86_400.0


class CallTimeOutOfRange(ValueError):
    """The form's ``dateTime`` is not a plausible call start in unix seconds."""


def call_epoch_in_range(seconds: float, *, now: float | None = None) -> bool:
    """True when ``seconds`` is a plausible call start: 2000-01-01 … now + one day."""
    ceiling = (time.time() if now is None else now) + MAX_CALL_FUTURE_S
    return MIN_CALL_EPOCH <= seconds <= ceiling


def form_epoch(form: dict[str, Any]) -> int | None:
    """The form's ``dateTime`` in unix seconds, ``None`` when absent (or ``0``/unparseable).

    Raises :class:`CallTimeOutOfRange` for a number that cannot be a call start — the
    push endpoint answers that ``400`` before it reads a byte of the audio.  ``0`` and a
    non-number keep their old meaning, "no dateTime", so the filename and ID3 rungs still
    get their turn.
    """
    epoch = _int(form.get("dateTime"))
    if not epoch:
        return None
    if not call_epoch_in_range(epoch):
        raise CallTimeOutOfRange(f"dateTime {epoch} is not unix seconds between 2000 and tomorrow")
    return epoch


def _ms_epoch(value: str | None, *, near: int | None) -> float | None:
    """Unix **seconds, with the milliseconds kept**, from a ``startMs``/``endMs`` field.

    ``None`` when the field is absent or not credible.  Two copies of one 0.65 s key-up can
    be stamped a whole second apart when each recorder rounds its own clock to seconds, and
    then they share no interval at all and reconciliation never sees them
    (:func:`app.ingest.reconcile.matches`, rule ``short-call``, is the other half of that
    fix).  So trunkd sends the millisecond stamps as well, from 2026-09-10 — and they are
    believed only when they agree with ``dateTime``, which stays both the fallback and the
    cross-check.
    """
    ms = _int(value)
    if ms is None or ms <= 0 or len(str(ms)) != MS_DIGITS:
        return None
    seconds = ms / 1000.0
    # Bounded on its own as well as against ``dateTime``: without one to agree with, a
    # 13-digit value is otherwise believed anywhere up to the year 2286.
    if not call_epoch_in_range(seconds):
        return None
    if near is not None and abs(seconds - float(near)) > MS_SANITY_WINDOW_S:
        return None
    return seconds


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _flag(value: str | None) -> bool | None:
    """A form flag as a **tri-state**: absent means "the recorder did not say".

    ``emergency`` and ``encrypted`` are nullable columns for exactly that reason (§3.2), and
    collapsing an explicit ``0`` to NULL would make `encrypted=false` skip every uploaded
    call — including the ones the recorder went to the trouble of telling us were clear.
    """
    if value is None or not str(value).strip():
        return None
    return _truthy(value)


def parse_patches(raw: str | None, *, supergroup_first: bool = True) -> tuple[int | None, tuple[int, ...]]:
    """``"[9230, 19282, 50051]"`` -> ``(9230, (19282, 50051))``.

    sdrtrunk prints the **supergroup first** and its members after it, so the first id is
    the patch's own number (§1.6).  trunk-recorder's uploader posts its
    ``patched_talkgroups`` verbatim — a plain list of members with no supergroup and no
    patch number at all — so ``supergroup_first=False`` reads every id as a member.
    :func:`is_trunk_recorder_form` is what decides which dialect is on the wire.

    For sdrtrunk a single id is not a patch, so it yields no patch and no members.
    """
    ids = tuple(int(m) for m in re.findall(r"\d+", raw or ""))
    if not supergroup_first:
        return None, ids
    if len(ids) < 2:
        return None, ()
    return ids[0], ids[1:]


def parse_sources(raw: str | None, duration: float) -> tuple[CallSourceRef, ...]:
    """trunk-recorder's ``sources`` form field -> ``call_sources`` rows.

    The uploader posts its ``srcList`` as JSON: ``[{"pos": 0, "src": 5250057, "tag": "SO 41"}]``.
    As in the file path (§1.7) the sidecar says when each radio *started* and the parser
    derives how long it held the channel from the next one's position.
    """
    if not (raw or "").strip():
        return ()
    try:
        entries = json.loads(raw or "")
    except json.JSONDecodeError:
        log.warning("call-upload: sources is not JSON, ignoring it")
        return ()
    if not isinstance(entries, list):
        return ()

    parsed: list[tuple[float, int | None, bool, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        src = _int(entry.get("src"))
        pos = entry.get("pos")
        try:
            offset = float(pos) if pos is not None else 0.0
        except (TypeError, ValueError):
            offset = 0.0
        tag = (str(entry.get("tag") or "")).strip() or None
        parsed.append((offset, src, bool(entry.get("emergency")), tag))
    parsed.sort(key=lambda item: item[0])

    out: list[CallSourceRef] = []
    for index, (offset, src, emergency, tag) in enumerate(parsed):
        following = parsed[index + 1][0] if index + 1 < len(parsed) else duration
        length = following - offset
        out.append(
            CallSourceRef(
                src=src,
                raw=str(src) if src is not None else "",
                pos_s=round(offset, 3),
                len_s=round(length, 3) if length > 0 else None,
                emergency=emergency,
                tag=tag,
                seq=index,
            )
        )
    return tuple(out)


def parse_json_field(raw: str | None) -> object | None:
    """A form field that carries JSON — ``frequencies``, kept verbatim in ``raw_meta``."""
    if not (raw or "").strip():
        return None
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return None


def has_non_finite(value: object) -> bool:
    """True when ``value`` holds a float Postgres' ``jsonb`` cannot store.

    :func:`json.loads` accepts three spellings JSON itself does not have — ``NaN``,
    ``Infinity`` and ``-Infinity`` — and hands back the corresponding float.  ``jsonb`` has
    no such value, so a dict carrying one raises ``InvalidTextRepresentation`` at flush
    time: a 500 to the recorder, which then spools the call and retries it for ever.  The
    same floats also poison every comparison they touch (``NaN < x`` and ``NaN > x`` are
    both False), which is the other half of why they are refused rather than clamped.
    """
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(has_non_finite(item) for item in value.values())
    if isinstance(value, list):
        return any(has_non_finite(item) for item in value)
    return False


#: How much better the incoming copy has to score before it takes the stored one's place.
#: Two receivers on one transmission land within a frame or two of each other, and swapping
#: the archive's audio over that is churn, not an improvement: the margin is what makes a
#: replacement mean "this one is materially better" rather than "this one is different".
BEST_SOURCE_MARGIN = 1.05

def parse_source_quality(raw: str | None) -> dict[str, Any] | None:
    """The recorder's ``sourceQuality`` blob, or ``None`` when there isn't a usable one.

    Deliberately forgiving in one direction only: anything that is not a JSON *object* —
    junk, a bare number, a truncated body — is dropped and the upload proceeds exactly as if
    the field had never been sent.  A recorder must never lose a call because it garbled an
    optional quality report, and a field FastAPI silently ignored until now must not become
    a way to make uploads fail.  What is kept is stored verbatim: the fields keyup scores on
    today are ``voice_frames``/``bad_frames``, and the recorder is free to send more.

    A report holding a non-finite number anywhere in it (:func:`has_non_finite`) is dropped
    *whole*, for the same reason and by the same rule: storing it would fail the ``jsonb``
    write and cost the recorder its call, and keeping the finite half of a report whose
    arithmetic has already gone wrong would be scoring on a number nobody meant.  The upload
    then proceeds as one that sent no report at all.
    """
    parsed = parse_json_field(raw)
    if not isinstance(parsed, dict):
        return None
    if has_non_finite(parsed):
        log.debug("call-upload: sourceQuality holds a non-finite number, dropped: %r", raw)
        return None
    return parsed


def source_instance_of(quality: dict[str, Any] | None) -> str | None:
    """Which recorder a quality report names, capped to the ``instance_id`` length."""
    if not quality:
        return None
    instance = quality.get("instance")
    if not isinstance(instance, str) or not instance.strip():
        return None
    return instance.strip()[:MAX_INSTANCE_ID_LEN]


def is_trunk_recorder_form(form: dict[str, Any]) -> bool:
    """Which recorder posted this.

    Both speak the Rdio-Scanner contract, and the one field that separates them is one
    only trunk-recorder sends: ``sources`` (or ``frequencies``), its per-transmission
    lists.  It matters because the two read ``patches`` differently — sdrtrunk puts the
    supergroup first and trunk-recorder posts a bare member list — and guessing from the
    array's shape would misread a two-member sdrtrunk patch as a trunk-recorder one.
    """
    return bool((form.get("sources") or "").strip() or (form.get("frequencies") or "").strip())


#: what trunkd puts in the ``recorder`` form field to name itself
TRUNKD_RECORDER = "trunkd"


def is_trunkd_form(form: dict[str, Any]) -> bool:
    """Whether trunkd posted this.

    trunkd speaks trunk-recorder's dialect on the wire — the same ``sources`` and
    ``frequencies`` lists, the same bare-member ``patches`` — so no field *shape* can tell
    the two apart.  It says so instead, in ``recorder``, and that is the only thing that
    decides ``ingest_source='trunkd'``.
    """
    return (form.get("recorder") or "").strip().lower() == TRUNKD_RECORDER


#: ``audioType`` -> the extension the call is stored under.  trunk-recorder posts the MIME
#: type of what it encoded; sdrtrunk posts nothing and always sends MP3.
AUDIO_EXTENSIONS: dict[str, str] = {
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
}

DEFAULT_AUDIO_EXTENSION = ".mp3"


def audio_extension(audio_type: str | None, audio_name: str | None = None) -> str:
    """The extension to store the upload under.

    It has to be right, not merely plausible: the rest of the pipeline dispatches on it —
    which mutagen reader probes the duration, which ``Content-Type`` the audio route
    serves — so an M4A saved as ``.mp3`` is a call nobody can play.  ``audioType`` decides;
    the posted filename's own extension is the fallback; MP3 is the default, which is what
    sdrtrunk sends and never labels.
    """
    mime = (audio_type or "").split(";")[0].strip().lower()
    if mime in AUDIO_EXTENSIONS:
        return AUDIO_EXTENSIONS[mime]
    suffix = Path(audio_name or "").suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return suffix
    return DEFAULT_AUDIO_EXTENSION


#: Longest stored filename, extension included.  Every filesystem this runs on allows 255
#: bytes; 180 leaves room for the ``_1``…``_999`` collision suffix below.
MAX_NAME_LENGTH = 180


def safe_filename(name: str | None, fallback: str, extension: str = DEFAULT_AUDIO_EXTENSION) -> str:
    """Keep the recorder's own name where it is sane; never let it escape the directory.

    Two edge cases the obvious version gets wrong (L6, L7): a name that sanitizes away to
    nothing (``"../../"``, ``"...."``) has to fall back to something, and the length cap
    has to be applied to the *stem*, or a long name loses the extension the rest of the
    pipeline dispatches on.  ``extension`` comes from :func:`audio_extension`, because a
    trunk-recorder push may be M4A or WAV rather than sdrtrunk's MP3.
    """
    stem = Path(name or "").name
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._")
    for suffix in AUDIO_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem:
        stem = _SAFE_NAME_RE.sub("_", fallback).strip("._") or "call"
    return f"{stem[: MAX_NAME_LENGTH - len(extension)]}{extension}"


def resolve_legacy_names(session: Session, base: CallRecord) -> CallRecord:
    """Re-split a pre-2025 filename's ``System_Site_Name`` against the archive.

    The parser has to guess where that block's system ends (it renders every space as an
    underscore, so there is no marker), and it guesses by counting one token of site and
    one of channel off the right.  That is right for three of the four legacy shapes in
    the production archive and wrong for the fourth — a two-token channel, "District Schools" —
    where it would mint a system named "… DISTRICT Eastside" beside the real one, which is
    the exact damage ``fix-upload-system`` was written to undo.

    So the split is re-asked here, where there is a session and therefore a list of the
    systems and sites that actually exist (:func:`app.ingest.service.resolve_legacy_sysite`).
    Only fields the *filename* supplied are replaced: an ID3 frame or a form field that
    named the system already outranks the filename, and this must not overrule it.  A
    block naming no known system is left exactly as the parser read it — the resolver can
    move a call onto a system that exists, never onto one it invents.
    """
    parts = base.filename_parts
    if not parts.legacy or not parts.sysite:
        return base
    resolved = resolve_legacy_sysite(session, parts.sysite)
    if resolved is None:
        return base
    system, site, channel_name = resolved
    # ``build_call_record`` reads the parser's ``-`` as a space; compare against what it
    # would have produced, so "came from the filename" is an exact test, not a guess.
    guessed = parts.system.replace("-", " ") if parts.system else None
    fields: dict[str, Any] = {}
    if base.system == guessed and system != guessed:
        fields["system"] = system
    if base.site == parts.site and site != parts.site:
        fields["site"] = site
    if base.channel_name == parts.channel_name and channel_name != parts.channel_name:
        fields["channel_name"] = channel_name
    if not fields:
        return base
    log.info(
        "call-upload: legacy name %r re-split against the archive: %s",
        parts.name,
        ", ".join(f"{k}={v!r}" for k, v in fields.items()),
    )
    return replace(base, **fields)


def build_upload_record(  # noqa: PLR0912, PLR0915 - one merge, read top to bottom
    path: Path | None,
    audio_root: Path,
    form: dict[str, Any],
    settings: Settings,
    *,
    rel_path: str | None = None,
    session: Session | None = None,
) -> CallRecord:
    """Merge the stored file with the form fields, form authoritative.

    sdrtrunk's streaming module may or may not have written ID3 frames (it does not when
    it re-encodes), so the parser is *tried* and the form fills in whatever it could not
    supply — up to and including the whole record.

    ``path``, when there is one, is the body in ``uploads/.incoming/`` — the record's own
    ``started_at`` is what decides the partition it moves to, so ``file_path`` here is
    provisional and :func:`file_upload` rewrites it after the rename.

    ``path`` is ``None`` for a call that arrived with **no audio at all** — P25 encrypts
    the voice payload only, so trunkd knows the talkgroup, the radios, the timing, the
    algorithm and the key id of an encrypted call and has no sample to send.  Then
    ``rel_path`` is the ``calls.file_path`` the row is filed under (nothing is on disk),
    the duration comes from the form's ``durationMs``, and the record carries
    ``audio_state='purged'`` — the same vocabulary the trunk-recorder sidecar path uses
    for the same fact (§1.7), so the audio route answers ``410 Gone`` rather than
    inventing a file, and correlation still sees the call.
    """
    zone = ZoneInfo(settings.tz)
    from_trunkd = is_trunkd_form(form)
    from_trunk_recorder = is_trunk_recorder_form(form) and not from_trunkd
    base: CallRecord | None = None
    # Only sdrtrunk's own MP3 is worth handing to the file parser: it may carry the ID3
    # frames, and then the filename and the frames say the same things the form does.  A
    # trunk-recorder push has no sidecar next to it and would send the dialect switch
    # looking for a ``<shortName>/Y/M/D`` layout that the uploads tree does not have, and
    # trunkd's MP3 is LAME output with no ID3 frames and a name of its own choosing, so
    # handing it to the sdrtrunk filename parser can only fail.
    parse_as_sdrtrunk = (
        path is not None
        and path.suffix.lower() == ".mp3"
        and not from_trunkd
        and not is_trunk_recorder_call(path)
    )
    if parse_as_sdrtrunk:
        try:
            base = build_call_record(path, audio_root, tz=settings.tz)
        except (ParseError, ValueError):
            base = None
        if base is not None and session is not None:
            # A pre-2025 name's system/site split is a guess until the archive is asked.
            base = resolve_legacy_names(session, base)

    if path is None:
        id3 = Id3Meta()
        # The recorder timed the call it could not record; a recorder that did not say so
        # leaves a zero-length row, which still places the talkgroup at that second.
        duration = max(0.0, (_int(form.get("durationMs")) or 0) / 1000.0)
        bitrate = sample_rate = channels = size = None
        sha = None
        mtime = None
    elif base is not None:
        id3 = base.id3
        duration = base.duration_s
        bitrate, sample_rate, channels = base.bitrate, base.sample_rate, base.channels
        size, sha, mtime = base.file_size, base.sha256, base.file_mtime
    else:
        id3 = parse_id3(path)
        probe = probe_audio(path)
        duration = probe.duration_s
        bitrate, sample_rate, channels = probe.bitrate, probe.sample_rate, probe.channels
        size, sha, mtime = probe.size, probe.sha256, probe.mtime

    # dateTime is the recorder's call *start* — the whole point of the push path — and
    # ``startMs`` is the same instant with the milliseconds still on it.  Sub-second
    # precision is not a nicety here: a 0.65 s call rounded to whole seconds by two
    # recorders lands in two disjoint intervals, and every rung of reconciliation is an
    # interval test.
    epoch = form_epoch(form)
    start_ms = _ms_epoch(form.get("startMs"), near=epoch)
    end_ms = _ms_epoch(form.get("endMs"), near=epoch)
    # ``endMs`` is a *cross-check only*.  It is the wall-clock end, and trunkd falls back to
    # the audio length for ``durationMs`` when that clock read non-positive — so the pair can
    # be zero apart on a call that really lasted 0.65 s.  The length therefore keeps coming
    # from ``durationMs`` (or the probe), exactly as before, and a disagreeing ``endMs`` is
    # noted and dropped rather than allowed to shorten a call.
    if start_ms is not None and end_ms is not None:
        span = end_ms - start_ms
        if span <= 0.0 or abs(span - duration) > MS_END_TOLERANCE_S:
            log.debug(
                "upload: endMs disagrees with durationMs (%.3f s vs %.3f s); using durationMs",
                span,
                duration,
            )
    if start_ms is not None:
        started_at = datetime.fromtimestamp(start_ms, tz=UTC)
        time_basis = TIME_BASIS_FORM
    elif epoch:
        started_at = datetime.fromtimestamp(epoch, tz=UTC)
        time_basis = TIME_BASIS_FORM
    elif base is not None:
        started_at = base.started_at
        time_basis = TIME_BASIS_PARSED
    elif id3.date is not None:
        # The filename parse failed but the recording itself still says when it was
        # written: sdrtrunk's ``COMM`` ``Date`` frame, which is the instant it *closed* the
        # file, exactly as ``build_call_record`` reads it.  This rung is why the endpoint
        # can promise that ID3 answers when ``dateTime`` is absent — without it a legacy
        # name (the parse raises, so ``base`` is None) fell straight past its own frames to
        # the arrival fallback, which is how the backlog was mis-stamped.
        started_at = _localize(id3.date, zone) - timedelta(seconds=duration)
        time_basis = TIME_BASIS_PARSED
    else:
        # Nothing in the push says when the call happened.  ``mtime`` is not a third
        # opinion here the way it is for a watched file: this file was written by *this
        # request*, seconds ago, so it and ``now()`` are the same wrong answer and both are
        # labeled as what they are.  Every one of the million backlog calls that landed
        # on their upload date came out of this branch (docs/MIGRATION.md, "Timestamps").
        started_at = (mtime or datetime.now(UTC)) - timedelta(seconds=duration)
        time_basis = TIME_BASIS_ARRIVAL
    ended_at = started_at + timedelta(seconds=duration)

    # trunkd posts trunk-recorder's bare member list, not sdrtrunk's supergroup-first one.
    patch_id, members = parse_patches(
        form.get("patches"), supergroup_first=not (from_trunk_recorder or from_trunkd)
    )
    tgid = _int(form.get("talkgroup")) or (base.tgid if base is not None else None)
    # trunkd files a patched call under its supergroup — the call's own talkgroup — and
    # posts the members as a bare list (no supergroup-first id, §1.6).  So for trunkd the
    # supergroup *is* the call's talkgroup, and that is the patch id the Patch table upserts
    # on; without it the members would be indexed but never linked to a patch row.  sdrtrunk
    # already prints the supergroup first, so its ``patch_id`` is set and this is a no-op.
    if from_trunkd and members and patch_id is None and tgid is not None:
        patch_id = tgid
    if members:
        talkgroup_ids = tuple(dict.fromkeys([*(t for t in (tgid,) if t is not None), *members]))
        call_type = "patch"
    elif tgid is not None:
        talkgroup_ids, call_type = (tgid,), "group"
    else:
        talkgroup_ids, call_type = (), "group"

    # Who the transmission was addressed to.  A ``unit`` push is a P25 private call: no
    # talkgroup at all (the endpoint refuses one that carries one), the source radio at one
    # end and ``target_radio_id`` at the other.  ``call_type`` follows it to ``individual``,
    # which is the value the enum has always had for exactly this and which nothing was
    # able to set until the recorder could say so.
    kind = parse_call_kind(form.get("call_kind")) or DEFAULT_CALL_KIND
    target_radio_id = _int(form.get("target_radio_id")) if kind == "unit" else None
    if kind == "unit":
        call_type = "individual"

    # trunk-recorder posts every keying radio; sdrtrunk posts the one ``source`` field, so
    # a single source at offset 0 is synthesized from it and both dialects write the same
    # ``call_sources`` shape.
    sources = parse_sources(form.get("sources"), duration)
    source = _int(form.get("source"))
    if sources and sources[0].src is not None:
        radio: RadioRef | None = RadioRef(raw=sources[0].raw, radio_id=sources[0].src)
        radio_alias = sources[0].tag
    elif source:
        radio = RadioRef(raw=str(source), radio_id=source)
        radio_alias = None
        sources = (
            CallSourceRef(src=source, raw=str(source), pos_s=0.0, len_s=round(duration, 3), seq=0),
        )
    else:
        radio = base.radio if base is not None else None
        radio_alias = None
        if radio is not None and radio.radio_id is not None:
            sources = (
                CallSourceRef(
                    src=radio.radio_id, raw=radio.raw, pos_s=0.0, len_s=round(duration, 3), seq=0
                ),
            )

    # Never invent a system name.  This used to end in ``or "upload"``, and that literal is
    # how the production archive grew a second, phantom ``System("upload")`` holding its own
    # duplicate ``Central`` and ``Eastside`` sites beside the real Example System: an sdrtrunk
    # upload whose *filename* the parser rejects still carries the system in its ID3 COMM
    # frames — which this chain ignored while the ``site`` line below has always read them —
    # so the site was recovered and the system was not, and ``upsert_system`` dutifully
    # created a row named after the placeholder.
    #
    # ``system`` (the recorder's numeric id) is not a name either and is no longer read as
    # one; a bare "1" is a worse system row than none at all.  An empty string here means
    # "unlabeled", which ``resolve_system_id`` answers with the sole existing system or a
    # refusal — it never mints one.
    system = (
        (form.get("systemLabel") or "").strip()
        or (base.system if base is not None else "")
        or (id3.system or "")
    )
    alias = (form.get("talkgroupLabel") or "").strip() or (
        base.talkgroup_alias if base is not None else None
    )
    # The decoder the filename or ID3 named, else the one the push says it used — trunkd
    # sends ``decoder=NBFM`` on a conventional-channel call and nothing on a P25 one.  It
    # becomes the protocol of a system this push *creates* (so ``infer_trunked`` says no
    # and Broadcastify Calls refuses it by rule, not by the blank-protocol default); it
    # never fills in an existing system's protocol (``decoder_from_form``).
    decoder = base.decoder if base is not None else id3.decoder
    decoder_from_form = False
    if not decoder and (form_decoder := (form.get("decoder") or "").strip()):
        decoder, decoder_from_form = form_decoder, True

    # ``raw_meta`` keeps whatever the recorder chose to tell us that has no column of its
    # own — trunk-recorder's per-transmission ``frequencies``, its service tag, its slot —
    # so the detail page can show it and nothing has to guess at a home for it.
    raw_meta: dict[str, Any] = {
        key: value
        for key, value in form.items()
        if value not in (None, "") and key not in {"key", "audio", "test"}
    }
    if (frequencies := parse_json_field(form.get("frequencies"))) is not None:
        raw_meta["frequencies"] = frequencies
    if sources and (parsed_sources := parse_json_field(form.get("sources"))) is not None:
        raw_meta["sources"] = parsed_sources
    if base is not None and base.raw_meta:
        raw_meta["id3"] = base.raw_meta

    if path is None:
        # Nothing was written: ``rel_path`` is the name the row is filed under and the
        # place the recording would have been (see the docstring).
        rel = rel_path or ""
        file_name = Path(rel).name
    else:
        try:
            rel = str(path.resolve().relative_to(audio_root.resolve()))
        except ValueError:  # pragma: no cover - the caller always writes under audio_root
            rel = str(path)
        file_name = path.name

    return CallRecord(
        file_path=rel,
        file_name=file_name,
        folder_alias=None,
        system=system,
        # trunkd names the receive site in the form; sdrtrunk only ever says it in ID3.
        site=(form.get("site") or "").strip() or (base.site if base is not None else id3.site),
        channel_name=base.channel_name if base is not None else id3.channel_name,
        decoder=decoder,
        decoder_from_form=decoder_from_form,
        channel=base.channel if base is not None else id3.channel,
        frequency_hz=_int(form.get("frequency")) or (base.frequency_hz if base is not None else None),
        tgid=tgid,
        talkgroup_ids=talkgroup_ids,
        patch_id=patch_id,
        member_tgids=members,
        member_aliases=dict(id3.member_aliases),
        talkgroup_alias=alias,
        call_type=call_type,
        radio=radio,
        radio_resolved=radio is not None and radio.radio_id is not None,
        recorded_at=ended_at,
        started_at=started_at,
        ended_at=ended_at,
        started_local=started_at.astimezone(zone).replace(tzinfo=None),
        time_source="upload",
        duration_s=duration,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        file_size=size,
        sha256=sha,
        file_mtime=mtime,
        recorder_version=id3.recorder_version,
        ingest_source=(
            "trunkd" if from_trunkd else ("trunk_recorder" if from_trunk_recorder else "upload")
        ),
        # An encrypted call has metadata and no voice payload; the row is kept with the
        # retention vocabulary for "the metadata is right and the bytes are not there".
        audio_state="present" if path is not None else "purged",
        emergency=_flag(form.get("emergency")),
        encrypted=_flag(form.get("encrypted")),
        audio_type=(form.get("audioType") or "").strip() or None,
        talkgroup_group=(form.get("talkgroupGroup") or "").strip() or None,
        talkgroup_description=(form.get("talkgroupName") or "").strip() or None,
        radio_alias=radio_alias,
        sources=sources,
        raw_meta=raw_meta,
        time_basis=time_basis,
        kind=kind,
        target_radio_id=target_radio_id,
        filename_parts=base.filename_parts
        if base is not None
        else FilenameParts(
            name=file_name,
            stamp=None,
            system=None,
            site=None,
            channel_name=None,
            to_raw=str(tgid or ""),
            from_raw=str(source) if source else None,
        ),
        id3=id3 if base is None else base.id3,
    )


# --------------------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------------------


def store_upload(
    session: Session,
    record: CallRecord,
    form: dict[str, Any],
    settings: Settings,
    *,
    choice: PriorityChoice | None = None,
    count: bool = True,
) -> Call:
    """Write the call and everything hanging off it — the push-path twin of
    ``app.ingest.service._ingest_new``, sharing every upsert with it.

    ``choice`` is the upload's admission class (``app.ingest.flow``).  It decides two
    things: the priority the transcription job is queued at, and — when the client actually
    asked for one — the ``calls.ingest_priority`` the row keeps, so "these came from the
    archive migration" survives the job being vacuumed.

    ``count=False`` when reconciliation (§5.1 step 4) already recognized this push as a
    second copy of a transmission the archive has: the row is still written — that is the
    whole point of reconciliation, the better copy may be this one — but the directory's
    counters were moved by the copy it is about to be compared with."""
    quality = parse_source_quality(form.get("sourceQuality"))
    wacn = record.radio.wacn if record.radio and record.radio.wacn else None
    # Raises ``UnknownSystem`` when the push named no system and there is no single one to
    # attach it to; the endpoint turns that into "incomplete call data: no system".
    system_id = resolve_system_id(
        session,
        record.system,
        protocol=record.decoder,
        wacn=wacn,
        fill_protocol=not record.decoder_from_form,
    )
    site_id = upsert_site(session, system_id, record.site)

    group_name = record.talkgroup_group
    primary_tg: Talkgroup | None = None
    # Ascending ``tgid``, and talkgroups before radios: the §5.2b lock order.  The tuple
    # itself keeps the recorder's order — it is what lands on ``calls.talkgroup_ids``.
    for tgid in sorted(record.talkgroup_ids or ()):
        alias = record.member_aliases.get(tgid) or (
            record.talkgroup_alias if tgid == record.tgid else None
        )
        talkgroup = upsert_talkgroup(
            session,
            system_id,
            tgid,
            alias=alias,
            seen_at=record.started_at,
            settings=settings,
            count=count,
        )
        # The upload carries the recorder's own labels; fill them in where the directory
        # has nothing, never overwrite what a human or a playlist import set.
        if talkgroup.alias is None and alias:
            talkgroup.alias = alias
        if talkgroup.group_name is None and group_name:
            talkgroup.group_name = group_name
        if tgid == record.tgid:
            primary_tg = talkgroup
            if talkgroup.description is None and record.talkgroup_description:
                talkgroup.description = record.talkgroup_description

    patch_pk = None
    if record.patch_id is not None and record.member_tgids:
        patch_pk = upsert_patch(
            session,
            system_id,
            patch_id=record.patch_id,
            member_tgids=list(record.member_tgids),
            seen_at=record.started_at,
            count=count,
        )

    # Every radio the push touches — the keying unit, the far end of a unit call, and every
    # ``srcList`` entry — in one ascending-``radio_id`` pass, after the talkgroups and
    # before the affiliations (§5.2b in app/db.py).
    #
    # ``sourceWacn``/``sourceSystem`` are the P25 Link Control opcode 9 extension, and they
    # arrive *beside* a plain ``source`` rather than inside it: the recorder knows the unit
    # is 101010.57.9701025 but still prints ``source=9701025``.  LC 9 rides some
    # transmissions of a call and not others, so the same unit reaches this path bare and
    # qualified by turns — which is why the push path *resolves* the row (adopting the one
    # already there and filling its home in place) instead of upserting on whichever
    # spelling this upload happened to carry.  The file path keeps the plain upsert: an
    # sdrtrunk filename has no extension to disagree with (docs/RADIO_EVENTS.md).
    radios = upsert_call_radios(
        session,
        system_id,
        record,
        seen_at=record.started_at,
        count=count,
        primary_wacn=(record.radio.wacn if record.radio else 0) or _int(form.get("sourceWacn")) or 0,
        primary_home=(record.radio.home_system if record.radio else 0)
        or _int(form.get("sourceSystem"))
        or 0,
        primary_alias=record.radio_alias,
        # The far end of a private call is a radio the archive should know about — it is a
        # unit on this system that was reachable at that moment — so it is resolved through
        # the same adopt-or-create path the source is.
        target_radio_id=record.target_radio_id if record.kind == "unit" else None,
        resolve_primary=True,
    )
    radio_pk = radios.primary
    target_pk = radios.target

    call = Call(
        system_id=system_id,
        site_id=site_id,
        kind=record.kind,
        target_radio_id=target_pk,
        talkgroup_id=primary_tg.id if primary_tg is not None else None,
        talkgroup_ids=list(record.talkgroup_ids),
        patch_id=patch_pk,
        radio_id=radio_pk,
        radio_raw=record.radio.raw if record.radio else None,
        talker_alias=(form.get("talkerAlias") or "").strip() or None,
        ingest_source=record.ingest_source,
        radio_resolved=record.radio_resolved,
        recorded_at=record.recorded_at,
        started_at=record.started_at,
        started_local=record.started_local,
        ended_at=record.ended_at,
        time_source="upload",
        duration_s=Decimal(str(round(record.duration_s, 3))),
        frequency_hz=record.frequency_hz,
        channel=record.channel,
        decoder=record.decoder,
        call_type=record.call_type,
        emergency=record.emergency,
        encrypted=record.encrypted,
        audio_type=record.audio_type,
        raw_meta=record.raw_meta or None,
        talkgroup_alias_at_record=record.talkgroup_alias,
        file_path=record.file_path,
        file_name=record.file_name,
        file_size=record.file_size,
        sha256=record.sha256,
        bitrate=record.bitrate,
        sample_rate=record.sample_rate,
        channels=record.channels,
        recorder_version=record.recorder_version,
        file_mtime=record.file_mtime,
        # ``purged`` when the push carried no audio: an encrypted P25 call has metadata and
        # no voice payload, and the row proves the talkgroup was held (§1.7).
        audio_state=record.audio_state,
        ingest_priority=(choice.name if choice is not None and choice.explicit else None),
        # Which receiver this recording is, and what it said about hearing it.  Both stay
        # NULL for a push that sent no ``sourceQuality`` — which is every recorder but
        # trunkd, and trunkd before it grew the field.
        source_instance=source_instance_of(quality),
        source_quality=quality or None,
        transcript_status="pending",
        # The Record toggle as the push path saw it (§5.6).  The file is written either
        # way — the transcriber and the exporters read it — and the discard pass deletes it
        # afterwards.
        audio_policy=audio_policy_for(primary_tg),
    )
    session.add(call)
    session.flush()

    radio_pk = write_call_sources(
        session,
        call,
        record,
        system_id,
        primary_radio_pk=radio_pk,
        primary_talkgroup_pk=primary_tg.id if primary_tg is not None else None,
        count=count,
        radios=radios.sources,
    )
    if radio_pk != call.radio_id:
        call.radio_id = radio_pk
    if not count:
        # A second copy: reconciliation is about to decide which of the two is the primary
        # and it owns the conversation and the queue from here (app/ingest/reconcile.py).
        call.transcript_status = "skipped"
        record_ledger(session, record.file_path, "ingested", call_id=call.id)
        return call
    if radio_pk is not None and primary_tg is not None and not record.sources:
        touch_affiliation(session, radio_pk, primary_tg.id, record.started_at)
    assign_conversation(session, call, settings.conversation_gap_seconds)
    if record.audio_state == "present":
        # The class resolves against *this* talkgroup: ``live`` is its own priority (today's
        # behavior), ``backlog`` is that plus 500, ``urgent``/``bulk``/an integer are fixed.
        job_priority = (
            choice.resolve(primary_tg.priority if primary_tg is not None else None)
            if choice is not None
            else None
        )
        enqueue_audio_process(session, call, settings, priority=job_priority)
        enqueue_transcription(session, call, primary_tg, settings, priority=job_priority)
    else:
        # Nothing to transcribe: the encrypted-call row has no audio by construction.
        call.transcript_status = "skipped"
    record_ledger(session, record.file_path, "ingested", call_id=call.id)

    notify(
        session,
        "call.new",
        {
            "id": call.id,
            "kind": call.kind,
            "target_radio_id": call.target_radio_id,
            "talkgroup_id": call.talkgroup_id,
            "talkgroup_ids": list(record.talkgroup_ids),
            "started_at": call.started_at.isoformat(),
            "duration_s": float(call.duration_s),
            "duplicate": False,
            "source": "upload",
            # The worker's LISTEN thread reads it to decide whether to wake the idle
            # transcription threads (app/wake.py): a backlog upload does not.
            "ingest_priority": call.ingest_priority,
        },
    )
    # Same transaction as the row and the bus event: a push that fails after this point
    # rolls the promise back with the call it was about (app/webhooks.py).
    from app import webhooks  # noqa: PLC0415 - avoids an import cycle at module load

    webhooks.enqueue_call_event(session, "call.stored", call.id)
    had_audio = record.audio_state == "present"
    tg_label = (
        # A unit call has no talkgroup to name, so the activity log names the pair instead —
        # which is the only thing that identifies it.
        f"{record.radio.radio_id if record.radio else '?'} \u2192 {record.target_radio_id}"
        if record.kind == "unit"
        else (primary_tg.alias or str(primary_tg.tgid))
        if primary_tg is not None
        else (str(record.tgid) if record.tgid is not None else "?")
    )
    priority = primary_tg.priority if primary_tg is not None else None
    if not had_audio:
        activity.record(
            session,
            source="upload",
            level="info",
            kind="call.no_audio",
            message=f"{record.system} {tg_label} "
            + ("encrypted, no audio" if record.encrypted else "accepted without audio"),
            call_id=call.id,
            meta={"system": record.system, "talkgroup": tg_label, "encrypted": bool(record.encrypted)},
        )
    else:
        activity.record(
            session,
            source="upload",
            level="info",
            kind="call.accepted",
            message=f"{record.system} {tg_label} accepted (p{priority}, {float(call.duration_s):.1f}s)",
            call_id=call.id,
            meta={
                "system": record.system,
                "talkgroup": tg_label,
                "priority": priority,
                "had_audio": True,
                "duration_s": float(call.duration_s),
                "class": choice.name if choice is not None else None,
            },
        )
    return call


def replace_with_better_source(
    session: Session,
    matched_id: int | None,
    record: CallRecord,
    form: dict[str, Any],
    settings: Settings,
    *,
    choice: PriorityChoice | None,
) -> str | None:
    """Give a stored call this push's audio when this receiver heard it better.

    Two recorders hear one site.  The dedupe above recognizes the second push as the same
    transmission and, historically, threw it away — which is right when it is the *same*
    recorder re-sending, and wrong when it is a second receiver that heard the call
    properly.  With both copies carrying a ``sourceQuality`` report there is finally
    something to compare, so the better recording takes the row and the worse one is
    dropped.

    Everything here is conservative, because the failure mode is losing audio:

    * both sides must carry a scoreable report (:func:`quality_score`) and must name
      **different** instances — a second push from the same recorder is a retransmit, not a
      second opinion, and never replaces anything;
    * the incoming copy must win by :data:`BEST_SOURCE_MARGIN` — 5 % of the magnitude of
      the stored score, so a negative score cannot be "beaten" by a slightly-less-negative
      one, and two receivers that heard the call equally well never swap the audio to and
      fro;
    * only a stored call whose audio is actually ``present`` is replaced.  An archived or
      purged row's bytes are not ours to swap out from under retention;
    * the whole change is one transaction with the ledger row and the activity line, and
      the old file is unlinked by the caller **after** that commit — a crash before it
      leaves the archive exactly as it was, holding audio it can still play.

    Returns the relative path of the audio the row no longer points at, for the caller to
    unlink after committing, or ``None`` when nothing was replaced (the ordinary case, and
    the answer is then the duplicate rejection it always was).
    """
    if matched_id is None or not settings.best_source_enabled:
        return None
    if record.audio_state != "present":
        return None
    quality = parse_source_quality(form.get("sourceQuality"))
    instance = source_instance_of(quality)
    incoming = quality_score(quality)
    if instance is None or incoming is None:
        return None

    matched = session.get(Call, matched_id)
    if matched is None or matched.audio_state != "present":
        return None
    stored_instance = matched.source_instance
    stored = quality_score(matched.source_quality)
    if stored_instance is None or stored is None or stored_instance == instance:
        return None
    # ``incoming > stored × 1.05`` as long as the stored score is positive, and a genuine
    # improvement rather than an arithmetic accident when it is not: scaling a negative
    # score by 1.05 *lowers* the bar, which would let a worse copy win.
    if incoming - stored <= abs(stored) * (BEST_SOURCE_MARGIN - 1.0):
        return None

    old_path = matched.file_path
    matched.file_path = record.file_path
    matched.file_name = record.file_name
    matched.file_size = record.file_size
    matched.sha256 = record.sha256
    matched.bitrate = record.bitrate
    matched.sample_rate = record.sample_rate
    matched.channels = record.channels
    matched.duration_s = Decimal(str(round(record.duration_s, 3)))
    # The row keeps the start time the archive already recorded — that is the transmission's
    # identity, and moving it would change what the call dedupes and groups against — but
    # the end follows the audio that is now on it, so the interval still describes the file.
    matched.ended_at = matched.started_at + timedelta(seconds=record.duration_s)
    if record.recorder_version:
        matched.recorder_version = record.recorder_version
    matched.source_instance = instance
    matched.source_quality = quality
    session.flush()

    # The new file is this call's recording now, in the ledger as much as on the row.  The
    # ledger row for the old path is left as it is: it names a file that no longer exists,
    # and rewriting history to say it was never ingested would be a lie about what happened.
    record_ledger(session, record.file_path, "ingested", call_id=matched.id)

    # The transcript on the row was made from the bytes that just left; the new audio is
    # why the swap happened, so it goes back through transcription.  ``enqueue_transcription``
    # respects the talkgroup's own policy, so a talkgroup nobody transcribes stays silent.
    #
    # Two edges where the row keeps the *old* transcript on purpose, both of them the
    # conservative answer:
    #
    # * a job already ``queued``/``running`` on the audio that just left is not re-enqueued —
    #   the partial index behind ``on_conflict_do_nothing`` matches it — so the call is
    #   transcribed from the superseded file that one time.  Re-running it would mean racing
    #   a worker that is mid-decode of a file this function is about to unlink;
    # * a human correction (``transcripts.edited_by_user``) stays primary.  A person who has
    #   already written down what was said outranks a fresh machine pass over better audio,
    #   and silently replacing their text with Whisper's would be losing work.
    #
    # An operator who wants either of them redone re-runs transcription from the call page.
    talkgroup = session.get(Talkgroup, matched.talkgroup_id) if matched.talkgroup_id else None
    job_priority = (
        choice.resolve(talkgroup.priority if talkgroup is not None else None)
        if choice is not None
        else None
    )
    enqueue_audio_process(session, matched, settings, priority=job_priority)
    enqueue_transcription(session, matched, talkgroup, settings, priority=job_priority)

    tg_label = (
        (talkgroup.alias or str(talkgroup.tgid))
        if talkgroup is not None
        else (str(record.tgid) if record.tgid is not None else "?")
    )
    activity.record(
        session,
        source="upload",
        level="info",
        kind="call.better_source",
        message=(
            f"{record.system} {tg_label}: {instance} heard it better than {stored_instance} "
            f"({incoming:.0f} vs {stored:.0f}) — audio replaced"
        ),
        call_id=matched.id,
        meta={
            "system": record.system,
            "talkgroup": tg_label,
            "from_instance": stored_instance,
            "to_instance": instance,
            "from_score": stored,
            "to_score": incoming,
        },
    )
    notify(session, "call.updated", {"id": matched.id})
    log.info(
        "call-upload: call %s audio replaced by %s (%.0f > %.0f, was %s)",
        matched.id,
        instance,
        incoming,
        stored,
        stored_instance,
    )
    return old_path


def _unlink_superseded(relative: str, settings: Settings) -> None:
    """Remove the recording a better copy has replaced — after the commit, never before.

    Containment is checked the way retention checks it (:func:`app.audio.audio_path`): this
    deletes a file, so a ``file_path`` that somehow escaped ``AUDIO_ROOT`` must cost a log
    line and a leftover file, never an unlink outside the archive.  A failure here is
    logged and swallowed: the row already points at the new audio and the call is safe; an
    orphaned MP3 is a housekeeping problem, not a reason to 500 a recorder that did nothing
    wrong.
    """
    path = audio_path(SimpleNamespace(file_path=relative, id=None), settings)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - an orphaned file is not worth failing the upload over
        log.warning("call-upload: could not remove superseded audio %s", path, exc_info=True)
        return
    # The superseded copy may have been the last file in an old partition — a 2022 bucket
    # that a better recorder has now emptied.  ``prune_empty_dirs`` stops at the first
    # directory that still holds something, and never at a root.
    prune_empty_dirs(path.parent, settings)


def incoming_path(audio_root: Path, name: str) -> Path:
    """A private directory under ``uploads/.incoming/`` holding ``name``, for the receive.

    The body is streamed here and ``os.rename``d into its partition once the record is
    built and the call's own date is known (:func:`file_upload`).  It keeps its **real
    name** — a directory of its own is what makes that safe against a second push of the
    same file — because the name is evidence: an sdrtrunk push carries its timestamp,
    system, site and talkgroup only in ``audioName``, and :func:`build_upload_record` hands
    the file to the filename parser.  A random temp *name* would silently cost every legacy
    push its true time, which is the bug this archive has already paid for once
    (docs/MIGRATION.md, "Timestamps").
    """
    directory = audio_root / INCOMING_DIR / secrets.token_hex(8)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def discard_incoming(path: Path | None) -> None:
    """Remove a temp body and the private directory it sat in.  Never raises."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
        if path.parent.parent.name == ".incoming":
            path.parent.rmdir()
    except OSError:  # noqa: BLE001 - a leftover temp file is housekeeping, not a 500
        log.warning("call-upload: could not clear the temp body %s", path, exc_info=True)


def _drop_upload(path: Path | None, settings: Settings) -> None:
    """Remove a body *this request* wrote, wherever the two-step write had got it to.

    Before :func:`file_upload` that is a temp directory; after it, a partition, and then the
    partition may be left empty — a legacy push whose one 2022 call is refused should not
    leave a ``2022/03/11/7f/`` behind it.  Both cases are failure paths, so neither may
    raise: the caller is already answering an error.
    """
    if path is None:
        return
    if path.parent.parent.name == ".incoming":
        discard_incoming(path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 - an orphan is housekeeping, not a reason to change the answer
        log.warning("call-upload: could not remove %s", path, exc_info=True)
        return
    prune_empty_dirs(path.parent, settings)


#: How often :func:`upload_path` re-makes a partition a concurrent prune removed.
RESERVE_ATTEMPTS = 3


def upload_path(
    audio_root: Path,
    started_at: datetime,
    name: str,
    settings: Settings,
    *,
    sha256: str | None = None,
) -> Path:
    """Reserve ``AUDIO_ROOT/uploads/YYYY/MM/DD[/ab]/<name>`` and return it.

    ``started_at`` is the **call's** start, not the push's arrival, and the two are years
    apart for a backlog migration — see :func:`app.audio.upload_partition` for why the
    layout is what it is and when the ``ab`` bucket appears.

    The name is *reserved*, not merely chosen: the free candidate is created ``O_EXCL``
    before it is returned, so two pushes racing on one name cannot both be told it is free
    and then have the second ``rename`` silently destroy the first's recording.  Every
    caller either writes over the reservation or unlinks it on the way out.

    ``name`` already carries the extension :func:`audio_extension` chose — MP3 from
    sdrtrunk, M4A or WAV from trunk-recorder.
    """
    directory = audio_root / upload_partition(started_at, settings, sha256=sha256, name=name)
    for attempt in range(RESERVE_ATTEMPTS):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            return _reserve_in(directory, name)
        except FileNotFoundError:
            # ``prune_empty_dirs`` — a superseded copy unlinked, a retention move — removed
            # the partition while it was still empty, between the ``mkdir`` and the
            # ``O_EXCL`` create.  Make it again and retry; the reservation itself is what
            # keeps it from being pruned once it exists.
            if attempt == RESERVE_ATTEMPTS - 1:
                raise
            log.info("call-upload: %s was pruned under the reservation; retrying", directory)
    raise AssertionError("unreachable")  # pragma: no cover


def _reserve_in(directory: Path, name: str) -> Path:
    """Create ``name`` (or its first free ``_n`` form) in ``directory`` ``O_EXCL``."""
    stem, suffix = Path(name).stem, Path(name).suffix
    for leaf in (name, *(f"{stem}_{n}{suffix}" for n in range(1, 1000))):
        target = directory / leaf
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            continue
        return target
    # A thousand calls sharing one name in one day is not a thing that happens, but
    # returning a taken path would overwrite somebody's recording, so take a new one.
    target = directory / f"{stem}_{secrets.token_hex(4)}{suffix}"
    target.touch()
    return target


def file_upload(
    temp: Path, audio_root: Path, record: CallRecord, settings: Settings
) -> tuple[Path, CallRecord]:
    """Move a received body out of ``.incoming`` into the partition its own date names.

    This is the whole point of the two-step write.  ``upload_path`` cannot be called before
    the record exists, because the date it partitions on is the one the record resolves —
    ``dateTime`` from a live push, the filename stamp or the ID3 ``Date`` frame from a
    backlog one — and that resolution needs the file on disk to parse.  So the body lands
    in a temp directory, the record is built from it, and this renames it into place:
    atomic, same filesystem, no copy, and the row that is about to be written already names
    where the bytes now are.

    Returns the final path and the record with ``file_path``/``file_name`` corrected — the
    leaf name can change, because the reserved slot may be the ``_1`` form.
    """
    final = upload_path(audio_root, record.started_at, temp.name, settings, sha256=record.sha256)
    os.rename(temp, final)
    discard_incoming(temp)
    try:
        rel = str(final.resolve().relative_to(audio_root.resolve()))
    except ValueError:  # pragma: no cover - upload_path always builds under audio_root
        rel = str(final)
    return final, replace(record, file_path=rel, file_name=final.name)


#: ``pg_advisory_xact_lock`` class for :func:`metadata_upload_path`.  4242-4245 are ingest's
#: (conversation, dedupe, reconcile, unit reconcile); this is the next namespace.  Taken
#: before the channel locks and never after them, so it cannot join a cycle.
METADATA_PATH_LOCK_CLASS = 4246


def metadata_upload_path(session: Session, started_at: datetime, name: str, settings: Settings) -> str:
    """``calls.file_path`` for a call that arrived with **no audio**.

    Nothing is written and no directory is made: an encrypted P25 call has metadata and no
    voice payload, so the row is stored with ``audio_state='purged'`` and this is where the
    recording would have been.  ``calls.file_path`` is unique and there is no file on disk
    to collide with, so uniqueness has to be taken from the table instead — the same
    ``_1``…``_999``-then-random walk :func:`upload_path` does against the filesystem.

    A *re-post* of the same call does not get here twice: the dedupe upstream matches it on
    talkgroup, start and duration and answers ``duplicate call rejected``.

    **Chosen under a lock.**  The check is a SELECT and the row is written later in the same
    transaction, so two audio-less copies of one call arriving together (two recorders, one
    ``audioName``) each used to see the name free, and the second INSERT died on the unique
    index as a 500.  A transaction-scoped advisory lock on the name makes the second wait
    for the first's commit and then see the name taken; the caller must have run
    :func:`guard_lock_wait` so the wait is bounded.
    """
    # Same partition rule as a call that *did* bring audio, so the two kinds of row sort
    # together and ``keyup refile-audio`` computes one answer for both; the bucket falls
    # back to a hash of the name, because there are no bytes to hash.
    prefix = upload_partition(started_at, settings, name=name)
    stem, suffix = Path(name).stem, Path(name).suffix
    session.execute(
        text("SELECT pg_advisory_xact_lock(:cls, hashtext(:key))"),
        {"cls": METADATA_PATH_LOCK_CLASS, "key": f"{prefix}/{name}"},
    )

    def taken(candidate: str) -> bool:
        return (
            session.execute(select(Call.id).where(Call.file_path == candidate).limit(1)).first()
            is not None
        )

    first = f"{prefix}/{name}"
    if not taken(first):
        return first
    for n in range(1, 1000):
        candidate = f"{prefix}/{stem}_{n}{suffix}"
        if not taken(candidate):
            return candidate
    return f"{prefix}/{stem}_{secrets.token_hex(4)}{suffix}"  # pragma: no cover


# --------------------------------------------------------------------------------------
# a push with no time of its own
# --------------------------------------------------------------------------------------


def no_call_time_response(head: dict[str, str], choice: PriorityChoice, audio_name: str | None) -> Response:
    """Refuse a backlog push that says nothing about when the call happened.

    A backlog upload is a *file that still exists* being sent again: the client can retry
    it after whatever kept the time out of it — a missing ``dateTime``, a filename this
    parser cannot read — is fixed, and nothing is lost by saying no.  A live push has no
    such second chance, so it keeps the arrival fallback and only gets the warning.

    ``priority`` is the opt-in to the JSON contract, and a backlog pusher always sends one,
    so this is JSON in practice; the plain-text body is there for the case that cannot
    happen rather than as a shape anybody has to handle.
    """
    if not choice.explicit:  # pragma: no cover - a backlog class implies an explicit priority
        return PlainTextResponse(NO_TIME_BODY, status_code=400, headers=head)
    return JSONResponse(
        {
            "error": "no_call_time",
            "class": choice.name,
            "detail": (
                "this push carries no call time: send dateTime (unix seconds, the call "
                "start), or an audioName whose sdrtrunk stamp this server can parse. A "
                "backlog upload is refused rather than filed under its arrival time."
            ),
            "audioName": audio_name,
            "retry_after": 0,
        },
        status_code=400,
        headers=head,
    )


def note_time_fallback(session: Session, audio_name: str | None, choice: PriorityChoice) -> None:
    """Say — at most once a minute — that a call was filed under its arrival time.

    Both halves matter.  The WARNING is what an operator tailing the log sees; the
    activity row is what makes "when did this start, and how many?" answerable afterwards,
    which is precisely the question nobody could ask while a million rows were being
    mis-stamped in silence.  ``suppressed`` carries the ones between two complaints, so the
    count is never the number of log lines.
    """
    _time_fallback[1] += 1
    now = time.monotonic()
    if _time_fallback[0] and now - _time_fallback[0] < TIME_FALLBACK_INTERVAL_S:
        return
    suppressed = int(_time_fallback[1]) - 1
    _time_fallback[0], _time_fallback[1] = now, 0.0
    log.warning(
        "call-upload: no call time in the push, filed under its arrival instead — "
        "audioName=%r priority=%s%s",
        audio_name,
        choice.name,
        f" ({suppressed} more since the last warning)" if suppressed else "",
    )
    activity.record(
        session,
        source="upload",
        level="warn",
        kind="call.time_fallback",
        message=f"no call time in the push, filed under its arrival: {audio_name or '(unnamed)'}",
        meta={"audioName": audio_name, "priority": choice.name, "suppressed": suppressed},
    )


# --------------------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------------------


#: Wall clock past which one upload is worth a WARNING with its per-stage breakdown.  The
#: route no longer runs on the event loop, so a slow upload costs one threadpool worker
#: rather than the whole API — but it is still the symptom, and the incident it came from
#: (2026-09-03: 122 s in one request, 38 timed-out recorders) was invisible in the log
#: precisely because nothing measured a *single* upload.
SLOW_UPLOAD_SECONDS = 5.0


class UploadStages:
    """Per-stage wall clock for one upload, so a slow one names what was slow.

    The stages are the five things an upload does that can take real time — ``receive``
    (copy the spooled body to ``.incoming``), ``parse`` (ID3, ffprobe, sha256), ``move``
    (the rename into the call's own partition, or the ``file_path`` walk for a call with no
    audio), ``lookup`` (the channel lock and the dedupe/reconcile match) and ``store`` (the
    row, its upserts, the queue rows and the commit).  Anything not inside a stage is a few
    hundred microseconds of form handling, and the difference between the total and the sum
    of the stages is exactly that.
    """

    __slots__ = ("started", "times")

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.times: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one stage.  Counted even when the body raises — a slow *failure* is the
        interesting kind, and every stage below has an error path out of it."""
        begin = time.monotonic()
        try:
            yield
        finally:
            self.times[name] = self.times.get(name, 0.0) + (time.monotonic() - begin)

    @property
    def total(self) -> float:
        return time.monotonic() - self.started

    def render(self) -> str:
        """``receive=3ms parse=61ms …`` in the order the stages ran."""
        return " ".join(f"{name}={seconds * 1000:.0f}ms" for name, seconds in self.times.items())

    def warn_if_slow(self, name: str | None, label: str) -> None:
        """One WARNING when the request as a whole outran :data:`SLOW_UPLOAD_SECONDS`.

        Called from a ``finally`` around the whole of :func:`_accept_upload`, so it fires
        for every way out — stored, duplicate, refused, raised — and never twice.
        """
        total = self.total
        if total < SLOW_UPLOAD_SECONDS:
            return
        slowest = max(self.times.items(), key=lambda item: item[1], default=("(none)", 0.0))
        log.warning(
            "call-upload: slow upload %.1fs — slowest stage %s at %.1fs (%s) — audioName=%r [%s]",
            total,
            slowest[0],
            slowest[1],
            self.render() or "no stage ran",
            name,
            label,
        )


def guard_lock_wait(session: Session, settings: Settings) -> None:
    """Cap how long this upload's transaction may wait on a lock.

    The write below takes a channel advisory lock (:func:`app.ingest.reconcile.lock_reconcile`
    or :func:`app.ingest.service.lock_dedupe`), a conversation advisory lock, and row locks
    on the talkgroup, site and radio rows its ``ON CONFLICT DO UPDATE`` counters touch.
    Every one of those can already be held by a *different* process — the file watcher, or
    ``keyup reconcile``'s batch pass, which walks up to 100,000 calls in a single
    transaction and holds every row it has updated until it commits — and Postgres waits
    for a lock forever unless it is told not to.

    ``SET LOCAL`` scopes the cap to this transaction and Postgres drops it at the commit or
    the rollback, so nothing leaks onto a pooled connection.  A wait that outruns it raises,
    which the endpoint already answers as ``import failed:`` with the temp body removed —
    and a recorder spools and retries, so a refused push costs one retry where the wait
    costs a connection (and, before this route left the event loop, the whole API).
    """
    ms = int(getattr(settings, "upload_lock_timeout_ms", 0) or 0)
    if ms <= 0:
        return
    # No bind parameters: ``SET`` takes a literal, and ``ms`` is an int by construction.
    session.execute(text(f"SET LOCAL lock_timeout = '{ms}ms'"))


# --------------------------------------------------------------------------------------
# flow control (docs/RECORDERS.md "Priority and flow control")
# --------------------------------------------------------------------------------------


def refusal_response(admission: Admission) -> Response:
    """A 429/507 in whichever dialect this client speaks.

    sdrtrunk and trunk-recorder's uploader decide whether an upload worked by
    **string-matching the body**, and neither knows what to do with JSON, so a client that
    did not send ``priority`` gets plain text and the same five headers.  Sending
    ``priority`` *is* the opt-in to the JSON contract — which is why ``trunkd`` and
    ``tools/migrate_sdrtrunk.py`` always send one, even when the class they want is the
    default.
    """
    headers = flow.headers(admission.view)
    headers["Retry-After"] = str(admission.retry_after)
    if admission.choice.explicit:
        return JSONResponse(admission.body(), status_code=admission.status, headers=headers)
    body = NO_STORAGE_BODY if admission.status == 507 else QUEUE_FULL_BODY
    return PlainTextResponse(body, status_code=admission.status, headers=headers)


@router.get(
    "/upload/flow",
    summary="Queue depth and whether the API is accepting backlog",
    description=(
        "The same `queue` block `GET /api/status` carries, on an endpoint a pushing client "
        "may poll: `{live, backlog, audio_seconds, eta_seconds, accepting: {live, backlog}, "
        "paused_backlog}`. Needs the `upload` scope — as a bearer token, or as the same "
        "`key` the upload form carries, so a recorder needs no second credential. Poll it "
        "before a burst; every upload response carries the same numbers as `X-Keyup-Queue-*` "
        "headers, so a client that reads those never has to poll at all."
    ),
)
def upload_flow(
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str | None, Query(description="an `upload`-scope token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    raw = key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    if verify_upload_key(session, raw) is None:
        return JSONResponse({"detail": "This action needs the 'upload' scope"}, status_code=401)
    session.commit()  # last_used_at
    settings = effective_settings(session, base=get_settings())
    view = flow.gauge(session, settings)
    return JSONResponse(view.as_dict(), headers=flow.headers(view))


# --------------------------------------------------------------------------------------
# POST /upload/exists — "do you already have these bytes?" (docs/MIGRATION.md)
# --------------------------------------------------------------------------------------

#: How many hashes one precheck may carry.  A migration client batches its pending
#: recordings; 500 keeps the ``IN`` list well inside what Postgres plans happily and the
#: body under a kilobyte per twenty calls.
MAX_EXISTS_HASHES = 500
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExistsRequest(BaseModel):
    """The hashes a client is about to spend an upload on."""

    sha256: list[str] = Field(default_factory=list, max_length=MAX_EXISTS_HASHES)

    @field_validator("sha256")
    @classmethod
    def _lowercase_hex(cls, value: list[str]) -> list[str]:
        out = []
        for item in value:
            digest = str(item).strip().lower()
            if not _SHA256_RE.match(digest):
                raise ValueError(f"not a sha256 hex digest: {item!r}")
            out.append(digest)
        return out


@router.post(
    "/upload/exists",
    summary="Which of these sha256 digests are already stored",
    description=(
        "Body `{\"sha256\": [\"<hex>\", …]}`, at most 500 digests; answers "
        "`{\"present\": [\"<hex>\", …]}` — the subset `calls.sha256` already holds. A "
        "client migrating an archive asks this *before* it uploads, so identical bytes are "
        "never pushed twice: `POST /api/call-upload` can only answer `duplicate call "
        "rejected` after it has read the whole multipart body, which over a million "
        "already-stored calls is days of pure transfer. Needs the `upload` scope, as a "
        "bearer token or as the same `key` the upload form carries. One indexed `SELECT`: "
        "it queues nothing, stores nothing, and is not an upload as far as the flow gauge "
        "is concerned, so it is never refused for backlog reasons."
    ),
)
def upload_exists(
    session: Annotated[Session, Depends(get_session)],
    payload: ExistsRequest,
    key: Annotated[str | None, Query(description="an `upload`-scope token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    raw = key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    if verify_upload_key(session, raw) is None:
        return JSONResponse({"detail": "This action needs the 'upload' scope"}, status_code=401)
    session.commit()  # last_used_at
    wanted = list(dict.fromkeys(payload.sha256))
    if not wanted:
        return JSONResponse({"present": []})
    rows = session.scalars(select(Call.sha256).where(Call.sha256.in_(wanted))).all()
    present = {row for row in rows if row}
    # Answered in the order asked, so a client can zip the two lists if it wants to.
    return JSONResponse({"present": [digest for digest in wanted if digest in present]})


@router.post("/call-upload", response_class=PlainTextResponse, summary="Rdio-Scanner call upload")
def call_upload(  # noqa: PLR0913 - the wire contract is a flat form
    session: Annotated[Session, Depends(get_session)],
    key: Annotated[str | None, Form()] = None,
    system: Annotated[str | None, Form()] = None,
    test: Annotated[str | None, Form()] = None,
    audio: Annotated[UploadFile | None, File()] = None,
    audioName: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    audioType: Annotated[str | None, Form()] = None,  # noqa: N803
    dateTime: Annotated[str | None, Form()] = None,  # noqa: N803
    # The same call start and end as unix *milliseconds* (trunkd, 2026-09-10).  Preferred
    # over ``dateTime`` when they agree with it; see :func:`_ms_epoch`.
    startMs: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells them so
    endMs: Annotated[str | None, Form()] = None,  # noqa: N803
    talkgroup: Annotated[str | None, Form()] = None,
    source: Annotated[str | None, Form()] = None,
    frequency: Annotated[str | None, Form()] = None,
    talkerAlias: Annotated[str | None, Form()] = None,  # noqa: N803
    talkgroupLabel: Annotated[str | None, Form()] = None,  # noqa: N803
    talkgroupName: Annotated[str | None, Form()] = None,  # noqa: N803
    talkgroupGroup: Annotated[str | None, Form()] = None,  # noqa: N803
    talkgroupTag: Annotated[str | None, Form()] = None,  # noqa: N803
    systemLabel: Annotated[str | None, Form()] = None,  # noqa: N803
    patches: Annotated[str | None, Form()] = None,
    # trunk-recorder's uploader adds its per-transmission lists and the two flags a
    # sidecar carries (§1.7).  sdrtrunk sends none of them; their presence is what tells
    # the two dialects apart, see is_trunk_recorder_form.
    sources: Annotated[str | None, Form()] = None,
    frequencies: Annotated[str | None, Form()] = None,
    emergency: Annotated[str | None, Form()] = None,
    encrypted: Annotated[str | None, Form()] = None,
    # trunkd names itself (``recorder``) because its form is otherwise trunk-recorder's,
    # and adds what a P25 decoder knows and the two others do not: the receive site, and —
    # for an encrypted call, which it can describe but not record — the NAC, the encryption
    # algorithm, the key id and the length it timed.  All but ``site`` live in ``raw_meta``.
    recorder: Annotated[str | None, Form()] = None,
    site: Annotated[str | None, Form()] = None,
    nac: Annotated[str | None, Form()] = None,
    # The fully qualified source (P25 Link Control opcode 9, "Source ID Extension"): the
    # WACN and home system of a radio roaming onto the site we watch.  ``source`` alone is
    # ambiguous — 5701025 on our system and 5701025 on system 57 are different radios —
    # and ``radios.wacn`` / ``home_system_id`` have been waiting for these two numbers
    # since the schema was written (docs/NON-VOICE-DATA.md §4, docs/RADIO_EVENTS.md).
    sourceWacn: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    sourceSystem: Annotated[str | None, Form()] = None,  # noqa: N803
    algorithm: Annotated[str | None, Form()] = None,
    keyId: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    durationMs: Annotated[str | None, Form()] = None,  # noqa: N803
    # How well this receiver heard the call, as JSON: {"instance", "voice_frames",
    # "bad_frames", "rssi_dbfs"}.  It has to be declared here even though nothing but
    # ``form`` reads it — an undeclared multipart field is dropped by FastAPI before any of
    # this runs, which is exactly what happened to it until now.  Optional, and unusable
    # content is ignored rather than refused: a garbled quality report must never cost the
    # recorder its call (docs/RECORDERS.md, "Best source").
    sourceQuality: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    # trunkd's analog conventional channels (``[[channel]]``): ``mode=nbfm`` and
    # ``decoder=NBFM`` mark the call as analog narrowband FM, the rest is what the channel
    # measured — the configured and the detected CTCSS/DCS tone, why squelch closed, the
    # SNR, the peak deviation and the carrier offset.  All of it lands in ``raw_meta`` for
    # the call page; ``decoder`` also names a *new* system's protocol (never an existing
    # one's), and ``mode``/``decoder`` keep the call out of Broadcastify Calls, which takes
    # trunked systems only (app.exporters.gate.is_analog_call, docs/EXPORTERS.md).
    mode: Annotated[str | None, Form()] = None,
    decoder: Annotated[str | None, Form()] = None,
    tone: Annotated[str | None, Form()] = None,
    toneDetected: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    squelchReason: Annotated[str | None, Form()] = None,  # noqa: N803
    snrDb: Annotated[str | None, Form()] = None,  # noqa: N803
    peakDeviationHz: Annotated[str | None, Form()] = None,  # noqa: N803
    offsetHz: Annotated[str | None, Form()] = None,  # noqa: N803
    # Unit-to-unit (private) calls.  ``call_kind=unit`` says this recording is one radio
    # talking to one radio rather than traffic on a talkgroup, and ``target_radio_id`` names
    # the far end.  A push that sends neither is a group call, which is what every recorder
    # sent before private calls were recordable (docs/RECORDERS.md, "Unit-to-unit calls").
    call_kind: Annotated[str | None, Form()] = None,
    target_radio_id: Annotated[str | None, Form()] = None,
    # What the recorder already did about Broadcastify Calls, when it is the one uploading
    # (``broadcastify_upload_at = recorder``).  The call is stored either way and a
    # malformed outcome is logged and dropped: an archive must never lose a recording over
    # bookkeeping about somebody else's archive (docs/EXPORTERS.md).
    broadcastifyOutcome: Annotated[str | None, Form()] = None,  # noqa: N803 - the wire spells it so
    broadcastifyCode: Annotated[str | None, Form()] = None,  # noqa: N803
    broadcastifyResponse: Annotated[str | None, Form()] = None,  # noqa: N803
    broadcastifyRemoteId: Annotated[str | None, Form()] = None,  # noqa: N803
    broadcastifyHolder: Annotated[str | None, Form()] = None,  # noqa: N803
    # Flow control (docs/RECORDERS.md).  A recorder that sends neither keeps the plain-text
    # contract exactly as it was; sending one opts in to JSON refusals and the 429 protocol.
    priority: Annotated[str | None, Form()] = None,
    x_keyup_priority: Annotated[str | None, Header()] = None,
) -> Response:
    """Accept one call from a recorder and store it exactly as the file path would."""
    settings = get_settings()

    token = verify_upload_key(session, key)
    if token is None:
        # The one response that carries no queue headers: an unauthenticated caller has no
        # business learning how deep this deployment's queue is.
        return PlainTextResponse(INVALID_KEY_BODY, status_code=401)
    session.commit()  # persist last_used_at whatever happens next

    # The pause switch and the queue caps are runtime settings (Admin page, or
    # POST /admin/upload/pause-backlog), so the decision reads the settings table — behind
    # ``runtime_settings``' few-second cache — rather than only the environment.
    settings = effective_settings(session, base=settings)
    view = flow.gauge(session, settings)
    head = flow.headers(view)

    try:
        choice = flow.parse_priority(priority, x_keyup_priority)
    except BadPriority as exc:
        return JSONResponse(
            {
                "error": "bad_priority",
                "class": None,
                "detail": str(exc),
                "queue": view.as_dict(),
                "retry_after": 0,
            },
            status_code=400,
            headers=head,
        )

    # The probe, first of all: sdrtrunk posts key + system + test=1 and expects this exact
    # complaint.  Answered before admission control *and* before the validation below, on
    # purpose — a recorder proving its key works must not be refused for anything else.
    if _truthy(test):
        return PlainTextResponse(NO_TALKGROUP_BODY, status_code=200, headers=head)

    # Who this call was addressed to, decided before anything else looks at ``talkgroup``:
    # for a unit call the absence of one is the point, and the probe below reads a missing
    # talkgroup as "nothing to store".
    kind = parse_call_kind(call_kind)
    if kind is None:
        return PlainTextResponse(BAD_KIND_BODY, status_code=400, headers=head)
    if kind == "unit":
        # Both halves of the private-call contract are refused rather than repaired.  A
        # talkgroup on a unit call is a recorder bug and either reading of it loses
        # something; a unit call with no far end is not a unit call at all.
        if _int(talkgroup):
            return PlainTextResponse(UNIT_WITH_TALKGROUP_BODY, status_code=400, headers=head)
        if not _int(target_radio_id):
            return PlainTextResponse(NO_TARGET_BODY, status_code=400, headers=head)

    # A group push with nothing to file: the same 200 the probe gets, because that is the
    # body sdrtrunk string-matches.  A unit call is the one push that legitimately carries
    # no talkgroup, so it is past this gate by the time it is reached.
    if kind != "unit" and not _int(talkgroup):
        return PlainTextResponse(NO_TALKGROUP_BODY, status_code=200, headers=head)

    # Everything above is a few hundred bytes of form.  This is the decision, taken before
    # the audio part is copied anywhere: refusing after reading it would cost exactly the
    # bandwidth the refusal exists to save.
    admission = flow.admit(choice, view, settings, audio_root=settings.audio_root)
    if not admission.ok:
        log.info(
            "call-upload: refused %s (%s) — %s [%s]",
            choice.name,
            admission.status,
            admission.detail,
            token.label,
        )
        return refusal_response(admission)

    stages = UploadStages()
    try:
        return _accept_upload(
            session=session,
            settings=settings,
            token=token,
            choice=choice,
            head=head,
            audio=audio,
            stages=stages,
            form={
                "system": system,
                "audioName": audioName,
                "audioType": audioType,
                "dateTime": dateTime,
                "startMs": startMs,
                "endMs": endMs,
                "talkgroup": talkgroup,
                "source": source,
                "frequency": frequency,
                "talkerAlias": talkerAlias,
                "talkgroupLabel": talkgroupLabel,
                "talkgroupName": talkgroupName,
                "talkgroupGroup": talkgroupGroup,
                "talkgroupTag": talkgroupTag,
                "systemLabel": systemLabel,
                "patches": patches,
                "sources": sources,
                "frequencies": frequencies,
                "emergency": emergency,
                "encrypted": encrypted,
                "recorder": recorder,
                "site": site,
                "nac": nac,
                "sourceWacn": sourceWacn,
                "sourceSystem": sourceSystem,
                "algorithm": algorithm,
                "keyId": keyId,
                "durationMs": durationMs,
                "sourceQuality": sourceQuality,
                "mode": mode,
                "decoder": decoder,
                "tone": tone,
                "toneDetected": toneDetected,
                "squelchReason": squelchReason,
                "snrDb": snrDb,
                "peakDeviationHz": peakDeviationHz,
                "offsetHz": offsetHz,
                "call_kind": kind,
                "target_radio_id": target_radio_id,
                "broadcastifyOutcome": broadcastifyOutcome,
                "broadcastifyCode": broadcastifyCode,
                "broadcastifyResponse": broadcastifyResponse,
                "broadcastifyRemoteId": broadcastifyRemoteId,
                "broadcastifyHolder": broadcastifyHolder,
            },
        )
    finally:
        admission.release()
        # Every way out of the upload passes here — stored, duplicate, refused, raised —
        # so this is the one place a per-upload wall clock can be reported from once.
        stages.warn_if_slow(audioName, token.label)


#: The ``broadcastifyOutcome`` values a recorder may report, and nothing else is read.
BROADCASTIFY_OUTCOMES = frozenset(
    {"uploaded", "duplicate", "failed", "late", "claim_denied", "skipped"}
)


def record_broadcastify_outcome(
    session: Session, call: Any, form: dict[str, Any], settings: Settings
) -> None:
    """Write the ledger row for an upload a **recorder** already did (docs/EXPORTERS.md).

    With ``broadcastify_upload_at = recorder`` the call reaches Broadcastify before it
    reaches keyup, and this is what keeps keyup the system of record anyway: the recorder
    reports what it was told on the same push that carries the audio, and the answer is
    written into ``call_exports`` exactly as the worker would have written it.  The row
    existing is also what stops the worker from uploading the call a second time —
    :func:`app.exporters.ledger.discover` inserts on a ``(call_id, service)`` unique and
    finds this one already there.

    Nothing here can refuse a call.  An outcome that is missing, misspelled or unparseable
    is logged and dropped: the recording is the thing worth keeping, and a recorder whose
    bookkeeping is wrong still recorded the call.  ``ON CONFLICT DO NOTHING`` for the same
    reason — a re-push must not overwrite the answer the first one recorded.
    """
    raw = (form.get("broadcastifyOutcome") or "").strip().lower()
    if not raw:
        return
    from app.exporters import claims  # noqa: PLC0415 - the exporters package imports httpx
    from app.exporters.broadcastify import (  # noqa: PLC0415
        DUPLICATE_REASON,
        LATE_REASON,
        MAX_RESPONSE_CHARS,
        SERVICE,
    )

    if raw not in BROADCASTIFY_OUTCOMES:
        log.warning("call-upload: unknown broadcastifyOutcome %r on call %s", raw, call.id)
        return
    response = (form.get("broadcastifyResponse") or "").strip()[:MAX_RESPONSE_CHARS] or None
    code = _int(form.get("broadcastifyCode"))
    holder = (form.get("broadcastifyHolder") or "").strip()[:MAX_INSTANCE_ID_LEN] or None
    instance = source_instance_of(parse_source_quality(form.get("sourceQuality"))) or (
        form.get("recorder") or ""
    ).strip()[:MAX_INSTANCE_ID_LEN]

    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "call_id": call.id,
        "service": SERVICE,
        "response": response,
        "response_code": code,
        "next_attempt_at": now,
        "updated_at": now,
    }
    if raw == "uploaded":
        values |= {
            "status": "uploaded",
            "uploaded_at": now,
            "remote_id": (form.get("broadcastifyRemoteId") or "").strip() or None,
            "uploaded_by": instance or None,
        }
    elif raw == "duplicate":
        # Their word, not ours, and the same constant the worker writes so the Exports
        # card's duplicate count sees both paths as one number.
        values |= {"status": "skipped", "skip_reason": DUPLICATE_REASON}
    elif raw == "late":
        # Prefixed the way :func:`app.exporters.worker.late_reason` prefixes it, because
        # the status endpoint counts on that prefix and an operator reads the two side by
        # side.  The tail says who measured it: this one was measured at the recorder.
        values |= {
            "status": "skipped",
            "skip_reason": f"{LATE_REASON} (at {instance or 'the recorder'})",
        }
    elif raw == "claim_denied":
        values |= {"status": "skipped", "skip_reason": claims.reason_for(holder)}
    elif raw == "failed":
        # The recorder's attempt counts as the first, and the row goes back in the queue
        # rather than parking: what the recorder reports here is a PUT that did not get
        # through ("Peer disconnected", a timeout), and the worker is the retry — it will
        # re-register and PUT from this side, where a Broadcastify "duplicate" answer means
        # the recorder's copy landed after all.  A rejection the worker recognises as
        # permanent still parks on that first retry; one that is merely late is skipped.
        # Written as ``failed`` this used to park 1,757 rows at one attempt.
        #
        # Due *now*, not after the 30 s backoff: that is twice Broadcastify's skew window,
        # so a backed-off retry was always skipped as late and this path never uploaded
        # anything.  The worker's next pass either sends it inside the window — taking over
        # the recorder's claim, which this push marks ``failed`` just below — or skips it
        # as late on the spot.
        values |= {
            "status": "pending",
            "attempts": 1,
            "last_error": response or "recorder upload failed",
            "next_attempt_at": now,
        }
    else:  # "skipped" — the recorder decided not to, and said why
        values |= {"status": "skipped", "skip_reason": f"recorder: {response or 'not selected'}"}

    session.execute(
        pg_insert(CallExport)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["call_id", "service"])
    )
    # …and close the claim the recorder made a moment before the call existed here, so the
    # registry can be read as "who uploaded what" rather than only "who asked".
    tgid = _int(form.get("talkgroup"))
    if instance and tgid:
        # The claim is matched on what the *recorder* said, not on what was stored: it was
        # made one request earlier, from the same numbers this form carries, and the call
        # row did not exist yet to be named.
        claims.stamp_call(
            session,
            instance=instance,
            tgid=tgid,
            ts=_int(form.get("dateTime")) or int(call.started_at.timestamp()),
            call_id=int(call.id),
            outcome=raw,
            response=response,
        )


def _accept_upload(  # noqa: PLR0913 - one call site; it needs every one of them
    *,
    session: Session,
    settings: Settings,
    token: ApiToken,
    choice: PriorityChoice,
    head: dict[str, str],
    audio: UploadFile | None,
    stages: UploadStages,
    form: dict[str, Any],
) -> Response:
    """Store one admitted upload — write the audio, parse it, dedupe it, enqueue it.

    Split out of :func:`call_upload` for one reason: the backlog semaphore an admitted
    upload holds has to be released down every path out of here, and a ``finally`` around a
    single call is the only version of that which cannot be forgotten when a new early
    return is added.  ``head`` is the five queue headers, measured before the decision, so
    every answer — stored, duplicate, too large, broken — carries the same flow-control
    reading the refusals do.
    """
    audioName = form.get("audioName")  # noqa: N806 - the wire spells them so
    audioType = form.get("audioType")  # noqa: N806
    talkgroup = form.get("talkgroup")
    source = form.get("source")
    encrypted = form.get("encrypted")

    root = Path(settings.audio_root)
    # Checked before a byte of the audio is read: a millisecond ``dateTime`` used to reach
    # ``datetime.fromtimestamp`` below and come back as a 500 (or, around 1e11, as a call
    # filed in the year 5138).
    try:
        epoch = form_epoch(form)
    except CallTimeOutOfRange as exc:
        log.warning("call-upload: refused %s — %s [%s]", audioName, exc, token.label)
        return PlainTextResponse(BAD_TIME_BODY, status_code=400, headers=head)
    started_hint = datetime.fromtimestamp(epoch, tz=UTC) if epoch else datetime.now(UTC)
    # The stored extension follows what the recorder says it encoded, because everything
    # downstream dispatches on it: which reader probes the duration, which Content-Type
    # the audio route serves.
    # The fallback name identifies the channel the call was on; for a unit call that is the
    # pair of radios, because there is no talkgroup.
    channel_part = form.get("target_radio_id") if form.get("call_kind") == "unit" else talkgroup
    name = safe_filename(
        audioName or (audio.filename if audio is not None else None),
        f"{int(started_hint.timestamp())}_{channel_part}_{source or 0}",
        audio_extension(audioType, audioName or (audio.filename if audio is not None else None)),
    )

    target: Path | None = None
    if audio is not None:
        # Into ``uploads/.incoming/`` first, not into a partition: which partition this
        # belongs in is the *call's* date, and nothing knows that until the record is built
        # from the bytes below.  The move is an ``os.rename`` on the same filesystem
        # (:func:`file_upload`), so the indirection costs one directory entry.
        target = incoming_path(root, name)
        # Copied a megabyte at a time with a running-size abort: `Content-Length` was
        # checked by the middleware before the parse, but it is a claim, and this is the
        # fact (H4).
        try:
            with stages.stage("receive"):
                written = stream_to_path(audio, target, MAX_UPLOAD_BYTES)
        except UploadTooLarge:
            log.warning("call-upload: audio part outran %d bytes", MAX_UPLOAD_BYTES)
            discard_incoming(target)
            return PlainTextResponse(TOO_LARGE_BODY, status_code=413, headers=head)
        except BaseException:
            # A full disk, a client that went away mid-body: nothing downstream will ever
            # look at this temp file again, so it goes now rather than to the sweep.
            discard_incoming(target)
            raise
        if not written:
            discard_incoming(target)
            target = None

    # No audio.  P25 encrypts the voice payload and nothing else, so a recorder knows
    # everything about an encrypted call except how it sounded: the row is worth keeping —
    # it proves an agency held that talkgroup at that second, which is what the correlation
    # API is asked for — and is written the way the trunk-recorder sidecar path writes the
    # same fact, with ``audio_state='purged'`` (§1.7).  A *clear* call with no audio is
    # still the recorder failing to send one.
    if target is None and not _flag(encrypted):
        activity.record(
            session,
            source="upload",
            level="warn",
            kind="call.no_audio",
            message=f"rejected, clear call with no audio: tg {talkgroup}",
            meta={"talkgroup": _int(talkgroup)},
        )
        session.commit()
        return PlainTextResponse(NO_AUDIO_BODY, status_code=400, headers=head)

    try:
        # ``rel_path=None`` for the no-audio case too: the row's nominal path is partitioned
        # on the call's own date like everybody else's, and that date is what the record is
        # being built to resolve, so it is filled in below.
        with stages.stage("parse"):
            record = build_upload_record(
                target, root, form, settings, rel_path=None, session=session
            )
    except CallTimeOutOfRange as exc:  # pragma: no cover - refused above, before the receive
        _drop_upload(target, settings)
        log.warning("call-upload: refused %s — %s", name, exc)
        return PlainTextResponse(BAD_TIME_BODY, status_code=400, headers=head)
    except (ParseError, ValueError) as exc:
        _drop_upload(target, settings)
        log.warning("call-upload: unusable audio %s: %s", name, exc)
        return PlainTextResponse(NO_AUDIO_BODY, status_code=400, headers=head)
    except Exception as exc:  # noqa: BLE001 - never leave the temp body behind
        # A MutagenError the parser does not translate, an OverflowError/OSError out of a
        # timestamp, a DB error in ``resolve_legacy_names``: all used to escape with the
        # body still in ``.incoming``.
        return _upload_failed(session, target, settings, name, exc, head)

    try:
        return _file_and_store(
            session=session,
            settings=settings,
            token=token,
            choice=choice,
            head=head,
            stages=stages,
            form=form,
            root=root,
            name=name,
            target=target,
            record=record,
        )
    except Exception as exc:  # noqa: BLE001 - never leave the temp body behind
        # ``_file_and_store`` answers every failure it expects itself; this is the one it
        # did not (the arrival-time bookkeeping, the move) — the temp body is still in
        # ``.incoming`` then, and it is removed on the way out.
        return _upload_failed(session, target, settings, name, exc, head)


def _upload_failed(  # noqa: PLR0913 - the pieces of one error answer
    session: Session,
    target: Path | None,
    settings: Settings,
    name: str,
    exc: BaseException,
    head: dict[str, str],
) -> Response:
    """Roll back, remove the body this request wrote, and answer ``500 import failed``."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 - the answer is already an error
        log.warning("call-upload: rollback failed", exc_info=True)
    _drop_upload(target, settings)
    log.error("call-upload failed for %s", name, exc_info=exc)
    return PlainTextResponse(f"import failed: {type(exc).__name__}", status_code=500, headers=head)


def _file_and_store(  # noqa: PLR0913 - one call site; it needs every one of them
    *,
    session: Session,
    settings: Settings,
    token: ApiToken,
    choice: PriorityChoice,
    head: dict[str, str],
    stages: UploadStages,
    form: dict[str, Any],
    root: Path,
    name: str,
    target: Path | None,
    record: CallRecord,
) -> Response:
    """The rest of :func:`_accept_upload`, from a parsed record to the answer.

    ``target`` is still the temp body in ``.incoming`` on entry; the caller removes it if
    this raises before the move.
    """
    audioName = form.get("audioName")  # noqa: N806 - the wire spells it so

    if record.time_basis == TIME_BASIS_ARRIVAL:
        # Nothing in this push says when the call happened, so the row would be filed under
        # the moment it arrived and ``time_source='upload'`` would claim a recorder said so.
        if choice.is_backlog:
            _drop_upload(target, settings)
            log.warning(
                "call-upload: refused %s — a backlog push with no derivable call time [%s]",
                name,
                token.label,
            )
            activity.record(
                session,
                source="upload",
                level="warn",
                kind="call.no_time",
                message=f"backlog push refused, no call time: {audioName or name}",
                meta={"audioName": audioName, "priority": choice.name},
            )
            session.commit()
            return no_call_time_response(head, choice, audioName)
        note_time_fallback(session, audioName, choice)

    # The record has resolved the call's own time, so its partition is decided now and not
    # a moment earlier.  Everything below — the ledger, the dedupe, the row — names the
    # path the bytes are already at.
    with stages.stage("move"):
        if target is not None:
            target, record = file_upload(target, root, record, settings)
        else:
            guard_lock_wait(session, settings)
            rel_path = metadata_upload_path(session, record.started_at, name, settings)
            record = replace(record, file_path=rel_path, file_name=Path(rel_path).name)

    try:
        # Step 4 (§5.1).  Reconciliation stores the second copy instead of dropping it —
        # the pushed one may well be the better recording — so all this does before the
        # write is find the copy it will be compared with; ``reconcile_upload`` does the
        # comparing after.  With RECONCILE_ENABLED=false the old path runs unchanged: the
        # same lock ingest takes (the recorder's push and the watcher can both be holding
        # this call, M4), the same fuzzy dedupe, and the second copy is not stored at all.
        guard_lock_wait(session, settings)
        with stages.stage("lookup"):
            decision = reconcile_lookup(session, record, settings)
        if decision.reject:
            # Before answering "duplicate", ask whether this copy is *better* than the one
            # the archive has (docs/RECORDERS.md, "Best source").  When it is, the row takes
            # this audio and the push is answered as stored — the sender spools and retries
            # on anything but success, so it must only be told "kept" after the commit.
            superseded = replace_with_better_source(
                session, decision.matched_id, record, form, settings, choice=choice
            )
            if superseded is not None:
                session.commit()
                _unlink_superseded(superseded, settings)
                return PlainTextResponse(OK_BODY, status_code=200, headers=head)
            session.rollback()
            _drop_upload(target, settings)
            record_ledger(session, record.file_path, "duplicate", call_id=decision.matched_id)
            activity.record(
                session,
                source="upload",
                level="info",
                kind="call.duplicate",
                message=f"duplicate rejected: {record.system} tg {record.tgid}",
                call_id=decision.matched_id,
                meta={"system": record.system, "talkgroup": record.tgid, "matched_id": decision.matched_id},
            )
            session.commit()
            return PlainTextResponse(DUPLICATE_BODY, status_code=200, headers=head)

        with stages.stage("store"):
            call = store_upload(
                session, record, form, settings, choice=choice, count=decision.matched_id is None
            )
            outcome = reconcile_upload(session, call, decision, settings)
            # The recorder's own Broadcastify result, written into the ledger inside the
            # same transaction as the call: the row is what stops the exporter worker from
            # uploading the call a second time, so it must not be able to exist without
            # the call or the call without it.  Never fatal — see the writer.  Inside a
            # SAVEPOINT because PostgreSQL aborts the whole transaction on a failed
            # statement and the COMMIT below would then *silently* roll the call back while
            # the recorder is told it was imported: only the bookkeeping may be lost.
            try:
                with session.begin_nested():
                    record_broadcastify_outcome(session, call, form, settings)
            except Exception:  # noqa: BLE001 - bookkeeping must never cost the recording
                log.exception("call-upload: broadcastify outcome for call %s", call.id)
            session.commit()
    except UnknownSystem as exc:
        session.rollback()
        _drop_upload(target, settings)
        log.warning(
            "call-upload: refused %s — the push names no system (%s) [%s]",
            record.file_name,
            exc,
            token.label,
        )
        return PlainTextResponse(NO_SYSTEM_BODY, status_code=400, headers=head)
    except Exception as exc:  # noqa: BLE001 - never leave a half-written file behind
        session.rollback()
        _drop_upload(target, settings)
        log.exception("call-upload failed for %s", record.file_name)
        return PlainTextResponse(
            f"import failed: {type(exc).__name__}", status_code=500, headers=head
        )

    log.info(
        "call-upload: call %s tg %s priority %s (%s)%s",
        call.id,
        record.tgid,
        choice.name,
        token.label,
        (
            f" — copy of call {outcome.primary_id}"
            f"{', which it replaces as primary' if outcome.swapped else ''}"
        )
        if outcome.matched
        else "",
    )
    if outcome.matched:
        # sdrtrunk string-matches this body, so it is still the answer whenever the push
        # turned out to be a second copy — but the copy is stored now, under
        # ``uploads/`` and in the ledger, and it may have taken the primary seat
        # (docs/RECONCILIATION.md).
        return PlainTextResponse(DUPLICATE_BODY, status_code=200, headers=head)
    return PlainTextResponse(OK_BODY, status_code=200, headers=head)
