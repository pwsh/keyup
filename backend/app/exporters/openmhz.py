"""The OpenMHz uploader — one multipart POST.

Field names and formats are cribbed from both ends of the wire, and the *server* is the
authority where the two disagree:

* **server** — ``github.com/openmhz/trunk-server``, ``backend/controllers/uploads.js`` and
  ``backend/index.js``.  ``index.js`` routes
  ``app.post('/:shortName/upload', upload.single('call'), …)`` — multer's ``single('call')``
  is why the audio part *must* be named ``call``.  ``uploads.js`` reads ``api_key``,
  ``talkgroup_num``, ``freq``, ``start_time``, ``stop_time``, ``call_length``,
  ``emergency``, ``error_count``, ``spike_count``, ``source_list`` and ``patch_list``.
* **client** — trunk-recorder ``plugins/openmhz_uploader/openmhz_uploader.cc``, for the
  value formatting (``freq`` in Hz at precision 0, ``call_length`` whole seconds,
  ``emergency`` as ``"1"``/``"0"``) and the URL, ``openmhz_server + "/" + sysid + "/upload"``.

Three details are easy to get wrong and are handled explicitly below:

1. ``freq`` is in **Hz** here and in **MHz** for Broadcastify.
2. The patch field is ``patch_list`` — not ``patches``, which is only the name of the
   Mongo document field — and it is bracketed CSV rather than JSON
   (``uploads.js``: ``req_patches.replace("[","").replace("]","").split(",")``).  Sending
   ``"[]"`` makes the server store a ``NaN``, so an unpatched call omits the field entirely.
3. ``source_list`` is a JSON string and a parse failure is a hard 500, so it is always
   present — ``[]`` at worst.  Only ``pos`` and ``src`` survive on the server
   (``srcSchema = {pos: Number, src: String}``); ``tag`` is accepted and dropped.

Authentication is the ``api_key`` form field.  There is no ``Authorization`` header.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx

from app.config import Settings

from .base import (
    AmbiguousExportError,
    CallFacts,
    ExportError,
    PermanentExportError,
    SkipCall,
    UploadResult,
)

log = logging.getLogger("app.exporters.openmhz")

__all__ = ["OpenMhzClient", "audio_filename", "form_fields", "source_list"]

SERVICE = "openmhz"

USER_AGENT = "keyup-exporter/1.0"

#: The server checks ``path.extname(req.file.originalname)`` and 500s on anything that is
#: not ``.m4a`` or ``.mp3``; the part's declared MIME type is never looked at.
AUDIO_CONTENT_TYPE = "audio/mp4"

#: 500-body substrings trunk-recorder classifies as configuration errors — retrying them
#: cannot help (``openmhz_uploader.cc``, the ``config_errors[]`` table).
PERMANENT_BODIES = (
    "API Keys do not match",
    "ShortName does not exist",
    "Error, invalid filename",
)
#: …and the one that is a deliberate server-side skip rather than an error.
SKIP_BODY = "Talkgroup does not exist"

#: The statuses that mean "a gateway in front of OpenMHz gave up waiting", not "OpenMHz
#: refused": 502/504, and Cloudflare's 520 (origin sent something unusable) and 524 (origin
#: took too long).  The upload handler (``uploads.js``) answers ``200`` *before* its S3 put
#: and Mongo save and every refusal it makes is a ``500`` sent before either, so a ``500``
#: from the application is a clean "not stored" and safe to retry — but one of these can
#: arrive after the application already said yes to a request the gateway stopped
#: waiting for.  Ambiguous, then (:class:`~app.exporters.base.AmbiguousExportError`).
AMBIGUOUS_STATUSES = frozenset({502, 504, 520, 524})

#: The httpx failures that can only happen *after* the request was written: waiting for,
#: or reading, the status line.  A connect error, a connect/pool timeout or a write error
#: means the body never fully left and the server has nothing to act on — retried as ever.
AMBIGUOUS_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)

#: How many calls one lookup reads — the server's own page size (``defaultNumResults``).
LOOKUP_PAGE = 50


def audio_filename(facts: CallFacts) -> str:
    """``{talkgroup}-{epoch}_{freqHz}.m4a`` — trunk-recorder's own naming convention.

    Only the extension is load-bearing (the server renames the file to
    ``media/{shortName}/{tg}/{shortName}-{tg}-{start_time}.m4a``), but matching the
    convention keeps their logs readable.
    """
    return f"{facts.talkgroup}-{facts.start_epoch}_{facts.frequency_hz or 0}.m4a"


def source_list(facts: CallFacts) -> str:
    """The ``source_list`` JSON: one entry per transmitting unit, ``pos`` seconds in.

    Keyup stores one radio per call, so the list is either empty or a single entry at
    ``pos: 0.00``.  ``pos`` is a float at two decimals and ``src`` an unquoted integer,
    exactly as ``openmhz_uploader.cc`` emits them.  ``emergency`` and ``signal_system``
    exist on trunk-recorder's ``Call_Source`` but are deliberately *not* part of this
    payload, and are not sent here either.
    """
    if not facts.radio_id:
        return "[]"
    entry = {"pos": 0.00, "src": facts.radio_id}
    if facts.radio_alias:
        # ``tag`` is the unit's own label — the transmitting radio's talker alias, never
        # the talkgroup name.  The current server drops it (``srcSchema`` keeps only
        # ``pos`` and ``src``), but sending the wrong thing would be wrong the moment they
        # start keeping it.  Omitted when we have no talker alias.
        entry["tag"] = facts.radio_alias
    return json.dumps([entry])


def form_fields(facts: CallFacts, settings: Settings) -> dict[str, str]:
    """Every non-file part of the request."""
    fields = {
        "api_key": settings.openmhz_api_key,
        "talkgroup_num": str(facts.talkgroup),
        # Hz, integer-formatted — ``setprecision(0)`` on a double in the reference client.
        "freq": str(facts.frequency_hz or 0),
        "start_time": str(facts.start_epoch),
        "stop_time": str(facts.stop_epoch),
        # whole seconds; the server falls back to (stop - start) if it is missing
        "call_length": str(int(round(facts.duration_s))),
        "emergency": "1" if facts.emergency else "0",
        "error_count": "0",
        "spike_count": "0",
        "source_list": source_list(facts),
    }
    if len(facts.patched) >= 1:
        # Bracketed CSV, patched members alongside the primary talkgroup.  Omitted entirely
        # when there is no patch: the server turns a literal "[]" into a NaN entry.
        members = ",".join(str(t) for t in (facts.talkgroup, *facts.patched))
        fields["patch_list"] = f"[{members}]"
    return fields


def classify(status: int, body: str) -> None:
    """Raise the right exception for a non-success response, per the server's own strings.

    Every exception carries ``response`` — ``HTTP <status>: <body>``, capped — because the
    ledger records what the service said on every attempt.  OpenMHz has no ``response_code``
    of its own: the leading integer the column holds is Broadcastify's stage-1 code, and
    inventing an HTTP status in that field would make the two services' rows uncomparable.
    """
    text = (body or "").strip()
    said = f"HTTP {status}: {text}"[:500] if text else f"HTTP {status}"
    if any(marker in text for marker in PERMANENT_BODIES):
        raise PermanentExportError(f"openmhz refused the request: {text[:200]}", response=said)
    if SKIP_BODY in text:
        raise SkipCall("openmhz does not know this talkgroup", response=said)
    if status in (401, 403):
        raise PermanentExportError(f"openmhz refused the credentials (HTTP {status})", response=said)
    if status in AMBIGUOUS_STATUSES:
        raise AmbiguousExportError(
            f"openmhz's gateway answered HTTP {status} after the upload was sent; it may "
            "have been stored",
            response=said,
        )
    raise ExportError(f"openmhz returned HTTP {status}: {text[:200]}", response=said)


def _epoch(value: object) -> int | None:
    """A call's ``time`` from the calls API as whole Unix seconds, or ``None``.

    Mongo's ``Date`` serializes as an ISO string (``2026-09-23T14:05:07.000Z``); a number is
    taken as milliseconds, which is what the server stores (``start_time * 1000``).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value // 1000)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


