/**
 * Types for the Keyup API.
 *
 * Everything here is derived from `generated.ts`, which `npm run gen:api-types`
 * produces from `openapi.json` (a snapshot of the API's own `/api/openapi.json`).
 * The aliases below exist so components keep reading like the domain — `Call`,
 * `Talkgroup`, `SttBackend` — while the shapes stay whatever the server actually
 * sends. Nothing in this file invents a field.
 *
 * Two transformations are applied on the way through:
 *
 * 1. `Concrete<T>`. FastAPI marks every field that has a default as *optional* in
 *    the schema, but a response model always serializes all of its fields, so the
 *    UI would be checking for `undefined` on ~80 % of every object for no reason.
 *    `Concrete` puts the `?` back to required; `| null` is left exactly as the
 *    schema declares it, because null really does arrive.
 * 2. Narrowing of a handful of `string` columns the database does not constrain
 *    but the UI renders from a fixed map (talkgroup category, alert channel).
 *    Those are the only two places a cast is involved, and both are marked.
 */
import type { components, operations } from "./generated";

type Schemas = components["schemas"];

/** Optional-because-it-has-a-default → required. See the note above. */
type Concrete<T> = { [K in keyof T]-?: T[K] };

/* ------------------------------------------------------------------ enums */

/**
 * The unions come from the schema so they cannot drift; the `const` arrays next
 * to them are what the selects and legends iterate at runtime. `types.test.ts`
 * asserts each pair still describes the same set.
 */

export type CallType = NonNullable<Schemas["CallOut"]["call_type"]>;
export const CALL_TYPES = ["group", "patch", "individual"] as const satisfies readonly CallType[];

/**
 * Who the transmission was addressed to — a different axis from `CallType`, which says
 * how the channel was granted. `unit` is a P25 private call: one radio to one radio, on
 * no talkgroup at all, so `talkgroup_id`, `tgid` and `talkgroup_ids` are empty and
 * `target_radio_id` names the far end.
 */
export type CallKind = NonNullable<Schemas["CallOut"]["kind"]>;
export const CALL_KINDS = ["group", "unit"] as const satisfies readonly CallKind[];

/** Is this a unit-to-unit (private) call? Anything that does not say, is not. */
export const isUnitCall = (call: { kind?: CallKind | null }): boolean => call.kind === "unit";

export type TranscriptStatus = NonNullable<Schemas["CallOut"]["transcript_status"]>;
export const TRANSCRIPT_STATUSES = [
  "pending",
  "queued",
  "running",
  "done",
  "skipped",
  "failed",
  "empty",
] as const satisfies readonly TranscriptStatus[];

export type TranscriptQuality = NonNullable<Schemas["CallOut"]["transcript_quality"]>;
export const QUALITIES = ["good", "suspect", "noise"] as const satisfies readonly TranscriptQuality[];

export type IngestSource = NonNullable<Schemas["CallOut"]["ingest_source"]>;
export const INGEST_SOURCES = [
  "file",
  "upload",
  "trunk_recorder",
  "trunkd",
] as const satisfies readonly IngestSource[];

/**
 * The three classes `?priority=` filters `calls.ingest_priority` by.
 *
 * Not an enum on the wire — the column is free text, because the push path also accepts
 * `urgent` and a bare integer — so this is a hand-kept list rather than a generated one.
 * It mirrors `FILTER_CLASSES` in `backend/app/ingest/flow.py`, which is also where the
 * reading is defined: `urgent` reads back as `live`, an integer is judged against the
 * transcription queue's band edges, and a null (a call the file watcher found on disk)
 * is in none of the three.
 */
export const INGEST_PRIORITY_CLASSES = ["live", "backlog", "bulk"] as const;
export type IngestPriorityClass = (typeof INGEST_PRIORITY_CLASSES)[number];

/**
 * Which class a stored `ingest_priority` reads back as, or `null` for none.
 *
 * The two band edges are `BACKLOG_BAND` (500) and `BULK_PRIORITY` (900) in
 * `backend/app/ingest/flow.py`: at or above 500 a job may be starved by live traffic for
 * as long as live traffic lasts, which is what "backlog" means, and 900 is the back of
 * the queue. Anything unreadable — and a null — is in no class at all, matching the
 * server's fail-closed reading rather than being folded into `live`.
 */
export function priorityClass(call: { ingest_priority?: string | null }): IngestPriorityClass | null {
  const raw = (call.ingest_priority ?? "").trim().toLowerCase();
  if (!raw) return null;
  if (raw === "live" || raw === "urgent") return "live";
  if (raw === "backlog" || raw === "bulk") return raw;
  const numeric = Number(raw);
  if (!Number.isInteger(numeric)) return null;
  if (numeric < 500) return "live";
  return numeric < 900 ? "backlog" : "bulk";
}

/** Did this recording arrive long after it was recorded, rather than live? */
export function isBackfilled(call: { ingest_priority?: string | null }): boolean {
  const name = priorityClass(call);
  return name === "backlog" || name === "bulk";
}

/**
 * Will the API serve this call's recording? The `PRESENT_STATES` in `backend/app/audio.py`:
 * `present`, and `archived` (moved by retention, still served). `discarded`, `purged` and
 * `missing` answer 410, and `undefined` (the row not loaded yet) is `false` so nothing is
 * requested on a guess.
 */
export function hasPlayableAudio(state: string | null | undefined): boolean {
  return state === "present" || state === "archived";
}

export type TimeSource = NonNullable<Schemas["CallOut"]["time_source"]>;
export const TIME_SOURCES = [
  "file_write",
  "upload",
  "event_log",
  "sidecar",
] as const satisfies readonly TimeSource[];

export type RadioKind = NonNullable<Schemas["RadioOut"]["kind"]>;
export const RADIO_KINDS = [
  "console",
  "portable",
  "mobile",
  "unknown",
] as const satisfies readonly RadioKind[];

/**
 * Not an enum in the database — a talkgroup's category is free text seeded from
 * the playlist import — but the UI colors and groups by it, so these are the
 * values it knows how to draw. Anything else falls through to the neutral slot.
 *
 * Mirrors `TALKGROUP_CATEGORIES` in `backend/app/models.py`, in the same order.
 * There is no codegen for it — the column is free text, so it never reaches the
 * OpenAPI document as an enum — which is why the two lists are kept side by side
 * and why `CategoryIcon` falls back rather than trusting the value.
 */
export const TALKGROUP_CATEGORIES = [
  "law",
  "fire",
  "ems",
  "multi",
  "interop",
  "pw",
  "utility",
  "transit",
  "rail",
  "aviation",
  "schools",
  "corrections",
  "security",
  "federal",
  "military",
  "media",
  "business",
  "ham",
  "data",
  "other",
] as const;
export type TalkgroupCategory = (typeof TALKGROUP_CATEGORIES)[number];

/**
 * What `TalkgroupIn.priority` and `TalkgroupPatch.priority` accept.
 *
 * A talkgroup's priority is copied onto its live transcription jobs, and the same
 * number 500 higher is what its backlog runs at, in a queue that stops at 999 — so
 * 499 is the ceiling and 1 is worked first. The bound is on the schema
 * (`backend/app/schemas.py`), but `ge`/`le` on an integer does not reach
 * `generated.ts`, so the two numbers are mirrored here rather than derived.
 */
export const TALKGROUP_PRIORITY_MIN = 1;
export const TALKGROUP_PRIORITY_MAX = 499;

/**
 * RadioReference's own service-tag vocabulary, in RR's spelling — what the RR
 * sync writes into `talkgroups.service_tag` and the only values `PATCH
 * /talkgroups/{id}` accepts (a 422 otherwise). Finer than the category: RR
 * separates dispatch from tac from talk, which the category deliberately does
 * not. Mirrors `RR_SERVICE_TAGS` in `backend/app/models.py`.
 */
export const RR_SERVICE_TAGS = [
  "Aircraft",
  "Business",
  "Corrections",
  "Data",
  "Deprecated",
  "Emergency Ops",
  "EMS Dispatch",
  "EMS-Tac",
  "EMS-Talk",
  "Federal",
  "Fire Dispatch",
  "Fire-Tac",
  "Fire-Talk",
  "Ham",
  "Hospital",
  "Interop",
  "Law Dispatch",
  "Law Tac",
  "Law Talk",
  "Media",
  "Military",
  "Multi-Dispatch",
  "Multi-Tac",
  "Multi-Talk",
  "Other",
  "Public Works",
  "Railroad",
  "Schools",
  "Security",
  "Transportation",
  "Utilities",
] as const;
export type RrServiceTag = (typeof RR_SERVICE_TAGS)[number];

export type JobStatus = Schemas["TranscriptionJobOut"]["status"];
export const JOB_STATUSES = [
  "queued",
  "running",
  "done",
  "failed",
  "cancelled",
] as const satisfies readonly JobStatus[];

