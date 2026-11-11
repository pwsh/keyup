# Talkgroup fields

A talkgroup row is mostly numbers the recorder discovered on its own — the tgid, the system,
the call count. Three of its fields are not: **agency**, **category** and **service tag** are
classifications a person makes, and they are what turn a list of channel numbers into
something you can filter, colour and reason about.

This page says where each of the three comes from, what to put in it, and what setting it
actually changes. The same words appear on the **Help** page in the app (`/help`, the
"Talkgroup fields" section), because both render `backend/app/vocabulary.py` rather than
keeping their own copy — the API serves it at `GET /api/directory/vocabulary` and
`backend/tests/test_vocabulary.py` fails if the tables below drift from the code.

The three fields are edited together in the talkgroup dialog on the **Talkgroups** page, on
a batch of channels at once in the bulk-edit drawer, and in bulk from a playlist CSV
(`keyup playlist import`) or a RadioReference sync.

---

## Agencies

### Where they come from

The `agencies` table is usually seeded from an agency list — typically your county dispatch
centre's own, exported from its CAD/incident system. The seed CSV holds four columns (`code`,
`name`, `short_name`, `kind`; see `examples/agencies.example.csv`), and `keyup seed-agencies`
loads it. **Nothing is seeded unless you name the file**, so a fresh install starts with an
empty agency list.

Keep your own files in a directory of your choosing — the *site directory* — and name it in
`.env`. compose mounts it read-only at `/site` in the `migrate` container (with
`KEYUP_SITE_DIR` blank it mounts the repository's `examples/` instead, which only shows the
format):

```bash
# .env
KEYUP_SITE_DIR=/path/to/my/site                    # mounted at /site in the migrate container
KEYUP_SEED_AGENCIES=/site/agencies.csv             # migrate runs `seed-agencies --if-configured`
```

With both set, every `docker compose up` seeds (or re-seeds, below) after the migrations.
To run it once by hand instead, from the same mount:

```bash
docker compose run --rm migrate keyup seed-agencies --csv /site/agencies.csv
# try the fictional example list:  --csv /site/agencies.example.csv  (KEYUP_SITE_DIR blank)
```

The codes are the CAD system's own (an ORI such as `XX0000100`, a department code
such as `EXAMPLEPD`, the dispatch centre's own code), which is the entire point: a call
correlated against a CAD incident matches on the code, so it has to be the same string on
both sides.

Not everything this receiver hears belongs to one of the seeded agencies. A neighbouring
county's sheriff, a railroad, a school district, a federal task force, a hospital's own
security — the CAD system has no code for any of them. Those are added in Keyup, on **Admin → Agencies**,
and are marked `source: local`.

<!-- generated:agency-sources -->
| `source` | Meaning |
| --- | --- |
| `seed` | Loaded from the agency list this deployment seeds from (keyup seed-agencies). Keep the codes the same as an outside system's — a CAD system's agency codes, say — and a record there matches on them. Re-seeding refreshes the name unless somebody here has edited it. |
| `local` | Added in Keyup for an agency the seeded list has no code for — a neighbouring county, a railroad, a school district, a federal unit. It is yours to rename, deactivate and delete, and a re-seed never touches it. |
<!-- /generated:agency-sources -->

### How a re-seed behaves

`keyup seed-agencies` is safe to run at any time. Three rules, implemented in
`backend/app/seed/__init__.py` and covered by `backend/tests/api/test_agencies.py`:

1. **A row the CSV does not name is never touched and never deleted.** That is every `local`
   agency. The seed does no `DELETE` at all — a talkgroup or a radio may be pointing at the
   row, and deleting it would leave a code the API then refuses to save.
2. **A `local` row whose code collides with a CSV code keeps its own values.** If the CAD
   system later issues a code somebody here already used, that wants a person looking at both rows,
   not a silent overwrite. `--force` does *not* override this rule.
3. **A seeded row somebody has renamed here keeps the new name.** Editing an agency through
   the admin API stamps `edited_at`, and the seed skips the name, short name and kind of any
   stamped row. `keyup seed-agencies --force` is the explicit "the seeded list wins" button,
   which is also how an upstream rename lands; it clears the stamp so the row goes back under
   ordinary refresh.

A row nobody has edited is refreshed exactly as it always was, so a fresh database and an
untouched deployment behave as before.

