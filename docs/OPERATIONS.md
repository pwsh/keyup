# Operating the database

Everything here is about the Postgres instance behind the archive: how it is tuned, the
per-table autovacuum the archive needs and the cluster defaults do not give it, the indexes
that were removed and the four that look removable but are not, and the handful of
operator commands that are deliberately *not* in a migration.

Measurements are from one long-running archive on **2026-09-15** — Postgres 18.6 in Docker,
20 GB RAM, 6 cores, SSD, a 16 GB database. Numbers age; the reasoning is what to keep.

---

## 1. Where the tuning lives

Every Postgres setting the archive depends on is a `-c` flag on the `db` service's
`command:` in `compose.yaml` — not `ALTER SYSTEM SET`. `ALTER SYSTEM` writes
`postgresql.auto.conf` **inside the data volume**, where the tuning is invisible in the
repository, absent from any clone or test database, and lost with the volume; and nobody
reading `compose.yaml` can tell the database is tuned at all (`docker inspect keyup-db-1`
shows `Cmd ["postgres"]` with no flags). Command-line `-c` flags outrank
`postgresql.auto.conf`, so a database that was once tuned with `ALTER SYSTEM` is taken over
without clearing the auto.conf first; the stale copy in the volume is then dead weight.

### Memory: the `PG_*` knobs

The memory settings depend on the host, so they come from `.env`; the defaults suit a host
with about 4 GB of RAM that also runs the rest of the stack and the `stt-cpu` speech
container. `.env.example` has the same table with a line per host size:

| `.env` key | Postgres setting | default | sizing |
|---|---|---|---|
| `PG_SHARED_BUFFERS` | `shared_buffers` | 512MB | ~25% of the RAM Postgres can count on |
| `PG_EFFECTIVE_CACHE_SIZE` | `effective_cache_size` | 2GB | ~60% of it (a planner hint, allocates nothing) |
| `PG_WORK_MEM` | `work_mem` | 16MB | per sort or hash, per query node — multiplies by every concurrent query |
| `PG_MAINTENANCE_WORK_MEM` | `maintenance_work_mem` | 128MB | index builds, `VACUUM`, restores |
| `PG_MAX_WAL_SIZE` | `max_wal_size` | 1GB | checkpoint spacing under bulk writes |
| `PG_SHM_SIZE` | the container's `shm_size` (`/dev/shm`) | 256m | see below |

| host RAM | SHARED_BUFFERS | EFFECTIVE_CACHE_SIZE | WORK_MEM | MAINTENANCE_WORK_MEM | MAX_WAL_SIZE | SHM_SIZE |
|---|---|---|---|---|---|---|
| 4 GB | 512MB | 2GB | 16MB | 128MB | 1GB | 256m |
| 8 GB | 1GB | 4GB | 32MB | 256MB | 1GB | 512m |
| 16 GB | 3GB | 9GB | 32MB | 512MB | 2GB | 1g |
| 32 GB | 8GB | 20GB | 64MB | 1GB | 4GB | 2g |

The archive the measurements here come from (20 GB RAM, a 16 GB database) runs
`shared_buffers` 4GB, `effective_cache_size` 12GB, `maintenance_work_mem` 512MB,
`max_wal_size` 2GB, `work_mem` 64MB and a 1g `/dev/shm`. `work_mem` went from 32 MB to 64 MB
there after 27 GB of temp files had spilled since the last stats reset — a sort or hash that
did not fit. This workload runs a handful of concurrent queries, not hundreds, so a larger
`work_mem` is affordable; `log_temp_files` (below) tells you whether you need it.

**`/dev/shm` is not optional sizing.** Parallel query workers hand each other results (and
parallel hash joins keep their tables) in POSIX shared memory under `/dev/shm`, and Docker's
default is 64 MB. The transcription stats query wanted 16 MB segments × 3 workers and failed
with `could not resize shared memory segment … No space left on device`. It is tmpfs, so a
bigger one costs nothing until it is used — but it is not unlimited either: 1 GB was
exhausted again by ~30 simultaneous full-text searches, each with its own parallel workers,
which is why searches now queue for a few slots and run with one parallel worker each
(`backend/app/api/search_load.py`). If that error shows up in the Postgres log
(`docker compose logs db`), raise `PG_SHM_SIZE`.

### Fixed settings

These do not depend on the host and are written straight into `compose.yaml`:

