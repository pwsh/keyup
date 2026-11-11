# Embedding live transcripts

`/embed/transcripts` is a small page built to live in somebody else's `<iframe>`: a live list of
transcribed calls, each with its local time, talkgroup, duration, the transcript and a link to the full call
on this archive. By default it shows the **Emergency dispatch** built-in feed (law, fire, EMS, the
multi-service event channels and the interop channels — the same filter as
`/api/feeds/preset/emergency.rss`) and loads the **last 15 minutes** before going live.

```html
<iframe src="https://radio.example.org/embed/transcripts"
        title="Live radio transcripts" width="100%" height="480"
        style="border:0" loading="lazy"></iframe>
```

The **Feeds** page has an *Embed live transcripts* card that builds this snippet from a feed, a talkgroup
list and the display options below, with a copy button and a preview. The app's sidebar (and the phone's
*More* menu) has a **Transcripts** entry that opens the same page, full-window, in a new tab — a plain link,
not an app route, because nginx serves the page and it has no navigation back.

## How it behaves

- **First screen.** `GET /api/transcripts/recent` with the page's filter and `minutes` — newest first, only
  the fields the page prints.
- **Live.** It then opens `WS /api/live` and sends the filter that answer resolved as its first frame
  (falling back to `GET /api/live/sse` if a proxy in front of the site refuses the WebSocket upgrade, exactly
  as the app does). A row appears when its transcript lands (`call.transcribed`), changes in place when
  somebody edits the call (`call.updated` — a human correction wins over the engine's text, as everywhere),
  and disappears when reconciliation demotes it to a duplicate (`call.superseded`). A call that has only been
  recorded (`call.new`) is not shown until its transcript arrives, unless `show_pending=1`.
- **Which calls get a row.** A finished transcript with text in it. Calls whose transcript is still pending,
  empty, or thrown away by the hallucination check are skipped unless `show_pending=1`. Duplicates never
  appear.
- **Reading while it updates.** New rows slide in at the top. A reader who has scrolled down is not moved:
  the row they are reading stays put and an *N new* pill takes them back to the top.
- **Older calls.** Reaching the end of the list reads the next page before it (`cursor=` from the
  answer's `next_cursor`, `limit` rows at a time), past the `minutes` window and as far back as the archive
  goes; a sticky divider marks each change of day. The last row of the list says where it stands: a
  **Load older calls** button (the same read, for a reader who would rather click, or whose browser has no
  `IntersectionObserver`), *Loading older calls…* while a page is on its way, or **No older calls** once a
  page comes back short. A first screen with no rows shows only its empty-state note, so reading back past
  the window then means widening `minutes` or the filter. The page holds at most 1,000 rows, dropping the oldest (they load again
  on scrolling down). Live updates only add calls from inside the window the page opened with — the
  backlog re-transcribes months-old calls continuously, and those are not news.
- **Reconnecting.** A status dot says *Live*, *Connecting…* or *Reconnecting…*. The socket reconnects with
  backoff (at most 15 s apart) and immediately when the tab becomes visible or the network returns; after a
  reconnection the window is read again, so nothing transcribed during the gap is missed.
- **Rolling.** The first screen is at most `limit` rows (100 by default) and each older page is another
  `limit`; the page holds at most 1,000 in all, dropping the oldest. Rows are not expired by age: a quiet
  night keeps showing the last traffic rather than an empty box.
- **Times** are the archive's local wall clock (`TZ`), the same clock the rest of Keyup prints.
- **Links** are absolute (`https://radio.example.org/calls/123`) and open in a new tab, so they work from
  inside the frame.

## URL parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `preset` | `emergency` | A built-in feed to start from: `emergency`, `all`, or a talkgroup category (`law`, `fire`, `ems`, …). An unknown slug shows an error. |
| `category`, `talkgroup`, `agency`, `radio`, `unit`, `q`, `system`, `site`, `kind`, `emergency` | — | Filters, with the Calls page's names and meanings (`talkgroup` is the on-air tgid). Repeat a parameter or comma-separate values for OR. |
| `minutes` | `15` | How much history the first screen loads, 1–240. |
| `limit` | `100` | Rows per read, 1–500: the first screen at most this many, and each older page this many. The page holds 1,000 at most. |
| `controls` | `1` | `controls=0` hides the filter bar (feed picker and talkgroup/category chips) for a clean embed. The status dot stays. |
| `theme` | `auto` | `light`, `dark`, or `auto` (follow the visitor's system setting). |
| `compact` | `0` | `compact=1` puts each call on one denser line. |
| `show_pending` | `0` | `show_pending=1` also lists calls before their transcript lands, and ones whose transcript was rejected. |

How a preset and filters combine:

- **No preset and no filters** → the Emergency dispatch feed.
- **Filters without a preset** replace the default feed: `?talkgroup=1101` is that channel, whatever category
  it is filed under.
- **A preset with filters** narrows the preset: `?preset=emergency&talkgroup=1101` is 1101 only if it is an
  emergency channel. A `category` in the URL overrides the preset's categories.
- `preset=all` is everything the filters match.

Examples:

```text
/embed/transcripts                                   Emergency dispatch, last 15 minutes, filter bar on
/embed/transcripts?controls=0&theme=dark             the same, clean, dark
/embed/transcripts?preset=fire&minutes=60            fire channels, the last hour
/embed/transcripts?talkgroup=1101&talkgroup=1102   two channels, any category
/embed/transcripts?preset=all&q=structure%20fire     every channel, calls mentioning "structure fire"
/embed/transcripts?compact=1&limit=40&controls=0     a narrow sidebar widget
```

## Headers: who may frame what

nginx in the `web` container (`frontend/nginx.conf`) sends:

| Path | `Content-Security-Policy` | `X-Frame-Options` |
| --- | --- | --- |
| `/embed/*` (and `/embed.html`) | `frame-ancestors $EMBED_FRAME_ANCESTORS` (default `*`) | — |
| everything else (the app, the admin pages, the API) | `frame-ancestors 'self'` | `SAMEORIGIN` |

So the embed can be framed by any site, and nothing else in Keyup can be framed by anybody but Keyup itself
— which also stops a hostile page from overlaying the admin pages in an invisible frame (click-jacking).

`EMBED_FRAME_ANCESTORS` is a CSP `frame-ancestors` source list, set in `.env` and passed to the `web` service
by `compose.yaml`. To allow only your own sites:

```sh
EMBED_FRAME_ANCESTORS="'self' https://example.org https://*.example.org"
```

The Caddy `edge` adds no headers of its own. An **outer reverse proxy** (docs/REVERSE-PROXY.md) must not add
`X-Frame-Options` or its own `frame-ancestors` for `/embed/` either — a browser obeys the strictest of the
policies it receives, and one `X-Frame-Options: SAMEORIGIN` from the outer hop is enough to blank every embed.

The page itself needs no CORS: inside the iframe it is served from the archive's own origin, so its
`fetch` and its WebSocket are same-origin (the live socket's Origin check already allows that).

## Access

The embed reads exactly what the archive lets an anonymous visitor read. With `AUTH_REQUIRE_READ` off (the
default) it works for everybody. With it on, the page says the archive is closed to guests and links to the
sign-in page; a browser that blocks third-party cookies will not send a Keyup session from inside another
site's frame, so a closed archive is in practice not embeddable.

## Size

The page is its own Vite entry (`frontend/embed.html` → `src/embed/`), not a route of the app: it shares
the React chunk with the app and nothing else — no router, query cache, charts, player or help registry.
Its own script is about 4 KB gzipped and its stylesheet about 1 KB, on top of the ~60 KB React chunk
(which a visitor who also uses the app already has cached). Each first screen is one small JSON request.

## For developers

- `npm run dev` serves the page at `/embed/transcripts` too (a dev-server rewrite in `vite.config.ts`).
- `frontend/e2e/transcripts.spec.ts` drives it in Chromium against a real archive: the sidebar link, the end
  row loading the next page, a day divider, and *No older calls* (`make e2e`, frontend/README.md).
- The URL contract lives in `frontend/src/embed/params.ts`; the Feeds card builds its snippet with the same
  functions, so the two cannot drift.
- The API side is `backend/app/api/transcripts.py`. It uses the same filter parsing, directory resolution and
  `base_call_select` as `GET /calls`, so a row is in the embed exactly when it would be on the Calls page under
  the same filter (`tests/api/test_transcripts_recent.py` checks that against `/api/calls` directly).
