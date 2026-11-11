#!/usr/bin/env bash
# keyup-db-backup.sh — one consistent pg_dump of the Keyup database, a manifest that can
# prove a restore, and rotation (docs/OPERATIONS.md §7).
#
# Runs on the Docker host, as root, from the systemd timer beside this file or by hand.
# Writes into $KEYUP_BACKUP_DIR (default /srv/keyup/backups/db — outside the checkout, so
# replacing or re-cloning the checkout never takes the backups with it):
#
#   keyup-<UTC stamp>.dump   pg_dump custom format, zstd-compressed, restorable in parallel
#   keyup-<UTC stamp>.json   the manifest: sha256, sizes, timings, alembic head, and the
#                            exact row count of every table *in the dump's own snapshot*
#
# The manifest is written last and is the success marker: a dump without one is a failed
# run, and the worker's backup_stale health check (app/health.py) looks only at manifests.
#
# Consistency: a psql session opens a REPEATABLE READ transaction and exports its snapshot;
# the row counts are taken in that session and pg_dump is started on the same snapshot, so
# the counts are exactly what the dump holds — which is what keyup-restore-drill.sh compares
# a restore against.  The source is never written to.
#
# Load: pg_dump (and its compression) runs under nice 19 in a throwaway container from the
# db image; the server backend serving it is reniced too (common.sh), and the whole run
# holds a flock so two can never overlap.
#
# Environment (all optional): KEYUP_BACKUP_DIR, KEYUP_DB_CONTAINER, KEYUP_DB_USER,
# KEYUP_DB_NAME, KEYUP_BACKUP_MIN_FREE_GB (40), KEYUP_BACKUP_KEEP_DAILY (3),
# KEYUP_BACKUP_KEEP_WEEKLY (2), KEYUP_BACKUP_COMPRESS (zstd:3).
set -euo pipefail

if ! declare -F kb_psql >/dev/null; then
  # shellcheck source=common.sh
  source "$(dirname "$(readlink -f "$0")")/common.sh"
fi

KEEP_DAILY=${KEYUP_BACKUP_KEEP_DAILY:-3}
KEEP_WEEKLY=${KEYUP_BACKUP_KEEP_WEEKLY:-2}
COMPRESS=${KEYUP_BACKUP_COMPRESS:-zstd:3}
APP=keyup-backup

mkdir -p "$KEYUP_BACKUP_DIR"
# The worker (uid/gid 10001) lists the directory for its backup_stale health check through a
# read-only mount; the dumps and manifests themselves stay 0600 root.
chgrp "$KEYUP_BACKUP_GROUP" "$KEYUP_BACKUP_DIR" 2>/dev/null || true
chmod 750 "$KEYUP_BACKUP_DIR"
exec 9>"$KEYUP_BACKUP_DIR/.lock"
flock -n 9 || kb_die "another backup or drill holds $KEYUP_BACKUP_DIR/.lock"

# Leftovers of a run that died (the manifest is the success marker; a .partial never is).
find "$KEYUP_BACKUP_DIR" -maxdepth 1 -name 'keyup-*.partial' -delete

IMAGE=$(docker inspect -f '{{.Config.Image}}' "$KEYUP_DB_CONTAINER") || kb_die "no container $KEYUP_DB_CONTAINER"
DB_BYTES=$(kb_psql "$KEYUP_DB_NAME" "SELECT pg_database_size(current_database())")
ALEMBIC=$(kb_psql "$KEYUP_DB_NAME" "SELECT string_agg(version_num, ',') FROM alembic_version")

# Space: the previous dump's size is the estimate (plus a quarter); with no previous dump,
# a third of the database, which is generous for a zstd custom-format dump without indexes.
last=$(ls -1 "$KEYUP_BACKUP_DIR"/keyup-*.dump 2>/dev/null | sort | tail -1 || true)
if [[ -n $last ]]; then estimate=$(( $(stat -c %s "$last") * 5 / 4 )); else estimate=$(( DB_BYTES / 3 )); fi
free=$(kb_free_bytes "$KEYUP_BACKUP_DIR")
floor=$(( KEYUP_BACKUP_MIN_FREE_GB * 1024 * 1024 * 1024 ))
if (( free - estimate < floor )); then
  kb_die "refusing: $((free / 1024**3)) GiB free, dump needs ~$((estimate / 1024**3)) GiB and $KEYUP_BACKUP_MIN_FREE_GB GiB must stay free"
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE="keyup-$STAMP"
PARTIAL="$KEYUP_BACKUP_DIR/$BASE.partial"
started_epoch=$(date +%s)
kb_log "backup $BASE: database $((DB_BYTES / 1024**2)) MiB, alembic $ALEMBIC, $((free / 1024**3)) GiB free"

# --- the snapshot session -----------------------------------------------------------------
coproc SNAP { exec 9>&-; docker exec -i -e PGAPPNAME="$APP" "$KEYUP_DB_CONTAINER" \
  psql -U "$KEYUP_DB_USER" -d "$KEYUP_DB_NAME" -AtqX -v ON_ERROR_STOP=1 2>&1; }
SNAP_PID_=$SNAP_PID
renicer=""
cleanup() {
  [[ -n $renicer ]] && kill "$renicer" 2>/dev/null || true
  if [[ -n ${SNAP_PID_:-} ]] && kill -0 "$SNAP_PID_" 2>/dev/null; then
    printf 'ROLLBACK;\n\\q\n' >&"${SNAP[1]}" 2>/dev/null || true
    wait "$SNAP_PID_" 2>/dev/null || true
  fi
  rm -f "$PARTIAL"
}
trap cleanup EXIT