| setting | value | why |
|---|---|---|
| `random_page_cost` | **1.1** (default 4) | The archive expects SSD. 4 is a spinning-disk number, and it is what makes the planner prefer a sequential scan over an index it should have used — on the measured archive, the likeliest single cause of an 83% cache hit rate. On spinning disks, put 4 back. |
| `effective_io_concurrency` | **200** (default 16) | Same reason: an SSD will take far more concurrent prefetches on a bitmap heap scan than 16. |
| `log_temp_files` | **10MB** (default off) | Names the queries still spilling to temp files, so the next tuning pass has evidence instead of a total. |
| `shared_preload_libraries` | **`pg_stat_statements`** | `pg_stat_user_indexes` can say whether an index was scanned; only this can say *which queries* are slow or spilling. `pg_stat_statements.track=all` counts statements inside functions too. |

> **Changing a `PG_*` value or `command:` recreates the db container** on the next
> `docker compose up -d`. A few seconds of database outage —
> the container stops and a new one starts on the same volume. Recorders spool and retry,
> so nothing is lost, but **do it when the transcription queue is low**, because in-flight
> transcription and export jobs will fail their current attempt and back off.
>
> `shared_preload_libraries` in particular *cannot* be set any other way: it is read at
> postmaster start, so it needs the restart regardless.

The extension itself is created by migration `c4e91f3b57a8`, which is written to tolerate
the library not being preloaded yet — a dev box, a test database, or an existing database
before the container is recreated all migrate fine and simply get a `NOTICE`. After the recreate:

```bash
docker exec keyup-db-1 psql -U keyup -d keyup -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements'
```

…or just let the next `alembic upgrade head` do it. Then the query that was missing:

```bash
docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT calls, mean_exec_time::int, temp_blks_written, left(query,90)
     FROM pg_stat_statements ORDER BY temp_blks_written DESC LIMIT 20"
```

---

## 2. Autovacuum (migration `a3f7c21e9b40`)

The cluster runs the shipped global defaults — `autovacuum_vacuum_scale_factor = 0.2`,
`autovacuum_analyze_scale_factor = 0.1` — and no table had per-table `reloptions`.

Twenty percent is a reasonable number for a ten-thousand-row table and a bad one for
`calls`: it means Postgres waits for **615 000** dead tuples before vacuuming. The measured
state was 593 K dead against 3.07 M live — that is, parked just under the trigger,
permanently, which is where a 0.2 scale factor leaves any steadily-updated table. Dead space
is never reclaimed, index scans walk it, and statistics drift a tenth of the table out of
date between analyses.

| table | live rows | dead | dead % |
|---|---|---|---|
| `calls` | 3.08 M | 593 K | 19% |
| `transcription_jobs` | 1.59 M | 3.7 K | — |
| `audio_jobs` | 1.58 M | 98 K | 6% |
| `call_exports` | 454 K | 4.4 K | 1% |

Eight tables now carry `autovacuum_vacuum_scale_factor = 0.02` and
`autovacuum_analyze_scale_factor = 0.01` plus a flat floor sized to the table. For `calls`
that is a vacuum every ~71 K dead rows instead of every 615 K. The ~200 other tables are
untouched — this is not a cluster-wide change.

`webhook_deliveries` gets much smaller floors (1 000 / 500) because it is not shaped like
the others: 59 K live rows, but every row is inserted, delivered and deleted seven days
later. A 0.2 scale factor on 59 K rows means 11 800 dead tuples, which that churn rate
outruns permanently — which is how it reached 453 MB of heap for 37 MB of live payload.

To check the settings took, and to watch the effect:

```bash
docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT relname, reloptions FROM pg_class WHERE reloptions IS NOT NULL ORDER BY relname"

docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
     FROM pg_stat_user_tables WHERE n_dead_tup > 10000 ORDER BY n_dead_tup DESC"
```

The migration's `downgrade()` uses `ALTER TABLE … RESET (…)` rather than writing 0.2/0.1
back in. That is the honest inverse: a table with no reloptions follows `postgresql.conf`,
so if the cluster is later retuned these tables follow it too.

---

## 3. The one-off: `VACUUM FULL webhook_deliveries`

**`webhook_deliveries` holds 59 K rows and 453 MB of heap for 37 MB of live payload** — an
11× bloat, and the largest single piece of recoverable space in the database.

