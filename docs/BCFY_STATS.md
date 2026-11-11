# Broadcastify feed statistics

**What this is, and what it is not.** Keyup already talks to Broadcastify in one direction:
`docs/EXPORTERS.md` describes the **Calls** uploader, which publishes this archive's
recordings to their call-ingest product. This document is about the other direction and a
different product entirely — the **feed owner API**, which reports on the operator's own
*live audio feed*: how many people are listening right now, whether it is up, and which
archived blocks of it exist. Nothing here uploads anything, and the two share no
credentials: Calls authenticates with a per-system API key, and the owner API
authenticates with the operator's own Broadcastify **site login**.

Both switches ship **off**. With `bcfy_stats_enabled` false — the default — the worker's
tick is one cached settings read and a return.

> **Terms of service.** Only a feed's own provider can use the owner API, and only for feeds
> they broadcast. The public-page scrape and the dashboard CSV import below read pages and
> exports Broadcastify publishes for people, not a documented API: they rely on undocumented
> markup, can break without notice, and are an advanced, use-at-your-own-risk feature. Read
> [Broadcastify's terms](https://www.broadcastify.com/terms) before turning any of this on, keep
> the poll intervals at their defaults or longer, and use it only for a feed you provide.

## Why keep a local history at all

Broadcastify shows a feed's current listener count and not much else. The questions an
operator actually has are historical and comparative: *when is this feed hot? does that
follow the radio traffic or the clock? did last week's incident bring listeners? which
archived block covers 17:40 last Tuesday?* None of those can be asked of a number that is
only ever "right now", so the poller keeps one row per poll and rolls it up by the hour,
and the summary endpoint puts the listener history beside Keyup's **own** call volume for
the same talkgroups.

## The API, verbatim

The operator's copy of Broadcastify's documentation says, in full:

> The broadcastify feed owner API is available to feed providers to provide: Status and
> statistics in real-time; Feed archive listings and URLs. Access is restricted to feed
> providers and the feeds that they broadcast and own.
>
> API Endpoint URL: `http://api.broadcastify.com/owner/` and
> `https://api.broadcastify.com/owner/` (Secure)
>
> Required API Parameters: `a` = action, `type` = response type (json or xml), `feedId` =
> unique feed ID, `u` = feed owner Broadcastify username, `p` = feed owner Broadcastify
> password.
>
> Methods: `feed` — returns details on an individual feed. `archives` — returns all dates
> that a feed has archives available, or, with `day=YYYY-MM-DD`, all archives available for
> that day with the timeframe and download link for each.

The per-day `archives` answer, as observed on 2026-09-03, is
`{"archives": [{"archive": "10:35 PM - 10:50 PM", "url": "https://archives.broadcastify.com/…mp3"}, …]}`:
each block is a **clock range on the feed's own local time** (`TZ`, the deployment's own zone)
and a download link, nothing else. `parse_archive_range` turns that into two UTC instants
on the day that was asked for, and a block whose end reads earlier than its start crossed
midnight. The day list is `{"days": [{"day": "2026-09-03"}, …]}` and runs back a full year.

Two consequences are baked into this implementation:

1. **HTTPS only.** The password is a *query parameter*. Over plain HTTP it would be on the
   wire in clear text on every poll, so `bcfy_owner_url` defaults to the secure endpoint and
   nothing here ever downgrades. A URL for this API **is a credential**:
   `app.bcfy_stats.client.redact` blanks `p=` in every string that could be logged or
   returned, and there are tests that a 4xx body, a transport error and the
   `Credentials` repr all fail to contain the password.
2. **The response is undocumented.** The parameters are published; the *fields that come
   back* are not. That was the whole design problem. It is now half solved by observation
   and the other half still contained by the machinery in the next section but one.

## The response, as observed

`a=feed&type=json` was captured from a real feed in September 2026. Its shape is kept as a
fixture under `backend/tests/fixtures/bcfy/`; the identifying values below are replaced with
examples:

```json
{"Feed": [{"id": 12345, "descr": "Example County Fire/EMS", "genre": "Public Safety",
           "mount": "/examplemount",
           "Relays": [{"host": "listen.broadcastify.com", "port": "80"}],
           "status": 1, "bitrate": 32,
           "Counties": [{"lat": "40.0000000", "lon": "-90.0000000", "coid": "1", "ctid": 100,
                         "name": "Example", "stid": "99", "type": "County",
                         "stateCode": "EX", "stateName": "Example State", "countryCode": "US",
                         "countryName": "United States", "countyDetails": ""}],
           "listeners": 12}]}
```

