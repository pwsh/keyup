"""The worker's side: one poll, one archive sync, and the backoff that keeps both polite.

Both jobs are registered unconditionally and gated *inside* the tick, exactly as the
arbiter, the exporters and the webhook queue are: that is what makes turning the feed
poller on in the Admin page take effect on the next tick rather than the next deploy, and
with the switch off a tick is one cached settings read and a return.

**Failure is quiet.**  Somebody else's API being down is not this deployment's incident, so
a failing poll logs one WARN when the streak *starts* and nothing more until it ends (then
one INFO saying it recovered).  The alternative — a line per poll — is a hundred identical
warnings an hour for a fault nobody here can fix, which is how a log stops being read.
Between failures the poller backs off exponentially from the poll interval to
:data:`MAX_BACKOFF_S`, so a dead feed is asked about every few minutes rather than every
minute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.bcfy_stats import store
from app.bcfy_stats.client import BcfyOwnerClient, BcfyOwnerError, archive_rows
from app.models import Setting

log = logging.getLogger("app.bcfy_stats.poller")

__all__ = [
    "ARCHIVE_DAYS",
    "BACKFILL_DAYS_PER_SYNC",
    "FULL_BACKFILL_CEILING",
    "MAX_BACKOFF_S",
    "PAGE_POLL_SECONDS",
    "FeedPoller",
    "credentials",
    "poller",
    "sync_public_page",
]

#: The ceiling on the backoff between two failed polls.  Ten minutes is long enough that a
#: night-long outage costs a handful of requests and short enough that a feed which came
#: back is noticed within one coffee.
MAX_BACKOFF_S = 600.0

#: How many days back the archive sync fetches per-day listings for on every pass.
#: Broadcastify keeps archives for far longer, but a listing older than a week does not
#: change, and the rows for it are already stored -- or, if they are not, the *backfill*
#: below is what fetches them, once.
ARCHIVE_DAYS = 7

#: The default per-pass budget for the backfill, when the setting is unreadable.  A day is
#: one request, so 30 is thirty extra requests every few hours: a year of archives catches
#: up inside a fortnight and Broadcastify never sees a burst.
BACKFILL_DAYS_PER_SYNC = 30

#: The ceiling on an operator-requested ``full`` catch-up.  "Full" means "do not stop at the
#: per-pass budget", not "make unbounded requests from inside an HTTP handler": a feed with
#: two years of archives would otherwise hold the request open for a quarter of an hour.
#: A year in one pass, and a second run finishes anything older.
FULL_BACKFILL_CEILING = 365

#: Where the archive sync remembers its last run, so a restart does not re-fetch a week of
#: listings on every boot.  A ``settings`` row for the same reason the exporters' watermark
#: is one: the worker may be replaced, and an in-memory clock would reset with it.
ARCHIVE_WATERMARK_KEY = "bcfy_archives_last_sync"

#: The fallback interval between two public-page fetches, when the setting is unreadable.
#: An hour, because the page's sparkline is *hourly*: fetching it more often re-reads bars
#: that have not moved, and the only one that can have changed is the hour in progress,
#: which the ordinary poll already covers far better.
PAGE_POLL_SECONDS = 3600.0

#: Where the public-page fetch remembers its last run.  A ``settings`` row for the same
#: reason the archive sync's is: the worker may be replaced mid-hour and an in-memory clock
#: would reset with it, which on a 15-second tick means fetching somebody else's page
#: continuously after a crash loop.
PAGE_WATERMARK_KEY = "bcfy_public_page_last_fetch"


@dataclass
class Credentials:
    """What a poll needs, and whether it has all of it."""

    username: str = ""
    password: str = ""
    feed_id: str = ""
    url: str = "https://api.broadcastify.com/owner/"

    @property
    def complete(self) -> bool:
        return bool(self.username and self.password and self.feed_id)

    #: Deliberately no ``__repr__`` of the password: a dataclass repr on a credential is
    #: how one ends up in a traceback.
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Credentials(username={self.username!r}, feed_id={self.feed_id!r}, password=***)"


def credentials(settings: Any) -> Credentials:
    """The feed credentials out of the effective settings, trimmed."""
    return Credentials(
        username=str(getattr(settings, "bcfy_owner_username", "") or "").strip(),
        password=str(getattr(settings, "bcfy_owner_password", "") or "").strip(),
        feed_id=str(getattr(settings, "bcfy_feed_id", "") or "").strip(),
        url=str(getattr(settings, "bcfy_owner_url", "") or "https://api.broadcastify.com/owner/"),
    )


def make_client(settings: Any, *, http_client: Any | None = None) -> BcfyOwnerClient | None:
    """A client for the configured feed, or ``None`` when something is missing."""
    creds = credentials(settings)
    if not creds.complete:
        return None
    return BcfyOwnerClient(
        username=creds.username,
        password=creds.password,
        feed_id=creds.feed_id,
        url=creds.url,
        client=http_client,
    )


@dataclass
class PollResult:
    """What one tick did, for the caller's log line and for the tests."""

    polled: bool = False
    stored: bool = False
    skipped: str | None = None
    error: str | None = None
    listeners: int | None = None


