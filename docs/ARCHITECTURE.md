# Keyup — Architecture

Keyup ingests call recordings produced by [sdrtrunk](https://github.com/dsheirer/sdrtrunk) and
[trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder), extracts every piece of metadata each recorder
leaves behind (filename, ID3 frames, JSON sidecar, directory layout), transcribes the audio (GPU server *or* local CPU),
stores everything in PostgreSQL, and exposes it through a feature-complete REST/WebSocket API and a responsive web UI.
It can also serve as the radio-audio source for an external CAD / calls-for-service system that correlates incidents
with the radio traffic around them (an optional integration, not part of this repository).

This file is the contract shared by the ingest worker, the transcription pipeline, the API, the frontend and the
migrations. Keep it in sync with `backend/app/models.py` and the Alembic migrations.

---

## 1. Ground truth about the input (measured)

sdrtrunk (nightly 2026-02-19) writing P25 Phase 1 calls from one site's *T-Control* channel produced,
over two days, 8,027 MP3s (7.78 h of audio). Every fact below was verified across all files.

### 1.1 Directory layout

sdrtrunk itself writes every recording into **one flat folder** (`recordings/`). The corpus we have was sorted by a
post-processing script into

```
<AUDIO_ROOT>/YYYYMMDD/<talkgroup alias>/<file>.mp3
```

The ingest walks recursively, so both layouts work. The alias folder (80 of 82 map to exactly one talkgroup; the two
exceptions are patch folders — see 1.4) is *informational only*: the parser never depends on it, it only records it.

#### Storage layout for pushed calls

Everything that arrives over `POST /api/call-upload` — sdrtrunk, trunk-recorder, trunkd, and the backlog migration —
is filed under

```
<AUDIO_ROOT>/uploads/YYYY/MM/DD/<ab>/<file>
```

**The date is the call's own** `started_at`, rendered in `TZ` (the same zone `started_local` uses), and it is decided
*after* the record is built, never from the form before the recording has been parsed. That is the whole rule, and
getting it wrong is what this layout exists to fix: `upload_path` used to take the date from the push, so a backlog
migration streaming a 2022–2024 archive across one afternoon put **972,347 files in `uploads/2026/09/01`** and 82,174
in `uploads/2026/08/31`. ext4's `dir_index` keeps a directory that size functionally fine — a lookup by name is a hash
probe — but nothing that *doesn't* look up by name is fine: `readdir` returns hash order, so `rsync`, `find`, a
backup and any directory walk pay for the whole directory and then seek all over the disk. Partitioned by call date
the same archive spreads over roughly 1,100 real days.

`<ab>` is a two-hex-character bucket taken from the recording's **sha256** (from a hash of the file name for a
metadata-only call, which has no bytes), controlled by `AUDIO_BUCKET_UPLOADS`, **on by default**. It is one boolean
and it applies to every push alike. The obvious alternative — bucket only when a day is expected to exceed ~20k files
— cannot be answered at write time without either counting the directory (the enumeration this layout exists to
avoid) or a `COUNT(*)` per upload, and a per-push threshold would split one day between two layouts for no gain,
because `calls.file_path` is the source of truth and nothing ever guesses a path. 256 buckets hold a day's directory
to a few hundred entries even when a whole system is re-pushed into it; the cost is 256 inodes for a quiet day, which
is the cheaper mistake.

**How a push is written.** The body is streamed to `uploads/.incoming/<random>/<real name>` first, because the
partition depends on a date only the parse can produce and the parse needs the file on disk. The record is built from
it, and the file is then `os.rename`d into its partition: same filesystem, atomic, no copy. The temp file keeps its
real name — an sdrtrunk push carries its timestamp, system, site and talkgroup only in `audioName` — and the target
name is reserved `O_EXCL` before the rename, so two pushes racing on one name cannot destroy each other's recording.
Nothing else in the tree is ever named `.incoming`, and the ingest walk never descends into a hidden directory.
Every error path removes the request's own temp body, and the worker sweeps `.incoming` hourly for entries untouched
for six hours (a process killed mid-request). The ingest walk also skips Keyup's own `<stem>.proc.mp3` copies and their
`.proc-*.mp3` temps, which audio processing writes beside the original in any tree it touches.

**Re-filing the archive.** `keyup refile-audio` moves recordings already on disk onto this rule (see MIGRATION.md).
It walks `calls`, never the filesystem, and every move is an `os.rename` verified by comparing the inode before and
after.

**Don't enumerate the tree.** No hot path may walk `AUDIO_ROOT` whole. The ingest walk (`walk_recordings`) is
`os.scandir` with *pruning*, not `rglob` with filtering: `INGEST_SCAN_SKIP` (default `uploads`) is cut before the
descent, hidden directories are never entered, `INGEST_SCAN_MAX_FILES` caps a run and says so loudly when it fires,
and every pass logs what it cost (`dirs`, `entries`, `candidates`, `pruned`, `elapsed_s`). The inotify observer follows
the same rule — the root is watched shallowly and each non-pruned subtree recursively, because watchdog adds one
inotify watch *per directory* by walking the tree, and `uploads/` under this layout is of the order of 100k
directories, past `fs.inotify.max_user_watches`. A day directory created later is adopted and caught up on the spot.
Anything under `uploads/` is written **and its row created** by the push endpoint, so a walk there could only ever
learn that every file in it is already settled.

### 1.2 Filename

```
YYYYMMDD_HHMMSS_<System>_<Site>_<Name>__TO_<TO>[_FROM_<FROM>][_V2].mp3
```

