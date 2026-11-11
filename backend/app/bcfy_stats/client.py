"""The Broadcastify **feed owner** API client — one GET, and a tolerant reading of it.

The owner API (``https://api.broadcastify.com/owner/``) is a single endpoint with an
``a=`` method, a ``type=`` of ``json`` or ``xml``, the ``feedId``, and the owner's own
Broadcastify ``u``/``p`` login.  Two methods matter here:

``a=feed``
    status and statistics for one feed the account owns;
``a=archives``
    every day the feed has archives for, or — with ``day=YYYY-MM-DD`` — that day's
    individual blocks, each with a timeframe and a download link.

**What is guessed, and how the guess is contained.**  Broadcastify publishes the
*parameters* but not the *response*: the field names of ``a=feed`` are not documented
anywhere this deployment has seen.  So nothing here depends on getting them right.  The
whole response is stored verbatim (``bcfy_feed_samples.raw``), and the interpretation is
:data:`FIELD_ALIASES` — a list of candidate names per concept, matched
case-insensitively after stripping ``_``/``-``/spaces, so ``currentListeners``,
``current_listeners`` and ``CURRENTLISTENERS`` are one name.  Widening it is a one-line
change, and the history is re-derivable from ``raw`` because ``raw`` is still there.
``keyup bcfy-stats probe`` (and ``POST /api/admin/bcfy-stats/probe``) exist to make one
real request and print exactly what came back, which is how the guesses get replaced with
observations.

**The real response is now known** (2026-09, ``a=feed&type=json``)::

    {"Feed": [{"id": 12345, "descr": "...", "genre": "Public Safety",
               "mount": "/examplemount", "Relays": [...], "status": 1,
               "bitrate": 32, "Counties": [...], "listeners": 12}]}

— a capital ``Feed`` holding a *list*, ``listeners`` for the count and ``status`` (``1``)
for up.  It carries no history whatsoever: one instant's listener count and nothing else,
which is why the listener chart starts at this deployment's first poll and can never be
backfilled.  The guessing machinery below stays exactly as it was, because one observed
response is one account's response and the mapping costs nothing.

The response is also allowed to be *shaped* differently: a bare object, an object with the
feed under ``feed`` / ``Feed`` / ``feeds``, or a one-element list of those.
:func:`feed_payload` unwraps all of them, and ``type=xml`` is parsed into the same shape
(attributes and leaf text both become keys) so the mapping runs once for either format.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("app.bcfy_stats.client")

__all__ = [
    "ARCHIVE_ALIASES",
    "FIELD_ALIASES",
    "BcfyOwnerClient",
    "BcfyOwnerError",
    "archive_rows",
    "extract",
    "feed_payload",
    "parse_body",
    "redact",
]

USER_AGENT = "keyup-bcfy-stats/1.0"

#: One request's ceiling.  The owner API answers in well under a second; ten seconds is a
#: dead connection, not a slow one.
TIMEOUT_S = 10.0


class BcfyOwnerError(RuntimeError):
    """The API could not be reached, or answered with something unusable."""


# --------------------------------------------------------------------------------------
# the mapping table — THE GUESS.  See docs/BCFY_STATS.md.
# --------------------------------------------------------------------------------------

#: ``concept -> candidate field names``, most specific first.  Names are normalized
#: (lowercased, ``_``/``-``/space removed) on both sides before comparison, so only the
#: *letters* here matter.  Everything a lookup finds is recorded in
#: ``bcfy_feed_samples.extracted`` under the concept's name; ``listeners`` and ``online``
#: additionally get their own columns because they are what every chart reads.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "listeners": ("currentlisteners", "listeners", "listenercount", "numlisteners", "clisteners"),
    "listeners_peak": ("peaklisteners", "maxlisteners", "listenerspeak", "peak"),
    "listeners_total": ("totallisteners", "listenerstotal", "alltimelisteners"),
    "status": ("status", "feedstatus", "state"),
    # ``status`` is last so a real boolean field still wins, but the observed response
    # carries *only* ``status: 1`` — see ``feed_sample.json`` — so the concept has to be
    # reachable from it directly and not merely through the fallback in `extract`.
    "online": ("online", "isonline", "up", "active", "broadcasting", "status"),
    "name": ("descr", "description", "feedname", "name", "title"),
    "genre": ("genre", "category", "type"),
    "mount": ("mount", "mountpoint", "mountpt"),
    "bitrate": ("bitrate", "kbps"),
    "listeners_limit": ("listenerlimit", "maxconnections", "limit"),
    "started_at": ("starttime", "startedat", "since", "uptimestart"),
    "archives_enabled": ("archive", "archives", "archiveenabled", "archivesenabled"),
}

#: The same idea for one row of ``a=archives&day=``.  ``start``/``end`` are epoch seconds
#: in Broadcastify's own archive URLs, so both an epoch and an ISO-ish string are accepted.
ARCHIVE_ALIASES: dict[str, tuple[str, ...]] = {
    "start": ("start", "starttime", "ts", "begin", "startat"),
    # The observed ``a=archives&day=`` row: ``{"archive": "10:35 PM - 10:50 PM", "url": …}``.
    "range": ("archive", "range", "time", "timeframe", "span"),
    "end": ("end", "endtime", "stop", "endat"),
    "url": ("url", "file", "link", "href", "download", "downloadurl"),
    "size": ("size", "filesize", "bytes", "sizebytes"),
    "day": ("day", "date"),
}

#: Query-parameter names that must never reach a log line or an error message.
SECRET_PARAMS = frozenset({"p", "password", "pw"})

#: Values that read as "yes" from an API that has never committed to a boolean type.
TRUTHY = frozenset({"1", "true", "yes", "y", "on", "online", "up", "active"})
FALSY = frozenset({"0", "false", "no", "n", "off", "offline", "down", "inactive"})


def _norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def redact(text: str) -> str:
    """Blank every secret query parameter in a URL or an error string.

    The password travels in the query string — that is the API's design, not a choice made
    here — which means a URL is a credential.  Nothing logs or returns one that has not
    been through this function.
    """
    out = text
    for name in SECRET_PARAMS:
        out = _redact_param(out, name)
    return out


def _redact_param(text: str, name: str) -> str:
    lowered = text.lower()
    needle = f"{name}="
    result: list[str] = []
    index = 0
    while True:
        at = lowered.find(needle, index)
        if at == -1:
            result.append(text[index:])
            return "".join(result)
        # only a real parameter boundary, not the tail of another name (…&pp=)
        if at > 0 and text[at - 1] not in "?&":
            result.append(text[index : at + len(needle)])
            index = at + len(needle)
            continue
        end = len(text)
        for stop in ("&", " "):
            found = text.find(stop, at)
            if found != -1:
                end = min(end, found)
        result.append(text[index:at])
        result.append(f"{name}=***")
        index = end


def _lookup(payload: dict[str, Any], names: tuple[str, ...]) -> Any:
    """The first key of ``payload`` that normalizes to one of ``names``, in order."""
    normalized = {_norm(key): value for key, value in payload.items()}
    for name in names:
        value = normalized.get(_norm(name))
        if value not in (None, ""):
            return value
    return None


def as_bool(value: Any) -> bool | None:
    """A boolean out of whatever the API used to mean one; ``None`` when it is unclear."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def _xml_to_dict(element: ElementTree.Element) -> Any:
    """One element as a dict: attributes, child elements, and leaf text all as keys.

    Repeated children collapse into a list, which is what makes ``<archives><archive/>…``
    read the same as the JSON form's list.
    """
    node: dict[str, Any] = {key: value for key, value in element.attrib.items()}
    for child in element:
        value = _xml_to_dict(child)
        if child.tag in node:
            existing = node[child.tag]
            node[child.tag] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            node[child.tag] = value
    text = (element.text or "").strip()
    if text and not node:
        return text
    if text:
        node.setdefault("text", text)
    return node