export type IngestFileStatus = Schemas["IngestFileOut"]["status"];
export const INGEST_FILE_STATUSES = [
  "ingested",
  "duplicate",
  "failed",
  "ignored",
] as const satisfies readonly IngestFileStatus[];

export type SttEngine = Schemas["TranscriptOut"]["engine"];
export const STT_ENGINES = [
  "openai_compatible",
  "faster_whisper_local",
  "openvino_genai",
  "ovms",
  "manual",
] as const satisfies readonly SttEngine[];

export type LinkConfidence = NonNullable<Schemas["ExternalLinkOut"]["link_confidence"]>;
export const LINK_CONFIDENCES = ["strong", "probable", "weak"] as const satisfies readonly LinkConfidence[];

export type AlertKind = Schemas["AlertRuleOut"]["kind"];
export const ALERT_KINDS = [
  "talkgroup",
  "radio",
  "keyword",
  "regex",
  "channel_audit",
  "new_kind",
  "exemplar_changed",
  "disk_low",
  "health",
  "emergency",
] as const satisfies readonly AlertKind[];

/** `channels` is `string[]` on the wire; these are the transports the UI offers. */
export const ALERT_CHANNELS = ["webhook", "discord", "ntfy", "email"] as const;
export type AlertChannel = (typeof ALERT_CHANNELS)[number];

export type TokenScope = NonNullable<Schemas["ApiTokenIn"]["scopes"]>[number];
export const TOKEN_SCOPES = [
  "read",
  "write",
  "admin",
  "stream",
  "upload",
] as const satisfies readonly TokenScope[];

export type RetentionScope = NonNullable<Schemas["RetentionPolicyOut"]["scope"]>;
export const RETENTION_SCOPES = [
  "global",
  "system",
  "talkgroup",
] as const satisfies readonly RetentionScope[];

/* -------------------------------------------------------------- envelopes */

/** `page`/`page_size` list response; every `Page_*_` in the schema has this shape. */
export interface Paginated<T> {
  items: T[];
  /**
   * Rows matching the filter, ignoring pagination — or `null` while cursor paging, where
   * the API deliberately does not count (an infinite scroll shows no total, and counting
   * for one is a full scan of the filtered set per page).
   */
  total: number | null;
  /** True when `total` stopped at the API's counting cap and the real number is larger. */
  total_capped?: boolean;
  page: number;
  page_size: number;
  /** Opaque cursor for the next page of a large scroll (§4 conventions). */
  next_cursor: string | null;
}

/* -------------------------------------------------------------- directory */

/** `wacn` and `system_id` are numbers: they are radio-network identifiers, not names. */
export interface SystemInfo extends Concrete<Schemas["SystemOut"]> {}

/** What `PATCH /systems/{id}` accepts — the name and the description, and nothing else. */
export type SystemPatch = Schemas["SystemPatch"];

/** What `POST /systems/{id}/merge-into/{target}` reports about the fold. */
export type SystemMergeResult = Schemas["SystemMergeResult"];

/**
 * A tower site. `name` is unique only *within* its system — two systems can both
 * have a `Springfield` — so `system_name` travels with every row and the UI labels a
 * site "System / Site" rather than by name alone (`siteLabel` in `lib/format`).
 */
export interface SiteInfo extends Concrete<Schemas["SiteOut"]> {}

/** What `PATCH /sites/{id}` accepts — name, notes and the RadioReference mapping. */
export type SitePatch = Schemas["SitePatch"];

/** What `POST /sites/{id}/merge-into/{target}` reports about the fold. */
export type SiteMergeResult = Schemas["SiteMergeResult"];

/* ------------------------------------------------ recorders & tuners (§4.8) */

/** What a tuner may be doing, from the operator's point of view. */
export type TunerRole = NonNullable<Schemas["TunerDesiredIn"]["role"]>;

export const TUNER_ROLES: readonly TunerRole[] = ["auto", "control", "voice", "data"] as const;

/**
 * What a recorder last reported for one tuner. The recorder may add fields, so the set is
 * open; these are the ones the operator view reads today.
 */
export interface TunerReported {
  serial?: string | null;
  backend?: string | null;
  center_hz?: number | null;
  span_hz?: number | null;
  role?: string | null;
  site?: string | null;
  locked?: boolean | null;
  control_hz?: number | null;
  nac?: number | null;
  tsbk_rate?: number | null;
  pool_in_use?: number | null;
  pool_size?: number | null;
  overflow_per_s?: number | null;
  calls_active?: number | null;
  applied_version?: number | null;
  /**
   * The downlink ranges this tuner has actually restricted itself to — band labels or
   * explicit `"lo-hi"` Hz ranges. Absent means unrestricted: it records anything inside
   * its capture span, which is not the same fact as an empty list and reads differently.
   */
  bands?: string[] | null;
  /** What the operator says is on this tuner's antenna run. keyup only displays it. */
  description?: TunerDescription | null;
  /**
   * The gain the tuner is running — what `rssi_dbfs` has to be read against.
   *
   * Two shapes, both live: an older recorder sends the nested {@link TunerGain} object, a
   * current one sends the overall figure as a bare number and the rest flat beside it.
   * {@link tunerGain} reads either, and everything that renders a gain goes through it.
   */
  gain?: number | TunerGain | null;
  /**
   * Wideband level in dBFS, referenced to the ADC full scale rather than to an antenna:
   * it moves with every gain setting above, so it is a level, not a dBm signal strength.
   */
  rssi_dbfs?: number | null;
  /** Control-channel tracking error in Hz; only the control-following tuner reports one. */
  freq_error_hz?: number | null;

  /* ---- the front end, as the tuner reports it -----------------------------------
   *
   * The reported twin of every editable setting on {@link TunerDesired}. The dialog reads
   * these to seed itself and to bound what may be typed, so a tuner that has not reported
   * one of them is edited without that bound rather than not edited at all.
   */
  /** Per-element gain in dB, keyed as in {@link gain_elements}. */
  gains?: Record<string, number> | null;
  agc?: boolean | null;
  ppm?: number | null;
  auto_ppm?: boolean | null;
  /** What auto-ppm has actually settled on — read-only, and the reason the toggle is worth it. */
  auto_ppm_applied?: number | null;
  bandwidth_hz?: number | null;
  rx_port?: string | null;
  settings?: Record<string, string> | null;
  lna_db?: number | null;
  vga_db?: number | null;
  amp?: boolean | null;
  sample_rate?: number | null;
  data_follow?: boolean | null;
  control_frequencies?: number[] | null;
  /** The named gain stages this device has — the list the per-element sliders are built from. */
  gain_elements?: TunerGainElement[] | null;
  capabilities?: TunerCapabilities | null;
  /**
   * Desired fields the recorder refused, keyed by field name with the reason. Also lifted
   * onto {@link Tuner.rejected} by the API, which is where the page reads it.
   */
  rejected?: Record<string, string> | null;
  [key: string]: unknown;
}

/** One named gain stage and the range it accepts. Without it there is no slider to draw. */
export interface TunerGainElement {
  name: string;
  min?: number | null;
  max?: number | null;
  /** Granularity in dB; 0 or absent means continuous. */
  step?: number | null;
}

/**
 * What this tuner can change without restarting.
 *
 * `restart_on` names the desired fields whose change tears the stream down and builds it
 * again — a gap in the recording on a live site. Nothing refuses those edits; the dialog
 * says so next to the field, because an operator who knows is choosing and one who does
 * not is surprised.
 */
export interface TunerCapabilities {
  runtime_gain?: boolean | null;
  runtime_ppm?: boolean | null;
  restart_on?: string[] | null;
}

/** One tuner's hardware, as its operator described it. Every field may be missing. */
export interface TunerDescription {
  receiver?: string | null;
  antenna?: string | null;
  cable?: string | null;
  amplifier?: string | null;
  height_m?: number | null;
  notes?: string | null;
}

/**
 * The gain settings a tuner is running; which fields exist depends on the backend.
 *
 * A current recorder reports these flat on {@link TunerReported} *and* an overall `gain` as
 * a bare number; this nested object is the older shape, still sent by recorders that have
 * not moved. {@link tunerGain} reads whichever is there, so nothing else has to care.
 */
export interface TunerGain {
  gain?: number | null;
  lna_db?: number | null;
  vga_db?: number | null;
  amp?: boolean | null;
}

/**
 * One tuner's gain, whichever way it was reported: the flat fields first, then the nested
 * `gain` object, then the bare overall number `gain` may also be.
 *
 * Written as a reader rather than as a migration because both shapes are live at once — a
 * deployment runs whatever recorder binaries it runs — and a page that understood only the
 * newer one would show "—" for the gain of a recorder that is working perfectly.
 */
export function tunerGain(reported: TunerReported | null | undefined): TunerGain {
  if (!reported) return {};
  const nested =
    typeof reported.gain === "object" && reported.gain !== null ? (reported.gain as TunerGain) : {};
  const overall = typeof reported.gain === "number" ? reported.gain : nested.gain;
  return {
    gain: overall ?? null,
    lna_db: reported.lna_db ?? nested.lna_db ?? null,
    vga_db: reported.vga_db ?? nested.vga_db ?? null,
    amp: reported.amp ?? nested.amp ?? null,
  };
}

