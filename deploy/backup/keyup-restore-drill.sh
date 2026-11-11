#!/usr/bin/env bash
# keyup-restore-drill.sh — prove the newest backup restores (docs/OPERATIONS.md §7).
#
#   keyup-restore-drill.sh [DUMP]      default: the newest dump in $KEYUP_BACKUP_DIR with a manifest
#
# 1. checks the dump's sha256 against its manifest;
# 2. refuses unless the filesystem keeps $KEYUP_BACKUP_MIN_FREE_GB free *after* a restore
#    the size of the source database;
# 3. restores it into a throwaway database on the same server, keyup_drill_<stamp>, with
#    pg_restore --exit-on-error in a nice 19 container, its backends reniced;
# 4. counts every table in the restore and compares each with the count the backup took in
#    its own snapshot (the manifest), and the alembic head with the manifest's;
# 5. drops the scratch database — always, on success, failure or Ctrl-C;
# 6. writes <dump>.drill.json beside the dump and exits 0 only when every count matched.
#
# The live database is only read (to look up sizes); nothing is written to it.
#
# Environment (all optional): everything common.sh reads, plus KEYUP_DRILL_JOBS (2) —
# pg_restore's parallel jobs — and KEYUP_DRILL_SECTIONS: blank for a full restore
# (schema, data, indexes, constraints), or "pre-data,data" to skip the post-data section
# (indexes, keys, triggers) when disk or time is short; the counts still prove the data.
set -euo pipefail

if ! declare -F kb_psql >/dev/null; then
  # shellcheck source=common.sh
  source "$(dirname "$(readlink -f "$0")")/common.sh"
fi

JOBS=${KEYUP_DRILL_JOBS:-2}
SECTIONS=${KEYUP_DRILL_SECTIONS:-}
APP=keyup-drill

mkdir -p "$KEYUP_BACKUP_DIR"
exec 9>"$KEYUP_BACKUP_DIR/.lock"
flock -n 9 || kb_die "a backup or another drill holds $KEYUP_BACKUP_DIR/.lock"

dump=${1:-}
if [[ -z $dump ]]; then
  manifest=$(ls -1 "$KEYUP_BACKUP_DIR"/keyup-*.json 2>/dev/null | grep -v '\.drill\.json$' | sort | tail -1 || true)
  [[ -n $manifest ]] || kb_die "no backup manifest in $KEYUP_BACKUP_DIR — run keyup-db-backup.sh first"
  dump=${manifest%.json}.dump
else
  dump=$(readlink -f "$dump")
  manifest=${dump%.dump}.json
fi
[[ -f $dump && -f $manifest ]] || kb_die "need both $dump and $manifest"
[[ $(dirname "$dump") == "$(readlink -f "$KEYUP_BACKUP_DIR")" ]] || kb_die "the dump must be in $KEYUP_BACKUP_DIR"
name=$(basename "$dump")

started_epoch=$(date +%s)
kb_log "drill: $name ($(( $(stat -c %s "$dump") / 1024**2 )) MiB)"

want_sha=$(jq -r .sha256 "$manifest")
have_sha=$(nice -n 19 sha256sum "$dump" | cut -d' ' -f1)
[[ $want_sha == "$have_sha" ]] || kb_die "sha256 mismatch: manifest $want_sha, file $have_sha"
sha_s=$(( $(date +%s) - started_epoch ))

source_bytes=$(jq -r .database_bytes "$manifest")
free=$(kb_free_bytes "$KEYUP_BACKUP_DIR")
floor=$(( KEYUP_BACKUP_MIN_FREE_GB * 1024 * 1024 * 1024 ))
if (( free - source_bytes < floor )); then
  kb_die "refusing: $((free / 1024**3)) GiB free, a restore needs ~$((source_bytes / 1024**3)) GiB and $KEYUP_BACKUP_MIN_FREE_GB GiB must stay free (KEYUP_DRILL_SECTIONS=pre-data,data needs less)"
fi

