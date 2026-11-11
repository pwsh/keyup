"""The Broadcastify Calls uploader — the five-step workflow, of which this module is 3–5.

Broadcastify's own documentation states the workflow as five steps, and it is worth keeping
them in view because three of them happen outside this file:

1. **Verify time synchronization on the host system.**  "NTP accuracy is required; calls
   with bad timestamps are rejected or deduplicated incorrectly."  :mod:`app.exporters.clock`
   measures the offset and :class:`app.exporters.worker.ExportWorker` pauses *this* service
   (and only this one) while it is more than :data:`~app.exporters.clock.PAUSE_OFFSET_S`
   out.
2. **Record the call and capture its metadata** — ingest, long before an exporter sees it;
   :func:`app.exporters.base.facts_for` flattens the row into what step 4 asks for.
3. **Encode the audio** to AAC (M4A) or MP3 per the format spec —
   :mod:`app.exporters.transcode`, selected by ``broadcastify_encoding``.
4. **Register the call**: an RFC 1867 ``multipart/form-data`` POST of metadata only, no
   audio.  The server answers one line of plain text and, on success, that line is a
   single-use, time-limited upload URL.
5. **PUT the audio** to that URL with the right ``Content-Type``.  The URL is single-use
   and time-limited; it is never reused and never cached.

Two environments.  Broadcastify issues development and production credentials separately,
and is explicit that "the development endpoint [is used] with development credentials only.
Production credentials are issued separately and must be used against the production
endpoint."  ``broadcastify_environment`` therefore picks the URL, the key **and** the
system id as one unit (:func:`endpoint`): there is no path through this module that can
send a production key to the development host or the reverse.

The response table, verbatim from the documentation and implemented in :func:`parse_stage1`:

===================================================  ==================================
``0 <upload URL>``                                   success — PUT the file there
``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL``           duplicate (same system, talkgroup,
                                                     timestamp); no upload needed
``100 NO-API-KEY-SPECIFIED``                         missing/invalid apiKey; do not retry
                                                     without correcting it
===================================================  ==================================

``1 REJECTED…``, ``Invalid-API-Key`` and ``API-Key-Access-Denied`` are not in the published
table but are what the two reference clients classify as permanent, and they are treated
the same way here.

Sources beyond the documentation the user supplied, for the fields the table does not
spell out:

* **sdrtrunk** — ``src/main/java/io/github/dsheirer/audio/broadcast/broadcastify/``:
  ``FormField.java`` (the exact field names) and ``BroadcastifyCallBroadcaster.java``
  (assembly, patch formatting, response parsing).
* **trunk-recorder** — ``plugins/broadcastify_uploader/broadcastify_uploader.cc`` (the
  response classification and the stage-2 PUT's header handling).

Note that trunk-recorder's *current* master sends a different, undocumented shape (a
``metadata`` JSON file part in place of ``tg``/``src``/``freq``/``ts``).  This module
implements the **documented** form, which is what the spec specifies and what sdrtrunk
sends.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.config import Settings

from . import clock
from .base import CallFacts, ExportError, PermanentExportError, SkipCall, UploadResult

log = logging.getLogger("app.exporters.broadcastify")

__all__ = [
    "BroadcastifyClient",
    "DUPLICATE_REASON",
    "Endpoint",
    "LATE_REASON",
    "Registration",
    "content_type_for",
    "encoding_for",
    "endpoint",
    "format_talkgroup",
    "metadata_fields",
    "monitoring_url",
    "mp3_bitrate_kbps",
    "parse_stage1",
    "redact",
    "remote_id_for",
    "response_class",
    "stage1_record",
]

SERVICE = "broadcastify"

#: sdrtrunk sends its own name; trunk-recorder sends ``TrunkRecorder1.0``.  Ours says what
#: it is, so an operator reading Broadcastify's logs knows which uploader is running.
USER_AGENT = "keyup-exporter/1.0"

#: The two values ``broadcastify_environment`` takes.  Anything unrecognized reads as
#: ``production`` — an unreadable environment must not silently redirect real traffic at a
#: development host, and production is the one whose credentials are the ones set.
ENVIRONMENTS = ("production", "development")

#: ``enc`` and the stage-2 ``Content-Type``, from the documentation's Uploading section:
#: "Content-Type ``audio/aac`` for M4A/AAC, ``audio/mpeg`` for MP3".  ``m4a`` is the
#: documented default and the one this deployment sends.
ENCODINGS: dict[str, str] = {"m4a": "audio/aac", "mp3": "audio/mpeg"}
#: Kept as a name because it is what ``enc`` defaults to when nothing is configured.
ENCODING = "m4a"
AUDIO_CONTENT_TYPE = ENCODINGS[ENCODING]

#: The Monitoring section's pages, per environment: where an operator watches what this
#: uploader is doing on Broadcastify's side.  ``manage`` exists for production only.
MONITORING: dict[str, dict[str, str]] = {
    "production": {
        "status": "https://www.broadcastify.com/calls/status/",
        "manage": "https://www.broadcastify.com/calls/manage/",
    },
    "development": {
        "status": "https://www.broadcastify.com/calls-dev/status/",
    },
}

#: Stage-1 messages that mean "do not retry, and do not treat it as broken".  From
#: ``broadcastify_uploader.cc`` (``rfind("SKIPPED", 0) == 0``).
SKIP_PREFIX = "SKIPPED"
#: …and the ones that mean the call was refused outright and never will be accepted.
REJECT_PREFIX = "REJECTED"
#: Credential failures.  Retrying these is pointless until a human edits the environment.
#: ``NO-API-KEY-SPECIFIED`` is the one the published response table names, with code 100.
PERMANENT_MESSAGES = (
    "INVALID-API-KEY",
    "API-KEY-ACCESS-DENIED",
    "NO-API-KEY-SPECIFIED",
)

#: The ledger column is 500 characters and nothing they send is longer than a URL; a line
#: longer than this is a server having a bad day, not a response, and is truncated rather
#: than dropped so the shape of it survives.
MAX_RESPONSE_CHARS = 500

#: The one skip reason the duplicate counter keys on.  Written once here so the worker's
#: activity line, the status endpoint's ``duplicates`` tally and the historical rows that
#: predate ``call_exports.response`` all agree on the same string.
DUPLICATE_REASON = "broadcastify already has this call"

#: The skip reason for a call this end already knows is outside Broadcastify's skew window.
#: Written once here for the same reason :data:`DUPLICATE_REASON` is: the worker writes it,
#: the status endpoint counts rows that start with it, and the two must not drift apart.
#:
#: Broadcastify measures the skew as *receipt* − (``ts`` + ``callDuration``) — the age of
#: the call at the moment stage 1 lands, counted from the **end** of the transmission — and
#: answers ``1 REJECTED-CALL-SKEW-TOO-LONG-<secs>`` past its budget.  A call that is already
#: past that budget before we register it cannot become fresher, so registering it spends a
#: round trip (and, in the old order, an ffmpeg encode) on a refusal we can predict.
LATE_REASON = "past the Broadcastify skew window"


def remote_id_for(url: str) -> str | None:
    """The part of a one-time upload URL worth keeping: its last path segment.

    The query string is dropped — it carries the presigning signature and expiry, secrets
    with a lifetime of minutes that have no business in a ledger row — and the host and
    path are identical on every upload, so the segment is the only part that identifies
    *this* one.
    """
    return (url or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:200] or None


# --------------------------------------------------------------------------------------
# environment: url + key + system id, chosen together
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One environment's whole identity: where to post, as whom, for which system.

    Resolved in one place so no caller can assemble a half-and-half request.  ``name`` is
    carried along because it is what the status block and the UI badge report.
    """

    name: str
    url: str
    api_key: str
    system_id: str

    @property
    def is_production(self) -> bool:
        return self.name == "production"

    def configured(self) -> bool:
        """Both credentials present for *this* environment, and somewhere to send them."""
        return bool(self.api_key.strip() and self.system_id.strip() and self.url.strip())

    def missing(self) -> list[str]:
        """The environment-variable names of whichever credentials are blank."""
        prefix = "BROADCASTIFY_API_KEY" if self.is_production else "BROADCASTIFY_DEV_API_KEY"
        system = "BROADCASTIFY_SYSTEM_ID" if self.is_production else "BROADCASTIFY_DEV_SYSTEM_ID"
        out = []
        if not self.api_key.strip():
            out.append(prefix)
        if not self.system_id.strip():
            out.append(system)
        return out