/**
 * Where and what a recorder is, as its operator described it: facts nobody but the person
 * who built the station knows. `null` on the recorder means it has never reported one —
 * which is not the same as reporting an empty station, and reads differently on the page.
 */
export interface RecorderStation {
  name?: string | null;
  location?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  elevation_m?: number | null;
  contact?: string | null;
  notes?: string | null;
}

/** A recorder's instance-level lifetime counters. */
export interface RecorderCounters {
  calls_started?: number | null;
  calls_ended?: number | null;
  uploaded?: number | null;
  spooled?: number | null;
  failed?: number | null;
  spool_bytes?: number | null;
  disk_free_bytes?: number | null;
}

/** What an operator wants of one tuner. */
export interface TunerDesired {
  role?: TunerRole | null;
  site_id?: number | null;
  site?: string | null;
  center_frequency?: number | null;
  enabled?: boolean | null;
  spectrum?: boolean | null;
  /**
   * Which downlink ranges the tuner may record. `[]` is a real desired value — *no*
   * restriction — while the key being absent means keyup is not managing the bands at all
   * and the recorder stays on whatever its own configuration says.
   */
  bands?: string[] | null;

  /* ---- the front end ------------------------------------------------------------- */
  gain?: number | null;
  gains?: Record<string, number> | null;
  agc?: boolean | null;
  ppm?: number | null;
  auto_ppm?: boolean | null;
  auto_ppm_limit?: number | null;
  bandwidth_hz?: number | null;
  rx_port?: string | null;
  settings?: Record<string, string> | null;
  lna_db?: number | null;
  vga_db?: number | null;
  amp?: boolean | null;
  data_follow?: boolean | null;
  /** Act on grants a linked peer published, not only the ones this tuner decoded itself. */
  remote_grants?: boolean | null;
  control_frequencies?: number[] | null;
  sample_rate?: number | null;
  [key: string]: unknown;
}

export interface Tuner extends Omit<Concrete<Schemas["TunerOut"]>, "reported" | "desired" | "rejected"> {
  reported: TunerReported | null;
  desired: TunerDesired | null;
  /**
   * What the recorder refused of the last write, keyed by field. A refusal does *not* leave
   * the tuner drifted — the recorder acknowledges the version either way — so this is the
   * only thing that says the setting did not take.
   */
  rejected: Record<string, string> | null;
}

/**
 * What a recorder reports about its **upload path**: whether it is holding calls in its
 * local spool rather than pushing them, where that setting came from, and how much is
 * waiting while it holds. Null on any recorder running a binary that predates the hold.
 *
 * `source` is a plain string rather than a union on purpose: it is the *recorder's* account
 * of why it is holding, and a word keyup does not know is worth showing verbatim rather
 * than flattening to "unknown".
 */
export interface RecorderUplink {
  spool_only?: boolean | null;
  /** Where the current setting came from: `config` | `keyup` | `panel`, or whatever else. */
  source?: string | null;
  spooled_calls?: number | null;
  spool_bytes?: number | null;
  url?: string | null;
  [key: string]: unknown;
}

/**
 * One peer this recorder is linked to. Everything is optional because the whole `link`
 * object is opaque on the wire: keyup stores what the recorder sends and renders what it
 * recognizes, so a recorder that grows a field costs no migration and one that omits a
 * field costs one blank cell.
 */
export interface RecorderLinkPeer {
  instance?: string | null;
  alive?: boolean | null;
  last_seen?: string | null;
  /** What this peer currently claims — sites, tuners, whatever the recorder calls them. */
  claims?: unknown;
  [key: string]: unknown;
}

/** What a recorder reports about the peers it is linked to. */
export interface RecorderLink {
  peers?: RecorderLinkPeer[] | null;
  [key: string]: unknown;
}

/**
 * What a recorder says about its own event-log archive: how far behind it is, and which day
 * it last shipped. What actually *arrived* is `GET /admin/event-logs`; this is the
 * recorder's side of the same question, and the two disagreeing is itself worth seeing.
 */
export interface RecorderEventLogStatus {
  /** Days still waiting to be uploaded. */
  pending?: number | null;
  /** The last day successfully shipped, `YYYY-MM-DD`. */
  last_date?: string | null;
  bytes_pending?: number | null;
  [key: string]: unknown;
}

/**
 * The recorder-to-recorder link **as an operator reads it**: everything keyup wants, with
 * the shared key replaced by the two facts a page may know about it. `key_set` says one is
 * stored at all and `key_fingerprint` — the first 8 hex of its sha256 — is what shows two
 * peers hold the same key without either page ever having the key.
 *
 * The secret itself is fetched once, deliberately, by the settings dialog
 * (`GET /admin/recorders/{id}/link/key`) and never rides along in a list.
 */
export interface RecorderDesiredLink {
  enabled: boolean;
  bind: string;
  peers: string[];
  hello_interval_s: number;
  accept_remote_grants: boolean;
  publish_grants: boolean;
  defer_to_claims: boolean;
  key_set: boolean;
  key_fingerprint: string | null;
}

/**
 * The link block **as it is written**. The mirror image of {@link RecorderDesiredLink}: the
 * key is present and the fingerprint is not, and `key` may be left out entirely to keep
 * whatever is already stored — which is the ordinary save, because the dialog holds the
 * secret only if the operator asked to see it.
 */
export interface RecorderLinkInput {
  enabled: boolean;
  bind: string;
  peers: string[];
  hello_interval_s: number;
  accept_remote_grants: boolean;
  publish_grants: boolean;
  defer_to_claims: boolean;
  key?: string;
}

/**
 * What the recorder echoed back that it applied — the recorder-level twin of a tuner's
 * `applied_version`, with the values beside the number so the dialog can show applied
 * against pending per field rather than one badge for the whole block.
 */
export interface RecorderApplied {
  version?: number | null;
  spool_only?: boolean | null;
  event_log_archive?: boolean | null;
  link?: (Partial<RecorderDesiredLink> & { [key: string]: unknown }) | null;
  [key: string]: unknown;
}

export interface Recorder
  extends Omit<
    Concrete<Schemas["RecorderOut"]>,
    | "tuners"
    | "station"
    | "counters"
    | "uplink"
    | "link"
    | "event_log"
    | "desired_link"
    | "recorder_applied"
    | "recorder_rejected"
    | "recorder_capabilities"
  > {
  tuners: Tuner[];
  station: RecorderStation | null;
  counters: RecorderCounters | null;
  uplink: RecorderUplink | null;
  link: RecorderLink | null;
  event_log: RecorderEventLogStatus | null;
  desired_link: RecorderDesiredLink | null;
  recorder_applied: RecorderApplied | null;
  /** Recorder-level `{field: reason}` — the instance-level twin of a tuner's `rejected`. */
  recorder_rejected: Record<string, string> | null;
  /** Chiefly `restart_on`: which recorder-level fields cost the process a restart. */
  recorder_capabilities: { restart_on?: string[] | null; [key: string]: unknown } | null;
}

/** One archived day of a recorder's raw event log (`GET /admin/event-logs`). */
export interface EventLogArchive extends Concrete<Schemas["EventLogArchiveOut"]> {}

/** The body of `PUT /admin/recorders/{id}/tuners/{key}`. */
export type TunerDesiredInput = Schemas["TunerDesiredIn"];

/**
 * What a recorder is *for* when several of them hear one site. `hot` records whenever it is
 * healthy; `warm` sits registered with its tuners disabled and may be switched on by the
 * arbiter when every hot recorder for a site goes quiet; `cold` is never touched
 * automatically — a bench box, whatever it happens to be doing.
 */
export type RecorderStandby = NonNullable<Schemas["RecorderPolicyIn"]["standby"]>;

export const RECORDER_STANDBY: readonly RecorderStandby[] = ["hot", "warm", "cold"] as const;

/** The body of `PUT /admin/recorders/{id}` — the recorder's own policy, not a tuner's. */
export type RecorderPolicyInput = Omit<Schemas["RecorderPolicyIn"], "link"> & {
  /**
   * The link block, or an explicit `null` to stop managing it. Narrowed from the generated
   * `Record<string, unknown>` so a typo in a field name is a compile error here rather than
   * a 422 the operator finds out about.
   */
  link?: RecorderLinkInput | null;
};

/** `GET /admin/recorders/{id}/link/key` — the one read that returns the shared secret. */
export type RecorderLinkKey = Concrete<Schemas["RecorderLinkKeyOut"]>;

/**
 * What one receiver said about how well it heard a call (`calls.source_quality`). The
 * recorder may send more than this; these are the fields keyup scores and renders.
 */
export interface CallSourceQuality {
  instance?: string | null;
  voice_frames?: number | null;
  bad_frames?: number | null;
  rssi_dbfs?: number | null;
  [key: string]: unknown;
}