### Adding, retiring and deleting

**Admin → Agencies** lists every agency with its source, whether it is active, and how many
talkgroups and radios point at it. (`?tab=directory` still lands there — the tab was called
"Directory" before agencies got one of their own.) Name, kind and active are edited in the
row itself and save on blur; the pencil opens the fuller form, which is where the short name
lives and where a seeded row's name is overridden.

- **Add** takes a code, a name and a kind. The code is validated against `^[A-Z0-9_-]{2,16}$`
  and must be unique. It is the primary key everything else points at, so it cannot be
  changed afterwards — a wrong one is deleted and retyped while nothing references it yet.
  Pick something that reads as the agency (`NORTH_SO`, `SHORTLINE_RR`) unless the agency has a real
  ORI or CFS code, in which case use that.
- **Edit** changes the name, short name, kind and active flag, on seeded rows as well as
  local ones. A seeded name is often the one CAD shouts in all caps; this is how it is made
  readable. A `local` row's name is editable in the table; a seeded row's is not, because
  `keyup seed-agencies` owns that spelling until an edit stamps `edited_at` — so overriding
  it goes through the pencil, where that consequence is stated.
- **Deactivate** (`active: false`) is the retirement that is not a delete. The agency drops
  out of the pickers, and every call, talkgroup and radio still mapped to it stays exactly as
  it is, so the history keeps reading correctly. This is the right answer for a department
  that merged or disbanded.
- **New agency** on that tab, and a **"New agency…"** entry at the foot of the agency picker
  on the Talkgroups page, open the same add form — an admin who finds a channel belonging to
  an agency Keyup does not know yet can create it without leaving the row being mapped.
- **Delete** is refused (409) for a seeded row — it is the CAD system's to issue and the next seed
  would put it back — and refused for any row something still points at, with the four counts
  in the response so the page can say what to reassign.

### What mapping a talkgroup to an agency does

- **Filters.** `GET /calls?agency=EXAMPLEPD` and the agency filter on the Calls page resolve
  through this map: an agency matches the calls on the talkgroups mapped to it. An agency
  with no talkgroups mapped simply matches nothing.
- **Correlation with CAD.** An agency's talkgroups answer "which channels would a call for
  this police department at 14:32 have been on?", which is what lets an external CAD
  integration line an incident up against the audio.
- **Roster.** Radios, units and personnel carry an `agency_code` too, and a unit callsign is
  unique *per agency* — `E1` means a different engine to each one.
- **Statistics.** Volume on the Stats page can be broken down by agency.

The county's shared dispatch and interop channels can be mapped to the dispatch centre's own
agency row rather than left blank: unlike a NULL, it is a value `?agency=` can actually ask for.
Channels flagged `shared` are additionally reported on *every* agency's
`shared_talkgroup_ids`, so a consumer can discover that set rather than hardcode it.
`keyup mark-shared` flags them — tgids as arguments or one per line in `--file` — and fills a
blank `agency_code` with `--agency CODE`, or with the `shared_agency_code` setting
(`SHARED_AGENCY_CODE`, Admin → Settings → Directory) when `--agency` is not given:

```bash
docker compose run --rm migrate keyup mark-shared --system "Example System" \
  --file /site/shared-talkgroups.txt
```

### Classifying many talkgroups from a rules file

`keyup classify-talkgroups --rules FILE` replays an ordered list of
`alias regex → {category, service_tag, agency_code}` rules over the directory
(`examples/classification.example.csv` is the format, with comments; put your copy in the site
directory and pass it as `/site/…` through `docker compose run --rm migrate keyup …`, as
above). The first matching rule
wins, a talkgroup no rule matches is left alone, and it only fills blanks unless `--overwrite`
is given. Without `--apply` it prints the plan and writes nothing; `--radios` adds a pass that
gives each radio the agency of the talkgroups it is most heard on.

### Agency kinds

Six words, kept small so a CAD system's agency list maps onto them without a translation table. The list is closed.

<!-- generated:agency-kinds -->
| Kind | What it names |
| --- | --- |
| `dispatch` | The dispatch centre itself. Usually the one row of this kind, and it is what the shared and interop channels are mapped to when no single department owns them. |
| `police` | A municipal police department. |
| `sheriff` | A county sheriff's office, including its jail and civil divisions. |
| `university` | A university's own department — campus police, campus fire, campus EMS. |
| `fire` | A fire department or fire district, volunteer or career. |
| `ems` | An ambulance service, whether county-run, hospital-based or private. |
<!-- /generated:agency-kinds -->

