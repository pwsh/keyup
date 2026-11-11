# Migrating the sdrtrunk archive into Keyup

Years of sdrtrunk recordings sit on an SMB share from before Keyup existed — about **20 GB**,
filed as **one folder per channel, one ZIP per day inside it**:

```
//nas/scanner/FIRE OPS 4/20260828.zip
                        /20260829.zip
             /EVENT 6/20260828.zip
             /STATE LAW 2/20260828.zip
```

This is how they get into the archive **while trunkd keeps feeding live calls**, without the
migration ever delaying a live transcription.

The mechanism is [priority and flow control](RECORDERS.md#priority-and-flow-control): every
migrated call is pushed with `priority=backlog`, which puts its transcription job 500 above
the talkgroup's own priority — behind every live call, forever if need be — and lets the API
refuse the migration with `429` whenever the queue is deep, while live pushes sail past.

**The calls themselves are never delayed.** A backlog upload that is accepted is written,
indexed, searchable, playable and visible to the correlation API immediately. Only the
transcript waits.

---


> **Superseded — kept as history. Measured 2026-08-31 (docs/RECONCILIATION.md §1):** the
> sdrtrunk filename stamp is the file-close instant — the call **end** plus ≈1.7 s — not the
> start. The advice that followed was to run the client with `--datetime-is-end`. **It no
> longer applies:** the server-side parser now reads the stamp as the call end itself
> (`started_at = stamp − duration`, `app/ingest/parser.py`) and also parses the pre-2025
> legacy names, so the client should send **neither** flag — sending one would only have the
> client redo, less well, what the server already does.

## Which path

Two ways in, and they are not equivalent:

| | |
|---|---|
| **Copy the files under `AUDIO_ROOT`** and let the watcher scan them | Fastest, no HTTP, no token — but it needs the files on the Keyup host, it enqueues everything at the talkgroup's *live* priority, and there is no back-pressure at all. Fine for a few thousand files on a quiet system. |
| **`tools/migrate_sdrtrunk.py`** over the API | Works from anywhere, reads the ZIPs in place, is resumable, obeys the flow control, and marks every row `ingest_priority='backlog'` so the migration is auditable and reversible. **Use this for a real backlog.** |

The tool never extracts an archive. It reads each ZIP's central directory to count and order
the members, then streams **one member at a time per worker** straight into the upload body —
so a 1 GB archive costs one call's worth of memory, not a gigabyte of scratch space, and the
share needs no write access at all.

---

## How long 20 GB is

Worth internalising before starting, because it is the reason for every flow-control
decision below:

| | |
|---|---|
| 20 GB of sdrtrunk MP3 at ~16 kb/s | ≈ **2 800 hours** of audio |
| Whisper on the appliance, ~25× real time | ≈ **110 hours** of transcription if it had the box to itself |

It does not have the box to itself — live calls come first, always. So every migrated call
goes up as `priority=backlog`, the client polls `GET /api/upload/flow` and parks itself
whenever the queue is deep, and the transcripts land over days or weeks while the **calls
themselves are searchable within seconds of being accepted**. Nothing about the migration is
holding a live call up, and nothing about a live incident loses the migration its place.

A call Keyup already holds costs nothing but the question: before each batch of 200 the
client hashes what it is about to send and asks `POST /api/upload/exists`
(`{"sha256": [...]}` → `{"present": [...]}`, matched against `calls.sha256`), records every
hit in its ledger as `duplicate` / `precheck: already stored`, and uploads only the rest.
`POST /api/call-upload` can only say `duplicate call rejected` after reading the whole
multipart body, so without this a re-run over an archive that is mostly already stored
spends days transferring bytes to be told they were not needed. The progress line shows the
saving as `dup=N (pre M)`; `--no-precheck` turns it off, and a server too old to have the
endpoint (404) makes the client stop asking and upload as before.

---

## Mounting the share

Read-only, on the recorder laptop (or anywhere else with python3.11+ and a route to Keyup):

```sh
sudo mkdir -p /mnt/sdrtrunk-archive
sudo mount -t cifs //nas/scanner /mnt/sdrtrunk-archive \
    -o ro,username=archive,uid=$(id -u),gid=$(id -g),vers=3.0,iocharset=utf8
# password prompt; or -o credentials=/root/.smbcred (chmod 600, "username=…/password=…")
```

`ro` is not a formality — the tool only ever reads, and a read-only mount means a bug here
cannot damage the only copy of the archive. `vers=3.0` matters on modern Samba/Windows hosts,
which refuse SMB1. Add `nofail` if you put it in `/etc/fstab`.

Without root, the desktop session can mount it in userspace instead:

```sh
gio mount smb://nas/scanner
ls /run/user/$(id -u)/gvfs/'smb-share:server=nas,share=scanner'
```

gvfs is slower and less patient with tens of thousands of small reads; prefer `cifs` for a
run of this size and keep gvfs for a look around.

Then point the tool at the mount, and say where it came from:

```sh
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --source-note '//nas/scanner ro 2026-08-31' \
    --ledger ~/keyup-migration.jsonl /mnt/sdrtrunk-archive
```

`--source-note` is free text copied onto every ledger line. It costs nothing and it is what
tells a later reader which share, which mount, which pass a row came from.

---

## Before you start

1. **Mint a token** with the `upload` scope — its own, so it can be revoked without
   silencing a recorder:

   ```sh
   docker compose exec worker keyup token create migration --scopes upload
   # {"id": 7, "label": "migration", "scopes": ["upload"], "token": "kup_…"}   ← shown once
   ```

2. **Check the queue** is somewhere near empty before adding a hundred thousand jobs:

   ```sh
   curl -s 'http://192.0.2.10:8095/api/upload/flow?key=kup_…' | jq
   # {"live":3,"backlog":0,"audio_seconds":41.2,"eta_seconds":20.6,
   #  "accepting":{"live":true,"backlog":true},"paused_backlog":false}
   ```

3. **Check the disk.** `GET /api/status` reports `disk_free_bytes`; the push path refuses
   *everything* with `507` below `UPLOAD_MIN_DISK_FREE_BYTES` (5 GiB), which is a floor, not
   a budget. An hour of 8 kHz mono MP3 is roughly 7 MB; size the archive first
   (`du -sh /mnt/sdrtrunk-archive`), and remember the ZIPs are already compressed audio, so
   what lands on the Keyup host is about what the share holds.

4. **Decide how fast.** The defaults let 300 backlog jobs queue and refuse more once the
   whole queue needs over 15 minutes to drain, so the migration self-paces to roughly the
   speed the transcription backends actually run at. If you want it slower, use
   `--rate-per-min`; if you want the calls in now and the transcripts later, raise
   `UPLOAD_MAX_QUEUE_BACKLOG` and `UPLOAD_BACKLOG_MAX_ETA_SECONDS` in Admin → Settings →
   Upload.

---

## Running it

`tools/migrate_sdrtrunk.py` is a single standard-library file — copy it to whatever host has
the share mounted, no install required (Python 3.11+):

```sh
scp tools/migrate_sdrtrunk.py recorder-host:~/
ssh recorder-host
export KEYUP_KEY=kup_…

# look first: how many calls are in there, in how many archives, and how big
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --dry-run /mnt/sdrtrunk-archive
# 486213 recordings (19.7 GB) in 4180 archives; priority=backlog workers=2

# a hundred calls, to prove the round trip
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --limit 100 \
    --ledger ~/keyup-migration.jsonl /mnt/sdrtrunk-archive

# then the lot, oldest first, two threads
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --workers 2 \
    --source-note '//nas/scanner' --ledger ~/keyup-migration.jsonl /mnt/sdrtrunk-archive
```

### Sources

Any number of them, and each may be:

| | |
|---|---|
| a **directory** | recursed: every `.zip` inside is opened and read member by member, and any loose `.mp3` is taken as it is (sdrtrunk's `.raw.wav` captures are skipped, as always) |
| a single **`.zip`** | one day of one channel |
| a **glob** | `'/mnt/sdrtrunk-archive/FIRE OPS 4/2026*.zip'` — **quote it**, so the tool expands it rather than the shell, which matters when the channel folders have spaces in them |

```sh
# one channel, one month
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --ledger ~/l.jsonl \
    '/mnt/sdrtrunk-archive/FIRE OPS 4/202608*.zip'

# two channels at once; the same ledger keeps them from overlapping
./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --ledger ~/l.jsonl \
    '/mnt/sdrtrunk-archive/FIRE OPS 4' '/mnt/sdrtrunk-archive/STATE LAW 2'
```

Ordering is by the **timestamp in each member's own name**, across every archive at once —
not per archive and not by mtime — so the run walks the calls in the order they happened
however the files were zipped or copied around. `--newest-first` reverses it when you would
rather have recent history searchable sooner.

A ZIP that will not open is printed, written to the ledger as failed, counted, and stepped
over; a member that will not decompress (the CRC is checked as it streams, so a bit-rotted
call never reaches the API) fails on its own and the run carries on. Neither is ever fatal.
Both are retried on the next run.

Progress goes to stderr:

```
4120/486213  ok=4038 dup=71 skip=11 fail=0  53 MB  312/min 4.1 MB/min  eta 1541m  waited 940s
```

The total is the member count from every archive's central directory, computed before the
first upload, so the ETA is real from the first minute.

Options worth knowing:

| flag | |
|---|---|
| `--ledger path.jsonl` | one JSON line per finished call. Read at startup, so a re-run skips what is done. **Always use it.** |
| `--source-note TEXT` | free text stamped on every ledger line: which share, which mount, which pass |
| `--workers N` | upload threads (default 2). Each holds one member at a time. More than four is pointless: `UPLOAD_BACKLOG_MAX_CONCURRENCY` (4) bounds what the API will take in flight anyway, and the fifth thread would only collect `429`s |
| `--rate-per-min N` | hard cap across all threads |
| `--priority` | `backlog` by default; `bulk` for a feed that may wait indefinitely, or an integer |
| `--system 'Example System'` | sends `systemLabel`. Omit it and Keyup reads the system out of each recording's ID3 frames and filename, which is right per-file when the share spans systems |
| `--limit N`, `--dry-run` | try before committing |
| `--newest-first` | most recent calls first |
| `--send-datetime`, `--datetime-is-end` | **leave both off** — the server reads the filename stamp as the call end and derives the start itself; see below |

Exit code is non-zero if any archive or any call failed.

### The ledger

One JSON object per finished call, appended as it finishes:

```json
{"key": "/mnt/sdrtrunk-archive/FIRE OPS 4/20260829.zip!20260829_015213_Example-System_Site-A_T-Control__TO_1801_FROM_1234567.mp3",
 "path": "/mnt/sdrtrunk-archive/FIRE OPS 4/20260829.zip",
 "member": "20260829_015213_Example-System_Site-A_T-Control__TO_1801_FROM_1234567.mp3",
 "name": "20260829_015213_Example-System_Site-A_T-Control__TO_1801_FROM_1234567.mp3",
 "folder": "FIRE OPS 4", "size": 15005, "crc32": "3d26e12e",
 "sha256": "7d62d608…", "status": "uploaded", "detail": "Call imported successfully.",
 "note": "//nas/scanner", "at": 1788168254.229}
```

`key` is `archive!member` for a member and the plain path for a loose file — which is exactly
what the ledger held before archives were a thing, so an older ledger still resumes. A re-run
skips a call whose **key** it has already finished, and also one whose **sha256** it has
already finished: a call that was loose last month and is zipped this month is recognized as
the same call and is not sent twice. `size`/`crc32` are the archive's own record of the
member, kept so a ledger line can be tied back to the bytes it came from without the share.

Only `uploaded`, `duplicate` and `skipped` count as finished. A `failed` line is a note for
you, not a tombstone: the next run tries it again.

### The channel folder is not the talkgroup

The talkgroup comes from the **filename** (`__TO_1801`) and the **ID3 frames**, which is
what makes the ZIP layout safe to flatten: nothing about placing a call depends on which
folder it was filed under. The folder name is recorded in the ledger regardless.

It is only ever sent as `talkgroupLabel` when the call carries no alias of its own — neither
in its name nor in its `TIT2` frame (`1801"FIRE OPS 4"`, which the client reads out of the
bytes it is already holding). That restraint is deliberate: `talkgroupLabel` **outranks** the
ID3 alias on the push path, so sending the folder blindly would overwrite the recorder's own
name for a talkgroup with a directory name. A folder that is just a date (`20260829`) is
never treated as a channel name at all.

### `dateTime`: `--send-datetime` and `--datetime-is-end`

**Send neither.** The question these flags existed for is settled, and it is settled on the
server: keyup's parser reads the sdrtrunk filename stamp (and the ID3 `Date` frame, which wins
where it has a value) as the instant the finished file was *closed* — the call's **end** — and
derives `started_at = stamp − duration` from the duration it measures out of the audio itself
(`app/ingest/parser.py`). It also parses the pre-2025 legacy names, the ones without the
underscore between the stamp and the system, so a name the old parser rejected now places
correctly. That is the same reading the file watcher has always used, and the one the archive
is indexed under.

So the client sends no `dateTime` at all and the server places the call. The two flags remain
for the case where a client genuinely knows better than the file — `--send-datetime` sends the
stamp as an epoch second, `--datetime-is-end` (which implies it) sends the stamp minus the
call's own duration, counted out of the MP3's frame headers — but neither is needed against a
current keyup, and the push path reads `dateTime` as the call's **start**, so `--send-datetime`
alone would place every migrated call one duration late.

### Timestamps: the pre-2025 filenames, and the repair

**This already went wrong once — read this before you resume the run.**

Sending no `dateTime` (above) makes the server's reading of `audioName` the *only* source of a
migrated call's time. Pre-2025 sdrtrunk writes that name **without the underscore between the
stamp and the system**:

```
20240223_210414Example_System_SiteA_T-Control__TO_1102_FROM_1234520.mp3
 ^ modern builds put a "_" here
```

The client's own regex was fixed for that; Keyup's parser was not, so `parse_filename` raised
for every pre-2025 name, the push path's time chain fell through to *now minus the duration*,
and **1,041,258 calls were filed on the day they were uploaded** — with `time_source='upload'`
claiming a recorder had said so. Reconciliation then compared those invented times and matched
911,778 of them against each other; conversations were threaded over the same fiction.

Three things fix it, in this order:

1. **Deploy the parser fix** (`app/ingest/parser.py`: the underscore after the time is
   optional, and in that dialect the system's spaces are underscores, so `Example_System_SiteA_T-Control`
   is the *Example System* system on the SiteA site). Without it the repair below finds nothing
   to do and the client's next push is refused. Where that block's system *ends* is a guess from
   the name alone — three of the four legacy shapes in the archive are one token of site and one
   of channel, but `EXAMPLE_COMMUNITY_SCHOOL_DISTRICT_Northside_District_Buses` has a
   two-token channel — so the push path re-splits it against the systems and sites that already
   exist and only falls back to counting from the right. It can move a call onto a system that
   exists; it can never mint one. (Names the filename parse rejects outright now also read the
   ID3 `COMM` `Date` before anything falls through to the arrival time.)
2. **Repair the archive**, from the filenames every row still carries:

   ```sh
   docker compose exec api keyup fix-upload-timestamps                     # dry run: what it would do
   docker compose exec api keyup fix-upload-timestamps --apply             # times + reconciliation
   docker compose exec api keyup fix-upload-timestamps --apply --regroup   # conversations
   ```

   Batched, committed per batch and resumable — the live feed keeps flowing beside it, and
   Ctrl-C then re-running picks up where it stopped. It never deletes a call, and no call loses
   the transcript made from its bytes. It does move **queued** transcription work, because phase
   B is reconciliation: a row ruled a duplicate goes `transcript_status='skipped'` and its queued
   jobs are cancelled, and a swap enqueues the new primary in the **backlog** band (the
   talkgroup's own priority + 500) rather than at the live path's priority 1 — a repair promotes
   hundreds of thousands of 2022–2024 primaries, and at priority 1 every one of them would sit in
   front of the live feed. Phase A also frees the
   rows *pointing at* a repaired row — a trunkd call or an unparsable-name row that was hidden
   behind one — and phases B and C rule on those too. Phase C works in batches of 1,000 because
   it holds the per-talkgroup conversation lock the live feed waits on
   (`--regroup-batch-size`), and it keeps an emptied thread that an `external_links` row points
   at rather than cascading somebody's call-for-service link away. Rows whose names it still
   cannot parse are left exactly as they are and counted in the summary, with a sample.
3. **Resume the migration** (`tools/migrate_sdrtrunk.py`, same ledger). Calls already in are
   skipped; the ones refused below are re-sent.

**Pause the scheduled reconcile pass for the duration of `--apply`.** `RECONCILE_ENABLED` /
`RECONCILE_MINUTES` drive a worker job that reconciles a recent window in one transaction, and it
takes the same per-talkgroup `pg_advisory_xact_lock(4242, tgid)` conversation locks the repair
takes — over different rows, in a different order. The first full-size run died on exactly that
at 86 % of 1,041,258 rows: two transactions each holding several of those locks, each waiting on
one the other held. Both phases now take every lock they can predict **up front and in ascending
talkgroup order**, which is a total order and therefore has no cycle in it, and any batch that
still loses a race is rolled back, slept on with jitter and re-run up to five times (a `WARNING`
per attempt) before the run gives up — resumable from its watermark either way. So leaving the
job on now costs retries rather than the run; turning it off costs nothing, and the repair
reconciles the repaired rows itself. The command prints the reminder when `--apply` is used with
the job enabled. The job itself is unchanged — it belongs to the live archive, not to this run.

A match can cross talkgroups (`sha256` matches on bytes alone, and the interval rung also keys on
`frequency_hz`), so phase B cannot know every key it needs before it looks: the candidate's
talkgroup is locked the moment `find_match` names it, which is the one acquisition that can be
out of order. It is rare — two copies of one transmission almost always carry the same talkgroup,
and then the key is already held — and it is what the retry is for.

The endpoint now refuses a **backlog** push it cannot derive a time for — 400,
`{"error": "no_call_time", …}` — rather than filing it under its arrival. The client counts that
as failed, keeps it out of the ledger's done set, and re-sends it on the next run, so the fix is
always "deploy the parser (or pass `--send-datetime`) and start the tool again". A *live* push
keeps the arrival fallback, with a rate-limited `WARNING` and a `call.time_fallback` activity
event, because a live call has no second chance.

**Until `--regroup` runs, the conversations are still wrong** — and that is not only cosmetic.
Phase A moves the calls but not `conversations.started_at` / `ended_at`, so the enormous bogus
threads stay parked on 2026-09-01 with no members, and a live call on one of those talkgroups
today is within the grouping gap of one and *joins* it. Run `--regroup` in the same maintenance
window, not "later": afterwards those threads hold nothing but live calls, so the prune leaves
them standing and the mess needs untangling by hand. *(Follow-up worth considering, not built:
having the regroup re-derive each thread's bounds from the members it actually has and split a
day-long merged thread on the grouping gap, which would repair the ones that already absorbed
live traffic.)*

### Re-filing the audio: run `refile-audio` once, after the timestamp repair

`fix-upload-timestamps` moves the rows; the **files** are still in the directory the upload day
chose — 972,347 of them in `uploads/2026/09/01` and 82,174 in `uploads/2026/08/31`. That is not a
correctness problem (`calls.file_path` is the source of truth and a lookup by name in an ext4
`dir_index` directory is a hash probe) and it is a serious operational one: `readdir` returns hash
order, so `rsync`, `find`, a backup and any directory walk pay for the whole directory and then
seek all over the disk. So, **once, after the timestamps are right**:

```sh
docker compose exec api keyup refile-audio                              # dry run: what it would move
docker compose exec api keyup refile-audio --apply                      # ~1.05M renames, minutes
docker compose exec api keyup refile-audio --apply \
    --source-prefix uploads/2026/09/01                                  # or just the one directory
```

It walks `calls`, not the filesystem — enumerating the directory it exists to break up would be
the slowest possible way to fix it — and each move is an `os.rename` within one filesystem plus
the `file_path` update in the same batch transaction, committed every `--batch-size` (5,000) rows.
The inode is compared before and after, so a move is verified rather than assumed; a file that is
not where its row says is **reported, not fatal**; nothing is ever deleted. Safe to interrupt and
safe to re-run: a moved row agrees with its own date and is counted `unchanged` next time, and a
row whose file made it across before the commit did is recognised and repaired. Emptied
directories are pruned as it goes.

Order matters: run it **after** `fix-upload-timestamps --apply`, because it partitions on
`started_at`, and a run against the un-repaired rows would faithfully re-file a million calls
under 2026-09-01 all over again.

New pushes need nothing — the endpoint has filed by call date since this branch (ARCHITECTURE.md
§1.1, "Storage layout").

---

## While it runs

```sh
watch -n5 "curl -s 'http://192.0.2.10:8095/api/upload/flow?key=$KEYUP_KEY' | jq -c"
curl -s http://192.0.2.10:8095/api/status | jq .queue
```

The worker logs the same gauge once a minute:

```
stt queue: live=2 backlog=298 audio=3120s eta=780s (backlog threads 1/1)
```

**To stop it feeding without killing it** — a live incident, a backend outage:

```sh
curl -sX POST http://192.0.2.10:8095/api/admin/upload/pause-backlog -H 'Authorization: Bearer kup_…admin'
```

The client notices (`accepting.backlog: false` on `/api/upload/flow`), parks itself, and
resumes on its own when you `resume-backlog`. Nothing is lost and no ledger entry is written
for a call that was not stored.

**To stop it entirely**, Ctrl-C. Re-run the same command with the same `--ledger` and it
picks up where it left off — the archive is re-walked (seconds, it is only central
directories) and every finished member is skipped without being read.

**If the mount drops** mid-run, the members in flight fail, the rest of the walk fails with
them, and the run ends non-zero having written every success to the ledger. Remount, re-run
the same command: it resumes.

**One thing to watch on a very large share.** The walk builds its index up front — that is
what makes the ETA honest — at roughly **350 bytes per call**, so half a million calls is
about 180 MB of RSS and a share with a couple of million is heading for a gigabyte. On a
laptop that would rather not, run it a channel at a time against the same ledger:

```sh
for channel in /mnt/sdrtrunk-archive/*/; do
  ./migrate_sdrtrunk.py --url http://192.0.2.10:8095 --ledger ~/keyup-migration.jsonl \
      --source-note '//nas/scanner' "$channel" || echo "FAILED: $channel" >> ~/migration-failures
done
```

---

## Afterwards

```sh
# how many calls came in under the migration, and how many are still waiting on a transcript
psql -c "SELECT ingest_priority, transcript_status, count(*) FROM calls
         WHERE ingest_priority IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2;"

# what the ledger says
jq -r .status ~/keyup-migration.jsonl | sort | uniq -c

# anything that failed, and why — a corrupt archive, a corrupt member, a refusal
jq -c 'select(.status == "failed") | {key, detail}' ~/keyup-migration.jsonl | head

# how much of each channel folder made it in
jq -r 'select(.status == "uploaded") | .folder' ~/keyup-migration.jsonl | sort | uniq -c | sort -rn

# which archives were touched at all
jq -r .path ~/keyup-migration.jsonl | sort -u | wc -l
```

`calls.ingest_priority` is `backlog` (or whatever class you pushed) for every migrated row
and NULL for everything the live recorders brought in, so the two are always tellable apart —
in SQL, in the API's filters, and in an audit a year from now.

The transcription queue drains on its own from there, at the share of the workers
`STT_BACKLOG_WORKER_SHARE` allows, whenever live traffic leaves them idle. A backlog of a
hundred thousand calls takes as long as it takes; nothing about it is holding a live call up.

Finally, **revoke the token**:

```sh
docker compose exec worker keyup token revoke 7
```
