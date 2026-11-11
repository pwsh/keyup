# Pointing a recorder at Keyup

Keyup takes calls from three recorders and two transports, and the combinations are
independent — you can run all three, or one recorder over both transports, and the archive
ends up with the same rows either way.

| | **File** (Keyup watches a directory) | **Push** (the recorder POSTs each call) |
|---|---|---|
| **sdrtrunk** | MP3s under `AUDIO_ROOT`; §1.1–1.3 of the [architecture](ARCHITECTURE.md) | *Rdio Scanner* audio module → `POST /api/call-upload` |
| **trunk-recorder** | `captureDir` mounted at `AUDIO_ROOT`; §1.7 | `rdioscanner_uploader` plugin → the same endpoint |
| **trunkd** | — | built-in uplink → the same endpoint; §5 |

**Which to choose.** The file path needs no credential and survives a Keyup outage —
the recorder keeps writing and the next scan catches up — but it needs the recorder and
Keyup to see the same filesystem, and the call arrives whenever the scan or the inotify
event does. The push path works across a network with nothing shared, and gives sdrtrunk
calls a *true* call start instead of a file-write time; but if Keyup is down the call is
gone, so on one host the file path is the better default and the push path is what a remote
recorder uses.

Both paths converge on the same code: the same directory upserts, the same dedupe, the same
conversation grouping, the same transcription queue. A call that arrives twice — because you
turned on both — is caught by the dedupe (identical `sha256`, or the same talkgroups within
`DEDUPE_WINDOW_SECONDS`) and the second copy is kept, marked, and hidden from listings.

---

## 1. sdrtrunk, watching files

sdrtrunk writes every recording into one flat folder. Point `AUDIO_ROOT` at it — the walk is
recursive, so a post-processing script that sorts into `YYYYMMDD/<alias>/` works equally well.

In sdrtrunk: **Playlist → Channel → your channel → Record → Audio**, and set the recording
directory under **View → Preferences → Directories** to a path Keyup can read. Then:

```sh
# .env next to compose.yaml
AUDIO_ROOT=/srv/sdrtrunk/recordings
```

`AUDIO_ROOT` is a folder on the Docker host; compose binds it into the containers at
`/srv/keyup/audio`. The containers run as **uid 10001**, so that user needs to read the
recordings, and to write `AUDIO_ROOT/uploads/` if anything pushes calls (§2, §4, §5), and
`EVENT_LOG_ROOT` (default `/srv/keyup/eventlogs` in `.env.example`) if a trunkd recorder
ships its event logs. `scripts/bootstrap.sh` creates any of these folders that do not exist
yet, owned by 10001, and adds the `uploads/` folder to an existing recording tree without
changing its owner. A tree it did not create must be readable by uid 10001 — for example
`chmod -R o+rX` on it, or a group the recorder and uid 10001 share.

**Set `TZ` to the recorder's time zone.** sdrtrunk's filename stamps and ID3 `Date` frame
are naive local time, and Keyup reads them in `TZ` — an IANA
name such as `America/New_York`. `.env.example` ships `UTC`; `scripts/bootstrap.sh` writes
the host's own zone into a new `.env`. A wrong zone files every file-path call hours off.

```sh
docker compose up -d
docker compose exec worker keyup ingest scan /srv/keyup/audio   # backfill what is there
```

The `worker` service watches it from then on (inotify plus a full rescan every
`INGEST_SCAN_MINUTES`, so nothing is lost to downtime). Nothing else is needed: the ID3
frames sdrtrunk writes carry the system, site, channel, frequency, talkgroup, aliases and
the write timestamp, and the filename carries the rest.

Import your aliases so talkgroups arrive named rather than as bare numbers:

```sh
curl -F 'file=@/path/to/playlist/default.xml' \
     -H "Authorization: Bearer $KEYUP_TOKEN" \
     http://192.0.2.10:8095/api/talkgroups/import
```

## 2. sdrtrunk, streaming uploads

sdrtrunk's **Rdio Scanner** audio module posts one call per request and decides whether it
worked by string-matching the response body; Keyup implements that contract literally
(§1.6). Mint a token with the `upload` scope — that scope and nothing more, because the key
travels in the form body of every call:

```sh
docker compose exec api keyup token create sdrtrunk --scopes upload
# {"id": 3, "label": "sdrtrunk", "scopes": ["upload"], "token": "kup_…"}   ← shown once
```

or, without a shell: **Admin → Recorder → Connect a recorder** mints the same
`upload`-scoped key and prints the block below with the key already in it, addressed to
whatever URL the browser reached Keyup on.

In sdrtrunk: **Playlist → Streaming → New → Rdio Scanner**, then

| Field | Value |
|---|---|
| Server URL | `http://192.0.2.10:8095/api/call-upload` |
| API Key | the token printed above |
| System ID | any number; Keyup reads the system from `systemLabel`, not this |

and enable the stream on each channel's **Record** tab. sdrtrunk's *Test* button sends
`test=1` and expects `incomplete call data: no talkgroup` back — that reply means it worked.

Uploaded calls are stored under `AUDIO_ROOT/uploads/YYYY/MM/DD/<ab>/` with
`time_source='upload'`, because the form's `dateTime` is the recorder's own call start and
is better than anything a file path can say. That day is the **call's** day, not the push's:
the body is spooled to `uploads/.incoming/` and renamed into its partition once the record
has resolved the start (ARCHITECTURE.md §1.1, "Storage layout").

**A push with no time at all.** Without `dateTime`, sdrtrunk's ID3 `Date` frame and its
filename stamp answer instead. With neither, a **backlog** or **bulk** push is refused — 400,
`{"error": "no_call_time", …}` — because it is a file that still exists and can be sent again,
and filing it under the moment it arrived is how a million calls once came to be stamped with
their upload date (docs/MIGRATION.md, "Timestamps"). A **live** push keeps that fallback, since
there is no second chance at a live call, and says so: a `WARNING` at most once a minute, and a
`call.time_fallback` activity event carrying the `audioName` and how many were suppressed.

## 3. trunk-recorder, watching its capture directory

trunk-recorder writes, per call, into `<captureDir>/<shortName>/<Y>/<M>/<D>/`:

```
1701-1788012000.123_851250000-call_1.wav    the audio (and/or .m4a)
1701-1788012000.123_851250000-call_1.json   the sidecar — the interesting half
```

Mount that `captureDir` as `AUDIO_ROOT` and there is nothing else to configure:

```sh
# .env next to compose.yaml
AUDIO_ROOT=/srv/trunk-recorder/media
TR_PREFER_AUDIO=m4a        # when both a .wav and a .m4a exist, which becomes the call
TR_SIDECAR_WAIT_SECONDS=30 # how long to hold a finished recording waiting for its .json
```

The matching `config.json` for trunk-recorder:

```jsonc
{
  "captureDir": "/srv/trunk-recorder/media",
  "callTimeout": 3,
  "systems": [
    {
      "shortName": "EXAMPLE",          // becomes the Keyup system name
      "type": "p25",
      "control_channels": [851250000, 852500000],
      "talkgroupsFile": "example-talkgroups.csv",
      "audioArchive": true,            // keep the call audio
      "callLog": true,                 // write the .json sidecar — Keyup needs this
      "compressWav": true,             // also write an .m4a (see TR_PREFER_AUDIO)
      "recordUnknown": true,
      "hideEncrypted": false           // keep encrypted calls: see the note below
    }
  ]
}
```

Three settings matter more than they look:

- **`callLog: true`** writes the sidecar. Without it Keyup still ingests every call —
  from the filename and the audio header — but loses the exact start and stop times, the
  emergency and encryption flags, the patched talkgroups, and `srcList`, which is the only
  place any recorder says *who keyed up when* inside one call.
- **`compressWav: true`** makes trunk-recorder write both a WAV and an M4A. They are the
  same audio, so Keyup ingests exactly one of them (`TR_PREFER_AUDIO`, default `m4a`)
  and the other is not a second call.
- **`hideEncrypted: false`** leaves a sidecar behind for encrypted calls, which have no
  audio at all. Keyup ingests those too, with `audio_state='purged'`: the row proves an
  agency held that talkgroup at that second, which is exactly what an outside system
  linking incidents to calls (ARCHITECTURE §4.6) asks for, and it costs a few hundred bytes.

trunk-recorder writes the audio first and the sidecar a moment later, so a call is held for
up to `TR_SIDECAR_WAIT_SECONDS` before it is ingested. If the sidecar never comes the call
is ingested anyway, from the filename, with `time_source='file_write'` to say so.

`.raw.wav` — trunk-recorder's undecoded baseband capture, written when you are debugging a
decoder — is skipped, so leaving `audioArchive` debugging on does not fill the archive with
gigabyte "calls".

## 4. trunk-recorder, streaming uploads

trunk-recorder's bundled `rdioscanner_uploader` plugin posts to the same endpoint. Mint the
token the same way (`--scopes upload`), then add to `config.json`:

```jsonc
{
  "plugins": [
    {
      "name": "rdioscanner_uploader",
      "library": "librdioscanner_uploader.so",
      "server": "http://192.0.2.10:8095/api/call-upload",
      "systems": [
        {
          "shortName": "EXAMPLE",
          "apiKey": "kup_…",           // the token printed by `keyup token create`
          "systemId": 1                // any number; Keyup reads systemLabel instead
        }
      ]
    }
  ]
}
```

The plugin sends the same fields sdrtrunk does plus its own: `sources` (its `srcList`),
`frequencies`, `patches`, `talkgroupLabel`/`Name`/`Group`/`Tag`, and `audioType`. Keyup
consumes all of them — and it is the presence of `sources`/`frequencies` that tells it which
recorder is on the wire, which matters because the two spell `patches` differently
(sdrtrunk puts the supergroup first; trunk-recorder posts a bare member list).

`audioType` decides how the call is stored: `audio/mp4` → `.m4a`, `audio/wav` → `.wav`,
anything else → `.mp3`. Getting it right is not cosmetic — the extension is what picks the
duration probe and the `Content-Type` the browser is later handed.

---

## 5. trunkd, streaming uploads

