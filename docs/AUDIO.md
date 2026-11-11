# Audio profiles

The processing chain every call goes through is described in
[ARCHITECTURE §5.5](ARCHITECTURE.md#55-audio-processing). This document is about the one
part of it that is **per channel** rather than per deployment.

## Why

The global chain is the right shape for what every recording needs. Trim the dead air,
turn the page tone down before the loudness stage reads it, land on -16 LUFS, guarantee
nothing clips: those are true of the whole archive, and a setting that changes them changes
them everywhere on purpose.

Noise is not like that. On the system this was built against, one talkgroup carries medevac helicopter
transports, and every call on it has rotor and turbine energy in a narrow band around
1.9 kHz. Nothing in the global chain removes it. `afftdn` at a lower noise floor does not —
the noise is not a stationary hiss, it is a loud band with structure — and a denoiser
aggressive enough to take it out eats the speech on the other four hundred channels that
share the setting. What removes it is a **40 dB cut from 1.7 to 2.2 kHz**, which is exactly
the wrong thing to do to a fire dispatch channel.

So the chain's noise stages become data: a named, ordered list of filter steps, assigned to
the talkgroups that need it.

## What a profile is and is not

A profile replaces the **noise section** of the global chain — the 300–3800 Hz band-pass,
the denoiser and the gate — for the talkgroups pointed at it. It is spliced in where those
stages would have been, so a profile that wants band edges declares its own (`Default` and
`Speech band-pass` both do):

```
atrim → tones → ┤ the profile's steps ├ → loudnorm → alimiter
```

Everything on either side is measured from the call itself and stays where it is. A profile
cannot switch off the trim, cannot keep a page tone at full level, and cannot remove the
loudness tail — that tail (`loudnorm` to `audio_target_lufs`, `alimiter` at
`audio_true_peak_db`) is appended by the renderer, and it is the one promise every file in
the archive makes.

An **empty** chain is a real profile: it means "no denoiser, no gate", which is the honest
way to say a channel is clean and should be left alone.

A talkgroup with **no** profile follows the global `audio_*` settings, exactly as every
talkgroup did before profiles existed. The shipped `Default` profile is a snapshot of those
settings written as data — assigning it changes nothing on a default-configured deployment,
and freezes the chain on one whose settings have moved on.

## Storage

`audio_profiles(id, name unique, description, chain jsonb, builtin, created_at, updated_at)`
and `talkgroups.audio_profile_id` (nullable, `ON DELETE SET NULL`).

`chain` holds the steps, never a rendered ffmpeg string:

```json
[
  {"kind": "highpass",  "params": {"f": 250.0}},
  {"kind": "equalizer", "params": {"f": 1950.0, "width": 500.0, "gain_db": -40.0}},
  {"kind": "afftdn",    "params": {"nf": -28.0, "nr": 12.0}}
]
```

The command line is generated from that on every use. Nothing an operator typed is ever
stored in the form it would be executed in, and a step kind that gains a parameter does not
have to re-parse an old command line to find out what the row meant.

`app/audioproc/profiles.py` is the whole boundary between operator input and ffmpeg. Every
kind is in `STEP_KINDS`, every parameter is a number confined to a range that table
declares, and a chain is validated when it is **written** — so a stored profile is by
construction one that renders, rather than one that is discovered to be broken at three in
the morning while the queue drains. A chain the validator accepted cannot contain an
operator's punctuation, because it does not contain any of an operator's characters at all:
only numbers the module formatted itself. A chain is capped at **12 steps**.

## Step reference

The editor generates its form from `GET /api/admin/audio/profiles/steps`, which is this
table. A parameter with a default may be left out; a parameter with no default is genuinely
optional and, left out, is simply not passed to ffmpeg.

| Kind | Parameters (range, default) | Renders as | What it is for |
|---|---|---|---|
| `highpass` | `f` 20–20000 Hz, 300 | `highpass=f=…` | Engine rumble, handling thumps, rotor beat. Speech starts at the male fundamental near 85 Hz but carries almost no intelligibility below 300. |
| `lowpass` | `f` 20–20000 Hz, 3000 | `lowpass=f=…` | 3000–3800 Hz spans the telephone band up to the top of what an 8 kHz P25 vocoder carries; the global chain uses 3800, since 3400 was measured to cost 1.2 dB of consonant energy in a band-pass comparison on recorded calls. |
| `bandreject` | `f` 20–20000 Hz, 1950; `width` 10–10000 Hz, 500 | `bandreject=f=…:w=…:t=h` | Removes a band outright. Use it when nothing you want is inside the band. |
| `equalizer` | `f` 20–20000 Hz, 1950; `width` 10–10000 Hz, 500; `gain_db` -60–20 dB, -40 | `equalizer=f=…:w=…:g=…:t=h` | A peaking filter: negative gain cuts, positive lifts. **This is the aircraft fix.** Attenuates rather than removes, so speech sharing the band survives. |
| `notch` | `f` 20–20000 Hz, 1000; `q` 0.1–100, 10; `gain_db` -60–0 dB, -40 | `equalizer=f=…:w=…:g=…:t=q` | A narrow cut placed by Q rather than width — one steady whistle (a data carrier, a squelch tail, a rotor harmonic). |
| `afftdn` | `nf` -80–-20 dB, -25; `nr` 0.01–97 dB, *(none)* | `afftdn=nf=…[:nr=…]` | Spectral noise reduction. Lower `nf` is more aggressive, `nr` is how many dB of the assumed floor it removes. The global chain runs `nf=-35:nr=18` behind a 300–3800 Hz band-pass; `nf` must sit near the real floor (about -63 dBFS on this system) or the subtraction comes out of the fricatives (the same band-pass comparison measured it). |
| `anlmdn` | `s` 1e-5–10, *(none)*; `p` 0.001–0.1 s, *(none)*; `r` 0.002–0.3 s, *(none)* | `anlmdn[=s=…:p=…:r=…]` | **Not recommended here.** Measured on this system's vocoder audio it *raised* the noise floor 8–9 dB (one radio's calls, Δsnr −6.05) and did nothing on the next radio tested (+0.35). Use the band-pass with `afftdn nf=-35 nr=18` for hiss. Kept selectable for a channel with a different problem. |
| `agate` | `threshold` -100–0 dBFS, -50; `ratio` 1–9000, 4; `attack` 0.01–9000 ms, 5; `release` 0.01–9000 ms, 250 | `agate=threshold=<linear>:ratio=…:attack=…:release=…` | Ducks what is under the threshold, so a 30 dB lift on a quiet talker does not raise the hiss between his words. The threshold is stored as the dBFS an operator reads off the meter and rendered as the amplitude ffmpeg wants. A profile's threshold is the literal number written here; only the *global* chain's gate follows the clip's own level (§5.5). |

