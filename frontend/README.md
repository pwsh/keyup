# Keyup web

The React SPA: the calls table and player, a call's own page, search, the live
feed, the talkgroup and radio directories, statistics, alerts, admin and help.
Design and page inventory are in `docs/ARCHITECTURE.md` §6.

React 19 + Vite + TypeScript, Tailwind v4 for styling, Radix for the primitives
that need real accessibility (dialog, tooltip, select, switch, tabs, popover),
TanStack Query for the server cache, TanStack Virtual for the calls table, ECharts
for the charts, Zustand for the player and the panel state, and React Router for
routing. Biome lints and formats; Vitest runs the unit tests; Playwright runs the
end-to-end suite against the real API.

## Running it

```sh
npm install
npm run dev            # http://localhost:5174
```

`/api` is proxied to `http://localhost:8096` — the port compose maps the API to.
`VITE_API_TARGET=… npm run dev` points it somewhere else.

**With no API running**, the dev server answers `/api` with a 503 carrying
`X-Api-Unreachable`, the client latches mock mode, and the whole app runs on the
fixture corpus in `src/api/mock/`. `VITE_MOCK=1 npm run dev` forces that from the
start. Neither happens in a production build (see *Mock mode* below).

The proxy deliberately does **not** set `changeOrigin`. The API refuses a
cookie-authenticated mutation whose `Origin` is not one of its own hosts, and
rewriting `Host` while the browser keeps sending its own `Origin` makes every
sign-in and every save fail with 403 in dev and only in dev.

## The API contract

`src/api/generated.ts` is generated from `openapi.json`, a committed snapshot of
the API's own document, and `src/api/types.ts` derives every shape from it. To
pick up an API change:

```sh
curl -s http://127.0.0.1:8096/api/openapi.json > openapi.json
npm run gen:api-types
npm run typecheck
```

The generator, openapi-typescript 7, lives in `tools/openapi-gen` with its own lock
rather than in this package's devDependencies: it builds its output through the
TypeScript 5 compiler API, which the app's TypeScript 7 no longer has, and npm cannot
give it a second `typescript` while this package depends on 7. `gen:api-types` installs
it from that lock, runs it with `--default-non-nullable false` (a field with a server
default stays optional, which is what `Concrete<T>` below expects) and formats the
result with Biome so `npm run lint` passes on it. Audit it with
`npm audit --prefix tools/openapi-gen`.

The one transformation `types.ts` applies is `Concrete<T>`, which makes fields
that are optional *only because they have a server-side default* required again:
a response model always serializes them, so treating most of every object as
possibly-undefined would be noise rather than safety. `| null` is left exactly as
the schema declares it, because null really does arrive.

`src/api/types.test.ts` is what keeps this honest. It assigns the mock fixtures to
the generated schema types, checks the runtime enum arrays still describe the same
sets as the schema's enums, and checks every `CallFilterParams` key is a query
parameter `GET /calls` declares — all at compile time, so `npm run typecheck` is
the thing that fails when the fixtures drift.

## Tests

```sh
npm run typecheck      # tsc --noEmit
npm run lint           # biome check src
npm run test           # vitest: units, pages, mock contract, help completeness
npm run e2e            # playwright, against a real API
```

The unit suite runs entirely against the mock, in jsdom. Two of its tests are
worth knowing about: `src/help/help.test.ts` asserts every `Button`, `Chip`,
`Switch` and `InfoDot` in the app carries an explanation and that every id it
references exists, and `src/api/types.test.ts` is the contract test above.

### End to end

`npm run e2e` starts a dev server with `VITE_MOCK=0`, points it at
`http://127.0.0.1:8096`, and drives Chromium against whatever archive that API is
serving. Every page has to render, say something specific to that archive, and
produce no console error and no failed request.

```sh
npx playwright install chromium          # once
npm run e2e                              # the read-only suites
npm run e2e -- --headed --project=chromium
make -C .. e2e E2E_API=http://keyup.example.org  # the same, headless, against a live archive (read-only)
```

It needs an API with **recent** traffic behind it — most specs look at the last 7 days,
and the live-transcript specs at the last 4 hours — so a local API over an old dev
database fails them; pointing the dev server at a deployment with `VITE_API_TARGET`
(what `make e2e E2E_API=…` does) is the usual way to run it. The dev server is
started with `reuseExistingServer`, so if another one is already on 5174 set
`E2E_PORT` (or it will test that one). The specs find their own fixtures through
the API (the busiest talkgroup, a quiet one, a word with spelling variants), so they
hold on any archive; a spec whose precondition the archive cannot meet is skipped,
not failed.

