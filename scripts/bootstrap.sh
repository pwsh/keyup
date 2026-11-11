#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# First run of a Keyup install: configuration, containers, the first admin account and a
# recorder upload token.  Safe to run again: every step checks before it acts, so a second run
# changes nothing and says so.
#
#   scripts/bootstrap.sh                       # asks for the admin name and password
#   scripts/bootstrap.sh --non-interactive --admin admin --admin-password 'correct horse'
#
# What it does, in order:
#   1. .env       created from .env.example if it does not exist, with a fresh SECRET_KEY and
#                 POSTGRES_PASSWORD, TRUSTED_PROXY_HOPS=2 (edge -> web -> api), TZ from this host
#                 and PUBLIC_URL/PUBLIC_BASE_URL.  An existing .env is never modified unless
#                 --force-env is given (the old one is kept as .env.bak.<timestamp>).
#   2. build      docker compose build, unless the stack is already running (a re-run must not
#                 rebuild and recreate it: that is scripts/update.sh's job; --build forces it).
#   3. dirs       the host directories compose binds into the containers, created owned by the
#                 container's user (uid 10001) when they do not exist yet.  Existing ones are left
#                 alone apart from AUDIO_ROOT/uploads, where pushed calls are written.
#   4. up         docker compose up -d (the migrate service runs first), then waits for
#                 /api/healthz.
#   5. STT        the CPU transcription profile, when this checkout has one.
#   6. admin      keyup user create <name> --admin, unless an admin account already exists.
#   7. token      keyup token create <label> --scopes upload, unless an active token with that
#                 label already exists; prints the [uplink] block for trunkd.toml.
#
# Compose's own variables apply as usual: COMPOSE_PROJECT_NAME, COMPOSE_PROFILES, and shell
# variables (WEB_PORT=80 ...) override .env.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap.sh [options]

  --admin NAME            admin account to create (default: prompt; "admin" when non-interactive)
  --admin-password PW     its password (default: prompt; generated and printed once when
                          non-interactive).  KEYUP_ADMIN_PASSWORD in the environment also works
                          and keeps the password out of the process list.
  --token-label LABEL     label of the recorder upload token (default: trunkd)
  --public-url URL        the address recorders and browsers use for this server
                          (default: http://<this host's first address>:<WEB_PORT>)
  --tz ZONE               timezone written to a new .env (default: this host's)
  --build                 build the images even when the stack is already running
  --no-build              never build (use the images already built)
  --no-stt                do not enable the CPU transcription profile in a new .env
  --force-env             replace an existing .env (the old one is kept as .env.bak.<timestamp>)
  --non-interactive, -y   never prompt
  -h, --help              this text
EOF
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

ADMIN=""
ADMIN_PASSWORD=${KEYUP_ADMIN_PASSWORD:-}
TOKEN_LABEL=trunkd
PUBLIC_URL_ARG=""
TZ_ARG=""
BUILD=auto
STT=1
FORCE_ENV=0
INTERACTIVE=1
[[ -t 0 ]] || INTERACTIVE=0

while (( $# )); do
  case $1 in
    --admin) ADMIN=${2:?--admin needs a name}; shift ;;
    --admin=*) ADMIN=${1#*=} ;;
    --admin-password) ADMIN_PASSWORD=${2:?--admin-password needs a value}; shift ;;
    --admin-password=*) ADMIN_PASSWORD=${1#*=} ;;
    --token-label) TOKEN_LABEL=${2:?--token-label needs a value}; shift ;;
    --token-label=*) TOKEN_LABEL=${1#*=} ;;
    --public-url) PUBLIC_URL_ARG=${2:?--public-url needs a URL}; shift ;;
    --public-url=*) PUBLIC_URL_ARG=${1#*=} ;;
    --tz) TZ_ARG=${2:?--tz needs a zone}; shift ;;
    --tz=*) TZ_ARG=${1#*=} ;;
    --build) BUILD=1 ;;
    --no-build) BUILD=0 ;;
    --no-stt) STT=0 ;;
    --force-env) FORCE_ENV=1 ;;
    --non-interactive|-y) INTERACTIVE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

