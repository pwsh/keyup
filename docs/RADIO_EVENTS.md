# Radio events

*What the control channel says about subscribers — who registered, who affiliated to which talkgroup, who asked for
a data channel — and what Keyup does with it.*

The call archive answers **what was said**. Radio events answer **who was there**. The two are not the same question,
and until this table existed only the first one could be asked: `radio_affiliations` was written from calls alone, so
a radio that turned on, registered with the site and selected a talkgroup — and then never keyed up — was invisible
to the archive.

The measurement behind this is `docs/NON-VOICE-DATA.md` in the **trunkd** repository, §3–§5 (it is not
part of this repository: [https://github.com/pwsh/trunkd/blob/main/docs/NON-VOICE-DATA.md](https://github.com/pwsh/trunkd/blob/main/docs/NON-VOICE-DATA.md)). Over 150 s of one P25
system's control channel it counted 3 956 TSBKs at 26.4/s, of which the registration/affiliation family is roughly
**600 an hour** — three orders of magnitude below the firehose, and the reason this is a table while the rest is a
histogram (§5 below).

---

## 1. Tables (ARCHITECTURE §3.6)

**radio_events** — one decoded message about one radio.

```
id bigserial, at timestamptz, system_id, site_id, kind radio_event_kind, opcode smallint, mfid smallint,
radio_pk → radios, radio_id bigint, radio_wacn bigint, radio_system int, target_radio_id bigint,
talkgroup_tgid int, talkgroup_pk → talkgroups, result text, frequency_hz bigint, meta jsonb, created_at
```

Indexes: `(at DESC)`, `(radio_pk, at DESC)`, `(talkgroup_pk, at DESC)`, `(site_id, kind, at DESC)`, and the unique
`uq_radio_events_identity` described in §3.

One table with a `kind` and a JSONB `meta` rather than a table per opcode family. Six tables for 15 000 rows a day
would be six migrations and six joins, and the *rare* commands — status, call alert, radio-unit monitor, extended
function — are individually the most interesting messages on a P25 system and collectively too rare to have been
observed even once in 150 s. `kind='other'` catches them with the opcode intact, so the first one to appear lands in
the archive rather than being argued with.

`radio_pk` / `talkgroup_pk` are the directory rows the event was folded into; `radio_id` / `talkgroup_tgid` are the
numbers the air actually carried, kept whether or not the fold resolved them.

**`kind`** (native enum `radio_event_kind`, so a new family is `ALTER TYPE … ADD VALUE`):

| kind | P25 message | opcode |
|---|---|---|
| `unit_registration` | Unit Registration Response | OSP 44 |
| `unit_deregistration` | De-registration Acknowledge | OSP 47 |
| `unit_registration_command` | Unit Registration Command | OSP 45 |
| `group_affiliation` | Group Affiliation Response | OSP 40 |
| `group_affiliation_query` | Group Affiliation Query | OSP 42 |
| `location_registration` | Location Registration Response | OSP 43 |
| `sndcp_grant` | SNDCP Data Channel Grant | OSP 20 |
| `sndcp_page` | SNDCP Data Page Request | OSP 21 |
| `private_call` | Unit-to-Unit Voice Channel Grant | OSP 4 |
| `ack` / `deny` / `queued` | Acknowledge / Deny / Queued Response | OSP 32, 39, 33 |
| `other` | anything else the recorder decoded | kept in `opcode`/`mfid`/`meta` |

`private_call` is the one kind that is about *two* radios: `radio_id` is the caller and
`target_radio_id` the unit being called. It is a fact about the control channel and arrives
whether or not the recorder went on to capture the traffic channel — when it did, the
recording is a separate row in `calls` with `kind='unit'` and the same pair
([RECORDERS.md](RECORDERS.md), "Unit-to-unit calls"). Kept apart on purpose: the grant is
what the site *said*, the call is what was *heard*, and one can happen without the other.

**radios** gains `last_registered_at` and `last_deregistered_at`. `last_seen_at` now means "heard talking **or** seen
on the control channel"; the two new columns are presence alone, and are NULL for a radio only ever heard in a call.

**radio_affiliations** gains `source` (`call` | `affiliation`) and `last_affiliated_at`. This is the change of
meaning the column exists to make explicit: the table used to mean "was heard talking on that talkgroup" and can now
also mean "was present on it". Existing rows were all written from calls, so the migration defaults them to `call`.

---

## 2. `POST /api/recorder/events`

One batch from one recorder watching one site. `Authorization: Bearer kup_…` on a token carrying the **`upload`**
scope — the same token `/api/call-upload` and `/api/recorder/observations` use, checked by the same
`require_recorder_token`, so a recorder needs one credential and not three. Like the other two it is in
`OPEN_MUTATION_PATHS` and `NO_CSRF_PATHS`: it authenticates itself, and a daemon is not a browser.

```jsonc
{
  "recorder": "trunkd", "recorder_version": "0.0.1",
  "system": { "label": "Example System", "wacn": 781824, "system_id": 659, "nac": 659 },
  "site":   { "label": "Site A", "rfss": 10, "site": 1, "lra": 10 },
  "events": [
    { "at": "2026-08-31T12:00:00.123Z", "kind": "unit_registration", "opcode": 44, "mfid": 0,
      "radio_id": 6591234, "radio_wacn": 781824, "radio_system": 660, "target_radio_id": null,
      "talkgroup": 1101, "result": "accept", "frequency_hz": null, "meta": { } }
  ]
}
```

At most **1 000 events** per batch. The system and site are named once for the whole batch, exactly as an observation
names them, so a thousand events cost one system/site resolution. Everything the batch says about the *identity* of
the system and site (WACN, system id, NAC, RFSS, site number, LRA) is written only where the directory has nothing.

**The response is a count, not a verdict:**

```json
{"ok": true, "stored": 998, "duplicates": 1, "rejected": 1,
 "system_id": 1, "site_id": 1, "radios_touched": 63, "affiliations_touched": 12,
 "errors": ["event 44: unusable timestamp 'not-a-time'"]}
```

Two rules follow from the recorder spooling batches and retrying them:

* **One bad event never costs the batch.** `RadioEventIn` is deliberately the most permissive model in the schema —
  every field optional, numbers coerced or dropped — because a recorder posting a thousand events cannot inspect the
  reply per event, and refusing the batch would leave it choosing between dropping 999 good events and retrying the
  same bad batch for ever. Validation happens in the fold, and `rejected` plus up to five `errors` is the report.
* **A retry is free.** See §3.

A `kind` the archive does not know becomes `other`, with the reported spelling kept in `meta.kind_reported` and the
opcode in its own column, so the row can be reclassified later without asking the recorder to hear the site again. A
short alias table maps the obvious spellings (`affiliation` → `group_affiliation`, `data_grant` → `sndcp_grant`).

---

## 3. Idempotency

`uq_radio_events_identity` is a unique index on

```
(COALESCE(site_id,0), at, kind, opcode, COALESCE(radio_id,-1), COALESCE(talkgroup_tgid,-1))
```

and the insert is `ON CONFLICT DO NOTHING`. Three of the six parts are nullable and NULLs do not collide in a unique
index, hence the `COALESCE`s. A recorder that lost its connection mid-batch, spooled it, and resent the whole thing
gets `stored: 0, duplicates: n` and no error.

---

## 4. Folding

Every event with a `radio_id`:

* **resolves a `radios` row** — see "one unit, one row" below — and touches `first_seen_at` / `last_seen_at` with
  `least`/`greatest`, never a plain assignment, because a spooled backlog delivers older events after newer ones and
  presence must not walk backwards;
* **never moves `call_count`.** A registration is not a transmission. The directory's counters mean "was heard", and
  an affiliation storm at shift change must not inflate them. The same holds for the talkgroup an affiliation names,
  which is created if the directory has never seen it and left at `call_count = 0`;
* `unit_registration` and `location_registration` stamp `radios.last_registered_at`; `unit_deregistration` stamps
  `last_deregistered_at`.

An **accepted `group_affiliation`** additionally upserts `radio_affiliations` with `source='affiliation'` and
`last_affiliated_at`. `call_count` and `last_heard_at` are untouched — nobody spoke. `source` is raised to
`affiliation` and never lowered again: it is the stronger fact, and a later call on the same pair does not make it
less true. A **denied** affiliation writes the event and no affiliation row: it is a fact about the site, not about
presence.

### One unit, one row

`radios` is unique on `(system_id, radio_id, wacn, home_system_id)`, with `0` meaning "the recorder printed a bare
id". That key is right only while a recorder always spells a unit the same way, and P25 Link Control opcode 9
("Source ID Extension") ends that: it rides some transmissions and not others, and a control-channel registration
carries the WACN while the affiliation two seconds later does not. Upserting each spelling on its own key would file
one unit as two rows and split its history down the middle.

`app.ingest.service.resolve_radio` is the answer, and it is used by the event fold and by the call-upload path:

1. prefer the **exact** key;
2. a **qualified** sighting with only a bare row present fills that row's `wacn` / `home_system_id` **in place**
   (`fill_radio_home`) — never overwriting a home already recorded, and never when another row already occupies the
   qualified key, because merging two radios is not a decision ingest may take;
3. a **bare** sighting with no bare row joins the one qualified row **if there is exactly one**. Two qualified rows
   for one id means two genuinely different radios roaming from two systems, and choosing between them is not this
   function's business.

`781824.660.6590041` and `781824.661.6590041` therefore stay two rows, as they always have. The file-ingest path keeps
the plain upsert: an sdrtrunk filename carries no extension to disagree with.

### `POST /api/call-upload`: `sourceWacn` and `sourceSystem`

Two new optional form fields carrying LC 9 for the call's source radio. They arrive *beside* a plain `source` — the
recorder knows the unit is `781824.660.6601025` and still prints `source=6601025` — and they turn "radio 6601025" into
"a system-660 radio roaming onto Site A", which is what `radios.wacn` and `radios.home_system_id` have been waiting
for since the schema was written. No new table, no new endpoint. They are also kept verbatim in `calls.raw_meta`,
because the row records what arrived and not only what was normalized out of it.

---

## 5. Analysis endpoints

**`GET /api/radios/{id}`** (radio detail) gains:

```jsonc
{ "presence": { "last_seen_at": "…", "last_registered_at": "…", "last_deregistered_at": null,
                "affiliations": [ { "talkgroup_id": 4, "tgid": 1101, "talkgroup_alias": "LAW 1",
                                    "source": "affiliation", "last_heard_at": null,
                                    "last_affiliated_at": "…", "call_count": 0 } ] },
  "events": [ /* the 50 most recent, newest first */ ], "event_count": 812 }
```

Affiliations are ordered on `greatest(last_affiliated_at, last_heard_at)`, because a row an affiliation event wrote
has no `last_heard_at` at all and would otherwise sort as the oldest thing on the page. The same ordering and the
same two new fields are on `GET /api/radios/{id}/affiliations`.

**`GET /api/radios?activity=all|transmitted|seen`** — the radio directory. Because presence creates a row at
`call_count = 0`, the list now carries radios that registered or affiliated but never keyed up; `activity=seen`
returns only those, `activity=transmitted` only radios with `call_count > 0`, and the default returns both. The
Radios page defaults to `all` (server-sorted and paged) and tags a zero-call radio "seen, no calls".

**`GET /api/radios/{id}/events?from&to&kind&page&page_size`** — the paged timeline. `kind` repeats for OR.

**`GET /api/sites/{id}/radio-events/summary?hours=24`** — counts by kind and by result, the twenty busiest radios,
and `unit_registration` per hour bucket, which is the series that should spike at shift change and which neither
fixture in the source measurement was long enough to contain.

**`GET /api/sites/{id}/message-mix?days=7`** — the control channel's *message census*, per `mfid:opcode`, with
totals, the number of observation windows each appeared in, first and last sighting, and one hex exemplar for a
message the decoder could not name. This one reads no `radio_events` at all: the raw TSBK stream is 2.3 M messages a
day and is deliberately never stored as rows, only counted per window. Five new keys on the observation payload
carry it, stored verbatim in the existing JSONB column and normalized nowhere:

| key | what |
|---|---|
| `message_mix` | `{"0:2": 695, "164:15": 488, …}` — TSBKs by `mfid:opcode` for the window (`mfid/opcode` is accepted too) |
| `unknown_exemplars` | `[{mfid, opcode, count, exemplar}]` — one hex sample of each message the decoder could not name |
| `link_control_mix` | the same census for traffic-channel Link Control, by `mfid:lco` |
| `data_channel` | `{grants, pages, pdus, channels: [Hz]}` — SNDCP activity in the window |
| `lsd_nonzero` | low-speed-data octet pairs that were not all zero (this system's are all zero) |

25 % of every TSBK on the measured control channel is one of two L3Harris vendor messages that never change a byte —
23 400 an hour, semantics unknown to trunkd *and* to sdrtrunk. Storing them as rows would be absurd; one exemplar per
window is enough to notice the day they change, which is exactly what this endpoint makes visible.

**`GET /api/sites/{id}/message-kinds`** — the *memory* under that census, and the reason it exists. The mix above is
a rolling window: it can say a site sends `164:34` twelve thousand times an hour, and it cannot say whether it has
ever sent it before. `site_message_kinds` holds one row per `(site, mfid, opcode)` — running count, first and last
sighting, the newest exemplar and the one it replaced — written in the same transaction as the window that carried
it. A pair the site has never transmitted raises `site.unknown_new` in the activity log and a `new_kind` alert; a
kind counted more than a thousand times whose exemplar bytes change raises `site.unknown_changed` and an
`exemplar_changed` alert, at most one per site and kind per 24 hours. `unknown_only=true` lists only the kinds nobody
has named; `PATCH /api/admin/sites/{id}/message-kinds/{mfid}/{opcode}` with `{known, label}` names one, which is what
stops it being news. Run `keyup sites seed-message-kinds` once after upgrading — it replays the stored windows
silently, so the first real window reports what is genuinely new rather than the ninety-odd kinds the site has always
been sending.

Most kinds are not unknown to anybody: the 64 standard TSBK opcodes have had names since TIA-102.AABC, and
`app/p25/message_names.py` carries them in trunkd's own spelling (plus the L3Harris TSBKs its registry names). A kind
is named from that table as it is first inserted, and `keyup sites name-message-kinds [--toml identifiers.toml]`
names the rows written before the table existed — idempotent, leaving a hand-written label alone, and giving a
registry entry marked *noise* (a CRC false pass) its label while leaving `known` false, because such a row is
explained rather than known.

Both site endpoints also answer on `/api/directory/sites/{id}/…`, unlisted in the OpenAPI document; the canonical
spelling matches `/api/sites/{id}/channel-audit`.

---

## 6. Retention and privacy

**`RADIO_EVENTS_KEEP_DAYS`, default 90.** It is the one retention knob in Keyup that does *not* default to "keep for
ever", and that is deliberate.

These rows are **presence data about identifiable people**. A registration says which radio was on which site at
which minute; an affiliation says which talkgroup its user selected — which is to say which job that person was doing
— and both are recorded whether or not that person ever transmitted. It is the same class of data the archive already
holds per call, but denser, and it covers people the call archive never sees at all. Shift patterns, arrival and
departure times, and mutual-aid movements between systems all fall straight out of it.

So: a shorter window than the audio, on a clock of its own. The nightly retention pass
(`CRON_RETENTION`, `app.retention.purge_radio_events`) deletes `radio_events` older than the window **even when every
call policy keeps for ever** — nothing about a call depends on these rows, so the purge is a single indexed DELETE
and runs first. `0` keeps them for ever, for a deployment that has decided that is appropriate; the report and
`POST /api/admin/retention/run` both count what went as `radio_events_purged`.

What is *not* here, and why: **talker GPS** (L3Harris LC 42/43) is live officer location, the highest-value and
highest-sensitivity item in the source analysis, and it appeared zero times in any capture — the parser should be
built only when a capture actually shows the blocks, and treated as more sensitive than any of this. Nothing
**inbound** is observable at all: every subscriber→FNE message rides the uplink pair the recorder never tunes, so
everything here is the network's downlink response and never a request from the field.

---

## 7. Migration

`c9f1a2b30d44_radio_events` (down revision `b8d4e2f70c15`): the `radio_event_kind` and `radio_affiliation_source`
enums, the `radio_events` table with its five indexes, `radios.last_registered_at` / `last_deregistered_at`, and
`radio_affiliations.source` (server default `'call'`) / `last_affiliated_at`. `downgrade` drops all of it, enums
included.