/**
 * One live FFT frame for a tuner, streamed over `GET /admin/recorders/spectrum/stream`.
 * `db` is exactly `bins` signed bytes (−128..127), one quantized dBFS value per frequency
 * bin across `center_hz ± span_hz/2`. Ephemeral: the server never stores it.
 */
export interface SpectrumFrame {
  instance_id: string;
  tuner_key: string;
  ts_ms: number;
  center_hz: number;
  span_hz: number;
  bins: number;
  db: number[];
}

/* --------------------------------------------- outbound exporters (§4.7) */

/**
 * `GET /admin/exports` — Broadcastify Calls and OpenMHz. Both ship **off** and
 * the UI must never be the thing that turns one on by accident: the switch is
 * behind a confirm dialog because two uploaders on one system post every call
 * twice (docs/EXPORTERS.md).
 */
export type ExportServiceStatus = Concrete<Schemas["ExportServiceStatus"]>;
export type ExportLedgerRow = Concrete<Schemas["ExportLedgerRow"]>;

/**
 * The Broadcastify **Feed** — the live audio stream, which is a different product
 * from Broadcastify Calls above and carries different credentials. It has no
 * ledger: a stream has no per-call outcome to record and nothing to retry, so what
 * it reports is the state of one Icecast source connection. The mount password is
 * never part of this shape.
 */
export type BroadcastifyFeedStatus = Concrete<Schemas["BroadcastifyFeedStatus"]>;
export interface ExportStatus
  extends Omit<Concrete<Schemas["ExportStatusOut"]>, "services" | "recent" | "broadcastify_feed"> {
  services: ExportServiceStatus[];
  recent: ExportLedgerRow[];
  broadcastify_feed: BroadcastifyFeedStatus;
}
export type ExportTestResult = Concrete<Schemas["ExportTestResult"]>;

/**
 * The **unsaved** values a test button sends with it. The point of pressing Test is to
 * find out whether the key just typed in is right, so the endpoint takes the card's
 * draft, applies it for that one request and never stores it.
 */
export type ExportTestRequest = Schemas["ExportTestRequest"];

/** One system Broadcastify Calls may publish — `GET /admin/exports`'s `trunked_systems`. */
export type TrunkedSystem = Concrete<Schemas["TrunkedSystem"]>;

/** The two services the ledger knows, in the order the Exports tab shows them. */
export const EXPORT_SERVICES = ["broadcastify", "openmhz"] as const;
export type ExportService = (typeof EXPORT_SERVICES)[number];

/** How a service picks talkgroups: see `export_<service>_talkgroup_mode`. */
export const EXPORT_TALKGROUP_MODES = ["all", "allow", "deny"] as const;
export type ExportTalkgroupMode = (typeof EXPORT_TALKGROUP_MODES)[number];

/* ------------------------------- Broadcastify feed statistics (docs/BCFY_STATS.md) */

/**
 * `GET /admin/bcfy-stats/summary` — the operator's own **live feed**, which is a
 * different Broadcastify product from the Calls exporter above and carries a
 * different credential (a site login, not an API key).
 *
 * Both `*_hour_of_week` matrices are 7x24 with **Monday first**, cut on the local
 * clock, and built from the same hourly buckets — which is what makes putting them
 * side by side, and correlating them, mean anything. `correlation` is `null` rather
 * than `0` when there is not enough history to say.
 *
 * The two matrices do **not** cover the same history and the page must not imply
 * they do. Calls go back as far as this archive does; listeners begin at the first
 * poll (`first_sample_at`), because the owner API answers with one instant's count
 * and carries no history to backfill — plus at most the 24 hours the *public* feed
 * page's sparkline can reconstruct, which `hours_from_page` counts. A listener cell
 * with neither is `null`, never `0` — see `listeners_hours_covered` against
 * `window_hours`.
 *
 * …**and then there is the import.** The feed dashboard's two CSV exports carry about
 * a week of hours (`hours_from_import`) and about 180 days of days (`daily`), which is
 * the only listener history that reaches back months. So the page has two starting
 * dates and says both: `history_from` is where *any* history begins and
 * `first_sample_at` is where the *hourly* history begins, and after an import they are
 * usually months apart.
 */
export interface FeedSummary
  extends Omit<
    Concrete<Schemas["FeedSummary"]>,
    "hottest_hours" | "quietest_hours" | "quiet_windows" | "daily"
  > {
  hottest_hours: HotHour[];
  quietest_hours: HotHour[];
  quiet_windows: QuietWindow[];
  daily: FeedDailyPoint[];
}
/** One UTC day of the dashboard's daily export: its uptime, and its peak listeners. */
export type FeedDailyPoint = Concrete<Schemas["DailyPoint"]>;
/**
 * What one dashboard CSV import parsed, wrote and refused.
 *
 * `rows_written` is smaller than `rows_parsed` exactly to the extent this deployment had
 * already **polled** those hours: an import never overwrites a poll. `skipped_reasons`
 * counts per reason rather than per row, so a file that is entirely the wrong thing says
 * so in one line.
 */
export type FeedImportReport = Concrete<Schemas["ImportReport"]>;
export type HotHour = Concrete<Schemas["HotHour"]>;
/** A run of quiet hours long enough to restart the feed in. See `quietest_basis`. */
export type QuietWindow = Concrete<Schemas["QuietWindow"]>;
export type FeedSamplePoint = Concrete<Schemas["SamplePoint"]>;
export type FeedArchive = Concrete<Schemas["ArchiveOut"]>;
/** What one live `a=feed` request returned. `unmapped_fields` is the interesting half. */
export type FeedProbeResult = Concrete<Schemas["ProbeResult"]>;
/**
 * What one fetch of the **public** feed page said, and how much of it was new.
 *
 * `bars` is the page's 24-hour hourly sparkline, oldest first — the only listener
 * history Broadcastify publishes anywhere. `hours_written` is smaller than
 * `bars.length` exactly to the extent this deployment had already polled those hours:
 * a page value never overwrites a poll.
 */
export type FeedPublicPageResult = Concrete<Schemas["PublicPageResult"]>;

/** Row labels for both heatmaps, in the order the matrices are indexed. */
export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;

/* ------------------------------------------------ band occupancy (§4.8) */

export type OccupancyBand = Concrete<Schemas["OccupancyBand"]>;
export type OccupancyChannel = Concrete<Schemas["OccupancyChannel"]>;
export interface SiteOccupancy extends Omit<Concrete<Schemas["SiteOccupancy"]>, "bands" | "channels"> {
  bands: OccupancyBand[];
  channels: OccupancyChannel[];
}

/* ---------------------------------------------------- site channel audit */

export type ChannelAuditSite = Concrete<Schemas["ChannelAuditSite"]>;
export type ChannelAuditControl = Concrete<Schemas["ChannelAuditControl"]>;
export type ChannelAuditUndocumented = Concrete<Schemas["ChannelAuditUndocumented"]>;
export type ChannelAuditMissing = Concrete<Schemas["ChannelAuditMissing"]>;
export type ChannelAuditDrift = Concrete<Schemas["ChannelAuditDrift"]>;
export type ChannelAuditRecorderConfig = Concrete<Schemas["ChannelAuditRecorderConfig"]>;
export type ChannelAuditAdjacent = Concrete<Schemas["ChannelAuditAdjacent"]>;

/**
 * One row of `GET /directory/sites/{id}/frequencies` — a catalogued frequency and
 * its two export checkboxes, which decide whether *conventional* calls heard on it
 * (calls with no talkgroup) may be published.
 */
export type SiteFrequency = Concrete<Schemas["SiteFrequencyOut"]>;
export type SiteFrequencyPatch = Schemas["SiteFrequencyPatch"];

/** One `mfid:opcode` a site's control channel has ever transmitted, and what we call it. */
export type SiteMessageKind = Concrete<Schemas["SiteMessageKindOut"]>;
export type SiteMessageKindPatch = Schemas["SiteMessageKindPatch"];

/* ------------------------------------------------ the site profile (§3.5) */

export type SiteProfileObserver = Concrete<Schemas["SiteProfileObserver"]>;
export type SiteProfileIdentity = Concrete<Schemas["SiteProfileIdentity"]>;
export type SiteProfileChannel = Concrete<Schemas["SiteProfileChannel"]>;
export type SiteProfileBand = Concrete<Schemas["SiteProfileBand"]>;
export type SiteProfileTraffic = Concrete<Schemas["SiteProfileTraffic"]>;
export type SiteProfileAdjacent = Concrete<Schemas["SiteProfileAdjacent"]>;
export type SiteProfileDataChannel = Concrete<Schemas["SiteProfileDataChannel"]>;
export type SiteProfileMessageKinds = Concrete<Schemas["SiteProfileMessageKinds"]>;

export interface SiteProfileControl
  extends Omit<Concrete<Schemas["SiteProfileControl"]>, "secondary_control"> {
  secondary_control: SiteProfileChannel[];
}