def environment_name(settings: Settings) -> str:
    """``production`` or ``development``; anything else reads as ``production``."""
    raw = str(getattr(settings, "broadcastify_environment", "") or "").strip().lower()
    return raw if raw in ENVIRONMENTS else "production"


def endpoint(settings: Settings) -> Endpoint:
    """The URL, key and system id for the configured environment — never a mixture.

    The development branch reads *only* the ``broadcastify_dev_*`` fields and the
    production branch reads *only* the plain ones.  There is deliberately no fall-through
    from one to the other: a development environment with no development key is
    unconfigured and sends nothing, which is the right answer, whereas quietly borrowing
    the production key would send real credentials to a test host.
    """
    name = environment_name(settings)
    if name == "development":
        return Endpoint(
            name=name,
            url=str(getattr(settings, "broadcastify_dev_url", "") or "").strip(),
            api_key=str(getattr(settings, "broadcastify_dev_api_key", "") or ""),
            system_id=str(getattr(settings, "broadcastify_dev_system_id", "") or ""),
        )
    return Endpoint(
        name="production",
        url=str(getattr(settings, "broadcastify_url", "") or "").strip(),
        api_key=str(settings.broadcastify_api_key or ""),
        system_id=str(settings.broadcastify_system_id or ""),
    )


def monitoring_url(settings: Settings, page: str = "status") -> str:
    """The Monitoring section's URL for the configured environment, or ``""``.

    ``page`` is ``status`` or ``manage``; development has no management page, and the empty
    string is how the UI is told not to render a link to one.
    """
    url = MONITORING.get(environment_name(settings), {}).get(page, "")
    if page == "manage" and url:
        # The node's own management page is /calls/manage/<systemId>; without a system id
        # the bare listing is the best that can be offered.
        system_id = str(endpoint(settings).system_id or "").strip()
        if system_id:
            return f"{url}{system_id}"
    return url