trunkd has no file mode: it streams, one
`multipart/form-data` POST per call to the same `/api/call-upload` endpoint, in
trunk-recorder's dialect — the same `sources`/`frequencies` lists, the same **bare member
list** in `patches` (no supergroup at the head).

**If you installed with `scripts/bootstrap.sh`, the token already exists.** Its last step
mints an `upload`-scoped token labelled `trunkd` (`--token-label` picks another label) and
prints a ready `[uplink]` block, addressed to `PUBLIC_URL` from `.env`, with the token in it.
The token is shown once — only its hash is stored — so a re-run of the script finds the
active `trunkd` token and does not mint another; to replace a lost one, revoke it (below) and
run the script again. **Admin → Recorder → Connect a recorder** does the same from the
browser. By hand, mint it the same way as for the other recorders:

```sh
docker compose exec api keyup token create trunkd --scopes upload
# {"id": 4, "label": "trunkd", "scopes": ["upload"], "token": "kup_…"}   ← shown once
```

and point the uplink at Keyup (here at 192.0.2.10, through the web edge on `WEB_PORT`,
8095 by default — the API's own `API_PORT` is published on the host's loopback only):

```toml
# trunkd.toml
[uplink]
url          = "http://192.0.2.10:8095/api/call-upload"
api_key      = "kup_…"              # the token printed above
system_label = "Example System"     # becomes the Keyup system name
site_label   = "Site 1"
```

`system_label` is what Keyup files the calls under (`systemLabel` on the wire); the numeric
`system` field is ignored, as it is for the other two recorders. It must match the system
name your talkgroups are filed under in Keyup, or the calls start a second system.

A push that sends no `systemLabel` at all does **not** create a system named after
whatever else was on the form. sdrtrunk's own ID3 `System` frame is read first, and failing
that the call joins the only system in the archive; where there is none, or more than one,
the push is refused with `incomplete call data: no system` (400) rather than filed under a
guess. Keyup grew a phantom `System("upload")` once, holding duplicate copies of two real
sites — `keyup fix-upload-system` folds one back into the real system if you have it.

### Folding a phantom `upload` system

The phantom is not small. By the time it was noticed on the archive it grew in, it held **1,005,338
calls**, 399 talkgroups, 2,396 radios and 2 sites, and the repair has to move every one of
those references onto the real system without taking the live feed down with it. It runs in
three phases and only the middle one is long:

| | what it does | how long it holds a lock |
|---|---|---|
| **A — map** | pairs each phantom site/talkgroup/radio/patch with its twin under the real system, and re-parents the ones with no twin (a site only the phantom had heard) | one transaction, milliseconds, and only on rows the phantom owns |
| **B — repoint** | moves every call, radio event, gap, observation and frequency off the phantom's ids, in batches of `--batch-size` with a `COMMIT` after each | one batch at a time — a few hundred milliseconds per 20,000 rows |
| **C — finish** | sums the call counters, fills the real rows' blanks, deletes the emptied duplicates and the phantom system | one transaction, milliseconds — the only moment a *real* row is locked |

**The live feed keeps flowing.** Phase B never reads or writes a row of the real system; it
only touches rows still pointing at something the phantom owns, which nothing live is
writing. Between batches it holds no locks at all, so an upload's `INSERT INTO sites …
ON CONFLICT` and a transcription's `UPDATE calls` go through as usual. The earlier
single-transaction version did the opposite — it locked the real site and the real
talkgroups in its first seconds and held them to the end, which is why the *dry run* alone
stalled the feed for six minutes before it was killed.

**Expect about ten to fifteen minutes on a million calls** at the default batch size: five
referring columns of `calls` at roughly 50 batches each, plus the small tables. Watch the
progress lines on stderr rather than the clock.

**Any two systems, from the Sites page.** `fix-upload-system` finds the phantom by its
name and refuses to guess when there is more than one candidate, which is right for a
repair run at 3am and useless for a `systemLabel` typo an operator can see. The same three
phases are behind **Sites → Radio systems → Merge**
(`POST /api/systems/{id}/merge-into/{target}`), which chooses the two systems from the
request instead: same folding of twins, same re-parenting of the rows that have none, same
committed batches — plus the fold's conversation advisory locks, taken in ascending id
order, because this one runs while somebody is watching the live feed.

```bash
# read-only: what it would do, how many rows each table would have repointed.
# No rehearsal transaction, no locks — safe to run at any time, on a live archive.
docker compose exec api keyup fix-upload-system

# do it.  Progress goes to stderr, the JSON summary to stdout.
docker compose exec api keyup fix-upload-system --apply | tee fold.json

# gentler on a busy hour, or bolder on a quiet one
docker compose exec api keyup fix-upload-system --apply --batch-size 5000
```

**Re-runs are safe.** Ctrl-C during phase B commits nothing further and loses nothing:
the batches already written stay written, and running the command again re-derives the
same plan — a re-parented row is no longer a phantom child, and a repointed row no longer
matches the phantom's ids — then carries on from where it stopped. Phase C refuses to run
at all while anything still points at a phantom id, naming the table and the count, so a
half-finished repair can never delete a row someone is still using. After a clean run the
command reports `no system named 'upload' — nothing to do`.

### Repairing backlog timestamps

The phantom system's sibling, and from the same push (docs/MIGRATION.md, "Timestamps"): a
parser that rejected every pre-2025 sdrtrunk filename left **a million backlog calls stamped
with the day they were uploaded**, then deduped and threaded against each other on those
invented times. `keyup fix-upload-timestamps` puts them back, from the filename each row still
carries.

| | what it does | cost on ~1 M rows |
|---|---|---|
| **A — repair** | recomputes `recorded_at`/`ended_at`/`started_at`/`started_local` from the filename stamp exactly as the file parser does, sets `time_source='file_write'`, and clears the `duplicate_of` / `reconciled_at` / `reconcile_reason` that were ruled on the wrong times — on the repaired row *and* on any row still pointing at it (`unhidden` in the summary), which is how a trunkd call or an unparsable-name row stops being a duplicate of a transmission that has just moved to 2024 | ~50 batches of a bulk `UPDATE … WHERE id`, **5–15 min** |
| **B — reconcile** | re-runs matching and scoring over the repaired rows so genuine duplicates re-link on their true times and everything else stays primary; a promoted primary's transcription is re-queued in the **backlog** band, never at the live path's priority 1 | three queries per row — the row, the checksum probe and the candidate lookup — plus one advisory lock per talkgroup per batch, **30–60 min** |
| **C — regroup** (`--regroup`) | takes the repaired calls out of the threads built over the fiction and re-runs conversation grouping in **true chronological order**, then drops the emptied threads | a lock, a lookup and a write per row, **45–90 min**, in batches of 1,000 (`--regroup-batch-size`) |

```bash
# read-only: what it would rewrite, what already agrees, what it still cannot parse
docker compose exec api keyup fix-upload-timestamps

# times and reconciliation.  Progress on stderr, the JSON summary on stdout.
docker compose exec api keyup fix-upload-timestamps --apply | tee timestamps.json