[[ $TOKEN_LABEL =~ ^[A-Za-z0-9._-]+$ ]] || die "--token-label: letters, digits, '.', '_' and '-' only"
require_docker

# --------------------------------------------------------------------------------------
# 1. .env
# --------------------------------------------------------------------------------------

random_hex() {  # random_hex BYTES
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$1" /dev/urandom | od -An -vtx1 | tr -d ' \n'
  fi
}

host_tz() {
  local tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  fi
  if [[ -z $tz && -f /etc/timezone ]]; then
    tz=$(head -n 1 /etc/timezone)
  fi
  if [[ -z $tz && -L /etc/localtime ]]; then
    tz=$(readlink /etc/localtime | sed -n 's|.*/zoneinfo/||p')
  fi
  printf '%s' "${tz:-UTC}"
}

host_address() {
  local ip=""
  if command -v hostname >/dev/null 2>&1; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  fi
  printf '%s' "${ip:-localhost}"
}

# env_set KEY VALUE: replace every KEY= line of .env (or append one).  awk, not sed, so a value
# containing / & or | needs no escaping.
env_set() {
  local tmp
  tmp=$(mktemp .env.XXXXXX)
  awk -v k="$1" -v v="$2" '
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' .env >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" .env
}

db_volume_exists() {
  local project
  project=$(compose config 2>/dev/null | sed -n 's/^name: //p' | head -n 1)
  [[ -n $project ]] && [[ -n $(docker volume ls -q \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.volume=dbdata") ]]
}

write_env() {
  local old_pg=""
  if [[ -f .env ]]; then
    old_pg=$(env_get POSTGRES_PASSWORD "")
    local bak
    bak=.env.bak.$(date +%Y%m%d%H%M%S)
    cp -p .env "$bak"
    note "kept the old configuration as $bak"
  fi
  [[ -f .env.example ]] || die ".env.example is missing: run this from a complete checkout"
  ( umask 077 && cp .env.example .env )

  env_set SECRET_KEY "$(random_hex 32)"
  if [[ -n $old_pg ]] && db_volume_exists; then
    # The database volume was initialised with the old password and Postgres only reads
    # POSTGRES_PASSWORD on an empty volume: a new one would lock every service out.
    env_set POSTGRES_PASSWORD "$old_pg"
    note "kept POSTGRES_PASSWORD: the database volume already exists and was created with it"
  else
    env_set POSTGRES_PASSWORD "$(random_hex 24)"
  fi
  env_set TRUSTED_PROXY_HOPS 2
  env_set TZ "${TZ_ARG:-$(host_tz)}"
  local url=${PUBLIC_URL_ARG:-http://$(host_address):$(env_get WEB_PORT 8095)}
  url=${url%/}
  env_set PUBLIC_URL "$url"
  env_set PUBLIC_BASE_URL "$url"

  # The CPU transcription profile.  .env.example turns it on (COMPOSE_PROFILES=stt-cpu); a
  # checkout whose .env.example leaves the profiles blank gets it here.  --no-stt takes it out.
  if (( STT )); then
    if [[ -z $(sed -n 's/^COMPOSE_PROFILES=//p' .env) ]] && compose_has --profiles stt-cpu; then
      env_set COMPOSE_PROFILES stt-cpu
    fi
  elif [[ $(sed -n 's/^COMPOSE_PROFILES=//p' .env) == stt-cpu ]]; then
    env_set COMPOSE_PROFILES ""
  fi

  note "wrote .env (secrets generated; TZ=$(env_get TZ), PUBLIC_URL=$url)"
}

say "configuration"
if [[ ! -f .env ]]; then
  write_env
elif (( FORCE_ENV )); then
  write_env
else
  note ".env exists: left unchanged (--force-env replaces it)"
  if [[ $(env_get SECRET_KEY "") == change-me-in-production || -z $(env_get SECRET_KEY "") ]]; then
    warn "SECRET_KEY in .env is still the placeholder: set it to a long random value (openssl rand -hex 32)"
  fi
  if [[ $(env_get POSTGRES_PASSWORD keyup) == keyup ]]; then
    warn "POSTGRES_PASSWORD in .env is the default 'keyup'"
  fi
fi

# --------------------------------------------------------------------------------------
# 2. build
# --------------------------------------------------------------------------------------

if [[ $BUILD == auto ]]; then
  BUILD=1
  if [[ -n $(compose ps -q --status running api 2>/dev/null) ]]; then
    BUILD=0
    say "build"
    note "the stack is already running: not rebuilding (scripts/update.sh does that; --build forces it)"
  fi
fi
if (( BUILD )); then
  say "building the images (the first build takes several minutes)"
  compose build
fi

# --------------------------------------------------------------------------------------
# 3. host directories
# --------------------------------------------------------------------------------------
# A bind-mount source that does not exist is created by Docker as root:root 755, and the
# containers run as uid 10001, so the API could then not store a single pushed call.  Create
# the missing ones here, owned by that uid, from a throwaway root container of the backend
# image (no sudo needed: access to Docker is root on this host already).  A directory that
# already exists — an sdrtrunk recording tree, say — keeps its owner; only the uploads/
# directory inside AUDIO_ROOT is created if missing.

APP_UID=10001

abs_path() {  # relative to the checkout, as compose resolves bind sources
  case $1 in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$PWD" "${1#./}" ;;
  esac
}

prepare_dir() {  # prepare_dir HOSTPATH [SUBDIR]
  local dir sub=${2-} script
  dir=$(abs_path "$1")
  if [[ -e $dir ]]; then
    [[ -z $sub || -e $dir/$sub ]] && return 0
    script="mkdir -p /d/$sub && chown $APP_UID:$APP_UID /d/$sub"
  else
    script="chown $APP_UID:$APP_UID /d${sub:+ && mkdir -p /d/$sub && chown $APP_UID:$APP_UID /d/$sub}"
  fi
  compose run --rm --no-deps -T --user 0:0 --entrypoint sh -v "$dir:/d" migrate -c "$script" >/dev/null
  note "prepared $dir${sub:+/$sub}"
}

say "data directories"
prepare_dir "$(env_get AUDIO_ROOT ./audio)" uploads
prepare_dir "$(env_get EXPORT_DIR ./exports)"
prepare_dir "$(env_get WAVEFORM_CACHE_DIR ./cache/waveform)"
prepare_dir "$(env_get EVENT_LOG_ROOT ./eventlogs)"
prepare_dir "$(env_get BACKUP_DIR /srv/keyup/backups/db)"

# --------------------------------------------------------------------------------------
# 4. up
# --------------------------------------------------------------------------------------

say "starting the stack"
compose up -d --remove-orphans
wait_healthy 300

# --------------------------------------------------------------------------------------
# 5. speech-to-text
# --------------------------------------------------------------------------------------
# The `stt-cpu` compose profile (a Speaches container on the CPU, compose.yaml).  When it is
# active, the first start downloads the model, so its health is reported, not awaited; a
# checkout without it only explains where transcription is configured.

say "speech-to-text"
if compose_has --services stt-cpu; then
  if [[ -n $(compose ps -q stt-cpu 2>/dev/null) ]]; then
    note "stt-cpu is running; its first start downloads the model, so transcripts begin a few minutes later"
  else
    note "stt-cpu is defined but not started: add stt-cpu to COMPOSE_PROFILES in .env and run 'docker compose up -d'"
  fi
elif compose_has --profiles stt-cpu; then
  note "the CPU profile is available but not enabled: COMPOSE_PROFILES=stt-cpu in .env, then 'docker compose up -d'"
else
  note "no CPU transcription profile in this checkout: add a backend in Admin -> Backends (docs/STT.md)"
fi
note "STT_BACKENDS in .env only seeds the backends table on the first start; after that Admin -> Backends wins"

# --------------------------------------------------------------------------------------
# 6. admin account
# --------------------------------------------------------------------------------------

say "admin account"
existing_admin=$(keyup_json "' '.join(u['username'] for u in d if 'admin' in u['scopes'] and u['is_active'])" -- user list)
if [[ -n $existing_admin ]]; then
  note "an admin account already exists ($existing_admin): skipped"
else
  if [[ -z $ADMIN ]]; then
    if (( INTERACTIVE )); then
      read -r -p "    admin username [admin]: " ADMIN
    fi
    ADMIN=${ADMIN:-admin}
  fi
  if [[ -n $ADMIN_PASSWORD ]]; then
    # Over stdin (the CLI's password prompt, answered twice), never as an argument, so the
    # password is not in any process list.
    printf '%s\n%s\n' "$ADMIN_PASSWORD" "$ADMIN_PASSWORD" \
      | compose exec -T api keyup user create "$ADMIN" --admin >/dev/null 2>&1 \
      || die "keyup user create $ADMIN failed"
    note "created admin '$ADMIN'"
  elif (( INTERACTIVE )); then
    compose exec api keyup user create "$ADMIN" --admin
    note "created admin '$ADMIN'"
  else
    ADMIN_PASSWORD=$(random_hex 12)
    printf '%s\n%s\n' "$ADMIN_PASSWORD" "$ADMIN_PASSWORD" \
      | compose exec -T api keyup user create "$ADMIN" --admin >/dev/null 2>&1 \
      || die "keyup user create $ADMIN failed"
    note "created admin '$ADMIN' with the generated password: $ADMIN_PASSWORD"
    note "(shown once; change it under your account menu or with 'docker compose exec api keyup user passwd $ADMIN')"
  fi
fi

# --------------------------------------------------------------------------------------
# 7. recorder token and the trunkd [uplink] block
# --------------------------------------------------------------------------------------

say "recorder upload token"
PUBLIC=$(env_get PUBLIC_URL "http://localhost:$(env_get WEB_PORT 8095)")
PUBLIC=${PUBLIC%/}
existing_token=$(keyup_json \
  "' '.join(t['prefix'] for t in d if t['label'] == '$TOKEN_LABEL' and not t['revoked_at'] and 'upload' in t['scopes'])" \
  -- token list)
if [[ -n $existing_token ]]; then
  TOKEN="<the token printed when it was created>"
  note "an active upload token labelled '$TOKEN_LABEL' already exists (prefix $existing_token): not minting another"
  note "a lost token cannot be shown again: revoke it (keyup token list / revoke) and run this again"
else
  TOKEN=$(keyup_json "d['token']" -- token create "$TOKEN_LABEL" --scopes upload)
  [[ $TOKEN == kup_* ]] || die "keyup token create did not return a token"
  note "minted '$TOKEN_LABEL' (shown once below; only its hash is stored)"
fi

cat <<EOF

Paste this into trunkd.toml on the recorder (system_label must match the system name your
talkgroups are filed under in Keyup):

[uplink]
url          = "$PUBLIC/api/call-upload"
api_key      = "$TOKEN"
system_label = "Example System"
site_label   = "Site 1"

EOF

say "Keyup is up: $PUBLIC  (API docs: $PUBLIC/api/docs)"
if [[ $(env_get AUTH_REQUIRE_READ false) != true ]]; then
  warn "AUTH_REQUIRE_READ=false: anyone who can reach $PUBLIC can read and play every call."
  warn "Set AUTH_REQUIRE_READ=true in .env (then 'docker compose up -d') before exposing it."
fi