def redact(text: str, settings: Settings) -> str:
    """Blank every configured API key out of a string before it is logged or returned.

    Stage 1 does not echo the key — that has been checked against the response shapes, and
    the observed reply is one line of ``<code> <MESSAGE>`` with nothing of the request in
    it.  This runs anyway.  "It does not echo the key" is a claim about somebody else's
    server that costs nothing to stop relying on, and a ``detail`` field ends up in an
    admin API response, in a log line and in a screenshot.

    Both environments' keys are redacted regardless of which one is in use, because the
    reason to redact is that the string is a secret, not that it is the *active* secret.
    """
    out = text or ""
    for value in (
        getattr(settings, "broadcastify_api_key", ""),
        getattr(settings, "broadcastify_dev_api_key", ""),
    ):
        key = str(value or "").strip()
        if len(key) >= 4:
            out = out.replace(key, "***")
    return out


# --------------------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------------------


def encoding_for(settings: Settings) -> str:
    """``m4a`` or ``mp3``; anything unrecognized reads as the spec's default, ``m4a``."""
    raw = str(getattr(settings, "broadcastify_encoding", "") or "").strip().lower()
    return raw if raw in ENCODINGS else ENCODING


def content_type_for(settings: Settings) -> str:
    """The stage-2 ``Content-Type`` that goes with the configured encoding."""
    return ENCODINGS[encoding_for(settings)]


def mp3_bitrate_kbps(settings: Settings) -> int:
    """32 or 16 — the only two the spec lists.  Anything else reads as 32."""
    try:
        value = int(getattr(settings, "broadcastify_mp3_bitrate_kbps", 32) or 32)
    except (TypeError, ValueError):
        return 32
    return value if value in (32, 16) else 32