# then the conversations, which is a second full pass in a different order
docker compose exec api keyup fix-upload-timestamps --apply --regroup
```

**Pause the scheduled reconcile pass while `--apply` runs** (`RECONCILE_ENABLED=false`, restart
the worker). It reconciles a recent window in one transaction and takes the same per-talkgroup
conversation locks phases B and C take, over different rows in a different order — which is how
the first full-size run deadlocked at 86 % of the archive. Both phases now take every lock they
can predict up front in ascending talkgroup order, and any batch that still loses a race is
retried up to five times with jittered backoff before the run gives up, so leaving the job on
costs retries rather than the run. The command prints the reminder when it applies.

**Why `--regroup` is a flag rather than a third phase everybody pays for.** A and B walk the
archive by primary key; C has to walk it by `started_at`, because the grouper attaches a call to
a thread it *finds*, and out-of-order walking builds two threads where there should be one. It
also takes the per-talkgroup advisory lock on every row and prunes conversations afterwards.
That is a different access pattern, a different cost and a different failure surface, so it is
opted into and resumed on its own; a run without it reports `regroup.pending` — how many rows are
still threaded on the old times — rather than leaving it unsaid.

**Why phase C's batches are twenty times smaller.** It takes `pg_advisory_xact_lock(4242, tgid)`
for every call it moves — the same lock live ingest takes, and taken *before* the call leaves its
old thread so the two orders can never deadlock — and a transaction-scoped advisory lock is held
until the batch commits. A 20,000-row batch would hold every talkgroup it touched against the
live feed for minutes; 1,000 is seconds. `--regroup-batch-size` moves it.

**Re-runs are safe.** Phase A is self-limiting: a repaired row's time now agrees with its
filename and its `time_source` is no longer `upload`, so it drops out of the query. B and C keep
a watermark in `settings` and resume from it, and clear it when they finish; B also records that
it *finished*, so re-running the command to complete an interrupted phase C skips it rather than
re-walking a million rows to make no decision (any later phase A that repairs something clears
that marker). Nothing here deletes a call and no call loses the transcript made from its bytes.
Phase B does move **queued** transcription work, because it is reconciliation: a row it rules a
duplicate goes `transcript_status='skipped'` with its queued jobs cancelled, and a swap re-enqueues
the new primary — the live reconciler uses priority 1, and only when the copy it displaced had text, while the
timestamp repair passes the **backlog** class instead (the talkgroup's own priority + 500), because a repair
promotes hundreds of thousands of old primaries at once and every one of them at priority 1 sits in front of the
live feed (docs/MIGRATION.md). Finished text is never deleted. Emptied conversations are
dropped, except any an `external_links` row points at (a CAD incident number, a manual link): that
foreign key cascades, so those threads are kept and counted instead. Rows whose names still
cannot be parsed are left stamped exactly as they are and reported, with a sample, so they can be
looked at by hand.

### What trunkd sends

Everything sdrtrunk sends, everything trunk-recorder's uploader adds, and six fields of its
own. Audio is an MP3 by default — 8 kHz mono, 16 kbps CBR from LAME, with **no ID3 frames**
and a name of trunkd's own choosing, so nothing is parsed out of the file and every field
below is taken from the form. WAV (8 kHz mono 16-bit) is the option; `audioType` decides
which, and therefore the stored extension, the duration probe and the `Content-Type` the
browser is later handed.

| Field | |
|---|---|
| `key` | the upload-scoped token |
| `system`, `systemLabel` | the label is the system name; the number is ignored |
| `dateTime` | unix seconds, the **true call start** → `time_source='upload'` |
| `audio`, `audioName`, `audioType` | `audio/mpeg` → `.mp3` (default), `audio/wav` → `.wav` |
| `talkgroup`, `talkgroupLabel` | |
| `frequency`, `frequencies` | the second is JSON, kept in `raw_meta` |
| `source`, `sources` | `sources` is JSON `[{"pos","src","emergency","tag"}]` → `call_sources` |
| `patches` | JSON, **bare member list** → `call_type='patch'`, `talkgroup_ids` |
| `emergency`, `encrypted` | `0`/`1`; absent means "the recorder did not say" |
| `talkerAlias` | P25 talker alias |
| `recorder=trunkd` | what makes the row `ingest_source='trunkd'` — see below |
| `site` | the receiving site → `sites` / `calls.site_id` |
| `nac`, `algorithm`, `keyId` | P25 network id, and how an encrypted call was encrypted → `raw_meta` |
| `durationMs` | the length trunkd timed, used when there is no audio to measure |
| `sourceQuality` | JSON: which receiver this is and how well it heard the call — see *Best source* below |
| `call_kind` | `group` (default, and what every recorder sent before this existed) or `unit` — see *Unit-to-unit calls* below |
| `target_radio_id` | **unit calls only**: the radio the private call was addressed to |
| `mode`, `decoder` | **conventional channels only**: `nbfm`, `NBFM` — the call is analog narrowband FM → `raw_meta`, and `decoder` → `calls.decoder` |
| `tone`, `toneDetected` | conventional: the configured and the detected CTCSS/DCS squelch tone (e.g. `192.8`, `D445N`) → `raw_meta`, shown on the call page |
| `squelchReason`, `snrDb`, `peakDeviationHz`, `offsetHz` | conventional: why squelch closed, and what the channel measured → `raw_meta` |

**`recorder=trunkd` is the only thing that separates trunkd from trunk-recorder.** The two
forms are otherwise identical, and Keyup does not guess: without that field a trunkd call is
filed as `ingest_source='trunk_recorder'`, which is not wrong about the dialect but is wrong
about the recorder. Set it.

**Analog conventional channels.** trunkd sends a conventional-channel call as an ordinary
group call on the channel's own system label and (synthetic) talkgroup, with no keying radio
(`sources=[]`), plus `mode=nbfm` and `decoder=NBFM`. `decoder` is used as the **protocol of a
system this push creates** — so a system first seen from a conventional channel is `NBFM`,
which is not trunked — and never fills in or changes the protocol of a system that already
exists (a P25 system's label reused for a channel keeps its protocol). A name or ID3 frame
that names a decoder still wins over the form. **Analog calls are never sent to a
trunked-only exporter (Broadcastify Calls)**: the export gate refuses any call whose
`raw_meta` says `mode`/`decoder` `nbfm`, whatever its system, as well as every call on a
system that is not trunked (docs/EXPORTERS.md). The Broadcastify Feed and OpenMHz take them.

### Unit-to-unit calls

A P25 **private call** is one radio talking to one radio. The FNE grants it a traffic
channel like any other call, so trunkd can record it — and it is on **no talkgroup at
all**, which is the whole difficulty: before this, a call with no talkgroup meant
*conventional* traffic keyed by the frequency it was heard on, and the two must never be
confused.

Two form fields say so:

| Field | |
|---|---|
| `call_kind` | `group` or `unit`. Absent or empty is `group` — the contract every recorder already speaks is unchanged |
| `target_radio_id` | the radio at the far end. Required when `call_kind=unit`, refused otherwise |

A unit push sends **no `talkgroup`**, and everything else exactly as it always did:

```
POST /api/call-upload   call_kind=unit, source=9100001, target_radio_id=9100109,
                        dateTime=…, frequency=…, systemLabel=…, recorder=trunkd, audio=…
200  Call imported successfully.
```

The row is written with `calls.kind='unit'`, `calls.call_type='individual'`,
`calls.radio_id` the caller and `calls.target_radio_id` the callee — both resolved to
`radios` rows the same way a source always was, so the far end appears in the directory. The
call against the *callee* is deliberately not counted in its `call_count`: it did not key
up. **No talkgroup row is created**; `talkgroup_id` is NULL and `talkgroup_ids` is empty,
and that is the honest record of a call that was on no channel anybody can subscribe to.

Three refusals, all `400`, all because the alternative is to invent something:

```
call_kind=telepathy                 incomplete call data: unknown call kind
call_kind=unit  +  talkgroup=1701  incomplete call data: a unit call has no talkgroup
call_kind=unit  +  no target        incomplete call data: no target radio
```

A unit call carrying a talkgroup is a recorder bug and both readings of it — file it under
the talkgroup, or drop the talkgroup — lose something nothing downstream could see.

**Reconciliation** keys a unit call on `(system, source radio, target radio, time)` rather
than on a talkgroup: a private call shares its traffic frequency with whatever the site
granted next, so the frequency rung that saves a conventional call would pair two unrelated
conversations. A unit call and a group call never reconcile with each other whatever their
timing says (docs/RECONCILIATION.md). **Transcription** treats a unit call like any other,
on the global default — there is no talkgroup policy to consult. **The exporters** never
publish one to Broadcastify Calls or OpenMHz, and by default do publish it on the live Feed
(docs/EXPORTERS.md).

The control-channel grant that precedes the recording is a radio event in its own right,
`kind='private_call'` with `target_radio_id` (see *Radio events from trunkd* below); it
arrives whether or not a recording follows.

### Encrypted calls arrive without audio

P25 encrypts the voice payload and nothing else, so trunkd knows the talkgroup, the radios,
the timing, the algorithm and the key id of an encrypted call and has no sample to send. An
upload with **`encrypted=1` and no `audio` part** (or an empty one) is therefore accepted as
a call rather than refused:

```
POST /api/call-upload   encrypted=1, durationMs=4200, talkgroup=1701, …, no audio part
200  Call imported successfully.
```

The row is written with `audio_state='purged'` — the same vocabulary the trunk-recorder
sidecar path uses for the same fact (§3) — `duration_s` from `durationMs` (`0` when trunkd
did not time it), `time_source='upload'`, and every talkgroup, radio, site, patch and
`raw_meta` field the form carried. `GET /calls/{id}/audio` answers `410 Gone` rather than
inventing a file, nothing is queued for transcription, and the calls API (and anything linking
outside records to calls, §4.6 of the architecture) still sees that an agency held that talkgroup at that second — which is the
whole reason to keep the row.

A **clear** call with no audio is still the recorder failing to send one:
`400 incomplete call data: no audio`, as before.

### What Keyup sends back: the tuner desired state

trunkd polls `GET /api/recorder/desired-state?instance=<instance_id>` with the same
upload-scoped token and gets back
`{version, tuners: {<tuner_key>: {version, config}}, recorder: {version, spool_only,
event_log_archive, link, broadcastify, skip_talkgroups}}`. The `config` is what an operator set from **Admin → Recorder**,
and the recorder echoes the per-tuner `version` back as `applied_version` in its next status
push — until it does, Keyup calls the tuner *pending*. The `recorder` block is the same
contract one level up, for what Keyup wants of the *instance* rather than of a tuner — see
*The recorder's own desired state*.

**There is no registration endpoint.** A `recorders` row is created by the recorder's first
desired-state poll or its first status push, keyed on the `instance_id` it sends, and its tuner
rows appear the same way — an unknown `tuner_key` in a status push creates one. Nothing has to be
declared in Keyup first. A status push may also carry tuner fields beyond the ones Keyup types; they
are accepted and stored verbatim in `tuners.reported`, so the reported set can grow with the
recorder without a Keyup migration.

| key | |
|---|---|
| `enabled` | whether to use the tuner at all |
| `role` | `auto` \| `control` \| `voice` \| `data` |
| `site_id`, `site` | which site to focus on: a Keyup site id, or a free-form label |
| `center_frequency` | Hz; refused unless it falls inside the tuner's last reported span |
| `spectrum` | whether to stream FFT frames for the live waterfall |
| `bands` | which downlink ranges this tuner may record — below |
| `sample_rate` | Hz; `sample_rate / 12500` must be an **even** whole number of at least 4 channels |
| `bandwidth_hz` | analog front-end filter, Hz; `0` or null lets the driver choose |
| `gain` | overall gain, dB (Soapy devices) |
| `gains` | `{element: dB}`, keyed by a name the tuner reported in `gain_elements` (Soapy) |
| `agc` | hardware AGC on (Soapy) |
| `rx_port` | which antenna port to receive on (Soapy) |
| `settings` | `{key: value}` passed straight to the Soapy driver, at most 64 entries |
| `lna_db`, `vga_db`, `amp` | HackRF only: 0–40 dB in 8 dB steps, 0–62 dB in 2 dB steps, front-end amp |
| `ppm` | crystal correction, ±200 |
| `auto_ppm`, `auto_ppm_limit` | let the recorder track its own error, and how far it may wander |
| `data_follow` | follow SNDCP data grants as well as voice ones |
| `control_frequencies` | Hz candidates to hunt, in order; multiples of 12 500 inside the reported span |
| `arbiter_promoted` | the arbiter's own stamp; trunkd passes over it (see *Standby classes*) |

Every key follows the same three-way merge: **omitted** leaves the stored value alone, a
**value** sets it, and an explicit **null** removes the key from the desired state, which
hands the setting back to whatever the recorder's own configuration says.

**`bands`** is an array of strings, each either a band label — `vhf`, `uhf`, `700`, `800`,
`900`, case-insensitive and stored lowercase — or an explicit `"lo-hi"` downlink range in
hertz such as `"856000000-866000000"` (`lo <= hi`). At most 16 entries, each at most 64
characters; duplicates are dropped. It exists because an antenna and its bandpass may cover
several disjoint ranges, and saying so from Keyup beats editing TOML on a headless box.

Its three states are worth spelling out, because two of them look alike:

- `"bands": ["700", "800"]` — record only those ranges, inside the capture span;
- `"bands": []` — **no restriction**: record anything inside the capture span. This is a
  value the recorder applies, and it overrides bands set in its own `trunkd.toml`;
- `"bands": null` — Keyup stops saying anything about this tuner's bands, and the recorder
  keeps whatever its own configuration holds. Not the same as the line above.

Keyup validates every entry before it writes one (422 naming the entry that is wrong), for a
blunt reason: trunkd refuses a *malformed* list by dropping that tuner's whole desired entry,
and a list that does not overlap the tuner's capture span by leaving the tuner exactly as it
is — both quietly, on a poll nobody is watching, with the tuner left reading pending. The
band editor draws the tuner's span with its bands shaded over it and warns before you save a
list that misses the span, but it does not block the save: the recorder decides.

The status push echoes it back: each tuner in `POST /api/recorder/tuners/status` carries
`"bands": [...]` — the list actually in force — and omits the key entirely when the tuner is
unrestricted. That is what the Recorder page shows as *applied*, against the desired list, so
a push that trunkd refused reads as pending rather than as done.

Two things `bands` does **not** do. It never stops a tuner following a control channel —
control tuners publish every grant regardless — so bands on a `control` tuner change nothing
and trunkd logs as much. And it never narrows the capture itself, only which *traffic grants*
inside that capture get recorded.

### Push channel

`WS /api/recorder/stream` exists so a recorder learns about a desired-state change *now*
instead of at its next ~10 s poll. It never carries the desired-state document: it carries a
version number and the word "go fetch", and the recorder answers it with the same
`GET /api/recorder/desired-state` it already makes. **The poll stays.** A dropped frame, a
dropped socket, a Keyup restart or a recorder built before this existed all cost at most one
poll interval, which is exactly today's behaviour — the socket is an accelerator, never the
mechanism.

**Connecting.** `Authorization: Bearer kup_…` on the recorder's own `upload`-scoped token —
the same one the call push, the observation push and the desired-state poll use — or
`?key=<token>` for a tool that cannot set a header. Anything else is closed with **4401**
before a single frame is exchanged. There is no cookie path and no `Origin` check because
there is no cookie for a hostile page to ride on.

**Client → server, first frame**, within 10 seconds of the socket being accepted or it is
closed with **4408**:

```jsonc
{"type": "hello", "instance": "<instance_id>", "recorder_version": "<trunkd version string>",
 "desired_seen": 7}          // or null when the recorder has no last-seen version