This is **not** a retention problem, and it is worth being clear about that because the
obvious fix is the wrong one. Every row in the table is `status = 'delivered'`, spanning
2026-09-07 to 2026-09-15 — eight days against `DELIVERED_KEEP_DAYS = 7`. The nightly prune
in `app/webhooks.py` is running and is doing exactly its job; 59 K rows *is* the bounded
steady state. The 453 MB is space that autovacuum reclaimed for reuse but never returned to
the filesystem, because a plain `VACUUM` does not shrink a heap.

Nothing in `app/webhooks.py` needs changing, and no new retention setting is wanted here.
The existing comment on `DELIVERED_KEEP_DAYS` argues deliberately that the keep window
should not be a knob — "a knob whose only effect is how much of a receipt log survives is a
knob nobody would tune correctly" — and the measurement supports that rather than
undermining it.

So this is a one-time reclaim, plus the per-table autovacuum above to stop it coming back:

```bash
docker exec keyup-db-1 psql -U keyup -d keyup -c 'VACUUM FULL webhook_deliveries'
```

**Expect seconds** — it rewrites 37 MB of live data, not 453 MB. It takes an **ACCESS
EXCLUSIVE** lock for its duration, so every read and write of that table blocks; webhook
deliveries queue behind it and the admin delivery list stalls. Nothing else in the archive
touches the table. It also needs free disk space for a second copy of the *live* data (tens
of MB), not of the bloated heap.

It rebuilds the table's indexes as it goes, which incidentally fixes
`ix_webhook_deliveries_created` — 22 MB for 59 K rows, the same bloat by another name.

`VACUUM FULL` is deliberately **not** in a migration. Migrations run unattended on every
`docker compose up` (the one-shot `migrate` service, which the api and worker wait for); an
exclusive lock and a table rewrite belong to an operator who chose the moment, not
to a deploy.

---

## 4. Indexes (migration `b8d4e60a17c2`)

`pg_stat_user_indexes` listed eleven non-unique indexes with fewer than fifty scans between
them over two weeks. **Seven were dropped, four were kept.** "Unused" is not on its own a
reason to drop an index — it can be unused because the feature is rare, or because *the pass
that needs it has not run yet* — so each was matched against the queries in `backend/app`
and checked with `EXPLAIN` before being touched.

### Dropped

| index | size | why |
|---|---|---|
| `ix_calls_started_local` | 197 MB | `started_local` is a presentation column. Every reference is a read or a write of the value; nothing filters or orders by it. `started_at` is the clock the archive queries. |
| `ix_calls_ingest_priority` | 42 MB | Partial on `IS NOT NULL`; the only WHERE-clause use in the tree is the backfill's `IS NULL` — the rows it excludes. |
| `ix_radio_events_instance_at` | 36 MB | `instance` is never a leading filter (3 distinct values over 822 K rows), and EXPLAIN showed it producing the *worse* plan — scanning `instance` with the selective `radio_pk` demoted to a heap filter. |
| `ix_activity_events_source` | 36 MB | 8 distinct values, of which `upload` and `transcribe` are 97% of the table. |
| `ix_activity_events_level` | 36 MB | **Replaced**, see below. |
| `ix_transcripts_primary` | 33 MB | Partial `(call_id) WHERE is_primary`; `ix_transcripts_call_id` already covers the leading term and a call has 1–3 transcripts. |
| `ix_radio_affiliations_last_heard` | 17 MB | The directory orders by `coalesce(last_affiliated_at, last_heard_at)` — an expression a plain b-tree cannot serve. EXPLAIN confirms a seq scan, correct at 30 K rows. |

`ix_activity_events_level` came back **narrowed**, as `ix_activity_events_level_at` on
`(level, id DESC) WHERE level <> 'info'`: 99.1% of the table is `info`, and the rare levels
are the only ones an operator filters to (31 155 `warn`, 2 130 `error`). A few hundred KB
instead of 36 MB, and strictly better — carrying `id DESC` means it serves the activity
list's `ORDER BY id DESC LIMIT n` as well as the filter, so the sort node disappears.
Postgres proves `level = 'error'` implies `level <> 'info'` and uses it.

### Kept, despite the scan count

