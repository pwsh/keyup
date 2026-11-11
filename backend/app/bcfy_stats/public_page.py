"""The **public** feed page — the only place Broadcastify shows listener *history*.

The owner API answers with one instant's listener count and nothing else
(``docs/BCFY_STATS.md``), so a deployment switched on this morning has no way to say what
last night looked like.  The page at ``https://www.broadcastify.com/listen/feed/<id>`` does:
it is public, needs no login, and server-renders a **24-hour hourly sparkline** — one bar
per hour, oldest first, each carrying its listener count in a ``<title>`` for the tooltip.
Twenty-four hours is not a year, but it is the only backfill that exists, and it is what
turns the first day of a new poller from a blank grid into a day.

**This is scraped HTML and nothing here pretends otherwise.**  Broadcastify publishes no
contract for a page they render for browsers; the classes below (``listen-sparkline-bar``,
``lp-detail-status``) are what the markup used in September 2026 and they may change on any
deploy of theirs.  So the same rule the owner client follows applies twice over here:

* :func:`parse_public_page` is **pure** and **never raises**.  Every piece it looks for is
  independently optional — a page with a sparkline and no status badge yields the bars and
  ``online=None`` — because a partial answer from a changed page is worth more than an
  exception, and a page that changed beyond recognition simply yields an empty
  :class:`PublicPage` that writes nothing.
* the bar count is **reported, not asserted**.  Twenty-four is what the page draws today;
  a page that draws 12 or 48 is parsed as 12 or 48 and the caller (see
  :func:`app.bcfy_stats.store.record_page_hours`) lays them into the last *n* hours rather
  than assuming a day.

What is written from a parsed page, and what a page may never overwrite, is the merge rule
in :mod:`app.bcfy_stats.store`: a page value fills an hour nobody polled and a real poll
always wins.  The page is a *fallback*, never a correction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger("app.bcfy_stats.public_page")

__all__ = [
    "PUBLIC_PAGE_URL",
    "TIMEOUT_S",
    "USER_AGENT",
    "PublicPage",
    "PublicPageError",
    "fetch_public_page",
    "parse_public_page",
]

#: The public listen page.  ``https`` is baked into the template rather than configurable:
#: there is no credential in this request, but there is no reason to accept a downgrade
#: either, and a settable URL would be one more thing to point somewhere unexpected.
PUBLIC_PAGE_URL = "https://www.broadcastify.com/listen/feed/{feed_id}"

#: One page fetch's ceiling.  Longer than the owner API's ten seconds because this is a
#: rendered HTML page behind a CDN rather than a small JSON endpoint.
TIMEOUT_S = 15.0

#: A browser-ish agent.  This is a page meant for browsers; asking for it as ``keyup/1.0``
#: invites a WAF to answer with something that is not the page.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class PublicPageError(RuntimeError):
    """The public page could not be fetched, or did not answer with a page."""


@dataclass
class PublicPage:
    """What one render of the public feed page said.

    Every field is independently optional.  ``bars`` is the reason this module exists —
    hourly listener counts, **oldest first** — and the rest is what the page happens to show
    beside it, useful for a status line and for confirming the page is the right feed's.
    """

    #: hourly listener counts, oldest first.  24 on today's page; whatever the page drew.
    bars: list[int] = field(default_factory=list)
    #: the page's own "Min / Max / Now" summary of the same 24 hours, when it renders one
    min: int | None = None
    max: int | None = None
    now: int | None = None
    #: the status badge, read from its modifier class and falling back to its text
    online: bool | None = None
    #: when the current up/down state began (``data-since``, epoch seconds), as UTC
    since: datetime | None = None
    #: the "Listeners" row of the detail table — the same number the owner API's poll gets
    listeners: int | None = None

    @property
    def empty(self) -> bool:
        """True when the page yielded nothing at all — a redesign, or not a feed page."""
        return not self.bars and self.online is None and self.listeners is None


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------

#: One bar: the count in its tooltip ``<title>``.  Matched on the class rather than on
#: ``<rect>`` so the baseline line and any other shape in the same ``<svg>`` are skipped.
_BAR_RE = re.compile(
    r"<rect\b[^>]*\blisten-sparkline-bar\b[^>]*>\s*<title>\s*(-?\d+)",
    re.IGNORECASE,
)
#: The same bars, with the ``x`` the page lays them out on (left to right, oldest first).
_BAR_X_RE = re.compile(
    r"<rect\b[^>]*\blisten-sparkline-bar\b[^>]*\bx=\"(-?[\d.]+)\"[^>]*>\s*<title>\s*(-?\d+)",
    re.IGNORECASE,
)
_STATS_RE = re.compile(
    r"\blisten-sparkline-stats\b[^>]*>(.{0,600}?)</span>\s*</span>",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_RE = re.compile(r"id=\"lp-detail-status\"[^>]*>(.{0,400}?)</span>\s*<", re.IGNORECASE | re.DOTALL)
_SINCE_RE = re.compile(r"id=\"lp-detail-duration\"[^>]*\bdata-since=\"(\d+)\"", re.IGNORECASE)
_LISTENERS_RE = re.compile(r"id=\"lp-detail-listeners\"[^>]*>\s*([\d,]+)", re.IGNORECASE)


def _stat(block: str, label: str) -> int | None:
    match = re.search(rf"\b{label}\b\s*<strong>\s*(-?\d+)", block, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _bars(html: str) -> list[int]:
    """Every sparkline bar's listener count, ordered oldest first.

    The page emits them in order already, but they carry an explicit ``x`` and ordering by
    it is free: a future page that emits them in some other order still reads left to
    right, which is the axis the counts are on.
    """
    plain = _BAR_RE.findall(html)
    with_x = _BAR_X_RE.findall(html)
    if with_x and len(with_x) == len(plain):
        return [int(count) for _x, count in sorted(with_x, key=lambda pair: float(pair[0]))]
    return [int(count) for count in plain]


def parse_public_page(html: str) -> PublicPage:
    """Read a public feed page.  Pure, total, and never raises.

    Anything it cannot find is ``None`` (or an empty ``bars``): this parses somebody else's
    markup, so "the page changed" has to be an ordinary outcome rather than an incident.
    A caller that got an empty :class:`PublicPage` writes nothing and logs it.
    """
    page = PublicPage()
    if not isinstance(html, str) or not html:
        return page
    page.bars = _bars(html)

    stats = _STATS_RE.search(html)
    if stats:
        block = stats.group(1)
        page.min = _stat(block, "Min")
        page.max = _stat(block, "Max")
        page.now = _stat(block, "Now")

    status = _STATUS_RE.search(html)
    if status:
        block = status.group(1)
        lowered = block.lower()
        if "portal-status-badge--online" in lowered:
            page.online = True
        elif "portal-status-badge--offline" in lowered or "portal-status-badge--down" in lowered:
            page.online = False
        else:
            # No modifier class this parser knows: fall back to the badge's own words, which
            # are what a reader of the page goes by anyway.
            text = re.sub(r"<[^>]+>", " ", block).strip().lower()
            if "offline" in text or "down" in text:
                page.online = False
            elif "online" in text:
                page.online = True

    since = _SINCE_RE.search(html)
    if since:
        try:
            page.since = datetime.fromtimestamp(int(since.group(1)), tz=UTC)
        except (OverflowError, OSError, ValueError):
            page.since = None

    listeners = _LISTENERS_RE.search(html)
    if listeners:
        page.listeners = int(listeners.group(1).replace(",", ""))
    return page


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------


def fetch_public_page(feed_id: str | int, http_client: Any | None = None) -> str:
    """GET the public page for ``feed_id`` and return its HTML.

    No credential is sent and none is needed — this is the page any visitor gets — so
    nothing here goes through :func:`app.bcfy_stats.client.redact`.  Redirects are followed
    because the site redirects a bare feed id to its slugged URL.
    """
    feed = str(feed_id).strip()
    if not feed:
        raise PublicPageError("no feed id")
    url = PUBLIC_PAGE_URL.format(feed_id=feed)
    client = http_client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)
    try:
        response = client.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise PublicPageError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if http_client is None:
            client.close()
    if response.status_code >= 400:
        raise PublicPageError(f"HTTP {response.status_code}")
    return response.text