```

**Server → client**, JSON text frames, one object per frame:

```jsonc
// immediately after a valid hello
{"type": "hello", "server_time": "2026-09-03T14:22:05.311Z", "heartbeat_s": 15,
 "desired_version": 7, "recorder_block_version": 2}

// whenever this recorder's desired state moves — a tuner desired_version bump, a
// recorder-block change (spool_only / event_log_archive / link), or a rename
{"type": "desired_state", "version": 8, "recorder_block_version": 2}

// every 15 seconds of silence
{"type": "heartbeat", "ts": "2026-09-03T14:22:20.004Z"}

// the answer to a client {"type":"ping"}
{"type": "pong"}
```

`desired_version` is the recorder's **top-level** desired-state version — the same
`version` the poll returns and the same counter any tuner write bumps.
`recorder_block_version` is the `recorder` block's own `desired_recorder_version`. Both are
in the greeting so a recorder can tell *which half* moved without fetching.

If the `desired_seen` in the hello is older than `desired_version` — and `null` counts as
older — a `desired_state` frame follows the greeting immediately, so a recorder that missed
a bump while its socket was down catches up on reconnect instead of waiting for a poll. A
recorder Keyup has never met has no row and therefore no desired state to fetch: it gets the
greeting with zeros, the socket stays open, and the first `desired_state` arrives once its
first status push has created the row.

Unknown client frames are ignored (logged at debug), which is how this contract grows: a
newer recorder may send something an older Keyup does not know, and neither end should drop
the connection over it.

**Bursts are coalesced** to at most one `desired_state` every 250 ms — an operator saving
four tuners is one "go fetch", not four — and redelivery is harmless anyway: trunkd applies
by version-and-diff, so a repeated bump is a no-op.

**How the server notices.** Two ways, and it needs both. Every connection re-reads its own
recorder's two version columns once a second with one indexed `SELECT`, run in a worker
thread so a synchronous session never touches the event loop; that is what makes the channel
work when Keyup runs several uvicorn workers, because the admin write that bumped the version
may have happened in a different process. On top of that, the admin write paths call an
in-process broadcaster after they commit, so a change made in *this* worker is pushed without
waiting for the poll.

**Diagnostics.** `GET /api/admin/recorders` carries `stream: {connected, since,
client_count}` per recorder. That registry lives in the worker's memory, so with several
uvicorn workers it describes **the worker that answered the request** — `connected: false`
means "not connected to me", not "not connected anywhere". Connect and disconnect are logged
at INFO with the instance and the reason.

**nginx.** The route has its own location block in `frontend/nginx.conf`, copying
`/api/live`'s upgrade headers with `proxy_read_timeout`/`proxy_send_timeout 300s` and
`proxy_buffering off` — twenty missed heartbeats before the proxy would cut a socket both
ends think is fine.

### The tuner front end: gain, correction, sample rate, control candidates

Everything in the second half of the table above is a *radio* setting rather than a *what to
record* one, and all of it follows the same three-way merge. Two properties are worth stating
outright, because they are what the operator's edit form is built on.

**Keyup checks a setting against what the tuner last reported**, and refuses a write it can
already tell will not work — `422` with the field named, in the same `field: reason` shape
every other validation failure uses. The rules:

| Keyup refuses | why it is worth refusing rather than sending |
|---|---|
| `ppm` outside ±200, `auto_ppm_limit` outside 0…200 | a hundreds-of-ppm figure is a typo, and applying it walks the tuner off the system |
| a `gains` key the tuner did not report in `gain_elements` | the element names are the driver's; a key the device does not have is a setting that silently does nothing |
| a `gains` value outside that element's `[min, max]` | the range is the driver's own; outside it the value is clamped, not applied |
| `gain` outside the sum of every element's range | which is what an overall gain *is* on a Soapy device — the driver spreads it across the same stages |
| `lna_db` off its 8 dB step to 40, `vga_db` off its 2 dB step to 62 | libhackrf rounds an off-step value silently, so the page would say 25 while the front end sat at 24 |
| a control candidate that is not a multiple of 12 500 Hz, or falls outside `center_hz ± span_hz/2` | a channel off the grid does not exist, and one outside the span cannot be heard |
| `sample_rate` that is not an even whole number of at least four 12.5 kHz channels | the channelizer works in 12.5 kHz units and its polyphase filter needs an even count — so 50 000 Hz up, in steps of 25 000 Hz |
| a Soapy-only field on a tuner reporting `backend: "hackrf"`, or a HackRF-only field on one that is not | the recorder would drop it, and a silently dropped setting is worse than a refused one |

Every one of these is **only** checked where the tuner has actually said something. No
reported `backend`, no backend rule; no reported `gain_elements`, no element check; no
reported span, and a control candidate is checked against the channel grid alone. A tuner
that has never pushed a status is configurable rather than frozen. Clearing a field — an
explicit `null` — is never refused, whatever the tuner turns out to be.

**And trunkd answers with what it could not use.** Each tuner in the status push may carry
`"rejected": {"<field>": "<reason>"}`, and the version is *still acknowledged*: a refusal
does not leave the tuner pending, because the recorder did apply the version, minus that one
field. `GET /api/admin/recorders` lifts the map onto each tuner as `rejected`, and the edit
dialog prints each reason under the input it belongs to — otherwise a refused setting would
leave no trace at all, and the operator would see a clean save that did nothing.

So that the form can be built at all rather than guessed at, the status push should carry:

```jsonc
{ "tuner_key": "airspy-0", "backend": "soapy",
  "gain": 22.5, "gains": {"LNA": 12.0, "MIX": 8.0, "IF": 2.5}, "agc": false,
  "ppm": 1.5, "auto_ppm": true, "auto_ppm_applied": 1.82,
  "bandwidth_hz": 8000000, "rx_port": "LNAW", "settings": {"biastee": "true"},
  "sample_rate": 10000000, "data_follow": false, "control_frequencies": [851250000],
  "gain_elements": [ {"name": "LNA", "min": 0, "max": 15, "step": 1} ],
  "capabilities": { "runtime_gain": true, "runtime_ppm": false,
                    "restart_on": ["sample_rate", "ppm"] },
  "rejected": {} }
```

`gain_elements` is what makes an editable gain form possible: without it Keyup would be
drawing a slider whose ends it had invented, so a tuner that reports none is offered the
overall gain and nothing per-stage. `capabilities.restart_on` names the desired fields whose
change tears the stream down and builds it again; Keyup never refuses one, it puts a
*restarts the tuner* note beside the field, because an operator who knows is choosing and one
who does not is surprised by a gap in the recording. `auto_ppm_applied` is shown read-only
next to the `auto_ppm` toggle — the point of automatic correction is being able to see what
it settled on.

### Link peers

A status push may carry `"link": {…}` — whatever this recorder knows about the peers it is
linked to. Keyup stores it **verbatim and opaque** in `recorders.link`, renders the keys it
recognizes on the recorder's card and decides nothing at all from it, so the shape is
trunkd's to grow without a Keyup migration. The conventional shape:

```jsonc
{ "link": { "peers": [ { "instance": "trunkd-tower", "alive": true,
                         "last_seen": "2026-09-02T18:04:11Z",
                         "claims": ["site:7"] } ] } }