printf 'BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;\nSELECT pg_export_snapshot();\n' >&"${SNAP[1]}"
IFS= read -r -t 60 snapshot <&"${SNAP[0]}" || kb_die "no snapshot from psql"
[[ $snapshot =~ ^[0-9A-F-]+$ ]] || kb_die "unexpected snapshot answer: $snapshot"

printf '%s\n\\echo __KEYUP_END__\n' "$KB_COUNT_SQL" >&"${SNAP[1]}"
counts_raw=""
while IFS= read -r -t 600 line <&"${SNAP[0]}"; do
  [[ $line == __KEYUP_END__ ]] && break
  [[ $line == *"|"* ]] || kb_die "counting failed: $line"
  counts_raw+="$line"$'\n'
done
COUNTS=$(kb_counts_json <<<"$counts_raw")
tables=$(jq 'length' <<<"$COUNTS")
(( tables > 0 )) || kb_die "no tables counted"
kb_log "snapshot $snapshot: $tables tables counted (calls: $(jq '.calls // 0' <<<"$COUNTS"))"

# --- the dump -----------------------------------------------------------------------------
kb_renice_loop "$APP" 9>&- &
renicer=$!
dump_started=$(date +%s)
docker run --rm --user "$(id -u):$(id -g)" --network "container:$KEYUP_DB_CONTAINER" \
  -e PGAPPNAME="$APP" -v "$KEYUP_BACKUP_DIR:/backups" --entrypoint nice "$IMAGE" -n 19 \
  pg_dump -h 127.0.0.1 -U "$KEYUP_DB_USER" -d "$KEYUP_DB_NAME" \
  --format=custom --compress="$COMPRESS" --snapshot="$snapshot" \
  --file="/backups/$BASE.partial"
dump_s=$(( $(date +%s) - dump_started ))
kill "$renicer" 2>/dev/null || true
renicer=""
printf 'COMMIT;\n\\q\n' >&"${SNAP[1]}"
wait "$SNAP_PID_" 2>/dev/null || true
SNAP_PID_=""

# A dump pg_restore cannot list is not a backup.
toc=$(docker run --rm --user "$(id -u):$(id -g)" -v "$KEYUP_BACKUP_DIR:/backups:ro" --entrypoint pg_restore "$IMAGE" \
  --list "/backups/$BASE.partial" | grep -c ' TABLE DATA ' || true)
(( toc > 0 )) || kb_die "pg_restore --list found no table data in the dump"

mv "$PARTIAL" "$KEYUP_BACKUP_DIR/$BASE.dump"
chmod 600 "$KEYUP_BACKUP_DIR/$BASE.dump"
size=$(stat -c %s "$KEYUP_BACKUP_DIR/$BASE.dump")
sha=$(nice -n 19 sha256sum "$KEYUP_BACKUP_DIR/$BASE.dump" | cut -d' ' -f1)
total_s=$(( $(date +%s) - started_epoch ))

jq -n \
  --arg file "$BASE.dump" --arg sha256 "$sha" --arg snapshot "$snapshot" --arg alembic "$ALEMBIC" \
  --arg image "$IMAGE" --arg compress "$COMPRESS" --arg database "$KEYUP_DB_NAME" \
  --arg started_at "$(date -u -d "@$started_epoch" +%Y-%m-%dT%H:%M:%SZ)" \
  --arg finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson bytes "$size" --argjson database_bytes "$DB_BYTES" \
  --argjson dump_seconds "$dump_s" --argjson total_seconds "$total_s" \
  --argjson toc_tables "$toc" --argjson counts "$COUNTS" \
  '{file: $file, sha256: $sha256, bytes: $bytes, database: $database, database_bytes: $database_bytes,
    alembic: $alembic, image: $image, compress: $compress, snapshot: $snapshot,
    started_at: $started_at, finished_at: $finished_at, dump_seconds: $dump_seconds,
    total_seconds: $total_seconds, toc_tables: $toc_tables, counts: $counts}' \
  >"$KEYUP_BACKUP_DIR/$BASE.json.tmp"
mv "$KEYUP_BACKUP_DIR/$BASE.json.tmp" "$KEYUP_BACKUP_DIR/$BASE.json"
chmod 600 "$KEYUP_BACKUP_DIR/$BASE.json"
kb_log "backup $BASE: $((size / 1024**2)) MiB in ${dump_s}s (run ${total_s}s), sha256 ${sha:0:16}…"

# --- rotation -----------------------------------------------------------------------------
# Newest KEEP_DAILY complete dumps, then the newest dump of each of the next KEEP_WEEKLY older ISO
# weeks.  Only dumps with a manifest count; a dump never outlives its manifest or vice versa.
declare -A weeks=()
kept=0; weekly=0
for manifest in $(ls -1 "$KEYUP_BACKUP_DIR"/keyup-*.json 2>/dev/null | grep -v '\.drill\.json$' | sort -r); do
  base=${manifest%.json}
  stamp=${base##*/keyup-}
  week=$(date -u -d "${stamp:0:8}" +%G-%V)
  # a week a daily already covers does not use up a weekly slot
  if (( kept < KEEP_DAILY )); then kept=$((kept + 1)); weeks[$week]=1; continue; fi
  if [[ -z ${weeks[$week]:-} ]] && (( weekly < KEEP_WEEKLY )); then
    weeks[$week]=1; weekly=$((weekly + 1)); continue
  fi
  kb_log "rotate: removing ${base##*/}"
  rm -f "$base.dump" "$base.json" "$base.drill.json"
done
# A dump whose manifest is gone (or never came) and is not the one just written.
for dump in "$KEYUP_BACKUP_DIR"/keyup-*.dump; do
  [[ -e $dump && ! -e ${dump%.dump}.json ]] && { kb_log "rotate: removing orphan ${dump##*/}"; rm -f "$dump"; }
done
exit 0
