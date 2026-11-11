# Shared helpers for the Keyup backup scripts (docs/OPERATIONS.md §7).  Sourced, never
# run.  Everything talks to Postgres through the running db container, so the host needs
# nothing but docker, jq, flock and coreutils.

KEYUP_BACKUP_DIR=${KEYUP_BACKUP_DIR:-/srv/keyup/backups/db}
KEYUP_DB_CONTAINER=${KEYUP_DB_CONTAINER:-keyup-db-1}
KEYUP_DB_USER=${KEYUP_DB_USER:-keyup}
KEYUP_DB_NAME=${KEYUP_DB_NAME:-keyup}
# The floor under every step that writes: a dump or a scratch restore that would leave less
# than this free is refused before it starts.  40 GB keeps the filesystem clear of the disk
# alert's 30 GB warning (app/disk_watch.py), so a backup never *causes* the alert.
KEYUP_BACKUP_MIN_FREE_GB=${KEYUP_BACKUP_MIN_FREE_GB:-40}
# The group that may list the backup directory: the app's uid/gid in the backend image, so
# the worker's backup_stale check can see the manifests' timestamps (never their contents).
KEYUP_BACKUP_GROUP=${KEYUP_BACKUP_GROUP:-10001}

kb_log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
kb_die() { kb_log "FATAL: $*"; exit 1; }

# kb_psql DB SQL — one statement, unaligned, tuples only.
kb_psql() {
  docker exec -e PGAPPNAME=keyup-backup-ctl "$KEYUP_DB_CONTAINER" \
    psql -U "$KEYUP_DB_USER" -d "$1" -AtqX -v ON_ERROR_STOP=1 -c "$2"
}

# The SQL that counts every table in `public`, one "table|rows" line each.  Generated, so a
# table added by a migration is covered without touching this file.  Run through \gexec.
KB_COUNT_SQL="SELECT string_agg(format('SELECT %L, count(*) FROM public.%I', tablename, tablename), ' UNION ALL ' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public' \\gexec"

# kb_free_bytes PATH — bytes available to an unprivileged writer on PATH's filesystem.
kb_free_bytes() { df --output=avail -B1 "$1" | tail -1 | tr -d ' '; }

# kb_renice_backends APPNAME — push every backend (and its parallel workers) serving
# APPNAME to nice 19 / best-effort I/O 7.  `nice` on `docker exec` only reaches the client;
# the backend doing the COPY or the CREATE INDEX is a child of the postmaster and has to be
# found by its pid inside the container's namespace.
kb_renice_backends() {
  local app=$1 pm ids hp ns
  pm=$(docker inspect -f '{{.State.Pid}}' "$KEYUP_DB_CONTAINER" 2>/dev/null) || return 0
  ids=$(kb_psql postgres "SELECT pid FROM pg_stat_activity WHERE application_name = '$app'
          UNION SELECT pid FROM pg_stat_activity WHERE leader_pid IN
            (SELECT pid FROM pg_stat_activity WHERE application_name = '$app')" 2>/dev/null) || return 0
  [[ -n $ids ]] || return 0
  for hp in $(pgrep -P "$pm"); do
    ns=$(awk '/^NSpid:/{print $NF}' "/proc/$hp/status" 2>/dev/null) || continue
    if grep -qx "$ns" <<<"$ids"; then
      renice -n 19 -p "$hp" >/dev/null 2>&1 || true
      ionice -c 2 -n 7 -p "$hp" >/dev/null 2>&1 || true
    fi
  done
}

# kb_renice_loop APPNAME — kb_renice_backends every 5 s until killed (parallel restore jobs
# and index-build workers appear and go throughout a run).
kb_renice_loop() { while sleep 5; do kb_renice_backends "$1"; done; }

# kb_counts_json < "table|rows" lines — {"table": rows, ...}
kb_counts_json() { jq -Rn '[inputs | select(length > 0) | split("|") | {(.[0]): (.[1] | tonumber)}] | add // {}'; }