```

The card shows *Link: n peers* and, per peer, whether it is alive, when it was last heard and
what it currently claims. Omitting `link` means **no change**, exactly as omitting `station`
or `uplink` does: a recorder rolled back to an older binary must not wipe what a newer one
reported.

### The recorder's own desired state

Three settings are the *instance's* business rather than a tuner's, and they share one
`recorder` block, one generation counter and one rule:

| field | what it is | where it is described |
|---|---|---|
| `spool_only` | hold the calls in the local spool instead of uploading | *Holding a recorder's uploads*, below |
| `event_log_archive` | ship the daily raw control-channel log to Keyup | *Managing the event-log archive* |
| `link` | the whole recorder-to-recorder link block | *Managing the link* |

Two more fields ride in the same block but are **not** covered by its counter, because they
are derived from Keyup's own settings and directory rather than from anything an operator
wrote on this recorder: `broadcastify` (upload this recorder's own calls to Broadcastify
Calls) and `skip_talkgroups` (below). Each carries its own 12-hex `revision`, and the
recorder re-reads one when its revision changes rather than when `recorder.version` moves.

**`skip_talkgroups`, since 2026-09-09.** `{"tgids": [1755, …], "revision": "9f2c41a80b7e"}`,
or `null` — which is what every deployment gets until an operator makes a channel skippable.
These are talkgroups the recorder should **drop without uploading**: Keyup has `record` off
(do not keep the audio), `transcribe` off (do not keep the words) and no export service
selecting them, so an upload would be a file Keyup stores and deletes having done nothing
with it. Keyed by tgid alone — a recorder knows the number it decoded, not which of Keyup's
systems it belongs to — so a number appears only when every talkgroup row carrying it is
skippable. It is an *exclusion* list on purpose: a recorder that cannot reach Keyup, or one
on a build that has never heard of the field, keeps uploading everything, and Keyup stores
the metadata and deletes the audio itself.

The rule is the same for all three of the counted fields and is worth stating once: **every one of them is
three-valued, and `null` is the important value.** `null` means *Keyup has no opinion* — the
recorder keeps whatever its own `trunkd.toml` or front panel says — and it is the default for
every recorder nobody has touched, which is why silence from Keyup never reconfigures a
recorder. A setting Keyup manages is a setting that can no longer be fixed on the box, so
taking one over is a decision, never a side effect of opening a dialog.

They share `desired_recorder_version`, which is bumped **once** by a write that changes any of
them — the recorder applies the block as a whole and echoes one number back. A write of the
value already stored bumps nothing, so a form that resubmits itself never leaves a recorder
pending for ever.

### Holding a recorder's uploads

A recorder can be told to stop uploading and **spool** instead: it keeps recording, keeps
every call on its own disk, and sends the lot when the hold is released. It is what you want
while the link is metered, while Keyup is being upgraded, or while somebody is working on the
network the recorder sits behind — the alternative, stopping the recorder, loses the traffic
outright.

trunkd has had `spool_only` in its own configuration and on its front panel all along. What
is new is that Keyup can set it at runtime, and that a spooling recorder still registers,
still polls and still reports — a held recorder is a *quiet* recorder, not an absent one.

The desired-state poll carries it in the `recorder` block:

```jsonc
{
  "version": 12,                                  // any tuner write moves this
  "tuners": { "rtl-0": { "version": 3, "config": {} } },
  "recorder": { "version": 2, "spool_only": true }  // and only a recorder write moves this
}
```

The two versions are separate on purpose. `recorder.version` is bumped **only** by a write of
`spool_only`, so the counter the recorder echoes back cannot be shifted underneath it by an
unrelated tuner edit.

`spool_only` is three-valued, and the third value is the important one:

- `true` — hold: spool every call rather than uploading it;
- `false` — upload, whatever the recorder's own configuration says;
- `null` — **Keyup has no opinion.** The recorder keeps whatever its own `trunkd.toml` or its
  front panel holds. This is the default for every recorder nobody has touched, which is why
  silence from Keyup never switches a recorder's uploads on or off.

The status push answers with two blocks of its own, both optional:

```jsonc
{
  "instance_id": "rec-01",
  "tuners": [ ... ],
  "uplink": {
    "spool_only": true,
    "source": "keyup",              // config | keyup | panel — where the setting came from
    "spooled_calls": 46,
    "spool_bytes": 39845888,
    "url": "https://keyup.example/api"
  },
  "recorder_applied": { "version": 2, "spool_only": true }
}
```

`uplink` is stored verbatim on the recorder row and is what the Recorder page reads: it shows
what the recorder is *actually* doing, not what was asked of it. `recorder_applied.version` is
the echo, exactly as `applied_version` is for a tuner — while it lags `recorder.version` the
page shows the hold as **pending**.

Both blocks are **optional in both directions**. A recorder that omits them keeps whatever was
last reported rather than clearing it, so an older binary neither reports the hold nor has its
stored report wiped; and a recorder that never sends `recorder_applied` simply reads as pending
for as long as Keyup has an opinion it has not heard back about.

An operator sets it with `PUT /api/admin/recorders/{id}` (admin scope), the same route as the
standby policy:

```jsonc
{ "spool_only": true }    // hold
{ "spool_only": false }   // release; everything spooled goes up behind it
{ "spool_only": null }    // withdraw Keyup's opinion; the recorder keeps its own setting
```

`spool_only` is the one field on that route whose explicit `null` is a *write* rather than
"leave it alone" — the key's presence is what decides, so omit it entirely to change only the
standby policy. Every write that actually changes the value bumps `desired_recorder_version`;
saving the same value twice does not, so a no-op save never makes the recorder re-apply.

On the Recorder page each card carries the strip: *Uploading* or *Holding uploads (46 calls,
38.0 MiB spooled)*, where the setting came from, a **pending** badge with the direction it is
heading, and one button. Holding is immediate; releasing is confirmed, because it starts an
upload of everything that piled up.

### Managing the event-log archive

trunkd writes every decoded control-channel message to a local `<date>.jsonl.gz` and rolls one
file per UTC day. Whether it *ships* that file here has always been a line in its own config;
`event_log_archive` is Keyup asking for it instead:

```jsonc
{ "event_log_archive": true }   // ship the daily log
{ "event_log_archive": false }  // keep it on the recorder
{ "event_log_archive": null }   // withdraw Keyup's opinion; the recorder keeps its own
```

The recorder answers in the `event_log` block of its status push, which is stored verbatim:

```jsonc
"event_log": {
  "archive": true,              // what it is actually doing
  "source": "keyup",            // config | keyup — which setting won
  "pending": 2,                 // days still waiting to be uploaded
  "last_archived": "2026-09-01",
  "last_error": null
}
```

`source` is the interesting field. A recorder shipping logs because its own config says so and
one shipping them because Keyup asked look identical otherwise, and only the first keeps doing
it after Keyup withdraws its opinion. What actually *arrived* is `GET /api/admin/event-logs`,
not this — the recorder's account of its own backlog and the receipts on this side disagreeing
is itself the fact worth seeing.

### Managing the link

Two recorders that hear the same site can be **linked**: they tell each other what they have
claimed, so one records a grant and the other does not spend a slot on a second copy of it.
The whole block is Keyup's to manage, or none of it:

```jsonc
"link": {
  "enabled": true,
  "bind": "0.0.0.0:9110",              // host:port this recorder listens on
  "peers": ["rec-02:9110"],            // host:port of every other member
  "key": "0f3c…",                      // the shared secret, ≥ 16 characters
  "hello_interval_s": 5,               // 1…60
  "accept_remote_grants": true,        // act on grants a peer published
  "publish_grants": true,              // tell the peers about ours
  "defer_to_claims": true              // leave a call to whoever claimed it first
}
```

It is written and applied **as a unit**, never merged field by field: a peer list saved
without the key it authenticates with is not a link, and half-applied link settings are the
kind of state nobody can reason about afterwards. An explicit `null` stops managing it and
hands the whole thing back to the recorder's own configuration.

`PUT /api/admin/recorders/{id}` refuses a bad block with a `422` naming the field, in the same
`field: reason` shape as the tuner checks — `link.bind`, `link.peers[2]`, `link.key`:

- `bind` and every peer must be `host:port`: a hostname, an IPv4 address, or an IPv6 one in
  **brackets** (`[2001:db8::1]:9110` — unbracketed, its own colons and the port's cannot be
  told apart), with a port of 1…65535;
- peers must not repeat — a list with the same box in it twice is a typo in one of them, and
  silently deduplicating would hide which;
- an **enabled** link needs at least one peer. A disabled one does not: filling in the bind
  address and the key before the second box exists is how a link is staged;
- the shared key must be at least 16 characters, and `hello_interval_s` must be 1…60.

**The key.** Every member of the mesh authenticates with one shared secret, so the same key
has to be in all of them. Keyup stores it in clear — it is an HMAC secret it must be able to
hand back to the recorder, not a password to verify — and keeps it out of sight in the API
rather than in the database:

| route | what it carries |
|---|---|
| `GET /api/recorder/desired-state` | the key **in clear**, to the recorder that must hold it |
| `GET /api/admin/recorders` (and every operator read) | `key_set` and `key_fingerprint` — the first 8 hex of its sha256 — and never the key |
| `GET /api/admin/recorders/{id}/link/key` | the key in clear, admin only, and the read is written to the activity log |
| `POST /api/admin/recorders/{id}/link/generate-key` | 32 fresh random bytes as 64 hex characters, **stored nowhere** |

Generating is deliberately not saving: the operator pastes the same generated key into every
recorder's dialog, and each becomes a member when its own settings are saved. A route that
stored it would break a working link the moment someone pressed the button and thought better
of it.

`key` is also the one field that may be **omitted** from an update, which keeps the key
already stored — the edit dialog shows the secret masked and does not have it unless the
operator pressed *Reveal*.

The fingerprint is on the recorder card as well as in the dialog, because "do these two
recorders hold the same key" is a question about a *pair*, and the roster is the one place
both of them are on screen at once. Two recorders whose fingerprints differ will never link.

### What the recorder says back

Alongside `recorder_applied.version`, a recorder new enough to know about all this echoes what
it applied and what it could not:

```jsonc
{
  "recorder_applied": {
    "version": 7,
    "spool_only": null,
    "event_log_archive": true,
    "link": { "enabled": true, "bind": "0.0.0.0:9110", "peers": ["rec-02:9110"],
              "hello_interval_s": 5, "accept_remote_grants": true, "publish_grants": true,
              "defer_to_claims": true, "key_set": true, "key_fingerprint": "3f9a1c07" }
  },
  "rejected": { "link": "built without the link feature" },
  "capabilities": { "recorder": { "restart_on": ["link"] } }
}
```

The echoed `link` carries `key_set`/`key_fingerprint` and **not** the key: trunkd does not
send the secret back, and a second copy of it arriving over the status push is something
Keyup would rather not have to think about. The fingerprints matching is what proves the two
ends agree.

`rejected` is the recorder-level twin of the per-tuner map, and it exists for the same reason:
the recorder acknowledges the version whether or not it could use every field in it, so the
*pending* badge clears and this map is the only trace a refusal leaves. `{}` is a real value —
"the last apply refused nothing" — and is how a stale warning is cleared off the page.
`capabilities.recorder.restart_on` names the fields that cost the process a restart; it is
rendered as a warning beside them and refuses nothing.

All three blocks are **optional in both directions**. A recorder that sends none of them is an
older binary, and is stored exactly as it always was.

### Per-tuner: `remote_grants`

One tuner field belongs to the link: `remote_grants` lets a tuner act on the grants a peer
published rather than only the ones it decoded itself. It is an ordinary three-valued tuner
desired field, written with the rest of them at
`PUT /api/admin/recorders/{id}/tuners/{key}` — but it needs the *recorder's* link to be up and
accepting them, and a recorder with no link answers with the tuner `rejected` map rather than
silently doing nothing.

### Renaming a recorder

A recorder's `instance_id` is the name it files everything under, and boxes get renamed —
`rec-01` becomes `rec-east` when somebody decides the name should say where the machine is.
`PUT /api/admin/recorders/{id}` takes `instance_id` and, in the same transaction,
rewrites the instance text on everything already filed under the old one: `radio_events`,
`recorder_observations`, `event_log_archives` and a call's `source_instance`. A half-done
rename is the worst outcome available — the roster showing the new name and the whole archive
still under the old one, with nothing to say they belong together.

A name another recorder already holds is a `409` rather than a merge, and the message names
the route that clears the way. That case is almost always a stale empty row left by a box that
was renamed on its own side first; `DELETE /api/admin/recorders/{id}` forgets it, refusing
with a `409` of its own while the recorder is still reporting (`?force=true` overrides, though
a live recorder simply comes back on its next poll).

**Rename the recorder too.** Keyup renaming its row does not reconfigure the box: one that
keeps posting the old `instance_id` creates a second row under it on its very next poll.

---

## Priority and flow control

Live calls and a bulk migration of the old sdrtrunk archive share one transcription queue.
Left alone the migration wins — it can push a thousand calls a minute and the queue is FIFO
within a priority — so the push path takes a *class* per upload and refuses what it cannot
absorb, instead of quietly building a queue nobody can drain.

**Nothing changes for a recorder that does not opt in.** sdrtrunk and trunk-recorder send no
`priority`, keep the byte-exact plain-text bodies they string-match on, and are never refused
for a backlog reason. Sending a `priority` *is* the opt-in to the JSON contract below;
`trunkd` and `tools/migrate_sdrtrunk.py` both send one.

### The class

`priority` is a form field on `POST /api/call-upload` (or the `X-Keyup-Priority` header —
the form wins when both are present):

| `priority` | `transcription_jobs.priority` | band | for |
|---|---|---|---|
| `urgent` | `10` | live | a call that must be transcribed now |
| `live` *(default)* | the talkgroup's own priority (100 unless set) | live | live recorders |
| `backlog` | `500` + the talkgroup's priority | backlog | archive migration |
| `bulk` | `900` | backlog | a low-value feed that may wait indefinitely |
| `1`…`999` | as given | live below 500, backlog at or above it | anything else |

Anything at or above **500** is the *backlog band*: it is refused early, it queues behind
every live call, and at most `STT_BACKLOG_WORKER_SHARE` (default 0.5) of the transcription
threads may hold one at a time, so a burst of live calls always finds a free worker.
`POST /calls/{id}/transcribe` still enqueues at priority 1 — an operator asking beats
everything.

The class is stored on the call as `calls.ingest_priority` (NULL for anything that arrived
without one), so "which rows came from the migration" stays answerable in SQL long after the
job is gone.

**An accepted backlog call is stored immediately** — searchable, playable, in the archive,
visible to API consumers. Only its *transcription* waits behind live traffic.

### Refusals

The decision is taken from the form fields, **before the audio part is read**, so a refusal
costs the client nothing but the round trip.

| situation | status | applies to |
|---|---|---|
| free space under `AUDIO_ROOT` below `UPLOAD_MIN_DISK_FREE_BYTES` | **507** | every class |
| live band at or over `UPLOAD_MAX_QUEUE_LIVE` | **429** | `live`, `urgent`, integers < 500 |
| backlog paused by the operator | **429** | `backlog`, `bulk`, integers ≥ 500 |
| backlog band at or over `UPLOAD_MAX_QUEUE_BACKLOG` | **429** | ” |
| queue drain estimate over `UPLOAD_BACKLOG_MAX_ETA_SECONDS` | **429** | ” |
| `UPLOAD_BACKLOG_MAX_CONCURRENCY` uploads already in flight | **429** | ” |

Every refusal carries `Retry-After` in seconds (clamped 5–600 for queue pressure, 300 for
disk) and, for a client that sent `priority`, a JSON body:

```json
{
  "error": "queue_full",
  "class": "backlog",
  "detail": "312 backlog jobs queued, at or over the 300 cap",
  "queue": {"live": 41, "backlog": 312, "audio_seconds": 4210.5, "eta_seconds": 702.0,
            "accepting": {"live": true, "backlog": false}, "paused_backlog": false},
  "retry_after": 120
}
```

`"error"` is `queue_full` for a 429, `insufficient_storage` for a 507 and `bad_priority` for
a 400 (an unusable `priority` value). A client that sent **no** `priority` gets plain text
instead — `queue full, retry later` or `insufficient storage` — because that is what a
string-matching recorder can act on.

### Headers

Every upload response past authentication — accepted, duplicate, refused — carries the
gauge, so a well-behaved client never has to poll anything:

```
X-Keyup-Queue-Live: 41
X-Keyup-Queue-Backlog: 312
X-Keyup-Queue-Audio-Seconds: 4210
X-Keyup-Queue-Eta-Seconds: 702
X-Keyup-Accepting-Backlog: no
```

`GET /api/upload/flow` returns the same object for a client that wants to look before it
bursts. It takes the `upload` scope as a bearer token or as `?key=` — the same token the
form carries, so a recorder needs no second credential:

```sh
curl -s 'http://192.0.2.10:8095/api/upload/flow?key=kup_…' | jq
# {"live":41,"backlog":312,"audio_seconds":4210.5,"eta_seconds":702.0,
#  "accepting":{"live":true,"backlog":false},"paused_backlog":false}
```

`GET /api/status` carries the same block as `queue`.

### Not sending what we already have

`POST /api/call-upload` can only answer `duplicate call rejected` once it has read the whole
multipart body, so a client re-walking an archive Keyup mostly holds pays the full transfer
to be told nothing was needed. `POST /api/upload/exists` is the way around it: post
`{"sha256": ["<hex>", …]}` — up to 500 digests, 422 for more or for anything that is not a
64-character hex digest — and it answers `{"present": [...]}` with the subset `calls.sha256`
already holds. Same `upload` scope, bearer or `?key=`; one indexed `SELECT`, nothing queued
and nothing stored, so it is not an upload as far as flow control is concerned and is never
refused for backlog reasons. `tools/migrate_sdrtrunk.py` asks in batches of 200 before every
push and writes the hits straight to its ledger (`--no-precheck` turns it off).

```sh
curl -s 'http://192.0.2.10:8095/api/upload/exists?key=kup_…' \
  -H 'Content-Type: application/json' -d '{"sha256":["3b1f…","c0de…"]}'
