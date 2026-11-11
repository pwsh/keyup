# Site channel audit

*What the site is on the air, what RadioReference says it should be, and what the recorder is configured to hunt —
and every difference between the three.*

The recorder is the only thing that sees the air. A P25 control channel is continuously telling anybody who listens
what the site is: its own frequency (**RFSS/Network Status Broadcast**), the backup control channels it would move to
(**Secondary Control Channel Broadcast**), the neighboring sites and their control channels (**Adjacent Status
Broadcast**), the band plan the channel numbers are expressed in (**IDEN Update**), and — implicitly, one grant at a
time — every traffic channel it uses. [RadioReference](https://www.radioreference.com/) documents what *should* be
there. The recorder's own configuration lists what it was told to look for. Keyup holds all three and reports the
differences.

Five things the report answers:

| finding | means |
|---|---|
| **undocumented** | heard on the air, absent from RadioReference. A new voice channel usually means the system grew; a new control channel means this report is about to matter. |
| **missing** | documented by RadioReference and either never heard or not heard within `CHANNEL_AUDIT_STALE_DAYS`. |
| **control drift** | both sources know the frequency and disagree about whether it is a control channel — RR's primary now carrying voice, an alternate that has become the live control channel. |
| **recorder config** | control channels the site announces that the recorder is not configured to hunt, configured frequencies the site no longer announces, and the list to paste back into the recorder. |
| **adjacent sites** | the neighbors the control channel names, each resolved (or not) to a site Keyup already knows. |

The same recorder push carries two other things that are *not* this report: the message census the site channel
audit ignores, and — on a separate endpoint — the control channel's traffic about individual subscribers. Both are
[RADIO_EVENTS.md](RADIO_EVENTS.md).

Nothing here is stored as a *result*: the report is a comparison, and a stored comparison is wrong the moment either
side moves. The endpoint computes it per request; the worker job recomputes it hourly only to log a summary and to
raise the alerts.

---

## 1. Tables (ARCHITECTURE §3.5)

**recorder_observations** — one posted document, verbatim.
`id, system_id, site_id, recorder, recorder_version, observed_at, window_s, payload jsonb, created_at`.
Keeping the raw document means a mapping bug is repairable by replaying the payloads rather than by asking the
recorder to see the site again. The payload also carries the window's **message census** — `message_mix`,
`unknown_exemplars`, `link_control_mix`, `data_channel`, `lsd_nonzero` — stored verbatim and normalized nowhere, and
read back by `GET /api/sites/{id}/message-mix`: see [RADIO_EVENTS.md](RADIO_EVENTS.md) §5. The raw TSBK stream is
2.3 M messages a day and is deliberately never stored as rows; the histogram is.

**site_frequencies** — one row per **(site, frequency, source)**.
`id, site_id, frequency_hz, role, source, channel_ref, first_seen_at, last_seen_at, observation_count, grant_count,
alerted_at, meta jsonb`. Unique `(site_id, frequency_hz, source)`.

* `role` ∈ `primary_control` | `alternate_control` | `voice` | `unknown`
* `source` ∈ `observed` | `radioreference` | `configured`

The three sources are deliberately *not* merged: a channel RadioReference calls an alternate control channel and the
recorder hears carrying voice is two rows, and the audit is the comparison between them.

**adjacent_sites** — a neighbor one site announces.
`id, site_id, system_id_p25, rfss, site_number, lra, control_frequency_hz, channel_ref, flags jsonb, first_seen_at,
last_seen_at, observation_count, alerted_at`. Unique `(site_id, system_id_p25, rfss, site_number)` — the identity the
*air* gives it, because that is all the announcement carries. The Keyup site it corresponds to — and the RR site that
*names* it — are both resolved at read time.

**rr_sites** — every site RadioReference lists for a system, a reference cache (not "a site the recorder heard").
`id, system_id, rr_site_id, rr_system_id, site_number, rfss, nac, name (siteDescr), county, control_frequency_hz,
frequencies jsonb (the siteFreqs list), synced_at`. Unique `(system_id, rr_site_id)` when RR carries a site id,
falling back to `(system_id, rfss, site_number)` when it does not (two partial indexes). The sync's `getTrsSites`
already returns *all* of a system's sites and only one maps to a Keyup site (the tower the recorder hears); the rest
are the neighbors those towers announce, kept here so the audit can put a name to an `adjacent_sites` row **without a
second RR call**.

**systems** gains `nac`, `rr_system_id`, `rr_synced_at`; **sites** gains `nac`, `rfss`, `lra`, `rr_site_id`,
`rr_synced_at`. All of them are filled from the first observation that carries them and never overwritten — the air
is authoritative about what it hears, not about what an operator has already named.

---

## 2. `POST /api/recorder/observations`

One window of what a recorder saw of one site. `Authorization: Bearer kup_…` on a token carrying the **`upload`**
scope — the same token `/api/call-upload` uses, so a recorder needs one credential and not two.

```json
{"recorder":"trunkd","recorder_version":"0.0.1","observed_at":"2026-08-31T08:00:00Z","window_s":300,
 "system":{"label":"Example System","wacn":781824,"system_id":659,"nac":659},
 "site":{"label":"Site A","rfss":10,"site":1,"lra":10},
 "control":{"frequency_hz":851062500,"channel":"1-4","tsbk_rate":32.1},
 "secondary_control":[{"frequency_hz":859012500,"channel":"1-640","service_class":["backup_control"]}],
 "configured_control":[851062500,851812500],
 "bands":[{"id":1,"base_hz":851012500,"spacing_hz":12500,"offset_hz":-45000000,"bandwidth_hz":12500,"tdma":false}],
 "traffic":[{"frequency_hz":856737500,"channel":"1-458","grants":12,"calls":9,
             "first_seen":"2026-08-31T07:55:00Z","last_seen":"2026-08-31T07:59:00Z"}],
 "adjacent_sites":[{"system_id":660,"rfss":20,"site":20,"lra":20,"control_frequency_hz":851100000,
                    "channel":"1-7","flags":{"conventional":false,"failure":false,"valid":true,
                                             "rfss_connected":true}}]}
```

Only `recorder`, `system` and `observed_at` are required. Normalization:

| document key | role | source |
|---|---|---|
| `control` | `primary_control` | `observed` |
| `secondary_control[]` | `alternate_control` | `observed` |
| `traffic[]` | `voice` | `observed` |
| `configured_control[]` | `primary_control` | `configured` |

`bands` is kept with the document and not normalized — it is how the recorder turns a channel number into a
frequency, and the frequencies are already here.

**Repeated windows accumulate.** `observation_count` and `grant_count` add up; `first_seen_at` takes the earlier and
`last_seen_at` the later value, so a recorder posting its backlog out of order cannot move a first sighting forward.
The system and the site are matched by **name** (`system.label` + `site.label`) through the same `upsert_system` /
`upsert_site` the ingest path uses, so an observation and a call land on the same rows. A document with no
`site.label` is stored and *not* normalized: every audit row hangs off a site, and inventing one from the RFSS and
site numbers would put a site in the directory that no call ever names.

---

## 3. RadioReference

> **Terms of service.** RadioReference data is licensed to you, not to the archive's visitors: its
> [terms of use](https://www.radioreference.com/terms/) prohibit commercial use and redistribution without a licence.
> The sync is read-only, runs on your own Premium account and application key, and caches what it reads only to
> compare it with what the recorder hears. Do not republish the cached tables, and keep `RR_SYNC_HOURS` at its default
> or longer.

RadioReference's SOAP API (`http://api.radioreference.com/soap2/?wsdl&v=latest&s=rpc`) needs a **Premium
subscription** and an **application key**, both the operator's own. They are personal credentials: they are never
shipped with the code and Keyup has none by default, which leaves the RR half of the audit dormant — the tables, the
observations endpoint and the report all work, they simply have no `radioreference` rows to compare against. The API
logs one line at startup saying so.

Three calls per sync, no more — plus at most one `getTag` for the whole run:

* `getTrsDetails(sid, authInfo)` — the system (WACN, system id, NAC);
* `getTrsSites(sid, authInfo)` — every site: `siteId`, `siteNumber`, `siteDescr`, `rfss`, `nac`, `neighbors`, and
  `siteFreqs[{freq (MHz, double), use, lcn, colorCode, chanId}]`, where `use` is `d` (primary control), `a`
  (alternate control) or empty (voice);
* `getTrsTalkgroups(sid, 0, 0, 0, authInfo)` — used to fill in aliases, descriptions and the **service tag** each
  talkgroup carries (`tags[{tagId, tagDescr}]`, e.g. `Fire Dispatch`). **Never an overwrite**: an operator who renamed
  or reclassified a channel outranks RR, and the alias the recorder put on the call is history either way. Setting
  `talkgroups.service_tag` also sets `talkgroups.category` from `SERVICE_TAG_TO_CATEGORY` — but only when *that* is
  blank too, and an RR tag this build does not know is stored verbatim with `category` left alone;
* `getTag(0, authInfo)` — the `tagId` → `tagDescr` table, when a talkgroup arrives with a bare id and no name. It is
  the same forty-odd rows for every system, so it is fetched lazily and at most once per sync run, and the ids are
  never hardcoded: they are RR's to renumber.

`app/rr/client.py` puts those four behind a `RadioReferenceClient` protocol and imports `zeep` on first use, so the
test suite fakes the whole service and an install without the `rr` extra runs everything else unchanged.

**Matching an RR site to a Keyup site** is the one genuinely ambiguous step. In order: `sites.rr_site_id`; then the
site number the air announced against RR's `siteNumber` — and when both sides number their sites and the number is
not one of ours, that is an RR site the recorder has never heard, not an invitation to try a weaker rule; then the
NAC, which only helps where the directory has no site numbers at all (the NAC belongs to the *system*, so every site
shares it); then the description. Every fallback logs which rule matched, and an RR site that matches nothing — or
matches more than one — logs that too and is skipped rather than guessed at. Sites are never *created* from RR: a
site in the directory is one the recorder has actually heard.

A frequency RR stops listing is deleted from that site's `radioreference` rows, or it would sit in the missing list
for ever.

**Every site RR returns is retained** in `rr_sites`, matched to a Keyup site or not — the same `getTrsSites` result,
no extra call. The summary reports it as `rr_sites_cached`. This is what lets the report name the neighbors a control
channel announces: on this network each tower is its own P25 system, so a site's neighbors are the *other* sites RR
lists for the same system, and they are all sitting in the cache.

---

## 4. The report

**`GET /api/directory/sites/{site_id}/channel-audit`** — computed live.

```json
{"site": {"id": 1, "name": "Site A", "rfss": 10, "nac": 659, "rr_site_id": 9001},
 "rr_synced_at": "2026-08-31T04:00:00Z", "last_observed_at": "2026-08-31T08:00:00Z", "stale_days": 14,
 "control": {"observed": 851812500, "rr_primary": [851062500], "rr_alternate": [851812500],
             "observed_alternate": [851062500], "configured": [851812500, 852000000]},
 "undocumented": [{"frequency_hz": 856737500, "role": "voice", "first_seen_at": "…", "last_seen_at": "…",
                   "grant_count": 12}],
 "missing": [{"frequency_hz": 857000000, "rr_role": "voice", "last_seen_at": null}],
 "control_drift": [{"frequency_hz": 851062500, "rr_role": "primary_control",
                    "observed_role": "alternate_control"}],
 "recorder_config": {"announced_not_configured": [851062500], "configured_not_announced": [852000000],
                     "suggested_control_frequencies": [851812500, 851062500]},
 "adjacent_sites": [{"system_id_p25": 660, "rfss": 20, "site_number": 20, "control_frequency_hz": 851100000,
                     "known_in_rr": true, "known_site_id": null,
                     "known_site_name": "Site B tower", "known_county": "Example", "match": "rfss+site_number"}]}
```

* **missing** applies the staleness window `CHANNEL_AUDIT_STALE_DAYS` (default 14): a documented frequency counts as
  missing when it has never been heard *or* was last heard longer ago than that.
* **suggested_control_frequencies** is the live control channel first, then every other frequency either side calls a
  control channel, ascending — the order to paste into the recorder's `control_frequencies`.
* **known_in_rr** on an adjacent site means the neighbor resolves to a site in the `rr_sites` cache — so it carries
  a `known_site_name` (RadioReference's `siteDescr`) and, where RR has one, a `known_county`. The neighbor is matched
  against the cache by, in order, the `(rfss, site_number)` pair, then the exact `control_frequency_hz` (which breaks
  the tie when two towers share a pair), then the P25 `system_id_p25` against RR's system id or NAC; `match` names
  which key resolved it. A neighbor that resolves to nothing (`known_in_rr: false`) is the interesting case: a tower
  RadioReference does not document.
* **known_site_id** is a separate resolution: the Keyup `Site` the recorder *also* hears for that neighbor, or
  `null`. A neighbor can be named from RR (`known_in_rr: true`, `known_site_name` set) while `known_site_id` is still
  `null` — RR documents the tower but Keyup has never recorded it.

**`GET /api/directory/systems/{system_id}/channel-audit`** — one row per site with the same counts, plus totals, so a
system with a dozen towers says which one to open.

Both are ordinary reads: open unless `AUTH_REQUIRE_READ` is on, exactly like `GET /sites`.

**Admin** (all need the `admin` scope):

* `POST /api/admin/radioreference/sync[?force=true]` — sync now. Without `force`, a system synced within
  `RR_SYNC_HOURS` is left alone.
* `PUT /api/admin/systems/{id}/radioreference` `{"rr_id": 1234}` — map a system to RR's `sid`; wins over `RR_SYSTEMS`.
* `PUT /api/admin/sites/{id}/radioreference` `{"rr_id": 9001}` — the escape hatch for a site the matcher gets wrong.

---

## 5. Alerts and the worker job

An alert rule of kind **`channel_audit`** fires when a **new undocumented frequency** or a **new unknown adjacent
site** first appears, on the rule's usual channels (webhook, Discord, ntfy, email). The rate limit is per finding and
permanent: `site_frequencies.alerted_at` / `adjacent_sites.alerted_at` is stamped once, so a frequency heard every
five minutes for a year is one alert. Two deliberate quiets:

* an observed frequency is only "undocumented" once the site *has* RadioReference rows to be absent from — before the
  first sync everything is undocumented and none of it is news;
* nothing is stamped while no `channel_audit` rule is enabled, so enabling one later still reports what is already on
  the books.

The worker runs `run_channel_audit` every `CHANNEL_AUDIT_MINUTES` (default 60): it checks whether the RadioReference
sync is due, recomputes every site's report, raises the new alerts, and logs one line per site —

```
channel audit: Site A — control 851.8125 MHz, 1 undocumented, 2 missing, 2 drift, 1 announced-not-configured, 1 unknown neighbors
```

---

## 6. Environment

| variable | default | meaning |
|---|---|---|
| `RR_APP_KEY` | *(empty)* | RadioReference application key (the operator's own) |
| `RR_USERNAME` | *(empty)* | RadioReference account with a **Premium** subscription |
| `RR_PASSWORD` | *(empty)* | its password |
| `RR_SYSTEMS` | `{}` | JSON map of Keyup system name → RR system id, e.g. `{"Example System": 1234}` |
| `RR_SYNC_HOURS` | `24` | minimum hours between two syncs of one system |
| `CHANNEL_AUDIT_STALE_DAYS` | `14` | a documented channel not heard within this counts as missing |
| `CHANNEL_AUDIT_MINUTES` | `60` | how often the worker recomputes the audit |

All three RR credentials empty (the default) leaves the sync disabled. The SOAP client needs the `rr` extra:
`pip install -e 'backend[rr]'`.

---

## 7. Glossary (RadioReference and P25 terms)

* **WACN** — Wide Area Communications Network id, the 20-bit number that scopes a system id globally.
* **System id / `sysid`** — the system's number inside the WACN. RadioReference's `sid` is something else entirely:
  its own database id for the system, the number in its URL, and what `RR_SYSTEMS` maps to.
* **NAC** — Network Access Code, the 12-bit "color code" on every P25 frame; effectively per system.
* **RFSS** — RF SubSystem: a group of sites under one controller. A site is identified by `(rfss, site number)`.
* **LRA** — Location Registration Area, the roaming zone a site belongs to.
* **Control channel** — the frequency carrying the trunking messages. **Secondary/alternate** control channels are
  the ones the site would fail over to; RadioReference marks them `use='a'` and the primary `use='d'`.
* **LCN** — Logical Channel Number, the site-local index of a frequency.
* **TSBK** — Trunking Signaling Block, the control-channel message unit; its rate is a health signal for the
  decoder.