---

## Finding a channel: filtering and sorting the list

The **Talkgroups** page is the whole directory, and on a real deployment that is more rows
than a screen. Every column it shows can be both sorted and filtered, and both are done by
the **server**: a page caps at 500 rows, so a table that sorted or narrowed what it had
been handed would be answering "of the channels on this screen, which are not transcribed"
under a header that reads "not transcribed".

* **Sorting.** Click any header. A second click on the same one turns it around; the first
  click on a new one starts at the interesting end (the busiest, the most recent, the ones
  that are on). Channels with nothing in the column sort last either way, and a favourited
  channel still floats to the top of the page it is on.
* **The filter row** sits under the headers. Category, service tag and agency are pickers
  over the same vocabularies the edit dialog uses; **Record**, **Transcribe**, **Shared**
  and the three export flags are three-valued — *any*, *on*, *off* — because the audit an
  operator actually runs is "what is **not** going to OpenMHz", which a checkbox cannot
  ask. The system picker and the search box are in the toolbar, and "Show hidden" still
  decides whether hidden channels are in the answer at all.
* **Filters are the URL.** Everything in force is in the query string under the API's own
  parameter names (`?category=fire&export_openmhz=false&sort=-calls_24h`), so a filtered
  view is a link you can paste to someone else and a view that survives a reload and the
  back button.
* **Chips above the table** list every filter in force with an ✕ on each, and a **Clear
  all** that leaves the sort alone. A filter that arrived in a link is offered back as a
  chip even when its column is not on screen — a guest cannot see the Transcribe column,
  but can still take a Transcribe filter off.
* **A guest gets the same filters and sorts** for the columns they can see. The columns
  that exist only to change something — the tick boxes, the swatch, the policy switches —
  are not shown to them at all, so neither are their filters.
* Counts follow the filter: the "Showing 1–40 of 40" line under a filtered table describes
  the filtered directory, not the whole one.

The same question can be asked of the API directly — `GET /api/talkgroups` takes every one
of these as a query parameter, documented in the OpenAPI schema at `/api/docs`.

---

## Categories

`category` is **this archive's own** coarse bucket for what kind of channel something is. It
is not RadioReference's vocabulary and it is not the recorder's — it is the twenty values the
UI knows how to draw, filter and count.

A category colours the channel's icon on Calls and Live, gives the Calls filter rail and its
facet counts something to group by, is the `category` an alert rule matches and a webhook
payload carries, and is one of the columns the statistics page breaks volume down by. It does
**not** affect recording, transcription or retention — those are the channel's own settings.

There are eight colour slots and twenty categories, so related kinds share a colour
(corrections and security sit with law; rail and aviation with transit) and the icon's glyph
says which member of the family it is.

A channel with no category set is drawn and counted as `other`. A large `other` count on the
Talkgroups page is a to-do list, not a category.

