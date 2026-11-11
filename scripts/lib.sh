# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Helpers shared by scripts/bootstrap.sh and scripts/update.sh.  Sourced, never run.
#
# Everything runs from the checkout root with plain `docker compose`, so the usual compose
# environment applies: COMPOSE_PROJECT_NAME, COMPOSE_FILE and COMPOSE_PROFILES from the shell
# or from .env, and shell variables override .env for ${...} interpolation in compose.yaml.

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }

compose() { docker compose "$@"; }

# compose_has --services|--profiles NAME: does the compose configuration define NAME?  The list
# is captured first: `... | grep -q` would close the pipe early, and under pipefail the
# SIGPIPE'd compose turns a match into a failure.
compose_has() {
  local list
  list=$(compose config "$1" 2>/dev/null) || return 1
  grep -qx -- "$2" <<<"$list"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker is not installed (https://docs.docker.com/engine/install/)"
  docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is missing ('docker compose version' fails)"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon: is it running, and is this user in the docker group?"
}

# env_get KEY [DEFAULT]: the value compose would interpolate, i.e. the shell's own variable
# first, then the last KEY= line of .env, then DEFAULT.  Surrounding quotes are dropped.
env_get() {
  local key=$1 def=${2-} val
  if [[ -n ${!key+x} ]]; then
    printf '%s' "${!key}"
    return
  fi
  if [[ -f .env ]]; then
    val=$(sed -n "s/^${key}=//p" .env | tail -n 1)
    val=${val%\"}; val=${val#\"}; val=${val%\'}; val=${val#\'}
    if [[ -n $val ]]; then
      printf '%s' "$val"
      return
    fi
  fi
  printf '%s' "$def"
}

# wait_healthy [SECONDS]: poll /api/healthz from inside the api container, which needs no
# published port, no curl on the host, and answers the same question the compose healthcheck
# and the worker's depends_on ask.
wait_healthy() {
  local limit=${1:-300} waited=0
  say "waiting for the API (/api/healthz, up to ${limit}s)"
  until compose exec -T api python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/healthz', timeout=5)" \
      >/dev/null 2>&1; do
    if (( waited >= limit )); then
      warn "the API did not answer /api/healthz within ${limit}s"
      compose ps >&2 || true
      compose logs --tail 40 migrate api >&2 || true
      return 1
    fi
    sleep 3
    waited=$(( waited + 3 ))
  done
  note "API healthy"
}

# keyup_json PYTHON_EXPR -- CLI ARGS...: run `keyup CLI ARGS` in the api container and print
# PYTHON_EXPR evaluated over its JSON output (bound to `d`).  The JSON is parsed inside the
# container, so the host needs neither python nor jq.
keyup_json() {
  local expr=$1; shift
  [[ ${1-} == -- ]] && shift
  # shellcheck disable=SC2016  # $@ and the python are for the container's shell, not this one
  compose exec -T api sh -c '"$@" | python -c "import json,sys; d=json.load(sys.stdin); print($0)"' \
    "$expr" keyup "$@"
}
