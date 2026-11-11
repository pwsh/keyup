# Keyup

Radio-call archive for P25 trunked radio systems. Keyup takes the calls recorded by
[trunkd](https://github.com/pwsh/trunkd), [sdrtrunk](https://github.com/dsheirer/sdrtrunk) or
[trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder) — pushed over the Rdio Scanner-compatible
`POST /api/call-upload`, or picked up from a recording directory — and runs them through metadata extraction →
audio clean-up → speech-to-text (a CPU container, a GPU server or Intel OpenVINO) → PostgreSQL, behind a
searchable API, a web UI and a live feed.

It also keeps a **site channel audit**: the recorder posts what each site announces on the air, RadioReference says
what should be there, and Keyup reports the difference — undocumented channels, channels nobody has heard for a
fortnight, a control channel that has drifted onto its backup, control frequencies the recorder is not configured to
hunt, and neighboring towers nobody is recording. Optional exporters forward calls to Broadcastify Calls, a
Broadcastify live feed and OpenMHz (all off by default, bring your own keys).

The full guide — hardware, installing trunkd and Keyup, connecting them, transcription, TLS, exporters,
operations and the legal side of publishing radio traffic — is at
**[pwsh.github.io/radio-docs](https://pwsh.github.io/radio-docs/)**.

## Quick start

You need a Linux host with Docker (Compose v2) and a few GB of disk per month of audio.

```bash
git clone https://github.com/pwsh/keyup.git && cd keyup
scripts/bootstrap.sh          # or: make bootstrap
```

`scripts/bootstrap.sh` creates `.env` from `.env.example` with a fresh `SECRET_KEY` and `POSTGRES_PASSWORD`, this
host's timezone and `PUBLIC_URL`; builds and starts the stack (db, migrate, api, worker, web and edge; the UI is on
`http://<this host>:8095`); asks for the first admin account; mints a recorder upload token; and prints the
`[uplink]` block to paste into trunkd's `trunkd.toml`. It is safe to run again: an existing `.env`, admin or token
is left alone (`--force-env` replaces `.env`, keeping a backup). Unattended:
`scripts/bootstrap.sh --non-interactive --admin admin` (the password comes from `KEYUP_ADMIN_PASSWORD`, or is
generated and printed once). `scripts/bootstrap.sh --help` lists the rest.

To update later: `scripts/update.sh` (or `make update`) — `git pull --ff-only`, build, migrate, restart, wait for
health; `.env` is never touched.

Other recorders push to `<PUBLIC_URL>/api/call-upload` with the same kind of token
([docs/RECORDERS.md](docs/RECORDERS.md)); mint more with
`docker compose exec api keyup token create <label> --scopes upload`. Transcription backends are managed in
Admin → Backends ([docs/STT.md](docs/STT.md)): `STT_BACKENDS` in `.env` only seeds that table on the first start;
after that the database wins. Reading the archive needs no sign-in by default (`AUTH_REQUIRE_READ=false`): set it
to `true` before the archive is reachable by anyone you would not show every transcript to.

## Documentation

- Architecture contract: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Serving it over HTTPS: [docs/HTTPS.md](docs/HTTPS.md) — Let's Encrypt certificates for LAN hostnames via the
  ACME DNS-01 challenge against Cloudflare; off by default, and configured from Admin → HTTPS without a redeploy.
- Serving it publicly through another proxy: [docs/REVERSE-PROXY.md](docs/REVERSE-PROXY.md) — the outer hop must
  repeat the WebSocket upgrade and SSE no-buffering rules, or the live feed sits on "Offline"/"Reconnecting"
- Pointing recorders at it: [docs/RECORDERS.md](docs/RECORDERS.md)
- Auditing a site's channels against RadioReference: [docs/CHANNEL_AUDIT.md](docs/CHANNEL_AUDIT.md)
- Classifying a channel — agency, category and RadioReference service tag: [docs/TALKGROUPS.md](docs/TALKGROUPS.md)
  — what each value means, where agencies come from and how to add one your dispatch center has no code
  for. The same words appear on the app's own Help page; both render `backend/app/vocabulary.py`.
- Presence from the control channel: [docs/RADIO_EVENTS.md](docs/RADIO_EVENTS.md) — registration, affiliation and
  data events pushed by the recorder to `POST /api/recorder/events`, folded into `radios` and `radio_affiliations`
  so a radio that is *there* but never keys up stops being invisible; kept 90 days by default, because it is
  presence data about identifiable people
- Pointing recorders at it: [docs/RECORDERS.md](docs/RECORDERS.md) — including
  [priority and flow control](docs/RECORDERS.md#priority-and-flow-control): uploads carry a class
  (`urgent`/`live`/`backlog`/`bulk`), the API answers `429`/`507` with `Retry-After` and a queue gauge rather than
  building an unbounded backlog, and live calls are never starved by a bulk import
- Publishing a search as RSS/Atom: [docs/FEEDS.md](docs/FEEDS.md) — any call filter becomes a feed with the
  transcript as the item body and a link back to the call; the token in the URL is the whole credential, so the
  URL is a secret, and `?feed=` audio enclosures open only the calls that feed's own filters match
- Embedding live transcripts in another site: [docs/EMBED.md](docs/EMBED.md) — `/embed/transcripts` is a small,
  iframe-able page (Emergency dispatch and the last 15 minutes by default, any Calls filter by query string); the
  Feeds page builds the snippet, and only `/embed/*` may be framed by other sites; the app's sidebar
  **Transcripts** link opens it full-window in a new tab, and it scrolls on into older calls
- Migrating an existing sdrtrunk archive: [docs/MIGRATION.md](docs/MIGRATION.md) (`tools/migrate_sdrtrunk.py`)
- Running two recorders over one system: [docs/RECONCILIATION.md](docs/RECONCILIATION.md) — the same transmission
  arrives from sdrtrunk and from trunkd with different start times, lengths and metadata; Keyup matches them on
  interval overlap, keeps the better copy as the call and folds the other's metadata into it (`keyup reconcile`)
- Transcription backends — the CPU profile, a GPU server, Intel OpenVINO: [docs/STT.md](docs/STT.md)
- Exporting to Broadcastify and OpenMHz: [docs/EXPORTERS.md](docs/EXPORTERS.md)
- Operating the database: [docs/OPERATIONS.md](docs/OPERATIONS.md) — Postgres tuning and where it lives,
  per-table autovacuum, which indexes were dropped and which four only *look* unused, and the one-off
  reclaim commands that are deliberately not in a migration
- Not built, written up: sending corrections back to RadioReference ([docs/RR_WRITEBACK.md](docs/RR_WRITEBACK.md) —
  RR has no write API, so the route is a submission report an operator pastes) and word clouds per radio and
  talkgroup ([docs/WORD_CLOUD.md](docs/WORD_CLOUD.md))
- Transcribing on a separate Intel box: [github.com/pwsh/whisper-lxc](https://github.com/pwsh/whisper-lxc), a
  deployment kit for an OpenVINO Model Server appliance that Keyup uses as one more backend

## Layout

```
backend/   FastAPI + SQLAlchemy 2 + Alembic  (app/ingest, app/stt, app/api, app/worker.py)
frontend/  React 19 + Vite + Tailwind 4 + Radix (mock mode for dev, Playwright e2e)
deploy/    edge/ (the Caddy front door) and backup/ (database and audio backup units)
docs/      the architecture contract and the feature guides
tools/     standalone operator scripts (migrate_sdrtrunk.py — stdlib only, copy and run)
audio/     a local recording directory for development (untracked)
```

## Develop

```bash
make dev-db test-db          # Postgres on 55440 (dev) and 55441 (tests)
make venv migrate            # backend/.venv, alembic upgrade head
make ingest                  # scan ./audio into the dev DB
cd backend && .venv/bin/uvicorn app.main:app --port 8096 --reload
cd frontend && npm i && npm run dev     # http://localhost:5174, proxies /api → 8096
make test lint               # backend; frontend: npm run typecheck && npm test && npx playwright test
make migration-roundtrip     # every alembic revision downgraded and upgraded again, on a scratch database
make e2e                     # Playwright, headless, against the API on :8096 (E2E_API=http://<host> for a
                             # running archive, read-only); see frontend/README.md "End to end"
```

CI is `.github/workflows/ci.yml`: on every push to `main` and every pull request it
runs ruff, the migration round trip and the backend suite against a Postgres service, and the frontend's lint,
unit tests and build. The Playwright suite needs a live archive, so it runs only when the workflow is started by
hand with an `e2e_api` URL.

## Run it

An update is `git pull --ff-only`, then `docker compose build`, `docker compose run --rm migrate` (alembic and the
agency seed, while the old containers still serve) and `docker compose up -d`; stop at the first step
that fails. Backend dependencies come from `backend/requirements.lock`
(exact versions and hashes; `make lock` regenerates it, `make lock-upgrade` moves it forward).
Services: `db`, `migrate` (one-shot), `api` (:8096), `worker` (ingest watcher, transcription, retention, alerts), `web` (nginx, internal
only) and `edge` (Caddy, `:80` and `:443`, the host's only web listener). `edge` is a plain HTTP pass-through until
`KEYUP_TLS_DOMAINS` is set, at which point it obtains and renews Let's Encrypt certificates by itself. That
variable is the bootstrap; Admin → **HTTPS** is where it is actually configured, and a change there is pushed into
the running proxy over its admin API — see [docs/HTTPS.md](docs/HTTPS.md).

## Contributing and security

Bug reports and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Please report security
problems privately as described in [SECURITY.md](SECURITY.md). Third-party licenses are listed in
[THIRD-PARTY.md](THIRD-PARTY.md).

## License

Keyup is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version ([LICENSE](LICENSE), SPDX `AGPL-3.0-or-later`). If you run a modified Keyup as a network service,
the AGPL asks you to offer its users the source of your version.

Copyright © 2026 Eric Holzhueter.