| Spec | Covers |
|---|---|
| `smoke` | every route renders without a console error or failed request; no page scrolls the window; navigation |
| `calls` | the list, talkgroup filter, facet click, transcript search highlighting, audio playback, prev/next |
| `scroll` | the list's first page of 100, the keyset-cursor second page when the end is scrolled to (no row twice, all older), and no cursor request for a list that fits in one page |
| `detail` | a call's page: transcript segments, waveform, raw metadata, patch member links |
| `pages` | search (highlights, suggestions, empty state), live socket and hold, directories, statistics, admin signed out, help |
| `fuzzy` | the Search page's *Spelling variants* switch (URL, notice, the variants listed, off again) and the automatic widening of a word with no exact match |
| `transcripts` | the sidebar's *Transcripts* link opening `/embed/transcripts` in a new tab; the embed's end row loading the next page, a day divider when the list crosses midnight, and *No older calls* when the archive runs out |
| `mutations` | signed-in writes, each put back (skipped without credentials) |
| `screenshots` | not a test: every page in both themes and widths, for `npm run e2e:shots` |

| Variable | Effect |
|---|---|
| `VITE_API_TARGET` | API the dev server proxies to (default `http://127.0.0.1:8096`) |
| `E2E_BASE_URL` | Test an app that is already serving — a container, a deployment — instead of starting one |
| `E2E_PORT` | Port for the dev server it starts (default 5174) |
| `E2E_USERNAME`, `E2E_PASSWORD` | Enable the mutation suite |
| `E2E_FUZZY_WORDS`, `E2E_FUZZY_MISSPELLINGS` | Comma-separated words for the `fuzzy` suite: a few local place names from your archive, and misspellings of them (it skips when none widens) |

**The mutation suite** (`e2e/mutations.spec.ts`) signs in and writes: notes, tags
and stars on a call, a talkgroup alias, a favorite, a saved search, an API token,
an alert rule. It is skipped without credentials, because it writes to the archive
the API is pointed at. Everything it creates it deletes again, and the two rows it
edits it puts back. Make an account with:

```sh
cd ../backend && .venv/bin/keyup user create e2e --admin --password 'choose-one'
cd ../frontend && E2E_USERNAME=e2e E2E_PASSWORD='choose-one' npm run e2e
```

### Screenshots

```sh
npm run e2e:shots
```

Writes every page in both themes at both widths (1440×900 and 390×844) into
`e2e/screenshots/`, plus a `README.md` listing them. They are git-ignored —
pictures of live radio traffic are not fixtures — but the listing makes a missing
one obvious.

## Mock mode

`VITE_MOCK=1` serves everything from `src/api/mock/`: ~600 generated calls over
two days across twenty talkgroups, shaped like the real corpus (§1 of the
architecture) down to the squelch tails, the patches and the hallucinated
transcripts.

In a production build without `VITE_MOCK=1`, the fixtures are not merely unused —
they are **not in the bundle**. `client.ts` reaches them through one dynamic
import inside a branch guarded by a build-time constant, so the bundler folds the
branch away and drops every chunk behind it. `docker build --build-arg VITE_MOCK=1`
is the only way to get an image that contains sample data.

## Building and deploying

```sh
npm run build                       # → dist/
docker build -t keyup-web .         # nginx serving dist/, proxying /api to `api:8000`
```

`compose.yaml` runs it as the `web` service on `${WEB_PORT:-8095}`. nginx and the
API are one origin there, which is what lets the session cookie and the API's
same-origin check work with no CORS allowance at all. `nginx.conf` also gives the
live WebSocket and the SSE endpoint an hour-long read timeout with buffering off,
passes Range requests through untouched for the audio, and never caches
`index.html` while caching the content-hashed assets forever.

**Pre-compressed assets.** `npm run build` writes a level-9 `.gz` twin beside every
text file of 1 KB or more in `dist/` (the `keyup-precompress` plugin in
`vite.config.ts`), and nginx's `gzip_static on` sends that file as it is instead of
compressing the same bytes at level 5 on every request; anything without a twin —
the API's JSON, small files — still goes through on-the-fly gzip. The twins are taken
in `writeBundle`, from the files Vite has already written, and the build fails if a
chunk still holds an unresolved `__VITE_PRELOAD__`-style placeholder: a twin taken
earlier (in `generateBundle`) was once a different, broken file from the one beside
it, and every page failed with "Can't find variable: `__VITE_PRELOAD__`". Gzip only:
the stock nginx image has no brotli module.

None of that is inherited by a second proxy in front of the stack. If the site is published
through an external TLS reverse proxy, that hop has to repeat the upgrade and no-buffering rules
or the live feed never connects: [../docs/REVERSE-PROXY.md](../docs/REVERSE-PROXY.md). The app
falls back to `/api/live/sse` after two failed WebSocket handshakes inside 30 s and says so in the
connection badge's tooltip, which keeps the site usable but does not make the proxy correct.

## Layout

```
src/api/        generated.ts (from openapi.json), types.ts, client.ts, hooks.ts,
                live.ts, and mock/ — the only files that name an endpoint
src/pages/      one file per route
src/components/ ui/ (primitives), layout/, calls/, directory/, charts/, player/
src/state/      filters (URL-backed), player, panels, theme, auth, toasts
src/lib/        formatting, colors, chart options, CSV, highlighting
src/help/       every tooltip in the app, in one map, with its own test
e2e/            Playwright specs and the screenshot run
```

Two rules worth keeping: **every filter lives in the URL**, so every view is a
shareable link and the back button steps through filter changes; and **every
control has a tooltip**, whose text lives in `src/help/help.ts` so the Help page
and the control itself can never disagree.