# --------------------------------------------------------------------------------------
# the stage-1 form
# --------------------------------------------------------------------------------------


def format_talkgroup(facts: CallFacts) -> str:
    """``tg``: the talkgroup, or for a patch **the patch id first, then every member**.

    The documentation: "trunked: decimal talkgroup; patched calls: comma-delimited list of
    all talkgroups in the patch with the patch ID listed first".  sdrtrunk builds exactly
    that — ``sb.append(patchGroup…getValue()); for(patched: …) sb.append(",").append(patched)``.

    The subtlety is on **our** side, not theirs.  keyup records a patched call under its
    *first member* talkgroup, not under the patch id (``app/ingest/parser.py``: "the primary
    talkgroup of a patch is its first member"), so ``facts.talkgroup`` is a member and the
    patch id arrives separately as :attr:`~app.exporters.base.CallFacts.patch_id`.  Leading
    with ``facts.talkgroup`` — which is what this did before the patch id was carried —
    puts a *member* in the position the patch id is documented to occupy, and Broadcastify
    then files the call under the wrong supergroup.  So: patch id, then the call's own
    talkgroup, then the remaining members.

    Patch 13012 over 13013 and 13014, recorded on 13013, is ``"13012,13013,13014"``.
    """
    if facts.patch_id is not None:
        parts = [facts.patch_id, facts.talkgroup, *facts.patched]
    else:
        parts = [facts.talkgroup, *facts.patched]
    # A patch id that also appears among the members would otherwise be sent twice.
    seen: list[str] = []
    for value in parts:
        text = str(value)
        if text not in seen:
            seen.append(text)
    return ",".join(seen)


def format_frequency(facts: CallFacts) -> str:
    """``freq`` in **MHz**, not Hz.

    sdrtrunk's ``getFrequency()`` is ``value / 1E6f``.  This is the single easiest field to
    get wrong, because OpenMHz's identically-named field is in Hz.
    """
    if not facts.frequency_hz:
        return "0"
    return f"{facts.frequency_hz / 1_000_000:.6f}".rstrip("0").rstrip(".")


def metadata_fields(facts: CallFacts, settings: Settings) -> dict[str, str]:
    """The step-4 registration form, field for field.

    From the documentation's *Registering a call*: ``apiKey`` (string), ``systemId`` (int),
    ``callDuration`` (float seconds), ``ts`` (int, unix timestamp of call **start**), ``tg``
    (string), ``src`` (int, source unit id, decimal), ``freq`` (float, MHz) and the optional
    ``enc``.  ``src`` is ``"0"`` when the recorder never resolved a transmitting unit —
    sdrtrunk's own fallback.  ``apiKey`` and ``systemId`` come from :func:`endpoint`, so
    they always match the URL this form is about to be posted to.
    """
    where = endpoint(settings)
    slot = (getattr(settings, "broadcastify_slot", "") or "").strip()
    fields = {
        "apiKey": where.api_key,
        "systemId": where.system_id,
        "callDuration": f"{facts.duration_s:.1f}",
        "ts": str(facts.start_epoch),
        # A conventional system identifies the channel by a predefined Frequency Slot ID
        # instead of a talkgroup (spec, "conventional"); this deployment is trunked P25 and
        # leaves BROADCASTIFY_SLOT blank, so ``tg`` is the talkgroup.
        "tg": slot or format_talkgroup(facts),
        "src": str(facts.radio_id if facts.radio_id else 0),
        "freq": format_frequency(facts),
        "enc": encoding_for(settings),
    }
    if facts.radio_alias:
        # ``srcId_alias`` is the *transmitting radio's* over-the-air talker alias — an
        # undocumented extension both clients send (sdrtrunk's FormField.RADIO_ID_ALIAS,
        # trunk-recorder's broadcastify_uploader.cc).  It is the unit's name, not the
        # talkgroup's: sending the talkgroup name here would label every call on the
        # channel as coming from a unit of that name.  Omitted entirely when we have no
        # talker alias, which is the common case.
        fields["srcId_alias"] = facts.radio_alias
    return fields


