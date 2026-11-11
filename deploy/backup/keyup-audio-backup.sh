#!/usr/bin/env bash
# keyup-audio-backup.sh — copy the recordings somewhere else (docs/OPERATIONS.md §7).
#
#   KEYUP_AUDIO_BACKUP_TARGET=user@host:/path  keyup-audio-backup.sh [--dry-run]
#
# Deliberately inert until an operator names a target: there is no default, and a copy on
# the same physical disk as the archive is not a backup (a VM's or container's root, the
# hypervisor's root and its storage pool can all sit on one drive).
#
# What is copied: the **originals** under $KEYUP_AUDIO_ROOT (default /srv/keyup/audio).
# Skipped: the `.proc.mp3` processed copies (regenerable from the originals by the audio
# worker — 45 GB of the 116 GB on 2026-09-23), the waveform cache, and the upload temp
# directory `uploads/.incoming`.  Additive: nothing is ever deleted at the target, so a
# retention purge or an accident here does not propagate; a file whose bytes changed is
# replaced (`--backup` keeps the previous copy under the target's `.replaced/<date>/`).
#
# rsync is incremental, so after the first run a night costs the day's new recordings
# (~80 MB/day of originals on 2026-09-23) plus the walk of ~4.6 M files.
#
# Environment: KEYUP_AUDIO_BACKUP_TARGET (required), KEYUP_AUDIO_ROOT,
# KEYUP_AUDIO_BACKUP_BWLIMIT (KiB/s, default 0 = unlimited), KEYUP_AUDIO_BACKUP_SSH
# (the -e command, default "ssh -o BatchMode=yes").
set -euo pipefail

ROOT=${KEYUP_AUDIO_ROOT:-/srv/keyup/audio}
TARGET=${KEYUP_AUDIO_BACKUP_TARGET:-}
BWLIMIT=${KEYUP_AUDIO_BACKUP_BWLIMIT:-0}
SSH_CMD=${KEYUP_AUDIO_BACKUP_SSH:-ssh -o BatchMode=yes}
LOCK=${KEYUP_AUDIO_BACKUP_LOCK:-/run/lock/keyup-audio-backup.lock}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

[[ -n $TARGET ]] || { log "KEYUP_AUDIO_BACKUP_TARGET is not set — nothing to do (see docs/OPERATIONS.md §7)"; exit 2; }
[[ -d $ROOT ]] || { log "no audio root at $ROOT"; exit 1; }
case $TARGET in
  "$ROOT"|"$ROOT"/*) log "the target is inside the audio root — that is not a backup"; exit 1 ;;
esac

exec 9>"$LOCK"
flock -n 9 || { log "another audio backup is running"; exit 1; }

dry=()
[[ ${1:-} == --dry-run ]] && dry=(--dry-run --itemize-changes)

started=$(date +%s)
log "audio backup: $ROOT/ -> $TARGET/"
nice -n 19 ionice -c 3 rsync -a --partial --numeric-ids --stats \
  --backup --backup-dir=".replaced/$(date -u +%Y%m%d)" \
  --exclude='*.proc.mp3' --exclude='/uploads/.incoming/' --exclude='.replaced/' \
  --bwlimit="$BWLIMIT" -e "$SSH_CMD" "${dry[@]}" \
  "$ROOT/" "$TARGET/"
log "audio backup: done in $(( $(date +%s) - started ))s"
