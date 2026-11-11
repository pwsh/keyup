# Outbound exporters — Broadcastify Feed, Broadcastify Calls and OpenMHz

Keyup can re-publish this system's live traffic three ways, and **all three ship disabled**
because another client is already doing each job:

| Destination | What it is | Credentials | Switch |
| --- | --- | --- | --- |
| **Broadcastify Feed** | one **continuous audio stream** to an Icecast mount — the live stream on the feed's page | Server / Port / Mount / Password / Username | `export_broadcastify_feed_enabled` |
| **Broadcastify Calls** | one HTTP **upload per call** into their searchable archive | API key + numeric system id, per environment | `export_broadcastify_enabled` |
| **OpenMHz** | one HTTP upload per call | API key + `shortName` | `export_openmhz_enabled` |

> **Terms of service.** Each destination has its own terms, and you are the one agreeing to them: read
> [Broadcastify's terms](https://www.broadcastify.com/terms) and the Broadcastify Calls and OpenMHz
> contributor rules before you turn anything on. Use only credentials issued to you for a feed or system you
> are authorised to provide, upload only traffic you may lawfully rebroadcast (no encrypted or
> restricted channels: see the talkgroup selection below), and keep to the documented APIs. Keyup ships
> no keys of its own.

The two Broadcastify rows are **different products with different credentials**, and the
one mistake that costs an afternoon is using one's credentials for the other: the mount
password is refused by `call-upload`, and the Calls API key is refused by the mount.

Code: `backend/app/exporters/`. Table: `call_exports` (the two upload services only — the
feed has no ledger). Worker thread: `app.worker.Worker.start_exports`, which drives the
upload cycle and, beside it, the feed's own source thread.

## The two safety rules

A 400 000-call sdrtrunk archive migration streams into the same `calls` table as the live
recorder. Nothing is uploaded unless it passes **both** of these, and both are checked
twice — once as SQL when the ledger row is created, once in Python immediately before the
request goes out (`app/exporters/gate.py`):

1. **Live band.** `calls.ingest_priority` must be `live`/`urgent` or an integer below
   `BACKLOG_BAND` (500). `backlog`, `bulk` and **NULL** all fail. NULL is the file-watcher
   and bulk-import shape, so "we cannot prove this is live" means "do not publish it".
   trunkd's uploader sends `priority=live` on every call, so live traffic always passes.
2. **After the enable watermark.** Turning a switch on writes
   `settings['export_<service>_enabled_at'] = now`; a call whose `started_at` is not
   strictly after that instant is never uploaded. Turning the switch off deletes the
   watermark, so a disabled service can neither accumulate a queue nor flush one.

`export_max_age_minutes` (60) is a third, softer bound, and it applies to **discovery
only**: a call already more than an hour old the first time the worker sees it never gets a
ledger row at all, so a worker that was down for an afternoon starts from the last hour
rather than replaying the whole gap. It does not touch rows that already exist — those keep
retrying on their own backoff until they succeed or park.

## Broadcastify Calls publishes trunked systems only

A rule, not a setting: **no call from a system that is not trunked is ever uploaded to
Broadcastify Calls.**  Calls files every upload under a system id issued for a trunked
system; a conventional channel has nothing to be filed under, and keyup archives both kinds
side by side.  Neither the Feed nor OpenMHz is gated on this — conventional traffic is
perfectly at home on a live stream and on OpenMHz.

Whether a system is trunked is `systems.trunked`, a nullable boolean an operator edits
(`PATCH /api/systems/{id}`, and the Systems list on the Sites page):

* **`true` / `false`** — the operator has said, and nothing overrules it.
* **`NULL`** — nobody has said, so it is derived from `systems.protocol`: a name containing
  P25/APCO, DMR, Motorola, SmartNet, SmartZone, EDACS, LTR, NXDN, TETRA or "trunk" is
  trunked, *unless* it also says conventional or analog (`DMR conventional` is not a
  trunked system).  A blank protocol is **not** trunked — this fails closed, because "we
  are not sure" has to mean "do not publish" where somebody else's archive is concerned.
  The migration `b7c4d2e91a35` wrote that derivation over the rows that already existed, so
  an existing system's answer is a stored fact; only systems created later fall back to the
  guess.

`GET /api/admin/exports` reports `trunked_systems`, and the Exports page uses it three ways:
the Calls card lists them, its enable switch is **disabled with the reason** when the list
is empty, and the `export_broadcastify_systems` field is a picker over that list rather than
a free-text box (a conventional system's id typed there would be refused anyway).

The rule is enforced twice, the same way the two safety rules are.  Discovery filters
non-trunked systems out in SQL, so no ledger row is created for a call that could never go;
and a row that was discovered before somebody corrected a system's protocol is refused at
upload time with the distinct ledger reason **`not_trunked`**, which is the one skip reason
worth grepping for when the answer to "why is nothing uploading" is not the switch.

**Analog calls never go to Broadcastify Calls, whatever system they are filed under.**  A call
whose recorder said it is analog narrowband FM — `raw_meta.mode` or `raw_meta.decoder` equal
to `nbfm` (any case), which trunkd sends on every call from a conventional `[[channel]]` and
on nothing else — is refused by the same two layers with the same reason `not_trunked`, even
when its system is trunked or an operator set `systems.trunked = true`.  That covers a
conventional channel deliberately filed under a P25 system's label.  Nothing else marks a
call analog here: trunk-recorder's `audio_type=analog` is also what an analog talkgroup on a
trunked system says, so it is not read.  A system that keyup *first* sees from such a push is
created with `protocol = 'NBFM'` (from the push's `decoder` field), so its `trunked` guess is
false as well; an existing system's protocol is never changed by it.  The Feed and OpenMHz
take analog calls as before.

This is keyup's own uploader.  With `broadcastify_upload_at = recorder` the recorder
decides at call end from the desired-state block below, which carries no rule about analog
calls: in `allow`/`deny` mode its talkgroup list holds only trunked systems' talkgroups (so
a channel on a non-trunked system is never listed), but in `all` mode, or for a channel
filed under a trunked system with its Calls box ticked, the recorder itself must refuse
conventional-channel calls.

## Which talkgroups — the selection rule

The two safety rules above decide what an exporter *may* publish.  On top of them sits what
the operator *wants* published, and all of it is a runtime setting (Admin → **Exports**).

Every talkgroup carries **three** checkboxes, all **true** by default — one per
destination, because Broadcastify sells two products off one account and they are not one
decision:

| Column | Read by | Notes |
| --- | --- | --- |
| `talkgroups.export_broadcastify` | the live **Feed** | the column the two Broadcastify products used to share, so an existing selection keeps meaning what it meant |
| `talkgroups.export_bcfy_calls` | Broadcastify **Calls** | added by `b7c4d2e91a35`, seeded from the column above; held **false** on a talkgroup whose system is not trunked |
| `talkgroups.export_openmhz` | OpenMHz | unchanged |

The *mode* and the *system list* are still shared between the two Broadcastify products —
one account, one `export_broadcastify_talkgroup_mode`, one `export_broadcastify_systems`
(`SETTINGS_SERVICE` in `gate.py` is what maps the feed onto them).  Only the checkbox
split.  Each service has a mode, `export_<service>_talkgroup_mode`:

| Mode | What it publishes |
| --- | --- |
| `all` *(default)* | Everything.  The per-talkgroup checkbox is **not looked at**. |
| `allow` | **Only** the talkgroups whose checkbox is ticked. |
| `deny` | Everything **except** the talkgroups whose checkbox is cleared. |

`allow` and `deny` read the *same* column and differ only in what you intend an untouched,
default-true row to mean — with every box still at its default the two behave identically.
Pick `allow` when you are curating a short list (untick everything, tick the few you want);
pick `deny` when you are publishing the system with a handful of exceptions.  `all` is the
shipped mode and is the one to leave alone unless you are actually curating.

**Copying one flag onto another.**  `POST /api/admin/talkgroups/export-flags/copy`
(*Copy export flags* on the Talkgroups page) writes one of those columns into another over a
selection or over the whole directory, and reports `{considered, updated, unchanged,
skipped_not_trunked}`.  It exists for the day the shared Broadcastify checkbox became two:
"make Calls what Broadcastify already was" is one request rather than several hundred
clicks.  A talkgroup on a system that is not trunked never comes out of it with a ticked
Calls flag — it is held false and counted in `skipped_not_trunked` — and the same holds for
`PATCH /talkgroups/{id}` and `POST /talkgroups/bulk`, so the UI can show that box fixed off
with the reason instead of offering a promise the gate would break.

**Conventional channels.**  A call with no talkgroup is conventional traffic, and the
channel *is* the frequency it was heard on.  Those calls are judged by
`site_frequencies.export_broadcastify` / `export_openmhz` — the same checkboxes, on the
Sites page's frequency table, under the same mode.  A frequency this deployment has never
catalogued fails closed under `allow`/`deny`: we will not publish a channel we cannot name.
(The ledger separately skips every call with no talkgroup — neither service has anywhere to
put one — so today the frequency checkboxes matter only if that changes.)

**Patched calls** are exportable if the *primary* talkgroup passes; the patch members ride
along in the request as they always did.

**Unit-to-unit calls are not conventional traffic**, and the distinction matters here more
than anywhere else.  A P25 private call is one radio talking to one radio; it has no
talkgroup *by construction* (`calls.kind='unit'`, docs/RECORDERS.md), and the frequency it
was granted is a traffic channel the site hands to whatever it grants next.  So:

* **Broadcastify Calls and OpenMHz never publish one.**  Their archives are keyed on a
  talkgroup — `tg` and `talkgroup_num` — and there is no honest value to send; filing it
  under the frequency instead would put one radio's private conversation on a channel that
  means nothing and belongs to nobody.  Discovery filters them out, and a unit call that
  reaches the gate any other way is refused with the distinct reason **`unit_call`**.  There
  is no setting that changes this.
* **The Feed may carry them, and by default does.**  The stream is audio with no identity
  attached to it — no talkgroup, no radio id, nothing a listener could file — so a private
  call on it is one more voice, and leaving it out would silently drop traffic an operator
  relaying their own system can hear.  `broadcastify_feed_include_unit_calls` (**on**, Admin
  → Export) turns it off for anyone who would rather private conversations did not go out
  over a public feed; off, they are skipped with the same `unit_call` reason.

The per-talkgroup and per-frequency checkboxes do not apply to a unit call at all: it has no
talkgroup to tick, and judging it by whoever else was granted its traffic channel is not a
selection anybody made.  That one setting is its whole switch.

Three more filters sit beside the selection, all runtime settings:

* `export_include_encrypted` (**off**) — an encrypted call is a minute of digital noise.
  Off, it gets a `skipped` ledger row saying `encrypted`, which is a record that we saw it
  and decided against it.
* `export_min_duration_s` (0.5) — squelch tails and dead keys.  Unlike encryption this is a
  *discovery* filter: no ledger row is written at all, so moving the floor later is not
  fought by rows written under the old one.
* `export_broadcastify_systems` / `export_openmhz_systems` — comma-separated **keyup**
  `systems.id` values; empty means every system.  Not the remote service's system id.

**Calls nobody spoke on** are refused by `skip_reason_for` itself rather than by a setting
here, with the reason `no speech (0.04 s above -40 dB)` — the constant is
`ledger.NO_SPEECH_REASON` and the measurement in the parentheses is `audio_meta.speech_s`
against `audio_speech_floor_db`.  The audio pass decides it (ARCHITECTURE §5.5,
`audio_meta.content`): a mic keyed and released is a real transmission the archive keeps and
a worthless upload — one measured key-up, recorded twice, is 0.648 s of dead air and one click,
and both copies were queued to both services.  Like `encrypted` it writes a `skipped` row, so
the ledger says we saw it.  Only `content = "none"` and `"static"` refuse (`app.audio.NO_CONTENT`, with
`ledger.STATIC_REASON` naming the second): a **page** is `"page"` however
little was said over it, so a fire dispatch tone-out with no words behind it goes out exactly
as it always has.  A call whose audio job has not landed yet carries no verdict and
is exported exactly as before.

Two paths deliberately do **not** apply it.  The live **Broadcastify Feed** below passes
`no_speech=False`: a feed is the channel as it sounded, key-ups included.  And the
**recorder-direct** Broadcastify upload (trunkd uploading at call end, "Recorder-direct
upload") never reaches the ledger's checks at all — the recorder decides before keyup has
seen the audio, so a no-speech call still goes out that way.

## Broadcastify Feed — the live audio stream

`backend/app/exporters/broadcastify_feed.py`. This is the *other* Broadcastify product: an
Icecast **source** connection that plays the archive's live calls, in order, as one
continuous MP3 stream. It is what a listener hears on the feed's page, and it has nothing
to do with `call_exports`, retries or per-call outcomes — a stream either is connected or
is not.

### How it maps to Broadcastify's own page

The feed's management page has a panel headed *Feed Technical Details (Advanced)*: "These
feed technical details are provided for users who want to configure their own broadcasting
clients. If you are required to provide a username for your broadcast client, use:
`source`." Keyup is one of those clients, and its five settings are that panel, field for
field:

| Broadcastify's field | Setting | Default |
| --- | --- | --- |
| Server | `broadcastify_feed_server` | *(blank)* |
| Port | `broadcastify_feed_port` | `80` |
| Mount | `broadcastify_feed_mount` | *(blank)*, a leading `/` is supplied if you leave it out |
| Password | `broadcastify_feed_password` | *(blank)*, `secret` kind — stored, never read back |
| Username | `broadcastify_feed_username` | `source` |

…plus the knobs the panel does not mention:

| Setting | Default | What it does |
| --- | --- | --- |
| `export_broadcastify_feed_enabled` | **false** | the switch. It stays off — see below |
| `broadcastify_feed_bitrate_kbps` | `16` | CBR mono MP3. Broadcastify asks for 16–32 kbit/s on a voice feed; it must match how the mount was provisioned |
| `broadcastify_feed_sample_rate` | `22050` | mono, same rule |
| `broadcastify_feed_max_backlog_s` | `120` | how much un-played audio may queue before the **oldest** calls are dropped |
| `broadcastify_feed_silence` | `true` | fill the gaps between calls with encoded silence |

### The protocol

One socket, opened with the classic Icecast source verb — `SOURCE /mount HTTP/1.0` with
`Authorization: Basic source:<password>`, `Content-Type: audio/mpeg`, `ice-name` and
`ice-public: 0` — and held open for as long as the switch is on. Icecast answers
`HTTP/1.0 200 OK` (a Shoutcast front end answers `OK2`); anything else is an error and the
source reconnects with a 5 s → 60 s backoff. A refused mount is nearly always the same
thing: **another broadcasting client already holds it**, because a mount takes one source
at a time.

### Pacing and silence

The bytes are written on a clock, not as fast as they encode, and the unit is one MPEG
frame (`app/exporters/mp3.py` reads the frame headers; at 22.05 kHz that is 576 samples,
26.1 ms). The sender owes the socket one frame every 26.1 ms and sends exactly that:

* while a call is playing, the frames come from that call;
* between calls they come from a second of encoded silence, generated once per connection
  and cycled — because **an Icecast mount drops a source that stops writing**, and on a
  quiet system the gaps are most of the day;
* a stall longer than five seconds (a slow ffmpeg, a paused process) resynchronizes the
  clock rather than repaying the debt as a burst, which a listener hears as a skip and
  Icecast may refuse outright.

Encoding happens on a **second thread**, so an ffmpeg run never delays a frame. Each
accepted call is re-encoded to the mount's exact format from the *processed* copy when
there is one (§5.5) and from the original otherwise — the same rule the Calls uploader
uses, and for the same reason.

### Queue policy — the feed stays near live

If calls arrive faster than real time, the queue is trimmed from the **oldest** end back to
`broadcastify_feed_max_backlog_s` (120 s). The newest audio always goes out and what is
dropped is counted (`dropped` in the status block), because a feed that plays a
five-minute backlog is not a live feed. The only exception is that the *newest* call is
never dropped, so a bound shorter than one transmission still plays the current one. A call
already older than the bound when the loader sees it is not even encoded.

### The guards and the selection are Broadcastify's own

Unchanged from the two rules at the top of this document: **live band only**, and **only
after this service's own watermark** (`export_broadcastify_feed_enabled_at`, stamped when
the feed's switch goes on and cleared when it goes off). On top of them the feed obeys the
**Broadcastify** selection — `export_broadcastify_talkgroup_mode`, the per-talkgroup and
per-frequency `export_broadcastify` checkboxes, `export_broadcastify_systems`,
`export_include_encrypted` and `export_min_duration_s`. One set of checkboxes governs both
Broadcastify products: excluding a talkgroup from the Calls uploads excludes it from the
feed.

The one place the two part company is **unit-to-unit calls**: Calls never publishes one and
the feed does unless `broadcastify_feed_include_unit_calls` is turned off (see *Which
talkgroups* above). Nothing else about the selection differs.

### Status and testing

`GET /api/admin/exports` gains a `broadcastify_feed` block: `enabled`, `configured`,
`connected`, `since`, `enabled_at`, the Server/Port/Mount/Username as configured (never the
password), `bitrate_kbps`, `sample_rate`, `max_backlog_s`, `silence`, `bytes_sent`,
`calls_played`, `dropped`, `queued_s`, `last_error`/`last_error_at`. The worker process
holds the socket and the API process answers the read, so the live numbers travel through a
`settings` row (`broadcastify_feed_status`, not an editable setting); one that has not been
written for 90 s reads as **disconnected**.

`POST /api/admin/exports/broadcastify-feed/test` (admin) opens the mount, writes about two
seconds of **silence** and closes it. Icecast has no dry run, so this is the only honest
check — and note that it does briefly occupy the mount, so it fails while another client
holds it.

### Why it stays off

The operator already streams this feed from another broadcasting client. Two sources
cannot hold one Icecast mount: turning this on while that one runs simply gets refused, and
stopping that one with both configured leaves them racing for the mount. The cutover is a
deliberate, ordered thing:

Settings are runtime, so this is one `PUT` (an admin token, or the same fields in Admin →
**Exports** → *Broadcastify Feed (live audio stream)*). The values are the ones on the
feed's *Feed Technical Details (Advanced)* panel. Keep them wherever you keep your other
secrets, not in the repository:

```sh
curl -sS -X PUT https://archive.example.org/api/admin/settings \
  -H "Authorization: Bearer $KEYUP_ADMIN_TOKEN" -H 'Content-Type: application/json' -d '{
    "broadcastify_feed_server":   "audio9.broadcastify.com",
    "broadcastify_feed_port":     "80",
    "broadcastify_feed_mount":    "/yourmount",
    "broadcastify_feed_password": "<the mount password>",
    "broadcastify_feed_username": "source",
    "broadcastify_feed_bitrate_kbps": "16",
    "broadcastify_feed_sample_rate":  "22050",
    "export_broadcastify_feed_enabled": "false"
  }'
```

The last line is the point: **configure it, leave it off.** `broadcastify_feed_password`
has kind `secret` — it is stored and never read back, and sending the field blank later
keeps the password already there.

When the day comes: stop the other broadcasting client, `POST
/api/admin/exports/broadcastify-feed/test` (or press **Test mount**) and confirm the mount
accepts a source, then set `export_broadcastify_feed_enabled` to `true` and watch
`connected`, `calls_played` and `dropped` on `GET /api/admin/exports`. To stop: set it back
to `false` — the mount is closed within one worker cycle and the watermark is cleared, so
turning it on again later starts from live rather than replaying the gap.

## Broadcastify Calls — the five-step workflow

Their documentation states the upload as five steps, and keyup does them in that order.
Three of the five happen outside the uploader, which is why they are worth naming here.

### 1. Verify time synchronization on the host

> "NTP accuracy is required; calls with bad timestamps are rejected or deduplicated
> incorrectly."

This is not boilerplate. `ts` — the call's start in whole epoch seconds — is part of the
tuple Broadcastify deduplicates on (**system, talkgroup, timestamp**), and the archive
keeps whatever timestamp it was told. A host whose clock has drifted publishes every call
at the wrong moment, interleaves wrongly with the other uploaders on the same system, and
stops getting `SKIPPED---ALREADY-RECEIVED-THIS-CALL` for genuine duplicates. There is no
repairing it afterwards from this end.

So `backend/app/exporters/clock.py` measures the offset, two ways:

| Source | How | Precision |
| --- | --- | --- |
| `ntp` | one SNTP datagram to `export_ntp_server` (`EXPORT_NTP_SERVER`; point it at an NTP server you trust), stdlib socket, 2 s timeout | sub-millisecond |
| `http` | the `Date` header of a response we were already making — the credential test, or a stage-1 registration sampled at most once a minute | one second |

NTP is tried first and the `Date` header is the fallback for when the NTP server is blank
or unreachable. `clock_offset_s` is **host minus reference** — positive means this host is
ahead — and appears with `clock_ok` and `clock_source` in the Broadcastify block of
`GET /api/admin/exports` and in the test result.

| `\|offset\|` | What happens |
| --- | --- |
| ≤ 2 s | `clock_ok: true` |
| 2–5 s | `clock_ok: false`, shown on the card; uploads continue |
| > 5 s | a WARNING in the log and **Calls uploads pause** until it recovers |
| unmeasured | `clock_ok: null`; **nothing is paused** |

The pause is deliberately narrow. It stops Broadcastify **Calls** only — not the live Feed,
which carries no timestamps, and not OpenMHz, whose timestamps are its own problem. It does
not clear the enable watermark, so recovery resumes from where it paused rather than
skipping the outage. And an offset that could not be *measured* never pauses anything:
"we could not reach the NTP server" is not the same claim as "the clock is wrong", and a
missing NTP server must never be able to stop uploading. The worker re-measures every ten
minutes; pressing **Test credentials** measures afresh.

### 2. Record the call and capture its metadata

Ingest, long before an exporter sees the row. `app/exporters/base.py:facts_for` flattens a
`calls` row into the talkgroup, unit, frequency, start and duration step 4 asks for.

### 3. Encode the audio

> "M4A / AAC is preferred. Encode at approximately 32 kb/s with an 18 kHz cutoff using
> libfdk_aac or equivalent. MP3 is also accepted: 32 kb/s mono 22050 Hz CBR, or 16 kb/s
> mono 22050 Hz CBR."

`broadcastify_encoding` picks which, and the same choice drives the encode, the `enc` form
field and the upload's `Content-Type` together, so the three cannot disagree:

| `broadcastify_encoding` | ffmpeg | `enc` | PUT `Content-Type` |
| --- | --- | --- | --- |
| `m4a` (default) | `-c:a libfdk_aac` (or `aac`) `-b:a 32k -cutoff 18000 -ac 1` | `m4a` | `audio/aac` |
| `mp3` | `-c:a libmp3lame -b:a {32\|16}k -ac 1 -ar 22050` | `mp3` | `audio/mpeg` |

libfdk_aac is named by the spec but is GPL-incompatible and so is absent from almost every
distribution ffmpeg. Keyup probes `ffmpeg -encoders` **once per process**, caches the
answer, and falls back to the built-in `aac` encoder — the "or equivalent" the spec allows
for. `broadcastify_mp3_bitrate_kbps` chooses between the two MP3 bitrates the spec lists
and is ignored entirely on the AAC path.

### 4. Register the call

An RFC 1867 `multipart/form-data` POST of **metadata only, no audio**:

| Field | Type | Value |
| --- | --- | --- |
| `apiKey` | string | the environment's key (see below) |
| `systemId` | int | the environment's system id |
| `callDuration` | float | seconds |
| `ts` | int | unix timestamp of call **start** |
| `tg` | string | trunked: the decimal talkgroup. **Patched: the patch id first, then every talkgroup in the patch, comma-delimited.** Conventional: the predefined Frequency Slot ID (`broadcastify_slot`) |
| `src` | int | source unit id, decimal; `0` when the recorder never resolved one |
| `freq` | float | **MHz** — Hz is OpenMHz's spelling of the same-named field |
| `enc` | string | optional; `m4a` default, or `mp3` |

The patch rule has a trap on *keyup's* side. A patched call is filed under its **first
member** talkgroup (`app/ingest/parser.py`: "the primary talkgroup of a patch is its first
member"), not under the patch id, so the call's own talkgroup is a member and the patch id
comes separately from `patches.patch_id`. Patch 13012 over 13013 and 13014, recorded on
13013, must be sent as `13012,13013,13014` — leading with the call's own talkgroup would
put a member where the patch id is documented to go and file the call under the wrong
supergroup.

The reply is **one line of plain text**:

| Reply | Meaning | Keyup does |
| --- | --- | --- |
| `0 <upload URL>` | success | step 5, to that URL |
| `1 SKIPPED---ALREADY-RECEIVED-THIS-CALL` | duplicate (same system, talkgroup, timestamp) | ledger row `skipped`; no upload, no retry |
| `100 NO-API-KEY-SPECIFIED` | missing/invalid `apiKey` | parked immediately; retrying without a correction is pointless |
| `1 REJECTED…`, `Invalid-API-Key`, `API-Key-Access-Denied` | not in the published table, but what both reference clients treat as permanent | parked immediately |
| anything else | unknown | one more attempt, with backoff |

### Responses — what is stored, and the duplicate count

Every attempt records the service's own answer on the ledger row: `response_code`, the
integer that leads the line, and `response`, the line itself — redacted of any API key,
capped at 500 characters, and for a success reduced to `0 <last path segment>` because the
rest of the upload URL is a presigning signature that expires in minutes. An attempt that
never reached an answer (a timeout, a refused connection, a transcode failure) leaves both
`NULL`: "we never heard" is not "it said nothing", and a later attempt's line is never
blanked by an earlier failure. OpenMHz shares the columns and stores its HTTP status line;
it has no code of its own, so `response_code` stays `NULL` there.

`GET /api/admin/exports` counts the last 24 hours by **response class** — the line with its
per-call tail removed, so a run of skew rejections is one row with a count rather than one
row per call:

| Class | From |
| --- | --- |
| `0 OK` | `0 <upload URL>` |
| `1 SKIPPED` | `1 SKIPPED---ALREADY-RECEIVED-THIS-CALL` |
| `1 REJECTED-CALL-SKEW-TOO-LONG` | `1 REJECTED-CALL-SKEW-TOO-LONG-<secs>-See:https://www.broadcastify.com/skew` |
| `100 NO-API-KEY-SPECIFIED` | itself |
| *code* + first word | anything else, including OpenMHz's `HTTP 200` |

The same block carries `duplicates` (all time) and `duplicates_24h`. Those two count
`skip_reason` rather than `response`, deliberately: the response columns were added after
several thousand rows had been written, and the duplicate tally is the biggest number on
the page — on a busy system, thousands of duplicates against a few dozen uploads — so counting only rows
that postdate the column would report almost none of them.

Where it shows: the **Calls** card's stat grid ("Duplicates (24 h)") and, under it, a
"Responses (24 h)" line of `class: count` pairs sorted by count; the recent-ledger table
shows a row's raw answer as the tooltip on its status cell. A duplicate also writes an
activity line of its own — `source=export`, kind `export.duplicate`, level `info`,
"broadcastify already has this call" — so the log counts them without a string search.

### Skew — the window a call has to reach the archive in

Broadcastify refuses a call that arrives too long after it was transmitted, with

```
1 REJECTED-CALL-SKEW-TOO-LONG-<secs>-See:https://www.broadcastify.com/skew
```

The line is not in their published response table, so what the number means was measured
here rather than read: over six hours of rejections on a live system (several hundred of them), the reported
skew averaged 20.3 s and matched **(the moment the registration arrives) − (`ts` +
`callDuration`)** to within a second or two, while the same figure counted from `ts` alone
was consistently 3.5 s larger. So they measure from the **end** of the transmission, not
its start, and `ts` is the start — which is the useful way round, because a long call is
not penalized for being long. In the same window every upload that was *accepted* had
reached them within about 14 s of the call ending, and none beyond it did.

**Fifteen seconds is therefore the budget**, end of transmission to registration, and this
deployment was spending twenty. Ingest was not the problem — the call lands here within a
second of the recorder finishing it — the ledger row was reaching its upload 5–19 s later,
for four reasons that compounded:

* one `Worker.cycle()` ran the feed, then Broadcastify (discovery plus a batch of up to
  twenty claims), then OpenMHz (the same again) — so a fresh call waited behind the other
  service's whole batch, and the measured cadence was two or three cycles a *minute*
  against an `export_poll_seconds` of 3 s;
* every claim ran ffmpeg **before** the stage-1 register, so each upload paid an encode up
  front — and 90 % of the answers were `1 SKIPPED---ALREADY-RECEIVED-THIS-CALL`, another
  feeder having published first (which is by design here), so most of that encoding was
  work done to be told it was not wanted;
* `export_min_interval_s` (1 s) then paced each upload on top of that;
* and the queue was worked oldest-first, so a backlog spent its attempts on the calls that
  were already too late while the fresh ones behind them went stale in turn.

Four changes, and none of them touches what is sent:

1. **Register before transcoding.** `BroadcastifyClient.register()` and `put_audio()` are
   now separate, and the worker calls them either side of the encode: metadata goes out
   first, and only a `0 <url>` answer buys an ffmpeg run. A duplicate now costs one small
   POST. `upload()` remains as the composition of the two for every other caller.
2. **A pre-check on the skew window.** Before registering, the worker compares `now` with
   the call's end (`ended_at`, else `started_at` + duration). Already past
   `broadcastify_skew_limit_s` less a 1 s margin for the round trip, and the row is marked
   skipped with `past the Broadcastify skew window (18.2 s after call end)` — no request,
   no encode, and a reason that says how late it was.
3. **Newest-first claims.** `ledger.claim(..., newest_first=True)` for Calls only: the
   calls that can still be published are attempted while they can be, and the ones behind
   them are refused by the cheap pre-check rather than by a round trip. OpenMHz has no
   skew rule and stays oldest-first.
4. **One loop per service.** `Worker.run()` now gives each call-export service its own
   thread, its own session and its own `export_poll_seconds` cadence, with the feed's
   reconcile left in the main loop; uploading is almost entirely waiting on somebody
   else's server, so the two queues have no reason to take turns. `cycle()` is unchanged
   as the sequential single pass the CLI and the tests drive.

**Retries fit the window, or do not happen.** The ledger's backoff starts at 30 s, twice
the window, so a backed-off Broadcastify retry could only ever be skipped as late. With the
window on, a retryable failure on a row nothing has tried yet is retried **once, at once**,
inside the same pass — a fresh claim check, a fresh registration (the upload URL is
single-use), the same encode. If that fails too, or the row arrived with a recorder's
attempt already counted, no further retry is scheduled: past the window the row is skipped
as late with the failure appended (`past the Broadcastify skew window (…); last attempt
failed: …`), and still inside it the row is parked `failed`. A Broadcastify row is never
left `pending` with a retry time it cannot meet. With the window off (`0`) the ordinary
backoff applies.

The setting is **`broadcastify_skew_limit_s`** (Export group, "Broadcastify skew window",
default 15.0 s). `0` turns the pre-check off and lets Broadcastify answer for itself. Rows
skipped by it are counted on the Calls card as **"Too late (24 h)"** beside the duplicate
count (`late_24h` on `GET /api/admin/exports`) — the pair is worth reading together, since
a duplicate is somebody else getting there first and a late skip is this end not getting
there at all.

### 5. PUT the audio

A raw `PUT` to the returned URL: the audio as the body, no multipart wrapper, no field
name, `Content-Type` per the encoding table above, an explicit `Content-Length`, and
neither `Expect: 100-continue` nor chunked transfer. **Upload URLs are single-use and
time-limited** — keyup uses each one once, immediately, and stores only its last path
segment (never the query string, which carries the presigning signature).

### Environments

Broadcastify issues development and production credentials separately, and each endpoint
refuses the other's:

> "Use the development endpoint with development credentials only. Production credentials
> are issued separately and must be used against the production endpoint."

`broadcastify_environment` therefore selects the URL, the API key **and** the system id as
one unit. There is deliberately no fall-through: selecting `development` with no
development key leaves the exporter *unconfigured* rather than quietly sending the
production key to a test host.

| | `production` (default) | `development` |
| --- | --- | --- |
| endpoint | `broadcastify_url` — `https://api.broadcastify.com/call-upload` | `broadcastify_dev_url` |
| key | `broadcastify_api_key` | `broadcastify_dev_api_key` |
| system id | `broadcastify_system_id` | `broadcastify_dev_system_id` |
| status page | `broadcastify.com/calls/status/` | `broadcastify.com/calls-dev/status/` |
| node management | `broadcastify.com/calls/manage/` | — |

> Broadcastify's documentation names both endpoints: production `https://api.broadcastify.com/call-upload`
> and development `https://api.broadcastify.com/call-upload-dev`. Development credentials are
> issued separately and work only against the development endpoint, and production credentials
> only against production; keyup never crosses the two.

The Exports tab shows the environment on the Calls card with a **DEVELOPMENT** badge when
it is not production, and links the status and management pages for whichever environment
is selected.

### Testing the credentials

> "Both the development and production endpoints accept a `test=1` parameter alongside
> `apiKey` and `systemId` to verify that credentials are valid before sending real calls."

`POST /api/admin/exports/broadcastify/test` (the **Test credentials** button) posts exactly
those three fields as `multipart/form-data` to the selected environment's endpoint. That is
a real check of the key — the thing this button could not do before — and it is still not
an upload: the form carries no `ts`, no `tg`, no `freq` and no audio, so there is no call to
register and nothing that could hand back an upload URL. **Nothing is ever PUT from the
test path**, and a test in `tests/test_exporters.py` asserts it over a stub transport.

The reply is read with the step-4 semantics: `100 NO-API-KEY-SPECIFIED` and its siblings
mean the credentials were refused, and anything else is an acknowledgement. **What a
successful reply actually looks like is unknown until it has been seen against a real
account** — the published response table is for *registering a call*, not for the test — so
the raw first line is returned verbatim in `detail` rather than interpreted further, and an
unrecognized line reads as an acknowledgement rather than a failure. No observed response
shape echoes the API key back; `redact()` strips both configured keys from the string
anyway before it leaves the client, because a `detail` ends up in an API response, a log
line and a screenshot.

The result also carries `environment` and the three clock fields from step 1, because "is
this deployment fit to upload?" is the question the button is really being asked.

### Testing what is on the screen, not what is stored

Both test endpoints — `POST /api/admin/exports/{service}/test` and
`POST /api/admin/exports/broadcastify-feed/test` — accept an optional body
`{"overrides": {"<setting_key>": "<value>"}}`, and the Exports page sends the card's
**unsaved** draft with every press.  Pressing Test while filling the form in used to answer
a question nobody asked ("*Problem: not set: BROADCASTIFY_API_KEY*" for a key that was typed
but not yet saved); now it tests the key that is on the screen.

The rules around it are narrow on purpose, because a body that could carry anything would be
a second, unaudited settings API:

* every key must be in `SETTINGS_SPEC`, and must belong to **that service** (the shared
  rate/selection knobs are allowed too); anything else is a 422 naming the key;
* each value is coerced by the same code a save uses, so a draft test and the save after it
  cannot read a value differently, and an invalid enum is refused here as well;
* a blank value means "leave the stored one alone" — which is what a write-only `secret`
  field shows when it has not been touched;
* **nothing is persisted.**  The merged settings object lives for the one request.

The result says so: `used_draft` and `draft_keys` name what was applied, and the page prints
"tested with the unsaved values on this card … nothing was saved" beside the answer.

### Monitoring

- Production status: <https://www.broadcastify.com/calls/status/>
- Development status: <https://www.broadcastify.com/calls-dev/status/>
- Production node management: <https://www.broadcastify.com/calls/manage/>

### Recorder-direct upload

Everything above describes keyup doing the uploading: the recorder pushes the call here,
ingest stores it, the exporter worker picks it up on its next poll and registers it. That
is roughly **three seconds** after the transmission ended, out of a skew window that is
about fifteen (see "Skew" above) — comfortable, but most of the budget is spent on
plumbing rather than on the network.

`broadcastify_upload_at` (Admin → Exports → Calls, *Broadcastify Calls uploaded by*) moves
the upload to the edge instead:

| | `keyup` (default) | `recorder` |
| --- | --- | --- |
| who registers | this host's exporter worker | each trunkd recorder |
| when | ~3 s after the call ends, after ingest | at call end, typically < 1 s |
| encoding | `broadcastify_encoding` (m4a or mp3) | mp3, encoded by the recorder |
| credentials | stay on this host | delivered to recorders over the desired-state poll |
| ledger row | written by the worker | written from the recorder's report on the call push |

**keyup stays the system of record either way.** The recorder reports what Broadcastify
said on the same push that carries the audio, keyup writes that into `call_exports` exactly
as its own worker would have, and the Exports card counts both paths as one set of numbers.
A call no recorder answered for is still discovered and uploaded by the worker, so the
fallback needs no switch: turning the option off, or a recorder that never reports, simply
puts keyup back in the loop for that call.

#### The desired-state block

`GET /api/recorder/desired-state` grows a `recorder.broadcastify` block, **non-null only
when all three of these hold**: `broadcastify_upload_at` is `recorder`, the Calls exporter
is enabled, and the selected environment's key and system id are set. Null means "keyup
uploads them", which is what every recorder is sent by default.

```json
{"enabled": true, "endpoint_url": "https://api.broadcastify.com/call-upload",
 "api_key": "…", "system_id": "6551", "encoding": "mp3",
 "talkgroups": [1401, 1402], "skew_limit_s": 15.0, "claim": true,
 "claim_url": "/api/recorder/broadcastify/claim", "revision": "9f2c41a80b7e"}
```

- `talkgroups` is **resolved here, not shipped as a rule**: `all` mode sends `null` (every
  trunked group call), `allow` and `deny` both send an explicit list of tgids. A recorder
  has no talkgroup table and no idea which systems this archive calls trunked. One
  consequence: in `deny` mode a talkgroup keyup has never catalogued is not on the list, so
  the recorder does not upload it. Erring towards not publishing a channel nobody has
  described is the right direction to err in.
- `revision` is 12 hex characters of a sha256 over the fields above. It is **not**
  `DesiredRecorder.version`, which is deliberately not bumped for this block: the block is
  derived from runtime *settings*, which no per-recorder generation counter tracks. The
  recorder compares the revision and re-reads its credentials when it changes.
- Like `link`, this is one of the two responses that carry a credential in clear, and it
  goes only to the recorder that must hold it. Every operator-facing render of a recorder
  shows `desired_broadcastify` with the key replaced by `api_key_set` and an 8-hex
  `api_key_fingerprint` — the same defence, and the same one function
  (`app.schemas.public_broadcastify`), as `desired_link`.

#### The claim registry, and why it exists

Two recorders covering one site hear **every** call, and keyup itself uploads whatever no
recorder answered for. Broadcastify's own duplicate detection is not enough to lean on: it
keys a call on `(system, tg, ts)` and **`ts` is whole seconds**, so two uploaders whose
idea of when the transmission started differs by one second do not look like a duplicate
to them — they look like two calls, and the archive keeps both. Hence a registry on this
side, asked *before* anything is sent, with a **±2 s tolerance** wide enough to cover that
rounding and narrow enough not to swallow the next grant on a busy dispatch channel.

```
POST /api/recorder/broadcastify/claim      Bearer <upload-scoped token>
{"instance_id": "trunkd-site-a-01", "tgid": 1401, "ts": 1757030462, "duration_s": 4.8}
→ {"granted": true,  "holder": "trunkd-site-a-01", "claim_id": 8814}
→ {"granted": false, "holder": "trunkd-site-a-02", "claim_id": 8813}
```

The grant is taken inside a per-talkgroup `pg_advisory_xact_lock` — two recorders finish
the same call within milliseconds of each other, so "look, then insert" without a lock is
exactly the race this exists to close — and it is per *talkgroup* so a busy channel never
serializes the rest of the system behind it. Asking twice with the same `instance_id` is
granted the same claim, so a retry is safe. keyup's own worker claims here too, as the
literal `keyup`, and skips with `claimed by recorder <holder>` when it loses — no HTTP to
Broadcastify at all. Rows live in `bcfy_claims` and are pruned after two days by the export
vacuum.

Every holder keeps the lock for milliseconds: keyup's worker takes its claim in a short
transaction of its own and commits it *before* the throttle, the registration, the encode
and the PUT, so a recorder claiming on the same talkgroup never waits on keyup's upload.
Every waiter gives up after **1 s** (`claims.LOCK_TIMEOUT_MS`, with a 2 s statement
ceiling): the route then answers **`503`** with `Retry-After: 1`, never `granted: false`.
That is deliberate. A denial makes the recorder drop its upload, and the lock holder may be
claiming a different call on the same talkgroup; a 503 is what trunkd already reads as "keyup
could not answer" (`Claim::Unreachable`, the same as its own 1.5 s timeout), so it uploads
and notes `claim unreachable: HTTP 503` on the outcome — a possible duplicate rather than a
lost call. No trunkd change was needed for this.

A claim does not hold against **keyup** for ever, because keyup's worker is the retry path
for a recorder's failed upload. When `keyup` asks and the holder's claim has outcome
`failed`, or has had no outcome for **30 s** (`claims.STALE_S` — the recorder claimed and
never came back), keyup takes it over: it adds a `keyup` claim beside the recorder's (which
is kept, so the registry still shows who asked first) and from then on `keyup` is the
holder every other claimant is told about. Only keyup takes a released claim over; a second
recorder is still denied.

#### Reporting the outcome

`POST /api/call-upload` takes five optional form fields alongside the audio. They are
bookkeeping and **nothing here can refuse a call**: a missing, misspelled or unparseable
outcome is logged and dropped, and the recording is stored regardless.

| field | meaning |
| --- | --- |
| `broadcastifyOutcome` | `uploaded`, `duplicate`, `failed`, `late`, `claim_denied` or `skipped` |
| `broadcastifyCode` | the integer leading the stage-1 line (`0`, `1`, `100`) |
| `broadcastifyResponse` | the raw stage-1 line, ≤ 500 characters |
| `broadcastifyRemoteId` | the last path segment of the upload URL, on `uploaded` |
| `broadcastifyHolder` | who won the claim, on `claim_denied` |

Each becomes one `call_exports` row for service `broadcastify`, written in the same
transaction as the call, `ON CONFLICT DO NOTHING` on the `(call_id, service)` unique:

| outcome | ledger row |
| --- | --- |
| `uploaded` | `uploaded`, `uploaded_at` now, `remote_id`, `uploaded_by` = the instance |
| `duplicate` | `skipped`, `broadcastify already has this call` — the same constant the worker writes, so the duplicate count sees both paths as one number |
| `late` | `skipped`, `past the Broadcastify skew window (at <instance>)` |
| `claim_denied` | `skipped`, `claimed by recorder <holder>` |
| `failed` | `pending` again with `attempts` 1, `last_error` the response, due **now** — the worker's next pass sends it from keyup's side inside the skew window (taking over the recorder's `failed` claim; a `duplicate` answer then means the recorder's PUT landed after all), or skips it as late on the spot. A permanent rejection parks on that retry. (It used to wait the 30 s backoff, twice the window, so every one of these was skipped as late.) |
| `skipped` | `skipped`, `recorder: <response>` |

Because the row exists, `ledger.discover` never creates a second one — the same
`UNIQUE (call_id, service)` that has always been the deduplication guarantee. The matching
claim is stamped with the call id and the outcome at the same time, so the registry reads
as "who uploaded what" and not only "who asked".

#### What the Exports card shows

`GET /api/admin/exports` gains three fields on the Calls service: `upload_at` (`keyup` or
`recorder`), `by_recorder_24h` (uploads a recorder sent — counted from
`call_exports.uploaded_by`, which is NULL on every row written before this existed, so the
test is positive rather than "not keyup") and `claimed_24h` (calls this host stood down on).
The pair is the answer to "is it actually running": claims rising with no recorder uploads
behind them means the recorders are winning the claim and then failing to upload.

## Configuration in the GUI

Everything is in Admin → **Exports**, in three labelled sections — *Broadcastify Feed (live
audio stream)*, *Broadcastify Calls (per-call upload)* and *OpenMHz* — each with its own
enable switch behind its own confirm dialog, its own credentials, and a note on the
Broadcastify sections that the talkgroup *mode* is shared between the two.

Each card shows its **status** — switched on, configured, queue depth, last upload, errors,
environment, host clock, and for Calls the trunked systems it may publish — unfolded, and
puts its **settings** behind a *Settings* disclosure that is **collapsed by default**.  The
page is read far more often than it is edited.  Which cards an operator leaves open is
remembered per browser in `localStorage`; a card holding an unsaved change is forced open
however it was left, so nothing can be saved that cannot be seen.  The **save bar is
sticky**: it appears at the bottom of the viewport as soon as anything is dirty, says how
many fields changed and names them, and carries *Discard* beside *Save changes*. Plus the rate
knobs
(`export_poll_seconds`, `export_min_interval_s`, `export_timeout_s`, `export_batch_size`,
`export_max_age_minutes`, `export_max_attempts`).  The API keys have kind `secret` — stored,
never read back, and a blank field on save keeps the key you already have.

**Admin → Exports is the only place these are edited.** The Settings tab is the generic
form over `SETTINGS_SPEC`, and it deliberately holds the whole `Export` group back
(`DEDICATED_GROUPS` in `frontend/src/pages/Admin.tsx`) and links here instead: a key/value
list cannot say which service a knob belongs to or put a confirm step in front of the
switch that starts publishing, and rendering the group twice meant two forms with two
drafts over one table, where saving one discarded what had been typed in the other. The
same holds for the `Feed stats` group (the Feed Stats page, docs/BCFY_STATS.md) and the
`Audio` group (Admin → Audio). `ExportsTab.test.tsx` asserts that every `Export` key in the
API's response is rendered here and none of them on the Settings tab, so a knob added to
the spec cannot go missing between the two.

The **environment is the seed and the table wins**, the same precedence the STT backends
use: with no override set the `BROADCASTIFY_*` / `OPENMHZ_*` environment values are what the
exporter reads, a value set in the UI overrides it, and clearing the field deletes the row so
the environment value comes back.  The worker re-reads all of it every cycle, so nothing here
needs a restart.

The tab also shows, per service: whether the credentials are present, when the switch was
turned on (the watermark), the ledger tallies over all time and over the last 24 hours, the
queue depth, the last success and the last error — plus the last 50 ledger rows with a retry
button on the parked ones.  Those ledger figures and rows are cached by the API for up to a
minute (a retry clears the cache, so the re-queued row shows at once); the switches,
credentials and selection beside them are read fresh on every refresh.  The Broadcastify Calls card additionally shows the environment
(with a **DEVELOPMENT** badge when it is not production), the encoding, the host clock
offset and links to the status and management pages for that environment.  **Test
credentials** says exactly what it proved: for Broadcastify Calls it is the documented
`test=1` check of the real key against the selected environment's endpoint, registering no
call and uploading nothing; OpenMHz has no such check — its public `/{shortName}/calls`
read resolves the short name, but its API key is only ever validated by a real upload.

## The ledger

One row per `(call, service)` in `call_exports`, with `UNIQUE (call_id, service)`. That
constraint *is* the deduplication guarantee — a restart, a second worker or a repeated
discovery pass all race on the same insert and only one can win, so a recording cannot be
uploaded twice. Statuses: `pending` → `uploaded`, or `skipped` (encrypted, no talkgroup,
`unit_call`, no audio, no speech, a duplicate the service already has), or `failed` (parked after
`EXPORT_MAX_ATTEMPTS` tries, backing off 30 s / 60 s / 120 s… capped at an hour). A parked
row is never retried automatically.

An outcome is written in the transaction that claimed the ledger row. If *that* write
fails after an upload went out, it is retried in fresh transactions (ledger row first, activity line after), and
if the database refuses all of them the worker remembers the success and records it — does
not send it — the next time the row comes up. If a failure's bookkeeping fails, the row is
backed off in a fresh transaction rather than left due at once. Either way the batch loop
never takes back a row it already handled in the same pass.

Read it with `GET /api/admin/exports` (or the Exports tab), retry a parked row with
`POST /api/admin/exports/{service}/ledger/{id}/retry` — an `uploaded` row is refused, because
that row *is* the promise the call was not published twice — or read the activity log (`source=export`,
kinds `export.uploaded` / `export.failed` / `export.duplicate`). Each row also keeps
`response_code` and `response` — what the service actually answered on the last attempt
(see "Responses" above).

### One upload per transmission

`UNIQUE (call_id, service)` guarantees one upload per *call row*. Two recorders covering
one system produce **two call rows for one transmission**, and reconciliation
([RECONCILIATION.md](RECONCILIATION.md)) rules which of them is the primary — sometimes
after the first has already been published. Copy A lands, discovery gives it a ledger row
and it is uploaded about three seconds later; copy B lands, wins the ruling, and A is
demoted (`A.duplicate_of = B`). B is now a primary with no ledger row, `discover` only
looks for primaries without one, and the same transmission goes out a second time. On this
deployment that was 285 of 5 941 OpenMHz uploads and 76 of 2 287 Broadcastify uploads in a
day.

Two guards close it, and both write the same reason —
`already exported as call <id>` (`app.exporters.ledger.DUPLICATE_UPLOAD_REASON`):

- **Discovery** excludes a primary whose duplicate group is already spoken for: a
  correlated `NOT EXISTS` over `calls.duplicate_of = <this call>` joined to `call_exports`
  for this service in `uploaded`, `pending` or `failed`. It still writes a row — a
  `skipped` one — because a row is what stops the call being re-examined every three
  seconds for ever, and the reason is what tells an operator the gap is deliberate.
  `skipped` siblings do not count: a copy refused for being encrypted or audio-less says
  nothing about the copy that replaced it.
- **The swap** (`app.ingest.reconcile._swap`, and the batch pass when it demotes a row that
  was a primary) moves the ledger with the primary. Per service: an `uploaded` row stays
  where it is — the audio is in their archive and nothing here can un-send it — and the
  winner gets a `skipped` row so discovery never picks it up; a `pending` row is
  **re-pointed** at the winner (`call_id = winner`), because nothing has been sent yet and
  the winner is the better recording; `failed` and `skipped` rows are left alone. A
  recorder-direct Broadcastify upload counts as `uploaded` — its row carries the recorder's
  `uploaded_by`. The loser's rows are read `FOR UPDATE` (with `populate_existing`), so a row
  the worker is uploading at that moment is decided on the status it has *after* the
  worker commits: a row that has just become `uploaded` stays put rather than being moved to
  the winner and offered to the service again.

`GET /api/admin/exports` counts the day's skips of the first kind as
`duplicate_uploads_24h`, shown on **both** service cards as "Duplicates avoided (24 h)".
Keep it apart from `duplicates_24h` next to it: that one is Broadcastify answering
`1 SKIPPED---ALREADY-RECEIVED-THIS-CALL` about a call somebody else sent, this one is us
declining to send a second copy of our own.

## Audio

The **feed** encodes to CBR mono MP3 at the mount's bitrate and sample rate (16 kbit/s /
22.05 kHz by default) with no Xing header and no tags, so one call's frames, the next
call's and the silence between them concatenate into one legal bitstream.

Every **upload** carries the **same `-af` loudness filter the transcription path uses**
(`app/stt/normalize.py`) and leaves the stored recording, its `sha256` and the dedupe
untouched. OpenMHz always gets 16 kHz mono AAC at 32 kbit/s (`-movflags +faststart`);
Broadcastify Calls gets whichever of the two formats its spec allows and
`broadcastify_encoding` selects — see [step 3](#3-encode-the-audio) for the flags.

### Loudness

**The level we store and the level we send are two different numbers, on purpose.**

The stored copy is normalized to `audio_target_lufs` (-16 LUFS). That target is chosen for
*this app's* player, which runs a playback compressor on top of whatever it is handed:
-16 into that compressor is a comfortable listen. Nothing downstream has that compressor.
On OpenMHz and on a Broadcastify feed the file is played as-is, next to audio from feeders
who are not being modest about level, and -16 arrives audibly quiet. Raising
`audio_target_lufs` to fix that is the obvious move and the wrong one: it gives the
player's compressor several more decibels to work with and the in-app listening experience
becomes far too loud.

So there is a second setting. **`export_target_lufs`** (Admin → Exports, *Export loudness*,
default **-11 LUFS**) is the integrated loudness every outbound service is sent at, applied
**at export time, on top of the stored file** — the archive is never touched. `0` disables
it and the exports go out at the stored level, exactly as they did before the setting
existed. (Clearing the field is not the same thing: a blank deletes the override and resets
the setting to -11, as it does for every other key.)

`app/exporters/loudness.py` builds the stage, and it is *appended to* whatever `-af` the
encode already carried (the `app/stt/normalize.py` filter, which runs only on an
unprocessed original) rather than replacing it:

| clip | chain appended |
| --- | --- |
| ≥ 3 s | `loudnorm=I=<target>:TP=-3.0:LRA=11,alimiter=limit=0.707946:level=disabled:attack=5:release=50` |
| < 3 s | `volume=<measured gain>dB,alimiter=limit=0.707946:level=disabled:attack=5:release=50` |

Single-pass `loudnorm`, not two: a measuring pass would double the ffmpeg cost of every
upload to place a 2-10 s clip perhaps a decibel better. Under three seconds `loudnorm` is
not trustworthy at all — EBU R128 integrates over a gated 400 ms window and on a second and
a half of speech it either refuses to move the level or moves it wrongly — so the clip's
mean is measured with one `volumedetect` pass and the difference applied as a `volume=`,
capped by `audio_max_gain_db` — and capped again by the clip's **peak**, which the same
`volumedetect` pass reports for free: a gain computed from a mean says nothing about where
the loudest sample already sits, and a quiet clip with one sharp transient in it would take
the whole lift and hand the limiter ten decibels to clamp off a single peak, which is
audible as a dulled, pumping clip. The gain stops where `peak + gain` reaches the ceiling
instead. This is the same two-branch shape `app/audioproc/chain.py` uses for the stored
copy, for the same reason.

Both branches end on `alimiter=limit=0.707946:level=disabled:attack=5:release=50` so the lift cannot clip
somebody else's decoder; there is no separate limiter setting. Both halves of that stage
were wrong until they were not, in exactly the way the stored chain's limiter was
([ARCHITECTURE §5.5](ARCHITECTURE.md)):

* **`level=disabled`.** `alimiter`'s default `level=1` ("auto level") *raises* its output
  until the loudest sample sits on `limit`, so a stage meant to catch an overshoot instead
  guaranteed one on every single export, whatever the clip came in at.
* **-3 dBFS, not -1.** The number is a sample ceiling on the PCM going into the encoder, and
  what leaves is lossy — AAC for OpenMHz, MP3 for the Feed — so the decoder on the far end
  rebuilds a waveform 1.5-2 dB above it. -3 is shallower than the stored chain's -4 because
  the export is a higher bitrate at a higher sample rate, where the overshoot is smaller,
  and because these files have no player-side make-up gain behind them.

**The tone envelope goes back on after the lift.** A loudness stage measures the *whole*
clip, and a call that is nothing but a tone therefore measures as a quiet call: one measured
call (3.38 s) is a 2.0 s 993 Hz console attention tone at
-18 dBFS and no speech at all, the §5.5 pass took its span down to about -33 dB in the
stored file — and the export stage found a quiet clip, lifted it to -11 LUFS, and handed
OpenMHz and the Feed a tone *louder* than the one we had just turned down. So
`export_filter` re-applies the same envelope from the call's `audio_meta.tone_spans`, one
`volume=-<depth>dB:enable='between(t,a,b)'` per span, **after** the loudness stage and
before the limiter. The depth is the one the pass recorded on the span (`attenuate_db`),
falling back to the setting for that span's pattern — a page 18 dB, a lone held tone the
deeper `audio_tone_attenuate_db` — so an export can never be louder inside a tone span than
the file it was made from. Nothing is re-applied when the pass *removed* the tones instead
(the audio no longer contains them and its timeline was renumbered), when
`audio_tone_handling` is not `attenuate`, or when the call has no stored pass. The spans are
recorded in the original's timebase, so `head_trim_s` comes off them when the processed copy
is the source and not when the raw original is. The in-app player does the same thing in
Web Audio, for the same reason — its Normalize compressor and make-up gain would otherwise
undo the dip on playback (ARCHITECTURE §5.5).

It applies to **all three destinations**: the OpenMHz upload, the Broadcastify Calls
upload, and the Broadcastify Feed — which encodes call by call before the frames are
concatenated, so the gain lands per call and never rides over the silence between them.
The chain that was actually used is written into the `export.uploaded` activity row's
`filter` meta, so an operator can read off what was sent rather than reconstructing it from
the settings of the day.

**Recorder-direct Broadcastify Calls uploads are out of scope.** When
`broadcastify_upload_at` is `recorder` (see
[Recorder-direct upload](#recorder-direct-upload)) the bytes are encoded and PUT by trunkd
from its own MP3 and never pass through this host, so `export_target_lufs` does not reach
them. A deployment that cares about matching levels across the two paths should leave the
upload on `keyup`, or set the recorder's own gain to match.

## Request shapes

**Broadcastify Calls** is steps 4 and 5 above — see
[the five-step workflow](#broadcastify-calls--the-five-step-workflow) for the field table,
the response table and the patch rule. Sources beyond Broadcastify's own documentation, for
the parts it does not spell out: sdrtrunk's `FormField.java` /
`BroadcastifyCallBroadcaster.java`, and trunk-recorder's `broadcastify_uploader.cc`.

**OpenMHz** is one `POST https://api.openmhz.com/{shortName}/upload`, `multipart/form-data`:
the audio as the part named **`call`** with a `.m4a` filename (multer's
`upload.single('call')`; the extension is load-bearing and the part's MIME type is ignored),
plus `api_key`, `talkgroup_num`, `freq` (**Hz**), `start_time`, `stop_time`, `call_length`,
`emergency` (`0`/`1`), `error_count`, `spike_count`, `source_list` (JSON, `pos`/`src`) and —
only when patched — `patch_list`, which is bracketed CSV and *not* JSON. 200 with an empty
body is success. Source: the OpenMHz server itself, `openmhz/trunk-server`
`backend/controllers/uploads.js` and `backend/index.js`, plus trunk-recorder's
`openmhz_uploader.cc` for value formatting.

OpenMHz does **not** deduplicate: a second POST of one call is a second call in their
archive. So an attempt that sent the whole request and heard nothing back — a read timeout,
a connection dropped while waiting for the status line, or a gateway's 502/504/520/524 — is
*ambiguous*, not failed (`AmbiguousExportError`). The upload handler answers `200` before
its S3 put and Mongo save and makes every refusal (`500`) before either, so the
application's own 500s, connect errors and write timeouts stay ordinary retryable failures.
An ambiguous row stays `pending` with `last_error` starting `ambiguous:` and is not due for
60 s (`ledger.VERIFY_DELAY_S`, time for their save to land). Every later attempt on it
first asks the public read API — `GET /{shortName}/calls/newer?time=<start ms − 1>&filter-type=talkgroup&filter-code=<tg>`
(`calls.js`: `time > start`, oldest first) — and matches on talkgroup and start second:
found, the row becomes `uploaded` with their `_id` as `remote_id` and nothing is posted;
not found, it is posted; the lookup failing counts as an attempt and posts nothing. A client
with no lookup parks an ambiguous row as `skipped`, `uncertain: …`, which only an
operator's retry puts back in the queue.

`freq` is MHz for Broadcastify and Hz for OpenMHz. This is the easiest field to cross-wire.

## Turning one on

1. **Stop the other uploader first.** Disable the OpenMHz and/or Broadcastify stream in the
   sdrtrunk playlist and confirm it has stopped posting.
2. Put the credentials in Admin → **Exports** (or in `.env` as the seed, then
   `docker compose up -d`). Broadcastify Calls needs an API key and numeric
   system id from their Calls dashboard — the playlist only carries the live *feed*
   credentials (feed id, mount point, password), which are a different product and are not
   accepted by `call-upload`. Check the **Environment** field is the one those credentials
   were issued for. Press **Test credentials**: for Broadcastify that is a real `test=1`
   check of the key, and it also reports the host clock offset, which must be within a
   couple of seconds before anything will upload.
3. Choose the talkgroup selection if you want one, then flip **one** switch. It takes effect on the exporter's
   next cycle (≤ `EXPORT_POLL_SECONDS`, 15 s) and stamps the watermark then.
4. Watch the status panel on that tab (or `GET /api/admin/exports`) and Admin → Activity (`source=export`). `configured:
   false` means the environment is missing a credential and nothing will be sent.
5. Only once that service looks right, flip the second.

To stop: flip the switch off. In-flight rows finish; nothing new is discovered; the
watermark is cleared, so turning it back on later starts the clock again rather than
backfilling the gap.