def stage1_record(body: str, settings: Settings | None = None) -> tuple[int | None, str]:
    """The ``(response_code, response)`` pair to store for one stage-1 answer.

    The published table is four lines wide and every one of them is worth keeping, because
    "the queue is busy and nothing is arriving" has four different answers and only the
    service's own words tell them apart:

    ``0 <upload URL>``
        success.  Stored as ``0 <last path segment>`` — the query string is a presigning
        signature and an expiry, secrets with a lifetime of minutes, and the host and path
        are the same on every row.  The segment is the only part that identifies *this*
        upload, and it is the same one :attr:`~app.models.CallExport.remote_id` keeps.
    ``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL``
        the duplicate answer, and on this deployment much the most common one.
    ``1 REJECTED-CALL-SKEW-TOO-LONG-<secs>-See:…``
        not in the published table but observed in production; stored whole, seconds and
        all, because which call was how far out is the thing to look at.
    ``100 NO-API-KEY-SPECIFIED``
        the credential failure.

    An unparseable first token gives a ``None`` code and the line as it stands: an answer
    we do not recognize is exactly the one worth having verbatim.  Only the *first* line is
    kept, :func:`redact` runs over it when settings are supplied, and the result is capped
    at :data:`MAX_RESPONSE_CHARS`.
    """
    lines = (body or "").strip().splitlines()
    text = lines[0].strip() if lines else ""
    if not text:
        return None, ""
    code, _, message = text.partition(" ")
    message = message.strip()
    try:
        number: int | None = int(code)
    except ValueError:
        number = None
    if number == 0 and message:
        # The URL, reduced to the part that is about this upload.  ``split("?")`` first:
        # the signature must not survive even into the truncation below.
        segment = message.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        text = f"0 {segment}" if segment else "0"
    if settings is not None:
        text = redact(text, settings)
    return number, text[:MAX_RESPONSE_CHARS]


def parse_stage1(body: str, settings: Settings | None = None) -> str:
    """Turn one line of stage-1 response into the stage-2 URL, or raise.

    The response is ``<code><SPACE><message>``.  ``0`` means the message is the single-use,
    time-limited upload URL; ``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL`` is a duplicate
    Broadcastify already holds for this (system, talkgroup, timestamp);
    ``100 NO-API-KEY-SPECIFIED`` and its siblings are credential failures that must not be
    retried without a correction; ``1 REJECTED…`` is a refusal that will not change.
    Anything else is worth another try.

    The body is stripped first — **neither reference client does this**, and a trailing
    newline lands inside the URL they then PUT to.

    Every exception raised here carries the answer that caused it (:func:`stage1_record`)
    on ``response``/``response_code``, so the worker can write what the service said to the
    ledger without re-parsing the body.  ``settings`` is optional only because the parser
    is worth calling from a test with nothing configured; the client always passes it, and
    without it the line is not redacted.
    """
    text = (body or "").strip()
    number, stored = stage1_record(text, settings)
    if not text:
        raise ExportError("empty response from the call-upload endpoint")
    code, _, message = text.partition(" ")
    message = message.strip()
    if code == "0":
        if not message:
            raise ExportError("success code with no upload URL", response=stored, response_code=number)
        return message
    upper = message.upper()
    if upper.startswith(SKIP_PREFIX):
        raise SkipCall(DUPLICATE_REASON, response=stored, response_code=number)
    if upper.startswith(REJECT_PREFIX):
        raise PermanentExportError(f"rejected: {message}", response=stored, response_code=number)
    if any(upper.startswith(m) for m in PERMANENT_MESSAGES):
        raise PermanentExportError(
            f"credentials refused: {message}", response=stored, response_code=number
        )
    raise ExportError(f"call-upload said {code} {message}", response=stored, response_code=number)


#: A message's trailing ``-<digits>…`` — the part of
#: ``REJECTED-CALL-SKEW-TOO-LONG-4172-See:https://www.broadcastify.com/skew`` that is about
#: *this* call rather than about the kind of answer it is.
_VARIABLE_TAIL = re.compile(r"^(.*?)-\d")