@dataclass
class FeedPoller:
    """The poll job's memory: the failure streak and when the next attempt is due.

    Shared across ticks (the module-level :data:`poller`), because the backoff *is* the
    state: a new instance per tick would poll a dead API at full rate for ever.
    """

    failures: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    _warned: bool = field(default=False, repr=False)

    def backoff_seconds(self, base: float) -> float:
        """``base * 2^(failures-1)``, capped — the classic, and the same shape the
        exporters' ledger uses for a failed upload."""
        if self.failures <= 0:
            return base
        return min(MAX_BACKOFF_S, base * (2 ** (self.failures - 1)))

    def _fail(self, message: str, base: float, now: datetime) -> None:
        self.failures += 1
        self.last_error = message
        self.next_attempt_at = now + timedelta(seconds=self.backoff_seconds(base))
        if not self._warned:
            # One line per *streak*.  The next one is only written once the feed has
            # recovered and failed again, which is a fact worth a log line.
            log.warning("bcfy-stats: poll failed (%s); backing off", message)
            self._warned = True

    def _recover(self) -> None:
        if self._warned:
            log.info("bcfy-stats: poll recovered after %d failure(s)", self.failures)
        self.failures = 0
        self.last_error = None
        self.next_attempt_at = None
        self._warned = False

    def tick(
        self,
        session: Session,
        settings: Any,
        *,
        now: datetime | None = None,
        http_client: Any | None = None,
    ) -> PollResult:
        """One poll, if it is due and the feature is on.  Never raises."""
        moment = now or datetime.now(UTC)
        if not bool(getattr(settings, "bcfy_stats_enabled", False)):
            return PollResult(skipped="disabled")
        client = make_client(settings, http_client=http_client)
        if client is None:
            return PollResult(skipped="not configured")
        if self.next_attempt_at is not None and moment < self.next_attempt_at:
            return PollResult(skipped="backing off")

        base = max(5.0, float(getattr(settings, "bcfy_stats_poll_seconds", 60.0) or 60.0))
        try:
            document = client.feed()
        except BcfyOwnerError as exc:
            self._fail(str(exc), base, moment)
            return PollResult(polled=True, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a scheduler job must never die
            self._fail(f"{type(exc).__name__}: {exc}", base, moment)
            return PollResult(polled=True, error=str(exc))

        sample = store.record_sample(session, feed_id=client.feed_id, document=document, polled_at=moment)
        self._recover()
        return PollResult(polled=True, stored=True, listeners=sample.listeners)


#: The shared instance the worker ticks.  Module-level for the same reason
#: :data:`app.arbiter.arbiter` is: the backoff has to survive the tick.
poller = FeedPoller()


# --------------------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------------------


def _backfill_budget(settings: Any) -> int:
    """How many backlog days one pass may fetch.  ``0`` turns the backfill off entirely."""
    try:
        value = int(getattr(settings, "bcfy_archives_backfill_days_per_sync", BACKFILL_DAYS_PER_SYNC))
    except (TypeError, ValueError):
        return BACKFILL_DAYS_PER_SYNC
    return max(0, value)


def _watermark(session: Session, key: str = ARCHIVE_WATERMARK_KEY) -> datetime | None:
    row = session.get(Setting, key)
    if row is None or not row.value:
        return None
    try:
        moment = datetime.fromisoformat(str(row.value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _set_watermark(session: Session, moment: datetime, key: str = ARCHIVE_WATERMARK_KEY) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=moment.isoformat()))
    else:
        row.value = moment.isoformat()


def _fetch_day(session: Session, client: Any, day: date, report: dict[str, Any], *, tz: str = "UTC") -> bool:
    """One day's listing, upserted.  ``False`` when the request failed, which is not fatal.

    A listing is per-day precisely so that a bad one costs that day and not the pass, so the
    failure is recorded in ``report["errors"]`` and the caller carries on to the next day.
    """
    try:
        document = client.archives(day)
    except BcfyOwnerError as exc:
        report["errors"].append(f"{day.isoformat()}: {exc}")
        return False
    rows = archive_rows(document, day=day, tz=tz)
    report["rows"] += store.upsert_archives(session, feed_id=client.feed_id, rows=rows)
    return True


def sync_archives(
    session: Session,
    settings: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
    full: bool = False,
    http_client: Any | None = None,
    days: int = ARCHIVE_DAYS,
) -> dict[str, Any]:
    """Fetch the archive day list, the last week's listings, and a slice of the backlog.

    Three things, in one pass:

    * the **day list** (``a=archives``), for its own sake — it says how far back the feed's
      archives go, which is a question the page asks, and it is the input to the third step;
    * the **recent days** (the last ``days``), refetched every pass because a recent
      listing's download links expire and the newest listing is the one whose link works;
    * the **backfill**: days the API says exist and this database has never stored.

    The backfill is what makes the archive half of the page cover the whole feed rather than
    the last week.  It is bounded on purpose — ``bcfy_archives_backfill_days_per_sync``
    days, oldest first, per pass — because "catch up on two years" and "make seven hundred
    requests to somebody else's API in one go" are the same sentence otherwise.  Oldest
    first so the coverage grows from the far end towards the recent days that are already
    stored, which means an interrupted catch-up leaves a contiguous history and not a
    lattice.  ``full=True`` lifts the budget to :data:`FULL_BACKFILL_CEILING` for one
    operator-requested pass.

    Nothing here is retried inside the pass: a day that failed is simply not stored, so it
    is still missing from ``stored_archive_days`` and the next pass picks it up again.
    """
    moment = now or datetime.now(UTC)
    if not force and not bool(getattr(settings, "bcfy_archives_sync_enabled", False)):
        return {"skipped": "disabled"}
    client = make_client(settings, http_client=http_client)
    if client is None:
        return {"skipped": "not configured"}
    if not force:
        interval = max(1.0, float(getattr(settings, "bcfy_archives_sync_hours", 6.0) or 6.0))
        last = _watermark(session)
        if last is not None and moment - last < timedelta(hours=interval):
            return {"skipped": "not due"}

    tz = str(getattr(settings, "tz", "UTC") or "UTC")
    report: dict[str, Any] = {
        "days_listed": 0,
        "rows": 0,
        "backfilled_days": 0,
        "backfill_remaining": 0,
        "errors": [],
    }
    listed: list[date] = []
    try:
        listing = client.archives()
    except BcfyOwnerError as exc:
        report["errors"].append(f"day list: {exc}")
    else:
        from app.bcfy_stats.client import archive_days  # noqa: PLC0415 - one caller

        listed = archive_days(listing)
        report["days_listed"] = len(listed)

    today = moment.date()
    recent = {today - timedelta(days=offset) for offset in range(days)}
    for offset in range(days):
        _fetch_day(session, client, today - timedelta(days=offset), report, tz=tz)

    # ---- the backfill.  Every listed day that is neither recent nor already stored.
    if listed:
        stored = store.stored_archive_days(session, feed_id=client.feed_id)
        missing = sorted(day for day in listed if day not in stored and day not in recent)
        budget = FULL_BACKFILL_CEILING if full else _backfill_budget(settings)
        for day in missing[:budget]:
            if _fetch_day(session, client, day, report, tz=tz):
                report["backfilled_days"] += 1
        report["backfill_remaining"] = max(0, len(missing) - budget)

    _set_watermark(session, moment)
    if report["errors"]:
        log.warning(
            "bcfy-stats: archive sync wrote %d row(s) with %d failure(s)",
            report["rows"],
            len(report["errors"]),
        )
    else:
        log.info(
            "bcfy-stats: archive sync wrote %d row(s); backfilled %d day(s), %d left",
            report["rows"],
            report["backfilled_days"],
            report["backfill_remaining"],
        )
    return report


# --------------------------------------------------------------------------------------
# the public page — the only backfill that exists
# --------------------------------------------------------------------------------------


def _page_interval(settings: Any) -> float:
    """Seconds between two public-page fetches; ``0`` means never."""
    try:
        value = float(getattr(settings, "bcfy_public_page_poll_seconds", PAGE_POLL_SECONDS))
    except (TypeError, ValueError):
        return PAGE_POLL_SECONDS
    if value <= 0:
        return 0.0
    # A floor of a minute for the same reason the poll interval has one: this fetches a
    # rendered page from somebody else's site, and an operator typing `1` into a settings
    # box should not turn that into a request a second.
    return max(60.0, value)


def sync_public_page(
    session: Session,
    settings: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """Fetch the public feed page and backfill the hours nobody polled.

    The **only** history Broadcastify hands out (docs/BCFY_STATS.md, "The public feed
    page"): a login-free page whose server-rendered sparkline carries the last 24 hourly
    listener counts.  Every hour of it that this deployment has no poll for becomes a row;
    every hour it *does* have a poll for is left alone
    (:func:`app.bcfy_stats.store.record_page_hours` is where that rule is enforced, in SQL).

    Needs no credentials — only ``bcfy_feed_id`` — but is still gated on
    ``bcfy_stats_enabled``, because that switch is what an operator understands as "talk to
    Broadcastify on a timer" and it would be a surprise for one thing to keep fetching with
    it off.  ``force`` is the operator asking for it now: it ignores the switch and the
    interval but not the feed id, since there is nothing to fetch without one.

    Never raises.  A changed page yields no bars, writes nothing, and logs one line.
    """
    from app.bcfy_stats.public_page import (  # noqa: PLC0415 - imports httpx
        PublicPageError,
        fetch_public_page,
        parse_public_page,
    )

    moment = now or datetime.now(UTC)
    feed_id = str(getattr(settings, "bcfy_feed_id", "") or "").strip()
    if not feed_id:
        return {"skipped": "not configured"}
    if not force:
        if not bool(getattr(settings, "bcfy_stats_enabled", False)):
            return {"skipped": "disabled"}
        interval = _page_interval(settings)
        if interval <= 0:
            return {"skipped": "disabled"}
        last = _watermark(session, PAGE_WATERMARK_KEY)
        if last is not None and (moment - last).total_seconds() < interval:
            return {"skipped": "not due"}

    report: dict[str, Any] = {
        "feed_id": feed_id,
        "bars": [],
        "hours_written": 0,
        "online": None,
        "since": None,
        "listeners": None,
    }
    try:
        html = fetch_public_page(feed_id, http_client)
    except PublicPageError as exc:
        # The watermark is *not* moved: a page that would not load is worth trying again on
        # the next tick, and unlike the owner API this costs nothing to be wrong about.
        report["error"] = str(exc)
        log.warning("bcfy-stats: public page fetch failed (%s)", exc)
        return report
    except Exception as exc:  # noqa: BLE001 - a scheduler job must never die
        report["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("bcfy-stats: public page fetch failed (%s)", report["error"])
        return report

    page = parse_public_page(html)
    report["bars"] = list(page.bars)
    report["online"] = page.online
    report["since"] = page.since
    report["listeners"] = page.listeners
    report["hours_written"] = store.record_page_hours(session, feed_id=feed_id, bars=page.bars, now=moment)
    _set_watermark(session, moment, PAGE_WATERMARK_KEY)
    if not page.bars:
        # Not an error: the page loaded and this parser did not recognize a sparkline in it,
        # which is exactly what a redesign on their side looks like.
        log.info("bcfy-stats: public page carried no sparkline (markup may have changed)")
    else:
        log.info(
            "bcfy-stats: public page gave %d hour(s), %d written (the rest were already polled)",
            len(page.bars),
            report["hours_written"],
        )
    return report
