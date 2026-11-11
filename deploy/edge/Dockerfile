# Caddy 2 with the Cloudflare DNS provider compiled in.
#
# Stock `caddy:2` cannot solve the ACME DNS-01 challenge: that challenge is answered by
# writing a TXT record, and the code that knows how to talk to a DNS provider ships as a
# separate module.  So the binary is built here with xcaddy and copied into the ordinary
# runtime image, which already carries the entrypoint conventions, the CA bundle and the
# /data + /config layout.
#
# Versions are pinned on purpose: an unpinned `caddy:2` would silently change the ACME
# client under a working deployment.  2.11.7 is the current stable tag on Docker Hub
# (tag list checked 2026-10-16).  Both the Caddy version and the DNS plugin are named
# again on the xcaddy line: without `@version` xcaddy resolves the plugin's *latest*
# release at build time, so an unrelated rebuild could swap the code that answers the
# DNS-01 challenge.  v0.2.4 (which pulls libdns/cloudflare v0.2.2, libdns v1.1.1) is what
# the production edge was running on 2026-09-23 (`caddy build-info` in keyup-edge-1).
# Change them together, deliberately, and check `caddy build-info` after the build.
FROM caddy:2.11.7-builder AS build

RUN xcaddy build v2.11.7 \
      --with github.com/caddy-dns/cloudflare@v0.2.4

FROM caddy:2.11.7

COPY --from=build /usr/bin/caddy /usr/bin/caddy

# Five complete configurations, one per mode (plain; DNS-01 and HTTP-01, each redirecting
# or not); entrypoint.sh picks between them from the environment.  The Caddyfile has no
# `if`, and a config that references a DNS token is not the same file as one that never
# mentions TLS at all.  All five are GENERATED from
# backend/app/edge/render.py — the same template the API renders when it pushes a new
# configuration into the running container over the admin API.
COPY Caddyfile.plain          /etc/caddy/Caddyfile.plain
COPY Caddyfile.tls            /etc/caddy/Caddyfile.tls
COPY Caddyfile.tls-noredirect /etc/caddy/Caddyfile.tls-noredirect
COPY Caddyfile.tls-http01            /etc/caddy/Caddyfile.tls-http01
COPY Caddyfile.tls-http01-noredirect /etc/caddy/Caddyfile.tls-http01-noredirect
COPY entrypoint.sh            /usr/local/bin/keyup-edge-entrypoint

RUN chmod +x /usr/local/bin/keyup-edge-entrypoint

EXPOSE 80 443
# The base image's CMD names /etc/caddy/Caddyfile, a file that does not exist here.  The
# entrypoint ignores arguments and chooses the config itself, but leaving an inherited CMD
# in place is a trap for anyone who later adds an `exec "$@"` to it.
CMD []
ENTRYPOINT ["/usr/local/bin/keyup-edge-entrypoint"]