def parse_body(body: str, content_type: str = "") -> Any:
    """A response body as Python, whichever of the two formats it turned out to be.

    ``type=json`` is asked for first and is not always what comes back — an error page, or
    an account whose plan answers in XML regardless — so the body is sniffed rather than
    trusted: a leading ``{``/``[`` is JSON, a leading ``<`` is XML, and anything else is a
    failure the caller reports rather than stores.
    """
    import json  # noqa: PLC0415 - only this function needs it

    stripped = body.strip()
    if not stripped:
        raise BcfyOwnerError("the API returned an empty body")
    if stripped[0] in "{[":
        try:
            return json.loads(stripped)
        except ValueError as exc:
            raise BcfyOwnerError(f"unparseable JSON: {exc}") from exc
    if stripped[0] == "<":
        try:
            root = ElementTree.fromstring(stripped)  # noqa: S314 - Broadcastify's own document
        except ElementTree.ParseError as exc:
            raise BcfyOwnerError(f"unparseable XML: {exc}") from exc
        return {root.tag: _xml_to_dict(root)}
    if "json" in content_type.lower():
        raise BcfyOwnerError("the API declared JSON and sent something else")
    raise BcfyOwnerError("the API returned neither JSON nor XML")


def feed_payload(document: Any) -> dict[str, Any]:
    """The one feed object inside whatever envelope the response used.

    Accepts a bare object, ``{"feed": {...}}``, ``{"feeds": [{...}]}``, a list of one, and
    the XML form's ``{"broadcastify": {"feed": {...}}}`` — all of which are plausible and
    none of which is documented.  An unrecognized shape is returned as-is rather than
    rejected: an empty extraction beside a stored ``raw`` is recoverable, a dropped sample
    is not.
    """
    node: Any = document
    for _ in range(4):  # a bounded unwrap; a self-referential document cannot spin here
        if isinstance(node, list):
            if not node:
                return {}
            node = node[0]
            continue
        if not isinstance(node, dict):
            return {}
        keys = {_norm(key): key for key in node}
        for name in ("feed", "feeds", "broadcastify", "owner", "response", "data"):
            key = keys.get(name)
            if key is not None and isinstance(node[key], dict | list):
                node = node[key]
                break
        else:
            return node
    return node if isinstance(node, dict) else {}


