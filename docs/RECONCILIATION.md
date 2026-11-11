# Cross-recorder call reconciliation

*Two recorders, one transmission, one row — and the better copy in it.*

Keyup is fed by two recorders covering the same P25 system at the same time: the old sdrtrunk chain (whose backlog is
bulk-uploaded later, and which may keep running for a while) and [trunkd](https://github.com/pwsh/trunkd) (live, near real time). The
same key-up therefore arrives twice — different tuner, different software, slightly different start, duration and
metadata, sometimes **days** apart when the backlog lands after the live copy. Reconciliation recognizes the two as
one transmission, keeps the better copy as the primary row, folds the other's metadata into it, and hides the other
behind `duplicate_of`.

The old dedupe (ARCHITECTURE §5.1 step 4, before this) could not do any of that. It matched on `|Δstart| ≤ 1 s` **and**
`|Δduration| ≤ 10 %`, which two tuners rarely satisfy; it always kept whichever copy arrived first, so a better later
copy lost; and the push path answered `duplicate call rejected` without storing the second copy at all, so the better
copy was not merely demoted, it was thrown away.

Everything below is `backend/app/ingest/reconcile.py`, and every rule has a test in
`backend/tests/test_reconcile.py`.

---

## 1. Matching: a time slice on a channel

On one talkgroup — and on one traffic frequency — two transmissions cannot occupy the same time slice. So the question
"are these the same call?" is **interval overlap**, not start equality.

Two calls of one system are the same transmission when **both** hold:

**(a) the channel.** Their `talkgroup_ids` sets intersect, **or** both carry a `frequency_hz` and it is the same one.
The frequency arm is what catches a trunkd push that arrived without a usable talkgroup: one traffic channel carries one
transmission at a time, whatever it is labeled.

**(b) the time slice**, by any of four rungs.

**1 — interval overlap.** Each interval `[started_at, ended_at]` is widened by `RECONCILE_CLOCK_SKEW_SECONDS` (default
**2.0**) on both sides, and the two must overlap by at least `RECONCILE_MIN_OVERLAP` (default **0.5**) of the *shorter*
recording. The skew allowance is then subtracted back out of the measured overlap before the ratio is taken, so two
calls that merely abut do not collect `2 × skew` of free overlap and score as a match. This is the whole story for a
call of 4 s or more.

**2 — aligned edges.** Both recorders agree, within the allowance, about when the transmission began *and* when it
ended. Rung 1 cannot see a short call, and short calls are most of the archive: an sdrtrunk file's `started_at` is
derived from a stamp written at `call end + 1.7 s` (§5), and 1.7 s of offset on a 2.4 s call — the corpus median is
2.2 s — leaves 28 % overlap, nowhere near the floor. Two copies of one transmission still agree on *both* edges to
within that offset; a following transmission does not.

The allowance here is capped at the shorter recording's own length, because slack longer than the call itself would let
two consecutive key-ups collapse into one: two 1.2 s transmissions 1.3 s apart agree on both edges to within 2 s, and
must not match. The price of the cap is that a very short call carrying the full offset does not reconcile at all —
a **miss**, costing one duplicated row, rather than a **merge** of two real transmissions, which would cost one. That
is the trade the cap is chosen to make.

**3 — short-call.** Both recordings shorter than `2 × RECONCILE_CLOCK_SKEW_SECONDS`, the **same radio** keyed both
(when the archive can resolve one — `Interval.source_radio`), their starts within
`SHORT_CALL_MAX_START_DELTA_S` (**1.0 s**) of each other, and their lengths within
`SHORT_CALL_MAX_DURATION_DELTA_S` (**0.25 s**). This is the rung for a recorder that reports its call start in
**whole seconds**: production calls 2395971 and 2395972 are one 0.648 s key-up on a bus-fleet talkgroup by one radio, heard by both
recorders, stamped 04:59:32 and 04:59:33 — two intervals with a 0.352 s *gap* between them, so rung 1 has no overlap
to measure and rung 2's allowance (capped at 0.648 s) is smaller than the second between them. Both copies were stored
as primaries. (The other half of that fix is the push path's millisecond `startMs` upload field — this rung
is what reconciles the calls already in the archive, and the copies from any recorder that still rounds.)

The start allowance is deliberately **not** the 2 s skew: rounding one instant to whole seconds on two clocks can move
the two answers at most one second apart, so the allowance is the size of the error being corrected and nothing wider.
At 2 s this rung would collapse the two 1.2 s key-ups 1.3 s apart that rung 2's cap exists to keep separate. The radio
guard is what keeps a busy talkgroup honest — two sub-second key-ups a second apart *are* two transmissions when two
different radios made them — and "the archive cannot say who" is never read as evidence either way.

**4 — fragment inside.** A copy no longer than `RECONCILE_FRAGMENT_SECONDS` (default **1.5**) that lies *inside* the
other matches whatever the ratio says. That is the late-granted trunkd fragment: trunkd was handed the grant a moment
before the transmission ended and caught 1.2 s of a call sdrtrunk recorded whole. The ratio is under the floor there
for a reason that is not doubt.

**Above both rungs sits the exact one:** an identical `sha256` is the same call whatever the metadata says. A *missing*
hash is never a match — a metadata-only encrypted call has no bytes to hash, and `sha256 = NULL` renders as `IS NULL`,
which would make the first audio-less row the "original" of every one that followed it.

### The exact rung excludes the caller's own copies

A hash hit is not the answer; the **primary it resolves to** is, because a row that already points at another is
represented by the row it points at. That resolution is where the rung went wrong for months: the exclusion list (the
row being ruled on, plus everything the batch pass has already resolved in this pass) was applied to the row the query
*found* and not to the primary that row resolves to. So a call `A` whose bytes were stored twice — `B.duplicate_of =
A.id`, a recorder retransmitting a file — matched `B`, followed `B` home to `A`, and handed `reconcile_pair` the pair
`(A, A)`. Two identical rows score identically; a tie leaves the *existing* primary in place and demotes the newcomer;
newcomer and existing were the same row. The write was `A.duplicate_of = A.id`, which hides the call from every listing
(they all filter `duplicate_of IS NULL`), takes it out of its conversation, and — if its transcript was still pending —
marks it `skipped` with its jobs cancelled. **18,708 rows on production**, roughly 2,000 live calls a day plus every
2022 backlog recording.

Two things fix it. `_find` now checks the *primary* against the exclusion list as well, and falls through to the
interval rung when it is excluded, exactly as if the hash had hit nothing — the guard the interval rung has always had
on its own candidates. Because more than one row can carry a hash (the file, plus every copy already ruled a duplicate
of it), the rung walks up to `EXACT_HASH_SCAN_LIMIT` of them looking for one that resolves somewhere else, rather than
taking the first by id. And `reconcile_pair` refuses outright to rule a row a duplicate of itself: it logs a warning
and returns a no-op outcome, at the place where the write would have happened.

Rows already written that way are freed by `keyup repair-self-duplicates` (§7).

### And it excludes a hash hit from another day

Identical bytes prove the same *recording*, not the same transmission. A short recording with no voice in it — a
squelch tail, a grant with nothing on it, 0.14–2 s — encodes to **byte-identical** MP3 bytes every time, so one hash
spreads across hundreds of unrelated calls months and talkgroups apart, and the first row carrying it became the
"original" of all of them. Production ruled call 2250773 — a 1.7 s *unit* call on 2026-09-08 10:13 — a duplicate of
call 8371, a 1.7 s *group* call on talkgroup 2 from 2026-08-31, rule `identical sha256`: **927 rows** (924 group, 3
unit) whose primary started more than 300 s away, one hash alone covering 155 of them.

So an exact-hash hit counts only when the primary it resolves to started within `EXACT_HASH_MAX_SKEW_S` (60 s) of the
caller's own start — two recorders' copies of one key-up differ by a second or two of clock skew, and a retransmitted
file carries its original timestamp, so the window is generous for every real case. A hit outside it is skipped exactly
like an excluded one: the scan continues, and the rung falls through to the interval rung, which does compare the
channel and the time slice. Rows already written that way are freed by `keyup repair-far-hash-duplicates` (§7).

### What must *not* match

Two genuinely different back-to-back transmissions on one talkgroup — the case the whole design has to get right. They
abut, so the only overlap they have is the skew allowance, and the allowance is removed before the ratio is computed
(rung 1); their starts are a whole call apart, and on short calls the allowance is capped at the call's own length
(rung 2); they are more than a rounded second apart, or a different radio keyed each (rung 3); neither is a fragment of
the other (rung 4). `test_back_to_back_transmissions_are_not_one_call`,
`test_two_short_key_ups_in_a_row_are_not_collapsed_by_the_allowance` and `TestTheShortCallRung` are the guards.

### The lock

The old dedupe took a transaction-scoped advisory lock on `(tgid, second)`. Interval matching had to give that up: two
copies of one transmission start in *different seconds*, so they would take different locks and neither would see the
other. The key is now the **channel** — every talkgroup on the call, plus the frequency, with no time in it — so two
calls that could possibly overlap always serialize. Unrelated talkgroups still never wait on each other, and the keys
are taken in sorted order, so two workers each holding two keys cannot deadlock. Frequency keys live in a disjoint half
of the key space so a frequency in Hz cannot collide with a tgid.

---

## 2. Choosing the primary

Each copy is scored; higher wins; **a tie leaves the existing primary in place**, which is what makes re-running the
batch pass a no-op. The components, in the order of their weight:

| rank | component | weight | what it says |
|---|---|---|---|
| 1 | **has audio** (`audio_state='present'`) | 10 000 | A recording beats a description of one. Nothing below can outvote it. |
| 2 | **longer duration** | up to 2 000 | More of the transmission. Pairwise: `4000 × (ratio − 1)`, capped at the merge ratio and withdrawn past it (below). |
| 3 | **source quality** | 700 | The receiver that heard it better, by its own decoder's count: `voice_frames − 5 × bad_frames` (`calls.source_quality`). Pairwise and a flag, not a magnitude — see below. |
| 4 | **exact time source** (`upload`/`sidecar`) | 300 | The recorder said when the call started. `file_write` is derived from the sdrtrunk file-close stamp and runs ≈ 2 s late (§5), so it does not count. |
| 5 | **metadata richness** | 32 × 0…8 | Non-null among radio, talker alias, encrypted, emergency, frequency, site, patch/members, `call_sources` rows. |
| 6 | **audio quality** | up to 20 | Bitrate and sample rate, normalized (32 kbps / 22.05 kHz is the sdrtrunk baseline). |
| 7 | **finished transcript** | 8 | There is text on this row already. |
| 8 | **recorder preference** | up to 3 | `RECONCILE_PREFER_SOURCES`, default `["trunkd","trunk_recorder","upload","file"]`. **A tie-break only.** |

The weights are chosen so each rank dominates every rank below it *combined*: preference ≤ 3 < transcript 8 < quality
≤ 20 < one metadata field 32 < time source 300 < source quality 700. Duration is the deliberate exception, because it
is continuous: a 0.2 % difference between two encoders must not outrank an exact timestamp, so only a materially
longer recording (≈ 8 % and up) outweighs the ranks beneath it.

### Source quality

The one component that describes the **audio** rather than the row around it. Two tuners on one transmission can agree
on every other signal and still differ by a hole in the speech — which is the difference a listener notices — so it
outranks them all combined (700 > 587), while staying under duration and audio: a materially longer or an
actually-present recording still wins.

It is a flag, not a scaled magnitude. Frame counts are unbounded and grow with the length of the call, so "twice the
voice frames" usually means a longer copy (which `duration` already scores), not a better one. All that is being
asserted is *this receiver heard it better*, and that is worth one fixed amount.

There is no term at all unless both copies carry a scoreable report, the two disagree, and they do not name the same
`source_instance` — comparing a receiver with itself says nothing about which of two receivers to keep, and an absent
or non-finite report says nothing at all. Copies without it score exactly as they did before.

The same number ranks an *arriving* copy against a stored one on the push path (docs/RECORDERS.md, "Best source"),
from one definition, so the two can never disagree about which receiver was better.

### The merged-calls cap

`RECONCILE_MERGE_RATIO` (default **1.5**). trunkd holds a channel through its hang timer, so two consecutive key-ups on
one talkgroup can come out of it as *one* recording. A copy more than 1.5× the other's duration is therefore evidence
of two transmissions joined, not of one recorded better — the duration credit is capped there and then **withdrawn**
(`notes: ["merged_suspect"]` on the score). A 24 s copy of an 8 s call is not three times the call; it is two calls,
and the 8 s copy keeps the seat.

**Unless the other copy is a fragment.** When the shorter copy is no longer than `RECONCILE_FRAGMENT_SECONDS` it is not
a call in its own right, so being many times longer than it is not suspicious — it is the whole point. The credit
stands, capped at the merge ratio, and the score notes `other_is_fragment`. Without that exception the 8 s sdrtrunk
call would look "merged" beside a 1.2 s trunkd fragment and lose to it, which is precisely backwards.

### The record

Both sides of a pair get `calls.reconciled_at` and `calls.reconcile_reason` (JSONB):

```json
{
  "rule": "audio",
  "winner": "newcomer",
  "winner_id": 8412,
  "match": {"rule": "interval overlap", "key": "talkgroup:1101", "overlap_s": 7.8, "overlap_ratio": 0.97},
  "scores": {
    "8412": {"call_id": 8412, "total": 10352.75, "parts": {"audio": 10000.0, "duration": 0.0, …}, "notes": []},
    "8399": {"call_id": 8399, "total": 483.0,   "parts": {"audio": 0.0, …}, "notes": []}
  },
  "merged_fields": ["encrypted", "raw_meta.algorithm", "raw_meta.keyId", "raw_meta.merged_from"],
  "at": "2026-08-31T14:02:11+00:00"
}
```

`rule` is the highest-ranked component the two copies disagreed on — the reason, in one word.

---

## 3. What happens to the loser

**Nothing is thrown away.** The loser is stored with `duplicate_of` pointing at the winner, and everything it knows
that the winner does not is merged **into the winner, filling only NULL/empty fields** — a merge never overwrites what
the winning recorder itself reported, and never touches a human's edits:

- the scalars: radio, talker alias, encrypted, emergency, frequency, site, channel, decoder, audio type, the sidecar's
  signal/noise/frequency-error readings, the recorder's call number, the recorded alias;
- patch membership: a patch the winner did not see makes its member set the truth about which talkgroups the
  transmission was on, which is what "calls involving talkgroup X" reads;
- `call_sources` rows, **only when the winner has none** — two recorders' source lists are two readings of one thing,
  and interleaving them would invent transmissions that never happened;
- `raw_meta` verbatim under a `merged_from` key, plus `algorithm`, `keyId` and `nac` promoted to the top level when the
  winner has none: those are the facts trunkd knows about an encrypted call that no other recorder does.

**When the newcomer wins, the pair swaps.** The newcomer becomes the primary; the old primary *and every row that
pointed at it* get `duplicate_of = newcomer.id`, so three copies collapse to one primary and two duplicates. The
conversation membership moves to the newcomer (the thread's bounds are widened rather than recomputed — the loser is
still part of the same exchange, it is simply no longer the row representing it). Alerts already sent are left alone:
they were true when they were sent.

**The export ledger follows the primary**, because otherwise the winner is a primary with no ledger row and export
discovery publishes the same transmission a second time — an `uploaded` row stays on the copy that was actually sent and
the winner gets a `skipped` one, a `pending` row is re-pointed at the winner: see docs/EXPORTERS.md, "One upload per
transmission".

**Transcripts are never copied across.** Two tuners produce two different recordings and a transcript belongs to the
bytes it was made from. Instead, when the displaced copy had a finished transcript and the new primary has none, the
new primary is queued at **priority 1** — ahead of the backlog, because the archive briefly shows a primary with no text
where it had one. The talkgroup's own transcription policy still decides *whether* to queue at all; only the place in
the queue is overridden. The loser's queued job is canceled.

**A demotion is announced on the live bus** (2026-09-10). Whenever the loser was a primary row before the ruling — every
swap, and the batch/repair passes where either side may lose — reconciliation emits
`call.superseded` `{id: loser, by: winner, talkgroup_id, started_at}`, which the live feed forwards to WebSocket and SSE
clients as `{"type":"call.superseded","id":…,"by":…}` (the live event stream, ARCHITECTURE §4.4). Without it a swap left both copies on
screen: the newcomer arrived as its own `call.new` while the demoted row simply stayed where it was, and no event could
dislodge it — a `call.new` for a row stored as a duplicate is filtered out, because the call filters exclude duplicates.
Nothing is sent on the ordinary ingest path where the *existing* primary wins: that newcomer was never shown. The
browser drops the row on the event, except the one call it is currently playing, which is dimmed and labelled
"superseded" instead and falls off at the next refresh.

### Worked examples

| scenario | primary | why |
|---|---|---|
| Two identical pushes of one call | the first | exact `sha256`; every component ties, so the existing primary stays |
| 1.2 s trunkd fragment vs 8 s sdrtrunk call | **sdrtrunk** | duration (rank 2) beats the fragment's richer metadata (rank 4); the alias, emergency flag and radio merge into the sdrtrunk row |
| sdrtrunk audio vs trunkd encrypted metadata-only (`audio_state='purged'`) | **sdrtrunk** | audio (rank 1); it inherits `encrypted`, `algorithm`, `keyId`, `nac` and trunkd's sources |
| trunkd live, then the sdrtrunk backlog with `file_write` time and 1.8 s of skew, both with audio and equal length | **trunkd** | everything ties down to rank 7, and `RECONCILE_PREFER_SOURCES` puts trunkd first |
| the same, but the trunkd copy is `purged` | **sdrtrunk** | audio |
| trunkd 24 s (hang-merged) vs sdrtrunk 8 s | **sdrtrunk** | the duration credit is withdrawn past the merge ratio |

---

## 4. Where it runs

**Inline, at ingest, on both paths** — the file watcher and `POST /api/call-upload` — replacing step 4. It runs in two
halves: before the insert, "is this a second copy?" (which is what says the directory's counters must not move again);
after the insert, the scoring, the merge and the possible swap, with two real rows to compare.

The push path **keeps answering `duplicate call rejected`** whenever the push turned out to be a second copy — sdrtrunk
string-matches that body — but the copy is now stored: the audio is written under `uploads/`, the ledger records it, and
it may have taken the primary seat.

**The batch pass** is for the backlog that arrives after the live feed, days later, where no inline hook can see both
copies at once:

```
keyup reconcile [--since ISO] [--system NAME] [--dry-run]
```

and, in the worker, every `RECONCILE_MINUTES` (default 30) over everything ingested since the previous pass — the
watermark is a `settings` row (`reconcile_last_pass_at`), and each pass re-covers one interval of overlap so a call
ingested *during* a pass cannot fall between two of them. One summary line per pass:

```
reconcile pass: scanned=1841 pairs=372 swaps=71 merges=372
```

It is **idempotent**: only primaries are examined and only primaries are matched, so on the second run each pair is
already resolved (the loser is no longer a primary) and nothing changes. Which of a pair is the "newcomer" is decided by
`ingested_at` (id as tie-break), so a tie leaves the row that was there first in the seat — exactly as it would at
ingest.

`RECONCILE_ENABLED=false` restores the old behavior everywhere: the old lock, the old fuzzy dedupe, first copy wins,
and the push path stores nothing on a duplicate.

---

## 5. The timestamps this rests on, and what they actually mean

Reconciliation compares intervals, so it is only as good as `started_at`. Three recorders write it three ways, and
only one of them is a guess:

| `time_source` | who says so | how good |
|---|---|---|
| `upload` | the recorder's own `dateTime` on the push — for trunkd, the **P25 control-channel grant** | exact |
| `sidecar` | trunk-recorder's `start_time_ms` | exact |
| `file_write` | derived: the sdrtrunk file stamp minus the measured duration | **≈ 2 s late** — see below |

### What the sdrtrunk filename stamp is

ARCHITECTURE §1.2 always said the `YYYYMMDD_HHMMSS` in the file name was the file-*write* instant, "≈ the call end",
and that call start = stamp − duration. That was an assumption, and a parity run against the previous recorder's archive appeared to
contradict it: 71 of 372 sdrtrunk file calls had a Keyup start "off by exactly one duration" from that archive's `StartTime`.

Chasing that produced a **wrong intermediate conclusion**, worth recording because the trap is a good one. Matching the
corpus against the archive on keys independent of any time hypothesis put 747 of 747 file stamps (≥ 3 s) on its `StartTime` and
none on `stamp + duration` — which looks like proof that the stamp is the call start, *if you assume the archive's `StartTime`
is one*. It is not. The archive is fed by sdrtrunk, and it turns out to record what sdrtrunk hands it at file close.

**trunkd settled it**, because trunkd's `started_at` comes from the control-channel grant and owes sdrtrunk nothing.
Pairing archive rows with trunkd calls of the same transmission — same talkgroup, duration within 15 %, and a search
bracket wide enough to admit *both* hypotheses, so the answer comes from where the residuals land rather than from the
search — over the window where both recorders were live:

| n = 17 unique pairs | `archive.StartTime − trunkd START` | `archive.StartTime − trunkd END` |
|---|---|---|
| min | +2.00 s | +0.78 s |
| median | +5.00 s | **+1.86 s** |
| max | +24.00 s | +2.26 s |
| standard deviation | 5.68 s | **0.43 s** |

The residual against the *start* spans 22 seconds and tracks the call's duration — the signature of a quantity that is
not a start at all. The residual against the *end* sits in a 1.5 s band. So:

> **The sdrtrunk filename stamp is the instant the recorder closed the file: `call end + 1.7 s`.**

The original rule stands. `started_at = recorded_at − duration_s`, `time_source='file_write'`, and the 71 "offset"
pairs in the parity run were the harness comparing a correct Keyup start against the archive's end-labeled `StartTime` — they
were right, and the label was wrong.

Three independent lines now agree: the corpus's own same-frequency collisions (one traffic channel carries one call at
a time, and reading the stamp as the end produces 4× fewer impossible overlaps than reading it as the start), the
chain corpus → archive, and archive → trunkd.

### What that costs, and who pays for it

An sdrtrunk file row's `started_at` is systematically **≈ 1.7 s late** — not scattered, biased. That single number is
why two of the defaults are what they are:

- `RECONCILE_CLOCK_SKEW_SECONDS = 2.0` is sized to swallow it, so a trunkd push carrying the true grant time and an
  sdrtrunk file of the same transmission still match. Anything under about 1.8 s would start missing real pairs.
- the **aligned-edges rung** exists because of it: at 1.7 s of offset the overlap ratio only clears 0.5 on calls of
  3.4 s and longer, and the corpus median is 2.2 s. Without rung 2 reconciliation would simply not fire on most of the
  archive.
- `file_write` is the one time source the score does **not** count as exact (rank 3). When two copies are otherwise
  equal, the one that knows when the call began should keep the seat.

If the bias is ever worth removing, it is a constant to subtract, not a rule to change — and it should be re-measured
first, since it is a property of the recorder's dequeue latency and not a law.

---

## 6. API and UI

| route | what |
|---|---|
| `GET /api/calls/{id}` | gains `duplicates: [{id, ingest_source, duration_s, audio_state, started_at, time_source, transcript_status, score}]`, `reconcile_reason` and `reconciled_at` |
| `POST /api/calls/{id}/make-primary` | forces a swap by hand (write scope). 409 when the call is already the primary. The ruling is recorded with `rule: "manual"` |
| `GET /api/admin/reconcile/stats` | pairs, the deciding rule's tally, primaries/duplicates per recorder, the settings in force, and when the batch pass last ran |

`include_duplicates=1` on `GET /api/calls` is unchanged and still the way to see hidden copies in a listing.

The call detail page grows an **Other copies** panel: each copy with its recorder, length, time source, audio state and
score, and a **Make primary** button on each.

---

## 7. Operations

| setting | default | |
|---|---|---|
| `RECONCILE_ENABLED` | `true` | `false` = the pre-reconciliation behavior, everywhere |
| `RECONCILE_CLOCK_SKEW_SECONDS` | `2.0` | two recorders' clocks, plus the ±1 s of an old `file_write` row |
| `RECONCILE_MIN_OVERLAP` | `0.5` | fraction of the shorter recording that must overlap |
| `RECONCILE_FRAGMENT_SECONDS` | `1.5` | a copy this short inside a longer one is a fragment of it |
| `RECONCILE_MERGE_RATIO` | `1.5` | past this multiple, the duration credit is withdrawn as merged calls |
| `RECONCILE_PREFER_SOURCES` | `["trunkd","trunk_recorder","upload","file"]` | JSON list; tie-break only |
| `RECONCILE_MINUTES` | `30` | worker batch pass interval |

**Reconciling an archive that predates this.** Run the batch pass over the whole thing once, in a dry run first:

```
keyup reconcile --dry-run                 # count the pairs, change nothing
keyup reconcile --system "Example System" # then do it
```

**Freeing calls hidden behind their own id.** The exact-hash rung's missing exclusion check (§1) wrote
`duplicate_of = <the row's own id>` onto 18,708 production calls. They are not lost — the bytes, the metadata and any
finished transcript are all still on the row — but nothing can reach them. Deploy the `_find` fix first, or the next
pass writes the same rows again; then:

```
keyup repair-self-duplicates --dry-run    # count them, change nothing
keyup repair-self-duplicates              # clear the self-pointer, in batches of 500
```

Each restored row gets a `reconcile_reason` of `{"rule": "repair:self-duplicate", …}` with the bogus tie kept under
`previous`, is offered to conversation grouping again (only if it is not already in a thread, so the thread's
`call_count` stays honest), and — if its transcript was `skipped` and it has no transcript row — is requeued in the band
it was *ingested* in, so the 2022 backlog goes behind today's traffic rather than in front of it. A call that is `done`
or `empty`, or that already has a transcript, is left exactly as it is. Committed every `--batch` rows so the live feed
keeps flowing beside it; safe to interrupt and safe to re-run, since a repaired row no longer points at itself.

**Freeing calls hidden behind a hash from another day.** The exact rung's missing time check (§1) folded 927 production
calls into unrelated recordings that happened to encode to the same bytes. Deploy the `_find` fix first; then:

```
keyup repair-far-hash-duplicates --dry-run          # count them, change nothing
keyup repair-far-hash-duplicates                    # restore them, in batches of 500
keyup repair-far-hash-duplicates --max-skew-s 300   # a narrower definition of "far"
```

Each restored row is treated exactly as the self-duplicate repair treats one — `reconcile_reason` becomes
`{"rule": "repair:far-hash-duplicate", …}` with the bogus ruling under `previous`, the conversation is reattached only
if the row is not already in a thread, and a `skipped` transcript with no transcript row is requeued in the band the
call was ingested in. Committed every `--batch` rows, safe to interrupt, safe to re-run. The next batch reconciliation
pass *will* re-examine every row this frees — with the fixed rung — and may rule some of them duplicates again on the
interval rung, which is the intent.

**"Why is my call hidden?"** `GET /api/calls/{id}` on the *duplicate* returns its `reconcile_reason`, which names the
winner, the component that decided and both scores. If the ruling is wrong, `POST /api/calls/{id}/make-primary`
overrides it and says so.

**Widening or narrowing the match.** Raise `RECONCILE_CLOCK_SKEW_SECONDS` if the two recorders' hosts are not
NTP-synchronized (it costs precision against back-to-back calls); raise `RECONCILE_MIN_OVERLAP` towards 0.8 if
short calls on a busy talkgroup are being folded together that should not be. `RECONCILE_ENABLED`,
`RECONCILE_CLOCK_SKEW_SECONDS` and `RECONCILE_MIN_OVERLAP` are also in the Admin page's **Ingest** section: the file
watcher re-reads them on every recording, so a change there lands on the next call; the API's push path and the
worker's batch pass read the environment at startup, so those follow on the next restart.