export type SiteProfileServices = Concrete<Schemas["SiteProfileServices"]>;

/**
 * `GET /sites/{id}/profile` — everything known about one tower site, and (in
 * `unknowns`) everything that is not.
 *
 * `Concrete` is applied to the nested objects as well as the envelope for the
 * same reason it is applied at all: every field of every section is serialized,
 * so a list that defaults to `[]` arrives as `[]` rather than `undefined`. The
 * one thing that is genuinely nullable and means it is `services.available` —
 * null there is "nobody has decoded the bitmap", which is not "offers nothing".
 */
export interface SiteProfile
  extends Omit<
    Concrete<Schemas["SiteProfileOut"]>,
    | "observed_by"
    | "identity"
    | "control"
    | "services"
    | "bands"
    | "traffic"
    | "adjacent_sites"
    | "data_channel"
    | "message_kinds"
  > {
  observed_by: SiteProfileObserver[];
  identity: SiteProfileIdentity;
  control: SiteProfileControl;
  services: SiteProfileServices;
  bands: SiteProfileBand[];
  traffic: SiteProfileTraffic[];
  adjacent_sites: SiteProfileAdjacent[];
  data_channel: SiteProfileDataChannel | null;
  message_kinds: SiteProfileMessageKinds;
}

/** Which of the four things a frequency is doing at a site. */
export type SiteFrequencyRole = Schemas["ChannelAuditUndocumented"]["role"];

/**
 * `GET /directory/sites/{id}/channel-audit` — what the site is on the air, what
 * RadioReference documents, what the recorder hunts, and every difference
 * between the three (docs/CHANNEL_AUDIT.md).
 *
 * `Concrete` is applied to the nested objects as well as the envelope, for the
 * same reason it is applied at all: the response model serializes every field of
 * every one of them, so a list that defaults to `[]` arrives as `[]`, not as
 * `undefined`.
 */
export interface SiteChannelAudit
  extends Omit<
    Concrete<Schemas["SiteChannelAudit"]>,
    "site" | "control" | "undocumented" | "missing" | "control_drift" | "recorder_config" | "adjacent_sites"
  > {
  site: ChannelAuditSite;
  control: ChannelAuditControl;
  undocumented: ChannelAuditUndocumented[];
  missing: ChannelAuditMissing[];
  control_drift: ChannelAuditDrift[];
  recorder_config: ChannelAuditRecorderConfig;
  adjacent_sites: ChannelAuditAdjacent[];
}

export type SystemChannelAuditRow = Concrete<Schemas["SystemChannelAuditRow"]>;

/** `GET /directory/systems/{id}/channel-audit` — one row per site. */
export interface SystemChannelAudit
  extends Omit<Concrete<Schemas["SystemChannelAudit"]>, "system" | "sites"> {
  system: Concrete<Schemas["SystemChannelAuditSystem"]>;
  sites: SystemChannelAuditRow[];
}

export interface Talkgroup extends Omit<Concrete<Schemas["TalkgroupOut"]>, "category"> {
  /** Free text on the wire; see `TALKGROUP_CATEGORIES`. */
  category: string | null;
}

/** `GET /talkgroups/{id}` — the row plus the counts the detail header shows. */
export interface TalkgroupDetail extends Omit<Concrete<Schemas["TalkgroupDetail"]>, "category"> {
  category: string | null;
}

/** What `PATCH /talkgroups/{id}` accepts — every field optional. */
export type TalkgroupPatch = Schemas["TalkgroupPatch"];

export interface Radio extends Omit<Concrete<Schemas["RadioOut"]>, "current_unit" | "current_person"> {
  /** Resolved from the current assignment by the endpoint; null when unassigned. */
  current_unit: UnitRef | null;
  current_person: PersonRef | null;
}

/**
 * The detail also carries presence and the last 50 control-channel events
 * (docs/RADIO_EVENTS.md). `Concrete` only reaches the top level, so the two
 * nested shapes are re-declared here — otherwise `presence.affiliations` would
 * be `… | undefined` for a field the server always sends.
 */
export interface RadioDetail
  extends Omit<Concrete<Schemas["RadioDetail"]>, "presence" | "events" | "current_unit" | "current_person"> {
  presence: RadioPresence;
  events: RadioEvent[];
  current_unit: UnitRef | null;
  current_person: PersonRef | null;
}

export interface RadioPresence extends Omit<Concrete<Schemas["RadioPresence"]>, "affiliations"> {
  affiliations: RadioPresenceAffiliation[];
}

export interface RadioPresenceAffiliation extends Concrete<Schemas["RadioPresenceAffiliation"]> {}

/** One control-channel message about a radio: registration, affiliation, data. */
export interface RadioEvent extends Concrete<Schemas["RadioEventOut"]> {}

export type RadioEventKind = NonNullable<Schemas["RadioEventOut"]["kind"]>;

/** What backs an affiliation row: heard talking, or the control channel saying so. */
export type RadioAffiliationSource = NonNullable<Schemas["RadioPresenceAffiliation"]["source"]>;

export type RadioPatch = Schemas["RadioPatch"];

export interface Patch extends Concrete<Schemas["PatchOut"]> {}

export interface RadioAffiliation extends Concrete<Schemas["RadioAffiliationOut"]> {}

export interface Agency extends Concrete<Schemas["AgencyOut"]> {}

/** Where an agency row came from: the seeded agency list, or somebody adding it here. */
export type AgencySource = NonNullable<Schemas["AgencyOut"]["source"]>;

/** One agency as the Admin page edits it: no id lists, but the counts a delete checks. */
export interface AdminAgency extends Concrete<Schemas["AdminAgencyOut"]> {}

export type AgencyIn = Schemas["AgencyIn"];
export type AgencyPatch = Schemas["AgencyPatchIn"];

/**
 * The agency kinds. Mirrors `AGENCY_KINDS` in
 * `backend/app/models.py`; `GET /directory/vocabulary` carries what each one means.
 */
export const AGENCY_KINDS = ["dispatch", "police", "sheriff", "university", "fire", "ems"] as const;
export type AgencyKind = (typeof AGENCY_KINDS)[number];

/**
 * The pattern the API validates a new agency code against, as a string rather
 * than a literal so the dialog's message and the API's rule cannot disagree.
 * Mirrors `AGENCY_CODE_PATTERN` in `backend/app/models.py`.
 */
export const AGENCY_CODE_RE = /^[A-Z0-9_-]{2,16}$/;

/* ------------------------------------------------------- field vocabularies */

export interface CategoryTerm extends Concrete<Schemas["CategoryTerm"]> {}
export interface ServiceTagTerm extends Concrete<Schemas["ServiceTagTerm"]> {}
export interface VocabularyTerm extends Concrete<Schemas["VocabularyTerm"]> {}

/**
 * What every category, service tag and agency kind means, straight from the API.
 *
 * The list members are spelled out rather than taken from `VocabularyOut`:
 * `Concrete` only reaches the top level, so the generated element types keep
 * their optional `service_tags` and every consumer would have to guard a field
 * the server always sends.
 */
export interface Vocabulary {
  categories: CategoryTerm[];
  category_effect: string;
  service_tags: ServiceTagTerm[];
  agency_kinds: VocabularyTerm[];
  agency_sources: VocabularyTerm[];
}

/* ----------------------------------------------------------------- roster */

/** Enough of a unit or a person to render a label and link to the record. */
export interface UnitRef extends Concrete<Schemas["UnitRef"]> {}
export interface PersonRef extends Concrete<Schemas["PersonRef"]> {}

/** What a callsign names: an engine, an ambulance, a station, a crew. */
export interface Unit extends Concrete<Schemas["UnitOut"]> {}

export interface UnitDetail extends Omit<Concrete<Schemas["UnitDetail"]>, "radios"> {
  radios: AssignedRadio[];
}

export type UnitKind = NonNullable<Schemas["UnitOut"]["kind"]>;

/** The runtime list behind the kind picker; `types.test.ts` keeps it honest. */
export const UNIT_KINDS = ["vehicle", "station", "team", "other"] as const satisfies readonly UnitKind[];

export type UnitInput = Schemas["UnitIn"];
export type UnitPatch = Schemas["UnitPatch"];

/** One person on the roster. */
export interface Person extends Concrete<Schemas["PersonOut"]> {}

export interface PersonDetail extends Omit<Concrete<Schemas["PersonDetail"]>, "radios"> {
  radios: AssignedRadio[];
}

export type PersonInput = Schemas["PersonIn"];
export type PersonPatch = Schemas["PersonPatch"];

/**
 * The most rows one `POST /{talkgroups,radios,units,personnel}/bulk` will take.
 * The server refuses more with a 422 (`BULK_MAX_IDS` in `backend/app/schemas.py`);
 * a `maxItems` on a list does not reach `generated.ts`, so the number is mirrored
 * rather than derived — the same arrangement as the talkgroup priority bounds.
 */
export const BULK_MAX_IDS = 500;