| part | example | meaning |
|---|---|---|
| `YYYYMMDD_HHMMSS` | `20260829_090328` | **file-close time ≈ call end**, local time (the recorder host's time zone). Now measured rather than assumed: against trunkd's control-channel grant times for the same transmissions the stamp lands at **call end + 1.7 s** (sd 0.43 s, n=17), so sdrtrunk closes and stamps the file a second or two after the transmission ends. The `COMM` `Date` is the same clock read with milliseconds and mtime agrees within 1 s — one instant at three precisions. Call start = this − duration, which therefore runs ≈ 2 s **late**; that bias is what `RECONCILE_CLOCK_SKEW_SECONDS` is sized for ([RECONCILIATION.md](RECONCILIATION.md) §5). |
| `<System>_<Site>_<Name>` | `Example-System_Site-A_T-Control` | spaces replaced by `-`; constant per recorder channel |
| `TO` | `1101` or `P14257-[1012--2061--1011]` | talkgroup id, or patch id + member talkgroups |
| `FROM` | `1234567`, `781824_660_6604120`, absent | radio id; fully-qualified `WACN_SYS_ID` form (sdrtrunk `ISSI`/`ROAM` prefix stripped, dots → `_`) for radios addressed by home network; absent when never resolved (0.6 %) |
| `_V2` | | sdrtrunk collision suffix (a second file for the same second) |

### 1.3 ID3 frames (identical set on every file)

| frame | example | use |
|---|---|---|
| `COMM` | `Date:2026-08-28 05:52:19.894;System:Example System;Site:Site A;Name:T-Control;Decoder:P25 Phase 1;Channel:1-92;Frequency:852162500;` | **authoritative write time** (ms; same clock read as the filename), system, site, channel name, decoder, sdrtrunk channel id, frequency Hz |
| `TIT2` | `1821"EMS OPS 4"` / `P:9230 [1282, 2051, 2061, 3201]"PATCH 9230","LAW 31 PATCH",…` | talkgroup id + alias list; for patches: patch id, member list and every member alias |
| `TPE1` | `1234557` / `781824.660.6604120` | radio id (omitted when unresolved) |
| `TALB` / `TIT1` | `T-Control` / `Example System` | channel name / system |
| `TCOM` | `sdrtrunk nightly - 2026-02-19T09:01:39.551+0000` | recorder version |
| `TCON`, `TDRC` | `Scanner Audio`, `2026` | constant |

There are **no** encryption or emergency flags in the files. Those columns exist in the schema (nullable) so the
push-ingest path (§1.6) or a future event-log ingest can fill them.

### 1.4 Patches

A patch (`P:<id> [tg, tg, …]`) is a temporary supergroup. The patch id is regenerated each time dispatch re-creates the
patch, so patches are identified by their **member set**, and every call stores `talkgroup_ids[]` (all members) so
"calls involving talkgroup X" includes patched traffic.

### 1.5 Audio

MPEG-2 Layer III, 32 kbps CBR, 22.05 kHz mono. Durations: median 2.2 s, p95 12 s, max 44.5 s; **14 % are < 1 s**
(squelch tails). Whisper hallucinates on those unless VAD is on.

### 1.6 Second input path: sdrtrunk streaming upload

sdrtrunk can POST each call as `multipart/form-data` to an Rdio-Scanner-compatible endpoint (`key, system, audioName,
audio, dateTime (epoch s, **true call start**), talkgroup, source, frequency, talkerAlias, talkgroupLabel,
talkgroupGroup, systemLabel, patches "[super, tg, tg, …]"`). It expects the literal body `Call imported successfully.`
on success, `duplicate call rejected` for dedupe, and `incomplete call data: no talkgroup` for the `test=1` probe.
Keyup implements `POST /api/call-upload` with exactly that contract, so a recorder can feed it directly without any
file-sorting script; the file-watching path stays for archives and for hosts that only write files.

**trunkd** pushes to the same endpoint in trunk-recorder's dialect (bare-member `patches`, `sources`, `frequencies`)
with 8 kHz mono MP3 (or WAV), naming itself in `recorder=trunkd` → `ingest_source='trunkd'`, adding `site`, `nac`,
`algorithm`, `keyId` and `durationMs`, and posting an encrypted call **with no audio part at all** — P25 encrypts only
the voice payload — which is stored with `audio_state='purged'` exactly as the trunk-recorder sidecar path stores the
same fact (§1.7); [docs/RECORDERS.md](RECORDERS.md) §5.

### 1.7 Third input path: trunk-recorder file output

[trunk-recorder](https://github.com/TrunkRecorder/trunk-recorder) (the open-source Linux recorder) writes `<captureDir>/<shortName>/<Y>/<M>/<D>/<tg>-<epoch>.<ms>_<freqHz>[.<slot>]-call_<n>.{wav,m4a,json}`.
The `.json` sidecar carries what sdrtrunk files cannot: true `start_time`/`stop_time` (epoch, ms precision),
`emergency`/`encrypted` (0/1), `audio_type`, optional `patched_talkgroups[]`, `freqList[]` per transmission and
`srcList[]` — every keying radio with its position in the audio. Ingest rules: when a sidecar exists next to an audio
file it is authoritative (`time_source='sidecar'`, `ingest_source='trunk_recorder'`, `started_at` from
`start_time_ms`, `duration_s` from `call_length_ms`); `srcList` becomes `call_sources` rows (per-transmission radio +
offset, each length derived from the next source's position) with the first source denormalized to `calls.radio_id`;
`talkgroup_group_tag` is the alpha tag (their JSON names are shifted vs their CSV: `talkgroup_tag`=service tag,
`talkgroup_group`=category → `talkgroups.group_name`); `patched_talkgroups[]` makes `call_type='patch'` and joins
`talkgroup_ids` — trunk-recorder gives a patch no id of its own, so `calls.patch_id` stays null and the member set is
what answers "calls involving talkgroup X". The whole sidecar is kept verbatim in `calls.raw_meta` (an sdrtrunk file's
ID3 frame map goes in the same column), so the UI can show exactly what the recorder said.

`.m4a`/`.wav` audio is accepted alongside `.mp3`, and `.raw.wav` — trunk-recorder's undecoded baseband capture — is
skipped. One call is up to three files sharing a stem: `TR_PREFER_AUDIO` (default `m4a`) picks between a WAV and an
M4A of the same audio, and the watcher waits `TR_SIDECAR_WAIT_SECONDS` (default 30) for the `.json`, which lands after
the audio; when it never arrives the call is ingested from the filename and the audio header with
`time_source='file_write'`. **An encrypted call has a sidecar and no audio at all; it is still ingested — it proves an
agency held that talkgroup at that second, which is exactly what a CAD integration asks — with
`audio_state='purged'` and `file_path` pointing at the JSON, so the audio route answers `410 Gone` rather than
inventing a file.**

trunk-recorder's Rdio-Scanner upload plugin posts to the same `/api/call-upload` contract as sdrtrunk with extra
fields (`frequencies`, `sources`, `patches`, `talkgroupGroup/Label/Tag/Name`, `audioType`) — the endpoint consumes
them when present, and their presence is what tells the two dialects apart, because sdrtrunk's `patches` puts the
supergroup first while trunk-recorder posts a bare member list. `audioType` decides the stored extension
(`audio/mp4` → `.m4a`, `audio/wav` → `.wav`, else `.mp3`), and `GET /calls/{id}/audio` serves each with the matching
`Content-Type`.

Pointing either recorder at Keyup — file watch, capture-directory mount, or streaming upload — is
[docs/RECORDERS.md](RECORDERS.md).

---

## 2. System overview

```
 sdrtrunk ──writes──▶ AUDIO_ROOT ──inotify/scan──▶ ingest worker ──▶ PostgreSQL ◀── API (FastAPI) ◀── web (React) / CAD integrations / others
                                                        │                ▲              │
                                                        └─ jobs ──▶ transcription worker ┘   └─ WebSocket live feed
                                                                     ├─ openai-compatible STT (Speaches on a GPU box or stt-cpu / stt-openvino)
                                                                     ├─ local faster-whisper (CPU int8)
                                                                     └─ OpenVINO GenAI (Arc iGPU / NPU, in-process or stt-openvino)
```

Compose services (`compose.yaml`): `db` (postgres 18), `api`, `worker` (ingest + transcription + retention scheduler),
`web` (nginx serving the built SPA and proxying `/api` and `/audio`). Optional profile `stt-cpu` runs a local Speaches
container so even the "remote" backend can be self-contained; optional profile `stt-openvino` runs whisper on the host's
Arc iGPU or NPU ([docs/STT.md](STT.md)).

`web`'s nginx (`frontend/nginx.conf`) carries the WebSocket upgrade and SSE no-buffering rules the live routes
need. A second, public-facing proxy in front of it (archive.example.org → 192.0.2.10:8095) does **not** inherit them
and has to repeat every one, or `/api/live` and the event streams break while the rest of the site looks fine:
[docs/REVERSE-PROXY.md](REVERSE-PROXY.md).

Stack: Python 3.12, FastAPI, SQLAlchemy 2 (sync, psycopg 3), Alembic,
pydantic-settings, ruff (line-length 110); React 19 + Vite + TypeScript + Tailwind 4 + Radix UI + lucide + TanStack
Query/Table + Biome. The CSS-variable palette is `--page --surface --ink --accent --good --warning --serious
--critical`, dark via `data-theme="dark"`.

---

## 3. Data model

All timestamps are `timestamptz` (UTC) **and** every call also carries `started_local` (naive local time in `TZ`) because
CAD systems commonly store naive local time; the API returns both.

### 3.1 Directory (who is who)

- **systems** — `id, name (unique), wacn, system_id, protocol ('P25 Phase 1'…), description`.
- **sites** — `id, system_id, name, site_number, description`.
- **talkgroups** — `id, system_id, tgid (int), alias, group_name (sdrtrunk alias Group), description,
  category (law/fire/ems/multi/interop/pw/utility/transit/rail/aviation/schools/corrections/security/federal/military/media/business/ham/data/other),
  service_tag (RadioReference's own tag, verbatim: 'Fire Dispatch', 'Law Tac', 'Public Works'…),
  agency_code (the CAD system's agency code, nullable), color, priority (int), transcribe (bool), transcribe_min_seconds,
  retention_days (nullable → global), is_hidden, first_seen_at, last_seen_at, call_count, notes`.
  Unique `(system_id, tgid)`. Aliases are auto-created from `TIT2` and user-editable; the recorded alias is also kept on
  each call (`talkgroup_alias_at_record`) so history survives renames.
  `category` is this archive's coarse bucket — free text, the values the UI colors — and is *derived* from
  `service_tag` (`app.models.SERVICE_TAG_TO_CATEGORY`) when RR supplies one. The RR sync fills either column only when
  it is blank, so an operator's classification always outranks RR's; an RR tag this build does not know is still stored
  verbatim, leaving `category` alone (§3.5).
- **radios** — `id, system_id, radio_id (bigint), wacn, home_system_id (from FQ form or LC 9, §3.6), alias, unit_id
  (apparatus id such as `E1`/`A21` to match CAD / PulsePoint units), agency_code, kind
  ('console'|'portable'|'mobile'|'unknown'), first_seen_at, last_seen_at, last_registered_at, last_deregistered_at,
  call_count, notes`. Unique `(system_id, radio_id, wacn, home_system_id)`. `last_seen_at` means heard talking *or*
  seen on the control channel; the two `*registered_at` stamps are presence alone (§3.6).
- **units** — `id, callsign ('E1'), name ('Engine 1'), agency_code, kind ('vehicle'|'station'|'team'|'other'), notes,
  created_at, updated_at`. Unique `(agency_code, callsign)`, plus a partial unique index on `callsign` where
  `agency_code IS NULL` — two NULL agency codes are not equal in SQL, so the constraint alone would admit a second
  agency-less `E1`. A unit is what a callsign *is*; `radios.unit_id` is only what a radio is *labeled*.
- **personnel** — `id, name, rank, badge, agency_code, active (bool), notes, created_at, updated_at`. Deliberately
  thin: this is not a personnel system. `active` is a flag rather than a deletion, because a retired name is still the
  right answer about last year's traffic.
- **radio_assignments** — `id, radio_pk, unit_id (nullable), person_id (nullable), effective_from, effective_to
  (NULL = current), note`. `CHECK ((unit_id IS NOT NULL) <> (person_id IS NOT NULL))`: a row assigns a unit *or* a
  person, never both — they are independent facts, so a radio may hold one current assignment of each kind at once.
  Two partial unique indexes on `radio_pk` (`WHERE effective_to IS NULL AND unit_id IS NOT NULL`, likewise for
  `person_id`) enforce "at most one current holder of each kind" in the database, where the race between two operators
  clicking *assign* actually happens. History is closed-ended, never overwritten, so a call from March resolves against
  March's assignment.
  `radios.unit_id` stays the denormalized label the call lists render (the canonical name is
  `coalesce(radios.unit_id, radios.alias, radios.radio_id, calls.radio_raw)`, computed on every row of every page) and
  is kept in step from `app.services.roster`: assigning a unit writes its callsign there, ending an assignment clears
  it *only if it still holds that callsign*, and a person assignment never touches it. A direct `PATCH /radios/{id}`
  still works; the next assignment change wins.
- **patches** — `id, system_id, patch_id (int), member_tgids int[], first_seen_at, last_seen_at, call_count`.
  Unique `(system_id, patch_id, member_tgids)`.
- **radio_affiliations** — rolling "last seen on" view: `radio_id_fk, talkgroup_id_fk, first_heard_at, last_heard_at,
  call_count, source ('call'|'affiliation'), last_affiliated_at` (updated on ingest; powers "who's active" and unit
  history). `source` says what backs the row — heard talking, or the control channel saying so (§3.6) — because the
  two are different claims and merging them silently would change what the table means.

### 3.2 Calls

**calls**

| column | type | notes |
|---|---|---|
| `id` | bigserial PK | |
| `system_id`, `site_id` | FK | |
| `talkgroup_id` | FK nullable | primary TG (for a patch: first member) |
| `talkgroup_ids` | int[] (tgids) | all TGs incl. patch members; GIN index |
| `patch_id` | FK patches nullable | |
| `radio_id` | FK radios nullable | |
| `radio_raw` | text | exactly as recorded (`781824.660.6604120`) |
| `talker_alias` | text nullable | P25 talker alias (upload path only) |
| `ingest_source` | enum `file|upload|trunk_recorder|trunkd` | |
| `radio_resolved` | bool | false when FROM missing |
| `ingest_priority` | text nullable | the admission class the push path was given (`urgent`/`live`/`backlog`/`bulk`, or a bare integer); NULL for anything that did not arrive over `POST /call-upload` with an explicit `priority` — it is what still answers "this row came from the migration" in SQL |
| `source_instance` | text nullable | which recorder's copy of the transmission these bytes are (the `sourceQuality` field's `instance`) |
| `source_quality` | jsonb nullable | that recorder's own report of how well it heard the call (`voice_frames`, `bad_frames`, `rssi_dbfs`, and whatever else it sends); the basis of the reconciler's source-quality rank (§5.1) |
| `recorded_at` | timestamptz | file-close time from `COMM` Date (ms) — the raw fact, ≈ the call end (§1.2) |
| `started_at` | timestamptz | `recorded_at − duration_s` for file ingest (≈ 2 s late, §1.2); the recorder's own start for a sidecar or a push — index |
| `started_local` | timestamp | naive local time in `TZ` |
| `ended_at` | timestamptz | started_at + duration |
| `time_source` | enum `file_write|upload|event_log|sidecar` | how `started_at` was derived. `sidecar` and `upload` are the recorder's own call start and exact; `file_write` is derived from the sdrtrunk stamp and runs ≈ 2 s late (§1.2), which is the one reconciliation does not treat as exact |
| `duration_s` | numeric(8,3) | from MP3 header |
| `frequency_hz`, `channel`, `decoder` | | from `COMM` |
| `call_type` | enum `group|patch|individual` | |
| `emergency`, `encrypted` | bool nullable | from a trunk-recorder sidecar (§1.7) or the push path; an sdrtrunk file carries neither |
| `audio_type` | text nullable | `analog` / `digital` / `digital tdma` (sidecar) |
| `signal_db`, `noise_db`, `freq_error` | int nullable | sidecar radio measurements |
| `recorder_call_num` | int nullable | trunk-recorder's own `call_num`, for reading its logs |
| `raw_meta` | jsonb nullable | the recorder's metadata verbatim: the whole sidecar, or an sdrtrunk file's ID3 frame map. Nothing parses it back |
| `talkgroup_alias_at_record` | text | folder / TIT2 alias |
| `file_path` | text unique | relative to AUDIO_ROOT |
| `file_name`, `file_size`, `sha256`, `bitrate`, `sample_rate`, `channels`, `recorder_version` | | |
| `file_mtime` | timestamptz | |
| `duplicate_of` | FK calls nullable | set by reconciliation (§5.1 step 4); points at the copy that won, which is not always the one that arrived first |
| `reconciled_at` | timestamptz nullable | when reconciliation last ruled on this row — set on both sides of a pair |
| `reconcile_reason` | jsonb nullable | the ruling: the match, both scores broken into components, which one decided, what was merged |
| `audio_state` | enum `present|archived|purged|discarded` | retention outcome; when archived, `file_path` becomes absolute (archive path). `discarded` is §5.6 rather than retention: the talkgroup's `record` toggle was off and the file was deleted once transcription and the exporters were done with it |
| `audio_archived_at` | timestamptz nullable | also stamped when the audio is *discarded* |
| `audio_policy` | text NOT NULL, default `keep` | what the ingest decided about keeping the recording, **at the moment it landed**: `keep`, or `discard` when the talkgroup's `record` was off. Written once and never re-derived, exactly like `transcript_status` — turning Record back on does not make yesterday's calls keepers, and turning it off does not condemn a year of archive |
| `transcript_status` | enum `pending|queued|running|done|skipped|failed|empty` | denormalized for filters |
| `transcript_text` | text nullable | primary transcript, denormalized for FTS |
| `transcript_quality` | enum `good|suspect|noise` nullable | from hallucination heuristics |
| `search_tsv` | tsvector generated | `english`, from transcript_text ∥ notes ∥ aliases |
| `notes` | text | user-editable |
| `tags` | text[] | user labels; GIN |
| `starred` | bool | |
| `ingested_at`, `updated_at` | | |

Indexes: `(started_at desc)`, `(talkgroup_id, started_at desc)`, `(radio_id, started_at desc)`, GIN `talkgroup_ids`,
GIN `search_tsv`, `(transcript_status)`, partial on `starred`. Monthly range partitioning on `started_at` is deferred
until volume warrants it (≈4 k calls/day now).

**transcripts** — one call can have several (different engines/models, re-runs); exactly one `is_primary`.

`id, call_id, engine ('openai_compatible'|'faster_whisper_local'), backend_name, model, language, text, segments jsonb
([{start,end,text,avg_logprob,no_speech_prob,compression_ratio}]), words jsonb nullable, avg_logprob, no_speech_prob,
compression_ratio, quality ('good'|'suspect'|'noise'), quality_reasons text[], prompt_used, vad_used bool,
processing_ms, created_at, is_primary, edited_by_user bool, edited_text`.

**transcription_jobs** — `id, call_id, status ('queued'|'running'|'done'|'failed'|'cancelled'), priority, backend_name,
attempts, max_attempts, last_error, parked_reason, scheduled_at, started_at, finished_at, locked_by, locked_at`. Workers
claim with `SELECT … FOR UPDATE SKIP LOCKED`. `parked_reason` says why a `failed` row was parked when the reason was
not the recording — only `'backend_unavailable'` so far — and carries a partial index, because the pass that reads it
(§5.2) runs every two minutes and finds nothing almost every time.

**call_sources** — per-transmission keying radios: `id, call_id FK cascade, radio_id FK nullable, src (bigint, as the
recorder reported it), pos_s (seconds into the recording), len_s (to the next source, or the call's end), emergency
bool, tag (the recorder's own name for the unit), seq`. Indexed on `call_id`. Every distinct `src` also becomes a
`radios` row (with `tag` as an alias hint) and a `radio_affiliations` touch, so "who was on this channel" counts the
third unit on a call as much as the first. sdrtrunk file ingest yields one row at pos 0 — all its files can say — so
both dialects read the same way; trunk-recorder sidecars yield the full list.

**conversations** (derived, optional) — consecutive calls on the same `talkgroup_ids` overlap with gaps < N s
(default 20) are grouped: `id, talkgroup_id, started_at, ended_at, call_count, first_call_id`. Calls get
`conversation_id`. Rebuilt incrementally on ingest; lets the UI show a thread and lets a CAD integration pull "the whole
exchange" rather than one 3-second key-up.

### 3.3 Integration

- **external_links** — `id, call_id (or conversation_id), source ('cad'|'manual'|…), external_id (e.g.
  cfs_number), link_confidence ('strong'|'probable'|'weak'), link_method ('time'|'time+agency'|'unit'|'manual'),
  link_note, created_by, created_at`. The same vocabulary PulsePoint-style incident links use.
- **agency_map** is expressed on `talkgroups.agency_code` / `radios.agency_code`; a CAD integration uses it to answer
  "which channels would a call for this police department at 14:32 be on?".

### 3.4 Operations

- **ingest_runs** — each scan/watch batch: `started_at, finished_at, files_seen, files_new, files_duplicate,
  files_failed, errors jsonb`.
- **ingest_files** — per-file ledger keyed by path: `status ('ingested'|'duplicate'|'failed'|'ignored'), error,
  call_id, attempts, last_attempt_at` (so failures are visible and retryable).
- **recorder_gaps** — derived: periods > N minutes with no calls on a system that normally has traffic (recorder
  down detection).
- **retention_policies** — `scope ('global'|'system'|'talkgroup'), scope_id, keep_days, keep_audio_days,
  keep_transcripts (bool), archive_path nullable` — purge audio but keep metadata/transcripts when desired.
  Resolution order: talkgroup policy → `talkgroups.retention_days` → system policy → global policy → env defaults.
  Starred calls are never touched.
- **export_jobs** — async audio/metadata bundles for a filter: `id, filters jsonb, status, file_path, size_bytes,
  call_count, error, created_by, created_at, finished_at`.
- **audio_profiles** — named per-channel filter chains (§5.5, [docs/AUDIO.md](AUDIO.md)): `id, name unique,
  description, chain jsonb, builtin, created/updated_at`.  `chain` is an ordered list of whitelisted steps
  (`[{"kind": "equalizer", "params": {"f": 1950, "width": 500, "gain_db": -40}}, …]`), never a rendered ffmpeg
  string — the command line is generated from it, so nothing an operator typed is stored in the form it would be
  executed in.  `talkgroups.audio_profile_id` (nullable, `ON DELETE SET NULL`) points a channel at one; NULL
  means the global `audio_*` settings.
- **settings** — key/value runtime settings editable in the UI (STT backend selection, prompt, VAD, thresholds…) with
  env defaults.  Two of its keys are not operator knobs but generation counters the CRUD below maintains and the
  workers poll: `stt_config_version` and `ingest_config_version`.
- **stt_backends** — the speech-to-text backends this deployment routes to, as rows rather than as the
  `STT_BACKENDS` environment JSON: `name (pk), kind, base_url, api_key, model, device, compute_type, cpu_threads,
  cache_dir, use_prompt, use_hotwords, concurrency, priority, timeout, enabled, source ('env'|'db'), notes,
  deleted_at, created/updated_at, updated_by`.  Columns mirror `app.config.SttBackend` exactly — the table *is*
  that JSON with a primary key and an audit trail.  `api_key` is **write-only**: stored, never echoed, and a
  response carries `has_api_key` instead.  `deleted_at` exists because `transcripts.backend_name` names the engine
  behind every transcript in the archive: deleting a backend any transcript names marks the row deleted and
  disabled rather than dropping it, so history stays answerable (§5.2).
- **ingest_sources** — where recordings come from: `id, kind ('watch_dir'|'push_token'|'migration'), label (unique),
  enabled, path, recursive, tr_prefer_audio, tr_sidecar_wait_seconds, token_id → api_tokens, notes, meta jsonb,
  last_activity_at, last_error, last_error_at, created/updated_at, updated_by`.  Three kinds because the three ways
  a recording arrives are different objects: a directory the watcher watches, a *label* for the upload token a
  pushing recorder presents (the upload path itself is unchanged and still authenticates on the token alone), and a
  bulk import that owns no runtime behavior and exists to have somewhere to report progress (§5.1).
- **users**, **user_sessions**, **api_tokens** (`scopes text[]`: `read`, `write`, `admin`, `stream`, `upload`; `label`,
  `created_by`, `last_used_at`, `expires_at`, `revoked_at`) — reads are open by default (`AUTH_REQUIRE_READ=false`),
  mutations require a session or Bearer token.
- **saved_searches** (per user, shareable), **favorites** (user↔talkgroup, with mute flag), **alert_rules**
  (`kind: 'talkgroup'|'radio'|'keyword'|'regex'`, filters, `channels: webhook/discord/ntfy/email`, cooldown),
  **notifications** (delivery log).

### 3.5 Site channel audit (docs/CHANNEL_AUDIT.md)

The recorder is the only thing that sees the air; RadioReference documents what should be there; the recorder's config
lists what it hunts. All three are kept, apart, and the report is the difference.

- **recorder_observations** — one posted document, verbatim: `system_id, site_id, recorder, recorder_version,
  observed_at, window_s, payload jsonb`.
- **site_frequencies** — one row per **(site, frequency, source)**: `frequency_hz, role ('primary_control'|
  'alternate_control'|'voice'|'unknown'), source ('observed'|'radioreference'|'configured'), channel_ref,
  first_seen_at, last_seen_at, observation_count, grant_count, alerted_at, meta jsonb`. Unique
  `(site_id, frequency_hz, source)` — the three sources are never merged, because their disagreement *is* the report.
- **adjacent_sites** — neighbors a control channel announces: `system_id_p25, rfss, site_number, lra,
  control_frequency_hz, channel_ref, flags jsonb, first/last_seen_at, observation_count, alerted_at`. Unique
  `(site_id, system_id_p25, rfss, site_number)`.
- **systems** gains `nac, rr_system_id, rr_synced_at`; **sites** gains `nac, rfss, lra, rr_site_id, rr_synced_at`,
  all filled from the first observation that carries them and never overwritten.
- `alert_rules.kind` gains `channel_audit`, raised once per newly undocumented frequency or unknown neighbor.
- **site_message_kinds** — one row per `(site_id, mfid, opcode)`: every control-channel message kind a site has
  *ever* transmitted, with `first/last_seen_at`, `count`, `exemplar` / `exemplar_previous` /
  `exemplar_changed_at` for the bodies the recorder cannot decode, the operator's `known` + `label`, and
  `alerted_at` as the 24-hour rate limiter. Written in the same transaction as the observation window that
  carried it. The message *census* (`GET /api/sites/{id}/message-mix`) stays a rolling histogram folded out of the
  payloads; this table is the memory underneath it, and is the only thing that can say a kind is **new**.
  `alert_rules.kind` gains `new_kind` and `exemplar_changed` for it (activity: `site.unknown_new`,
  `site.unknown_changed`, source `site`). The first window into an empty table seeds that site silently, and
  `keyup sites seed-message-kinds` replays the stored windows so a fresh deployment does not report the ~97 kinds
  a site has always been sending as news. A kind arrives *named* when the
  built-in P25 table (`app/p25/message_names.py`, the 64 standard TSBK opcodes in trunkd's spelling plus the
  L3Harris ones) knows a name for it; `keyup sites name-message-kinds [--toml …]` names the rows that predate the
  table, so `known = false` means nobody anywhere has named it.

### 3.6 Radio events (docs/RADIO_EVENTS.md)

The archive knows what was *said*; only the control channel knows who was *there*. A radio that turns on registers
with the site and affiliates to a talkgroup minutes before it keys up, and one that never keys up at all appears
nowhere else. The control-channel measurement behind these tables, `NON-VOICE-DATA.md`, lives in the
trunkd repository ([trunkd docs/NON-VOICE-DATA.md](https://github.com/pwsh/trunkd/blob/main/docs/NON-VOICE-DATA.md)), not in this one.

- **radio_events** — one decoded message about one radio: `at, system_id, site_id, kind, opcode, mfid, radio_pk,
  radio_id, radio_wacn, radio_system, target_radio_id, talkgroup_tgid, talkgroup_pk, result, frequency_hz,
  meta jsonb, created_at`. `kind` ∈ `unit_registration` | `unit_deregistration` | `unit_registration_command` |
  `group_affiliation` | `group_affiliation_query` | `location_registration` | `sndcp_grant` | `sndcp_page` | `ack` |
  `deny` | `queued` | `other`. Indexed on `(at)`, `(radio_pk, at)`, `(talkgroup_pk, at)`, `(site_id, kind, at)`.
  Unique on `(site_id, at, kind, opcode, radio_id, talkgroup_tgid)`, COALESCE'd — the recorder spools batches and
  retries them, and a retry must be free.
- One table with a `kind` and a JSONB `meta`, not a table per opcode family: ~600 events/hour, and the *rare*
  commands are individually the most interesting messages on a P25 system and collectively too rare to model.
  `other` keeps the opcode.
- Folding never moves `call_count` — a registration is not a transmission. An accepted `group_affiliation` writes
  `radio_affiliations` with `source='affiliation'`; `unit_registration` and `unit_deregistration` stamp the two new
  `radios` columns; `resolve_radio` keeps a unit that arrives bare and fully qualified by turns on **one** row.
- `POST /api/call-upload` accepts `sourceWacn`/`sourceSystem` (P25 LC 9), which fill `radios.wacn` /
  `home_system_id` where they are empty. No new table: the columns have been waiting since the schema was written.
- `recorder_observations.payload` additionally carries `message_mix`, `unknown_exemplars`, `link_control_mix`,
  `data_channel` and `lsd_nonzero` — the message *census* per window, stored verbatim. The raw TSBK stream is
  2.3 M messages a day and is deliberately never stored as rows.
- Retention: `RADIO_EVENTS_KEEP_DAYS` (90), a clock of its own and the one window that does not default to "for
  ever". This is presence data about identifiable people (§5.3, docs/RADIO_EVENTS.md §6).

---

## 4. API (`/api`, OpenAPI at `/api/openapi.json`, docs at `/api/docs`)

Conventions: JSON (orjson), `{"detail": "..."}` errors, `page`/`page_size` (≤ 500) **and** cursor pagination
(`cursor=`) for large scrolls, `sort=` with `-` prefix for desc, `date_from/date_to` (ISO, tz-aware or local), array
filters repeated (`talkgroup=1101&talkgroup=1102`), `X-Total-Count` header, ETag on audio.

### 4.1 Calls
- `GET /calls` — filters: `q` (FTS with websearch syntax), `date_from`, `date_to`, `time_from`, `time_to` (time-of-day),
  `dow[]`, `system`, `site`, `talkgroup[]` (matches `talkgroup_ids`), `radio[]`, `unit[]`, `agency[]`, `category[]`,
  `call_type`, `emergency`, `encrypted`, `has_audio` (true → `audio_state='present'`; false → the
  metadata-only rows an encrypted or purged call leaves; omit for both), `audio_type[]`, `min_duration`, `max_duration`,
  `transcript_status[]`, `quality[]` (repeat for OR — `quality=good&quality=suspect` is the "hide noise" view), `tag[]`, `starred`, `patch`,
  `conversation`, `priority[]` (`live`|`backlog`|`bulk`, repeat for OR — the `ingest_priority` class the push path
  stamped, which is how a migrated archive is told from live traffic; a call the file watcher picked up off disk
  carries no class and matches none of the three), `include_duplicates` (default false), `sort`,
  `page`/`page_size`/`cursor`.
  The web Calls page defaults to `has_audio=true` (an "Audio only" toggle), and its column set and a
  full-transcript second row are per-device preferences kept in `localStorage`, not filters.
- `GET /calls/facets` — counts per talkgroup/radio/agency/category/hour/status for the current filter.
- `GET /calls/{id}` — full detail incl. primary transcript segments, `sources[]` (§1.7), `raw.raw_meta`, adjacent
  calls (prev/next on same TG), links.
- `GET /calls/{id}/audio` — the recording as stored (MP3/M4A/WAV, `Content-Type` from the extension) with HTTP Range,
  ETag, `?download=1`; `GET /calls/{id}/waveform` — peaks JSON (cached; ffmpeg decodes any of the three).
- `GET /calls/{id}/transcripts`, `POST /calls/{id}/transcribe` (enqueue, optional backend/model), `PATCH /calls/{id}`
  (notes, tags, starred, transcript text correction), `DELETE /calls/{id}` (admin).
- `GET /calls/export?format=csv|json|ndjson` — streamed, **uncapped**; `POST /exports` — async job that zips audio +
  metadata for a filter, `GET /exports/{id}` status/download.
- `ids=` is part of the shared filter vocabulary, so every one of the above (and `/calls`, `/search`,
  `/calls/playlist.m3u`) accepts a hand-picked set of call ids. It **narrows** the other filters rather than replacing
  them — "these, out of those" — and is capped at 1000 per request (422 over, never a silent truncation).
- `GET /calls/playlist.m3u` — M3U for the current filter (external players).
- `POST /call-upload` — sdrtrunk/Rdio-Scanner-compatible push ingest (§1.6); authenticated by an API token with the
  `upload` scope passed as the `key` field. Optional `priority` (form, or `X-Keyup-Priority`; the form wins):
  `urgent` → 10, `live` (default) → the talkgroup's priority, `backlog` → 500 + it, `bulk` → 900, or an integer
  1–999, stored on the call as `ingest_priority`. **Admission control** decides before the audio is read: below
  `UPLOAD_MIN_DISK_FREE_BYTES` every class gets `507`; the live band is refused only over `UPLOAD_MAX_QUEUE_LIVE`;
  the backlog band (priority ≥ 500) is refused when paused, over `UPLOAD_MAX_QUEUE_BACKLOG`, over
  `UPLOAD_BACKLOG_MAX_ETA_SECONDS` of drain, or over `UPLOAD_BACKLOG_MAX_CONCURRENCY` in flight — `429` with
  `Retry-After` (5–600 s) and a JSON body for a client that sent `priority`, the legacy plain text for one that did
  not. Every response carries `X-Keyup-Queue-{Live,Backlog,Audio-Seconds,Eta-Seconds}` and
  `X-Keyup-Accepting-Backlog`. An accepted backlog call is stored and searchable at once; only its transcription
  waits (`docs/RECORDERS.md`, `docs/MIGRATION.md`).
- `GET /upload/flow` — the same queue gauge for a pushing client to poll before a burst (`upload` scope, bearer or
  `?key=`): `{live, backlog, audio_seconds, eta_seconds, accepting: {live, backlog}, paused_backlog}`.

### 4.2 Directory
- `GET/POST/PATCH /talkgroups`, `GET /talkgroups/{id}` (+ `stats`: calls/day, busiest hours, top radios),
  `POST /talkgroups/import` (sdrtrunk playlist `default.xml` — `<alias name group color><id xsi:type="talkgroup"
  value protocol/></alias>` — or CSV → aliases/groups/colors), `GET /talkgroups/export`.
- `GET/PATCH /radios`, `GET /radios/{id}` (+ `affiliations`, `history`, `events`), `POST /radios/import`. The detail
  carries `presence` (last seen / registered / de-registered, and the affiliations with their `source`) and the 50
  most recent radio events; `GET /radios/{id}/events?from&to&kind&page` is the paged timeline (§3.6).
- `GET /systems`, `GET /sites`, `GET /patches`, `GET /conversations`, `GET /conversations/{id}`.
- `GET /directory/sites/{id}/channel-audit`, `GET /directory/systems/{id}/channel-audit` — the site channel audit
  (§3.5): undocumented and missing channels, control-channel drift, the recorder-config gap and the adjacent sites,
  computed live from `site_frequencies`.
- `GET /sites/{id}/radio-events/summary?hours=24` — radio-event counts by kind and result, the busiest radios and
  registrations per hour; `GET /sites/{id}/message-mix?days=7` — the control channel's message census per
  `mfid:opcode`, aggregated out of the observation payloads (§3.6). Both also answer under `/directory/sites/…`.
- `GET /agencies` — agency codes known to the map (seeded from the agency CSV, plus any added locally) with their talkgroups.
- **Bulk edits** — `POST /talkgroups/bulk`, `POST /radios/bulk`, `POST /units/bulk`, `POST /personnel/bulk`, body
  `{ids: int[], changes: <the matching Patch model>}` → `{updated, items}`. `changes` has the same `exclude_unset`
  semantics as the single `PATCH` (a key present with `null` clears the column), the agency code is validated once for
  the batch, and the whole thing is one transaction with **one** activity line (`*_bulk_edited`, the ids in `meta`)
  rather than one per row. A POST on a literal sub-path rather than a `PATCH /talkgroups` because
  `PATCH /talkgroups/{id}` already owns that shape and would parse `bulk` as an id. At most 500 ids (422 over);
  a missing id 404s the whole batch rather than silently updating the rest. The per-row identity fields are refused
  with a 422 — `callsign` on units (the unique key), `alias` and `unit_id` on radios — because writing one of them
  across a selection is never an edit, it is the loss of the directory.

**Roster** (§3.1) — a separate router (`app/api/roster.py`) because units and personnel are what an operator curates,
while talkgroups and radios are what the recorder discovers; nothing here is ever created by ingest.
- `GET/POST /units`, `GET/PATCH /units/{id}`, `GET /units/{id}/radios`, `GET /units/export`, `POST /units/import`
  (CSV `callsign,name,agency_code,kind,notes`; matched on `(agency_code, callsign)` and **overwritten**, a row naming
  an unknown agency skipped rather than imported without it).
- `GET/POST /personnel`, `GET/PATCH /personnel/{id}`, `GET /personnel/{id}/radios`, `GET /personnel/export`,
  `POST /personnel/import` (CSV `name,rank,badge,agency_code,notes[,active]`; matched case-insensitively on
  `(agency_code, name)` because `personnel` has no unique key of its own).
- `GET /radios/{id}/assignments` — the history, current first. `POST /radios/{id}/assignments {unit_id|person_id, note}`
  closes the current assignment *of the same kind* at the instant the new one opens.
  `DELETE /radios/{id}/assignments/current?kind=unit|person` closes one; the row stays as history.
- `RadioOut`/`RadioDetail` carry `current_unit {id, callsign, name}` and `current_person {id, name}`, resolved from the
  assignments by the endpoint in one query per page — the calls list is untouched and still reads `radios.unit_id`.

### 4.3 Search
- `GET /search?q=` — FTS across transcripts/notes/aliases with `ts_headline` highlights and ranking; `GET
  /search/suggest?q=` — prefix suggestions from talkgroup aliases, roster units and personnel, radio unit ids and
  aliases, and user tags; each carries its `kind`.
- **Spelling variants**. `fuzzy=true` — a filter like any other, so `/calls`, `/calls/facets`, `/search`
  and every other endpoint that parses the calls filter take it — widens each query word of four letters or more to its near spellings in
  `search_vocab` (every distinct `search_tsv` stem, trigram-indexed; within 1–2 edits, at most 8 per word) and ORs
  them into the same full-text query, so ranking, highlights, websearch syntax and every other filter are
  unchanged. `/search?fallback=true` widens only when the exact search finds nothing, and exact answers stay
  byte-identical. The response's `fuzzy {automatic, widened, terms[{term, variants}]}` says what was searched;
  suggestions carry `fuzzy: true` when they came from a trigram rather than a prefix match. The vocabulary and
  its upkeep: [OPERATIONS.md §4](OPERATIONS.md).

### 4.4 Live
- `WS /live` — JSON events `call.new`, `call.transcribed`, `call.updated`, `ingest.status`; client sends a filter
  object identical to `GET /calls` params. `GET /live/sse` — same over Server-Sent Events for simple clients.
- `GET /transcripts/recent` — the embeddable live-transcript page's first screen: the last `minutes` (≤ 240) of
  displayable transcripts under any `GET /calls` filter or a built-in feed preset, with the filter it resolved (to
  send as the socket's first frame) and a keyset `next_cursor`; passed back as `cursor`, it reads the next `limit`
  rows before it with no `minutes` bound, and is null once a cursor page comes back short
  ([EMBED.md](EMBED.md)).

### 4.5 Statistics
- `GET /stats/overview` (totals, today, ingest lag, transcript coverage, STT throughput), `GET /stats/activity`
  (calls per bucket per talkgroup for a range; heatmap-ready), `GET /stats/talkgroups`, `GET /stats/radios`,
  `GET /stats/transcription` (queue depth, RTF per backend, quality distribution), `GET /stats/gaps`.

### 4.6 Links to outside records (for CAD integrations and other consumers)
- `GET|POST|DELETE /links` — persist confirmed links (`source`, `external_id`) so an outside system (a CAD or
  calls-for-service application, an incident tracker) can store its own record number against calls;
  `GET /links/{source}/{external_id}` returns the linked calls.
- `GET /timeline?from=&to=` — compact per-minute activity for the window (a sparkline beside an incident).
- Integration-specific endpoints live in optional packages under `backend/app/integrations/`, discovered at start-up
  (each exposes `register(app)`); with none installed the API simply has no such routes.

**Outbound webhooks** ([WEBHOOKS.md](WEBHOOKS.md)) — the push half of the same
integration, for a consumer that would otherwise poll. Off unless configured: `webhook_url`, `webhook_events`,
`webhook_secret` (write-only), `webhook_max_attempts` (5) and `webhook_timeout_s`, as runtime settings or `WEBHOOK_*`
in the environment; a blank URL or an empty event list queues nothing. Four events — `call.stored` (audio in the
archive and playable, before transcription), `call.transcribed`, `link.created`, `link.deleted` — delivered as a JSON
`POST` one at a time, in order, with `X-Keyup-Event`, `X-Keyup-Delivery` and `X-Keyup-Signature`
(`sha256=<hex>`, HMAC-SHA256 of the exact request bytes under `webhook_secret`; absent when no secret is set). A
non-2xx is retried with a widening backoff (30 s × 2ⁿ, capped at an hour) until `webhook_max_attempts`, then parked
as `failed`; a 4xx that is not 408 or 429 is parked at once. `GET /admin/webhooks` shows the queue and last
deliveries, `POST /admin/webhooks/deliveries/{id}/retry` re-queues a parked one.

### 4.7 Operations
- `GET /status/activity?window=` (2026-09-12) — the **Live activity** panel at the top of Admin and
  Statistics. `/status` says whether the system is up; this says whether anything is *moving*: ingest,
  dedupe, audio, transcription, export and webhook counters for the window **and for the window before
  it**, so the page draws a trend from one request; the queue depths; and one heartbeat row per recorder
  instance (`calls_in_window`, `seconds_since_last_call`, and the `monitor` block the recorder reported —
  where it serves its own live audio panel, with `local_only` computed here for a loopback address that
  only opens in a browser on that machine, which is what the panel's **Listen live** link is built from).
  Dated by when the work landed, not by when the call was recorded. The counters are an open read like the rest of `/status`; the `recent` ticker rows are
  admin-only, because `GET /activity` always has been.
- `GET /status` (health, versions, queue, disk — including the `queue` flow-control gauge), `GET /admin/ingest`,
  `POST /admin/upload/pause-backlog` / `resume-backlog` (stop and resume backlog ingestion; the
  `upload_pause_backlog` runtime setting, so it outlives a restart), `POST /admin/ingest/rescan`,
  `GET /admin/ingest/files?status=failed`, `POST /admin/ingest/files/{id}/retry`, `GET /admin/jobs`,
  `POST /admin/jobs/requeue`, `GET /admin/stt/backends` (the worker's published per-backend counters plus the
  queue block and both configuration versions — §5.2), `POST|PUT|DELETE /admin/stt/backends[/{name}]` (define,
  edit and remove a backend; the PUT is a **full replace** — every editable field takes its default when the body
  omits it, so send the whole row rather than a patch, `api_key` alone excepted; DELETE retains the row when
  transcripts name it),
  `POST /admin/stt/backends/{name}/probe` (health check plus one timed transcription of a real short recording —
  latency, RTF, model), `PUT /admin/stt/backends/{name}/enabled` (the runtime enable/disable *override*, which is
  a different thing from the configured `enabled` and takes effect faster), `GET /admin/backends`
  (the older compact view of the same rows),
  `GET|POST|PUT|DELETE /admin/ingest/sources[/{id}]` and `GET /admin/ingest/sources/{id}/status` (§5.1),
  `POST /admin/ingest/migration-progress` (a bulk import reporting itself), `GET|PUT /admin/settings`,
  `GET|POST /admin/retention/run`, `GET|POST|DELETE /admin/tokens`, `GET|POST|PATCH /admin/users`,
  `GET|POST|PATCH|DELETE /alerts`, `GET /notifications`, `GET|POST|DELETE /saved-searches`, `GET|PUT|DELETE
  /favorites`, `GET /metrics` (Prometheus).
- `POST /admin/radioreference/sync`, `PUT /admin/systems/{id}/radioreference`,
  `PUT /admin/sites/{id}/radioreference` — the RadioReference mapping and manual sync (§3.5).
- `GET /admin/recorders`, `PUT /admin/recorders/{id}` (the recorder's standby policy — `hot`/`warm`/`cold` and a
  preference number, which the arbiter reads; §5.3, docs/RECORDERS.md), `PUT /admin/recorders/{id}/tuners/{key}`
  (one tuner's desired state), `GET /admin/occupancy`.
- `POST /recorder/observations` — what a recorder sees of a site (control, secondary control, band plan, traffic,
  neighbors, its own configured control frequencies, and the window's message census); `upload` scope, like the
  call push.
- `POST /recorder/events` — a batch of ≤ 1 000 control-channel events about subscribers (§3.6); the same `upload`
  scope and the same token, idempotent on retry, answering `{stored, duplicates, rejected}` rather than refusing a
  batch for one bad event.
- `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`.

---

## 5. Workers

### 5.1 Ingest
0. **Push path** — `POST /api/call-upload` (Rdio-Scanner contract, §1.6) validates the key, applies admission
   control from the form alone (§4.1: disk floor, per-band queue caps, drain ETA, in-flight backlog semaphore),
   spools the body to `uploads/.incoming/` and renames it into `AUDIO_ROOT/uploads/YYYY/MM/DD/<ab>/` once the record
   has resolved the **call's own** start (§1.1, "Storage layout"), then runs steps 3–6 below with
   `time_source='upload'` — step 5 at the priority the upload's class resolves to.
1. Watch **every enabled `watch_dir` in `ingest_sources`** with inotify (watchdog) **and** run a periodic scan
   (`INGEST_SCAN_MINUTES`, default 5) so nothing is missed after downtime.  `AUDIO_ROOT` seeds the first such row
   and remains the archive root every `calls.file_path` is relative to — a directory outside it is watched and
   ingested just the same, with an absolute path in the ledger.  Neither the watch nor the scan enumerates a whole
   tree: under every root, `uploads/` and every hidden directory are pruned before the descent (§1.1, "Don't
   enumerate the tree"), the root itself is watched shallowly with one recursive watch per surviving child
   directory, a day directory that appears later is adopted into its own source, and each scan logs what the walk
   cost.  The watcher re-reads the table every 30 s
   (cheaply: one read of `ingest_config_version`, and only a change costs anything) and schedules or unschedules
   watchdog watches to match, so adding a second recorder's spool, re-pointing one or switching one off takes
   effect without a restart.  A path this process cannot read is **skipped, not fatal**: the row records why in
   `last_error` and the admin page shows it red, because configuring a spool before its mount comes up is a normal
   thing to do and refusing the save would be less useful than showing the state.  A source may override
   `TR_PREFER_AUDIO` and `TR_SIDECAR_WAIT_SECONDS` for itself, since two spools need not be configured alike.
   With no `watch_dir` row at all the watcher falls back to `AUDIO_ROOT`, so a deployment that has never opened the
   admin page behaves exactly as before; with rows that are all *disabled* it watches nothing, because that is an
   instruction rather than an absence. `.mp3`, `.m4a`, `.wav` and a trunk-recorder `.json` are all triggers; `.raw.wav`
   never is. A file is eligible when its size has been stable for `INGEST_SETTLE_SECONDS` (default 3) — sdrtrunk names
   the file at call start and closes it at call end. A trunk-recorder call additionally waits for its sidecar (§1.7),
   and all of its files resolve to one path, so three events become one ingest.
2. Parse filename → fallback; parse ID3 *or* the JSON sidecar → authoritative; read the audio header (mutagen for
   MP3/M4A/WAV, ffprobe when it refuses) for duration/bitrate; sha256.
3. Upsert system/site/talkgroup/radio/patch; write call; update affiliations and counters; conversation grouping.
4. **Reconcile** ([RECONCILIATION.md](RECONCILIATION.md)) — two recorders cover the same system, so one
   transmission arrives twice, from different tuners, with different start, duration and metadata, sometimes days
   apart. Matching is *interval overlap on a channel*, not start equality: the talkgroup sets intersect (or both name
   the same `frequency_hz`) **and** the `[started_at, ended_at]` intervals, widened by `RECONCILE_CLOCK_SKEW_SECONDS`
   (2 s), overlap by ≥ `RECONCILE_MIN_OVERLAP` (0.5) of the shorter — or both edges agree within that allowance
   (capped at the shorter call's own length), which is what carries the short calls that are most of the archive, since
   a file row's start is derived and runs ≈ 2 s late — or the shorter is ≤ 1.5 s and lies inside the longer (a
   late-granted trunkd fragment). An identical `sha256` is the rung above; a *missing* one never matches.
   Both copies are then **scored** (`app/ingest/reconcile.py`), each rank weighted to dominate everything below it
   combined: having audio 10000 ≫ longer duration ≤ 2000 (`4000 × the relative excess`, capped and then withdrawn
   past `RECONCILE_MERGE_RATIO`, where a copy is trunkd's hang timer joining two calls) > the receiver that heard
   the call better, by its own `source_quality` report, 700 > exact `time_source` 300 > metadata richness 32 × 0–8
   fields > audio quality ≤ 20 > a finished transcript 8 > `RECONCILE_PREFER_SOURCES` 3 — and the higher one takes
   the primary row; a tie leaves the existing primary. The loser is **kept** with `duplicate_of` set and its metadata merged into the winner
   (NULL/empty fields only, `call_sources` when the winner has none, its `raw_meta` under `merged_from`). When the
   newcomer wins the pair *swaps*: every pointer and the conversation move to it, and it is queued for transcription
   at priority 1 if the copy it displaced had text. The push path still answers `duplicate call rejected` — sdrtrunk
   string-matches it — but stores the copy. `calls.reconciled_at` / `calls.reconcile_reason` record the ruling.
   `RECONCILE_ENABLED=false` restores the old dedupe (exact `sha256`; fuzzy same-`talkgroup_ids`, |Δstart| ≤ 1 s,
   |Δduration| ≤ 10 %; first copy wins; the pushed second copy is not stored). A batch pass — `keyup reconcile` and
   the worker every `RECONCILE_MINUTES` — does the same for a backlog that arrives after the live copy.
5. Enqueue transcription if the talkgroup's policy allows and `duration_s ≥ transcribe_min_seconds` (default
   0.75 s), at the talkgroup's priority — or at whatever the push path's `priority` class resolved to (§4.1).
   Beside it, write `calls.audio_policy` from the talkgroup's **`record`** toggle the same way
   `transcript_status` is written from `transcribe`: `keep`, or `discard` when Record is off. The audio is stored
   either way — the transcriber and the exporters both read the file — and §5.6 is what deletes it afterwards.
6. Emit `call.new` on the live bus (Postgres `LISTEN/NOTIFY` → API → WebSocket).

### 5.2 Transcription
Pluggable backends behind one interface `transcribe(path, *, language, prompt, vad, word_timestamps) → TranscriptResult`:
- `openai_compatible` — any OpenAI Audio API server (Speaches on a GPU box, or the `stt-cpu`
  container). Config: `base_url, api_key, model, concurrency, timeout`.
- `faster_whisper_local` — CTranslate2 in-process on CPU (`compute_type=int8`, `model=small|medium|large-v3-turbo`,
  `cpu_threads`), so the Keyup host alone can transcribe.
- `ovms` — Intel's OpenVINO Model Server (`openvino/model_server`) over `POST {base_url}/v3/audio/transcriptions`.
  Config: `base_url, model` — where `model` is the server's `--model_name`, not a repo id. It is the only path
  that runs whisper on an Intel Core Ultra's **NPU** as well as its iGPU (openvino-genai's NPU whisper throws on this SKU),
  and it costs API surface to get there: it accepts no `prompt`, no `hotwords`, no `vad_filter` and no
  `response_format` (it takes those fields and ignores them), decodes only mp3/wav — a trunk-recorder m4a is
  transcoded to 16 kHz mono wav first — and answers `{"text": …}` with no segments and no probabilities. The
  result is mapped to one whole-call segment whose duration comes from ffprobe, with `compression_ratio`
  recomputed and an advisory `no_model_metrics` quality reason. In compose it runs in the `stt-ovms` profile,
  one container per device — [docs/STT.md](STT.md).
- `openvino_genai` — OpenVINO GenAI whisper in-process on that host's Intel silicon: `device=CPU|GPU|NPU|AUTO`,
  `model` an OpenVINO IR repo id or a local export. It has no `vad_filter`, so a duration + RMS guard in front of
  the decoder stands in for one, and it reports no `avg_logprob`/`no_speech_prob`, so the post-processing below
  runs on the checks whose inputs survive (`compression_ratio` is recomputed per segment). The API/worker image
  carries no Intel userspace, so in compose this backend runs in the optional `stt-openvino` service and the
  worker reaches it as an `openai_compatible` entry — [docs/STT.md](STT.md).
**Loudness normalization (before transcription).** Scanner traffic swings wildly in level — a paging tone pins the
meter, the dispatcher after it barely clears the noise floor, a distant soft talker sits 20 dB down — and whisper's
feature extractor is not loudness-invariant, so **every clip is loudness-normalized before it reaches an engine.**
This is a preprocessing step inside the audio-prep each backend already runs (they all transcode to 16 kHz mono, or
decode the file to samples): an ffmpeg `-af` filter spliced into that same pass, so it costs no extra decode on the
HTTP backends. It applies to **all** backends — `ovms` (which forces a transcode even for a native wav/mp3 it would
otherwise upload untouched), `openai_compatible` (which otherwise uploads the recording verbatim), and the
local `openvino_genai`/`faster_whisper_local` paths. The filter is `speechnorm` where the installed ffmpeg has it
(purpose-built to raise quiet speech; its `p=0.95` peak is the anti-clip guard), falling back to `dynaudnorm`
(+`alimiter`) then `loudnorm` (EBU R128) — detected once from `ffmpeg -filters`. Default `speechnorm=e=12.5:r=0.0001:l=1`.
`STT_NORMALIZE` (master switch, default on) and `STT_NORMALIZE_FILTER` (override; blank = the detected default) are in
the runtime-settings whitelist, so it toggles in Admin → Settings. If ffmpeg cannot run the filter the clip is
re-decoded without it (logged once) rather than dropped. **The stored recording, its `sha256` and the dedupe are never
touched — normalization affects the transcription input only** (`app/stt/normalize.py`).

Several backends can be configured with priorities; the scheduler routes by talkgroup priority, backend health and
queue depth, and fails over. Jobs are claimed `ORDER BY priority ASC, scheduled_at` with `FOR UPDATE SKIP LOCKED`,
and at most `STT_BACKLOG_WORKER_SHARE` (default 0.5, floored at one thread) of `STT_WORKERS` may hold a
**backlog-band** job (priority ≥ 500) at once — ordering alone cannot preempt a thread already inside a migrated
recording, so a thread is reserved for live traffic instead. The worker logs the queue gauge once a minute. Every request uses `vad_filter=true`, a **domain prompt** auto-assembled from talkgroup
aliases, unit aliases, city names and a user-editable vocabulary list, plus `hotwords` where supported.
Post-processing: hallucination detection (`no_speech_prob > 0.6`, `compression_ratio > 2.4`, empty text, known
phrase list "Thanks for watching" etc.), repeat-loop collapse (identical consecutive segments), quality label.

**Lock order: `calls` first, then `transcription_jobs`.** Every call is written by two paths that can run at the
same second — the STT worker's claim, which cancels the job of a call the audio pass has found no speech in
(`audio_meta.content`, §5.5) and marks the call `skipped`, and the audio pass itself, which writes the call's
`audio_meta`/`audio_policy` and cancels what is queued on it. They took the two rows in opposite orders and
Postgres resolved it the only way it can: 27 deadlocks in a day, a dozen of them surfacing as a failed
`POST /api/call-upload` the recorder spooled and retried. **Any transaction that writes both tables takes the
`calls` row first** — `app.db.lock_calls()` (`SELECT id FROM calls WHERE id IN (…) ORDER BY id FOR NO KEY
UPDATE`, ids ordered so two callers holding the same pair cannot deadlock either), or an `UPDATE calls` as the
first of the two statements, which is its own row lock. The claim commits its `FOR UPDATE SKIP LOCKED` claim statement *before*
touching the call — claiming is by definition a job-row lock, so it ends that transaction and re-opens with the
call locked — then re-reads the job under that lock and leaves it alone if it is no longer `running`: the audio
pass may have cancelled it in the gap, and it already wrote why. Note that `SessionLocal` is `autoflush=False`, so
`call.transcript_status = …` does not write the row until the flush; an ORM assignment followed by an
`update(TranscriptionJob)` is the *wrong* order and needs `lock_calls()` in front of it. The rule holds in the STT
worker (claim/complete/fail/`requeue_stale`/`requeue_failed`), the audio pass, reconciliation (which locks both
calls of a pair up front), `enqueue_transcription`, and the admin force-re-transcribe. `backend/tests/test_lock_order.py`
asserts it both by reading the statements each path emits, per transaction, and by running the two paths against
the same calls in two threads.

**Lock strength: `FOR NO KEY UPDATE`, not `FOR UPDATE`.** The stronger mode cost 17 deadlocks a day on a second,
unrelated pair. The export worker (`app.exporters.worker.ExportWorker._handle`) claims a `call_exports` row with
`FOR UPDATE SKIP LOCKED` and holds it across the encode and the whole HTTP upload — seconds — then records the
outcome, and the `activity_events` row it writes carries `call_id`, so Postgres takes `FOR KEY SHARE` on the
`calls` row for the foreign key. Meanwhile ingest reconcile locks both copies of a transmission and has
`ledger.repoint_exports()` move the loser's ledger rows onto the winner, an `UPDATE call_exports SET call_id = …`
that waits on the row the exporter is holding. `FOR UPDATE` conflicts with `FOR KEY SHARE`; `FOR NO KEY UPDATE`
does not, and nothing inside a `lock_calls()` transaction rewrites `calls.id` or deletes the row, so the weaker
mode is the true one. It still conflicts with every other `FOR NO KEY UPDATE`, with a plain `UPDATE calls`, with
`FOR UPDATE` and with `DELETE`, so the ordering rule above is unchanged — all of the writers it serializes are
non-key updates of `calls`. `backend/tests/test_lock_export_deadlock.py` reproduces the interleaving in two
threads and asserts the compiled statement.

**Caption-credit and sign-off families.** The exact phrase list only fires on an utterance that matches character
for character, and whisper's YouTube-caption credits are endlessly variable — a survey of this archive found 1,005
transcripts containing "captioning provided by" in a dozen spellings, 913 still labeled `good`. `postprocess` also
carries compiled pattern families (`HALLUCINATION_PATTERNS`), matched case-insensitively against the normalized
text:

* **credit** — `closed caption`, `captioning (provided )?by`, `subtitles? by`, `transcribed by`, `transcription by`,
  `amara\.org`, `\bwww\.`, `copyright`, `all rights reserved`, `brandhagen`, `wkno`, `\[music\]`, `♪`, plus
  `\.com\b` **only under 40 characters** (real traffic reads out websites; a two-second call that is nothing but a
  domain does not). These condemn the text wherever they occur — no dispatcher says "closed captioning".
* **sign-off** — `thank(s| you) (so much )?for watching`, `thank(s| you)( all| guys| everyone…)?( so much| very
  much)? for (listening|your attention|your time|joining us|tuning in|watching)`, `see you( guys| all| again)? (next
  time|(in|on) the next)`, `(please |don't forget to )?(like and )?subscribe`, `the end`, `bye bye`, `goodbye`.
  People say these, so the family is gated three ways: **only as the whole utterance** — every word must belong to
  a sign-off or a chained closing (`SIGNOFF_CLOSINGS`: "have a great day", "take care", "good night", "see you",
  "thank you", "thank you for having me", "I appreciate it", "bye"), bar at most three words of filler **in total**,
  each from the closed `SIGNOFF_FILLER_WORDS` list ("and", "I'll", "we'll", "okay", "everyone", …), so "Thank you
  for watching, and I'll see you next time." (1,708 rows in the 2026-10-10 survey) is one sign-off while "Thanks for
  your time, Steve." and "Black Hawk out, thanks for your time." are a person; closings alone never condemn — **only under
  `SIGNOFF_MAX_DURATION_S` = 5 s** of audio (the hallucinated ones average
  1.7–2.6 s; a 4.9 s "116, goodbye." and 6–10 s calls ending "thank you for your time" are real); **at or above
  it only a pure outro** — sign-offs, closings and the connectors that join them in place ("and", "I'll … see you")
  with not one other word, not even filler: a 42 s recording that is only "Thank you for watching, and I'll see you
  next time." is whisper over noise, while "Thank you for joining us today." or "…, sir, and thank you both." keeps
  its text — and **never when the text contains a digit** — a unit number, ten-code, address or plate means a
  person was talking. A duration nobody recorded does not vote either (the trailing-segment check relies on that).
  The repair pass judges `skipped` calls that still carry text as well as `done` ones: the call list shows
  `transcript_text` whatever the status. Credit patterns carry none of these gates: nobody says "closed captioning"
  on a radio, at any length, with any number in it.
* **credit_frame** — broadcast credits: `this (is|was|has been) a (production|presentation|project|program) of`,
  `this (video|program|recording|message|…) (is|was|has been) (brought to you|produced|made possible|presented|funded|
  sponsored|supported)`, `thank(s| you) for (listening|watching|joining us) (to|for) (this|our|the)
  (podcast|video|program|episode|edition|…)`, `thank(s| you) for listening to the u s department of`, and a whole
  utterance that is only `(the )?u s department of <1-5 words>`, all matched on the punctuation-free key. "Department
  of Transportation" and even "a production of" occur in real dispatch, so these condemn the text only when it
  **opens** with the frame (at most three words before it), contains **no digit**, and is at most 60 words — no
  duration gate, because the 10-17 s "…the U.S. Department of Transportation and the U.S. Department of
  Transportation and…" loops are the same hallucination. A real call with a credit glued onto its front ("This program
  is brought to you by Missing True Title, 110 West Aspen Street, …") is kept, prefix and all: the digit guard is
  the trade-off.
* **custom** — one regex per line (or `;`-separated) in the runtime setting **`stt_hallucination_patterns`**
  (Admin → Settings → Transcription → Hallucinations), merged with the credit family at assess time. An entry that
  is not a valid regex is logged once and skipped.

`normalize_text` first applies `HALLUCINATION_REWRITES`, which repair corruptions rather than reject them: a live
dispatch callsign and a school district name that whisper writes as domains (on 730 and 191 calls in the development
archive), sometimes several `.com` deep (`xcom.com.com.com.com.`), are rewritten back to the words they were, each by
a pattern of the form `\bxcom(\.com)+(\.au)?\b → XCOM`. The repeat groups are load-bearing: a rewrite that leaves one `.com` behind hands the
text straight to the credit pattern, which is how a dry run proposed discarding 627 real transmissions. A genuine
captioner credit like `cc by rev.com` is untouched by the rewrites and still rejected.

The reason stored on the transcript row is `hallucination_phrase:<family>:<evidence>` — `phrase:thank you`,
`credit:closed caption`, `credit_frame:<regex>`, `signoff:the end`, `custom:<regex>`. `hallucination_reason()` in the worker consults the
same check, so a phrase hit takes the same path as a repeat loop: the transcript row keeps the engine's words and
is labeled `noise`, and the **call** keeps no text at all and lands on `transcript_status='empty'`. Nothing
requeues an `empty` call by itself — only a human correction or a reconcile that gives it different audio.

`keyup transcripts flag-hallucinations [--dry-run|--apply] [--batch 1000] [--since DATE]` applies today's check to
text already stored — normalized through `normalize_text` (stored text pre-dates the rewrites) and judged with the
call's own `duration_s`, after a SQL `ILIKE` prefilter — leaving each hit exactly as the worker would leave it today. Idempotent, keyset-paged, one activity row per batch.
Checks whose input the engine did not report simply do not vote, and the label then carries a `no_model_metrics`
reason so a `good` resting on the text heuristics alone is not mistaken for a `good` resting on all of them.
Results are stored as a `transcripts` row and denormalized into `calls`.

**Where the backend counters live, and how they reach the UI.** The router's per-backend counters —
attempts, successes, errors, consecutive errors, in-flight, real-time factors, last error, last success — are
plain Python objects (`BackendStats`) mutated under the router's lock, and the only process that owns a router is
`keyup worker`. The API process has neither, so an admin endpoint that answers "what are the backends doing" from
its own memory is structurally reporting zeros: it was building a throwaway router and reading its untouched
counters, which is what made a busy deployment look idle. The crossing is the **`stt_backend_stats`** table: a
`StatsPublisher` thread in the worker upserts one row per backend every ~10 s and immediately when a backend's
health verdict flips (the router calls its `on_health_change` hook), and `GET /api/admin/stt/backends` reads them
back. The upsert sets only non-key columns — the same lock rule as the directory upserts in §3, for the same
reason — and never touches the operator's `admin_*` columns. Rows are **never deleted for age**: a row nobody has
updated for 60 s is reported with `stale: true` and rendered as "worker not reporting since …", because a silent
worker is the thing an operator most needs to see and an empty table looks like an idle system. The same response
carries a **queue block computed in SQL from `transcription_jobs`** — queued live/backlog, running, done and
failed in the last hour, mean `finished_at - started_at`, jobs per minute over the last ten minutes, audio
seconds queued and the ETA at that rate. That half is cross-process truth: it survives a worker restart and is
right with two workers, which is exactly what the in-memory half is not.

The same row carries the operator's **runtime enable/disable override** (`admin_enabled`; NULL = follow
`STT_BACKENDS`), written by `PUT /api/admin/stt/backends/{name}` (a full replace: an omitted field is reset to its
default, not left alone) and read back by the worker's router on a
five-second cache at selection time. So that a switch can be flipped *on* without a restart, the worker builds
every configured backend including the ones marked `enabled: false` and hands the router the disabled names as
`config_disabled`; effective state is the override if there is one, else the config. A backend that is off is
simply not a candidate — attempts already in flight finish normally. When *nothing* is enabled the router raises
`NoBackendEnabledError` rather than the generic failure, and the worker treats that as a **wait**: the job goes
back to `queued` after 20 s with its attempt count untouched, so turning every backend off pauses transcription
instead of parking the whole queue as `failed` three polls later.

**When the backends are enabled but all failing**, that argument does not apply — something really did fail, three
times, 60 s / 120 s / 240 s apart — so the job *is* parked as `failed`: an outage the operator cannot see in the
queue is worse than one they can. What used to be missing is the other end of it. A Whisper outage burns every job
in flight down to `failed` and nothing ever revisits a parked row, so the queue drains itself into the failed pile
and stays there; on 2026-09-07 that was 395 perfectly transcribable calls waiting for somebody to notice and call
`POST /api/admin/jobs/requeue` by hand. So the park is now **annotated**: when the exception that parked a job is
`NoBackendAvailableError` — the router reporting that every backend failed, as opposed to any exception about this
recording — `parked_reason` is set to `'backend_unavailable'`. A content failure leaves it NULL and is never
retried automatically, because it would fail again.

`Worker.run_unpark` (`app/worker.py`, every two minutes, registered beside the stale-job reaper) is what reads it.
It first asks whether at least one backend is currently **enabled, healthy and being reported on**, from the same
`stt_backend_stats` fold that `GET /api/admin/stt/backends` serves (`admin_config.backend_rows`) — it does not probe
anything: an outage is exactly when a probe hangs, and a `stale` row means a worker nobody can hear rather than a
backend that is up. With somewhere to send the work, it requeues `status='failed' AND
parked_reason='backend_unavailable'` in batches of 500 — `queued`, attempts back to 0, locks and timestamps
cleared, `scheduled_at` now, the call's `transcript_status` back to `queued` — keeping `last_error` and prefixing it
`(auto-requeued after outage)`, so the row still says what the outage looked like. One log line and one activity
event (`transcribe.unparked`, "N jobs requeued after the STT outage ended") when it moves anything, and silence
when it does not. The operator's `POST /api/admin/jobs/requeue` runs the *same* function
(`app.stt.worker.requeue_failed`) with different arguments — every failed job whatever parked it, no batch limit,
`last_error` cleared — so the manual and the automatic recovery cannot drift apart.

**A recording every backend chokes on is not an outage.** Because the unpark pass gives the attempts back, a job
misread as an outage never runs out of them: on 2026-09-23 call 5237413, a 43 s BOLO that sends large-v3 into a
repetition loop when the domain prompt is attached, timed out on all three GPU-server backends while they transcribed
everything else, parked as `backend_unavailable`, was requeued, and went round for two hours. So the router now
tells the two apart. Each backend error says whether it `reached` the server (a read timeout or an HTTP status did;
a refused, unresolvable or pooled-out connection did not), and when **every** backend that tried reached its server
**and** at least one of them transcribed something else within the last ten minutes (`SUCCESS_EVIDENCE_S`), the
router raises `RequestFailedEverywhereError` — deliberately *not* a `NoBackendAvailableError` — which the worker
counts like any content failure and parks with `parked_reason` NULL. Anything less (one unreachable backend, or
timeouts with no recent success anywhere, which is what a wedged server looks like) is still an outage. The retry
after such a failure goes **without the domain prompt** (that call transcribed in 52 s without it). A read timeout
is not retried on the same server, and the client deadline scales with the clip (45 s + 1.5 × duration, at most the
old 120 s). The backstop for any misreading is `transcription_jobs.outage_requeues`: the unpark pass counts its
requeues per job and stops at three (`OUTAGE_REQUEUE_CAP`), and a job that fails like an outage after that parks as
`outage_requeue_cap`, which the pass does not look at. An operator's requeue resets the count.

**Where the backend list comes from, and how an edit reaches the worker.**  `STT_BACKENDS` was the only way to say
a backend exists, which made adding a transcription server an edit to a `.env` on the host plus a restart of two
processes.  The `stt_backends` table (§3.4) replaces it, under one precedence rule stated in exactly one place
(`app/stt/config_store.py`): **the database wins; the environment only seeds.**  The migration that creates the
table copies `STT_BACKENDS` into it — but only while it is empty — and stamps those rows `source='env'`; from then
on the environment is consulted only if the table has no rows whatsoever.  That makes the upgrade invisible (the
deployment comes back on exactly the backends it was running), keeps a stale `.env` from silently reverting an
operator's edit, and leaves a never-edited install running off its environment as before.  The first write through
the admin page materializes the environment's backends first, so adding the second backend is not how you lose the
first one.

Every write bumps `stt_config_version`.  The worker's `StatsPublisher` already wakes every 10 s with a session in
hand, so the reload check hangs off that tick rather than a thread of its own: if the version has moved, it builds
a **new** `BackendRouter` and rebinds one attribute on the `RouterHandle` every consumer holds
(`app/stt/hotswap.py`).  Nothing is mutated in place, because a router is not a value — it owns a semaphore per
backend and threads are inside `transcribe` holding permits right now.  Attempts in flight therefore finish on the
router that started them, release *its* permits and update *its* counters; the new router simply starts with fresh
ones.  Nothing is cancelled and no job is lost.  The counters do reset on a swap, which is honest rather than
convenient — they described a router that no longer exists — and `stt_backend_stats.config_version` records which
generation a row's counters belong to, so the page can say "the worker has picked your edit up" instead of leaving
the operator watching a number that has not moved.  A configuration the local host cannot build (an engine whose
package is not installed) leaves the *old* router in place, logs, and does not mark the version applied, so the
next tick tries again and the page keeps showing the worker on the older generation.

`POST /admin/stt/backends/{name}/probe` builds the backend in the API process and asks it two questions: does it
answer its health check, and can it actually transcribe — timed against a short recording **from this archive**,
reported as latency and real-time factor.  The second question is the one that matters, because a server can
answer `/health` and still not have the model loaded, and a synthetic clip would answer a question nobody asked.
Nothing is written: the probe never touches the worker's router, the queue or any call.  Two guards keep it from
being a denial of service against the API it runs in: at most two probes run at once across the process (a third
gets `429`), and a backend whose engine runs **in-process** — `faster_whisper_local`, `openvino_genai` — is
health-checked but not benchmarked, because loading its model into a web worker would contend with the
transcription worker for the same device and time a path the API never takes.  That timing is the worker's to
report.

### 5.3 Retention / maintenance
Nightly: apply retention policies (audio purge or archive move, transcript keep), **purge `radio_events` older than
`RADIO_EVENTS_KEEP_DAYS` (90)** — a clock of its own that runs even when every call policy keeps for ever, because
those rows are presence data about identifiable people (§3.6, docs/RADIO_EVENTS.md §6) — refresh stats materialized
views, detect recorder gaps, vacuum export jobs, rotate notification log.

Everything that removes audio — the retention purge, the archive move, `DELETE /api/calls/{id}?delete_audio=1`, and
the best-source replacement on the push path — resolves the file through `calls.file_path` (`app.audio.audio_path`,
containment-checked), so the layout is not something any of them knows about. Each one then prunes the directory it
emptied, upward, stopping at the first that still holds a recording and never at an audio root: expiring a day of
2022 should not leave 256 empty buckets behind it.

Every `RECONCILE_MINUTES` (default 30): `reconcile_batch` over everything ingested since the previous pass — the
backlog copies that arrive too late for the inline hook to see both sides — logging one summary line
(`reconcile pass: scanned=… pairs=… swaps=… merges=…`). Idempotent, so a re-run changes nothing
([RECONCILIATION.md](RECONCILIATION.md) §4).

Every 30 s, and dormant unless `arbiter_enabled` is on (it ships off): the **standby arbiter**
(`app/arbiter.py`, docs/RECORDERS.md). It promotes a `warm` recorder's tuners for a site when every `hot` recorder
covering it has stopped reporting for `arbiter_stale_seconds`, and demotes them once every hot recorder has been
healthy for `arbiter_recovery_seconds`. It writes through the same locked path as the operator's tuner `PUT`, only
ever flips `enabled`, never touches a `cold` recorder, and never stands down a spare an operator enabled by hand
(`recorders.promoted_at` is NULL for those). A pass that changes nothing writes nothing.

Every `CHANNEL_AUDIT_MINUTES` (default 60): `run_channel_audit` syncs RadioReference when it is due
(`RR_SYNC_HOURS`), recomputes each site's channel audit, raises the `channel_audit` alerts for anything newly
undocumented, and logs one line per site. The report itself is computed live by the endpoint — the job caches
nothing.


### 5.4 Outbound exporters (Broadcastify Calls, OpenMHz)
A worker thread polls the `call_exports` ledger every `EXPORT_POLL_SECONDS` (15) and, **for each service an
operator has switched on**, transcodes the stored MP3 to m4a/AAC — through the same `-af` loudness filter §5.2
uses — and uploads it. `export_broadcastify_enabled` and `export_openmhz_enabled` are runtime settings and both
default **false**: this system is already fed by another uploader, and two uploaders publish every call twice.

Two independent guards, each checked twice (once as SQL when the ledger row is created, once in Python
immediately before the request): the call must have arrived in the **live** admission band
(`calls.ingest_priority`; `backlog`, `bulk` and NULL all fail) **and** have started after the **enable
watermark** stamped when the switch was turned on. That is what keeps the 400 k-call archive migration (§4.1)
out of somebody else's public archive. `UNIQUE (call_id, service)` on the ledger makes a double upload
structurally impossible; failures back off and park at `export_max_attempts`. Nothing here can affect ingest,
transcription or playback.

On top of the two guards sits the operator's **selection**, which is configuration rather than safety and can
only ever narrow what the guards allowed: `export_<service>_talkgroup_mode` is `all` (publish everything and
ignore the per-talkgroup checkbox), `allow` (only the talkgroups whose `talkgroups.export_<service>` box is
ticked) or `deny` (everything but the cleared ones) — conventional calls, which have no talkgroup, are judged
by `site_frequencies.export_<service>` under the same rule — plus `export_include_encrypted`,
`export_min_duration_s` and a per-service list of keyup `systems.id`. Every knob of it, and the credentials and
endpoints too, is a runtime setting edited in Admin → **Exports**, with the environment as the seed and the
settings table winning; the worker re-reads them every cycle. [EXPORTERS.md](EXPORTERS.md).

### 5.5 Audio processing
Calls are recorded as the receiver heard them, and on a live P25 system that is not a consistent product.
Measured over six hours of this archive: the mean level spans **20 dB** between the tenth percentile (-42 dBFS)
and the loudest call (-21); **a third of calls carry more than half a second of dead air** before anybody
speaks (0.5 s on average); some 0.5 s "calls" are empty (-80 dBFS); and a fire page opens with two pure tones
that are the loudest thing in the day (call 1103864, 625 Hz for 1.15-2.11 s then 750 Hz for 2.18-5.09 s).
Playing that back raw means riding the volume knob.

So every stored call gets a **processed copy** — `<stem>.proc.mp3`, beside the original, at the original's
sample rate and bitrate (16 kbit/s floor). The original is never modified: `calls.file_path` and `calls.sha256`
keep naming the recorder's own bytes, so the dedupe, the reconciliation and every "is this the same
transmission" answer are untouched, and `GET /api/calls/{id}/audio?raw=1` still serves them.

**The queue.** `audio_jobs` — a table of its own rather than a `kind` column on `transcription_jobs`, because
the admin jobs page, the upload flow gauge and the drain estimate all mean *transcription* by "a job". Ingest
enqueues one per stored call **before** the transcription job, at the same priority; a worker thread claims
with `FOR UPDATE SKIP LOCKED`, and a failure parks the job and leaves the call playing back raw, which is what
happened before this stage existed. A `UNIQUE (call_id) WHERE status IN ('queued','running')` index makes two
workers rendering the same file structurally impossible.

**The chain**, in the order the audio meets it — every stage a runtime setting (Admin → Audio):

| Stage | Filter | Why here |
|---|---|---|
| trim | `atrim` to the first/last 10 ms window over `audio_trim_threshold_db`, `audio_trim_keep_ms` of padding kept — the boundaries are measured in Python from the same PCM the tone detector reads, rather than left to `silenceremove`, so `audio_meta` records what actually happened and the tone offsets stay in a timebase we computed | half a second of nothing every time somebody clicks play |
| tones | `volume … enable='between(t,a,b)'` (attenuate), `aselect` (remove; on an ffmpeg whose `aselect` drops nothing — 8.0.x — `atrim`/`concat` of the kept spans instead, see below) | **before** the gain: normalizing first sets the whole clip's level from a tone that is about to be turned down 18-30 dB |
| band-pass | `highpass=f=audio_bandpass_low_hz`, `lowpass=f=audio_bandpass_high_hz` (300-3400 Hz; `0` drops an edge) | the band a P25 vocoder carries and nothing else — the rumble below and the hiss above it were never in the codec's output, so cutting them costs no speech and is most of the +13 dB SNR the reviewed chain gains |
| denoise | `afftdn=nf=-30:nr=18` / `arnndn` / `anlmdn` | before the gain, so the gain is not applied to noise about to be removed. `anlmdn` is selectable but **not recommended**: measured to *raise* this system's noise floor 8-9 dB |
| gate | `agate` at a threshold read off *this clip*: 30 dB under its trimmed mean, held in [-70, -40] dBFS and never above `audio_gate_db` | what keeps a 30 dB lift on a quiet talker from raising the hiss between his words — relative because a fixed threshold ahead of the gain sat *above* the content of the quiet calls it was meant to protect |
| loudness | two-pass `loudnorm` at ≥ 3 s, measured `volume=<x>dB` + `VOLUME_BRANCH_OFFSET_DB` below it | EBU R128 cannot integrate a loudness from 1.5 s, and **the median call is 2.2 s**; the offset is what keeps the two branches at the same level |
| limit | `alimiter` at `audio_true_peak_db`, `level=disabled` | the one promise the file makes: nothing in the archive clips |

**Tone removal on ffmpeg 8.** ffmpeg 8.0.x's `aselect` passes every frame whatever its expression
says (an upstream regression), so a removal would encode the tones untouched while `audio_meta` said
`tone_removed`. `aselect_works()` in `app/audioproc/process.py` probes it once per process (half a
second of silence through `aselect=0` must come out empty); when it fails, the chain cuts the tones
with `atrim` segments joined by `concat` instead, and logs `this ffmpeg's aselect drops nothing`.
The output is the same audio either way; the backend image's ffmpeg 7 takes the `aselect` path.

The default chain on a short untrimmed call at -30 dBFS is
`highpass=f=300,lowpass=f=3400,afftdn=nf=-30:nr=18,agate=threshold=0.001:ratio=4:attack=5:release=250,volume=<measured>dB,alimiter=limit=0.630957:level=disabled:attack=5:release=50`
— the gate's number moves with the clip, so a call that arrived at -20 dBFS gets the `0.003162` (-50 dBFS)
that used to be there for everything.

**The gate follows the clip.** `audio_gate_db` alone is an absolute threshold applied *before* the gain, and
that is only the right number for a call that arrived at a normal level. Production 2402904 measures
-46.28 dBFS whole-clip, so the -50 dB default sat **3.7 dB under the content**: a 4:1 downward ratio was
applied to the speech itself, and the `volume=30dB` the chain asked for came out as 19. Measured over the last
500 processed calls, **18 of the 18** calls more than 6 dB below the archive's median level are on the
`volume` branch and not one is a `loudnorm` undershoot; 2405543 was given 30 dB and came out +16.1.
`gate_threshold_db` therefore reads `min(audio_gate_db, clamp(trimmed_mean - 30, -70, -40))`.

Two properties made this the choice over the other candidate, *moving the gate behind the gain stage*. On a
normally-levelled call it is **exactly the old number** — a clip at -20 dBFS asks for -50, which is the default
— so the change touches the quiet tail and leaves every call the chain already got right bit-identical, where
gate-after-gain changes the level of the whole archive. And it keeps the gate where the chain's own argument
puts it, ahead of the lift: gate *then* lift raises the speech and not the hiss between the words, while lift
*then* gate has already made the roar it exists to prevent. Measured on a 2 s excerpt of real dispatch speech
at -55 dBFS, both orders asked for the +30 dB cap; the fixed gate delivered **+0.33 dB** and the relative one
**+17.50 dB**.

**And the two gain branches land in the same place.** `gain_for` aims `audio_target_lufs` — a LUFS number — at
the clip's **RMS**, and the two are not the same scale: LUFS is K-weighted and gated, so it reads a clip with
inter-word gaps above its flat RMS and a chain that hands the RMS the LUFS target lands under it.
`VOLUME_BRANCH_OFFSET_DB` (1.0 dB) is added on the short branch so a listener scrubbing a day of traffic does
not hear a step at `LOUDNORM_MIN_S`. The constant is deliberately smaller than the **3.2 dB** gap between the
two branches' *population* medians (`volume` -21.41 dB at a median 1.58 s, `loudnorm` -18.20 dB at 6.41 s):
that gap is measured between different calls, and a controlled measurement — the *same* recording cut either
side of the boundary — puts the scale error at 0 to 3.5 dB depending on how much silence the clip carries,
median about 1. Much of the population gap was the gate above, not the scale; it should be re-measured on the
archive once it has been reprocessed at v3, and this constant retuned then.

**Two things about that limiter, both learned the hard way.** In one afternoon 804 of about 930 processed calls
stored `peak_db_after` = 0.0 — every one of them decoding at full scale — and neither cause was the loudness
stage.

*`level=disabled`.* ffmpeg's `alimiter` defaults to `level=1`, "auto level", which normalizes its output so the
loudest sample lands **exactly on** `limit`. That is a maximizer, not a limiter: it lifts a clip that was never
close to clipping, and it makes the measured peak equal the ceiling by construction rather than as a cap. Call
2402902 arrived peaking at -8.4 dBFS and left the pass pinned to the ceiling. With auto level off the filter
only ever attenuates, which is the whole job. `attack=5:release=50` are the filter's own defaults, written out
so the chain recorded in `audio_meta.filter` says what it did.

*The ceiling is -4 dBFS, not -1.5.* `alimiter` caps a **PCM sample** peak, and what a listener plays is a
16 kbps MP3 at 8 kHz. A lossy decoder does not reproduce the samples it was handed; it reconstructs a waveform
that overshoots them by 1.5-2 dB on this material — so a chain that stops at -1.5 dBFS decodes above full scale
and the decoder clips. -4 dBFS is that headroom, and it is why `audio_true_peak_db` defaults there. `loudnorm`'s
`TP=` follows the same setting, so the two ceilings agree and the limiter is left as insurance rather than as
the stage doing the work. `peak_db_after` is itself measured by decoding the file that was written, which is
what made the symptom visible in the first place.

**Audio profiles** ([docs/AUDIO.md](AUDIO.md)) replace the *denoise* and *gate* rows of that table — and only
those two — for the talkgroups pointed at one. Those are the stages that are about a channel's own noise; the
trim, the tone envelope and the loudness tail are about every recording and are not a profile's to change.
Talkgroup 1034 carries medevac transports whose rotor and turbine energy sits in a band around 1.9 kHz that no
global denoiser setting removes without eating the speech on four hundred other channels, and a 40 dB cut from
1.7 to 2.2 kHz removes it; that is the shipped `Aircraft cabin` profile. A profile is stored as an ordered list
of whitelisted steps in `audio_profiles.chain` (JSONB), never as a rendered command line, and
`app/audioproc/profiles.py` is the only thing that turns one into an ffmpeg filter graph.
`talkgroups.audio_profile_id` is NULL for every channel until an operator sets it, and NULL means the settings
above.
`audio_meta.profile` records which profile produced each file.

**Tone detection** is Python over the same 8 kHz PCM decode the trim measurement uses: 32 ms frames at 50 %
overlap, a frame counts as tonal when its dominant bin (with the two the Hann window leaks into) carries ≥ 45 %
of the frame's energy, the frame is above -35 dBFS and the peak is inside 300-3500 Hz. ffmpeg can filter a band
but cannot answer "which frequency held most of the energy, and for how long", which is why this is numpy and
not a filter.

Duration alone cannot then separate a tone from a voice, and a 0.3 s floor on its own got it wrong in both
directions: over seven days it flagged **291 calls `single`**, most of them 0.3-0.4 s at 300-600 Hz — held
vowels, which the 24 dB attenuation then punched a hole in — while a **warble** that alternates 878.1 and
467.9 Hz every 0.25 s (calls 1170192, 1170733) produced nothing at all, and one where only the high halves
cleared the dominance test (1169412) was attenuated on those halves alone. What actually separates them is
**frequency stability**: an encoder holds its frequency for the whole run and a voice does not. Every run is
measured as the 90th-percentile deviation of its per-frame interpolated peak from the run's median — 0.0-0.8 Hz
on 1169412's tone halves against 18-25 Hz on 1170568's vowel — and `MAX_FREQ_SPREAD_HZ = 6.0` is the gate
(`_FREQ_TOLERANCE_HZ = 45` still decides only where one run *ends*). Duration is then applied by context:

| Rule | Each run | Also | Pattern |
|---|---|---|---|
| ≥ 3 runs, gaps ≤ `MAX_PATTERN_GAP_S = 0.15 s`, one or two frequencies | ≥ 0.15 s | — | two frequencies → `warble`; one, or three-plus → `alert` |
| ≥ 3 runs at **one** frequency, gaps ≤ `MAX_REPEAT_GAP_S = 0.5 s` | ≥ 0.15 s | — | `alert` — an evacuation tone, or a warble whose quiet halves went unseen |
| two left over, gap ≤ 0.4 s, ≥ 50 Hz apart | ≥ 0.4 s | — | `two_tone` (Quik-Call II) |
| one left over | ≥ 0.5 s | ≥ 55 % dominant, spread ≤ 6 Hz | `single` |
| one left over, **held** | ≥ 1.0 s | ≥ 45 % dominant, spread ≤ `MAX_SINGLE_SPREAD_HZ = 20 Hz` | `single` |

The second `single` row is the **held-tone** rule, added for call 2394781 (talkgroup 1001): 1.41 s of one
993.8 Hz tone at -14 dBFS, the loudest thing in the recording, 0.87-1.00 dominant in every frame — and rejected,
because its interpolated peak wanders 18.3 Hz (p90). A vocoder does not reproduce a frequency the way an
encoder generates one, so the encoder-grade 6 Hz gate throws real tones away once they have been through a
vocoder, and this one went out at full level into a loudness stage that then set the clip's gain from it. The
rule that lets it back in is duration and spread **together**: a full second inside ±20 Hz. Neither half decides
anything alone — the false-positive vowel of call 1170568 measures 18.4 Hz of spread, the same number, and is
excluded because its runs are 0.38 s and 0.16 s against a 7-day false-positive population that peaked at
0.3-0.4 s. A run admitted this way is *isolated only*: it can never form a `two_tone` pair or a pattern, both of
which still require every run to clear the tight 6 Hz limit. **Harmonic folding was tried and rejected** for the
dominance fraction (counting the energy at 2× and 3× the peak as part of the tone): measured over the fixtures
it lifts nine frames of the 1170568 *vowel* over the frame test and not one frame of 2394781's tone, because
speech is harmonic too and a voiced frame's peak is usually itself a harmonic.

Repetition is the evidence a short run lacks on its own, which is why a pattern's runs may be a sixth of a
second and an isolated one may not. A run adjacent to an established pattern and at one of the frequencies that
pattern is already alternating between is absorbed into it whatever its own spread says — a warble fading out
ends in halves that measure 7.6-8.2 Hz, and dropping them would end the attenuation short of the sound.

**Adjacent includes overlapping by one hop.** A run's end is the end of its last *frame*, not of its last hop,
so two runs consecutive in frame numbering report times that overlap by exactly one 16 ms hop. Testing that gap
against zero rejected the very edges the absorb step exists to recover. Production call 2403028 is a
878.1/467.9 Hz warble running **1.224 s to 4.312 s**, and its span stopped at **3.064 s**: two mid-warble halves
failed the 6 Hz stability gate, which opened a 0.24 s hole wider than `MAX_PATTERN_GAP_S`, and the frayed edges
past it could not be pulled back because they overlapped. The 1.25 s of warble left un-attenuated came out of
the chain at -12.44 dB RMS — **4.6 dB louder than the speech** behind it and 16.4 dB louder than the half of the
same warble that *was* turned down, which is a tone that drops out and comes back at full volume. The tolerance
is one hop and nothing wider: a genuinely overlapping pair is one run the frame loop split, while a run starting
well before the chain ends is a different sound.

**Attenuation is per pattern, not per run.** `ToneReport.spans` merges each pattern's runs into one span, first
start to last end plus 40 ms either side, and that is what the chain builds from; `ToneReport.segments` keeps
the per-run detail for the UI and for `audio_meta.tones`. A warble is therefore one `volume…enable=between`
stage over the whole burst rather than six over its halves with the gaps at full level.

**And how far a span comes down depends on what it is.** Each span carries its pattern name
(`ToneSegment.pattern`, in `audio_meta.tone_spans`), and the chain emits one `volume` stage per span with its
own depth. A `two_tone`, `warble` or `alert` — somebody was toned out — is *information*: a listener wants to
hear the page under the dispatch that follows it, so it gets `audio_page_tone_attenuate_db` (18 dB: off the top
of the archive, still plainly there). A `single` is the console's held attention tone in front of a dispatch —
call 2396065 is 1.95 s of 993 Hz at -10.5 dBFS before 17 s of speech — which carries nothing and is only loud,
so it gets the deeper `audio_tone_attenuate_db` (24 dB shipped, 30 dB on this deployment). One setting for both
was the old behaviour and could not be right for both at once: deep enough for the attention tone flattened the
page, and shallow enough for the page left the attention tone as the loudest thing in the call.

**And the dip has to survive every later loudness stage.** Each stored span records the depth it was actually
turned down by (`audio_meta.tone_spans[].attenuate_db`), because two things downstream measure the whole clip and
would otherwise undo the work: the **export** loudness stage
([EXPORTERS.md](EXPORTERS.md), *Loudness*), which re-applies the envelope after its lift, and the **player's
Normalize** compressor, which schedules the same dip on a gain node of its own while it is switched on
(`frontend/src/components/player/normalize.ts`; off, it is not needed, because the stored file already carries
it). Call 2396243 is what proves it: 3.38 s that is nothing but a 993 Hz tone, taken to -33 dB in the archive and
handed back at -11 LUFS by both of them. The spans are recorded in the *original's* timebase, so `head_trim_s`
comes off them wherever the processed copy is the source.

**No-speech calls (`audio_meta.content`).** `audio_silent_below_db` asks how loud the whole clip is, and a mic
key-up answers "loud enough": production calls 2395971/2395972 are 0.35 s at -80 dBFS and then one 30 ms click
at -22 dBFS, peaking at -9 — a whole-clip mean of about -50 dBFS, comfortably past the -60 silent floor, with
nobody having said a word. The question that separates them is not how loud the clip is but **how much of it
is**, so the pass also measures the *seconds above a floor* on the processed copy — after the trim, the gain and
the tone handling, so the floor means the same thing for a quiet talker as for a loud one. `silencedetect` at
`audio_speech_floor_db` (-40 dBFS) reports the quiet stretches, and what is left — **less the tone spans**, because
a page or a console tone is loud and is not somebody talking — is `audio_meta.speech_s`. That exclusion is what
makes call 2396243, 2.0 s of 993 Hz and nothing else, a no-speech call rather than two seconds of "speech". Under
`audio_min_speech_seconds` (0.25 s) the call is `audio_meta.content = "none"`; otherwise `"speech"`.

**Except static, which that measure cannot see at all.** Production calls 2403008, 2402904 and 2402672 are
broadband vocoder noise across the whole 300-3400 Hz band — a squelch tail or a failed decode, not a quiet
talker — and all three were stored `"speech"` with 0.4-1.3 s of "speech" in them, because `speech_seconds`
counts loud seconds on the **processed** copy and the gain stage had just lifted them 24-30 dB clear of the
floor. 2402672 measured 1.306 s of speech in a 1.584 s clip whose frame-level dynamic range is 4.94 dB.
What separates them is **in-band spectral flatness** — the geometric mean of a frame's power spectrum over its
arithmetic mean, 1.0 for white noise and near zero for a vowel, which puts its energy in formants and leaves
nulls between them. A call is `content = "static"` when

> the share of live frames (above -60 dBFS) whose 300-3400 Hz flatness exceeds `audio_static_flatness` (0.30)
> is at least `audio_static_frame_share` (0.70) **and** the trimmed mean is at or under -35 dBFS.

Measured: 3 of 3 static calls flagged (flat share 0.82/0.90/0.93), **0 of 36** others — the twelve quietest
real calls in the archive measure 0.02-0.20, and the nearest miss is 2402676 at 0.70 but -26.5 dBFS. The
**level term is the one carrying the rule**: `speechish` broadband speech and production 2405803/2405804 (the
same transmission recorded twice) measure 0.74-1.00 flat and are excluded on level alone, leaving the flatness
axis only about 0.05 of margin. The share is stored per call as `audio_meta.flat_frac` so the rule can be
audited against the archive rather than argued about.

It is measured on the **recorder's own file**, not the processed copy, and that is not a detail: on the
processed copy the three static calls come off the chain at -28.1 to -29.0 dBFS, above the level term, so the
rule flags **none** of them — while a genuinely quiet call (2405911) rises to 0.72 flat and starts to look like
one. The raw decode is also the one the trim measurement already has in hand, so this costs an FFT over a
couple of seconds and no extra decode.

`static` is handled **exactly like `none`** — the three consequences below read
`app.audio.NO_CONTENT`, which holds both — and is a separate name only so the archive can say *which*, the
same reason `page` is not `speech`: a mic key-up is quiet traffic, and a channel delivering noise is a radio or
an RF fault. It is counted apart as `static_24h` on `/api/stats/overview`, and the export and transcription
skip reasons say `static` rather than `no speech`.

**Except a page.** A `two_tone`, `warble` or `alert` pattern makes the call `"page"` whatever the speech seconds
say: production calls 2396355 and 2396357 are 4.25 s fire dispatch tone-outs — an A tone of about a second, a B
tone of about 2.75, and not one word — so by speech alone they are indistinguishable from a mic click, and the
discard pass would have taken the most important audio on the system. A lone `single` is deliberately *not* a
page: that is the console's attention tone, which is why the test is the span's pattern rather than "a tone was
found". `page` is treated exactly like `speech` everywhere below.

Three things follow, and **only from `content = "none"` or `"static"`** (`app.audio.NO_CONTENT`):

- **transcription** — the STT worker re-checks `audio_meta.content` when it *claims* a job and cancels it with
  `no speech` — or `static` — (the call's `transcript_status` becomes `skipped`). The re-check is on the claim rather than at
  enqueue time because ingest queues transcription and the audio pass on separate queues, so the job usually
  exists before the verdict does;
- **export** — `skip_reason_for` refuses it with `no speech (0.04 s above -40 dB)`
  (`ledger.NO_SPEECH_REASON`), or `static (0.93 of frames flat)` (`ledger.STATIC_REASON`), so the ledger
  records that we saw it and decided against it. The **live
  Broadcastify Feed** passes `no_speech=False` and streams it anyway: a feed is the channel as it sounded, and
  the recorder-direct upload path (§5.7) never consults the ledger at all;
- **retention** — `calls.audio_policy` is set to `discard` when `audio_discard_no_speech` (default on) is,
  which hands the recording to the §5.6 discard pass after its grace. The row, the talkgroup and the radio stay
  for ever; only the click goes. `/api/stats/overview` counts these as `no_speech_24h` and `static_24h`,
  footnoted on the Stats page beside the discard count.

`two_tone`, `warble` or `alert` beginning inside the first 8 s sets `calls.alert` — a **page**. That is
deliberately *not* the P25 `emergency` flag: emergency is what the radio asserted, alert is what the audio
sounds like, and neither ever overwrites the other.

**Consumers.** `calls.processed_path` and `calls.audio_meta` (levels before/after, trim, gain, tones, pipeline
`version`) are written by the pass. The audio route serves the processed copy by default and answers
`X-Keyup-Audio: processed|raw`; the waveform is computed from it (its cache is keyed by the decoded file's
name and size, so a copy written after an envelope was cached rebuilds it); the transcription worker
transcribes it and **skips its own §5.2 normalize filter** because doing it twice expands what is already even;
the exporters upload it, un-normalized for the same reason. Retention deletes the copy wherever it deletes or
archives the original — a regenerable derivative is not worth the archive tier, and leaving one behind would
leave playable audio for a call the policy said to forget.

**Backfill.** `keyup process-audio [--since] [--talkgroup] [--ids] [--only-tone-flagged] [--force]` and
`POST /api/admin/audio/reprocess` queue at the backlog band (600). Without `--force` they are *lazy*: only
calls with no processed copy or one from a pipeline older than `audio_reprocess_version`. Raising that setting
and running the backfill is therefore the whole procedure for applying new settings to the archive, and running
it twice queues nothing the first run finished. `only_tone_flagged` narrows the selection to calls whose stored
pass found a tone, which is what a change to the *detector* has to be re-run over — a call it heard nothing in
cannot have been attenuated wrongly. `PIPELINE_VERSION` is **3**: v1 rows carry the pre-stability detector's verdicts, and v2 rows carry four
things this section has since changed — a warble absorbed only to the first overlap, a gate that sat above the
content of a quiet call, a short branch without its offset, and no `static` verdict. Nothing reprocesses on a
bump by itself: the version is only read by `select_for_reprocess`, so an operator chooses when and over what.
The narrow re-run for the tone change alone is
`keyup process-audio --only-tone-flagged --force`.

### 5.6 Audio discard (the per-talkgroup **Record** toggle)

`talkgroups.record` is the companion to `transcribe`: that one decides whether the *words* are kept, this one
whether the *recording* is. Both default true, so the column changes nothing on its own.

Record off does **not** mean the channel is ignored. The call is ingested, counted, given its metadata row for
ever, transcribed when `transcribe` is on and uploaded when an export service selects it — and only then is the
file deleted. Deleting at ingest would be deleting the input to the two jobs the operator did ask for, so ingest
only writes the decision down (`calls.audio_policy = 'discard'`, §5.1 step 5) and a worker pass collects the rows
once every reader is finished with them.

**The pass** — `app/audio_discard.py`, `Worker.run_audio_discard` every **60 s**, batches of 500, oldest first.
A call is eligible when all of:

- `audio_policy = 'discard'` and `audio_state = 'present'`;
- `ingested_at` is at least **120 s** old. Export *discovery* runs on its own poll and writes the `call_exports`
  ledger rows; without the grace, "no pending export row" would be read as "nothing wants this" when it means
  "nobody has looked yet";
- `transcript_status` is terminal — `done`, `empty`, `skipped` or `failed`. `skipped` is what makes the both-off
  case immediate: a talkgroup with `record` and `transcribe` both off never queues a job, so its calls are
  eligible the first pass after the grace;
- no `call_exports` row for the call is still `pending`.

It then deletes the original **and** the processed copy beside it, through `app/retention.py`'s own
`_drop_processed` / `_resolve_audio` helpers rather than a second implementation, prunes the empty partition
directory, cancels any **queued** `audio_jobs` row for the call (a job that ran afterwards would write a
processed copy of a recording that no longer exists — the same `queued`-only rule reconciliation uses), and sets
`audio_state='discarded'`, `processed_path=NULL`, `file_size=NULL`. `duration_s`, the transcript and every count
are untouched: the archive still knows the transmission happened. One activity line per batch
(`audio.discarded`, with the count and the bytes), never one per call. `GET /api/stats/overview` carries
`audio_discarded_24h`.

`discarded` is a fourth `audio_state` rather than a reuse of `purged` because "expired after its retention
window" and "was never meant to be kept" are different answers to "where did the audio go". Everything that
already tests `audio_state <> 'present'` — `has_audio`, the `410` on the audio route, the player — is correct
for it without being told.

**Stopping it at the source.** When a talkgroup has `record` off, `transcribe` off *and* no export service
selecting it, keyup would ingest a file, store it and delete it having learned nothing — so the recorder is told
not to send it: `recorder.skip_talkgroups` in the desired state (§4.8, `{tgids, revision}`, null when empty).
Keyed by tgid alone, because that is all a recorder has, so a tgid is listed only when *every* talkgroup row
carrying that number is skippable. It is an exclusion list on purpose: a recorder that cannot reach keyup, or one
on an older build, keeps uploading everything, and keyup stores the metadata and discards the audio at once.

---

## 6. Frontend (React SPA)

Pages: **Calls** (filter rail — including "Pages only" and "Any tone", which filter on what the §5.5 audio pass
heard rather than on the P25 emergency flag — virtualized table with a tone/page/silent badge per row, sticky
player with continuous / auto-play-new / normalize / **processed** / live toggles (Processed is on wherever a
processed copy exists; turning it off requests `?raw=1`, the recorder's own bytes), keyboard shortcuts; a results toolbar carries an "Audio only" toggle — on by default, with a count of the
metadata-only calls it is hiding — a quality filter (all / one level / hide noise), a column picker and a
two-row "Transcript row" view, the last two remembered per device in `localStorage`); Normalize levels playback
loudness live in the browser — the `<audio>` element is routed through a Web Audio compressor and make-up gain, so
it works on every archived call and changes no files, and because `createMediaElementSource` claims an element
permanently the graph is built lazily on the first enable and "off" is a bypass, never a teardown), **Call** detail (waveform, transcript with segment highlighting synced to playback, adjacent
calls / conversation thread, tags, notes, links, share deep-link), **Search** (highlights, facets, a **Spelling variants** switch that widens each word to its near spellings in the
archive and says which it searched, and an automatic widening, announced, when nothing matches exactly),
**Live** (rolling
feed with filters, "who's active"; a live arrival older than every row the Calls list holds — the backlog
re-transcribing months-old calls — is left for a later page rather than appended under the reader), **Talkgroups**, **Radios/Units** (with a presence block and the control-channel timeline, §3.6), **Sites** (the tower sites and their channel
audit: the control-channel row, undocumented and missing channels, adjacent sites and the control-frequency list to
paste back into the recorder), **Statistics** (activity heatmap, per-TG volume, transcript coverage, STT
throughput, recorder gaps), **Alerts**, **Admin** (ingest, jobs, backends, recorder, **audio** —
the §5.5 chain, what it has covered, a "run it on call #" preview that stores nothing, the backfill button, and
the **audio profiles** ([docs/AUDIO.md](AUDIO.md)) card: the per-channel chains, a step editor that builds its
fields from the server's own whitelist, and an A/B preview that renders a chain over one call without storing
anything —
retention, settings, tokens, users), **Help**. The sidebar's **Transcripts** entry is not a route: it opens
`/embed/transcripts` — the iframe-able live transcript page, its own Vite entry served by nginx
([EMBED.md](EMBED.md)) — in a new tab; it scrolls on into older calls a page at a time with a divider at each
change of day, and like the Calls list ignores live transcripts of calls older than the window it opened with. Every control has a Radix tooltip explaining what it does; the tooltip texts live in
one `help.ts` map so they are consistent and testable. Mobile: filter rail collapses to a sheet, table becomes cards.

---

## 7. Functional map (what the system does, grouped)

| Group | Capabilities |
|---|---|
| Capture & ingest | watch/scan sdrtrunk and trunk-recorder folders, HTTP push from trunkd/trunk-recorder/sdrtrunk, parse filename/ID3/JSON sidecar, per-transmission keying radios, dedupe and cross-recorder reconciliation with best-copy selection (each recorder's `source_quality` report; a better copy from another instance replaces the audio), patch awareness, failure ledger, rescan/retry, gap detection |
| Directory | systems, sites, talkgroups (alias/category/agency/priority/policy), radios & units (aliases, kind, agency, home WACN/system from LC 9), the identity roster (units, personnel and
dated radio assignments, CSV import/export — filled by hand or import; talker aliases do not propose entries yet), patches, affiliations (from calls *and* from the control channel), radio presence and its event timeline, playlist import |
| Transcription | GPU or CPU backends, failover, per-TG policy, VAD, domain prompt, hallucination scoring, re-run, human correction, multiple transcripts per call |
| Search & discovery | FTS with highlights, spelling variants (trigram vocabulary, on request or when nothing matches exactly), time-of-day/dow filters, facets, saved searches, conversation threads |
| Playback & live | range streaming, waveform, continuous/auto-play, live WebSocket feed, embeddable live transcript page (`/embed/transcripts`), M3U, share links |
| Analytics | activity heatmaps, per-TG/radio stats, transcript coverage, STT throughput, recorder uptime, site radio-event summaries, the control channel's message census |
| Integration | uncapped export, async audio bundles, persisted links to outside records, webhooks/alerts (rule kinds in `app/alerts.py`, delivered by webhook, Discord, ntfy or email), RSS/Atom feeds, Broadcastify/OpenMHz exporters, Prometheus, scoped API tokens |
| Operations | retention/archive tiers, settings UI, users/sessions/tokens, health/status, backups |

Features the brief did not ask for but that this design includes because the data supports them: conversation
threading, unit affiliation history, keyword/regex alerts, hallucination scoring and correction workflow, recorder gap
detection, patch-aware talkgroup queries, auto-built STT vocabulary prompts, per-talkgroup retention tiers.
