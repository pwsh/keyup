#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Update a Keyup install in place: pull, build, migrate, restart, wait for health.
#
#   scripts/update.sh              # git pull --ff-only, then the rest
#   scripts/update.sh --no-pull    # deploy whatever is checked out now
#
# Each step can fail on its own before anything live is touched:
#   1. pull     fast-forward only, so local commits or a diverged branch stop here.
#   2. build    the new images, while the old containers keep running.
#   3. migrate  `alembic upgrade head` in a one-off container from the new image, while the
#               old API keeps accepting uploads.  A failure stops here with the old containers
#               still serving.
#   4. up       recreate on the already-built images, then wait for /api/healthz.
# .env is never touched.  Back up the database before a release whose notes mention a
# migration you cannot undo (docs/OPERATIONS.md).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/update.sh [--no-pull] [--no-build]

  --no-pull    skip git pull (deploy the checked-out tree as it is)
  --no-build   skip docker compose build (use the images already built)
  -h, --help   this text
EOF
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=scripts/lib.sh
. scripts/lib.sh

PULL=1
BUILD=1
while (( $# )); do
  case $1 in
    --no-pull) PULL=0 ;;
    --no-build) BUILD=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

require_docker
[[ -f .env ]] || die "no .env here: run scripts/bootstrap.sh for a first install"

if (( PULL )); then
  say "git pull --ff-only"
  git pull --ff-only || die "git pull failed: nothing was rebuilt or restarted"
fi
note "at $(git rev-parse --short HEAD 2>/dev/null || echo 'an unversioned tree')"

if (( BUILD )); then
  say "building the images"
  compose build || die "build failed: nothing was restarted; the previous version is still running"
fi

say "migrating the database"
compose run --rm -T migrate || die "migration failed: nothing was restarted; the previous version is still running"

say "restarting"
compose up -d --remove-orphans || die "docker compose up failed: check 'docker compose ps' and 'docker compose logs'"
wait_healthy 300
compose ps --format 'table {{.Service}}\t{{.Status}}'
say "updated"