# {"present":["3b1f…"]}
```

### Examples

```sh
# a live call, explicitly (opts in to the JSON contract)
curl -sD- -o- http://192.0.2.10:8095/api/call-upload \
  -F key=kup_… -F priority=live -F talkgroup=1701 -F dateTime=1756500000 \
  -F 'audio=@call.mp3;type=audio/mpeg' -F audioName=call.mp3

# one archived recording, as backlog
curl -sD- -o- http://192.0.2.10:8095/api/call-upload \
  -F key=kup_… -F priority=backlog -F talkgroup=1753 \
  -F 'audio=@20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9150041.mp3;type=audio/mpeg' \
  -F audioName=20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9150041.mp3
# HTTP/1.1 429 Too Many Requests
# Retry-After: 120
# X-Keyup-Accepting-Backlog: no
# {"error":"queue_full","class":"backlog", …}

# the header form, for a client that finds it easier
curl -H 'X-Keyup-Priority: bulk' …
```

### Operating it

```sh
curl -sX POST http://192.0.2.10:8095/api/admin/upload/pause-backlog  -H 'Authorization: Bearer kup_…admin'
curl -sX POST http://192.0.2.10:8095/api/admin/upload/resume-backlog -H 'Authorization: Bearer kup_…admin'
```

The switch is the `upload_pause_backlog` runtime setting (Admin → Settings → Upload), so it
survives a restart and every API process sees it. The caps are settable there too:

| setting | default | |
|---|---|---|
| `UPLOAD_MAX_QUEUE_LIVE` | `5000` | live-band jobs above which even a live push is refused |
| `UPLOAD_MAX_QUEUE_BACKLOG` | `300` | backlog-band jobs that may be queued at once |
| `UPLOAD_BACKLOG_MAX_ETA_SECONDS` | `900` | drain estimate above which backlog is refused |
| `UPLOAD_BACKLOG_MAX_CONCURRENCY` | `4` | simultaneous in-flight backlog uploads |
| `UPLOAD_MIN_DISK_FREE_BYTES` | `5 GiB` | free-space floor under `AUDIO_ROOT` |
| `UPLOAD_PAUSE_BACKLOG` | `false` | the operator switch |
| `UPLOAD_LOCK_TIMEOUT_MS` | `15000` | how long one upload may wait on a database lock (`0` waits forever) |
| `STT_BACKLOG_WORKER_SHARE` | `0.5` | share of `STT_WORKERS` that may hold backlog jobs |

Migrating the sdrtrunk archive end to end: **[docs/MIGRATION.md](MIGRATION.md)**.

### Threading and slow uploads

`POST /api/call-upload` is a **synchronous** route — a plain `def`, which FastAPI runs in
anyio's worker threadpool — and it is written that way on purpose. Nothing in the upload
path is asynchronous underneath: the session is sync SQLAlchemy on psycopg, the audio is
hashed and probed with blocking reads (mutagen, and `ffprobe` when mutagen will not read the
file), the body is moved with `os.rename`, and the dedupe takes Postgres advisory and row
locks that can wait. Written as an `async def`, every one of those ran on the event loop,
which means one slow upload was not a slow upload but an outage: on **2026-09-03 20:46Z** a
single backlog push took 122,682 ms, and for that whole window the API logged nothing and
answered nothing — every recorder POST timed out, and 38 of them failed at once in the second
it came back.

Nothing is given up by leaving the loop. Starlette parses the `multipart/form-data` body and
spools the audio part to a `SpooledTemporaryFile` (in memory to a megabyte, on disk past it)
*before* the route function is entered, so by the time any handler code runs there is no
network streaming left to do — `stream_to_path` is a file-to-file copy however it is spelled,
and it still copies a megabyte at a time with the running-size abort. The in-flight backlog
semaphore (`UPLOAD_BACKLOG_MAX_CONCURRENCY`) is a `threading.BoundedSemaphore` and was always
written for a threadpool.

**What this costs.** A sync route occupies one threadpool worker for the whole request.
anyio's default is **40**, shared by every sync route in this API, so 40 uploads can be
in-flight at once before a 41st waits for a worker rather than for the database. That is well
clear of `UPLOAD_BACKLOG_MAX_CONCURRENCY` (4 by default) plus the live band, and it is left at
the default deliberately: raising it trades a bounded queue for more simultaneous database
connections, and the pool, not the threadpool, is the next thing to run out. If a deployment
ever does need more, raise it *and* the SQLAlchemy pool together —
`anyio.to_thread.current_default_thread_limiter().total_tokens` at startup, or uvicorn's
`--limit-concurrency` on the other side of it — and measure before and after.

**Timing.** Every upload is timed per stage: `receive` (copy the spooled body into
`uploads/.incoming/`), `parse` (ID3, the audio probe, sha256), `move` (the rename into the
call's own date partition), `lookup` (the channel lock and the dedupe/reconcile match) and
`store` (the row, the directory upserts, the queue rows and the commit). One that runs past
five seconds logs a single WARNING naming the slowest stage and rendering all of them:

```
call-upload: slow upload 122.7s — slowest stage lookup at 122.4s
  (receive=41ms parse=88ms move=2ms lookup=122412ms store=0ms) — audioName='…' [sdrtrunk]