- **`ix_ingest_files_call_id` (89 MB, 0 scans)** — the trap. `ingest_files.call_id` is a
  foreign key to `calls.id` with `ON DELETE SET NULL`, and Postgres does not index the
  referencing side of a foreign key for you. It reads zero scans because **retention has
  never deleted a call** — both keep windows ship at 0. The moment an operator sets one,
  this index is the difference between a batch of 500 deletions and 500 sequential scans of
  a 3.08 M row, 1.19 GB table.
- **`ix_radio_events_at` (32 MB)** — `purge_radio_events` is `DELETE … WHERE at < cutoff` on
  a 90-day window, and the oldest row is younger than that. Not used *yet*, not unneeded.
- **`ix_calls_tags` (13 MB)** — `app/api/filters.py` builds `calls.tags && ARRAY[…]`, which
  is exactly and only what GIN answers; without it that filter seq-scans a 9.4 GB table.
- **`ix_webhook_deliveries_created` (22 MB)** — the nightly prune's exact shape. Its size is
  bloat, fixed by §3, not a reason to drop it.

Drops use `DROP INDEX CONCURRENTLY` inside an `autocommit_block`. A plain drop is fast too,
but takes ACCESS EXCLUSIVE on the table — on `calls` that queues every reader on a live
archive. The concurrent form takes SHARE UPDATE EXCLUSIVE instead. The trade-off is that a
failure part-way leaves earlier drops committed; `IF EXISTS` on both directions makes a
re-run pick up where it stopped.

### Fuzzy search: `pg_trgm` and `search_vocab` (migration `ec34a3a39b74`)

Fuzzy search does not put a trigram index on `calls`. It keeps `search_vocab`: one row per
distinct lexeme of `calls.search_tsv` (from `ts_stat`) with a trigram GIN index on `word`, and
widens a query word to its near spellings there before the normal full-text search runs.
Measured 2026-09-23: 181 354 lexemes, ~20 MB with indexes, 5.2 s to build from 5.2 M calls.
A trigram GIN on `transcript_text` would have been about 250 MB and added index entries to
every transcript write.

The worker keeps it current (`search_vocab` job, every 5 minutes): the words of transcripts
newer than the watermark in `settings.search_vocab_state` (~20 ms a pass), and a full rebuild
once a day (~6 s). The rebuild is also what removes words retention deleted and picks up
transcript corrections and notes. To rebuild by hand, after a restore or a bulk
re-transcription:

```sh
docker compose exec worker keyup transcripts search-vocab            # full rebuild
docker compose exec worker keyup transcripts search-vocab --incremental
```

The migration fills the table itself (the 5 s `ts_stat` runs inside `alembic upgrade`). The
six alias trigram indexes (`talkgroups.alias`, `radios.alias`/`unit_id`, `units.callsign`/
`name`, `personnel.name`) are built `CONCURRENTLY`. The downgrade leaves the `pg_trgm`
extension installed.

---

## 5. The activity log

`activity_events` is the archive's largest table by row count — **3.34 M rows and 1.2 GB
two weeks after it was introduced**, more rows than `calls` itself — and it is diagnostics:
one row per call accepted, transcribed, exported or failed, read only as the last few
hundred lines on the Activity page.

It now expires on a clock of its own, `activity_retention_days`, alongside the audio windows
and `radio_events_keep_days` in **Admin → Settings → Retention**. It ships at 30 days;
`0` means keep for ever, which on a busy archive means unbounded growth.

The purge is **batched** (`ACTIVITY_BATCH = 10 000` rows per statement), unlike the radio
event purge, and the difference is the first run. Radio events have been expiring on a
90-day window since they existed, so that pass only ever meets a day's worth. This window
is being applied to a table that has never had one: the first pass deletes everything past
the cutoff at once, which on a deployment that has been running for months is millions of
rows. One statement for all of them would hold a transaction open for minutes, pin every
dead tuple in the database against vacuum for its duration, and write the lot to WAL before
a byte could be reclaimed. Each batch is its own committed transaction, so a pass
interrupted half way keeps what it removed and the next night picks up from there.

It runs as part of the nightly `CRON_RETENTION` pass and its count appears in that pass's
report as `activity_events_deleted`. To run it by hand — which is what you want the first
time a window is applied to a large table, rather than discovering it at 03:20:

```bash
# the whole nightly pass, counting only
docker compose exec api keyup retention run --dry-run
docker compose exec api keyup retention run

# just the activity log, with a window for this run only
docker compose exec api keyup activity prune --keep-days 30 --dry-run
docker compose exec api keyup activity prune --keep-days 30
```