<!-- generated:categories -->
| Category | Name | What belongs in it | RadioReference tags that land here |
| --- | --- | --- | --- |
| `law` | Law enforcement | Police, sheriff and campus-police traffic — dispatch, car-to-car and tactical channels alike. The busiest bucket in this archive and the one the Calls filter is reached for most. | `Law Dispatch`, `Law Tac`, `Law Talk` |
| `fire` | Fire | Fire-department channels: the dispatch channel an alarm goes out on and the fireground and talk channels the crews work on afterwards. | `Fire Dispatch`, `Fire-Tac`, `Fire-Talk` |
| `ems` | EMS | Ambulance and paramedic traffic, and the hospital channels the crews reach the receiving end on — it is the same incident from the other side of the radio. | `EMS Dispatch`, `EMS-Tac`, `EMS-Talk`, `Hospital` |
| `multi` | Multi-service | Channels several services share as a matter of course: a combined dispatch channel, a joint tac channel, an EOC channel that is by definition everybody at once. Not the same as interop, which is for the channels set aside for it. | `Emergency Ops`, `Multi-Dispatch`, `Multi-Tac`, `Multi-Talk` |
| `interop` | Interoperability | The mutual-aid and interoperability channels that exist so agencies who do not normally share a system can talk on a bad day — the national and statewide calling channels and the county's own shared talkgroups. | `Interop` |
| `pw` | Public works | Public works: streets, sanitation, parks, snow and the county's own maintenance crews. Quiet most of the year and the busiest channel on the system in a storm. | `Public Works` |
| `utility` | Utilities | Water, sewer, gas and electric utilities — the crews who turn up when a line is down. Often a separate system this archive only hears the conventional half of. | `Utilities` |
| `transit` | Transit | Buses and the paratransit fleets, plus the road-and-highway operations traffic that is not public works. Rail and aviation get their own buckets. | `Transportation` |
| `rail` | Rail | Railroad road, yard and dispatcher channels, including the freight lines that cross the county. | `Railroad` |
| `aviation` | Aviation | Airport ground, tower and air-ambulance traffic, and the aircraft channels the county's own air support uses. | `Aircraft` |
| `schools` | Schools | School districts and universities: buses, buildings-and-grounds, athletics and the district's own security where it is not sworn law enforcement. | `Schools` |
| `corrections` | Corrections | Jail and prison operations, transport and court security. Coloured with law because it is the same family of traffic, and kept separate because the volume and the retention question are not the same. | `Corrections` |
| `security` | Security | Private and institutional security that is not a sworn agency — hospital security, mall and campus patrols, event staff. | `Security` |
| `federal` | Federal | Federal civilian agencies: the marshals, the alphabet agencies, the VA and the federal side of a joint operation. | `Federal` |
| `military` | Military | Military channels, including the National Guard units that appear on a state system during a deployment or a disaster. | `Military` |
| `media` | Media | Broadcast and press channels — news crews, remote trucks and the outlets' own dispatch. | `Media` |
| `business` | Business | Ordinary commercial licensees: taxi and tow companies, contractors, delivery fleets, the businesses that share a repeater. | `Business` |
| `ham` | Amateur radio | Amateur-radio channels, repeaters and the ARES/RACES nets that stand up during an event. | `Ham` |
| `data` | Data | Channels that carry data rather than voice — telemetry, paging, MDT and control traffic. Almost always worth turning transcription off on. | `Data` |
| `other` | Other | The bucket for a channel that belongs in none of the above, and where a channel nobody has classified ends up. A large `other` count on the Talkgroups page is a to-do list, not a category. | `Deprecated`, `Other` |
<!-- /generated:categories -->

---

## Service tags

`service_tag` is **RadioReference's** vocabulary, stored verbatim in RR's own spelling —
`Fire Dispatch`, `Law Tac`, `Public Works` — rather than slugged. It is somebody else's
classification, and keeping RR's own words means a re-sync, an export or a support question
can be matched back to the RR database page without a translation table. A tag is written by
the RadioReference sync, by a playlist CSV import, or by hand in the talkgroup dialog; input
is matched case-insensitively, so `fire-tac` lands on `Fire-Tac`.

RR splits a service by **role** — dispatch, tac, talk — which is a distinction worth keeping
(`Fire-Tac` is a different channel from `Fire Dispatch`) but not one worth colouring
differently. So the service tag is the fine classification and `category` is the coarse one
the UI uses, and every tag maps to exactly one category:

- all three fire roles collapse to `fire`, all three law roles to `law`, all three EMS roles
  to `ems`;
- `Hospital` joins `ems`, because it is the same traffic from the receiving end;
- `Emergency Ops` joins `multi`, because an EOC channel is several services at once by
  definition;
- `Deprecated` is RR's "this used to be something", and there is nothing to say about it
  beyond `other`.

Setting a service tag on a channel that has no category yet fills the category in from this
mapping; it never overwrites a category somebody has already chosen.