/**
 * The three per-talkgroup export checkboxes, and the pairs the copy action moves
 * between. Broadcastify sells two products off one account and they are chosen
 * separately: `export_broadcastify` is the live **Feed**, `export_bcfy_calls` the
 * per-call **Calls** archive.
 */
export const TALKGROUP_EXPORT_FLAGS = ["export_broadcastify", "export_bcfy_calls", "export_openmhz"] as const;
export type TalkgroupExportFlag = (typeof TALKGROUP_EXPORT_FLAGS)[number];

/** The label each flag is shown under, everywhere one is named. */
export const TALKGROUP_EXPORT_FLAG_LABELS: Record<TalkgroupExportFlag, string> = {
  export_broadcastify: "Broadcastify Feed",
  export_bcfy_calls: "Broadcastify Calls",
  export_openmhz: "OpenMHz",
};

/** What `POST /admin/talkgroups/export-flags/copy` answers with. */
export type TalkgroupExportFlagCopyResult = Concrete<Schemas["TalkgroupExportFlagCopyResult"]>;

/** `{updated, items}` — what every bulk endpoint answers with. */
export type TalkgroupBulkResult = Concrete<Schemas["TalkgroupBulkResult"]>;
export type RadioBulkResult = Concrete<Schemas["RadioBulkResult"]>;
export type UnitBulkResult = Concrete<Schemas["UnitBulkResult"]>;
export type PersonBulkResult = Concrete<Schemas["PersonBulkResult"]>;

/** A radio a unit or a person holds right now, with the assignment that says so. */
export interface AssignedRadio extends Concrete<Schemas["AssignedRadioOut"]> {}

/** One row of a radio's assignment history. */
export interface RadioAssignment extends Omit<Concrete<Schemas["RadioAssignmentOut"]>, "unit" | "person"> {
  unit: UnitRef | null;
  person: PersonRef | null;
}

export type AssignmentKind = NonNullable<Schemas["RadioAssignmentOut"]["kind"]>;
export type RadioAssignmentInput = Schemas["RadioAssignmentIn"];

/* ------------------------------------------------------------------ calls */

/** One row of the calls table / live feed. */
export interface Call extends Omit<Concrete<Schemas["CallOut"]>, "category"> {
  category: string | null;
}

export interface RawMetadata extends Concrete<Schemas["RawMetadata"]> {}

/**
 * One radio keying up inside one call. A trunked call is a short conversation and
 * trunk-recorder's sidecar says who spoke when (§1.7); an sdrtrunk call has exactly
 * one of these, at offset 0, because that is all its files can say.
 */
export interface CallSource extends Concrete<Schemas["CallSourceOut"]> {}

export interface TranscriptSegment extends Concrete<Schemas["TranscriptSegmentOut"]> {}

export interface Transcript extends Omit<Concrete<Schemas["TranscriptOut"]>, "segments"> {
  segments: TranscriptSegment[];
}

export interface ExternalLink extends Concrete<Schemas["ExternalLinkOut"]> {}

/**
 * `GET /calls/{id}` — the row plus everything the detail page needs.
 *
 * `conversation` is the conversation *record*, not its calls; the ids of the
 * calls in it arrive as `conversation_call_ids`, and the thread itself comes from
 * `GET /conversations/{id}` (`useConversation`).
 */
export interface CallDetail
  extends Omit<
    Concrete<Schemas["CallDetail"]>,
    | "category"
    | "raw"
    | "sources"
    | "transcript"
    | "conversation"
    | "links"
    | "system"
    | "site"
    | "talkgroup"
    | "radio"
    | "patch"
    | "duplicates"
    | "source_quality"
  > {
  category: string | null;
  /** What the receiver that recorded this said about hearing it; null when it said nothing. */
  source_quality: CallSourceQuality | null;
  /** The other recorders' copies of this transmission, scored (docs/RECONCILIATION.md). */
  duplicates: CallCopy[];
  raw: RawMetadata;
  sources: CallSource[];
  transcript: Transcript | null;
  conversation: Conversation | null;
  links: ExternalLink[];
  system: SystemInfo | null;
  site: SiteInfo | null;
  talkgroup: Talkgroup | null;
  radio: Radio | null;
  patch: Patch | null;
}

/** One other recorder's copy of the same transmission, with the score it was given. */
export interface CallCopy extends Concrete<Schemas["CallCopyOut"]> {}

export type CallPatch = Schemas["CallPatch"];

/** `POST /calls/{id}/transcribe` — the queue accepted (or already had) the job. */
export interface TranscribeAccepted extends Concrete<Schemas["TranscribeAccepted"]> {}

export type TranscribeRequest = Schemas["TranscribeRequest"];

/** `GET /calls/{id}/waveform` — normalized 0..1 peaks plus the length they cover. */
export interface Waveform extends Concrete<Schemas["WaveformOut"]> {}

export interface Conversation extends Concrete<Schemas["ConversationOut"]> {}

/** `GET /conversations/{id}` — the conversation and its calls, oldest first. */
export interface ConversationThread extends Omit<Concrete<Schemas["ConversationThread"]>, "calls"> {
  calls: Call[];
}

/* ---------------------------------------------------------------- filters */

/** Every query key the shared call filter accepts, straight from the schema. */
export type CallFilterKey = keyof NonNullable<operations["list_calls_api_calls_get"]["parameters"]["query"]>;

/**
 * The filter the UI sends to `GET /calls`, `/calls/facets`, `/calls/export`,
 * `/search`, `/stats/*` and the live feed.
 *
 * The API takes repeated values for most of these; the UI only ever sends one
 * `system`, `site`, `call_type`, `patch` or `conversation`, and `client.ts`
 * serializes a bare value and an array the same way. `types.test.ts` checks every
 * key here is a key the API knows.
 */
export interface CallFilterParams {
  q?: string;
  /** Widen `q` to the spelling variants the archive's transcripts contain. */
  fuzzy?: boolean;
  /**
   * Explicit call ids — the rows a reader ticked. Narrows whatever the other filters
   * already match rather than replacing them, and the API caps it at 1000 (422 over).
   */
  ids?: number[];
  date_from?: string;
  date_to?: string;
  /** Time-of-day window, `HH:MM`, wrapping past midnight when `from > to`. */
  time_from?: string;
  time_to?: string;
  dow?: number[];
  system?: number;
  site?: number;
  talkgroup?: number[];
  radio?: number[];
  unit?: string[];
  agency?: string[];
  category?: string[];
  call_type?: CallType;
  kind?: CallKind[];
  /** Only calls a recorder flagged as an emergency; an sdrtrunk file carries no flag. */
  emergency?: boolean;
  encrypted?: boolean;
  /** Only calls the audio pass found a sustained tone in — a page, an alert, a warble. */
  tone_detected?: boolean;
  /** Only pages: a two-tone or alert pattern in the call's first seconds. Not `emergency`. */
  alert?: boolean;
  /** Only calls with their recording on hand (`true`), or only the metadata-only rows (`false`). */
  has_audio?: boolean;
  audio_type?: string[];
  min_duration?: number;
  max_duration?: number;
  transcript_status?: TranscriptStatus[];
  quality?: TranscriptQuality[];
  /** How the call reached the archive; a call found on disk carries no class at all. */
  priority?: IngestPriorityClass[];
  tag?: string[];
  starred?: boolean;
  patch?: number;
  conversation?: number;
  include_duplicates?: boolean;
  sort?: string;
  page?: number;
  page_size?: number;
  cursor?: string;
}

export interface FacetBucket extends Concrete<Schemas["FacetBucket"]> {}

/** `GET /calls/facets` — counts for the current filter. */
export interface Facets
  extends Omit<Concrete<Schemas["FacetsOut"]>, keyof FacetDimensions>,
    FacetDimensions {}

interface FacetDimensions {
  talkgroup: FacetBucket[];
  radio: FacetBucket[];
  agency: FacetBucket[];
  category: FacetBucket[];
  /** 24 buckets, `key` = `"00".."23"`. */
  hour: FacetBucket[];
  /** 7 buckets, `key` = `"0".."6"`, Sunday first. */
  dow: FacetBucket[];
  transcript_status: FacetBucket[];
  quality: FacetBucket[];
  tag: FacetBucket[];
}

/* ----------------------------------------------------------------- search */

export interface SearchHit extends Omit<Concrete<Schemas["SearchHit"]>, "call"> {
  call: Call;
}

export interface SearchResponse
  extends Omit<Concrete<Schemas["SearchResponse"]>, "items" | "facets" | "fuzzy"> {
  items: SearchHit[];
  /** Only computed when the request asked for it (`facets=1`). */
  facets: Facets | null;
  /** Present only when spelling variants were looked up (`fuzzy`, or a `fallback` that fired). */
  fuzzy: FuzzySearch | null;
}

/** What a fuzzy search ran: each query word (stemmed, as the index keeps it) and its variants. */
export interface FuzzySearch extends Omit<Concrete<Schemas["FuzzyOut"]>, "terms"> {
  terms: Concrete<Schemas["FuzzyTermOut"]>[];
}