IMAGE=$(docker inspect -f '{{.Config.Image}}' "$KEYUP_DB_CONTAINER") || kb_die "no container $KEYUP_DB_CONTAINER"
SCRATCH="keyup_drill_$(date -u +%Y%m%d%H%M%S)"
renicer=""
cleanup() {
  [[ -n $renicer ]] && kill "$renicer" 2>/dev/null || true
  if [[ -n ${SCRATCH:-} ]]; then
    kb_log "drill: dropping $SCRATCH"
    kb_psql postgres "DROP DATABASE IF EXISTS $SCRATCH WITH (FORCE)" || kb_log "WARNING: could not drop $SCRATCH — drop it by hand"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

kb_psql postgres "CREATE DATABASE $SCRATCH TEMPLATE template0"
kb_log "drill: restoring into $SCRATCH (jobs $JOBS${SECTIONS:+, sections $SECTIONS})"

section_args=()
if [[ -n $SECTIONS ]]; then
  IFS=, read -ra parts <<<"$SECTIONS"
  for part in "${parts[@]}"; do section_args+=("--section=$part"); done
fi

kb_renice_loop "$APP" 9>&- &
renicer=$!
restore_started=$(date +%s)
docker run --rm --user "$(id -u):$(id -g)" --network "container:$KEYUP_DB_CONTAINER" \
  -e PGAPPNAME="$APP" -v "$KEYUP_BACKUP_DIR:/backups:ro" --entrypoint nice "$IMAGE" -n 19 \
  pg_restore -h 127.0.0.1 -U "$KEYUP_DB_USER" -d "$SCRATCH" \
  --jobs="$JOBS" --no-owner --no-privileges --exit-on-error "${section_args[@]}" \
  "/backups/$name"
restore_s=$(( $(date +%s) - restore_started ))
kill "$renicer" 2>/dev/null || true
renicer=""
kb_log "drill: restored in ${restore_s}s"

restored_bytes=$(kb_psql "$SCRATCH" "SELECT pg_database_size(current_database())")
restored_alembic=$(kb_psql "$SCRATCH" "SELECT string_agg(version_num, ',') FROM alembic_version")
count_started=$(date +%s)
restored=$(docker exec -i -e PGAPPNAME="$APP" "$KEYUP_DB_CONTAINER" \
  psql -U "$KEYUP_DB_USER" -d "$SCRATCH" -AtqX -v ON_ERROR_STOP=1 <<<"$KB_COUNT_SQL" | kb_counts_json)
count_s=$(( $(date +%s) - count_started ))

# Every table in either side, with both counts; a table missing from one side is a mismatch.
report=$(jq -n --argjson want "$(jq .counts "$manifest")" --argjson have "$restored" '
  [($want + $have) | keys[] as $t | {table: $t, backup: $want[$t], restored: $have[$t],
                                     ok: ($want[$t] == $have[$t])}]')
mismatches=$(jq '[.[] | select(.ok | not)] | length' <<<"$report")
tables=$(jq 'length' <<<"$report")
rows=$(jq '[.[].restored // 0] | add' <<<"$report")
want_alembic=$(jq -r .alembic "$manifest")
alembic_ok=false; [[ $want_alembic == "$restored_alembic" ]] && alembic_ok=true
ok=false; (( mismatches == 0 )) && $alembic_ok && ok=true
total_s=$(( $(date +%s) - started_epoch ))

jq -n --arg dump "$name" --arg scratch "$SCRATCH" --arg sections "${SECTIONS:-all}" \
  --arg finished_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg alembic "$restored_alembic" \
  --argjson ok "$ok" --argjson alembic_ok "$alembic_ok" --argjson jobs "$JOBS" \
  --argjson tables "$tables" --argjson rows "$rows" --argjson mismatches "$mismatches" \
  --argjson sha256_seconds "$sha_s" --argjson restore_seconds "$restore_s" \
  --argjson count_seconds "$count_s" --argjson total_seconds "$total_s" \
  --argjson restored_bytes "$restored_bytes" --argjson source_bytes "$source_bytes" \
  --argjson report "$report" \
  '{ok: $ok, dump: $dump, scratch_database: $scratch, sections: $sections, jobs: $jobs,
    finished_at: $finished_at, alembic: $alembic, alembic_ok: $alembic_ok, tables: $tables,
    rows: $rows, mismatches: $mismatches, source_bytes: $source_bytes,
    restored_bytes: $restored_bytes, sha256_seconds: $sha256_seconds,
    restore_seconds: $restore_seconds, count_seconds: $count_seconds,
    total_seconds: $total_seconds, tables_detail: $report}' >"${dump%.dump}.drill.json"
chmod 600 "${dump%.dump}.drill.json"

jq -r '.[] | select(.ok | not) | "MISMATCH \(.table): backup \(.backup) restored \(.restored)"' <<<"$report" >&2
kb_log "drill: $tables tables, $rows rows, $mismatches mismatches, alembic $restored_alembic ($alembic_ok); restore ${restore_s}s, total ${total_s}s, restored size $((restored_bytes / 1024**2)) MiB"
$ok || kb_die "drill FAILED — see ${dump%.dump}.drill.json"
kb_log "drill: OK"