## The shipped profiles

Three, seeded by the migration and marked `builtin`. Builtins are **editable** — an operator
who wants a different noise floor on `Default` should have it — and **not deletable**, so
the names this document and the seeds refer to keep existing.

| Name | Rendered chain |
|---|---|
| **Default** | `highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18,agate=threshold=<from the clip's level>:ratio=4:attack=5:release=250,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50` (`0.003162`, -50 dBFS, on a call that arrived at -20) |
| **Aircraft cabin** | `highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h,afftdn=nf=-28:nr=12,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50` |
| **Speech band-pass** | `highpass=f=300,lowpass=f=3000,afftdn=nf=-25,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50` |

`Default` is pinned by a test to render the *same stages the global chain renders*. If it
ever stops doing so, assigning it to a talkgroup would silently rewrite that channel's whole
archive for no audible reason, so the test is where that change gets noticed.

### The aircraft example, end to end

`Aircraft cabin` is the profile written for that medevac talkgroup. Read left to right:

* `highpass=f=250` — the airframe's low rumble, below anything the voice needs.
* `equalizer=f=1950:w=500:g=-40:t=h` — the rotor and turbine band, 1.7 to 2.2 kHz, down
  40 dB. A peaking cut rather than a `bandreject` on purpose: speech has energy in that band
  too, and attenuating it 40 dB leaves it inaudible against the formants above and below
  while removing the noise floor that was masking them.