`keyup activity prune` predates this work and now goes through the same batched pass, so
there is one implementation rather than a hand path and a scheduled path that differ in the
one respect that matters.

After a large first purge, the space is reusable but still allocated. If the 1.2 GB matters,
the same `VACUUM FULL` caveats as §3 apply — otherwise leave it: unlike
`webhook_deliveries`, this table is still growing into its steady state and will reuse the
space itself.

---

## 6. A quick health pass

```bash
# bloat and vacuum lag
docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT relname, n_live_tup, n_dead_tup,
          round(100.0*n_dead_tup/nullif(n_live_tup,0),1) AS pct, last_autovacuum
     FROM pg_stat_user_tables WHERE n_dead_tup > 10000 ORDER BY n_dead_tup DESC LIMIT 15"

# indexes nobody scans (check against §4 before believing it)
docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
     FROM pg_stat_user_indexes WHERE idx_scan < 50
     ORDER BY pg_relation_size(indexrelid) DESC LIMIT 20"

# cache hit rate and temp spill
docker exec keyup-db-1 psql -U keyup -d keyup -c \
  "SELECT round(100.0*blks_hit/nullif(blks_hit+blks_read,0),1) AS cache_pct,
          pg_size_pretty(temp_bytes) AS temp, stats_reset
     FROM pg_stat_database WHERE datname='keyup'"
```

Index scan counts are cumulative **since `stats_reset`**. Reading `idx_scan = 0` a day after
a reset means nothing at all; the two-week window above is the minimum worth acting on, and
even then §4's four exceptions are why the number alone is not the decision.

---

## 7. Backups, the restore drill and health alerts

What is backed up, where, how to prove it restores, and what raises an alert when something
stops. The scripts are in `deploy/backup/` and run on the Docker host, outside the
containers. They need `docker`, `jq`, `flock` and coreutils, and talk to Postgres through
the running `db` container (`keyup-db-1`; `KEYUP_DB_CONTAINER` if yours is named
differently).

### 7.1 What to back up, and where

The sizes of one archive, measured 2026-09-23, to give a sense of proportion:

| what | size | notes |
|---|---|---|
| database (`pg_database_size`) | 27.4 GiB (29.5 GB) | 5.21 M calls; `calls` alone is 16 GB, most of the rest is indexes |
| database dump (`pg_dump -Fc`, zstd:3) | **2.1 GiB** | no indexes, compressed — 7 % of the database |
| audio tree (`AUDIO_ROOT`) | 116 GB in 7.25 M files | `du` 123 GiB with block overhead |
| — originals | 70.6 GB, 4.63 M files | the recordings; irreplaceable |
| — `.proc.mp3` processed copies | 45.5 GB, 2.62 M files | regenerable by the audio worker; not backed up |
| new originals per day | ~76 MB (DB `file_size`), ~120 MB on disk | 14-day mean |

**A copy on the same physical disk is not a backup.** It protects against a bad migration,
an accidental `DELETE` or a broken container, not against the disk. If the Docker host is a
VM or container, check where its disk actually lives: a host snapshot on the same drive is
the same single point of failure.

So there are two tiers:

1. **On the host, automated:** nightly DB dumps in `KEYUP_BACKUP_DIR`
   (default `/srv/keyup/backups/db`, §7.2). Keep it outside your checkout, so that
   replacing or re-cloning the checkout never takes the backups with it.
2. **Off the disk (needs a target — your decision):** copy the newest DB dump and the audio
   originals somewhere else. Candidates, cheapest first:
   - a USB or second internal disk on the host (or on the hypervisor, mounted through) —
     survives losing the system disk's contents, not the machine;
   - another box on the LAN with ~100 GB spare (a NAS);
   - object storage (Backblaze B2 / S3 via `rclone` or `restic`): ~75 GB of originals plus a
     few dumps is roughly USD 0.50/month on B2, with the first upload ~a day on a residential
     uplink.
   `deploy/backup/keyup-audio-backup.sh` is ready for any rsync target (a path, or
   `user@host:/path`) and refuses to run without one; the DB dumps can ride along with the
   same rsync (or `rclone copy /srv/keyup/backups/db remote:keyup-db`).

### 7.2 The nightly database backup

`deploy/backup/keyup-db-backup.sh`, run as root on the Docker host by
`keyup-db-backup.timer` (02:30 host time):

