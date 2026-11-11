#!/bin/sh
# Pick the edge's configuration from the environment and run it.
#
# The Caddyfile has no conditionals, so the choice is made here: TLS on when
# KEYUP_TLS_DOMAINS names at least one hostname *and* the ACME contact address is present
# (and, for DNS-01, the Cloudflare token), plain HTTP otherwise.  Falling back rather than
# refusing to start is deliberate — a half-filled .env should leave the archive reachable
# on http, not take the stack down.
#
# How the certificate is obtained is KEYUP_TLS_CHALLENGE (default auto):
#   dns   — ACME DNS-01 through Cloudflare (CLOUDFLARE_API_TOKEN); the names may resolve to
#           a LAN address and no inbound port is needed.  Caddyfile.tls*.
#   http  — Caddy's default HTTP-01 / TLS-ALPN-01; the names must resolve publicly to this
#           box with inbound 80 and 443 (WEB_PORT=80, WEB_TLS_PORT=443).  Caddyfile.tls-http01*.
#   auto  — dns when CLOUDFLARE_API_TOKEN is set, http otherwise.  backend/app/edge/render.py
#           (tls_challenge) resolves it the same way.
#
# With TLS on there are two files per challenge, chosen by KEYUP_TLS_REDIRECT_HTTP (default true):
#   true  — :80 answers /health, proxies /api/*, and 308s browsers to https (the original
#           behaviour, and the one docs/HTTPS.md's "Recorders" table describes);
#   false — :80 serves the whole site in the clear as well, beside https.
#
# NOTE: these files are GENERATED from backend/app/edge/render.py, which is also what
# the API renders when it pushes a configuration into this container over the admin API.
# One template, so a Caddyfile started from the environment and one pushed from the Admin
# page cannot drift.  Regenerate with `cd backend && .venv/bin/python -m app.edge`.
set -eu

DOMAINS=$(printf '%s' "${KEYUP_TLS_DOMAINS:-}" | tr -s ' \t\n' ' ' | sed 's/^ //;s/ $//')

# Empty-or-unset default.  Caddy's own {$VAR:default} syntax does not help here: env_file
# lines like `ACME_CA=` set the variable to the empty string, which is "found", so the
# placeholder would expand to nothing and `acme_ca` would take an empty argument.
: "${ACME_CA:=https://acme-v02.api.letsencrypt.org/directory}"
: "${EDGE_TRUSTED_PROXIES:=127.0.0.1/32}"
export ACME_CA KEYUP_TLS_DOMAINS EDGE_TRUSTED_PROXIES

# Same truthiness the API's settings use, so the two agree on what "false" means.
# Same resolution as the API's tls_challenge(): anything but dns/http means auto.
CHALLENGE=$(printf '%s' "${KEYUP_TLS_CHALLENGE:-auto}" | tr 'A-Z' 'a-z' | tr -d ' \t\n')
case "$CHALLENGE" in
	dns | http) ;;
	*) if [ -n "${CLOUDFLARE_API_TOKEN:-}" ]; then CHALLENGE=dns; else CHALLENGE=http; fi ;;
esac

REDIRECT=1
case "$(printf '%s' "${KEYUP_TLS_REDIRECT_HTTP:-true}" | tr 'A-Z' 'a-z')" in
	0 | false | no | off) REDIRECT=0 ;;
esac

CONFIG=/etc/caddy/Caddyfile.plain
if [ -n "$DOMAINS" ]; then
	if [ "$CHALLENGE" = dns ] && [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
		echo "edge: KEYUP_TLS_DOMAINS is set but CLOUDFLARE_API_TOKEN is empty - serving plain HTTP" >&2
	elif [ -z "${ACME_EMAIL:-}" ]; then
		echo "edge: KEYUP_TLS_DOMAINS is set but ACME_EMAIL is empty - serving plain HTTP" >&2
	else
		# DNS-01 keeps the original file names; HTTP-01 is the -http01 pair.
		VARIANT=tls
		CHALLENGE_NOTE=""
		if [ "$CHALLENGE" = http ]; then
			VARIANT=tls-http01
			CHALLENGE_NOTE=", HTTP-01"
		fi
		if [ "$REDIRECT" -eq 1 ]; then
			CONFIG=/etc/caddy/Caddyfile.$VARIANT
			REDIRECT_NOTE="http redirects to https"
		else
			CONFIG=/etc/caddy/Caddyfile.$VARIANT-noredirect
			REDIRECT_NOTE="http also served in the clear"
		fi
		KEYUP_TLS_DOMAINS=$DOMAINS
		export KEYUP_TLS_DOMAINS
		echo "edge: TLS on for [$DOMAINS] via $ACME_CA$CHALLENGE_NOTE ($REDIRECT_NOTE)" >&2
	fi
else
	echo "edge: KEYUP_TLS_DOMAINS is empty - serving plain HTTP on :80" >&2
fi

# Fail loudly on a broken config instead of letting `caddy run` half-start.
caddy validate --config "$CONFIG" --adapter caddyfile

exec caddy run --config "$CONFIG" --adapter caddyfile