export interface Suggestion extends Concrete<Schemas["Suggestion"]> {}

export interface SavedSearch extends Concrete<Schemas["SavedSearchOut"]> {}

/* ------------------------------------------------------------------ feeds */

/**
 * A saved call filter published as RSS and Atom. `rss_url` and `atom_url` contain the
 * feed's token, which is the **whole** credential — anybody holding one of those URLs can
 * read every call the filter matches — so never put one in a link the page shares, a
 * screenshot, or an analytics event.
 */
export interface SyndicationFeed extends Concrete<Schemas["FeedOut"]> {}

export interface SyndicationFeedInput {
  name: string;
  /** A call filter object, the shape `toApiParams` produces. */
  query: Record<string, unknown>;
  enabled?: boolean;
  include_audio?: boolean;
  max_items?: number;
}

export interface SyndicationFeedPatch {
  name?: string;
  query?: Record<string, unknown>;
  enabled?: boolean;
  include_audio?: boolean;
  max_items?: number;
}

/**
 * A built-in feed: a fixed filter — everything, or one talkgroup category — at a URL that
 * holds no secret. There is nothing to rotate and nothing to leak, so unlike a
 * `SyndicationFeed` these URLs are safe to hand out; what they publish is governed by the
 * archive's own read policy.
 */
export interface FeedPreset extends Concrete<Schemas["FeedPresetOut"]> {}

/**
 * One of the busiest channels inside a built-in feed, over the last seven days. The Feeds
 * page prints a few of these so a reader can tell what a category feed actually carries
 * rather than only how many talkgroups are filed under it.
 */
export interface FeedPresetTalkgroup extends Concrete<Schemas["FeedPresetTalkgroup"]> {}

/* ------------------------------------------------------------------- live */

export type LiveEvent =
  | { type: "call.new"; call: Call }
  | { type: "call.transcribed"; call: Call }
  | { type: "call.updated"; call: Call }
  /**
   * Reconciliation demoted a call that was the primary copy: `id` is the row a live
   * list may be showing, `by` the copy that took its place (which arrives on its own
   * `call.new`). No call object — the demoted row no longer passes the call filters,
   * so there is nothing to attach and nothing to re-read. Drop the row (2026-09-10).
   */
  | { type: "call.superseded"; id: number; by: number | null }
  | { type: "ingest.status"; status: IngestStatus };

export type ConnectionState = "connecting" | "open" | "closed" | "mock";

/* ------------------------------------------------------------------ stats */

export interface StatsOverview extends Concrete<Schemas["StatsOverview"]> {}

/**
 * `GET /stats/activity`. `cells` is the long form; `buckets`/`rows`/`matrix` is
 * the same data pivoted for the heatmap, which is what the UI draws.
 */
export interface ActivityResponse extends Omit<Concrete<Schemas["ActivityResponse"]>, "cells"> {
  cells: ActivityCell[];
}

export interface ActivityCell extends Concrete<Schemas["ActivityCell"]> {}

/* -------------------------------------------------------- activity log */

export type ActivityLevel = "info" | "warn" | "error";
export type ActivitySource =
  | "ingest"
  | "upload"
  | "transcribe"
  /** The STT engine misbehaving, as distinct from the work `transcribe` reports:
   *  `stt.hallucination` is a transcript discarded before it reached a call. */
  | "stt"
  | "export"
  | "rr"
  | "import"
  | "system"
  /** The standby arbiter (docs/RECORDERS.md): `recorder.promoted` / `recorder.demoted`. */
  | "arbiter";

/**
 * One row of the operational activity log (`GET /api/activity`, Admin → Activity).
 * A hand-written type: the endpoint returns plain rows, not an OpenAPI schema shape.
 */
export interface ActivityEvent {
  id: number;
  at: string;
  source: string;
  level: ActivityLevel | string;
  kind: string;
  message: string;
  call_id: number | null;
  meta: Record<string, unknown> | null;
}

/* ------------------------------------------------------- live activity */

/**
 * One counter and the same counter over the window before it. The panel draws the
 * trend arrow from the pair, so it never has to remember anything between polls.
 */
export interface ActivityWindowCount {
  current: number;
  previous: number;
}

/** Every export outcome for one service, keyed the way the ledger buckets them. */
export interface LiveExportCounts {
  uploaded: ActivityWindowCount;
  skipped_duplicate: ActivityWindowCount;
  skipped_no_speech: ActivityWindowCount;
  skipped_static: ActivityWindowCount;
  skipped_late: ActivityWindowCount;
  claimed_by_recorder: ActivityWindowCount;
  skipped_other: ActivityWindowCount;
  failed: ActivityWindowCount;
  pending: ActivityWindowCount;
  [bucket: string]: ActivityWindowCount;
}

/**
 * Where a recorder serves its own live audio monitor, as the status row needs it.
 *
 * Keyup never carries this audio: the recorder streams what it is hearing from a little
 * panel of its own, and all the row does is offer a link to it. `local_only` is computed
 * on the server — the panel binds to the loopback unless its operator said otherwise, and
 * a link to `127.0.0.1` opens nothing unless the browser is running on that machine.
 */
export interface LiveMonitor {
  enabled: boolean;
  url: string | null;
  stream_url: string | null;
  token_required: boolean;
  local_only: boolean;
}

/** One recorder instance as the panel shows it: registration, volume, heartbeat. */
export interface LiveRecorderRow {
  instance: string;
  label: string | null;
  registered: boolean;
  last_seen_at: string | null;
  seconds_since_last_seen: number | null;
  calls_in_window: number;
  last_call_at: string | null;
  seconds_since_last_call: number | null;
  /** `null` for a recorder that has never reported a panel — which is most of them. */
  monitor: LiveMonitor | null;
}

/** The counters every deployment has, whatever is configured on top of them. */
export interface LiveActivityCounters {
  ingested: ActivityWindowCount;
  duplicates_merged: ActivityWindowCount;
  speech: ActivityWindowCount;
  pages: ActivityWindowCount;
  no_speech: ActivityWindowCount;
  static: ActivityWindowCount;
  audio_processed: ActivityWindowCount;
  audio_discarded: ActivityWindowCount;
  transcribed: ActivityWindowCount;
  transcripts_rejected: ActivityWindowCount;
}

/**
 * `GET /api/status/activity` — one request behind the Live activity panel.
 * Hand-written for the same reason `ActivityEvent` is: the route answers a plain
 * object rather than a declared response model.
 */
export interface LiveActivitySnapshot {
  window_s: number;
  server_time: string;
  window_start: string;
  previous_window_start: string;
  counters: LiveActivityCounters;
  exports: Record<string, LiveExportCounts>;
  webhooks: { delivered: ActivityWindowCount; failed: ActivityWindowCount; pending: number };
  queues: { audio_queued: number; transcription_queued: number; exports_pending: number };
  recorders: LiveRecorderRow[];
  last_call_at: string | null;
  seconds_since_last_call: number | null;
  /** Operational log lines for the ticker — empty unless the caller is an administrator. */
  recent: ActivityEvent[];
  recent_visible: boolean;
}

export interface TalkgroupStat extends Concrete<Schemas["TalkgroupStat"]> {}

export interface RadioStat extends Concrete<Schemas["RadioStat"]> {}

export interface BackendThroughput extends Concrete<Schemas["BackendThroughput"]> {}

export interface TranscriptionStats
  extends Omit<Concrete<Schemas["TranscriptionStats"]>, "quality" | "status" | "backends"> {
  quality: { quality: string; count: number }[];
  status: { status: string; count: number }[];
  backends: BackendThroughput[];
}

export interface RecorderGap extends Concrete<Schemas["RecorderGapOut"]> {}

/** `GET /timeline` — per-bucket counts for a window. */
export interface TimelinePoint extends Concrete<Schemas["TimelinePoint"]> {}

export interface HourCount extends Concrete<Schemas["HourCount"]> {}

export interface LabelCount extends Concrete<Schemas["LabelCount"]> {}

export interface TopRadio extends Concrete<Schemas["TopRadio"]> {}

export interface TalkgroupStatsResponse
  extends Omit<Concrete<Schemas["TalkgroupStatsOut"]>, "hourly" | "daily" | "top_radios"> {
  hourly: HourCount[];
  daily: LabelCount[];
  top_radios: TopRadio[];
}

export interface RadioHistory extends Omit<Concrete<Schemas["RadioHistoryOut"]>, "hourly" | "talkgroups"> {
  hourly: HourCount[];
  talkgroups: LabelCount[];
}

/* ------------------------------------------------------------ operations */

export interface IngestStatus extends Concrete<Schemas["IngestStatusOut"]> {}

export interface IngestRun extends Concrete<Schemas["IngestRunOut"]> {}

export interface IngestFile extends Concrete<Schemas["IngestFileOut"]> {}