1. opens a `REPEATABLE READ` transaction and exports its snapshot;
2. counts every table **in that snapshot** — the counts the restore drill checks against;
3. runs `pg_dump --format=custom --compress=zstd:3 --snapshot=…` in a throwaway
   `postgres:18` container on the db's network namespace (so the dump is a seekable file and
   `pg_restore --jobs` works), `nice 19`, and renices the server backend doing the `COPY`;
4. checks `pg_restore --list` can read the result, writes `keyup-<UTC stamp>.dump` and then
   the manifest `keyup-<stamp>.json` (sha256, sizes, timings, alembic head, counts). The
   manifest is the success marker;
5. rotates: the newest 3 dumps, plus the newest of each of the 2 older ISO weeks (≈ 5 × 2.1
   GiB). `KEYUP_BACKUP_KEEP_DAILY` / `KEYUP_BACKUP_KEEP_WEEKLY` change it.

The directory is `root:10001`, mode 0750 — the worker (uid/gid 10001) lists it through a
read-only mount for the `backup_stale` check; the dumps and manifests stay 0600 root.

It refuses to start unless 40 GiB (`KEYUP_BACKUP_MIN_FREE_GB`) stays free after the dump —
so a backup can never itself trip the disk alert — and holds a flock so two runs (or a run
and a drill) never overlap. The live database is only read.

**On the archive measured above** (2026-09-23): 2,101 MiB in 141 s (147 s end to end), 55
tables, 5,213,197 calls in the snapshot. The dump's backend ran at nice 19; load average
peaked at 5.6 on 6 cores; uploads kept answering in ~200 ms.

**Point the worker at the same directory.** The `backup_stale` health check (§7.4) reads the
manifests through a read-only mount of `BACKUP_DIR` from `.env`; the script writes to
`KEYUP_BACKUP_DIR`. Both default to `/srv/keyup/backups/db`, so out of the box they agree.
If you move the dumps, set `BACKUP_DIR` in `.env` and `KEYUP_BACKUP_DIR` in the service to
the same path and `docker compose up -d`; otherwise the check looks at an empty directory
and reports "no database backup … yet".

Install the timer (once, on the host, as root). The unit runs the script from the checkout
named by `KEYUP_DIR` in `keyup-db-backup.service` (default `/opt/keyup`); if your checkout
is elsewhere, change that line after installing (`systemctl edit --full
keyup-db-backup.service`), and `KEYUP_BACKUP_DIR` with it if you moved the dumps:

```bash
cd /path/to/keyup          # your checkout
install -m 644 deploy/backup/keyup-db-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now keyup-db-backup.timer
systemctl list-timers keyup-db-backup.timer        # next run
journalctl -u keyup-db-backup.service -n 20        # last run
```

By hand: `deploy/backup/keyup-db-backup.sh` from the checkout, as root (same environment
knobs).

### 7.3 The restore drill

`deploy/backup/keyup-restore-drill.sh [DUMP]` proves a dump restores. With no argument it
takes the newest dump that has a manifest.

1. verifies the dump's sha256 against the manifest;
2. refuses unless 40 GiB stays free **after** a restore the size of the source database
   (a full restore is ~ the source size: 27 GiB today);
3. `CREATE DATABASE keyup_drill_<stamp>` on the same server and `pg_restore --jobs 2
   --exit-on-error --no-owner` into it, nice 19, backends and index-build workers reniced;
4. counts every table in the restore and compares each with the manifest's count from the
   dump's own snapshot, and the alembic head with the manifest's;
5. drops the scratch database — on success, failure or Ctrl-C;
6. writes `<dump>.drill.json` beside the dump and exits 0 only when every table matched.

Short on disk or time: `KEYUP_DRILL_SECTIONS=pre-data,data` skips indexes and constraints
(the counts still prove the data); `KEYUP_DRILL_JOBS=1` halves the CPU.

**On the archive measured above** (2026-09-23, the dump just described): full restore, 2 jobs —
**OK**. 55 tables, 35,153,246 rows, 0 mismatches (calls 5,213,197; transcripts 2,885,367;
activity_events 6,466,671), alembic head `ec34a3a39b74` matched. Restore 660 s, counting
8 s, 11 min 10 s end to end; the restored database is 23.8 GiB (the source's 27.4 GiB less
its bloat). Load average peaked near 10 on 6 cores — everything the drill ran was nice 19
and uploads kept answering in ~200 ms with no 5xx, but on a busy day use
`KEYUP_DRILL_JOBS=1`. Free space went 96 → 70 GiB during the restore and back to 94 GiB
after the drop.