<!-- generated:service-tags -->
| RadioReference tag | What RR means by it | Category it maps to |
| --- | --- | --- |
| `Aircraft` | Air-to-ground and airborne traffic — air ambulance, law-enforcement aircraft, airport operations. | `aviation` |
| `Business` | A commercial licensee's own channel: taxis, tow trucks, contractors, delivery fleets. | `business` |
| `Corrections` | Jail, prison, inmate transport and court security. | `corrections` |
| `Data` | A channel carrying data rather than voice: telemetry, paging, MDT or control traffic. | `data` |
| `Deprecated` | RR's marker for a channel that used to be something and no longer is; kept so a re-sync does not quietly resurrect it. | `other` |
| `Emergency Ops` | Emergency-management and EOC channels — the ones that come alive during a declared incident. | `multi` |
| `EMS Dispatch` | The channel an ambulance is dispatched on. | `ems` |
| `EMS-Tac` | The tactical channel an EMS crew moves to once it is working an incident. | `ems` |
| `EMS-Talk` | EMS administrative and general talk traffic that is neither dispatch nor tactical. | `ems` |
| `Federal` | A federal civilian agency's channel. | `federal` |
| `Fire Dispatch` | The channel a fire alarm goes out on. | `fire` |
| `Fire-Tac` | A fireground or tactical channel the crews work an incident on. | `fire` |
| `Fire-Talk` | Fire administrative and general talk traffic away from an incident. | `fire` |
| `Ham` | Amateur radio: repeaters, nets and the ARES/RACES traffic that stands up in an emergency. | `ham` |
| `Hospital` | Hospital channels, including the med-control traffic between a crew and the receiving facility. | `ems` |
| `Interop` | A mutual-aid or interoperability channel set aside for agencies who do not share a system. | `interop` |
| `Law Dispatch` | The channel a police or sheriff unit is dispatched on. | `law` |
| `Law Tac` | A law-enforcement tactical channel: a pursuit, a perimeter, a warrant service. | `law` |
| `Law Talk` | Law-enforcement car-to-car and administrative traffic that is not dispatch or tactical. | `law` |
| `Media` | Broadcast and press: news crews, remote trucks and the outlets' own dispatch. | `media` |
| `Military` | Military channels, including National Guard units operating on a state system. | `military` |
| `Multi-Dispatch` | One dispatch channel serving more than one service — a combined law/fire/EMS dispatch. | `multi` |
| `Multi-Tac` | A tactical channel more than one service works an incident on together. | `multi` |
| `Multi-Talk` | General talk traffic shared by more than one service. | `multi` |
| `Other` | RR's catch-all for a channel none of its other tags fits. | `other` |
| `Public Works` | Streets, sanitation, parks, snow removal and municipal maintenance crews. | `pw` |
| `Railroad` | Railroad road, yard and dispatcher channels. | `rail` |
| `Schools` | School districts and universities: buses, buildings and grounds, athletics. | `schools` |
| `Security` | Private or institutional security that is not a sworn agency. | `security` |
| `Transportation` | Buses, paratransit and road-and-highway operations. | `transit` |
| `Utilities` | Water, sewer, gas and electric utilities and their field crews. | `utility` |
<!-- /generated:service-tags -->

---

## Record and transcribe

Two switches, one decision in two halves: **Record** keeps the channel's *audio*, **Transcribe**
keeps its *words*. Both default on, both are edited in place on the Talkgroups page and in the
bulk-edit drawer, and neither is visible to a guest.

| Record | Transcribe | An export service selects the channel | What happens |
| --- | --- | --- | --- |
| on | either | either | Today's behaviour: the recording is kept for its retention window. |
| off | on | either | Ingested, transcribed and exported as usual, then the audio file is deleted. The call, its metadata and its transcript stay. |
| off | off | yes | Ingested and exported, then the audio file is deleted. Never transcribed. |
| off | off | no | The recorder is told to stop uploading the channel at all (`skip_talkgroups` in its desired state). A recorder on an older build that uploads it anyway has the metadata stored and the audio deleted on the next pass. |

"An export service selects the channel" means Broadcastify Feed, Broadcastify Calls or OpenMHz
would publish it under its current `talkgroup_mode` — and in `all` mode, the shipped mode, every
channel is selected, so nothing is ever skipped at the recorder until an operator narrows a
selection. See [EXPORTERS.md](EXPORTERS.md).

Turning Record off is never retroactive and never retroactively undone: `calls.audio_policy` is
written at ingest and never re-derived, so the calls already in the archive keep whatever they
were recorded under. The deletion itself is a worker pass every 60 s, which waits for the
transcript to finish, for any queued upload to leave the ledger, and for a two-minute grace
after ingest so export discovery has had its say. A discarded call reads `audio_state:
"discarded"` — beside `purged`, which is retention expiring a recording that *was* kept — and
plays nothing, exactly as an encrypted call does.