```

which is the line the incident had no way to produce.

**Lock waits.** `lookup` and `store` take a channel advisory lock, a conversation advisory
lock, and row locks on the talkgroup, site and radio rows whose `ON CONFLICT DO UPDATE`
counters they move. Any of those can already be held by a *different* process — the file
watcher, or `keyup reconcile`'s batch pass, which walks up to 100,000 calls in one
transaction and holds every row it has updated until it commits — and Postgres waits for a
lock forever unless it is told not to. The upload's write transaction therefore sets
`SET LOCAL lock_timeout` from `UPLOAD_LOCK_TIMEOUT_MS` (15 s by default; `0` restores the
unbounded wait). A push that hits the cap is answered with the `import failed: …` 500 the
endpoint has always used for a failed write, with its temp body removed and nothing
half-written — and a recorder spools and retries, so that costs one retry where the wait
costs a held connection.

This is not an index problem, and it is worth saying so because it looks like one. Every
query the path runs per upload is index-backed at full scale: on a 1.1 M-row `calls`
table the sha256 dedupe is an `ix_calls_sha256` index scan (0.17 ms), the reconcile candidate
window an `ix_calls_started_at` range scan bounded by `MAX_CALL_SECONDS` (0.45 ms), the fuzzy
dedupe the same (0.06 ms), and the `file_path` uniqueness walk a `calls_file_path_key` scan
(0.37 ms). Contention, not planning, is what makes an upload take two minutes.

---

## Tokens and scopes

Only the push paths need a credential, and the only scope they need is `upload`:

```sh
docker compose exec api keyup token create trunk-recorder --scopes upload
docker compose exec api keyup token create trunkd --scopes upload
docker compose exec api keyup token list
docker compose exec api keyup token revoke <id>
```

`upload` permits exactly the recorder's own endpoints and nothing else: `POST /api/call-upload`,
`GET /api/upload/flow`, `POST /api/upload/exists`, `GET /api/recorder/desired-state`, `POST /api/recorder/tuners/status`,
`POST /api/recorder/spectrum`, `POST /api/recorder/observations`, `POST /api/recorder/events`,
`POST /api/recorder/event-log`, `POST /api/recorder/broadcastify/claim` and the `WS /api/recorder/stream`
WebSocket. It cannot read the
archive, edit anything, or stream the live feed. Give each recorder its own token so one can be
revoked without silencing the others — the label shows up in the API log line for every
call it imports. (`admin` also satisfies the endpoint, but a recorder should never hold it:
the key is in the body of every request it makes.)

## Checking it worked

On the Keyup host itself (`API_PORT`, 8096 by default, listens on the loopback only):

```sh
curl -s http://localhost:8096/api/admin/ingest | jq          # ledger counts, last run
curl -s 'http://localhost:8096/api/admin/ingest/files?status=failed' | jq  # and why
curl -s 'http://localhost:8096/api/calls?page_size=5' | jq '.items[] | {started_at, tgid, ingest_source, time_source}'
```

`ingest_source` tells you which recorder a call came from (`file`, `upload`,
`trunk_recorder`, `trunkd`) and `time_source` how much to trust its start time (`sidecar` and `upload`
are exact; `file_write` is within about a second). A call the ingest could not make sense of
is in the ledger with the reason, and `POST /api/admin/ingest/files/{id}/retry` (admin token)
re-runs it once you have fixed the cause.

## Running both recorders at once

Nothing stops you, and the archive is better for it — trunk-recorder sees the sources and
the flags, sdrtrunk decodes some things it does not. Two things to know:

- **Use one `AUDIO_ROOT` with a subdirectory each.** The walk is recursive and the two
  filename shapes are told apart by their own contents, not by where they sit, so
  `AUDIO_ROOT/sdrtrunk/` and `AUDIO_ROOT/trunk-recorder/` both just work.
- **Give them different system names** unless they really are recording the same system.
  Keyup keys the directory on the system name (sdrtrunk's `System`, trunk-recorder's
  `shortName`), so two recorders that call it the same thing share one set of talkgroups and
  radios — which is what you want when they are covering the same network, and confusing
  when they are not.

When both cover the same system, the dedupe does the rest: the same call caught twice is one
row plus one marked duplicate, and `include_duplicates=1` is how you go looking for the
second copy.

## Several recorders on one site: standby and best source

Two or three trunkd instances can hear the same P25 site. Running them all at once works
and wastes most of it: every instance uploads every call and the archive spends its dedupe
budget throwing the copies away. Two features exist for that case, and **both ship off or
inert by default** — a single-recorder deployment behaves exactly as it always did.

### Standby classes and the arbiter

Every recorder has a `standby` class and a `priority`, set from Admin → Recorder or with
`PUT /api/admin/recorders/{id}`:

| class | what it means |
|---|---|
| `hot` | records whenever it is healthy. The arbiter never touches it. |
| `warm` | a spare: registered, tuners disabled, and the only class the arbiter will switch **on**. |
| `cold` | never touched automatically — a bench radio, a laptop, a box you are testing with. |

`priority` orders the spares and **lower is preferred**; ties go to the older row.

Turn the arbiter on with the `arbiter_enabled` setting (Admin → Settings → Recorders; off by
default). Every 30 seconds it looks at each site a hot recorder reports and:

- **promotes** — when *every* hot recorder for the site has gone stale (no status push for
  `arbiter_stale_seconds`, 60 by default), it enables the tuners of the best healthy warm
  spare that knows the site, and logs `recorder.promoted` in the activity feed;
- **demotes** — once *every* hot recorder for the site has reported continuously for
  `arbiter_recovery_seconds` (300 by default), it disables them again and logs
  `recorder.demoted`.

Promotion is immediate and demotion waits, on purpose: a site off the air costs traffic
every second, and a recorder that comes back and drops out again would otherwise hand the
site to and fro every half minute.

Four things it will not do, ever:

- touch a `cold` recorder, for any reason;
- switch off a tuner **you** enabled by hand. The arbiter stands down only what it switched
  on itself, and it knows which that is because it says so on the tuner: promoting writes
  `"arbiter_promoted": true` into that tuner's desired state alongside `enabled`, and
  demoting takes it back off again. The recorder ignores the key — trunkd reads the settings
  it knows by name and passes over the rest — and it is what keeps a spare that keyup
  promoted for one site and *you* switched on for another from losing your half when the
  first site's hot recorder comes back. `recorders.promoted_at` is the coarser half of the
  same fact: it is set while the arbiter has switched anything on for this recorder and
  cleared once no stamped tuner is left. Turning a promoted recorder `cold` disowns the
  promotion, so it is then left exactly where it is;
- change anything but the per-tuner `enabled` flag and that stamp — role, focus site, center
  frequency, bands and spectrum are yours, and survive any number of promotions;
- write anything at all on a pass that changes nothing. A quiet night reads as a quiet night.

**Run exactly one full `keyup worker`.** The arbiter is a job on the worker's scheduler, and
every write it makes is serialized behind `SELECT … FOR UPDATE` on the recorder row, so a
second worker converges rather than corrupts — but it is designed for one. Two of them tick
independently and each keeps its own recovery clocks in memory, so a spare can be stood down
by whichever process happened to reach the window first, and the activity log reads as two
jobs arguing. If you scale the deployment out, run the extra processes as transcription
workers only.

Staleness is measured against the recorder's last **status push** (`reported_at`), not
against a bare desired-state poll: a process that is up but hearing nothing is down as far
as coverage goes. The recovery clock lives in the running process, so restarting keyup makes
a promoted spare wait one more full window before standing down — late rather than early,
which is the safe direction. The promotion itself is a column, so a restart never orphans an
enabled spare.

### Best source

An upload may carry a `sourceQuality` form field — a JSON object saying which receiver this
is and how well it heard the call:

```jsonc
{ "instance": "rec-01", "voice_frames": 1234, "bad_frames": 7, "rssi_dbfs": -38.4 }
```

It is stored on the call (`source_instance`, `source_quality`) and shown on the call detail
page. Anything unparseable is ignored outright: a garbled quality report never costs a
recorder its call.

When a second receiver pushes a transmission the archive already has, and the old dedupe
would have answered `duplicate call rejected`, keyup now compares the two — provided
`best_source_enabled` is on (it is by default):

```
score = voice_frames − 5 × bad_frames
```

If the incoming copy names a **different** instance and beats the stored one by more than
5 %, the stored call takes the new audio (path, size, duration), the old file is deleted
after the swap commits, the call is queued for transcription again, `call.better_source` is
logged, and the push is answered `Call imported successfully.` Anything less — the same
recorder re-sending, a report on only one side, a margin inside the noise — is the duplicate
rejection it always was. A report keyup cannot store is treated as no report at all: a
non-finite number anywhere in the object (`NaN`, `Infinity` — `json` parsers accept them,
Postgres `jsonb` has no such value) drops the whole report, and the call is ingested normally
without one.

**A call that has already been exported does not go out again with the new audio.** The
export ledger is `UNIQUE (call_id, service)` and a row that reached `uploaded` stays
`uploaded`, so Broadcastify Calls and OpenMHz keep the copy they were given: neither service
takes a replacement for an upload it has accepted, and re-sending would put the same
transmission on the feed twice. The archive holds the better recording; the feed holds
whichever arrived first. Re-discovery does not change that either — it inserts
`ON CONFLICT DO NOTHING` against the same unique constraint — so putting the better copy on
a feed means deleting that call's `call_exports` row by hand and letting the next pass pick
it up ([EXPORTERS.md](EXPORTERS.md)).

This only applies where the second copy would otherwise be **discarded**, i.e. with
`reconcile_enabled` off. With reconciliation on (the default), the second copy is stored as
its own row and its own scoring decides which is primary — nothing is thrown away there, so
there is nothing for this to rescue ([RECONCILIATION.md](RECONCILIATION.md)).

## Site observations from trunkd

trunkd also posts what it hears about the site's channel plan — live and backup control
channels, adjacent sites, the band plan and every granted frequency — to
`POST /api/recorder/observations` with the same `upload`-scope token. Keyup compares that with
RadioReference and the recorder's own configuration and reports undocumented, missing and
drifted channels: see [CHANNEL_AUDIT.md](CHANNEL_AUDIT.md).

**Send `"instance"` with it.** `recorder` names the *software* ("trunkd"); `instance` is the
recorder's own `instance_id` — the machine, the same identity the desired-state poll and the
status push use. Two boxes watching one site write into the same rows and are told apart by
nothing else, which is exactly the question worth asking when the two disagree about a
channel. It is optional, and a document without one is stored with a null instance rather
than a guess. `GET /api/sites/{site_id}/message-mix?instance=…` folds only the windows one
instance posted, so two recorders on a site can be compared rather than summed.

## Radio events from trunkd

The third thing a recorder posts. `POST /api/recorder/events`, the same `upload`-scope token
again, carries what the control channel says about *subscribers* — unit registration and
de-registration, group affiliation and affiliation queries, location registration, SNDCP
data grants and pages, **unit-to-unit (private) call grants**, and the
acknowledge/deny/queued responses to all of them.

A private-call grant is `kind='private_call'` with `radio_id` the caller and
`target_radio_id` the radio being called, plus the `frequency_hz` it was granted:

```jsonc
{ "at": "2026-08-29T20:09:45.100Z", "kind": "private_call", "opcode": 4,
  "radio_id": 9100001, "target_radio_id": 9100109,
  "frequency_hz": 853750000, "result": "accept" }