**Thin-provisioned storage:** if the Docker host's disk is a thin volume (a VM disk, LVM
thin) and the filesystem is mounted without `discard`, the ~27 GiB the drill wrote and
freed stays allocated in the underlying pool until it is trimmed (`fstrim` in the guest, or
the hypervisor's equivalent). Trim after a drill, or on a weekly schedule.

Run a drill after anything that changes the schema or the backup script, and monthly
otherwise — a backup that has never been restored is a hope, not a backup.

**A real restore** (the database is gone, or a migration went wrong) is the same
`pg_restore` against the real database with the app stopped — never while the api or worker
can write:

```bash
cd /path/to/keyup && docker compose stop api worker web edge
D=/srv/keyup/backups/db/keyup-<stamp>.dump
docker exec keyup-db-1 psql -U keyup -d postgres -c "DROP DATABASE keyup WITH (FORCE)" -c "CREATE DATABASE keyup"
docker run --rm --network container:keyup-db-1 -v /srv/keyup/backups/db:/b:ro postgres:18 \
  pg_restore -h 127.0.0.1 -U keyup -d keyup --jobs 4 --no-owner --exit-on-error /b/$(basename $D)
docker compose up -d        # migrate runs first; a dump from an older head is upgraded
```

Everything recorded between the dump and the failure is lost from the database. Its audio
is still on disk, and `keyup ingest scan` over the tree re-reads what a file's name and tags
carry — but the push path's metadata that lives only in the database (per-source radios,
talker aliases, quality reports) does not come back that way. That recovery path is
untested; treat the window since the last dump as lost until it has been tried.

### 7.4 Health alerts

The worker checks every minute (`app/health.py`) and raises each condition **once**, then
clears it **once** after it has stayed good for five minutes — a Keyup restart neither
repeats nor forgets an alert (the state is the `health_conditions` table). Thresholds are
runtime settings (Admin → Settings → Health); 0 switches a check off.

| check | fires when | default |
|---|---|---|
| `recorder_silent:<instance>` | a **hot** recorder has not reported (warm spares and cold boxes are ignored) | 5 min |
| `ingest_lag` | no call at all recorded (longest real quiet in 14 days: 13.5 min) | 20 min |
| `stt_backlog` | the oldest live-band transcription job — queued and due, or parked by an STT outage — has waited this long; backlog-band work never counts | 30 min |
| `backup_stale` | the newest manifest in `BACKUP_DIR` is older than this (skipped if the directory is not mounted into the worker; compose mounts `.env`'s `BACKUP_DIR`, default `/srv/keyup/backups/db`, read-only — §7.2) | 30 h |

Disk space is the separate `disk_low` alert (`app/disk_watch.py`).

A raise or a clear is a `system.health` activity row and a delivery on every enabled alert
rule of kind **`health`** — the same channels as every other rule (webhook, Discord, ntfy,
email). A rule's pattern may list the checks it wants (`recorder_silent, ingest_lag`);
blank hears all four. `GET /api/status` lists the raised ones under `health`, and the
status panel shows them in a banner.

What this does not catch: the worker itself being down (nothing checks the checker). An
external probe of `GET /api/healthz` (Uptime Kuma, a cron `curl` on another box) covers the
API; `/api/status` → `health` and `disk` cover the rest.

### 7.5 Emergency alerts

The P25 emergency flag lands in `calls.emergency`. The existing kinds could not express
"emergency" (a `talkgroup` rule matches ids only), so there is a kind for it: **`emergency`**,
evaluated on `call.new` like `talkgroup` and `radio`. Its pattern narrows it — numbers are
on-air tgids, words are talkgroup categories, a leading `-` excludes:

| pattern | fires on |
|---|---|
| *(blank)* | every emergency call |
| `law, fire, ems` | emergencies on law, fire and EMS talkgroups |
| `-1507` | every emergency except TG 1507 (say, a public-works radio whose button is stuck) |

The rule's cooldown applies (default 300 s), so a radio holding its emergency state across
a dozen transmissions is one notification per cooldown, not twelve. The notification title
reads `Keyup: EMERGENCY on <talkgroup>`.