export interface AdminIngestResponse extends Omit<Concrete<Schemas["AdminIngestOut"]>, "status" | "runs"> {
  status: IngestStatus;
  runs: IngestRun[];
}

export interface TranscriptionJob extends Concrete<Schemas["TranscriptionJobOut"]> {}

/** `detail` is the health probe's own words — a model count, or why it failed. */
export interface SttBackend extends Concrete<Schemas["SttBackendOut"]> {}

/**
 * One backend as the *worker* last reported it, not as this process imagines it.
 *
 * The counters are published to `stt_backend_stats` by the transcription worker; `stale`
 * means nobody has published this row lately, which is a different thing from a backend
 * being idle and is shown as such.
 */
export interface SttBackendStat extends Concrete<Schemas["SttBackendStatOut"]> {}

export interface SttQueue extends Concrete<Schemas["SttQueueOut"]> {}

export interface SttBackendsView extends Omit<Concrete<Schemas["SttBackendsOut"]>, "backends" | "queue"> {
  backends: SttBackendStat[];
  queue: SttQueue;
}

/**
 * The body of a backend create/edit.
 *
 * `api_key` is write-only end to end: it goes up, it never comes back, and a `SttBackendStat`
 * carries only `has_api_key`. `null` on an edit means "keep the stored key" and `""` means
 * "clear it", which is why the type is nullable rather than just optional.
 */
export type SttBackendConfigInput = Schemas["SttBackendConfigIn"];

export interface SttBackendDeleteResult extends Concrete<Schemas["SttBackendDeleteOut"]> {}

export interface SttBackendProbe extends Concrete<Schemas["SttBackendProbeOut"]> {}

/* ---------------------------------------------------------- ingest sources */

/**
 * A configured place recordings come from.
 *
 * `IngestSourceRow`, not `IngestSource`: that name is already taken by the enum on a call
 * saying *how it arrived* (`file` | `upload` | …), which is a different thing entirely — a
 * fact about one recording rather than a row an operator can edit.
 */
export interface IngestSourceRow extends Concrete<Schemas["IngestSourceOut"]> {}

export type IngestSourceInput = Schemas["IngestSourceIn"];

export type IngestSourceKind = IngestSourceRow["kind"];

export interface IngestSourcesView extends Omit<Concrete<Schemas["IngestSourcesOut"]>, "sources"> {
  sources: IngestSourceRow[];
}

export interface IngestSourceStatus extends Concrete<Schemas["IngestSourceStatusOut"]> {}

export interface RetentionPolicy extends Concrete<Schemas["RetentionPolicyOut"]> {}

export type RetentionPolicyInput = Schemas["RetentionPolicyIn"];

export interface RetentionRunResult extends Concrete<Schemas["RetentionRunResult"]> {}

export interface SettingDefinition extends Concrete<Schemas["SettingDefinition"]> {}

/* --------------------------------------------- the HTTPS edge (docs/HTTPS.md) */

/** One domain's certificate, read off the wire by handshaking with the edge. */
export interface HttpsCertificate extends Concrete<Schemas["CertificateOut"]> {}

/**
 * `GET /admin/https/status` — what the edge is serving beside what the settings ask
 * for. `mode` is observed and `desired_mode` is intended; they differ while an apply
 * has failed or the edge has restarted on its environment. The Cloudflare token is
 * never in here — `has_token` says only whether one is stored.
 */
export interface HttpsStatus
  extends Omit<
    Concrete<Schemas["HttpsStatusOut"]>,
    "certificates" | "domains" | "caddy_domains" | "missing"
  > {
  certificates: HttpsCertificate[];
  domains: string[];
  caddy_domains: string[];
  missing: string[];
}

export interface HttpsApplyResult extends Omit<Concrete<Schemas["ApplyOut"]>, "domains"> {
  domains: string[];
}

export interface HttpsDnsZone extends Omit<Concrete<Schemas["TestDnsZone"]>, "permissions"> {
  permissions: string[];
}

export interface HttpsDnsTestResult extends Omit<Concrete<Schemas["TestDnsOut"]>, "zones"> {
  zones: HttpsDnsZone[];
}

/** The runtime settings the HTTPS tab owns, in the order it renders them. */
export const HTTPS_SETTING_KEYS = [
  "tls_enabled",
  "tls_domains",
  "tls_acme_email",
  "tls_challenge",
  "tls_cloudflare_api_token",
  "tls_acme_ca",
  "tls_redirect_http",
] as const;
export type HttpsSettingKey = (typeof HTTPS_SETTING_KEYS)[number];

/** `GET /admin/audio` — the audio-processing queue and what it has covered (§5.5). */
export interface AudioStatus extends Concrete<Schemas["AudioStatusOut"]> {}

export type AudioReprocessInput = Schemas["AudioReprocessIn"];

export interface AudioReprocessResult extends Concrete<Schemas["AudioReprocessOut"]> {}

export interface AudioPreview extends Concrete<Schemas["AudioPreviewOut"]> {}

/* --------------------------------------------------------- audio profiles */

/**
 * A named filter chain a talkgroup can be processed through (docs/AUDIO.md).
 *
 * `rendered` is the whole `-af` string the server built from `chain`, loudness tail
 * included — the editor shows what the API says it will run rather than assembling a
 * second, hopeful copy of the same string in TypeScript.
 */
export interface AudioProfile extends Concrete<Schemas["AudioProfileOut"]> {}

export type AudioProfileInput = Schemas["AudioProfileIn"];
export type AudioProfilePatch = Schemas["AudioProfilePatch"];

/** One filter in a chain: a whitelisted kind and the numbers it was given. */
export type AudioProfileStep = Schemas["AudioProfileStep"];

/**
 * One parameter's range, default and explanation.
 *
 * `optional` marks a parameter with no default — `afftdn.nr` and all three of `anlmdn`'s.
 * Left blank it is not passed to ffmpeg at all and the filter's own default applies, so
 * nothing in a step is ever genuinely required and the editor never blocks a save on one.
 */
export interface AudioProfileParamSpec extends Concrete<Schemas["AudioProfileParamSpec"]> {}

export interface AudioProfileStepSpec extends Omit<Concrete<Schemas["AudioProfileStepSpec"]>, "params"> {
  params: AudioProfileParamSpec[];
}

/** `GET /admin/audio/profiles/steps` — the whitelist the editor generates its form from. */
export interface AudioProfileCatalog extends Omit<Concrete<Schemas["AudioProfileCatalogOut"]>, "steps"> {
  steps: AudioProfileStepSpec[];
}

export type AudioProfilePreviewInput = Schemas["AudioProfilePreviewIn"];
export type AudioProfileApplyInput = Schemas["AudioProfileApplyIn"];

export interface AudioProfileApplyResult extends Concrete<Schemas["AudioProfileApplyOut"]> {}

/** `prefix` and `suffix` are the visible ends of the token, so a row is recognizable. */
export interface ApiTokenInfo extends Omit<Concrete<Schemas["ApiTokenInfo"]>, "scopes"> {
  scopes: string[];
}

/** Returned once, on create — the plaintext token is never shown again. */
export interface ApiTokenCreated extends Omit<Concrete<Schemas["ApiTokenCreated"]>, "scopes"> {
  scopes: string[];
}

export type ApiTokenInput = Schemas["ApiTokenIn"];

export interface User extends Concrete<Schemas["UserOut"]> {}

export type UserInput = Schemas["UserIn"];
export type UserPatch = Schemas["UserPatch"];

export interface SystemStatus extends Omit<Concrete<Schemas["SystemStatusOut"]>, "ingest"> {
  ingest: IngestStatus | null;
}

/**
 * `GET /status/lag` — seconds since the newest recording, and nothing else. Hand-written
 * rather than generated: the route is newer than `generated.ts`.
 */
export interface IngestLag {
  ingest_lag_s: number | null;
  last_recorded_at: string | null;
  server_time: string;
}

/** One watched filesystem: free space, its alert level and the days-to-full estimate. */
export type DiskStatus = Schemas["DiskStatusOut"];
export type HealthCondition = Schemas["HealthConditionOut"];

export interface ImportResult extends Concrete<Schemas["ImportResult"]> {}

/* ----------------------------------------------------------------- alerts */

export interface AlertRule extends Omit<Concrete<Schemas["AlertRuleOut"]>, "channels"> {
  channels: string[];
}

export type AlertRuleInput = Schemas["AlertRuleIn"];

export interface NotificationRecord extends Concrete<Schemas["NotificationOut"]> {}

/* ------------------------------------------------------------------- auth */

export interface MeResponse extends Omit<Concrete<Schemas["MeResponse"]>, "user"> {
  user: User | null;
}

export type LoginRequest = Schemas["LoginRequest"];

/* ---------------------------------------------------------------- exports */

export interface ExportJob extends Concrete<Schemas["ExportJobOut"]> {}

/* ------------------------------------------------------------ favorites */

export interface Favorite extends Concrete<Schemas["FavoriteOut"]> {}