def response_class(line: str | None) -> str:
    """The bucket a stored response counts in, e.g. ``1 REJECTED-CALL-SKEW-TOO-LONG``.

    A tally of raw lines is useless: every success carries a different URL segment and
    every skew rejection a different number of seconds, so the histogram would have one
    entry per row.  What an operator wants is the four-line response table with a count
    beside each, so the variable tail comes off:

    * ``0 <segment>`` → ``0 OK`` — the segment is per-upload and is already in the row.
    * ``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL`` → ``1 SKIPPED``, the duplicate answer.
    * ``1 REJECTED-CALL-SKEW-TOO-LONG-4172-See:…`` → ``1 REJECTED-CALL-SKEW-TOO-LONG``:
      everything from the first digit run onwards is the offset and their help URL.
    * ``100 NO-API-KEY-SPECIFIED`` → itself; there is no variable part.

    Anything unrecognized — including OpenMHz's ``HTTP 200``, which shares the column —
    reads as the code plus the first word, which is the most that can be said about a line
    this parser has never seen.
    """
    text = (line or "").strip()
    if not text:
        return "unknown"
    code, _, message = text.partition(" ")
    message = message.strip()
    if code == "0":
        return "0 OK"
    if not message:
        return code
    word = message.split(" ", 1)[0]
    if word.upper().startswith(SKIP_PREFIX):
        # ``SKIPPED---ALREADY-RECEIVED-THIS-CALL`` is one word with no digits in it, so the
        # tail rule below would keep the whole thing; the duplicate bucket is named for the
        # code alone because that is what the published table calls it.
        return f"{code} {SKIP_PREFIX}"
    trimmed = _VARIABLE_TAIL.match(word)
    if trimmed and trimmed.group(1):
        word = trimmed.group(1)
    return f"{code} {word}"


def credentials_refused(body: str) -> bool:
    """Does this stage-1 line mean the key was refused, as opposed to anything else?

    Used by :meth:`BroadcastifyClient.test_credentials`, which cares about exactly one
    distinction — "the key is wrong" versus "everything else" — and must not treat a
    ``SKIPPED`` or an unrecognized line as a credential failure.
    """
    text = (body or "").strip()
    code, _, message = text.partition(" ")
    upper = message.strip().upper()
    if code == "100":
        return True
    return any(upper.startswith(m) for m in PERMANENT_MESSAGES)


@dataclass(frozen=True, slots=True)
class Registration:
    """The result of stage 1: where the audio goes, and what the service said getting there.

    ``response`` and ``response_code`` are already in ledger form (:func:`stage1_record`),
    so a PUT that fails afterwards can still record what the *register* answered rather
    than dropping the only line the service gave us.
    """

    url: str
    response_code: int | None = None
    response: str | None = None