def extract(document: Any) -> dict[str, Any]:
    """Everything :data:`FIELD_ALIASES` recognizes in one ``a=feed`` response.

    Concepts the response did not carry are simply absent, so a widened mapping shows up
    as new keys rather than as a schema change.
    """
    payload = feed_payload(document)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for concept, names in FIELD_ALIASES.items():
        value = _lookup(payload, names)
        if value is None:
            continue
        if isinstance(value, dict | list):
            # A nested object is not a scalar reading; keep it out of `extracted` rather
            # than stringifying something we do not understand.  `raw` still has it.
            continue
        out[concept] = value
    listeners = as_int(out.get("listeners"))
    if listeners is not None:
        out["listeners"] = listeners
    online = as_bool(out.get("online"))
    if online is None:
        online = as_bool(out.get("status"))
    if online is not None:
        out["online"] = online
    return out


def _as_datetime(value: Any) -> datetime | None:
    """An instant out of an epoch (seconds or milliseconds) or an ISO-8601 string."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) or str(value).strip().lstrip("-").isdigit():
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        moment = _as_datetime(value)
        return moment.date() if moment else None


def _rows(document: Any) -> list[Any]:
    """Every archive-ish row in an ``a=archives`` document, whatever it wrapped them in."""
    node: Any = document
    for _ in range(5):
        if isinstance(node, list):
            return [row for row in node if isinstance(row, dict | str)]
        if not isinstance(node, dict):
            return []
        keys = {_norm(key): key for key in node}
        for name in ("archives", "archive", "days", "day", "broadcastify", "response", "data"):
            key = keys.get(name)
            if key is not None:
                node = node[key]
                break
        else:
            # a bare object that is itself one row
            return [node]
    return []


_RANGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*[-–]\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$")


def _clock(hour: int, minute: int, meridian: str | None) -> tuple[int, int] | None:
    if meridian:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridian.lower() == "pm" else 0)
    elif not 0 <= hour <= 23:
        return None
    return (hour, minute) if 0 <= minute <= 59 else None


def parse_archive_range(text: Any, *, day: date, tz: str) -> tuple[datetime, datetime] | None:
    """``"10:35 PM - 10:50 PM"`` on ``day``, read on the feed's own clock, as two instants.

    This is the form the owner API actually uses (the block's start and end on the
    feed's local clock, and nothing else — the URL's own stamp is the upload moment).  A
    block that ends "earlier" than it starts crossed midnight; its end is on the next day.
    """
    if not isinstance(text, str):
        return None
    match = _RANGE.match(text)
    if match is None:
        return None
    sh, sm, sa, eh, em, ea = match.groups()
    # "11:45 - 12:15 AM" style: the first half borrows the second's meridian.
    start = _clock(int(sh), int(sm), sa or ea)
    end = _clock(int(eh), int(em), ea or sa)
    if start is None or end is None:
        return None
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError):
        zone = UTC
    start_at = datetime(day.year, day.month, day.day, start[0], start[1], tzinfo=zone)
    end_at = datetime(day.year, day.month, day.day, end[0], end[1], tzinfo=zone)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at.astimezone(UTC), end_at.astimezone(UTC)


def archive_rows(document: Any, *, day: date | None = None, tz: str = "UTC") -> list[dict[str, Any]]:
    """The ``a=archives&day=`` listing as ``{day, start_at, end_at, url, size_bytes, raw}``.

    Rows with no usable start instant are dropped: ``start_at`` is the row's identity
    (``UNIQUE (feed_id, start_at)``) and a row without one cannot be upserted.  ``day``
    falls back to the day that was asked for, which is what the date-list form gives.
    ``tz`` is the feed's clock, needed only for the ``"10:35 PM - 10:50 PM"`` row form.
    """
    out: list[dict[str, Any]] = []
    for row in _rows(document):
        if not isinstance(row, dict):
            continue
        start = _as_datetime(_lookup(row, ARCHIVE_ALIASES["start"]))
        end = _as_datetime(_lookup(row, ARCHIVE_ALIASES["end"]))
        if start is None:
            row_day_hint = _as_date(_lookup(row, ARCHIVE_ALIASES["day"])) or day
            span = (
                parse_archive_range(_lookup(row, ARCHIVE_ALIASES["range"]), day=row_day_hint, tz=tz)
                if row_day_hint is not None
                else None
            )
            if span is None:
                continue
            start, end = span
        row_day = _as_date(_lookup(row, ARCHIVE_ALIASES["day"])) or day or start.date()
        url = _lookup(row, ARCHIVE_ALIASES["url"])
        out.append(
            {
                "day": row_day,
                "start_at": start,
                "end_at": end,
                "url": str(url) if url is not None else None,
                "size_bytes": as_int(_lookup(row, ARCHIVE_ALIASES["size"])),
                "raw": row,
            }
        )
    return out


def archive_days(document: Any) -> list[date]:
    """The plain ``a=archives`` answer: every day the feed has archives for.

    The rows are dates in some form — an object with a ``date``/``day`` field, or a bare
    string — so both are read, and anything unparseable is skipped rather than fatal.
    """
    days: list[date] = []
    for row in _rows(document):
        value = _lookup(row, ARCHIVE_ALIASES["day"]) if isinstance(row, dict) else row
        if value is None and isinstance(row, dict):
            value = _lookup(row, ARCHIVE_ALIASES["start"])
        parsed = _as_date(value)
        if parsed is not None and parsed not in days:
            days.append(parsed)
    return sorted(days)


class BcfyOwnerClient:
    """One feed's owner-API calls.  Stateless apart from the credentials and the HTTP client.

    HTTPS always: the documented endpoint offers plain HTTP too, and the password is a
    query parameter, so plain HTTP would put the operator's Broadcastify login on the wire
    in clear text on every poll.
    """

    def __init__(
        self,
        *,
        username: str,
        password: str,
        feed_id: str,
        url: str = "https://api.broadcastify.com/owner/",
        timeout: float = TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.feed_id = str(feed_id)
        self.url = url
        self.timeout = timeout
        self._client = client

    def _params(self, action: str, response_type: str, **extra: str) -> dict[str, str]:
        return {
            "a": action,
            "type": response_type,
            "feedId": self.feed_id,
            "u": self.username,
            "p": self.password,
            **extra,
        }

    def _get(self, action: str, **extra: str) -> Any:
        """One call, JSON first and XML as the fallback.

        ``type=json`` is what this asks for; an account or a method that answers in XML
        anyway is handled by :func:`parse_body` sniffing the body.  A *rejected* JSON
        request — a non-2xx, or a body that is neither format — is retried once as
        ``type=xml`` before it is reported as a failure, because the documentation's own
        ``a=archives`` example is an XML one and it is not clear that every method offers
        both.
        """
        last: Exception | None = None
        for response_type in ("json", "xml"):
            try:
                return self._request(action, response_type, extra)
            except BcfyOwnerError as exc:
                last = exc
        raise BcfyOwnerError(str(last))

    def _request(self, action: str, response_type: str, extra: dict[str, str]) -> Any:
        client = self._client or httpx.Client(timeout=self.timeout, follow_redirects=True)
        try:
            response = client.get(
                self.url,
                params=self._params(action, response_type, **extra),
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError as exc:
            raise BcfyOwnerError(redact(f"{type(exc).__name__}: {exc}")) from exc
        finally:
            if self._client is None:
                client.close()
        if response.status_code >= 400:
            # The body of an error is Broadcastify's own text and can echo the request, so
            # it goes through `redact` before it is ever shown or logged.
            detail = redact(response.text.strip()[:200])
            raise BcfyOwnerError(f"HTTP {response.status_code}: {detail or 'no body'}")
        return parse_body(response.text, response.headers.get("content-type", ""))

    # ------------------------------------------------------------------ the two methods

    def feed(self) -> Any:
        """``a=feed`` — the whole response, exactly as it arrived."""
        return self._get("feed")

    def archives(self, day: date | str | None = None) -> Any:
        """``a=archives`` — every archived day, or one day's blocks when ``day`` is given."""
        if day is None:
            return self._get("archives")
        text = day.isoformat() if isinstance(day, date) else str(day)
        return self._get("archives", day=text)
