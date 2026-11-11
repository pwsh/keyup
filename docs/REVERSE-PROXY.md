# Putting Keyup behind an external reverse proxy

Keyup's own `web` container already runs nginx (`frontend/nginx.conf`): it serves the built SPA
and proxies `/api` to the API container, and it has the WebSocket and Server-Sent-Events plumbing
those routes need. **That plumbing is not inherited by a proxy in front of it.** When a second,
public-facing proxy terminates TLS and forwards to the Keyup host — `radio.example.com` →
`192.0.2.10:8095` (the box's address and its `WEB_PORT`) — every rule below has to be repeated on the *outer* hop, or the streaming routes
break while the rest of the site looks perfectly fine.

The symptom is always the same: the pages load, the calls list fills in, and the header badge
says **Offline** or the live page's pill says **Reconnecting** and never stops.

> **Or let Keyup terminate TLS itself.** If the goal is simply https, there is an option that
> needs no outer proxy at all: the `edge` container (Caddy) obtains and renews Let's Encrypt
> certificates itself — by HTTP-01 for public names on a box with ports 80/443 open, or by
> DNS-01 through Cloudflare for names that only resolve on the LAN, with no inbound port — see
> [HTTPS.md](HTTPS.md). It ships off (`KEYUP_TLS_DOMAINS` blank), in which case it is a plain
> HTTP pass-through and everything in this guide applies exactly as written: the outer proxy
> still forwards to the box's `WEB_PORT`. The two are alternatives rather than layers. If you do run both, the outer
> hop must talk https to `WEB_TLS_PORT` and forward the original `Host`, or Caddy cannot pick
> the certificate matching the name the browser asked for.

---

## 1. What actually has to survive the outer hop

| Route | Protocol | Needs |
| --- | --- | --- |
| `/api/live` | WebSocket | `proxy_http_version 1.1`, `Upgrade`/`Connection` headers, `proxy_buffering off`, **3600 s** read/send timeout |
| `/api/recorder/stream` | WebSocket | the same upgrade headers, **300 s** read/send timeout (the API heartbeats every 15 s) |
| `/api/live/sse` | SSE | `proxy_buffering off`, `proxy_cache off`, **3600 s** read timeout |
| `/api/activity/stream` | SSE | `proxy_buffering off`, long read timeout |
| `/api/admin/recorders/stream` | SSE | `proxy_buffering off`, long read timeout |
| `/api/admin/recorders/spectrum/stream` | SSE | `proxy_buffering off`, long read timeout |
| `/api/…` (everything else) | HTTP | `client_max_body_size 0` for recorder uploads and streamed exports; `proxy_buffering off` so range requests on call audio reach the API untouched |

The two WebSocket routes are the ones that fail *silently*: an upgrade request that arrives at
the inner nginx without `Upgrade: websocket` is answered as an ordinary HTTP request, the browser
sees the handshake fail, and nothing in any log says "your proxy ate the upgrade header".

The SSE routes fail differently — they connect, and then nothing arrives, because the outer proxy
is buffering the response. `proxy_buffering off` is what makes an event stream an event *stream*.

### Forwarded headers

The app derives its own URLs from the browser's location, not from configuration:

- `apiUrl()` (`frontend/src/api/client.ts`) returns a **relative** path — `` `/api${path}${query}` ``.
  It carries no scheme or host at all, so it is correct under any proxy as long as `/api` is
  routed to the same origin the page was served from.
- `liveSocketUrl()` (`frontend/src/api/client.ts`) builds
  `` `${protocol}//${window.location.host}/api/live` ``, where `protocol` is `wss:` when
  `window.location.protocol === "https:"` and `ws:` otherwise.

So the *frontend's* scheme choice comes from the browser, not from a header — a page served over
`https://radio.example.com` will always try `wss://`, and there is nothing to misconfigure there.
`X-Forwarded-Proto` and `X-Forwarded-Host` matter for the **API**: the session cookie's `Secure`
flag, the same-origin/CSRF checks and any absolute link the API writes are derived from them. Set
both on the outer hop. The inner `nginx.conf` deliberately passes an incoming value through rather
than overwriting it (see its `map $http_x_forwarded_proto` block), so the outer proxy's values win —
which is what you want, because the outer hop is the one that saw the real request.

---

## 2. nginx, complete

Drop this in `/etc/nginx/conf.d/radio.example.com.conf` on the public host. The `map` block must
be at `http` level; if the file is included from `http` (the usual `conf.d/*.conf`), it is already.

```nginx
# `Connection: upgrade` — but only for a request that actually asked to upgrade.
# Sending it unconditionally breaks keep-alive for the ordinary JSON calls.
map $http_upgrade $keyup_connection_upgrade {
    default upgrade;
    ""      close;
}

upstream keyup {
    server 192.0.2.10:8095;   # the Keyup box and its WEB_PORT
    keepalive 32;
}

server {
    listen 80;
    listen [::]:80;
    server_name radio.example.com;
    # ACME challenges, if the certificate is renewed over http-01.
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name radio.example.com;

    ssl_certificate     /etc/letsencrypt/live/radio.example.com/fullchain.pem;  # TLS placeholder
    ssl_certificate_key /etc/letsencrypt/live/radio.example.com/privkey.pem;    # TLS placeholder
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SSL:10m;

    # Recorder uploads and streamed CSV/NDJSON exports are unbounded. A limit here
    # answers 413 before the request ever reaches Keyup.
    client_max_body_size 0;

    # The list endpoints take up to 1000 repeated `id=` query parameters (~11 bytes
    # each); the default 8 kB request line would answer 414 at roughly 700 of them.
    large_client_header_buffers 4 32k;

    # Applied to every location below. `proxy_http_version 1.1` is required for
    # WebSockets *and* for upstream keep-alive.
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;   # https — the API's cookie/CSRF checks read this
    proxy_set_header X-Forwarded-Host  $host;

    # ---- WebSocket: the live feed. It must never time out on a quiet night; a
    # channel can legitimately say nothing for an hour and the API sends its own
    # keepalives. This block has to come before the generic /api/ one.
    location = /api/live {
        proxy_pass http://keyup;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $keyup_connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }

    # ---- WebSocket: the recorder push channel. The API heartbeats every 15 s, so
    # 300 s is twenty missed heartbeats before nginx would cut a live socket.
    location = /api/recorder/stream {
        proxy_pass http://keyup;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection $keyup_connection_upgrade;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }

    # ---- Server-sent events. No upgrade, but buffering must be off or the stream
    # is delivered as one response at the end of time.
    location = /api/live/sse            { include /etc/nginx/snippets/keyup-sse.conf; }
    location = /api/activity/stream     { include /etc/nginx/snippets/keyup-sse.conf; }
    location = /api/admin/recorders/stream { include /etc/nginx/snippets/keyup-sse.conf; }
    location = /api/admin/recorders/spectrum/stream { include /etc/nginx/snippets/keyup-sse.conf; }

    # ---- Everything else on the API: uploads, streamed exports, ranged audio.
    location /api/ {
        proxy_pass http://keyup;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        client_max_body_size 0;
    }

    # ---- The SPA and its assets. The inner nginx does the caching headers and the
    # SPA fallback; this hop only forwards.
    location / {
        proxy_pass http://keyup;
        proxy_read_timeout 60s;
    }
}
```

`/etc/nginx/snippets/keyup-sse.conf`:

```nginx
proxy_pass http://keyup;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;
proxy_cache off;
add_header X-Accel-Buffering no;
```

If you would rather not use a snippet file, inline those six lines into each of the four SSE
`location` blocks — they are identical.

Reload and check:

```bash
sudo nginx -t && sudo systemctl reload nginx
# 101 Switching Protocols is the whole test:
curl -is -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  https://radio.example.com/api/live
# SSE should dribble out, not arrive all at once at the end:
curl -N https://radio.example.com/api/live/sse
```

---

## 3. Caddy equivalent

Caddy proxies WebSockets and SSE correctly with no configuration — it speaks HTTP/1.1 upstream,
forwards `Upgrade`, and does not buffer. Only the timeouts and the body limit need saying:

```caddyfile
radio.example.com {
	encode zstd gzip

	# Uploads and streamed exports are unbounded.
	request_body {
		max_size 0
	}

	reverse_proxy 192.0.2.10:8095 {
		# Streaming routes must not be flushed on a buffer boundary.
		flush_interval -1
		transport http {
			read_timeout  3600s
			write_timeout 3600s
		}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host  {host}
	}
}
```

`flush_interval -1` is the one line people miss: without it Caddy will buffer an SSE response and
the activity and recorder streams look dead.

---

## 4. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Header badge reads **Offline**, calls list is fine | `/api/live` upgrade not forwarded — the outer proxy is missing `proxy_http_version 1.1` and/or the `Upgrade`/`Connection` headers |
| Live page pill reads **Reconnecting** and never settles | Same: the WebSocket handshake is being answered as plain HTTP, so the socket closes without ever opening |
| Badge tooltip says the feed fell back to server-sent events | The app has already worked around it (see §5), but the proxy is still wrong — fix the upgrade rules |
| Live feed connects, then drops every 60 s | The outer proxy's `proxy_read_timeout` is at its default; a quiet channel looks idle. Raise to 3600 s |
| Recorders page shows stale tuner state, admin edits do not appear | `/api/admin/recorders/stream` is buffered or timing out on the outer hop |
| A recorder reports "connected" in its own log but Keyup shows it offline | `/api/recorder/stream` — same upgrade problem, 300 s timeout |
| Activity log does not tail | `/api/activity/stream` is buffered — `proxy_buffering off` missing |
| Spectrum waterfall stays blank | `/api/admin/recorders/spectrum/stream` is buffered |
| Recorder uploads fail with **413** | `client_max_body_size` on the outer proxy; set it to `0` |
| A bulk selection answers **414 Request-URI Too Large** | `large_client_header_buffers 4 32k` missing on the outer proxy |
| Signed in, but every mutation answers **403** | `X-Forwarded-Proto`/`X-Forwarded-Host` not set, so the API's same-origin check sees the wrong origin |
| The SPA loads but every API call 404s | `/api/` is not routed to the Keyup host at all |
| On a **phone**, the feed stops as soon as a call is played | Not a proxy fault. Playing a call is what makes somebody put the phone down; iOS keeps the audio going while it suspends the page, and the socket dies with the page. See §6 |
| On a phone over plain **http**, the feed stalls while audio is loading | HTTP/1.1 allows six connections per host and a playing `<audio>` holds one for the whole call. Serve the site over HTTP/2 (`http2 on`, and TLS) so streams multiplex — see §6 |

---

## 5. What the app does about it

The frontend does not simply sit on a broken socket:

- `useLiveFeed` (`frontend/src/api/live.ts`) reconnects with exponential backoff — 1 s, 2 s, 4 s …
  capped at 15 s — after any close.
- A socket that closes **without ever having opened** is counted as a handshake failure. Two of
  those inside 30 s is taken as proof that something in front of the app does not do WebSockets,
  and the feed switches to `GET /api/live/sse` for the rest of the session. The decision is
  module-level, so changing a filter does not walk back into the socket and start over.
- Once it has fallen back, the header's connection badge says so in its tooltip
  (`frontend/src/components/layout/StatusBadges.tsx`).
- The badge's tooltip also carries a short journal of the last few transport transitions —
  "websocket open", "websocket handshake refused", "woke from a suspended page". On a phone
  there is no console to open, and this is the difference between "it broke" and "the socket
  died while the screen was off".

The fallback keeps the site usable through a misconfigured proxy; it is not a reason to leave one
misconfigured. SSE re-sends the filter as a query string and cannot change it without reopening
the connection, and it holds an HTTP response open rather than a socket.

Note also that the header badge reflects the **player's live toggle**, not the socket: with the
toggle off it reads "Offline" whatever the proxy is doing. The live page's own pill is the one that
reports the real connection state.

---

## 6. Phones: suspension and the connection budget

Two things happen on a phone that never happen on a desktop, and both look like a broken proxy.

### The page is suspended while the audio keeps playing

Starting a call is what makes somebody lock the screen or switch apps. iOS goes on playing the
audio and suspends everything else: the WebSocket is closed by the system, and the `setTimeout`
that was going to reconnect is frozen along with it. The page comes back to a dead feed and no
timer pending, so it never reconnects — which reads, exactly as it was reported, as "the live
view breaks as soon as you play anything".

`useLiveFeed` handles this itself and needs nothing from the proxy:

- `visibilitychange`, `pageshow` (including a bfcache restore), `pagehide`, `freeze` and `online`
  are all watched. Coming back reconnects **immediately**, skipping whatever backoff was pending,
  and leaves a socket that genuinely survived alone.
- A handshake that failed while the page was hidden is **not** counted toward the two-strikes SSE
  fallback. A phone with its radio off is not a proxy eating an upgrade, and counting it would
  latch the app onto SSE for the rest of the session.
- A stream replays nothing, so the pages refetch the window they missed: the calls table refetches
  its page, the live page pulls the recent archive back, and both fold the result in without
  moving anything the reader was looking at.

### The connection budget, on HTTP/1.1 only

A browser opens at most **six** connections to one host over HTTP/1.1, and a `<audio>` element
streaming a call holds one of them for the whole call — as does an `EventSource` on the SSE
fallback, for as long as it is connected. Add the calls page's own queries (the page, the facet
counts, the hidden-row count, the header's overview poll) and six is not much headroom; a stream
that has to reconnect can find itself queued behind an audio request that will not finish until
the call does.

`http2 on` in the server block above is what makes this a non-issue: every request becomes a
stream on one connection and the limit does not apply. It only bites where the site is reached
over plain http — a phone on the LAN hitting the box's `http://` address directly, or an outer proxy
without `http2 on`. If that is the deployment, serve it over TLS with HTTP/2 (the `edge`
container will do it without an outer proxy at all: see [HTTPS.md](HTTPS.md)).

Note that WebSockets are exempt either way: browsers pool them separately from ordinary HTTP,
and nginx does not implement RFC 8441, so a WebSocket always takes its own HTTP/1.1 connection
even when the page is on HTTP/2.

---

## Cloudflare Tunnel (or any tunnel connector)

Point the tunnel's public hostname at the box's `WEB_PORT` (the `edge` container), the same
target an outer proxy would use. Set `EDGE_TRUSTED_PROXIES` to the connector's address. Caddy then takes the visitor from
`Cf-Connecting-Ip` for requests from that address only, and passes nginx a single address, so
`TRUSTED_PROXY_HOPS=2` stays right on both the tunnel and the LAN path. Without it, every visitor
shares the connector's address for rate limits and login lockouts.

Recorders on the LAN can keep posting to the box's own `http://` address directly; they never
need to pass through the tunnel. (`Cf-Connecting-Ip` is Cloudflare's header; another tunnel
that names the visitor some other way is trusted for nothing until the edge is taught that
header, and every visitor then shares the connector's address.)