Three things are now settled:

* the envelope is a **capital `Feed` holding a list**, which `feed_payload` already unwrapped
  (names are normalized, so `Feed` and `feed` are one name) and which is now asserted;
* the count is plainly **`listeners`**, and "up" is **`status: 1`** — there is no boolean
  anywhere in the response, so `online` is derived from `status`;
* `descr`, `genre`, `mount` and `bitrate` are the feed's descriptive fields. `Relays` and
  `Counties` are nested objects and stay out of `extracted` (they are still in `raw`).

**And the fourth thing, which is the one that shapes the whole feature: there is no history
in it.** One instant's listener count, and nothing else — no series, no yesterday, no peak,
no total. Whatever the operator wants to know about *last* Tuesday has to have been recorded
by this deployment at the time. That is why the listener half of every chart on the page
begins at `first_sample_at` and cannot be made to reach further back, and why the page says
so rather than drawing zeros over the years before it was switched on.

## The public feed page

There is a second source, and it is the only one anywhere that carries listener **history**.

`https://www.broadcastify.com/listen/feed/<feedId>` is the page a listener lands on. It is
public, needs no login and no credential of any kind, and — beside the player — it
server-renders a **24-hour hourly listener sparkline**: twenty-four `<rect>`s, oldest
first, each with its count in a `<title>` for the tooltip, plus a Min/Max/Now summary, the
status badge and how long the feed has been in that state. A save of that page's markup is
kept as a fixture under `backend/tests/fixtures/bcfy/`, and it is what
`app/bcfy_stats/public_page.py` is written against:

```html
<span class="listen-sparkline-stats"> <span>Min <strong>0</strong></span> <span>Max <strong>23</strong></span> <span>Now <strong>0</strong></span> </span>
<svg class="listen-sparkline-svg" viewBox="0 0 240 40" aria-label="24-hour listener history">
  <rect class="listen-sparkline-bar" x="0" y="25" width="8" height="14"><title>9 listeners</title></rect>
  … 24 of them, x = 0, 10, … 230 …
</svg>
```

**This is undocumented HTML and it may change without notice.** Broadcastify publishes no
contract for a page they render for browsers; those class names are what the markup used in
September 2026 and a redesign on their side breaks the parse. So the containment is the
same shape as the owner client's, applied harder:

* `parse_public_page` is **pure and never raises**. Every piece is independently optional —
  bars without a status badge are still bars — and a page it cannot read at all yields an
  empty `PublicPage`, writes nothing, and logs one INFO line saying the markup may have
  changed. A scrape that stopped working is not an incident.
* the **bar count is reported, not asserted**. Twenty-four is their rendering decision;
  twelve or forty-eight parse as twelve or forty-eight, and the caller lays them into the
  last *n* hours rather than assuming a day.
* the fetch is `https` from a constant template, with a browser-ish `User-Agent` (a page
  meant for browsers, asked for as `keyup/1.0`, invites a WAF to answer with something that
  is not the page) and a 15 s timeout. No credential is sent, so unlike the owner API
  nothing here needs redacting.

### The merge rule

Bar *k* of *n* is the UTC hour `floor(now) - (n-1-k)` hours, so the last bar is the hour in
progress. Each becomes a `bcfy_feed_hourly` row with `listeners_avg = listeners_max = the
bar`, **`samples = 0`** and **`source = 'page'`**. One rule governs everything after that,
and it only runs one way:

| The hour already has | A page value does | A real poll does |
| --- | --- | --- |
| nothing | writes the row (`samples = 0`, `source = 'page'`) | writes the row as it always did |
| a **page** row (`samples = 0`) | overwrites it — a later render is a later look | **replaces** it whole: `avg = max = x`, `samples = 1`, `source = NULL` |
| a **polled** row (`samples > 0`) | **nothing at all** — not averaged, not maxed | folds in incrementally, as before |