* `afftdn=nf=-28:nr=12` — a denoise tuned for this one channel rather than the global `nf=-35:nr=18`, which is safe here
  because it is running on one channel rather than four hundred.

Then the tail the renderer always appends.

## Working in the UI

Admin → **Audio** → *Audio profiles*.

1. **Edit** a profile (or **New**) to open the step editor: add, remove and reorder steps,
   and type numbers into the fields the whitelist describes. The rendered chain is shown as
   you go, and a chain the server refuses comes back with a sentence saying which parameter
   is wrong.
2. **Preview** inside the editor. Give it a call id and press Render: the chain is run over
   that call's **original** audio and handed straight back as an MP3, with an A/B toggle
   between the original and the result. Nothing is written — no row is touched, no file is
   put beside the call, and the profile need not exist yet. This is how a chain is tuned
   *before* it is a profile at all. At most 60 seconds of audio is rendered per press.
   A call page's **Tune…** link opens the editor with that call already loaded.
3. **Assign** the profile on the talkgroup — in the talkgroup edit dialog, or to a selection
   at once in the bulk edit drawer.
4. **Apply** it. Assigning a profile changes what *future* calls sound like; the calls
   already on disk were rendered through whatever chain was current when they arrived.
   `POST /api/admin/audio/profiles/{id}/apply` assigns the profile to the talkgroups named
   and queues their calls from the last `hours` (24 by default) to be re-rendered through
   it. `all_with_profile` skips the assignment and sweeps every channel already on the
   profile, which is what to press after *editing* one.

The requeue is **forced** — the calls' processed copies are current by pipeline version, and
it is the chain that changed, not the pipeline — and runs at the backlog band (600), behind
anything arriving live. It is safe to repeat: a call with an open job is skipped by the
queue's own partial unique index. For a window longer than a day, use
`POST /api/admin/audio/reprocess` with `talkgroup` and `force`.

## Traceability

`calls.audio_meta` records what produced each file:

* `filter` — the literal ffmpeg `-af` string that was run, trim and tone stages included.
* `profile` — the name of the profile that supplied the middle of it, or `null` for the
  global chain.

The string alone does not answer "which chain made this" once two profiles happen to render
alike, which is why the name is stored beside it. The Calls page shows it as
*Audio profile: <name>*.

## API

| Route | What it does |
|---|---|
| `GET /api/admin/audio/profiles/steps` | The whitelist: every kind, its parameters, and their ranges. The editor's form is generated from this rather than repeating the ranges in TypeScript. |
| `GET /api/admin/audio/profiles` | The profiles, each with its rendered chain and how many talkgroups are on it. |
| `POST /api/admin/audio/profiles` | Create. 409 on a duplicate name, 422 on a chain the whitelist refuses. |
| `PATCH /api/admin/audio/profiles/{id}` | Edit. Builtins included; only the keys present are written. Editing does not re-render anything on its own. |
| `DELETE /api/admin/audio/profiles/{id}` | 409 for a builtin, and 409 **with the count** for a profile any talkgroup is still on: deleting it would quietly move those channels back to the global chain, which is a change to how their audio sounds and should be made by reassigning them. |
| `POST /api/admin/audio/profiles/preview` | Render a chain over one call's original audio; returns `audio/mpeg` and the chain in `X-Keyup-Filter`. Stores nothing. |
| `POST /api/admin/audio/profiles/{id}/apply` | Assign and requeue, as above. |
| `PATCH /api/talkgroups/{id}`, `POST /api/talkgroups/bulk` | Both take `audio_profile_id`; `null` clears it back to the global chain. |

Everything that mutates needs the `admin` scope. The reads are open, like the rest of the
archive's diagnostics.

## Failure behavior

A stored chain that will not render is logged once and the call falls back to the global
chain. It should be impossible — a chain is validated when it is written — but the
alternative is a talkgroup whose every call parks in the queue because of one bad row, and a
call processed by the global chain is a call an operator can still hear.