class BroadcastifyClient:
    """Steps 4 and 5, over one short-lived :class:`httpx.Client` per call."""

    service = SERVICE

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def endpoint(self) -> Endpoint:
        """The environment in force: URL, key and system id as one unit."""
        return endpoint(self.settings)

    def configured(self) -> bool:
        """The API key and system id **for the selected environment**, or this does nothing.

        The key here is the **Broadcastify Calls** API key from their calls dashboard — not
        the live-feed mount password, which is a different credential for a different
        product.  Switching to ``development`` with no development key configured therefore
        reads as *unconfigured*, which is exactly right: it must not fall back to the
        production key.
        """
        return self.endpoint.configured()

    def test_credentials(self) -> dict[str, object]:
        """The documented credential test: ``test=1`` alongside ``apiKey`` and ``systemId``.

        "Both the development and production endpoints accept a ``test=1`` parameter
        alongside ``apiKey`` and ``systemId`` to verify that credentials are valid before
        sending real calls."  That is a real check of the key — the thing this method could
        not do before — and it is still not an upload: the form carries no ``ts``, no
        ``tg``, no ``freq`` and no audio, so there is no call to register and nothing that
        could hand back an upload URL.  **Nothing is ever PUT from here.**

        The reply is parsed with stage 1's semantics: code ``0`` (or any line that is not
        one of the credential refusals) is an acknowledgement, and
        ``100 NO-API-KEY-SPECIFIED`` / ``Invalid-API-Key`` / ``API-Key-Access-Denied`` mean
        the credentials were refused.  **What a successful reply actually looks like is
        unknown until it has been seen against a real account** — the documentation gives
        the response table for *registering a call*, not for the test — so the raw first
        line is returned verbatim in ``detail`` rather than being interpreted further, and
        an unrecognized line is reported as an acknowledgement rather than as a failure.
        The key is never echoed in any response shape observed, and :func:`redact` strips it
        from the string anyway before it leaves this method.

        The clock is measured here too, because a test press is the moment an operator is
        asking "is this deployment fit to upload?" and step 1 of the workflow is part of the
        answer (:mod:`app.exporters.clock`).
        """
        where = self.endpoint
        check = clock.sampled(self.settings, force=True)
        checked = (
            f"the {where.name} credentials against {where.url or 'the configured endpoint'} "
            "with the documented test=1 parameter; no call is registered and no audio is "
            "uploaded"
        )

        def answer(ok: bool, detail: str, measurement: clock.ClockCheck = check) -> dict[str, object]:
            return {
                "ok": ok,
                "checked": checked,
                "detail": redact(detail, self.settings),
                "environment": where.name,
                **measurement.as_dict(),
            }

        if not where.configured():
            missing = where.missing() or ["BROADCASTIFY_URL"]
            return answer(False, f"not set: {', '.join(missing)}")

        client, owned = self._http()
        sent_at = time.time()
        try:
            response = client.post(
                where.url,
                files={
                    "apiKey": (None, where.api_key),
                    "systemId": (None, where.system_id),
                    "test": (None, "1"),
                },
                headers={"Accept": "*/*"},
            )
        except httpx.HTTPError as exc:
            return answer(False, f"unreachable: {redact(str(exc), self.settings)}")
        finally:
            if owned:
                client.close()
        received_at = time.time()

        # The credential test is also the one HTTP response we are guaranteed to have made,
        # so it is the natural fallback measurement when there is no NTP server.
        if check.offset_s is None:
            check = clock.check_clock(
                self.settings,
                date_header=response.headers.get("Date"),
                sent_at=sent_at,
                received_at=received_at,
                url=where.url,
            )

        first_line = (response.text or "").strip().splitlines()
        line = first_line[0].strip() if first_line else ""
        if response.status_code in (401, 403):
            return answer(False, f"HTTP {response.status_code}: the endpoint refused the credentials", check)
        if response.status_code >= 500:
            return answer(False, f"HTTP {response.status_code} from {where.url}: {line}"[:400], check)
        if credentials_refused(line):
            return answer(False, f"credentials refused: {line}"[:400], check)
        if response.status_code != 200:
            return answer(False, f"HTTP {response.status_code} from {where.url}: {line}"[:400], check)
        return answer(True, f"HTTP 200, answered: {line or '(empty body)'}"[:400], check)

    def _http(self) -> tuple[httpx.Client, bool]:
        """The client to use and whether we own it (and must close it)."""
        if self._client is not None:
            return self._client, False
        timeout = float(getattr(self.settings, "export_timeout_s", 30.0) or 30.0)
        return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}), True

    def register(self, facts: CallFacts) -> Registration:
        """Step 4 alone: register the call and hand back the one-time URL and the answer.

        Split out of :meth:`upload` so a caller can find out whether Broadcastify wants
        this call *before* paying for the encode.  Nine answers in ten on a system another
        feeder also uploads are ``SKIPPED---ALREADY-RECEIVED-THIS-CALL``, and stage 1 costs
        one small POST where the encode costs an ffmpeg run — so registering first turns
        most of the work into no work at all, and takes the call's age at the moment
        Broadcastify measures it down by however long the encode would have taken.
        """
        client, owned = self._http()
        try:
            url, code, response = self._register(client, facts)
        finally:
            if owned:
                client.close()
        return Registration(url=url, response_code=code, response=response)

    def put_audio(self, url: str, audio: bytes) -> None:
        """Step 5 alone: PUT the encoded audio to the URL :meth:`register` returned."""
        client, owned = self._http()
        try:
            self._put_audio(client, url, audio)
        finally:
            if owned:
                client.close()

    def upload(self, facts: CallFacts, audio: bytes) -> UploadResult:
        """Steps 4 and 5 over one connection — the whole-call form of the two above.

        The worker drives :meth:`register` and :meth:`put_audio` separately so it can
        encode between them; this stays the entry point for every other caller, and is what
        :class:`~app.exporters.base.ExportClient` asks for.
        """
        client, owned = self._http()
        try:
            url, code, response = self._register(client, facts)
            self._put_audio(client, url, audio)
        finally:
            if owned:
                client.close()
        # ``response`` is the stage-1 line as stored — the same segment with its ``0`` in
        # front — and ``remote_id`` is the segment on its own.
        return UploadResult(remote_id=remote_id_for(url), response=response, response_code=code)

    def _register(self, client: httpx.Client, facts: CallFacts) -> tuple[str, int | None, str]:
        """Step 4.  Metadata only; the answer is the single-use, time-limited upload URL.

        Returns the URL alongside the ``(code, line)`` the answer is recorded as, because
        the ledger keeps what the service said on a success as well as on a refusal.
        """
        where = self.endpoint
        fields = metadata_fields(facts, self.settings)
        sent_at = time.time()
        try:
            response = client.post(
                where.url,
                # Stage 1 carries no file, but the spec calls for an RFC1867
                # multipart/form-data POST.  ``files`` with a ``None`` filename is how httpx
                # is told to emit a *plain* form field inside a multipart body — one
                # ``Content-Disposition: form-data; name="…"`` part each, with no bogus
                # filename and no extra empty part.
                files={key: (None, value) for key, value in fields.items()},
                headers={"Accept": "*/*"},
            )
        except httpx.HTTPError as exc:
            raise ExportError(f"call-upload unreachable: {redact(str(exc), self.settings)}") from exc
        # Step 1 again, for free: this response's ``Date`` is a clock reading we already
        # have.  Sampled at most once a minute and never allowed to raise, so a burst of
        # uploads costs one header parse.
        clock.sample_response(
            self.settings,
            date_header=response.headers.get("Date"),
            sent_at=sent_at,
            received_at=time.time(),
            url=where.url,
        )
        # An HTTP-level refusal never produced a stage-1 line, so the ledger records the
        # status line as the response rather than pretending to a code we were not given.
        if response.status_code in (401, 403):
            raise PermanentExportError(
                f"call-upload refused the credentials (HTTP {response.status_code})",
                response=f"HTTP {response.status_code}",
            )
        if response.status_code != 200:
            raise ExportError(
                f"call-upload returned HTTP {response.status_code}",
                response=f"HTTP {response.status_code}",
            )
        url = parse_stage1(response.text, self.settings)
        code, stored = stage1_record(response.text, self.settings)
        return url, code, stored

    def _put_audio(self, client: httpx.Client, url: str, audio: bytes) -> None:
        """Step 5.  Raw PUT, explicit ``Content-Length``, no ``Expect: 100-continue``, never chunked.

        The ``Content-Type`` follows the encoding: ``audio/aac`` for M4A/AAC, ``audio/mpeg``
        for MP3, exactly as the Uploading section pairs them.

        Both reference clients go out of their way to disable those two headers
        (``broadcastify_uploader.cc``: ``curl_slist_append(headers, "Expect:")`` and
        ``"Transfer-Encoding:"``); the presigned endpoint wants a plain length-delimited
        body.  Passing ``content=`` bytes to httpx gives exactly that.  The URL is
        single-use and time-limited, so it is used here and never stored.
        """
        try:
            response = client.put(
                url,
                content=audio,
                headers={
                    "Content-Type": content_type_for(self.settings),
                    "Content-Length": str(len(audio)),
                },
            )
        except httpx.HTTPError as exc:
            raise ExportError(f"audio PUT failed: {exc}") from exc
        if response.status_code != 200:
            raise ExportError(f"audio PUT returned HTTP {response.status_code}")