Our own polls are the truth; the page is a fallback and never a correction. The "never
overwrites a polled hour" half is enforced in SQL — `ON CONFLICT … DO UPDATE … WHERE
samples = 0` — rather than by a read-then-write, so a poll landing concurrently still wins.
The "a poll replaces a page row" half is a `CASE` on the same `samples = 0` test in
`store.roll_up`: averaging the first real sample against the page value (`(20 + 4) / 2`)
would bake somebody else's rounded hourly figure into this deployment's own history for
ever, and `GREATEST` would keep a peak nothing here ever saw.

`samples` stays 0 on a page row deliberately. It is the flag both directions test, it keeps
the page value out of every sample-weighted mean the summary computes, and it keeps "how
many polls is this cell built from" meaning polls.

### What this does and does not buy

Page hours are real listener figures and they are in everything — the heatmap, the series,
`listeners_hours_covered` — because a reconstructed hour beats a hatched one. But the
summary also carries **`hours_from_page`**, and the Feed stats page says *"N of these hours
came from the public feed page"*, so nobody reads a backfilled grid as a poller that was
running all along.

**Twenty-four hours is the whole of it.** This does not make the listener history
backfillable in the sense the section below means: a deployment switched on today gets
yesterday, not last year, and the page has no memory beyond a day either. What it buys is
that the first day of a new poller is a day rather than a blank grid, and that a worker
which was down overnight comes back with the night filled in.

### Running it

`bcfy_public_page_poll_seconds` (default `3600`, `0` = off, floored at 60) is the interval;
the fetch rides on the poll job's fifteen-second tick against a `settings` watermark
(`bcfy_public_page_last_fetch`), the same way the archive sync does, so a restarted worker
does not refetch on every boot. It is gated on `bcfy_stats_enabled` and a feed id — the
switch is what an operator reads as "talk to Broadcastify on a timer" — though the fetch
itself needs no credentials, so a deployment with no Broadcastify login can still use it.
An hour between fetches reads everything there is to read: the sparkline is hourly, and the
only bar that can have moved is the one the ordinary poll covers far better.

One-shot, ignoring both the switch and the interval:

```
docker compose exec api keyup bcfy-stats page
```

or `POST /api/admin/bcfy-stats/poll-page` (the **Fetch public page** button), which returns
`{bars, hours_written, online, since, listeners}`. `hours_written` is smaller than
`len(bars)` exactly to the extent the poller was already running.

## Importing the dashboard exports

There is a third source, and it is the only one that reaches back **months**.

The feed-owner dashboard — Broadcastify → **Manage** → your feed → **Statistics** — offers
two CSV downloads. They are not part of the owner API, they are not published anywhere, and
nothing here fetches them: an operator downloads a file through their browser and imports
it. Between them they carry roughly a week of hours and half a year of days, which is
several orders of magnitude more listener history than everything above this line put
together.

```
docker compose exec api keyup bcfy-stats import owner_uptime.csv
```

or `POST /api/admin/bcfy-stats/import` (the **Import dashboard export** button on the Feed
stats page, which reads the file in the browser and posts its text). **The format is decided
by the file's own header** and never by a parameter — the operator picks either file and it
lands in the right table.

### The two formats

| Header | Rows | What each row is |
| --- | --- | --- |
| `Hour,Online Checks,Online Reports,Max Listeners` | ~180, one per hour, newest first | `2026-09-04 16:00:00,12,12,7` |
| `Day,Uptime %,Max Listeners` | ~180, one per day, back to 2026-03-09 | `2026-09-03 00:00:00,99.72,31` |

`Online Checks` is how many times Broadcastify looked at the feed in that hour — about 15,
one every four minutes — and `Online Reports` is how many of those looks found it up. Both
are stored, in new nullable `bcfy_feed_hourly.online_checks` / `online_reports` columns; a
row this deployment polled leaves them NULL, because a poll has no such figure to record.

`Max Listeners` is the hour's **peak**, and it is the only listener figure the hourly export
carries — there is no average anywhere in it. The import therefore writes
`listeners_avg = listeners_max` and says so through `source = 'import'`. The alternative,
leaving the average NULL, would put a hole in the middle of every grid and series that reads
`listeners_avg`, which is most of this page. `samples` stays 0, exactly as on a page row,
which keeps the peak out of every sample-weighted mean the summary computes.

The daily export goes to a **new table**, `bcfy_feed_daily(feed_id, day, uptime_pct,
listeners_max, source)`, and is deliberately **not** derived from the hourly one or vice
versa. The two overlap for about a week and need not agree there — Broadcastify's own daily
figure is not obliged to equal a roll-up of their hourly peaks — so keeping them apart means
neither is ever silently replaced by the other, and the summary shows them side by side at
read time (`daily`, `daily_days_covered`). `uptime_pct` is also the only uptime figure in
this schema at all: a poll knows whether the feed was up at one instant, and the hourly
rollup does not keep even that.

### The timestamps are UTC

Nothing in either export says so, and the dashboard renders every other time on the
operator's local clock, so this was **checked rather than assumed**.

On the day the fixtures were taken this feed was down from **03:57 to 04:19 UTC**. The
hourly export puts the whole of that in two adjacent rows and nowhere else: the
`2026-09-04 04:00:00` row reports **10 of 15** online checks, and the `03:00` row reports 14
of 15 — the single check that fell in the last three minutes of that hour. 22 minutes is
five or six four-minute checks, which is exactly the deficit. Every other hour of that day
saw every check it made. On local time the same two rows would have to describe hours in
which nothing happened.

Both exports are kept as fixtures under `backend/tests/fixtures/bcfy/`, and `test_the_hourly_timestamps_are_utc_and_the_outage_row_is_why`
asserts that finding directly. Two other things in them are asserted by value and must not
be "cleaned up": the three incident days (479 listeners on 2026-04-19, 187 on 04-02, 127 on
04-26) are **real days**, not outliers to be clipped, and an operator's whole reason for
keeping this history is to be able to point at them afterwards.

### Precedence: polled > import > page

The page rule above gains a middle rung, and it is still enforced in SQL rather than by a
read-then-write, so a poll landing concurrently still wins:

| The hour already has | A **page** value does | An **import** does | A real **poll** does |
| --- | --- | --- | --- |
| nothing | writes the row (`samples = 0`, `source = 'page'`) | writes the row (`samples = 0`, `source = 'import'`) | writes the row as it always did |
| a **page** row | overwrites it — a later render is a later look | **overwrites it** | **replaces** it whole |
| an **import** row | **nothing at all** | refreshes it | **replaces** it whole |
| a **polled** row | **nothing at all** | **nothing at all** | folds in incrementally |

An import beats a page row because the two are not the same quality of number: a sparkline
bar is a height read off an SVG that Broadcastify rounded for a tooltip, while the export is
the figure their own dashboard reports for that hour. Where they disagree the export is
right — and that is the one direction the page rule never allowed, because until now there
was nothing better than the page.

A poll still beats an import, in `store.roll_up`, exactly as it beats a page row: the
`samples = 0` test there does not care which kind of backfill it is displacing, and
`source`, `online_checks` and `online_reports` are all cleared when it does, because from
that moment the row is a polled row. Averaging instead — `(7 + 20) / 2` — would bake
somebody else's rounded hourly peak into this deployment's own history for ever.

Re-importing is therefore safe and idempotent: it refreshes import rows, leaves polled hours
alone, and `rows_written` in the report is smaller than `rows_parsed` exactly to the extent
the poller had already covered those hours.

### What the summary gains

* **`daily`** — one `{day, uptime_pct, listeners_max}` per UTC day of the window, and the
  only series on the page that can cover a long one. The Feed stats page draws it as
  *Daily peak listeners and uptime*: a bar per day's peak on the left axis, a line for
  uptime pinned to 0–100 on the right.
* **`peak_listeners` / `peak_at` / `peak_source`** — the peak now considers the daily maxima
  too, so a feed whose polling started last week can still report the April day when 479
  people listened. `peak_source` is `"samples"` (one poll, and it names the minute),
  `"hourly"` (an hour row, and it names the hour) or `"daily"` (a day, reported at that day's
  UTC midnight because pretending to an hour would be a lie). Largest wins; a tie prefers the
  sharper clock.
* **`hours_from_import`** beside `hours_from_page`, so the coverage line can say
  *hourly history from 2026-08-28 (168 h imported, 24 h from the public page), daily history
  from 2026-03-09* and nothing backfilled ever passes as a poll.
* **`history_from`** — the earliest day with **any** figure, hourly or daily. After an import
  it is months before `first_sample_at`, which is why they are two fields.

### Tolerance

This is somebody else's export, downloaded through a browser and possibly opened in a
spreadsheet on the way here. So a UTF-8 BOM is stripped, CRLF and LF both end a line, blank
lines are ignored, columns are matched by **name** rather than position (a column
Broadcastify adds later changes nothing), and either row order is accepted because the parser
sorts what it keeps. A row it cannot read is **counted with its reason and skipped** — never
guessed at, never fatal. 180 rows with three bad ones is 177 rows of history and a report
saying `{"not a date": 1, "not a number": 1, "duplicate": 1}`, which is strictly better than
a 422. A file whose header is *neither* export is refused outright, because importing 180
days of listener peaks into the wrong table would be silent and hard to undo.

The upload is capped at **2 MB** — three hundred times the largest real file — on
`Content-Length` first and then on the bytes themselves.

## What is still guessed, and how the guess is contained

One account's response is not a schema, and nothing outside that one captured `a=feed` response has been
observed at all — the archive listings included. So the tolerance stays. The client is built
so that being wrong costs the *interpretation* and never the *data*:

* **Every poll is stored whole.** `bcfy_feed_samples.raw` is the response as it arrived.
  When a field turns out to be called something else, the mapping is widened and the
  history is re-derived from `raw` — it does not have to be re-collected.
* **The interpretation is one table.** `app.bcfy_stats.client.FIELD_ALIASES` maps a concept
  to a list of candidate field names. Names are normalized on both sides (lowercased,
  `_`/`-`/space stripped), so `currentListeners`, `current_listeners` and `CURRENTLISTENERS`
  are one name and only the letters matter. Adding a name is a one-line change.
* **The envelope is unwrapped, not assumed.** `feed_payload` accepts a bare object,
  `{"feed": …}`, `{"feeds": [ … ]}`, a one-element list, and the XML form's
  `{"broadcastify": {"feed": …}}`.
* **JSON and XML read the same.** `type=json` is asked for first; the body is *sniffed*
  rather than trusted, and a rejected JSON request is retried once as `type=xml`. XML is
  parsed into the same dict shape (attributes and leaf text both become keys) so the
  mapping runs once for either format.
* **An unrecognized response is still a sample.** It gets a row with `listeners = NULL` and
  the full `raw`; it simply does not move the hourly rollup.

The concepts and the candidate names tried in order. **Observed** marks a name seen in a
real response; the rest are still candidates:

| Concept | Candidates |
| --- | --- |
| `listeners` (→ its own column) | `currentListeners`, **`listeners`** (observed), `listenerCount`, `numListeners`, `cListeners` |
| `online` (→ its own column) | `online`, `isOnline`, `up`, `active`, `broadcasting`, **`status`** (observed: `1`) |
| `listeners_peak` | `peakListeners`, `maxListeners`, `listenersPeak`, `peak` |
| `listeners_total` | `totalListeners`, `listenersTotal`, `allTimeListeners` |
| `status` | **`status`** (observed), `feedStatus`, `state` |
| `name` | **`descr`** (observed), `description`, `feedName`, `name`, `title` |
| `genre` | **`genre`** (observed), `category`, `type` |
| `mount` | **`mount`** (observed), `mountPoint`, `mountPt` |
| `bitrate` | **`bitrate`** (observed), `kbps` |
| `listeners_limit` | `listenerLimit`, `maxConnections`, `limit` |
| `started_at` | `startTime`, `startedAt`, `since`, `uptimeStart` |
| `archives_enabled` | `archive`, `archives`, `archiveEnabled`, `archivesEnabled` |

Booleans are read generously (`1`/`true`/`yes`/`on`/`online`/`up`/`active` and their
negatives), because an API that has never committed to a boolean type will use several of
them.

The archive listing has its own, smaller table (`ARCHIVE_ALIASES`) for `start`, `end`,
`url`, `size` and `day`. Times are accepted as epoch seconds, epoch milliseconds or an
ISO-8601 string. A row with **no usable start instant is dropped**: `start_at` is the row's
identity (`UNIQUE (feed_id, start_at)`) and a row without one cannot be upserted.

The captured `a=feed` shape in `backend/tests/fixtures/bcfy/` is asserted field by field.
The alternative shapes there are inventions and say so in that directory's README; they exist
to prove the tolerance works across several plausible shapes, and they stay because one
account's answer does not make the others impossible.

## Confirming the field names — the probe

This is the point of the feature's first hour of life. With the credentials saved:

```
docker compose exec api keyup bcfy-stats probe
```

or, from the Feed stats page, the **Probe** button (`POST /api/admin/bcfy-stats/probe`).

Both make **one** live `a=feed` request, store nothing, and return three things:

* `raw` — the response exactly as it arrived;
* `extracted` — what `FIELD_ALIASES` could find in it;
* `unmapped_fields` — the top-level keys no alias claimed.

**Read `unmapped_fields` first.** If `extracted` has no `listeners`, the real name is in
that list. Add it to the front of the right row of `FIELD_ALIASES`, and — because `raw` was
kept all along — every sample already collected can be re-read with the corrected mapping.

`--raw-only` prints just the response, which is the form to paste into an issue or into a
new fixture file.

## Settings

All runtime, all seeded from the environment, all defaulting to empty or off.

They are edited on the **Feed Stats page** (`/feed-stats`), beside the listener charts and
the archive listings they produce — *not* on Admin → Settings, which holds the `Feed stats`
group back and links here instead. One group, one form: two forms over one table meant two
drafts, and saving in one silently discarded what had been typed in the other.

| Key | Kind | Default | What it is |
| --- | --- | --- | --- |
| `bcfy_owner_username` | string | `""` | The Broadcastify account that owns the feed. |
| `bcfy_owner_password` | **secret** | `""` | Its password. Stored, never read back, never logged. A blank field on save keeps the existing one. |
| `bcfy_feed_id` | string | `""` | The numeric `feedId`, also the number in the feed's URL. |
| `bcfy_stats_enabled` | bool | `false` | The poller's switch. |
| `bcfy_stats_poll_seconds` | float | `60` | Seconds between polls. Floored at 5. |
| `bcfy_public_page_poll_seconds` | float | `3600` | Seconds between fetches of the **public** feed page, whose sparkline is the only listener history that exists. Floored at 60; `0` turns it off. |
| `bcfy_archives_sync_enabled` | bool | `false` | The archive sync's switch. |
| `bcfy_archives_sync_hours` | float | `6` | Hours between archive syncs. |
| `bcfy_archives_backfill_days_per_sync` | int | `30` | How many *older* days each sync catches up on, beyond the last week it always refreshes. One day is one request. `0` turns the backfill off. |

`bcfy_owner_password` has kind `secret`, so `GET /api/admin/settings` returns `""` for it
however it is set; `is_default` is how a reader tells whether one exists. This is the same
treatment `webhook_secret`, `broadcastify_api_key` and `openmhz_api_key` get, and for the
same reason: reads of this archive are open, and a credential that can be *read* back is a
credential anybody who can see the site holds.

## Tables

| Table | Row | Kept |
| --- | --- | --- |
| `bcfy_feed_samples` | one `a=feed` poll: `listeners`, `online`, `raw`, `extracted` | 400 days |
| `bcfy_feed_hourly` | one feed-hour: `listeners_avg`, `listeners_max`, `samples`, `source`, `online_checks`, `online_reports` | for ever |
| `bcfy_feed_daily` | one feed-day of the dashboard's daily export: `uptime_pct`, `listeners_max` | for ever |
| `bcfy_archives` | one archived block: `day`, `start_at`, `end_at`, `url`, `size_bytes`, `raw` | until Broadcastify stops listing it |

The rollup is maintained **on the write path**, not by a nightly job: the row is upserted
as each sample lands, with the average computed incrementally in SQL
(`(avg·n + x) / (n+1)`). That makes the current hour always current, and it is what lets the
samples be aged out without losing the history — a year of minute samples is half a million
rows, the same year in `bcfy_feed_hourly` is 8 760.

`keyup bcfy-stats prune --keep-days 400` applies the sample retention.

## The worker

Two APScheduler jobs, both registered unconditionally and gated *inside* the tick — the
same pattern as the arbiter, the exporters and the webhook queue, and for the same reason:
flipping a switch in the Admin page takes effect on the next tick rather than the next
deploy.

* **`bcfy_poll`** ticks every 15 s; the *setting* decides whether a poll is due, so changing
  `bcfy_stats_poll_seconds` needs no re-registration and a 300-second interval is not polled
  early. The **public page** fetch rides on this same tick against its own watermark row
  (`settings.bcfy_public_page_last_fetch`) — same feature, same switch, same feed id, and
  an interval of hours rather than a minute — and it runs *before* the due check, because
  this job returns early far more often than it polls.
* **`bcfy_archives`** ticks every 15 minutes against a watermark row
  (`settings.bcfy_archives_last_sync`), so a restarted worker does not re-fetch a week of
  listings on every boot. Each pass fetches the day list, then the last **7** days' listings,
  then a slice of the backlog (below); one failed day is recorded in `errors` and does not
  abandon the rest.

**Failure is quiet.** Somebody else's API being down is not this deployment's incident. A
failing poll logs **one** WARN when the streak starts and nothing more until it ends (then
one INFO saying it recovered), and backs off exponentially from the poll interval to a
10-minute ceiling. The alternative — a line per poll — is a hundred identical warnings an
hour for a fault nobody here can fix, which is how a log stops being read.

## What can be backfilled, and what cannot

The page has two histories in it and they are not the same age. Saying which is which is
most of the honesty of this feature.

**Calls: the whole archive.** They are Keyup's own rows, going back to 2024, so the call
heatmap is real for every window the page offers — 7, 30, 90, 180 or 365 days. A year costs
one `date_trunc('hour', started_at)` aggregate over a range scan of `ix_calls_started_at`:
8 760 rows out of the database, no per-hour queries, no loop. That is the whole reason the
long windows exist.

**Listeners: from the first poll, plus a day — or, with the exports, plus half a year.**
The owner API answers with one instant's count (see the observed response above), so the
*automatic* listener history is as old as this deployment's polling, and the **public feed
page** reaches exactly 24 hours further back. That is the whole of what Broadcastify
*publishes*. What it will **give an operator on request** is more: the feed dashboard's two
CSV exports (above) carry about a week of hours and about 180 days of days, and importing
them is the only way this page ever shows a month it was not running for. It is a manual act
and stays one — nothing here logs into a dashboard on a timer. Consequences the code and the
page both carry:

* a listener cell nobody polled is **`null`, never `0`** — in the API, in the matrix and in
  the heatmap, which draws it hatched and labels it "no data". A grid of zeros over an
  unmeasured year would read as "nobody listens", which is the opposite of what it knows;
* `listeners_hours_covered`, `hours_from_page`, `hours_from_import`, `first_sample_at`,
  `history_from` and `window_hours` are in the summary so the page can say *hourly history
  from 2026-08-14 · 480 of 8 760 hours (168 h imported, 24 h from the public page), daily
  history from 2026-03-09* rather than leaving any of it to be inferred;
* `quiet_windows` refuses to score a run of hours containing an unmeasured one. Otherwise
  the least-observed stretch of the week wins every time, and the advice becomes "take the
  feed down whenever you have no data", which is exactly backwards.

**Archives: backfilled, once, in slices.** `a=archives` with no `day` lists *every* day the
feed has archives for, which is usually far more than this database has stored. Each sync
therefore does three things: fetches that day list; refetches the last `--days` (7) days,
whose download links expire; and then fetches per-day listings for days the list names and
`bcfy_archives` has never stored — **oldest first**, at most
`bcfy_archives_backfill_days_per_sync` (30) of them per pass. A year of history catches up
in about a fortnight of six-hourly syncs and Broadcastify never sees a burst. Oldest first
so an interrupted catch-up leaves a contiguous history rather than a lattice; and nothing
marks a day "done" except its rows, so a day whose request failed is simply picked up again
by the next pass.

For the impatient there is a one-shot:

```
docker compose exec api keyup bcfy-stats archives --full
```

or `POST /api/admin/bcfy-stats/sync-archives?full=true` (the **Backfill all** button). `full`
raises the budget for that one run to 365 days — not to infinity, because an unbounded pass
would hold an HTTP request open for a quarter of an hour against somebody else's API. The
report's `backfill_remaining` says whether a second run is needed.

## Hot times

`GET /api/admin/bcfy-stats/summary?days=30` is the whole Feed stats page in one object.

Everything is cut on the **local** clock (`TZ`), because the question is
about the dispatcher's week: "Tuesday 17:00" is a shift change and the same instant in UTC
is nothing in particular. Both matrices are 7×24 with **Monday first**, and both are built
from the same hourly buckets, which is what makes comparing them meaningful.

* `listeners_hour_of_week[d][h]` — the mean of the four or five *actual* Tuesday-17:00s the
  window contains, so a longer window makes the picture steadier rather than larger.
* `calls_hour_of_week[d][h]` — the same grid for Keyup's own call volume, restricted to the
  talkgroups the **`export_broadcastify` selection** covers (`app.exporters.gate`). The feed
  and the Calls export are not the same product, but "which talkgroups go to Broadcastify"
  is one operator decision that is already expressed once; reading it here means the volume
  heatmap is about the traffic this feed actually carries, and stays that way when the
  operator changes their mind in one place.
* `hottest_hours` — the top ten cells, ranked by average listeners and tie-broken by call
  volume.
* `peak_listeners` / `peak_at` — read from the **samples**, not the rollup, because the time
  is the interesting half: an hour row says 41 people were listening some time in that hour,
  and this says it was at 17:42.
* `correlation` — Pearson's r between hourly call volume and hourly average listeners.

* `quietest_hours` / `quiet_windows` / `quietest_basis` — the maintenance answer, below.

**Read the correlation carefully.** It is a linear association over hourly totals and says
nothing about which way anything points; busy radio traffic and many listeners can both
follow the time of day without either causing the other. It is `null` — not `0` — below 24
paired hours or when either series never varies, because an r computed from three hours is a
number with no information in it, and a `0` would read as "measured, and there is no
relationship", which is a stronger claim than the data supports.

## Quiet times — when to do maintenance

The inverse question, and the one an operator actually acts on: *when can this feed go down
without anybody noticing?* Three fields answer it.

* **`quietest_hours`** — the ten cells with the **lowest** average listeners, tie-broken by
  the lower call volume. Only cells that have listener samples are eligible: never having
  looked at an hour is not evidence that it is quiet.
* **`quiet_windows`** — the three quietest **non-overlapping** contiguous runs of at least
  two hours, each `{weekday, start_hour, end_hour, listeners_avg, calls_avg}`. A window is
  not a cell, because maintenance takes longer than an hour and the quietest single hour is
  worth nothing if the hour on either side of it is the busiest. Runs may **wrap midnight**
  (`22:00 → 02:00` is one window) but never cross into the next weekday, because "Sunday
  night" is a thing an operator schedules. `end_hour` is exclusive and modulo 24.
  Non-overlapping because the naive ranking returns three descriptions of the same night.
* **`quietest_basis`** — `"listeners"` or `"calls"`. With no listener sample anywhere, both
  rankings fall back to **radio traffic** and this field says so; the page then says
  "quietest traffic" rather than "quietest listening", which is a weaker and different
  claim — a guess at the answer, not the answer.

## Frontend

**Feed stats** (`/feed-stats`, admin-only in the nav, linked from Admin → Exports): a status
card, a listeners-over-time chart, the two heatmaps, three ranking cards — **Hottest hours**,
**Quietest hours** and **Suggested maintenance windows** — and the archive table by day, with
the credential fields and switches at the bottom of the page rendered from
`GET /api/admin/settings` exactly as the Exports tab renders its own.

The heatmap is a real `<table>`, so colour is never the only encoding: every cell names its
day, hour, average, peak and sample count in its accessible name and its tooltip, and an
unpolled cell says "no data" there and is hatched rather than merely pale. The listener
card's header carries the coverage line described above; the quiet cards' hints change
wording when `quietest_basis` is `calls`.

## When you see another real response

The `a=feed` shape is known for one account; the archive listings are not known at all, and
a different feed's plan may answer differently. The loop is unchanged:

1. Run the probe, and keep the output.
2. Add the real field names to the front of the matching rows in `FIELD_ALIASES`.
3. Save the response beside `feed_sample.json` and assert it directly, the way
   `test_the_real_response_is_read_exactly_as_it_arrived` does.
4. Mark the newly observed rows in the table above.

If a response ever turns up that *does* carry listener history, that is a bigger change than
a mapping row: `test_the_real_response_carries_no_history_to_backfill` fails, and the
"cannot be backfilled" half of this document has to be rewritten before anything else.