```

Send it whether or not you go on to record the call: the grant is an observation about who
was talking to whom, and it is worth having even when nothing was captured. When a recording
does follow, push it to `/api/call-upload` with `call_kind=unit` (*Unit-to-unit calls*
above) — the two are kept in separate tables on purpose, and the radio detail page shows them
side by side. `unit_to_unit`, `unit_call` and `private_call_request` are accepted spellings.

```jsonc
{ "recorder": "trunkd", "recorder_version": "0.0.1",
  "system": { "label": "Example System", "wacn": 101010, "system_id": 501, "nac": 659 },
  "site":   { "label": "Central", "rfss": 10, "site": 1 },
  "events": [ { "at": "2026-08-31T12:00:00.123Z", "kind": "group_affiliation", "opcode": 40,
                "radio_id": 9101234, "talkgroup": 1701, "result": "accept" } ] }
```

Batches of at most 1 000. Post them every few seconds, spool them on failure exactly as you
spool calls, and resend anything you could not confirm: the batch is idempotent on
`(site, at, kind, opcode, radio_id, talkgroup)` and a resend answers
`{"stored": 0, "duplicates": n}`. One unusable event never costs the batch — the reply counts
`stored`, `duplicates` and `rejected` instead of refusing — and a `kind` Keyup does not know
is stored as `other` with the opcode kept, so you never have to wait for a Keyup release to
start sending a message you have just learned to decode.

Two more things a recorder can send while it is there:

* `sourceWacn` / `sourceSystem` on `POST /api/call-upload` — P25 Link Control opcode 9 for
  the call's source radio, which fills `radios.wacn` / `home_system_id` and turns
  "radio 5701025" into "a system-57 radio roaming onto this site".
* `message_mix`, `unknown_exemplars`, `link_control_mix`, `data_channel` and `lsd_nonzero`
  on `POST /api/recorder/observations` — the window's message *census*. Send the histogram,
  never the firehose: one exemplar per unnamed vendor message per window is enough.

`"instance"` belongs on this batch too, for the same reason it belongs on an observation: it
is stamped on every row the batch stores, and
`GET /api/radios/{radio_id}/events?instance=…` and
`GET /api/sites/{site_id}/radio-events/summary?instance=…` filter on it. Note that the
idempotency key deliberately does **not** include it — when two recorders decode the same
message at the same instant the archive keeps one row, stamped with whichever arrived first,
which is the right answer for a duplicate and the reason the filter is a narrowing rather
than a partition.

All of it: [RADIO_EVENTS.md](RADIO_EVENTS.md).

## Archiving a recorder's event log

The fourth thing a recorder posts, and the only one that is a *file*.

trunkd writes every decoded control-channel message to a local `<date>.jsonl.gz` and rolls
one file per UTC day. Keyup deliberately never stores that firehose as rows — 2.3 million
messages a day on a busy site, of which `radio_events` keeps the handful that name a
subscriber and `recorder_observations` keeps the per-window histogram
([RADIO_EVENTS.md](RADIO_EVENTS.md); the measurement is trunkd's [NON-VOICE-DATA §5](https://github.com/pwsh/trunkd/blob/main/docs/NON-VOICE-DATA.md)). But a recorder's disk is not an archive: it fills,
and the one artifact that could answer a question nobody has thought to ask yet is exactly
the one that gets rotated away. So ship the day whole.

```
POST /api/recorder/event-log        Authorization: Bearer kup_…   (upload scope)
multipart/form-data:
  instance=trunkd-garage
  date=2026-09-01                   the UTC day the log covers, YYYY-MM-DD
  sha256=<64 hex>                   of the file
  bytes=6815744                     of the file
  file=@2026-09-01.jsonl.gz

200 {"stored": true, "bytes": 6815744}
```

The digest and the length are **verified while the body streams to disk**, and the file is
moved into place only if both match; a truncated upload leaves nothing behind. At most
100 MB per day (`EVENT_LOG_MAX_BYTES`), refused with `413`.

| answer | |
|---|---|
| `200` | stored, and the byte count is what was actually written and hashed |
| `409` | that `(instance, date)` is already archived **with the same digest** — you succeeded and lost the response; stop retrying |
| `200` on a repeat with a *different* digest | the day is **replaced**, which is what a recorder that rebuilt a partly-written day is asking for |
| `422` | bad `sha256`, wrong `bytes`, a `date` that is not `YYYY-MM-DD`, or an `instance` that is not a usable directory name |
| `413` | over the size limit |

The bytes land under `EVENT_LOG_ROOT/<instance>/<date>.jsonl.gz` (`/srv/keyup/eventlogs` by
default, bound as its own volume in `compose.yaml` — a sibling of `AUDIO_ROOT` rather than a
directory inside it, because the ingest walk scans the audio tree and retention purges from
it, and neither has any business meeting a compressed JSONL log). The row in
`event_log_archives` is the *receipt*: instance, date, bytes, digest and path. The `instance`
becomes a directory name, so it is validated as one — letters, digits, dot, dash and
underscore — and a path-shaped name is refused outright rather than sanitized into something
the recorder will never ask for its logs by.

Report your side of it in the status push, as an opaque `"event_log"` object:

```jsonc
{ "event_log": { "pending": 2, "last_date": "2026-09-01" } }
```

Keyup stores it verbatim in `recorders.event_log` and shows it on the recorder's card beside
the list of what has actually arrived. The two disagreeing is the fact worth seeing: a
recorder that says it is caught up while nothing has landed for a week has an upload problem,
and neither figure alone would say so.

Reading it back is admin-only: `GET /api/admin/event-logs` (optionally `?instance=`) lists
the receipts newest first, and `GET /api/admin/event-logs/{archive_id}/download` serves the
`.jsonl.gz` exactly as it arrived. A receipt whose file has gone from disk answers `410` and
is **not** deleted — the row is the record that the day *was* archived, which outlives the
bytes on purpose.

**Retention.** There is none, deliberately: `retention_policies` is scoped to systems and
talkgroups and has no vocabulary for a per-instance file archive, and inventing one for a
tree that grows by single-digit megabytes a day would be a mechanism nobody asked for. Watch
the directory and prune it with whatever prunes the rest of the host; deleting a file leaves
its receipt behind, and the download then answers `410` rather than lying about it.
