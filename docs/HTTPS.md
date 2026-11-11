# HTTPS for Keyup

Keyup serves its own certificates: real Let's Encrypt certificates, trusted by every
browser and phone out of the box, obtained and renewed by the `edge` container (Caddy).

How Let's Encrypt checks that the box owns the names is the **challenge**, and Keyup
supports two:

* **HTTP-01** (with TLS-ALPN-01, which Caddy tries alongside it) — Let's Encrypt connects
  to each name on port 80 or 443. Needs the names to resolve **publicly** to this box and
  ports 80 and 443 open to the internet. No credential, no DNS provider. This is the
  simple case: a box with a public address, or a home router forwarding 80/443 to it.
  **Beta:** this path has been tested end to end against a local ACME server (Pebble), but
  not yet against Let's Encrypt itself. Run it against the
  [staging CA](#rehearse-against-staging-first) first, and please report how it went.
* **DNS-01 through Cloudflare** — proved by writing a TXT record in the zone, so the only
  thing that has to be reachable is Cloudflare's API, outbound. Works for **LAN-only
  names** on a box with no inbound port at all. Needs the zone on Cloudflare and an API
  token.

`KEYUP_TLS_CHALLENGE` (Admin → HTTPS → *ACME challenge*) chooses: `http`, `dns`, or `auto`
(the default) — DNS-01 when a Cloudflare token is set, HTTP-01 otherwise.

Off by default. A fresh install with no domains configured runs plain HTTP.

Ports, throughout this page: `:80` and `:443` are the edge container's own ports. On the
host they are published as `WEB_PORT` and `WEB_TLS_PORT` — 8095 and 8443 unless `.env`
says otherwise — so `http://192.0.2.10/…` below means `http://192.0.2.10:8095/…` on a
stock install. HTTP-01 needs them to be 80 and 443.

**It is configured from the Admin page.** Admin → **HTTPS** holds the switch, the domains,
the ACME contact address, the challenge, the Cloudflare token, the certificate authority
and the redirect — and saving there re-renders the proxy's configuration and loads it into the
running container, with no redeploy and no dropped connection. The environment variables
below still exist and still work; they are now the *bootstrap* rather than the last word.
[Where the configuration comes from](#where-the-configuration-comes-from) is the whole
story of which wins.

## How it works

```
browser ──https:443──▶ edge (Caddy)  ──http──▶ web (nginx)  ──http──▶ api (uvicorn)
         http:80 ──▶ 308 to https              SPA + /api proxy        FastAPI
                       │  ▲                                              │
                       │  └── POST /load, admin API on edge:2019 ────────┘
                       │      (compose network only — never published)
                       ├── DNS-01 ──▶ Cloudflare API (outbound only), or
                       └── HTTP-01 / TLS-ALPN-01 ◀── Let's Encrypt (inbound :80/:443)
```

* **`edge`** — the `edge` compose service, built from [`deploy/edge/`](../deploy/edge).
  Caddy 2.11.7 with `github.com/caddy-dns/cloudflare` compiled in by `xcaddy` (stock
  `caddy:2` cannot do DNS-01 at all — the provider is a separate module; HTTP-01 needs
  nothing beyond stock Caddy). It is now the
  only thing publishing a port on the host's web side; `web` lost its published port and
  is reached over the compose network.
* **`deploy/edge/entrypoint.sh`** picks one of five configurations from the environment,
  because a Caddyfile has no `if`:
  * `Caddyfile.plain` — TLS off. `:80` → `web:80`, nothing else. This is what runs when
    `KEYUP_TLS_DOMAINS` is blank, or when it is set but the ACME email is missing, or the
    challenge is DNS-01 and the token is missing (a half-filled `.env` leaves the archive
    reachable rather than taking the stack down; the reason is logged).
  * `Caddyfile.tls` — TLS on, `KEYUP_TLS_REDIRECT_HTTP=true` (the default). A site block
    for every name in `KEYUP_TLS_DOMAINS`, `tls` with the `dns cloudflare` solver, and a
    `:80` block that answers `/health`, proxies `/api/*` through to nginx over plain HTTP
    (see [Recorders](#recorders)) and 308s everything else to https.
  * `Caddyfile.tls-noredirect` — TLS on, `KEYUP_TLS_REDIRECT_HTTP=false`. The same https
    site, and `:80` serves the **whole site in the clear as well** rather than redirecting.
    `auto_https disable_redirects` is in its global block and is what makes that work:
    without it Caddy synthesises its own `:80` → `:443` redirect for every name in the site
    block, and that redirect takes precedence over the catch-all `:80`. Use it while
    something on the LAN cannot follow a redirect, or during a hostname cutover.
  * `Caddyfile.tls-http01` and `Caddyfile.tls-http01-noredirect` — the same two, for the
    HTTP-01 challenge (`KEYUP_TLS_CHALLENGE=http`, or `auto` with no token): a bare
    `tls <email>` with no `dns` solver and no `resolvers`, so Caddy uses its default
    HTTP-01/TLS-ALPN-01 issuer. The `:80` block's redirect does not get in the way of the
    challenge: while an issuance is pending, Caddy answers
    `/.well-known/acme-challenge/<token>` itself, ahead of every handler in the site
    blocks (verified with a local Pebble ACME server, with TLS-ALPN-01 made impossible so
    only HTTP-01 could pass, in both redirect modes).

  Either way the config is run through `caddy validate` before `caddy run`, so a broken
  file is a loud startup failure, not a half-started proxy.
* **All five files are generated** from `backend/app/edge/render.py` and checked in as
  artifacts (`cd backend && .venv/bin/python -m app.edge` regenerates them;
  `tests/test_edge_render.py` fails if they have drifted). That module is also what the API
  renders when it pushes a configuration over the admin API, so a Caddyfile started from
  the environment and one pushed from the Admin page cannot disagree.
* **The admin API** listens on `0.0.0.0:2019` *inside* the edge container, in every
  variant. `compose.yaml` publishes no port for it, deliberately: Caddy's admin API is
  unauthenticated and can replace the entire proxy configuration, so the only client that
  can reach `edge:2019` is another service on the compose network — which today means the
  `api` container, and nothing else. Do not publish it.
* **Streaming** — `reverse_proxy … { flush_interval -1 }` disables response buffering, so
  the live WebSocket, the SSE feed, ranged audio and streamed exports pass through
  unbuffered. Caddy upgrades WebSockets natively; there is no `Upgrade`/`Connection`
  plumbing to write. There is deliberately **no** `request_body max_size`: Caddy imposes
  no limit of its own, and recorder pushes and migration uploads are large.
* **Headers** — Caddy's `reverse_proxy` forwards the original `Host` and sets
  `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` by default; nginx
  (`frontend/nginx.conf`) passes an incoming `X-Forwarded-Proto`/`-Host` through rather
  than overwriting it with its own hop, so the API sees `https` and the browser's real
  hostname. uvicorn runs with `--proxy-headers --forwarded-allow-ips '172.16.0.0/12'`, so
  it believes those headers only from the compose network (the nginx hop) — a client on the
  directly published API port can no longer set its own address — and it rewrites the
  request's client address from them. The API's client-IP logic (`TRUSTED_PROXY_HOPS=2`:
  client → Caddy → nginx) then reads the same chain, so the two agree.

## Where the configuration comes from

Two places, and one of them wins per field.

| | |
| --- | --- |
| **A settings-table row is set** (Admin → HTTPS) | that value wins |
| **No row** | the environment value from `.env` is used |

That is the same precedence every other runtime setting in this deployment has
(`app/runtime_settings.py`). The environment is a **seed**, never a floor: nothing is
copied into the table at startup, and clearing a field in the Admin page deletes the row so
the environment value comes back.

The seven settings and the environment names they fall back to:

| Setting (Admin → HTTPS) | Environment |
| --- | --- |
| `tls_enabled` | *(none — a non-empty `KEYUP_TLS_DOMAINS` was the switch, and still implies on)* |
| `tls_domains` | `KEYUP_TLS_DOMAINS` |
| `tls_acme_email` | `ACME_EMAIL` |
| `tls_cloudflare_api_token` | `CLOUDFLARE_API_TOKEN` |
| `tls_acme_ca` (`production`/`staging`) | `ACME_CA` (a directory URL; a staging URL reads as `staging`) |
| `tls_redirect_http` | `KEYUP_TLS_REDIRECT_HTTP` |
| `tls_challenge` (`auto`/`dns`/`http`) | `KEYUP_TLS_CHALLENGE` |

`tls_enabled` has no environment variable of its own on purpose. Before the Admin page had
this group, a non-empty `KEYUP_TLS_DOMAINS` *was* the switch, and `entrypoint.sh` still
reads it that way — so an existing deployment that has never opened the page comes up with
the switch **on**, and the API's start-up apply does not quietly take a working certificate
down. (`KEYUP_TLS_ENABLED` is accepted if you want to be explicit.)

### How a change reaches the running proxy

Caddy keeps its whole configuration in memory and exposes it on an admin API. `POST /load`
with `Content-Type: text/caddyfile` hands it a new Caddyfile, which it adapts and swaps in
**atomically**: either the new configuration is valid and takes effect with no dropped
connection, or the old one keeps running and the response says why. That is the whole
reason "configure SSL and DNS from the settings page" is possible rather than a redeploy.

The API pushes at three moments:

1. **On start-up.** A deploy restarts the edge, which comes back on whatever
   `entrypoint.sh` read from `.env`. The API applies the effective configuration a second
   or two after it starts — so **after a redeploy the site is briefly in environment mode**
   until it does. Best effort: compose starts the edge *after* the API, so the first
   attempt often fails, and (3) is the net under that.
2. **When an HTTPS setting is saved.** `PUT /api/admin/settings` applies afterwards for
   `tls_*` keys, so there is no separate "and now press Apply".
3. **Every five minutes, from the worker.** The edge can restart on its own —
   `restart: unless-stopped`, an image pull, an OOM — and nothing tells the API when it
   does. The reconcile is one `GET /config/` against the edge: if the hash of the last
   Caddyfile we successfully loaded still matches what the settings render *and* the proxy
   is answering for the domains we asked for, it does nothing; otherwise it re-applies.
   (The hash is compared rather than the JSON: `GET /config/` returns *adapted* output with
   generated server names and filled-in defaults, and a diff over it would either never
   match or prove nothing.)

Three bookkeeping rows in the same `settings` table record the outcome —
`https_last_applied_at`, `https_last_error`, `https_last_applied_hash`. They are not in the
settable whitelist and never reach `Settings`; the Admin page reads the first two.

### What can go wrong, and how it looks

**A wrong or wrongly-scoped Cloudflare token leaves the site on plain HTTP, quietly.** The
DNS-01 challenge cannot be answered, no certificate is issued, and Caddy does not fail — it
keeps serving plain HTTP on `:80`, exactly as it did before, and https simply never comes
up. The symptom is "nothing happened". Two things guard against it:

* **Test Cloudflare token** (Admin → HTTPS) asks Cloudflare `GET /user/tokens/verify` (is
  this token live?) and `GET /zones?name=<zone>` for each domain's zone (can it *see* the
  zone?). A token scoped to the wrong zone passes the first and fails the second, which is
  exactly the mistake that would otherwise surface twenty seconds into a failed issuance.
  It writes nothing, issues nothing and spends no rate limit. The token travels in the
  `Authorization` header and is never logged, never returned and never put in a URL.
* The status card shows **wanted** beside **serving**. "Wanted HTTPS, serving plain HTTP"
  is this failure, named.

**HTTP-01 fails the same quiet way** when Let's Encrypt cannot reach the box: a name that
does not resolve publicly to it, port 80 or 443 not forwarded, or the edge published on
other host ports (`WEB_PORT`/`WEB_TLS_PORT` must be 80/443 for this challenge — Let's
Encrypt only ever connects to those two). `docker compose logs edge` names the failing
challenge and the address Let's Encrypt tried.

The certificate rows on that card are read by **handshaking with the edge**: the API opens
a TLS connection to `edge:443` with SNI set to each domain and reads the certificate off
the wire. Caddy's admin API exposes its configuration and its local PKI but nothing that
says "the certificate currently loaded for this name", so this is the only honest check.
A `staging` certificate reports its issuer and expiry with a "not trusted" marker — that is
correct, not a bug.

**The API is never redirected**, whichever way `tls_redirect_http` is set: `/health` and
`/api/*` answer over plain HTTP on `:80` (see [Recorders](#recorders)). So a failed cutover
does not lose a single recorder upload.

## Turning it on

Either fill the fields in on Admin → **HTTPS** and press Save — which needs no restart — or
set the environment in `.env` as below and `docker compose up -d`. Pick the challenge
first:

| | HTTP-01 | DNS-01 (Cloudflare) |
| --- | --- | --- |
| Names resolve | publicly, to this box | anywhere — a LAN-only address is fine |
| Inbound ports | 80 and 443 from the internet | none |
| Credential | none | Cloudflare API token, DNS edit on the zone |
| `KEYUP_TLS_CHALLENGE` | `http` (or `auto` with no token) | `dns` (or `auto` with a token) |

### Option A: HTTP-01 (public names, ports 80/443 open) — beta

Not yet run against Let's Encrypt itself (see the note at the top): do the
[staging rehearsal](#rehearse-against-staging-first) before switching to the production CA.

1. **DNS:** an A (and/or AAAA) record for each name, pointing at the box's **public**
   address — or at your router, with ports 80 and 443 forwarded to the box.
2. **Ports:** publish the edge on the standard ports, since Let's Encrypt only connects to
   80 and 443. In `.env`:

   ```sh
   WEB_PORT=80
   WEB_TLS_PORT=443
   ```

3. **`.env`:**

   ```sh
   KEYUP_TLS_DOMAINS=keyup.example.com
   KEYUP_TLS_CHALLENGE=http
   ACME_EMAIL=you@example.com
   ACME_CA=
   KEYUP_TLS_REDIRECT_HTTP=true
   PUBLIC_URL=https://keyup.example.com
   PUBLIC_BASE_URL=https://keyup.example.com
   ```

   `KEYUP_TLS_CHALLENGE=http` is explicit here; `auto` reaches the same answer as long as
   `CLOUDFLARE_API_TOKEN` is blank.

Then [rehearse against staging](#rehearse-against-staging-first) and
[start it](#start-and-watch).

### Option B: DNS-01 through Cloudflare (LAN-only names)

#### 1. Cloudflare token

Cloudflare dashboard → My Profile → **API Tokens** → *Create Token* → *Create Custom
Token*:

| Field | Value |
| --- | --- |
| Permissions | **Zone → DNS → Edit** |
| Zone Resources | Include → Specific zone → *the one zone holding these names* |

That is the whole scope. Not the Global API Key — a token limited to DNS edit on one
zone is all DNS-01 can use, and all it can do if it leaks.

#### 2. DNS records

In that zone, A records pointing at the box. DNS-01 does **not** care where the name
points — resolution is only for your own clients — so a private address is fine:

| Name | Type | Value | Proxy |
| --- | --- | --- | --- |
| `keyup.example.com` | A | `192.0.2.10` (the box's LAN address) | **DNS only** (grey cloud) |

Cloudflare's orange-cloud proxy must be **off**: it would try to reach the box from
Cloudflare's network. If you also want a public name that reaches the box some other way
(VPN, tunnel), add it to `KEYUP_TLS_DOMAINS` as a second hostname and the certificate
covers both.

#### 3. `.env`

```sh
KEYUP_TLS_DOMAINS=keyup.example.com
KEYUP_TLS_CHALLENGE=dns
WEB_TLS_PORT=443
CLOUDFLARE_API_TOKEN=<the token from step 1>
ACME_EMAIL=you@example.com
ACME_CA=
KEYUP_TLS_REDIRECT_HTTP=true
PUBLIC_URL=https://keyup.example.com
PUBLIC_BASE_URL=https://keyup.example.com
```

`.env` holds the token in the clear, beside the Postgres password; keep it out of version
control and readable only by the account that runs compose. Or leave
`CLOUDFLARE_API_TOKEN` blank here and paste the token into Admin → HTTPS, where it is
stored write-only in the settings table (then set `KEYUP_TLS_CHALLENGE=dns` explicitly, or
`auto` would read the blank variable as HTTP-01 when the edge container starts).

### Both: the redirect, and the public URL

`KEYUP_TLS_REDIRECT_HTTP`: `true` (the default) — plain `:80` 308s browsers to https.
`false` makes `:80` serve the whole site in the clear **as well as** https, for a client on
the LAN that cannot follow a redirect. `/health` and `/api/*` are never redirected either
way.

Filling these in and restarting is one of two ways; the other is Admin → HTTPS, and a row
set there wins over anything here (see
[Where the configuration comes from](#where-the-configuration-comes-from)).

`PUBLIC_URL` / `PUBLIC_BASE_URL` are **changed by hand**; nothing derives them from the
domain list. They are what alert links use, and `AUTH_COOKIE_SECURE=auto` reads
`PUBLIC_URL` to decide whether the session cookie gets the `Secure` flag — so leaving
them on an `http://` address after a cutover means the cookie ships without `Secure`.

### Rehearse against staging first

Let's Encrypt's production rate limits are per-week and unforgiving. Set

```
ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory
```

(or *Certificate authority* → `staging` on Admin → HTTPS), start, and confirm a
certificate is issued. Staging certificates are signed by an untrusted root, so browsers
and `curl` will complain — that is the expected result, and `curl -k` is how you look past
it. Then blank `ACME_CA` and restart for the real one.

### Start and watch

```sh
docker compose up -d
docker compose logs -f edge
```

A first issuance takes seconds (HTTP-01) to tens of seconds (DNS-01, which waits for the
TXT record to propagate). Success looks like `certificate obtained successfully`. Then:

```sh
curl -vk https://keyup.example.com/health      # -> ok, and the cert chain in the -v output
curl -sI http://keyup.example.com/             # -> 308 Permanent Redirect to https://
curl -s  https://keyup.example.com/api/status  # the archive answers over TLS
curl -s  http://192.0.2.10/health              # -> ok over plain http (the health probe)
curl -s  http://192.0.2.10/api/status          # -> the API, NOT a redirect (see Recorders)
```

`/health` and `/api/…` answer over plain http on `:80` — on the raw IP and on the TLS
hostname alike — and every other path is 308'd to https. `auto_https disable_redirects`
in the global block switches Caddy's own synthesised redirect off so that the explicit
`:80` block decides; that is intended.

## Renewal

Automatic, and there is nothing to schedule. Caddy renews each certificate about 30 days
before expiry using the same challenge, while running. The only operator duty is keeping
the DNS-01 token valid, or for HTTP-01 keeping the names and ports 80/443 pointed at the
box.

## Where the certificates live

In the **`keyup_caddy_data` named volume** (named after the compose project), not in the
checkout — deliberately. A certificate stored in the tree would be lost whenever the
checkout is replaced and re-issued each time, which walks straight into the rate limits.
The volume survives redeploys, `docker compose down` and image rebuilds.

```sh
docker run --rm -v keyup_caddy_data:/d alpine ls -R /d/caddy/certificates
```

The ACME **account key** lives in the same volume (`/d/caddy/acme/`). Losing the volume
costs a fresh account and a fresh issuance, not an outage — but it does spend rate limit,
so do not prune it casually.

## Turning it off

Turn **Serve HTTPS** off in Admin → HTTPS and save: the API pushes the plain configuration
into the running proxy within a second, no restart involved. Then set
`PUBLIC_URL`/`PUBLIC_BASE_URL` back to their `http://` address (nothing derives them) and
restart at your leisure.

The environment-only route still works: blank `KEYUP_TLS_DOMAINS` in `.env` and
`docker compose up -d`. The edge restarts on `Caddyfile.plain` and serves plain
HTTP on `WEB_PORT` again — but note that if a settings-table row says otherwise, the API
will push the table's version back a second later. The table is what decides.

Nothing else in the stack knows the difference either way, and the certificates stay in the
volume for whenever you switch back.

## Recorders

**A cutover does not break ingest.** The API stays reachable over plain HTTP on the LAN by
design: the `:80` listener passes `/api/*` straight through to nginx, unbuffered and with
no body limit, exactly as it did before TLS existed. Only browser paths are redirected.

That is deliberate rather than an oversight. Recorders on the LAN often push to
`http://192.0.2.10/api/call-upload` **by IP**, and no certificate can ever cover an IP
address — a blanket 308 would send them to `https://192.0.2.10/…`, where the handshake
fails on the hostname mismatch and the upload is lost. So:

| Request | over http on :80 |
| --- | --- |
| `/health` | `200` — the health probe |
| `/api/…` | proxied to the API, unchanged |
| `/.well-known/acme-challenge/…` while an HTTP-01 issuance is pending | answered by Caddy |
| anything else | `308` to `https://{host}{uri}` |

Move recorders to `https://keyup.example.com/api/call-upload` **when convenient, not as a
prerequisite**. sdrtrunk and trunk-recorder both take an `https://` upload URL and
validate against the system trust store, which a Let's Encrypt certificate satisfies with
no extra configuration; the recorder host only has to resolve the name, which is what the
A record is for. See [RECORDERS.md](RECORDERS.md).

Until they move, that one hop stays unencrypted on the LAN — and the recorder's upload
token travels with it. That is the same exposure as before TLS was introduced, no worse,
but it is the reason to make the move eventually.

## Relationship to an external reverse proxy

This document is about Keyup terminating TLS **itself**. If instead a separate
public-facing proxy (or a tunnel) terminates TLS and forwards to the box's `WEB_PORT` — the
`radio.example.com` arrangement — that outer hop has its own requirements (WebSocket
upgrade headers, `proxy_buffering off` for SSE, `client_max_body_size 0`), and they are
written down in [REVERSE-PROXY.md](REVERSE-PROXY.md). The two are alternatives, not
layers: with `KEYUP_TLS_DOMAINS` blank the `edge` container is a plain HTTP pass-through
and everything in that guide applies unchanged. If you ever run both — an external proxy
in front of an edge that is *also* doing TLS — the outer hop must talk https to
`WEB_TLS_PORT` and forward the original `Host`, or the certificate Caddy picks will not
match the name the browser asked for.

## Testing the configuration without deploying

```sh
make edge-check
```

builds the edge image and runs `caddy validate` over all five Caddyfiles, plus
`docker compose config` with and without `KEYUP_TLS_DOMAINS` set.

The renderer and its checked-in artifacts have tests of their own, and they need neither
Docker nor a database:

```sh
cd backend && .venv/bin/pytest tests/test_edge_render.py tests/test_edge_admin.py
```

`test_checked_in_caddyfiles_match_renderer` is the one that matters: it fails if
`deploy/edge/Caddyfile.*` has drifted from `app/edge/render.py`. Regenerate with
`.venv/bin/python -m app.edge`.

## Session timeouts (unrelated, same page)

Not TLS, but the other thing that used to need a redeploy: how long a sign-in lasts is now
Admin → Settings, group **Access** — `auth_session_days` (absolute, default 14, read when
the session is created, so it applies to the next sign-in) and `auth_idle_hours` (sliding,
re-read on every request, so shortening it signs idle sessions out at once). The
`AUTH_SESSION_DAYS` / `AUTH_IDLE_HOURS` environment variables are their defaults.