class OpenMhzClient:
    """One POST per call to ``{OPENMHZ_URL}/{shortName}/upload``."""

    service = SERVICE

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client

    def configured(self) -> bool:
        return bool(
            (self.settings.openmhz_api_key or "").strip()
            and (self.settings.openmhz_short_name or "").strip()
        )

    @property
    def url(self) -> str:
        base = (self.settings.openmhz_url or "").rstrip("/")
        # The server lowercases the segment itself (``req.params.shortName.toLowerCase()``);
        # sending it lowercase keeps their access log matching what we configured.
        return f"{base}/{self.settings.openmhz_short_name.strip().lower()}/upload"

    @property
    def calls_url(self) -> str:
        """The public read endpoint for this system — what :meth:`test_credentials` asks."""
        base = (self.settings.openmhz_url or "").rstrip("/")
        return f"{base}/{(self.settings.openmhz_short_name or '').strip().lower()}/calls"

    def test_credentials(self) -> dict[str, object]:
        """Verify the **short name** against their public read API; the key is not checkable.

        ``/{shortName}/calls`` is a plain public GET that 200s for a system that exists, so
        a typo in ``OPENMHZ_SHORT_NAME`` is caught here.  The API key is only ever validated
        by an actual upload (the server compares it inside ``/upload``), and uploading a
        call to test a switch that is off is not something this button will do.
        """
        checked = (
            "short name resolved against the public calls API and the host reachable; the "
            "API key is only verified by a real upload"
        )
        if not self.configured():
            missing = [
                name
                for name, value in (
                    ("OPENMHZ_API_KEY", self.settings.openmhz_api_key),
                    ("OPENMHZ_SHORT_NAME", self.settings.openmhz_short_name),
                )
                if not str(value or "").strip()
            ]
            return {"ok": False, "checked": checked, "detail": f"not set: {', '.join(missing)}"}
        client, owned = self._http()
        try:
            response = client.get(self.calls_url)
        except httpx.HTTPError as exc:
            return {"ok": False, "checked": checked, "detail": f"unreachable: {exc}"}
        finally:
            if owned:
                client.close()
        if response.status_code == 404:
            return {
                "ok": False,
                "checked": checked,
                "detail": f"openmhz does not know a system called {self.settings.openmhz_short_name!r}",
            }
        return {
            "ok": response.status_code < 500,
            "checked": checked,
            "detail": f"{self.calls_url} answered HTTP {response.status_code}",
        }

    def _http(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        timeout = float(getattr(self.settings, "export_timeout_s", 30.0) or 30.0)
        return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}), True

    def find_uploaded(self, facts: CallFacts) -> UploadResult | None:
        """Does OpenMHz already hold this call?  The check before re-posting an ambiguous one.

        OpenMHz has no idempotency key: a second POST of the same call is a second call in
        their archive.  So a row whose last attempt ended ambiguously is looked up first on
        the public read API — ``GET /{shortName}/calls/newer`` filtered to the talkgroup,
        starting one millisecond before the call (``calls.js``: ``time > start``, oldest
        first, 50 at most) — and matched on talkgroup and **start second**, which is exactly
        what the server stored (``new Date(start_time * 1000)``).

        Returns an :class:`UploadResult` carrying their ``_id`` when it is there, ``None``
        when it is not, and raises :class:`ExportError` when the question could not be
        answered — which the worker treats as "still do not know", never as "not there".
        """
        base = (self.settings.openmhz_url or "").rstrip("/")
        short = (self.settings.openmhz_short_name or "").strip().lower()
        client, owned = self._http()
        try:
            response = client.get(
                f"{base}/{short}/calls/newer",
                params={
                    "time": str(facts.start_epoch * 1000 - 1),
                    "filter-type": "talkgroup",
                    "filter-code": str(facts.talkgroup),
                },
            )
        except httpx.HTTPError as exc:
            raise ExportError(f"openmhz lookup failed: {exc}") from exc
        finally:
            if owned:
                client.close()
        if response.status_code != 200:
            raise ExportError(f"openmhz lookup answered HTTP {response.status_code}")
        try:
            listed = response.json().get("calls")
        except (ValueError, AttributeError) as exc:
            raise ExportError("openmhz lookup answered something that is not a call list") from exc
        if not isinstance(listed, list):
            raise ExportError("openmhz lookup answered something that is not a call list")
        for item in listed[:LOOKUP_PAGE]:
            if not isinstance(item, dict):
                continue
            try:
                talkgroup = int(item.get("talkgroupNum"))
            except (TypeError, ValueError):
                continue
            if talkgroup == int(facts.talkgroup) and _epoch(item.get("time")) == facts.start_epoch:
                remote = str(item.get("_id") or "") or None
                return UploadResult(
                    remote_id=remote,
                    response=f"found on openmhz as {remote or 'an existing call'} after an unanswered upload",
                )
        return None

    def upload(self, facts: CallFacts, audio: bytes) -> UploadResult:
        client, owned = self._http()
        try:
            response = client.post(
                self.url,
                data=form_fields(facts, self.settings),
                files={"call": (audio_filename(facts), audio, AUDIO_CONTENT_TYPE)},
            )
        except AMBIGUOUS_ERRORS as exc:
            # The body was written and the answer never came: OpenMHz may well have the
            # call, and it does not deduplicate, so this must not be retried blind.
            raise AmbiguousExportError(
                f"openmhz did not answer after the upload was sent ({type(exc).__name__}: {exc})"
            ) from exc
        except httpx.HTTPError as exc:
            raise ExportError(f"openmhz unreachable: {exc}") from exc
        finally:
            if owned:
                client.close()
        if response.status_code != 200:
            classify(response.status_code, response.text)
        # A 200 body is empty, and is returned *before* the S3 put and the Mongo save — it
        # means "accepted", not "stored".  There is no identifier to keep, so the ledger
        # gets the status line: the same shape a refusal records, so a row's ``response``
        # always says what was heard.
        return UploadResult(remote_id=None, response=f"HTTP {response.status_code}")
