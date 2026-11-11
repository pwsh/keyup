/**
 * The in-memory fake corpus behind `VITE_MOCK=1`.
 *
 * ~600 calls over the last two days across 20 talkgroups on one P25 system,
 * shaped like the real sdrtrunk corpus documented in ARCHITECTURE §1: a median
 * call around two seconds, a long tail to ~45 s, 14 % squelch tails under a
 * second, patches whose member set is what identifies them, a small share of
 * unresolved radios, and transcripts whose quality is mostly `good` with a
 * realistic sprinkling of `suspect` and `noise`.
 *
 * Everything is generated from one seeded PRNG so the fixture is identical on
 * every reload — screenshots, tests and the demo all see the same archive.
 */
import { talkgroupLabel } from "../../lib/format";
import type {
  ActivityCell,
  ActivityEvent,
  ActivityResponse,
  AdminAgency,
  Agency,
  AlertRule,
  ApiTokenInfo,
  AudioProfile,
  AudioProfileCatalog,
  AudioStatus,
  Call,
  CallSource,
  CallSourceQuality,
  Conversation,
  EventLogArchive,
  ExportStatus,
  ExternalLink,
  FeedArchive,
  FeedDailyPoint,
  FeedSamplePoint,
  FeedSummary,
  HttpsStatus,
  IngestFile,
  IngestRun,
  IngestSourceRow,
  LiveActivitySnapshot,
  NotificationRecord,
  Patch,
  Person,
  Radio,
  RadioAffiliation,
  RadioAssignment,
  RadioEvent,
  RawMetadata,
  Recorder,
  RecorderGap,
  RetentionPolicy,
  RrServiceTag,
  SavedSearch,
  SettingDefinition,
  SiteChannelAudit,
  SiteFrequency,
  SiteInfo,
  SiteMessageKind,
  SiteOccupancy,
  SiteProfile,
  SttBackend,
  SttBackendStat,
  SttQueue,
  SystemChannelAudit,
  SystemInfo,
  Talkgroup,
  TalkgroupCategory,
  Transcript,
  TranscriptionJob,
  TranscriptQuality,
  TranscriptSegment,
  TranscriptStatus,
  Unit,
  User,
  Vocabulary,
} from "../types";
import { RR_SERVICE_TAGS, TALKGROUP_CATEGORIES } from "../types";

/* ------------------------------------------------------------------- prng */

/** mulberry32 — small, fast, and identical across runs for a given seed. */
function makeRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rnd = makeRandom(20260829);
const pick = <T>(items: readonly T[]): T => items[Math.floor(rnd() * items.length)] as T;
const between = (low: number, high: number): number => low + rnd() * (high - low);
const intBetween = (low: number, high: number): number => Math.floor(between(low, high + 1));
const chance = (probability: number): boolean => rnd() < probability;

const iso = (ms: number): string => new Date(ms).toISOString();

/** Naive America/Denver, the shape CAD systems store (`2026-08-29T14:03:07`). */
const localIso = (ms: number): string => {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Denver",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(new Date(ms));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}:${get("second")}`;
};

/* --------------------------------------------------------------- directory */

export const SYSTEM: SystemInfo = {
  id: 1,
  name: "Example Network",
  // WACN and system id are numbers, not the hex strings sdrtrunk prints.
  wacn: 0xbee00,
  system_id: 39,
  protocol: "P25 Phase 1",
  // Nobody has said either way, so `is_trunked` is the guess from the protocol — which is
  // what the Exports page reads to decide whether Broadcastify Calls has anything to send.
  trunked: null,
  is_trunked: true,
  description: "Example statewide network — Example County simulcast",
  rr_system_id: 7804,
  // `NOW` is declared further down, and a mock date does not need to be relative.
  rr_synced_at: "2026-08-31T04:00:00Z",
  call_count: 0,
  site_count: 2,
  // Held by everything in the archive: the only system row whose delete is refused.
  deletable: false,
  blocking: { sites: 2, talkgroups: 12, radios: 40, calls: 1 },
};

/**
 * A second system, so that development sees the case the whole "System / Site"
 * labeling exists for: it has a `North` of its own, and the archive's `North`
 * belongs to Example Network. Two rows, one name, and only the system tells them apart.
 */
export const SCHOOL_SYSTEM: SystemInfo = {
  id: 2,
  name: "MAPLE VALLEY COMMUNITY SCHOOL DISTRICT",
  wacn: 0xbee00,
  system_id: 41,
  protocol: "P25 Phase 1",
  trunked: null,
  is_trunked: true,
  description: "The school district's own system, beside the county network",
  call_count: 0,
  site_count: 1,
  rr_system_id: null,
  rr_synced_at: null,
  // Nothing but its own North hangs off it, so the merge story has somewhere to start.
  deletable: false,
  blocking: { sites: 1 },
};

/**
 * A third system that is **not** trunked, because that case has a UI of its own: it is
 * never offered to Broadcastify Calls, its talkgroups' Calls checkbox is fixed off, and
 * the copy action counts it as skipped. Nothing else in the demo hangs off it.
 */
export const FIRE_SYSTEM: SystemInfo = {
  id: 3,
  name: "Example County Fire (conventional)",
  wacn: null,
  system_id: null,
  protocol: "Conventional analog",
  trunked: null,
  is_trunked: false,
  description: "The VHF fire channels beside the trunked network",
  rr_system_id: null,
  rr_synced_at: null,
  call_count: 0,
  site_count: 0,
  deletable: true,
  blocking: {},
};

export const SYSTEMS: SystemInfo[] = [SYSTEM, SCHOOL_SYSTEM, FIRE_SYSTEM];

export const SITES: SiteInfo[] = [
  {
    id: 1,
    system_id: 1,
    system_name: SYSTEM.name,
    name: "Central",
    site_number: 1,
    description: "Example County simulcast",
    notes: null,
    call_count: 0,
    // What the control channel says about itself, and the RadioReference mapping the
    // channel audit compares against (§3.5).
    nac: 0x34,
    rfss: 10,
    lra: 10,
    rr_site_id: 9001,
    rr_synced_at: "2026-08-31T04:00:00Z",
    services_available: ["Composite", "Group Voice", "Individual Voice", "Data", "Registration"],
    // Every call in the fixture is filed here, so it can never be deleted.
    deletable: false,
    blocking: { calls: 240 },
  },
  {
    id: 2,
    system_id: 1,
    system_name: SYSTEM.name,
    name: "North",
    site_number: 2,
    description: "North County simulcast",
    notes: null,
    call_count: 0,
    nac: 0x34,
    rfss: 10,
    lra: 10,
    rr_site_id: 9002,
    rr_synced_at: "2026-08-31T04:00:00Z",
    services_available: ["Composite", "Group Voice", "Data"],
    deletable: false,
    blocking: { site_frequencies: 4 },
  },
  {
    id: 3,
    system_id: 2,
    system_name: SCHOOL_SYSTEM.name,
    name: "North",
    site_number: 1,
    description: null,
    notes: "The other North. Nothing has been filed here yet.",
    call_count: 0,
    nac: null,
    rfss: null,
    lra: null,
    rr_site_id: null,
    rr_synced_at: null,
    // Null, not `[]`: nobody has decoded a service message at this site, which is a
    // different answer from a site that offers nothing.
    services_available: null,
    // Nothing names it, so this is the one row whose Delete button is live.
    deletable: true,
    blocking: {},
  },
];

interface TgSeed {
  tgid: number;
  alias: string;
  group_name: string;
  category: TalkgroupCategory;
  /** RadioReference's finer classification; defaulted from the category below. */
  service_tag?: RrServiceTag | null;
  agency_code: string | null;
  priority: number;
  /** Relative share of the day's traffic. */
  weight: number;
  transcribe?: boolean;
  /**
   * A real corpus always has a few channels somebody has hidden. Without any, the
   * directory's "show hidden" toggle looks like it does nothing and the hidden row
   * marker is never drawn.
   */
  hidden?: boolean;
  /**
   * The audio profile this channel is processed through, by row id, or absent for the
   * global chain — which is what all but one of them are on, as in a real deployment.
   */
  audio_profile_id?: number;
}

const TG_SEEDS: TgSeed[] = [
  {
    tgid: 1101,
    alias: "EC FIRE DISPATCH",
    group_name: "County Fire",
    category: "fire",
    agency_code: "XX0010000",
    priority: 1,
    weight: 14,
  },
  {
    tgid: 1102,
    alias: "EC FIRE TAC 1",
    group_name: "County Fire",
    category: "fire",
    agency_code: "XX0010000",
    priority: 2,
    weight: 6,
  },
  {
    tgid: 1103,
    alias: "EC FIRE TAC 2",
    group_name: "County Fire",
    category: "fire",
    agency_code: "XX0010000",
    priority: 3,
    weight: 3,
  },
  {
    tgid: 48201,
    alias: "EC EMS OPS",
    group_name: "County EMS",
    category: "ems",
    agency_code: "XX0010500",
    priority: 1,
    weight: 11,
  },
  {
    tgid: 48202,
    alias: "EC EMS MED CONTROL",
    group_name: "County EMS",
    // The medevac transports call medical control from the aircraft, so this is the
    // channel with rotor noise on it and the one profile in the fixtures that is
    // actually assigned to something.
    audio_profile_id: 2,
    category: "ems",
    agency_code: "XX0010500",
    priority: 2,
    weight: 4,
  },
  {
    tgid: 50051,
    alias: "RC LAW DISPATCH",
    group_name: "River City",
    category: "law",
    agency_code: "XX0010100",
    priority: 1,
    weight: 16,
  },
  {
    tgid: 50061,
    alias: "RC LAW 2",
    group_name: "River City",
    category: "law",
    agency_code: "XX0010100",
    priority: 2,
    weight: 8,
  },
  {
    tgid: 57011,
    alias: "WEST CO LAW DISPATCH",
    group_name: "Fairview",
    category: "law",
    agency_code: "XX0010200",
    priority: 1,
    weight: 9,
  },
  {
    tgid: 57012,
    alias: "WEST CO LAW TAC",
    group_name: "Fairview",
    category: "law",
    agency_code: "XX0010200",
    priority: 2,
    weight: 4,
  },
  {
    tgid: 19282,
    alias: "DPS LAW 2D",
    group_name: "State University",
    category: "law",
    agency_code: "XX0010600",
    priority: 1,
    weight: 7,
  },
  {
    tgid: 19283,
    alias: "DPS LAW 1A",
    group_name: "State University",
    category: "law",
    agency_code: "XX0010600",
    priority: 2,
    weight: 4,
  },
  {
    tgid: 29201,
    alias: "SHUTTLE OPS",
    group_name: "State University",
    category: "transit",
    agency_code: "XX0010600",
    priority: 4,
    weight: 6,
  },
  {
    tgid: 29202,
    alias: "RC TRANSIT OPS",
    group_name: "River City",
    category: "transit",
    agency_code: "XX0010100",
    priority: 4,
    weight: 4,
  },
  {
    tgid: 24821,
    alias: "EC SO DISPATCH",
    group_name: "County Sheriff",
    category: "law",
    agency_code: "XX0010000",
    priority: 1,
    weight: 10,
  },
  {
    tgid: 24822,
    alias: "EC SO TAC",
    group_name: "County Sheriff",
    category: "law",
    agency_code: "XX0010000",
    priority: 2,
    weight: 3,
  },
  {
    tgid: 61010,
    alias: "RC PUBLIC WORKS",
    group_name: "River City",
    category: "pw",
    agency_code: "XX0010100",
    priority: 5,
    weight: 3,
  },
  {
    tgid: 61011,
    alias: "EC SECONDARY ROADS",
    group_name: "Example County",
    category: "pw",
    agency_code: "XX0010000",
    priority: 5,
    weight: 2,
  },
  {
    tgid: 70001,
    alias: "COUNTY INTEROP 1",
    group_name: "Interop",
    category: "interop",
    agency_code: null,
    priority: 3,
    weight: 2,
  },
  {
    tgid: 70002,
    alias: "STATE INTEROP 2",
    group_name: "Interop",
    category: "interop",
    agency_code: null,
    priority: 3,
    weight: 1,
  },
  {
    tgid: 80101,
    alias: "EC SCHOOLS BUS OPS",
    group_name: "Schools",
    category: "schools",
    agency_code: null,
    priority: 5,
    weight: 2,
    transcribe: false,
  },
  {
    tgid: 70003,
    alias: "STATE SYSTEM TEST",
    group_name: "Interop",
    category: "interop",
    agency_code: null,
    priority: 400,
    weight: 1,
    transcribe: false,
    hidden: true,
  },
  {
    tgid: 61012,
    alias: "RC PARKS RADIO CHECK",
    group_name: "River City",
    category: "pw",
    agency_code: "XX0010100",
    priority: 400,
    weight: 1,
    transcribe: false,
    hidden: true,
  },
];

const NOW = Date.now();
/** The corpus covers exactly the last two days, ending a minute or so ago. */
const WINDOW_END = NOW - 70_000;
const WINDOW_START = WINDOW_END - 2 * 24 * 3600 * 1000;

/**
 * What RadioReference would call each kind of channel. A seed may override it;
 * anything not named here has no service tag at all, which is the ordinary state
 * of a channel nobody has synced or classified and is worth having in the
 * fixtures.
 */
const SERVICE_TAG_BY_CATEGORY: Partial<Record<TalkgroupCategory, RrServiceTag>> = {
  fire: "Fire Dispatch",
  ems: "EMS Dispatch",
  law: "Law Dispatch",
  transit: "Transportation",
  pw: "Public Works",
  interop: "Interop",
};

export const TALKGROUPS: Talkgroup[] = TG_SEEDS.map((seed, index) => ({
  id: index + 1,
  system_id: 1,
  tgid: seed.tgid,
  alias: seed.alias,
  group_name: seed.group_name,
  description: null,
  category: seed.category,
  service_tag: seed.service_tag ?? SERVICE_TAG_BY_CATEGORY[seed.category] ?? null,
  agency_code: seed.agency_code,
  color: null,
  priority: seed.priority,
  // The radio-check channel is the one the demo turns Record off on: nobody wants the
  // audio of a parks crew keying up to test their radio, and it is the shape the discard
  // pass exists for.
  record: seed.tgid !== 61012,
  transcribe: seed.transcribe ?? true,
  transcribe_min_seconds: 0.75,
  retention_days: seed.category === "schools" ? 30 : null,
  is_hidden: seed.hidden ?? false,
  audio_profile_id: seed.audio_profile_id ?? null,
  // Both default true and both are inert: the exporters only read them when their
  // talkgroup selection is `allow` or `deny`, and the demo has both services off.
  export_broadcastify: true,
  // Broadcastify's two products are chosen separately; both default true.
  export_bcfy_calls: true,
  export_openmhz: true,
  // The interop channels are the shape the real shared flag is for: county-wide, owned
  // by nobody, and the ones an agency filter has to be asked for explicitly.
  shared: seed.category === "interop",
  first_seen_at: iso(WINDOW_START - 86_400_000 * intBetween(30, 400)),
  last_seen_at: null,
  call_count: 0,
  notes: null,
}));

const TG_BY_TGID = new Map(TALKGROUPS.map((tg) => [tg.tgid, tg]));

/* ------------------------------------------------------------------ radios */

interface RadioSeed {
  radio_id: number;
  alias: string | null;
  unit_id: string | null;
  kind: Radio["kind"];
  agency_code: string | null;
  /** Which talkgroups this radio is normally heard on. */
  tgids: number[];
}

const RADIO_SEEDS: RadioSeed[] = [
  {
    radio_id: 5226001,
    alias: "RC FIRE ENGINE 1",
    unit_id: "E1",
    kind: "mobile",
    agency_code: "XX0010000",
    tgids: [1101, 1102],
  },
  {
    radio_id: 5226002,
    alias: "RC FIRE ENGINE 2",
    unit_id: "E2",
    kind: "mobile",
    agency_code: "XX0010000",
    tgids: [1101, 1102],
  },
  {
    radio_id: 5226003,
    alias: "RC FIRE ENGINE 3",
    unit_id: "E3",
    kind: "mobile",
    agency_code: "XX0010000",
    tgids: [1101, 1103],
  },
  {
    radio_id: 5226004,
    alias: "RC FIRE LADDER 1",
    unit_id: "L1",
    kind: "mobile",
    agency_code: "XX0010000",
    tgids: [1101, 1102],
  },
  {
    radio_id: 5226005,
    alias: "WEST CO FIRE ENGINE 21",
    unit_id: "E21",
    kind: "mobile",
    agency_code: "XX0010200",
    tgids: [1101, 1103],
  },
  {
    radio_id: 5226006,
    alias: "NORTH CO FIRE TENDER 5",
    unit_id: "T5",
    kind: "mobile",
    agency_code: null,
    tgids: [1101],
  },
  {
    radio_id: 5226007,
    alias: "RC FIRE BC 1",
    unit_id: "BC1",
    kind: "portable",
    agency_code: "XX0010000",
    tgids: [1101, 1102, 1103],
  },
  {
    radio_id: 5250001,
    alias: "EC AMB MEDIC 21",
    unit_id: "A21",
    kind: "mobile",
    agency_code: "XX0010500",
    tgids: [48201, 48202],
  },
  {
    radio_id: 5250002,
    alias: "EC AMB MEDIC 22",
    unit_id: "A22",
    kind: "mobile",
    agency_code: "XX0010500",
    tgids: [48201, 48202],
  },
  {
    radio_id: 5250003,
    alias: "EC AMB MEDIC 31",
    unit_id: "M31",
    kind: "mobile",
    agency_code: "XX0010500",
    tgids: [48201],
  },
  {
    radio_id: 5250004,
    alias: "EC AMB SUPERVISOR",
    unit_id: "MS1",
    kind: "portable",
    agency_code: "XX0010500",
    tgids: [48201, 48202, 70001],
  },
  {
    radio_id: 5250057,
    alias: "ECC DISPATCH 1",
    unit_id: null,
    kind: "console",
    agency_code: "XX0010000",
    tgids: [1101, 48201, 50051, 24821, 57011],
  },
  {
    radio_id: 5250058,
    alias: "ECC DISPATCH 2",
    unit_id: null,
    kind: "console",
    agency_code: "XX0010000",
    tgids: [50051, 57011, 24821],
  },
  {
    radio_id: 5250059,
    alias: "ECC DISPATCH 3",
    unit_id: null,
    kind: "console",
    agency_code: "XX0010000",
    tgids: [1101, 48201],
  },
  {
    radio_id: 5310011,
    alias: null,
    unit_id: "1A11",
    kind: "portable",
    agency_code: "XX0010100",
    tgids: [50051, 50061],
  },
  {
    radio_id: 5310012,
    alias: null,
    unit_id: "1A12",
    kind: "portable",
    agency_code: "XX0010100",
    tgids: [50051, 50061],
  },
  {
    radio_id: 5310013,
    alias: null,
    unit_id: "1A13",
    kind: "portable",
    agency_code: "XX0010100",
    tgids: [50051],
  },
  {
    radio_id: 5310021,
    alias: "RC PD SGT 2",
    unit_id: "1S2",
    kind: "portable",
    agency_code: "XX0010100",
    tgids: [50051, 50061, 70001],
  },
  {
    radio_id: 5310031,
    alias: null,
    unit_id: "1D4",
    kind: "portable",
    agency_code: "XX0010100",
    tgids: [50061],
  },
  {
    radio_id: 5320011,
    alias: null,
    unit_id: "2A21",
    kind: "portable",
    agency_code: "XX0010200",
    tgids: [57011, 57012],
  },
  {
    radio_id: 5320012,
    alias: null,
    unit_id: "2A22",
    kind: "portable",
    agency_code: "XX0010200",
    tgids: [57011, 57012],
  },
  {
    radio_id: 5320021,
    alias: "WEST CO PD SGT",
    unit_id: "2S1",
    kind: "portable",
    agency_code: "XX0010200",
    tgids: [57011, 57012],
  },
  {
    radio_id: 5410011,
    alias: "STATE UNIV DPS 21",
    unit_id: "D21",
    kind: "portable",
    agency_code: "XX0010600",
    tgids: [19282, 19283],
  },
  {
    radio_id: 5410012,
    alias: "STATE UNIV DPS 22",
    unit_id: "D22",
    kind: "portable",
    agency_code: "XX0010600",
    tgids: [19282, 19283],
  },
  {
    radio_id: 5410013,
    alias: null,
    unit_id: "D31",
    kind: "portable",
    agency_code: "XX0010600",
    tgids: [19283],
  },
  {
    radio_id: 5510001,
    alias: "SHUTTLE 41",
    unit_id: "C41",
    kind: "mobile",
    agency_code: "XX0010600",
    tgids: [29201],
  },
  {
    radio_id: 5510002,
    alias: "SHUTTLE 47",
    unit_id: "C47",
    kind: "mobile",
    agency_code: "XX0010600",
    tgids: [29201],
  },
  {
    radio_id: 5510003,
    alias: "SHUTTLE DISPATCH",
    unit_id: null,
    kind: "console",
    agency_code: "XX0010600",
    tgids: [29201],
  },
  {
    radio_id: 5520001,
    alias: "RC TRANSIT 605",
    unit_id: "B605",
    kind: "mobile",
    agency_code: "XX0010100",
    tgids: [29202],
  },
  {
    radio_id: 5520002,
    alias: "RC TRANSIT 612",
    unit_id: "B612",
    kind: "mobile",
    agency_code: "XX0010100",
    tgids: [29202],
  },
  {
    radio_id: 5610011,
    alias: null,
    unit_id: "3D11",
    kind: "portable",
    agency_code: "XX0010000",
    tgids: [24821, 24822],
  },
  {
    radio_id: 5610012,
    alias: null,
    unit_id: "3D12",
    kind: "portable",
    agency_code: "XX0010000",
    tgids: [24821, 24822],
  },
  {
    radio_id: 5610013,
    alias: "EC SO SGT 1",
    unit_id: "3S1",
    kind: "portable",
    agency_code: "XX0010000",
    tgids: [24821, 70001],
  },
  {
    radio_id: 5710001,
    alias: "RC STREETS 12",
    unit_id: "PW12",
    kind: "mobile",
    agency_code: "XX0010100",
    tgids: [61010],
  },
  {
    radio_id: 5710002,
    alias: "RC WATER 4",
    unit_id: "PW4",
    kind: "mobile",
    agency_code: "XX0010100",
    tgids: [61010],
  },
  {
    radio_id: 5710011,
    alias: "SEC ROADS 8",
    unit_id: "SR8",
    kind: "mobile",
    agency_code: "XX0010000",
    tgids: [61011],
  },
  {
    radio_id: 5810001,
    alias: "EC SCHOOLS BUS 32",
    unit_id: "S32",
    kind: "mobile",
    agency_code: null,
    tgids: [80101],
  },
  {
    radio_id: 5810002,
    alias: "EC SCHOOLS BUS 47",
    unit_id: "S47",
    kind: "mobile",
    agency_code: null,
    tgids: [80101],
  },
  {
    radio_id: 4831120,
    alias: "NORTH CO ROAM",
    unit_id: null,
    kind: "portable",
    agency_code: null,
    tgids: [70001, 70002],
  },
  { radio_id: 4831145, alias: null, unit_id: null, kind: "portable", agency_code: null, tgids: [70002] },
];

export const RADIOS: Radio[] = RADIO_SEEDS.map((seed, index) => ({
  id: index + 1,
  system_id: 1,
  radio_id: seed.radio_id,
  wacn: seed.radio_id < 5000000 ? 781824 : null,
  home_system_id: seed.radio_id < 5000000 ? 57 : null,
  alias: seed.alias,
  unit_id: seed.unit_id,
  agency_code: seed.agency_code,
  kind: seed.kind,
  first_seen_at: iso(WINDOW_START - 86_400_000 * intBetween(10, 300)),
  last_seen_at: null,
  call_count: 0,
  notes: null,
  // Filled from ASSIGNMENTS below, the way the API fills it from `radio_assignments`.
  current_unit: null,
  current_person: null,
}));

/* ------------------------------------------------------------------ roster */

/**
 * One unit per callsign the radio seeds carry, so the roster and the radio
 * directory agree — which is the whole point of the units table.
 */
export const UNITS: Unit[] = (() => {
  const seen = new Map<string, Unit>();
  for (const seed of RADIO_SEEDS) {
    if (!seed.unit_id || seen.has(seed.unit_id)) continue;
    seen.set(seed.unit_id, {
      id: seen.size + 1,
      callsign: seed.unit_id,
      name: seed.alias,
      agency_code: seed.agency_code,
      kind: seed.kind === "console" ? "station" : "vehicle",
      notes: null,
      created_at: iso(WINDOW_START - 86_400_000 * 30),
      updated_at: iso(WINDOW_START - 86_400_000 * 30),
      radio_count: 0,
    });
  }
  return [...seen.values()];
})();

export const PERSONNEL: Person[] = ["J. Smith", "R. Alvarez", "M. Novak", "T. Boyd", "K. Whitfield"].map(
  (name, index) => ({
    id: index + 1,
    name,
    rank: index % 3 === 0 ? "Captain" : "Firefighter",
    badge: String(4400 + index),
    agency_code: "XX0010100",
    active: index < 4,
    notes: null,
    created_at: iso(WINDOW_START - 86_400_000 * 60),
    updated_at: iso(WINDOW_START - 86_400_000 * 60),
    radio_count: 0,
  }),
);

/**
 * Current assignments: every radio that carries a callsign is on the matching
 * unit, and the first few also have somebody holding them — enough for the
 * assignment card, the reverse views and the history list to have content.
 */
export const ASSIGNMENTS: RadioAssignment[] = (() => {
  const rows: RadioAssignment[] = [];
  let id = 1;
  RADIOS.forEach((radio, index) => {
    const unit = UNITS.find((candidate) => candidate.callsign === radio.unit_id);
    if (unit) {
      rows.push({
        id: id++,
        radio_pk: radio.id,
        kind: "unit",
        unit: { id: unit.id, callsign: unit.callsign, name: unit.name },
        person: null,
        effective_from: iso(WINDOW_START - 86_400_000 * 20),
        effective_to: null,
        current: true,
        note: null,
      });
    }
    const person = PERSONNEL[index];
    if (person?.active) {
      rows.push({
        id: id++,
        radio_pk: radio.id,
        kind: "person",
        unit: null,
        person: { id: person.id, name: person.name },
        effective_from: iso(WINDOW_START - 86_400_000),
        effective_to: null,
        current: true,
        note: "on shift",
      });
    }
  });
  return rows;
})();

for (const assignment of ASSIGNMENTS) {
  const radio = RADIOS.find((candidate) => candidate.id === assignment.radio_pk);
  if (!radio) continue;
  if (assignment.unit) radio.current_unit = assignment.unit;
  if (assignment.person) radio.current_person = assignment.person;
}
for (const unit of UNITS) {
  unit.radio_count = ASSIGNMENTS.filter((a) => a.unit?.id === unit.id).length;
}
for (const person of PERSONNEL) {
  person.radio_count = ASSIGNMENTS.filter((a) => a.person?.id === person.id).length;
}

const RADIOS_BY_TGID = new Map<number, Radio[]>();
RADIO_SEEDS.forEach((seed, index) => {
  for (const tgid of seed.tgids) {
    const list = RADIOS_BY_TGID.get(tgid) ?? [];
    list.push(RADIOS[index] as Radio);
    RADIOS_BY_TGID.set(tgid, list);
  }
});

/** The label the UI shows for a radio: unit, then alias, then the bare id. */
export const radioLabel = (radio: Radio): string => radio.unit_id ?? radio.alias ?? String(radio.radio_id);

/** The fully-qualified `WACN_SYS_ID` form sdrtrunk writes for a roaming radio. */
const radioRaw = (radio: Radio): string =>
  radio.wacn === null ? String(radio.radio_id) : `${radio.wacn}.${radio.home_system_id}.${radio.radio_id}`;

/* ----------------------------------------------------------------- patches */

export const PATCHES: Patch[] = [
  {
    id: 1,
    system_id: 1,
    patch_id: 9238,
    member_tgids: [57012, 50061, 57011],
    member_aliases: ["WEST CO LAW 2", "STATE R6 CALL 61", "WEST CO LAW 1"],
    first_seen_at: null,
    last_seen_at: null,
    call_count: 0,
  },
  {
    id: 2,
    system_id: 1,
    patch_id: 9230,
    member_tgids: [19282, 50051],
    member_aliases: ["STATE VLAW31 PATCH", "RC LAW DISPATCH"],
    first_seen_at: null,
    last_seen_at: null,
    call_count: 0,
  },
  {
    id: 3,
    system_id: 1,
    patch_id: 9241,
    member_tgids: [1101, 48201, 70001],
    member_aliases: ["EC FIRE DISPATCH", "EC FIRE OPS", "INTEROP 1"],
    first_seen_at: null,
    last_seen_at: null,
    call_count: 0,
  },
];

/* ------------------------------------------------------------- transcripts */

const STREETS = [
  "Main Street",
  "Oak Street",
  "Riverside Drive",
  "Maple Avenue",
  "First Avenue",
  "Highway 12",
  "Park Avenue",
  "Elm Street",
  "Ridge Road",
  "Lincoln Boulevard",
  "Market Street",
  "Lakeview Boulevard",
];

const PLACES = [
  "the Riverside Mall",
  "Memorial Hospital",
  "Ridge Mall",
  "the River Landing",
  "Central High",
  "the county courthouse",
  "Riverside Hospital",
  "the grocery store on First Avenue",
  "the performing arts center",
  "the Fairview strip",
];

const LAW_LINES = [
  (u: string) => `${u} go ahead.`,
  (u: string) => `${u} show me out on a traffic stop at ${pick(STREETS)} and ${pick(STREETS)}.`,
  (u: string) => `${u} I'm ten-eight.`,
  (u: string) => `${u} copy, en route.`,
  (u: string) => `${u} start me another unit at ${pick(PLACES)}.`,
  (u: string) => `${u} clear, no report.`,
  (u: string) => `${u} can you run a plate for me, David Ocean Nine Four Two One.`,
  (u: string) => `Dispatch ${u}, we have a report of a vehicle in the ditch on ${pick(STREETS)}.`,
  (u: string) => `${u} received, show me arrived.`,
  (u: string) => `${u} negative contact, checking the area.`,
  () => `All units be advised, we are getting multiple calls on that one.`,
  (u: string) => `${u} party is going to be signal nine, no further.`,
];

const FIRE_LINES = [
  (u: string) => `${u} responding.`,
  (u: string) => `Engine ${u} on scene, nothing showing.`,
  (u: string) => `${u} we have smoke showing from the second floor, give me a second alarm.`,
  (u: string) => `${u} lay me a supply line off the hydrant at ${pick(STREETS)}.`,
  () => `Station 1, station 2, alarm sounding at ${pick(PLACES)}, structure fire.`,
  (u: string) => `${u} command is established, ${pick(STREETS)} command.`,
  (u: string) => `${u} we're going to be in service and returning.`,
  (u: string) => `${u} can you get me the fire alarm company on the phone.`,
  (u: string) => `${u} primary search complete, all clear.`,
];

const EMS_LINES = [
  (u: string) => `${u} en route to ${pick(PLACES)}.`,
  (u: string) => `${u} transporting one, priority two, to University Hospitals.`,
  (u: string) => `${u} we're going to need a lift assist here, can you start fire.`,
  (u: string) => `Medic ${u}, patient is a 68-year-old male, chest pain, stable vitals.`,
  (u: string) => `${u} show us clear of the hospital and back in service.`,
  (u: string) => `${u} copy that, we'll take it.`,
  (u: string) => `${u} we have a refusal, no transport.`,
];

const TRANSIT_LINES = [
  (u: string) => `${u} running about six minutes behind on the Red Route.`,
  (u: string) => `${u} I'm going to need a relief at the Interchange.`,
  (u: string) => `${u} clear at the hospital stop.`,
  (u: string) => `Base ${u}, can you check on the wheelchair lift on this coach.`,
  (u: string) => `${u} we have a disruptive passenger, requesting an officer.`,
];

const PW_LINES = [
  (u: string) => `${u} we're going to close the eastbound lane on ${pick(STREETS)}.`,
  (u: string) => `${u} main break at ${pick(STREETS)}, we'll need a crew.`,
  (u: string) => `${u} plow route four is complete.`,
  (u: string) => `${u} signal is flashing red at ${pick(STREETS)}.`,
];

const SHORT_LINES = [
  "Copy.",
  "Go ahead.",
  "Ten-four.",
  "Received.",
  "Clear.",
  "Standing by.",
  "Affirmative.",
];

const NOISE_LINES = [
  "Thanks for watching!",
  "you",
  "Subtitles by the Amara.org community",
  ".",
  "Thank you.",
];

const linesFor = (category: TalkgroupCategory | null): ((unit: string) => string)[] => {
  switch (category) {
    case "fire":
      return FIRE_LINES;
    case "ems":
      return EMS_LINES;
    case "transit":
      return TRANSIT_LINES;
    case "pw":
      return PW_LINES;
    case "schools":
      return TRANSIT_LINES;
    default:
      return LAW_LINES;
  }
};

/** Split a line into 2–4 s segments the way a Whisper backend would. */
function segmentize(text: string, duration: number): TranscriptSegment[] {
  const words = text.split(" ");
  const chunks = Math.max(1, Math.min(4, Math.ceil(words.length / 7)));
  const per = duration / chunks;
  const segments: TranscriptSegment[] = [];
  for (let i = 0; i < chunks; i++) {
    const slice = words.slice(
      Math.round((i * words.length) / chunks),
      Math.round(((i + 1) * words.length) / chunks),
    );
    if (slice.length === 0) continue;
    segments.push({
      start: Number((i * per).toFixed(2)),
      end: Number(((i + 1) * per).toFixed(2)),
      text: slice.join(" "),
      avg_logprob: Number(between(-0.9, -0.12).toFixed(3)),
      no_speech_prob: Number(between(0.01, 0.35).toFixed(3)),
      compression_ratio: Number(between(0.9, 1.9).toFixed(2)),
    });
  }
  return segments;
}

/* ------------------------------------------------------------------- calls */

export interface MockCall extends Call {
  raw: RawMetadata;
  sources: CallSource[];
  transcripts: Transcript[];
}

/**
 * How many calls come from trunk-recorder rather than sdrtrunk.
 *
 * A real deployment usually runs one recorder, but the UI has to render both, and a
 * fixture corpus with none of the second kind means the sources list, the emergency and
 * encrypted badges and the sidecar time source are never seen while working on them. One
 * in eight is enough to meet them on almost any page and few enough that the archive
 * still reads as the sdrtrunk one it is modeled on (§1).
 */
const TRUNK_RECORDER_SHARE = 0.125;

/**
 * Call length distribution from the corpus: median ~2.2 s, p95 ~12 s, max 44.5 s,
 * and 14 % under a second.
 */
function drawDuration(): number {
  if (chance(0.14)) return Number(between(0.3, 0.99).toFixed(3));
  if (chance(0.72)) return Number(between(1.0, 4.5).toFixed(3));
  if (chance(0.8)) return Number(between(4.5, 12).toFixed(3));
  return Number(between(12, 44.5).toFixed(3));
}

/** Diurnal weight: quiet overnight, busy mid-afternoon, a bump around 17:00. */
function hourWeight(hour: number): number {
  const base = 0.25 + 0.75 * Math.sin(((hour - 4 + 24) % 24) * (Math.PI / 22));
  return Math.max(0.08, base + (hour >= 16 && hour <= 18 ? 0.3 : 0));
}

const TAG_POOL = ["structure fire", "pursuit", "mutual aid", "training", "review", "mva", "welfare check"];

function buildCalls(): MockCall[] {
  const weighted: TgSeed[] = [];
  for (const seed of TG_SEEDS) for (let i = 0; i < seed.weight; i++) weighted.push(seed);

  const calls: MockCall[] = [];
  let id = 1000;
  let cursor = WINDOW_START;
  const spanMs = WINDOW_END - WINDOW_START;
  const target = 600;

  /** One transmission on a channel at a given instant. */
  const emit = (seed: TgSeed, startedAt: number): void => {
    const talkgroup = TG_BY_TGID.get(seed.tgid) as Talkgroup;
    const duration = drawDuration();
    const endedAt = startedAt + duration * 1000;

    const isPatch = chance(0.05);
    const patch = isPatch ? (PATCHES.filter((p) => p.member_tgids.includes(seed.tgid))[0] ?? null) : null;
    const talkgroupIds = patch ? patch.member_tgids : [seed.tgid];

    const pool = RADIOS_BY_TGID.get(seed.tgid) ?? RADIOS;
    // ~2 % of calls never resolved a FROM — the file simply has no TPE1 frame.
    const resolved = !chance(0.02);
    const radio = resolved ? pick(pool) : null;
    const unit = radio ? radioLabel(radio) : "Dispatch";

    // Transcription policy: the school channel is off, sub-0.75 s calls are skipped.
    let status: TranscriptStatus;
    let quality: TranscriptQuality | null = null;
    let text: string | null = null;
    if (!talkgroup.transcribe) status = "skipped";
    else if (duration < (talkgroup.transcribe_min_seconds ?? 0.75)) status = "skipped";
    else if (chance(0.07)) {
      // The 7 % that are not `done`: mostly still queued, a couple running, and a
      // handful the backend gave up on.
      const roll = rnd();
      status = roll < 0.45 ? "queued" : roll < 0.7 ? "running" : "failed";
    } else {
      status = "done";
      if (duration < 1.4 && chance(0.55)) {
        text = pick(SHORT_LINES);
        quality = chance(0.4) ? "suspect" : "good";
      } else if (chance(0.05)) {
        text = pick(NOISE_LINES);
        quality = "noise";
      } else {
        text = pick(linesFor(talkgroup.category as TalkgroupCategory | null))(unit);
        quality = chance(0.09) ? "suspect" : "good";
      }
      if (text.trim() === "") status = "empty";
    }

    const dupe = chance(0.015) && calls.length > 0;
    const fileStamp = new Date(endedAt);
    const pad = (n: number) => String(n).padStart(2, "0");
    const stamp = `${fileStamp.getFullYear()}${pad(fileStamp.getMonth() + 1)}${pad(fileStamp.getDate())}_${pad(fileStamp.getHours())}${pad(fileStamp.getMinutes())}${pad(fileStamp.getSeconds())}`;
    const toPart = patch ? `P${patch.patch_id}-[${patch.member_tgids.join("--")}]` : String(seed.tgid);
    const fromPart = radio ? `_FROM_${radioRaw(radio).replaceAll(".", "_")}` : "";
    const fileName = `${stamp}_Example-Network_Central_T-Control__TO_${toPart}${fromPart}.mp3`;
    const dayFolder = `${fileStamp.getFullYear()}${pad(fileStamp.getMonth() + 1)}${pad(fileStamp.getDate())}`;

    id += 1;
    const frequency = pick([852137500, 852362500, 853212500, 851287500, 853987500]);
    const fromTrunkRecorder = chance(TRUNK_RECORDER_SHARE);
    const tags = chance(0.06) ? [pick(TAG_POOL)] : [];

    const transcripts: Transcript[] = [];
    if (status === "done" && text) {
      const primaryModel = chance(0.7) ? "large-v3-turbo" : "medium";
      const reasons: string[] = [];
      if (quality === "noise") reasons.push("known hallucination phrase", "no_speech_prob > 0.6");
      if (quality === "suspect") reasons.push(chance(0.5) ? "compression_ratio > 2.4" : "very short audio");
      const remote = chance(0.75);
      transcripts.push({
        id: id * 10,
        call_id: id,
        engine: remote ? "openai_compatible" : "faster_whisper_local",
        backend_name: remote ? "whisper" : "local-cpu",
        model: primaryModel,
        language: "en",
        text,
        original_text: text,
        words: null,
        segments: segmentize(text, duration),
        avg_logprob: Number(between(-0.85, -0.15).toFixed(3)),
        no_speech_prob: Number((quality === "noise" ? between(0.62, 0.95) : between(0.01, 0.4)).toFixed(3)),
        compression_ratio: Number((quality === "suspect" ? between(2.4, 3.6) : between(0.9, 2.0)).toFixed(2)),
        quality: quality ?? "good",
        quality_reasons: reasons,
        prompt_used: `Radio traffic for ${talkgroup.alias}. Units: ${(RADIOS_BY_TGID.get(seed.tgid) ?? []).map(radioLabel).join(", ")}.`,
        vad_used: true,
        processing_ms: Math.round(duration * 1000 * between(0.08, 0.45)),
        rtf: Number(between(3.5, 12).toFixed(2)),
        created_at: iso(endedAt + intBetween(4000, 60_000)),
        is_primary: true,
        edited_by_user: false,
        edited_text: null,
      });
      // A second opinion on ~12 % of calls: a re-run on the other backend.
      if (chance(0.12)) {
        transcripts.push({
          id: id * 10 + 1,
          call_id: id,
          engine: "faster_whisper_local",
          backend_name: "local-cpu",
          model: "small",
          language: "en",
          text: text.replace(/\.$/, "").toLowerCase(),
          original_text: text.replace(/\.$/, "").toLowerCase(),
          words: null,
          segments: segmentize(text, duration),
          avg_logprob: Number(between(-1.2, -0.4).toFixed(3)),
          no_speech_prob: Number(between(0.05, 0.5).toFixed(3)),
          compression_ratio: Number(between(0.9, 2.2).toFixed(2)),
          quality: chance(0.4) ? "suspect" : "good",
          quality_reasons: [],
          prompt_used: null,
          vad_used: true,
          processing_ms: Math.round(duration * 1000 * between(0.7, 2.4)),
          rtf: Number(between(0.4, 1.4).toFixed(2)),
          created_at: iso(endedAt + intBetween(120_000, 900_000)),
          is_primary: false,
          edited_by_user: false,
          edited_text: null,
        });
      }
    }

    // trunk-recorder's sidecar names every radio that keyed up and where in the audio it
    // did; sdrtrunk can only ever see the first, so its call gets one source at offset 0.
    const extraSources = fromTrunkRecorder && duration > 3 && chance(0.5) ? intBetween(1, 2) : 0;
    const sources: CallSource[] = [];
    if (radio) {
      let offset = 0;
      for (let index = 0; index <= extraSources; index++) {
        const speaker = index === 0 ? radio : pick(pool);
        const span =
          index === extraSources
            ? Number((duration - offset).toFixed(3))
            : Number(between(0.8, Math.max(1, (duration - offset) / 2)).toFixed(3));
        sources.push({
          id: id * 100 + index,
          radio_id: speaker.id,
          radio_label: radioLabel(speaker),
          src: speaker.radio_id,
          pos_s: Number(offset.toFixed(3)),
          len_s: span > 0 ? span : null,
          emergency: false,
          tag: speaker.unit_id,
          seq: index,
        });
        offset += span;
      }
    }

    const encrypted = fromTrunkRecorder ? chance(0.04) : null;
    const audioType = fromTrunkRecorder ? pick(["digital", "digital tdma", "analog"]) : null;

    const raw: RawMetadata = {
      file_path: `${dayFolder}/${talkgroup.alias}/${fileName}`,
      file_name: fileName,
      file_size: Math.round(duration * 4000 + 500),
      sha256: Array.from({ length: 64 }, () => "0123456789abcdef"[Math.floor(rnd() * 16)]).join(""),
      bitrate: 32000,
      sample_rate: 22050,
      channels: 1,
      recorder_version: "sdrtrunk nightly - 2026-02-19T09:01:39.551+0000",
      file_mtime: iso(endedAt + 800),
      id3: {
        COMM: `Date:${iso(endedAt).replace("T", " ").replace("Z", "")};System:Example Network;Site:Central;Name:T-Control;Decoder:P25 Phase 1;Channel:1-90;Frequency:${frequency};`,
        TIT2: patch
          ? `P:${patch.patch_id} [${patch.member_tgids.join(", ")}]"PATCH ${patch.patch_id}",${patch.member_tgids.map((t) => `"${TG_BY_TGID.get(t)?.alias ?? t}"`).join(",")}`
          : `${seed.tgid}"${talkgroup.alias}"`,
        TPE1: radio ? radioRaw(radio) : "",
        TALB: "T-Control",
        TIT1: "Example Network",
        TCOM: "sdrtrunk nightly - 2026-02-19T09:01:39.551+0000",
        TCON: "Scanner Audio",
        TDRC: "2026",
      },
      raw_meta: {},
    };
    // The raw panel shows whatever the recorder itself wrote: an sdrtrunk file's ID3
    // frames, or a trunk-recorder sidecar. Nothing parses it back.
    raw.raw_meta = fromTrunkRecorder
      ? {
          freq: frequency,
          signal: intBetween(-70, -40),
          noise: intBetween(-110, -95),
          start_time_ms: startedAt,
          stop_time_ms: Math.round(endedAt),
          call_length_ms: Math.round(duration * 1000),
          talkgroup: seed.tgid,
          talkgroup_group_tag: talkgroup.alias,
          talkgroup_tag: talkgroup.category,
          talkgroup_group: talkgroup.group_name,
          audio_type: audioType,
          encrypted: encrypted ? 1 : 0,
          short_name: "CENTRAL",
          srcList: sources.map((source) => ({ src: source.src, pos: source.pos_s, tag: source.tag })),
        }
      : raw.id3;
    if (fromTrunkRecorder) raw.id3 = {};

    calls.push({
      id,
      system_id: 1,
      system_name: SYSTEM.name,
      site_id: 1,
      site_name: "Central",
      talkgroup_id: talkgroup.id,
      talkgroup_alias: talkgroup.alias,
      talkgroup_alias_at_record: talkgroup.alias,
      tgid: seed.tgid,
      talkgroup_ids: talkgroupIds,
      category: talkgroup.category,
      service_tag: talkgroup.service_tag,
      agency_code: talkgroup.agency_code,
      color: talkgroup.color,
      patch_id: patch?.id ?? null,
      patch_label: patch ? `PATCH ${patch.patch_id}` : null,
      patch_member_tgids: patch?.member_tgids ?? [],
      kind: "group",
      target_radio_id: null,
      target_radio_label: null,
      radio_id: radio?.id ?? null,
      radio_label: radio ? radioLabel(radio) : null,
      radio_raw: radio ? radioRaw(radio) : null,
      unit_id: radio?.unit_id ?? null,
      talker_alias: null,
      radio_resolved: resolved,
      ingest_source: fromTrunkRecorder ? "trunk_recorder" : "file",
      // A slice of the corpus stands in for the archive migration: those rows were pushed
      // long after they were recorded, which is what the "backfilled" marker and the
      // priority filter are for. A plain file pickup carries no class at all — null, not
      // "live" — because coming off disk says nothing about when it arrived.
      ingest_priority: fromTrunkRecorder ? "live" : chance(0.08) ? "backlog" : null,
      recorded_at: iso(endedAt + 900),
      started_at: iso(startedAt),
      started_local: localIso(startedAt),
      ended_at: iso(endedAt),
      time_source: fromTrunkRecorder ? "sidecar" : "file_write",
      duration_s: duration,
      frequency_hz: frequency,
      channel: "1-90",
      decoder: "P25 Phase 1",
      call_type: patch ? "patch" : chance(0.01) ? "individual" : "group",
      emergency: fromTrunkRecorder ? chance(0.05) : chance(0.01) ? true : null,
      encrypted,
      audio_type: audioType,
      // An encrypted call is kept for its metadata but has no recording to serve (§1.7),
      // so it is a metadata-only row — exactly what the "Audio only" toggle hides.
      audio_state: encrypted ? "purged" : "present",
      // The audio-processing stage (§5.5): most calls have a processed copy, a few carry a
      // page, and a couple measured as silence — enough of each to exercise every badge.
      tone_detected: !encrypted && chance(0.06),
      alert: !encrypted && chance(0.02),
      processed_audio: !encrypted,
      audio_meta: encrypted
        ? null
        : {
            version: 1,
            gain_db: Math.round((6 + Math.random() * 14) * 10) / 10,
            silent: chance(0.01),
            // Which chain produced the file — the profile's name, or null for the global
            // chain. Only the medical-control channel is on a profile, so only its calls
            // carry one, which is exactly the split the call page has to render.
            profile: talkgroup.audio_profile_id === 2 ? "Aircraft cabin" : null,
          },
      duplicate_of: dupe ? ((calls[calls.length - 1] as MockCall).id ?? null) : null,
      conversation_id: null,
      transcript_status: status,
      transcript_text: text,
      transcript_highlight: null,
      transcript_quality: quality,
      notes: "",
      tags,
      starred: chance(0.03),
      audio_url: `/api/calls/${id}/audio`,
      ingested_at: iso(endedAt + 2500),
      updated_at: iso(endedAt + 2500),
      raw,
      sources,
      transcripts,
    });
  };

  // Radio traffic arrives in *exchanges*, not as independent events: dispatch calls
  // a unit, the unit answers, dispatch acknowledges — three keyups in twenty
  // seconds. Generating bursts rather than a flat Poisson process is what makes
  // the conversation threading, the "who's active" panel and the continuous-play
  // queue behave the way they will against the real corpus.
  //
  // Each pass walks the whole two-day window once; the loop runs another pass if
  // the target is not met, which is also what 20 channels sharing one window
  // really look like — exchanges on different talkgroups overlap in time.
  for (let pass = 0; pass < 8 && calls.length < target; pass++) {
    cursor = WINDOW_START;
    while (calls.length < target && cursor < WINDOW_END) {
      const hour = new Date(cursor).getHours();
      const meanGap = spanMs / target / hourWeight(hour);
      cursor += Math.max(1500, -Math.log(1 - rnd()) * meanGap * 2.2);
      if (cursor >= WINDOW_END) break;

      const seed = pick(weighted);
      const burst = chance(0.55) ? intBetween(2, 5) : 1;
      let at = cursor;
      for (let i = 0; i < burst && calls.length < target; i++) {
        emit(seed, at);
        // Inside an exchange the next keyup follows within a few seconds.
        at += intBetween(1200, 9000);
      }
      cursor = at;
    }
  }

  // Two unit-to-unit (private) calls.  Cloned off rows the generator already made, so
  // every column but the ones that define a private call stays plausible — and made from
  // *resolved* radios, because a pair with an unknown end is the one shape the table
  // renders as "? → ?" and says nothing.
  //
  // Two rather than one: the calls table has to show both directions of the same pair (the
  // radio detail page lists a radio's private calls whichever end it was), and one row can
  // only be at one end.
  const pair = calls.filter((call) => call.radio_id !== null).slice(0, 2);
  const [alpha, beta] = pair;
  if (alpha && beta && alpha.radio_id !== null && beta.radio_id !== null) {
    const privates: Array<[MockCall, MockCall]> = [
      [alpha, beta],
      [beta, alpha],
    ];
    for (const [from, to] of privates) {
      calls.push({
        ...from,
        id: ++id,
        kind: "unit",
        // No talkgroup at all: that is what makes it a private call, and the fixture has
        // to prove the table copes with the blank rather than papering over it.
        talkgroup_id: null,
        talkgroup_alias: null,
        talkgroup_alias_at_record: null,
        tgid: null,
        talkgroup_ids: [],
        category: null,
        service_tag: null,
        agency_code: null,
        color: null,
        patch_id: null,
        patch_label: null,
        patch_member_tgids: [],
        call_type: "individual",
        radio_id: from.radio_id,
        radio_label: from.radio_label,
        radio_raw: from.radio_raw,
        target_radio_id: to.radio_id,
        target_radio_label: to.radio_label,
        conversation_id: null,
        duplicate_of: null,
        ingest_source: "trunkd",
        ingest_priority: "live",
        audio_url: `/api/calls/${id}/audio`,
      });
    }
  }
  return calls;
}

export const CALLS: MockCall[] = buildCalls();

/**
 * One call in the fixture archive carries a source-quality report, so the detail page's
 * "recorded by" line is reachable in dev without two real receivers.
 *
 * It is deliberately a *second* instance (`…-02`, the warm spare below): the line only
 * exists because more than one receiver can hear a call, and a fixture where the quality
 * always names the one recorder would render the feature without showing what it is for.
 */
export const SOURCE_QUALITY_CALL_ID: number = CALLS[0]?.id ?? 0;
export const SOURCE_QUALITY: CallSourceQuality = {
  instance: "trunkd-central-02",
  voice_frames: 1234,
  bad_frames: 7,
  rssi_dbfs: -38.4,
};

/* ----------------------------------------------------------- conversations */

export const CONVERSATIONS: Conversation[] = [];
{
  // Consecutive calls on the same talkgroup with gaps under 20 s are one exchange.
  const byTg = new Map<number, MockCall[]>();
  for (const call of CALLS) {
    // A conversation is consecutive traffic *on a talkgroup*; a private call is on none,
    // and bucketing them together under a null tgid would thread two unrelated pairs into
    // one exchange.
    if (call.kind === "unit") continue;
    const key = call.tgid ?? 0;
    const list = byTg.get(key) ?? [];
    list.push(call);
    byTg.set(key, list);
  }
  let conversationId = 1;
  for (const [, list] of byTg) {
    list.sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
    let group: MockCall[] = [];
    const flush = () => {
      if (group.length < 2) {
        group = [];
        return;
      }
      const first = group[0] as MockCall;
      const last = group[group.length - 1] as MockCall;
      const id = conversationId++;
      for (const call of group) call.conversation_id = id;
      CONVERSATIONS.push({
        id,
        talkgroup_id: first.talkgroup_id,
        talkgroup_alias: first.talkgroup_alias,
        tgid: first.tgid,
        started_at: first.started_at,
        started_local: first.started_local,
        ended_at: last.ended_at,
        call_count: group.length,
        first_call_id: first.id,
        duration_s: Number(((Date.parse(last.ended_at) - Date.parse(first.started_at)) / 1000).toFixed(3)),
      });
      group = [];
    };
    for (const call of list) {
      const previous = group[group.length - 1];
      if (previous && Date.parse(call.started_at) - Date.parse(previous.ended_at) > 20_000) flush();
      group.push(call);
    }
    flush();
  }
}

/* ------------------------------------------------------------- roll-ups */

CALLS.sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at));

for (const talkgroup of TALKGROUPS) {
  const own = CALLS.filter((c) => c.talkgroup_ids.includes(talkgroup.tgid));
  talkgroup.call_count = own.length;
  talkgroup.last_seen_at = own[0]?.started_at ?? null;
}
for (const radio of RADIOS) {
  const own = CALLS.filter((c) => c.radio_id === radio.id);
  radio.call_count = own.length;
  radio.last_seen_at = own[0]?.started_at ?? null;
}
// Presence without traffic: some radios are only ever seen registering/affiliating on the
// control channel and never key up (docs/RADIO_EVENTS.md).  Give a handful of the silent
// radios a recent `last_seen_at` so the "seen (silent)" state is real in dev and tests.
for (const radio of RADIOS.filter((r) => r.call_count === 0).slice(0, 6)) {
  radio.last_seen_at = iso(WINDOW_START - 3_600_000 * intBetween(1, 48));
}
for (const patch of PATCHES) {
  const own = CALLS.filter((c) => c.patch_id === patch.id);
  patch.call_count = own.length;
  patch.last_seen_at = own[0]?.started_at ?? null;
  patch.first_seen_at = own[own.length - 1]?.started_at ?? null;
}

export const AFFILIATIONS: RadioAffiliation[] = [];
{
  const seen = new Map<string, RadioAffiliation>();
  for (const call of CALLS) {
    if (call.radio_id === null || call.talkgroup_id === null) continue;
    const key = `${call.radio_id}:${call.talkgroup_id}`;
    const existing = seen.get(key);
    if (existing) {
      existing.call_count += 1;
      continue;
    }
    const radio = RADIOS.find((r) => r.id === call.radio_id) as Radio;
    seen.set(key, {
      radio_id: call.radio_id,
      radio_alias: radio.alias,
      unit_id: radio.unit_id,
      talkgroup_id: call.talkgroup_id,
      talkgroup_alias: call.talkgroup_alias,
      tgid: call.tgid ?? 0,
      first_heard_at: call.started_at,
      last_heard_at: call.started_at,
      call_count: 1,
      source: "call",
      last_affiliated_at: null,
    });
  }
  AFFILIATIONS.push(...seen.values());
  // Two of them are also backed by the control channel, so the source badge and the
  // "affiliated but never heard" case both have something to render in mock mode
  // (docs/RADIO_EVENTS.md).
  for (const affiliation of AFFILIATIONS.slice(0, 2)) {
    affiliation.source = "affiliation";
    affiliation.last_affiliated_at = affiliation.last_heard_at;
  }
}

/**
 * Control-channel events (docs/RADIO_EVENTS.md). Synthesized rather than derived
 * from the calls: their whole point is that they happen when nobody is talking, so
 * each radio registers a few minutes before its first transmission and affiliates
 * to the channel it then uses.
 */
export const RADIO_EVENTS: RadioEvent[] = (() => {
  const rows: RadioEvent[] = [];
  let id = 1;
  for (const radio of RADIOS.slice(0, 24)) {
    const own = CALLS.filter((call) => call.radio_id === radio.id);
    const first = own[own.length - 1];
    if (!first) continue;
    const registered = new Date(Date.parse(first.started_at) - 7 * 60_000).toISOString();
    const affiliated = new Date(Date.parse(first.started_at) - 6 * 60_000).toISOString();
    rows.push({
      id: id++,
      at: registered,
      kind: "unit_registration",
      opcode: 44,
      mfid: 0,
      system_id: 1,
      site_id: 1,
      site_name: "Central",
      radio_pk: radio.id,
      radio_id: radio.radio_id,
      radio_wacn: radio.wacn ?? 0,
      radio_system: radio.home_system_id ?? 0,
      target_radio_id: null,
      talkgroup_pk: null,
      talkgroup_tgid: null,
      talkgroup_alias: null,
      result: "accept",
      frequency_hz: null,
      instance: "trunkd-garage",
      meta: null,
    });
    rows.push({
      id: id++,
      at: affiliated,
      kind: "group_affiliation",
      opcode: 40,
      mfid: 0,
      system_id: 1,
      site_id: 1,
      site_name: "Central",
      radio_pk: radio.id,
      radio_id: radio.radio_id,
      radio_wacn: radio.wacn ?? 0,
      radio_system: radio.home_system_id ?? 0,
      target_radio_id: null,
      talkgroup_pk: first.talkgroup_id,
      talkgroup_tgid: first.tgid,
      talkgroup_alias: first.talkgroup_alias,
      result: "accept",
      frequency_hz: null,
      instance: "trunkd-garage",
      meta: null,
    });
  }
  // One private-call grant, on the pair the fixture's two unit calls use, so the radio
  // detail page shows the control-channel event and the recording of it side by side —
  // which is the whole point of keeping the two tables apart (docs/RADIO_EVENTS.md).
  const privateCall = CALLS.find((call) => call.kind === "unit");
  // ``radio_id``/``target_radio_id`` on an event are the numbers the *air* carried, not
  // this archive's primary keys — the call row holds the keys, so both are looked back up.
  const caller = RADIOS.find((row) => row.id === privateCall?.radio_id);
  const callee = RADIOS.find((row) => row.id === privateCall?.target_radio_id);
  if (privateCall && caller && callee) {
    rows.push({
      id: id++,
      at: iso(Date.parse(privateCall.started_at) - 1_500),
      kind: "private_call",
      opcode: 0,
      mfid: 0,
      system_id: 1,
      site_id: 1,
      site_name: "Central",
      radio_pk: privateCall.radio_id,
      radio_id: caller.radio_id,
      radio_wacn: 0,
      radio_system: 0,
      target_radio_id: callee.radio_id,
      talkgroup_pk: null,
      talkgroup_tgid: null,
      talkgroup_alias: null,
      result: "accept",
      frequency_hz: privateCall.frequency_hz,
      instance: "trunkd-garage",
      meta: null,
    });
  }
  return rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
})();

export const AGENCIES: Agency[] = (() => {
  const names: Record<string, string> = {
    XX0010000: "Example County / ECC",
    XX0010100: "River City",
    XX0010200: "Fairview",
    XX0010500: "Example County Ambulance",
    XX0010600: "State University",
    // Two rows the seeded list has no code for, so the Agencies tab and the
    // agency picker have a `local` source and an inactive row to draw. The
    // railroad has no talkgroups mapped, which makes it the one row in the mock
    // data that a delete would actually accept.
    EXAMPLE_RR: "Example Railway",
    OLD_PD: "Fairview Heights PD (disbanded)",
  };
  const local = new Set(["EXAMPLE_RR", "OLD_PD"]);
  return Object.entries(names).map(([code, name]) => {
    const tgs = TALKGROUPS.filter((tg) => tg.agency_code === code);
    return {
      code,
      name,
      short_name: name.split(" ")[0] ?? name,
      kind: tgs[0]?.category ?? null,
      // `talkgroup_ids` are the ids on the air; `talkgroups` are primary keys.
      talkgroup_ids: tgs.map((tg) => tg.tgid),
      talkgroups: tgs.map((tg) => tg.id),
      // The same list on every agency: it is the discoverable set, not a possession.
      shared_talkgroup_ids: TALKGROUPS.filter((tg) => tg.shared).map((tg) => tg.tgid),
      radio_count: RADIOS.filter((radio) => radio.agency_code === code).length,
      call_count: tgs.reduce((sum, tg) => sum + tg.call_count, 0),
      source: local.has(code) ? "local" : "seed",
      active: code !== "OLD_PD",
    };
  });
})();

/**
 * The agency table as the Admin page sees it: no id lists, but the four counts a
 * delete checks. Derived from `AGENCIES` so the two views cannot disagree about
 * which agencies exist.
 */
export const ADMIN_AGENCIES: AdminAgency[] = AGENCIES.map((agency) => ({
  code: agency.code,
  name: agency.name,
  short_name: agency.short_name ?? agency.code,
  kind: agency.kind ?? "police",
  source: agency.source,
  active: agency.active,
  edited_at: agency.source === "local" ? "2026-08-30T14:02:00Z" : null,
  talkgroup_count: agency.talkgroups.length,
  radio_count: agency.radio_count,
  unit_count: UNITS.filter((unit) => unit.agency_code === agency.code).length,
  person_count: PERSONNEL.filter((person) => person.agency_code === agency.code).length,
}));

/* ------------------------------------------------------ field vocabularies */

/**
 * A stand-in for `GET /directory/vocabulary`.
 *
 * The real descriptions live in `backend/app/vocabulary.py` and are a paragraph
 * each; these are one-liners, because the mock exists so the Help page renders
 * with the API down, not so the manual can be read offline. The *shape* and the
 * tag → category mapping are the real ones, which is what the page's layout and
 * the "which tags land here" column are exercised against.
 */
const CATEGORY_GLOSS: Record<TalkgroupCategory, [string, string]> = {
  law: ["Law enforcement", "Police, sheriff and campus-police traffic."],
  fire: ["Fire", "Fire dispatch, fireground and department talk channels."],
  ems: ["EMS", "Ambulance and paramedic traffic, and the hospitals they call."],
  multi: ["Multi-service", "Channels several services share as a matter of course."],
  interop: ["Interoperability", "Mutual-aid channels for agencies who share no system."],
  pw: ["Public works", "Streets, sanitation, parks, snow and county maintenance."],
  utility: ["Utilities", "Water, sewer, gas and electric field crews."],
  transit: ["Transit", "Buses, paratransit and road operations."],
  rail: ["Rail", "Railroad road, yard and dispatcher channels."],
  aviation: ["Aviation", "Airport ground and tower, and air ambulance."],
  schools: ["Schools", "School districts and universities: buses, grounds, athletics."],
  corrections: ["Corrections", "Jail and prison operations, transport and court security."],
  security: ["Security", "Private and institutional security that is not a sworn agency."],
  federal: ["Federal", "Federal civilian agencies."],
  military: ["Military", "Military channels, including the National Guard."],
  media: ["Media", "Broadcast and press crews."],
  business: ["Business", "Commercial licensees: taxis, tow trucks, contractors."],
  ham: ["Amateur radio", "Repeaters and the ARES/RACES nets."],
  data: ["Data", "Telemetry, paging and control traffic rather than voice."],
  other: ["Other", "Anything else, and where an unclassified channel lands."],
};

const TAG_CATEGORY: Record<RrServiceTag, TalkgroupCategory> = {
  Aircraft: "aviation",
  Business: "business",
  Corrections: "corrections",
  Data: "data",
  Deprecated: "other",
  "Emergency Ops": "multi",
  "EMS Dispatch": "ems",
  "EMS-Tac": "ems",
  "EMS-Talk": "ems",
  Federal: "federal",
  "Fire Dispatch": "fire",
  "Fire-Tac": "fire",
  "Fire-Talk": "fire",
  Ham: "ham",
  Hospital: "ems",
  Interop: "interop",
  "Law Dispatch": "law",
  "Law Tac": "law",
  "Law Talk": "law",
  Media: "media",
  Military: "military",
  "Multi-Dispatch": "multi",
  "Multi-Tac": "multi",
  "Multi-Talk": "multi",
  Other: "other",
  "Public Works": "pw",
  Railroad: "rail",
  Schools: "schools",
  Security: "security",
  Transportation: "transit",
  Utilities: "utility",
};

export const VOCABULARY: Vocabulary = {
  categories: TALKGROUP_CATEGORIES.map((key) => ({
    key,
    label: CATEGORY_GLOSS[key][0],
    description: CATEGORY_GLOSS[key][1],
    service_tags: RR_SERVICE_TAGS.filter((tag) => TAG_CATEGORY[tag] === key),
  })),
  category_effect:
    "A category colours the channel's icon, groups the Calls filters and facet counts, is what an alert rule and a webhook carry, and is a column the statistics break volume down by. It does not affect recording, transcription or retention.",
  service_tags: RR_SERVICE_TAGS.map((tag) => ({
    tag,
    category: TAG_CATEGORY[tag],
    category_label: CATEGORY_GLOSS[TAG_CATEGORY[tag]][0],
    description: `RadioReference's tag for ${tag.toLowerCase()} traffic; it lands the channel in ${CATEGORY_GLOSS[TAG_CATEGORY[tag]][0].toLowerCase()}.`,
  })),
  agency_kinds: [
    ["dispatch", "The dispatch centre itself, which owns the county's shared channels."],
    ["police", "A municipal police department."],
    ["sheriff", "A county sheriff's office, including its jail and civil divisions."],
    ["university", "A university's own department — campus police, fire or EMS."],
    ["fire", "A fire department or fire district, volunteer or career."],
    ["ems", "An ambulance service, county-run, hospital-based or private."],
  ].map(([key, description]) => ({
    key: key as string,
    label: (key as string).charAt(0).toUpperCase() + (key as string).slice(1),
    description: description as string,
  })),
  agency_sources: [
    {
      key: "seed",
      label: "Seeded",
      description:
        "Loaded from the agency list this deployment seeds from (keyup seed-agencies). Re-seeding refreshes the name unless somebody here has edited it.",
    },
    {
      key: "local",
      label: "Local",
      description:
        "Added in Keyup for an agency the seeded list has no code for. Yours to rename, deactivate and delete; a re-seed never touches it.",
    },
  ],
};

/* ----------------------------------------------------------- external links */

export const LINKS: ExternalLink[] = CALLS.filter((_, index) => index % 37 === 5)
  .slice(0, 12)
  .map((call, index) => ({
    id: index + 1,
    call_id: call.id,
    conversation_id: call.conversation_id,
    source: "cad",
    external_id: `2026-${String(120000 + index * 37).slice(0, 6)}`,
    link_confidence: index % 3 === 0 ? "strong" : index % 3 === 1 ? "probable" : "weak",
    link_method: index % 2 === 0 ? "time+agency" : "unit",
    link_note: "Matched on dispatch time and agency",
    created_by: "alex",
    created_at: call.ingested_at,
  }));

/* --------------------------------------------------------------- waveform */

/**
 * Peaks for a call. Deterministic in the call id so the same waveform is drawn
 * every time, with a keyed-up envelope: a fast attack, a talkover body and a
 * squelch tail.
 */
export function mockPeaks(callId: number, duration: number): number[] {
  const random = makeRandom(callId * 7919 + 13);
  const count = Math.max(24, Math.min(600, Math.round(duration * 24)));
  return Array.from({ length: count }, (_, i) => {
    const position = i / (count - 1);
    const envelope = Math.min(1, position * 12) * Math.min(1, (1 - position) * 8);
    const syllable = 0.55 + 0.45 * Math.sin(position * duration * 7.5);
    return Number(Math.max(0.02, Math.min(1, envelope * syllable * (0.55 + random() * 0.6))).toFixed(3));
  });
}

/* ------------------------------------------------------------ activity data */

/**
 * The API returns both forms: `cells` (one row per talkgroup × bucket) and the
 * same numbers pivoted into `rows`/`buckets`/`matrix`. The fixture builds the
 * pivot first and derives the cells, so the two can never disagree.
 */
function cellsFrom(
  rows: string[],
  buckets: string[],
  matrix: number[][],
  talkgroups: Talkgroup[] | null,
): ActivityCell[] {
  const cells: ActivityCell[] = [];
  rows.forEach((row, r) => {
    buckets.forEach((bucket, b) => {
      const calls = matrix[r]?.[b] ?? 0;
      if (calls === 0) return;
      const tg = talkgroups?.[r] ?? null;
      cells.push({
        talkgroup: tg?.id ?? null,
        tgid: tg?.tgid ?? null,
        alias: tg ? talkgroupLabel(tg) : (row ?? null),
        bucket: `${row}T${bucket}:00:00`,
        calls,
        seconds: Number((calls * 2.4).toFixed(3)),
      });
    });
  });
  return cells;
}

/** `matrix[day][hour]` counts for the whole corpus. */
export function activityByDayHour(): ActivityResponse {
  const days = new Map<string, number[]>();
  for (const call of CALLS) {
    const day = call.started_local.slice(0, 10);
    const hour = Number(call.started_local.slice(11, 13));
    const row = days.get(day) ?? new Array(24).fill(0);
    row[hour] = (row[hour] ?? 0) + 1;
    days.set(day, row);
  }
  const rows = [...days.keys()].sort();
  const matrix = rows.map((day) => days.get(day) as number[]);
  const buckets = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"));
  return {
    bucket: "hour",
    cells: cellsFrom(rows, buckets, matrix, null),
    buckets,
    rows,
    matrix,
    max: Math.max(1, ...matrix.flat()),
  };
}

/** `matrix[talkgroup][hour]` — the heatmap's other grouping. */
export function activityByTalkgroupHour(): ActivityResponse {
  const rows = TALKGROUPS.filter((tg) => tg.call_count > 0);
  const matrix = rows.map((tg) => {
    const row = new Array(24).fill(0);
    for (const call of CALLS) {
      if (!call.talkgroup_ids.includes(tg.tgid)) continue;
      const hour = Number(call.started_local.slice(11, 13));
      row[hour] += 1;
    }
    return row;
  });
  const buckets = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"));
  const labels = rows.map((tg) => talkgroupLabel(tg));
  return {
    bucket: "hour",
    cells: cellsFrom(labels, buckets, matrix, rows),
    buckets,
    rows: labels,
    matrix,
    max: Math.max(1, ...matrix.flat()),
  };
}

/** The operational activity log — one or two rows per recent call (Admin → Activity). */
export function activityLog(): ActivityEvent[] {
  const recent = [...CALLS].sort((a, b) => b.started_at.localeCompare(a.started_at)).slice(0, 60);
  const rows: ActivityEvent[] = [];
  let id = recent.length * 2 + 1;
  for (const call of recent) {
    const label = call.talkgroup_alias ?? String(call.talkgroup_ids?.[0] ?? "?");
    const source = call.ingest_source === "upload" || call.ingest_source === "trunkd" ? "upload" : "ingest";
    rows.push({
      id: id--,
      at: call.started_at,
      source,
      level: "info",
      kind: "call.accepted",
      message: `Example Network ${label} accepted (${call.duration_s.toFixed(1)}s)`,
      call_id: call.id,
      meta: { talkgroup: label, had_audio: call.audio_state === "present" },
    });
    if (call.transcript_status === "done") {
      rows.push({
        id: id--,
        at: new Date(new Date(call.started_at).getTime() + 8000).toISOString(),
        source: "transcribe",
        level: "info",
        kind: "transcribe.done",
        message: `transcribed call ${call.id} via faster_whisper_local (done)`,
        call_id: call.id,
        meta: { engine: "faster_whisper_local", status: "done" },
      });
    } else if (call.transcript_status === "failed") {
      rows.push({
        id: id--,
        at: new Date(new Date(call.started_at).getTime() + 8000).toISOString(),
        source: "transcribe",
        level: "error",
        kind: "transcribe.failed",
        message: `transcription failed for call ${call.id}: TimeoutError`,
        call_id: call.id,
        meta: { engine: "faster_whisper_local", error: "TimeoutError" },
      });
    }
  }
  return rows.sort((a, b) => b.id - a.id);
}

/**
 * `GET /api/status/activity` — the Live activity panel's snapshot.
 *
 * Built at call time rather than frozen at module load so the relative times on the
 * heartbeats and the ticker are always fresh: a demo whose recorder was last heard from
 * "3 hours ago" looks broken, and a broken-looking demo teaches the wrong thing about
 * what the colours mean. The numbers are deliberately healthy with one wart — a second
 * recorder that has gone quiet — because a panel nobody has ever seen go amber is a
 * panel nobody trusts when it does.
 */
export function liveActivity(windowSeconds: number): LiveActivitySnapshot {
  const now = Date.now();
  const iso = (ms: number) => new Date(ms).toISOString();
  const scale = windowSeconds / 300;
  const pair = (current: number, previous: number) => ({
    current: Math.round(current * scale),
    previous: Math.round(previous * scale),
  });
  const lastCall = now - 9_000;
  return {
    window_s: windowSeconds,
    server_time: iso(now),
    window_start: iso(now - windowSeconds * 1000),
    previous_window_start: iso(now - 2 * windowSeconds * 1000),
    counters: {
      ingested: pair(34, 29),
      duplicates_merged: pair(6, 4),
      speech: pair(24, 21),
      pages: pair(2, 1),
      no_speech: pair(5, 6),
      static: pair(3, 1),
      audio_processed: pair(33, 28),
      audio_discarded: pair(4, 4),
      transcribed: pair(31, 27),
      transcripts_rejected: pair(1, 0),
    },
    exports: {
      broadcastify: {
        uploaded: pair(21, 19),
        skipped_duplicate: pair(6, 4),
        skipped_no_speech: pair(5, 6),
        skipped_static: pair(3, 1),
        skipped_late: pair(1, 0),
        claimed_by_recorder: pair(0, 0),
        skipped_other: pair(0, 0),
        failed: pair(0, 1),
        pending: { current: 2, previous: 2 },
      },
      openmhz: {
        uploaded: pair(22, 20),
        skipped_duplicate: pair(6, 4),
        skipped_no_speech: pair(5, 6),
        skipped_static: pair(3, 1),
        skipped_late: pair(0, 0),
        claimed_by_recorder: pair(0, 0),
        skipped_other: pair(0, 0),
        failed: pair(0, 0),
        pending: { current: 1, previous: 1 },
      },
    },
    webhooks: { delivered: pair(8, 7), failed: pair(0, 0), pending: 0 },
    queues: { audio_queued: 2, transcription_queued: 5, exports_pending: 3 },
    recorders: [
      {
        instance: "signal",
        label: "Signal box",
        registered: true,
        last_seen_at: iso(now - 4_000),
        seconds_since_last_seen: 4,
        calls_in_window: Math.round(30 * scale),
        last_call_at: iso(lastCall),
        seconds_since_last_call: 9,
        // The box on the LAN: its monitor answers from anywhere in the house.
        monitor: {
          enabled: true,
          url: "http://192.0.2.30:8095",
          stream_url: "http://192.0.2.30:8095/api/monitor/stream",
          token_required: false,
          local_only: false,
        },
      },
      {
        instance: "attic",
        label: "Attic spare",
        registered: true,
        last_seen_at: iso(now - 20_000),
        seconds_since_last_seen: 20,
        calls_in_window: Math.round(4 * scale),
        last_call_at: iso(now - 190_000),
        seconds_since_last_call: 190,
        // An older build with no panel of its own — the row simply offers no link.
        monitor: null,
      },
    ],
    last_call_at: iso(lastCall),
    seconds_since_last_call: 9,
    recent: activityLog().slice(0, 30),
    recent_visible: true,
  };
}

/** 24 hourly counts ending now — the sparkline in the directory tables. */
export function sparkFor(predicate: (call: Call) => boolean): number[] {
  const buckets = new Array(24).fill(0);
  const end = Date.now();
  for (const call of CALLS) {
    if (!predicate(call)) continue;
    const hoursAgo = Math.floor((end - Date.parse(call.started_at)) / 3_600_000);
    if (hoursAgo < 0 || hoursAgo > 23) continue;
    buckets[23 - hoursAgo] += 1;
  }
  return buckets;
}

/* ------------------------------------------------------------- operations */

export const INGEST_RUNS: IngestRun[] = Array.from({ length: 14 }, (_, i) => {
  const started = NOW - i * 5 * 60_000 - 30_000;
  const seen = intBetween(4, 40);
  const failed = chance(0.15) ? intBetween(1, 2) : 0;
  const duplicate = intBetween(0, 2);
  const created = Math.max(0, seen - failed - duplicate);
  return {
    id: 500 - i,
    kind: i % 5 === 0 ? "scan" : "watch",
    root: "/data/audio",
    started_at: iso(started),
    finished_at: iso(started + intBetween(400, 9000)),
    files_seen: seen,
    files_new: created,
    files_duplicate: duplicate,
    files_skipped: Math.max(0, seen - created - duplicate - failed),
    files_failed: failed,
    errors: failed ? [{ path: "20260829/…/truncated.mp3", error: "mp3 header unreadable" }] : [],
  };
});

export const INGEST_FILES: IngestFile[] = [
  ...Array.from({ length: 7 }, (_, i) => ({
    id: 900 + i,
    path: `20260829/EC FIRE DISPATCH/20260829_1${i}3012_Example-Network_Central_T-Control__TO_1101_FROM_5226001.mp3`,
    status: "failed" as const,
    error: i % 2 === 0 ? "mp3 header unreadable: truncated frame" : "ID3 TIT2 missing talkgroup id",
    call_id: null,
    attempts: intBetween(1, 3),
    first_seen_at: iso(NOW - i * 900_000 - 60_000),
    last_attempt_at: iso(NOW - i * 900_000),
  })),
  ...Array.from({ length: 5 }, (_, i) => ({
    id: 950 + i,
    path: `20260829/RC LAW DISPATCH/20260829_0${i}1122_Example-Network_Central_T-Control__TO_50051_V2.mp3`,
    status: "duplicate" as const,
    error: null,
    call_id: (CALLS[i] as MockCall).id,
    attempts: 1,
    first_seen_at: iso(NOW - i * 1_800_000 - 60_000),
    last_attempt_at: iso(NOW - i * 1_800_000),
  })),
];

export const JOBS: TranscriptionJob[] = CALLS.slice(0, 40).map((call, i) => ({
  id: 7000 + i,
  call_id: call.id,
  talkgroup_alias: call.talkgroup_alias,
  status: i < 3 ? "running" : i < 18 ? "queued" : i < 34 ? "done" : "failed",
  priority: TALKGROUPS.find((tg) => tg.id === call.talkgroup_id)?.priority ?? 3,
  backend_name: i % 3 === 0 ? "local-cpu" : "whisper",
  model: i % 3 === 0 ? "small" : "large-v3-turbo",
  attempts: i >= 34 ? 3 : 1,
  max_attempts: 3,
  last_error: i >= 34 ? "backend timeout after 120s" : null,
  scheduled_at: call.ingested_at,
  started_at: i < 34 ? call.ingested_at : null,
  finished_at: i >= 18 && i < 34 ? iso(Date.parse(call.ingested_at ?? "") + 4200) : null,
  locked_by: i < 3 ? "worker-1" : null,
}));

export const BACKENDS: SttBackend[] = [
  {
    name: "whisper",
    engine: "openai_compatible",
    model: "large-v3-turbo",
    base_url: "http://whisper:8000/v1",
    enabled: true,
    priority: 1,
    concurrency: 4,
    healthy: true,
    detail: "3 model(s) installed",
    last_error: null,
    rtf: 0.11,
    queue_depth: 12,
    calls_24h: 318,
  },
  {
    name: "local-cpu",
    engine: "faster_whisper_local",
    model: "small",
    base_url: null,
    enabled: true,
    priority: 2,
    concurrency: 2,
    healthy: true,
    detail: "faster-whisper small, int8, 4 threads",
    last_error: null,
    rtf: 1.42,
    queue_depth: 3,
    calls_24h: 71,
  },
  {
    name: "gpu-spare",
    engine: "openai_compatible",
    model: "large-v3",
    base_url: "http://192.0.2.72:8000/v1",
    enabled: false,
    priority: 3,
    concurrency: 8,
    healthy: false,
    detail: "disabled",
    last_error: "connect ECONNREFUSED 192.0.2.72:8000",
    rtf: null,
    queue_depth: 0,
    calls_24h: 0,
  },
];

/** Ages for the backend rows, relative to the fixtures' "now". */
const secondsAgo = (seconds: number): string => iso(NOW - seconds * 1000);
const hoursAgo = (hours: number): string => iso(NOW - hours * 3_600_000);

/**
 * What the transcription worker publishes about itself: one busy backend, one that has
 * started erroring, and one the operator has switched off.  The third row is the state the
 * page has to distinguish — off by hand, with the configuration still saying "on".
 */
export const STT_BACKEND_STATS: SttBackendStat[] = [
  {
    name: "whisper",
    kind: "openai_compatible",
    model: "large-v3-turbo",
    base_url: "http://whisper:8000/v1",
    priority: 10,
    concurrency: 4,
    enabled: true,
    config_enabled: true,
    admin_enabled: null,
    admin_changed_at: null,
    admin_changed_by: null,
    healthy: true,
    health_detail: "3 model(s) installed",
    disqualified: null,
    in_flight: 2,
    attempts: 4181,
    successes: 4168,
    errors: 13,
    consecutive_errors: 0,
    last_error: "TranscriptionError: 502 Bad Gateway from http://whisper:8000/v1",
    last_error_at: hoursAgo(6),
    last_success_at: secondsAgo(4),
    audio_s: 61240.5,
    processing_s: 6702.1,
    mean_rtf: 9.14,
    mean_processing_s: 1.61,
    recent_completed_1m: 21,
    recent_failed_1m: 0,
    worker_instance: "keyup-worker:1",
    started_at: hoursAgo(31),
    updated_at: secondsAgo(3),
    stale: false,
    has_api_key: false,
    config_source: "db",
    device: null,
    compute_type: null,
    cpu_threads: null,
    cache_dir: null,
    use_prompt: true,
    use_hotwords: false,
    timeout: 300,
    notes: null,
    deleted_at: null,
    editable: true,
    config_version: 4,
  },
  {
    name: "whisper-npu",
    kind: "openvino_genai",
    model: "whisper-large-v3-turbo-int8-ov",
    base_url: null,
    priority: 50,
    concurrency: 1,
    enabled: true,
    config_enabled: true,
    admin_enabled: null,
    admin_changed_at: null,
    admin_changed_by: null,
    healthy: true,
    health_detail: "NPU, compiled blob cached",
    disqualified: null,
    in_flight: 0,
    attempts: 812,
    successes: 799,
    errors: 13,
    consecutive_errors: 4,
    last_error: "TranscriptionError: NPU inference request timed out after 300s",
    last_error_at: secondsAgo(95),
    last_success_at: secondsAgo(310),
    audio_s: 9120.0,
    processing_s: 3801.2,
    mean_rtf: 2.4,
    mean_processing_s: 4.76,
    recent_completed_1m: 0,
    recent_failed_1m: 2,
    worker_instance: "keyup-worker:1",
    started_at: hoursAgo(31),
    updated_at: secondsAgo(3),
    stale: false,
    has_api_key: false,
    config_source: "db",
    device: null,
    compute_type: null,
    cpu_threads: null,
    cache_dir: null,
    use_prompt: true,
    use_hotwords: false,
    timeout: 300,
    notes: null,
    deleted_at: null,
    editable: true,
    config_version: 4,
  },
  {
    name: "gpu-spare",
    kind: "openai_compatible",
    model: "large-v3",
    base_url: "http://192.0.2.72:8000/v1",
    priority: 90,
    concurrency: 8,
    enabled: false,
    config_enabled: true,
    admin_enabled: false,
    admin_changed_at: hoursAgo(2),
    admin_changed_by: "alex",
    healthy: null,
    health_detail: null,
    disqualified: null,
    in_flight: 0,
    attempts: 0,
    successes: 0,
    errors: 0,
    consecutive_errors: 0,
    last_error: null,
    last_error_at: null,
    last_success_at: null,
    audio_s: 0,
    processing_s: 0,
    mean_rtf: null,
    mean_processing_s: null,
    recent_completed_1m: 0,
    recent_failed_1m: 0,
    worker_instance: "keyup-worker:1",
    started_at: hoursAgo(31),
    updated_at: secondsAgo(3),
    stale: false,
    has_api_key: false,
    config_source: "db",
    device: null,
    compute_type: null,
    cpu_threads: null,
    cache_dir: null,
    use_prompt: true,
    use_hotwords: false,
    timeout: 300,
    notes: null,
    deleted_at: null,
    editable: true,
    config_version: 4,
  },
];

export const STT_QUEUE: SttQueue = {
  queued_live: 12,
  queued_backlog: 4310,
  running: 2,
  completed_1h: 1240,
  failed_1h: 6,
  mean_processing_s: 1.9,
  throughput_per_min: 20.6,
  audio_seconds_queued: 41280.0,
  eta_seconds: 12600.0,
};

export const RETENTION_POLICIES: RetentionPolicy[] = [
  {
    id: 1,
    scope: "global",
    scope_id: null,
    scope_label: "All systems",
    keep_days: 365,
    keep_audio_days: 90,
    keep_transcripts: true,
    archive_path: null,
    enabled: true,
  },
  {
    id: 2,
    scope: "talkgroup",
    scope_id: 20,
    scope_label: "EC SCHOOLS BUS OPS",
    keep_days: 30,
    keep_audio_days: 14,
    keep_transcripts: false,
    archive_path: null,
    enabled: true,
  },
  {
    id: 3,
    scope: "talkgroup",
    scope_id: 1,
    scope_label: "EC FIRE DISPATCH",
    // 0 is how the API spells "keep forever".
    keep_days: 0,
    keep_audio_days: 730,
    keep_transcripts: true,
    archive_path: "/archive/fire",
    enabled: true,
  },
];

export const GAPS: RecorderGap[] = [
  {
    id: 1,
    system_id: 1,
    system_name: "Example Network",
    started_at: iso(NOW - 26 * 3600_000),
    ended_at: iso(NOW - 25.4 * 3600_000),
    duration_s: 36 * 60,
    minutes: 36,
    note: null,
    detected_at: iso(NOW - 25 * 3600_000),
  },
  {
    id: 2,
    system_id: 1,
    system_name: "Example Network",
    started_at: iso(NOW - 41 * 3600_000),
    ended_at: iso(NOW - 40.8 * 3600_000),
    duration_s: 12 * 60,
    minutes: 12,
    note: "recorder restarted",
    detected_at: iso(NOW - 40 * 3600_000),
  },
];

/** `GET /admin/audio` — the Audio tab's numbers (§5.5). */
export const AUDIO_STATUS: AudioStatus = {
  enabled: true,
  version: 1,
  reprocess_version: 1,
  jobs: { queued: 12, running: 1, done: 41_208, failed: 3 },
  processed: 41_208,
  pending: 137,
  failed: 3,
  tone_today: 18,
  alert_today: 6,
  average_gain_db: 11.4,
};

/**
 * `GET /admin/audio/profiles/steps` — the filter whitelist, copied from the server's own
 * `STEP_KINDS` table so the demo's editor draws exactly the fields the real one draws.
 */
export const AUDIO_STEP_CATALOG: AudioProfileCatalog = {
  max_steps: 12,
  steps: [
    {
      kind: "highpass",
      label: "High-pass",
      help: "Rolls off everything below the corner. 250–300 Hz removes engine rumble, handling thumps and rotor beat without touching speech, which starts at the male fundamental near 85 Hz but carries almost no intelligibility below 300.",
      params: [
        {
          name: "f",
          low: 20,
          high: 20000,
          default: 300,
          unit: "Hz",
          help: "Corner frequency",
          optional: false,
        },
      ],
    },
    {
      kind: "lowpass",
      label: "Low-pass",
      help: "Rolls off everything above the corner. This is a two-pole filter, so the corner is where it is already 3 dB down and about 2 dB down 400 Hz below that: a P25 vocoder carries consonant energy to roughly 3.7 kHz, so 3400 is inside the voice and the global chain uses 3800 (measured: 3400 costs 3.35 dB of the 2.5-3.8 kHz band, 3800 costs 0.01).",
      params: [
        {
          name: "f",
          low: 20,
          high: 20000,
          default: 3000,
          unit: "Hz",
          help: "Corner frequency",
          optional: false,
        },
      ],
    },
    {
      kind: "bandreject",
      label: "Band reject",
      help: "Removes a band outright (`bandreject`, a two-pole notch). Use it when the noise occupies a band and nothing you want is inside it; when speech shares the band, prefer a deep `equalizer` cut, which attenuates rather than removes.",
      params: [
        {
          name: "f",
          low: 20,
          high: 20000,
          default: 1950,
          unit: "Hz",
          help: "Center frequency",
          optional: false,
        },
        {
          name: "width",
          low: 10,
          high: 10000,
          default: 500,
          unit: "Hz",
          help: "Band width",
          optional: false,
        },
      ],
    },
    {
      kind: "equalizer",
      label: "Band gain",
      help: "A peaking filter over one band: negative gain cuts it, positive lifts it. This is the aircraft fix — `f=1950 width=500 gain_db=-40` is a 40 dB hole from 1.7 to 2.2 kHz, which takes the rotor and turbine band out and leaves the voice above and below it audible.",
      params: [
        {
          name: "f",
          low: 20,
          high: 20000,
          default: 1950,
          unit: "Hz",
          help: "Center frequency",
          optional: false,
        },
        {
          name: "width",
          low: 10,
          high: 10000,
          default: 500,
          unit: "Hz",
          help: "Band width",
          optional: false,
        },
        {
          name: "gain_db",
          low: -60,
          high: 20,
          default: -40,
          unit: "dB",
          help: "Gain inside the band",
          optional: false,
        },
      ],
    },
    {
      kind: "notch",
      label: "Notch",
      help: "A narrow `equalizer` cut placed by Q rather than by width — for a single steady whistle (a data carrier, a squelch tail, a rotor harmonic) whose frequency you can read off a spectrum but whose band is one line wide.",
      params: [
        {
          name: "f",
          low: 20,
          high: 20000,
          default: 1000,
          unit: "Hz",
          help: "Center frequency",
          optional: false,
        },
        {
          name: "q",
          low: 0.1,
          high: 100,
          default: 10,
          unit: "",
          help: "Q — higher is narrower",
          optional: false,
        },
        {
          name: "gain_db",
          low: -60,
          high: 0,
          default: -40,
          unit: "dB",
          help: "Depth of the cut",
          optional: false,
        },
      ],
    },
    {
      kind: "afftdn",
      label: "FFT denoise",
      help: "Spectral noise reduction. `nf` is the assumed noise floor in dB — lower is more aggressive — and `nr` how many dB of it to take out. The global chain runs `nf=-35:nr=18` behind a 300-3800 Hz band-pass, which is the combination measured best on this system's audio; a channel with its own band edges takes the same denoiser well. `nf` is where the filter *thinks* the noise is, not how hard it pushes: measured floors here are a median -63 dBFS, and every `nf` from -30 to -45 removes the same 18 dB of noise while the too-loud end of that range takes 2-3 dB of the 2.5-3.8 kHz speech with it.",
      params: [
        { name: "nf", low: -80, high: -20, default: -25, unit: "dB", help: "Noise floor", optional: false },
        {
          name: "nr",
          low: 0.01,
          high: 97,
          default: null,
          unit: "dB",
          help: "Reduction (optional)",
          optional: true,
        },
      ],
    },
    {
      kind: "anlmdn",
      label: "Non-local means denoise",
      help: "**Not recommended on this system.** Measured against 20 calls of vocoder audio it *raised* the noise floor 8-9 dB (radio 5233201, Δsnr -6.05) and did nothing on the next radio tested (+0.35): non-local means wants a broadband hiss with structure to average over, and what a P25 codec emits is not that. For hiss, use a 300-3800 Hz band-pass with `afftdn nf=-35 nr=18` instead. Kept selectable for a channel with a different problem. `s` is the strength, `p` the patch duration and `r` the research duration, the last two in seconds. All three are optional.",
      params: [
        { name: "s", low: 1e-5, high: 10, default: null, unit: "", help: "Strength", optional: true },
        {
          name: "p",
          low: 0.001,
          high: 0.1,
          default: null,
          unit: "s",
          help: "Patch duration",
          optional: true,
        },
        {
          name: "r",
          low: 0.002,
          high: 0.3,
          default: null,
          unit: "s",
          help: "Research duration",
          optional: true,
        },
      ],
    },
    {
      kind: "agate",
      label: "Noise gate",
      help: "Ducks everything under the threshold, so a 30 dB lift on a quiet talker does not turn the hiss between his words into a roar. The threshold is the dBFS an operator reads off the meter; it is converted to the linear value ffmpeg wants.",
      params: [
        {
          name: "threshold",
          low: -100,
          high: 0,
          default: -50,
          unit: "dBFS",
          help: "Gate opens above this",
          optional: false,
        },
        {
          name: "ratio",
          low: 1,
          high: 9000,
          default: 4,
          unit: "",
          help: "Ducking ratio below it",
          optional: false,
        },
        { name: "attack", low: 0.01, high: 9000, default: 5, unit: "ms", help: "Attack", optional: false },
        {
          name: "release",
          low: 0.01,
          high: 9000,
          default: 250,
          unit: "ms",
          help: "Release",
          optional: false,
        },
      ],
    },
  ],
};

/**
 * `GET /admin/audio/profiles` — the three shipped profiles, with the chains and the
 * rendered strings `backend/tests/test_audio_profiles.py` pins byte for byte.
 *
 * "Aircraft cabin" carries one channel, Example Network, because the demo should show what
 * an assigned profile looks like as well as an unassigned one.
 */
export const AUDIO_PROFILES: AudioProfile[] = [
  {
    id: 1,
    name: "Default",
    description:
      "The shipped chain, as data: the 300-3800 Hz speech band-pass, FFT denoise at a -35 dB noise floor taking 18 dB out, then the -50 dBFS gate. What a talkgroup with no profile gets from the global settings.",
    chain: [
      { kind: "highpass", params: { f: 300 } },
      { kind: "lowpass", params: { f: 3800 } },
      { kind: "afftdn", params: { nf: -35, nr: 18 } },
      { kind: "agate", params: { threshold: -50, ratio: 4, attack: 5, release: 250 } },
    ],
    builtin: true,
    rendered:
      "highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18,agate=threshold=0.003162:ratio=4:attack=5:release=250,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50",
    talkgroup_count: 0,
    created_at: iso(NOW - 30 * 86_400_000),
    updated_at: iso(NOW - 30 * 86_400_000),
  },
  {
    id: 2,
    name: "Aircraft cabin",
    description:
      "Rotor and turbine noise: a 40 dB hole from 1.7 to 2.2 kHz over a 250 Hz high-pass, with a harder denoise behind it. Written for the medevac transports on Example Network, where the global chain leaves the beat audible and anything aggressive enough to remove it eats the speech.",
    chain: [
      { kind: "highpass", params: { f: 250 } },
      { kind: "equalizer", params: { f: 1950, width: 500, gain_db: -40 } },
      { kind: "afftdn", params: { nf: -28, nr: 12 } },
    ],
    builtin: true,
    rendered:
      "highpass=f=250,equalizer=f=1950:w=500:g=-40:t=h,afftdn=nf=-28:nr=12,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50",
    talkgroup_count: 1,
    created_at: iso(NOW - 30 * 86_400_000),
    updated_at: iso(NOW - 30 * 86_400_000),
  },
  {
    id: 3,
    name: "Speech band-pass",
    description:
      "The telephone band and nothing else: 300 Hz to 3 kHz, then a denoise. For a channel whose noise sits at both ends of the spectrum rather than in one band.",
    chain: [
      { kind: "highpass", params: { f: 300 } },
      { kind: "lowpass", params: { f: 3000 } },
      { kind: "afftdn", params: { nf: -25 } },
    ],
    builtin: true,
    rendered:
      "highpass=f=300,lowpass=f=3000,afftdn=nf=-25,loudnorm=I=-16:TP=-4:LRA=11,alimiter=limit=0.630957:level=disabled:attack=5:release=50",
    talkgroup_count: 0,
    created_at: iso(NOW - 30 * 86_400_000),
    updated_at: iso(NOW - 30 * 86_400_000),
  },
];

export const SETTINGS: SettingDefinition[] = [
  // The Broadcastify feed-owner settings (docs/BCFY_STATS.md). The demo has a feed
  // id and a username so the page is not stuck on its "nothing configured" state,
  // and the password is `secret` — its value is "" here exactly as the real API
  // returns it, with `is_default: false` saying one is nevertheless stored.
  {
    key: "bcfy_feed_id",
    value: "2211",
    kind: "string",
    options: null,
    group: "Feed stats",
    label: "Feed id",
    description: "The numeric feed id — the number in the feed's broadcastify.com URL.",
    default_value: "",
    is_default: false,
  },
  {
    key: "bcfy_owner_username",
    value: "operator",
    kind: "string",
    options: null,
    group: "Feed stats",
    label: "Broadcastify username",
    description: "The Broadcastify account that owns the feed.",
    default_value: "",
    is_default: false,
  },
  {
    key: "bcfy_owner_password",
    value: "",
    kind: "secret",
    options: null,
    group: "Feed stats",
    label: "Broadcastify password",
    description: "Write-only: stored, never shown again. Leaving it blank on save keeps the existing one.",
    default_value: "",
    is_default: false,
  },
  {
    key: "bcfy_stats_enabled",
    value: "true",
    kind: "bool",
    options: null,
    group: "Feed stats",
    label: "Poll feed statistics",
    description: "Ask the feed-owner API how the feed is doing, every poll interval.",
    default_value: "false",
    is_default: false,
  },
  {
    key: "bcfy_stats_poll_seconds",
    value: "60",
    kind: "float",
    options: null,
    group: "Feed stats",
    label: "Poll interval",
    description: "Seconds between two polls.",
    default_value: "60.0",
    is_default: true,
  },
  {
    key: "bcfy_public_page_poll_seconds",
    value: "3600",
    kind: "float",
    options: null,
    group: "Feed stats",
    label: "Public page interval",
    description:
      "Seconds between two fetches of the feed's public listen page, whose 24-hour sparkline is the only listener history Broadcastify publishes. 0 turns it off.",
    default_value: "3600.0",
    is_default: true,
  },
  {
    key: "bcfy_archives_sync_enabled",
    value: "true",
    kind: "bool",
    options: null,
    group: "Feed stats",
    label: "Sync archive listings",
    description: "Fetch the days the feed has archives for, and the last week's blocks.",
    default_value: "false",
    is_default: false,
  },
  {
    key: "bcfy_archives_sync_hours",
    value: "6",
    kind: "float",
    options: null,
    group: "Feed stats",
    label: "Archive sync interval",
    description: "Hours between two archive-listing syncs.",
    default_value: "6.0",
    is_default: true,
  },
  {
    key: "bcfy_archives_backfill_days_per_sync",
    value: "30",
    kind: "int",
    options: null,
    group: "Feed stats",
    label: "Archive backfill per sync",
    description: "How many older days each sync catches up on, beyond the last week it always refreshes.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "audio_process_enabled",
    value: "true",
    kind: "bool",
    options: null,
    group: "Audio",
    label: "Process stored audio",
    description: "Store a processed copy of every call — trimmed, denoised, levelled — beside the original.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "audio_target_lufs",
    value: "-16",
    kind: "float",
    options: null,
    group: "Audio",
    label: "Loudness target (LUFS)",
    description: "Where every call is normalized to, so one is as loud as the next.",
    default_value: "-16",
    is_default: true,
  },
  {
    key: "audio_tone_handling",
    value: "attenuate",
    kind: "enum",
    options: ["keep", "attenuate", "remove"],
    group: "Audio",
    label: "What to do with tones",
    description: "Turn detected page tones down, cut them out, or leave them alone.",
    default_value: "attenuate",
    is_default: true,
  },
  // The rest of the settings table: every group without a page of its own, which is
  // what Admin → Settings renders. Keys, kinds and groups match `SETTINGS_SPEC` so the
  // demo exercises the real sub-section layout rather than a made-up one.
  {
    key: "transcribe_default",
    value: "true",
    kind: "bool",
    options: null,
    group: "Transcription",
    label: "Transcribe new talkgroups",
    description: "Whether a talkgroup with no policy of its own has its calls queued for transcription.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "transcribe_min_seconds",
    value: "1.5",
    kind: "float",
    options: null,
    group: "Transcription",
    label: "Minimum duration",
    description: "Recordings shorter than this many seconds are squelch tails and are never queued.",
    default_value: "1.0",
    is_default: false,
  },
  {
    key: "transcribe_max_attempts",
    value: "3",
    kind: "int",
    options: null,
    group: "Transcription",
    label: "Max attempts",
    description: "How many times a job is retried after a backend error before it is parked as failed.",
    default_value: "3",
    is_default: true,
  },
  {
    key: "stt_vad",
    value: "true",
    kind: "bool",
    options: null,
    group: "Transcription",
    label: "Voice activity detection",
    description:
      "Ask the engine to skip non-speech, so silence and static are not turned into hallucinated words.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "stt_language",
    value: "en",
    kind: "string",
    options: null,
    group: "Transcription",
    label: "Language",
    description: "Language hint (an ISO code such as `en`) so the engine does not waste time detecting it.",
    default_value: "",
    is_default: false,
  },
  {
    key: "stt_prompt_extra",
    value: "",
    kind: "text",
    options: null,
    group: "Transcription",
    label: "Extra prompt",
    description:
      "Free text appended to the auto-built domain prompt, to bias decoding toward local vocabulary.",
    default_value: "",
    is_default: true,
  },
  {
    key: "stt_places",
    value: "Riverton, Lakeside, North Fork",
    kind: "text",
    options: null,
    group: "Transcription",
    label: "Place names",
    description:
      "Comma-separated place names heard on this system, added to the domain prompt and the hotword list.",
    default_value: "",
    is_default: false,
  },
  {
    key: "stt_hallucination_patterns",
    value: "",
    kind: "text",
    options: null,
    group: "Transcription",
    label: "Hallucination patterns",
    description:
      "Extra regular expressions, one per line, that mark a transcript as a hallucination; merged with the built-in caption-credit patterns.",
    default_value: "",
    is_default: true,
  },
  {
    key: "stt_word_timestamps",
    value: "false",
    kind: "bool",
    options: null,
    group: "Transcription",
    label: "Word timestamps",
    description: "Ask for per-word start and end times where the backend supports them.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "stt_normalize",
    value: "true",
    kind: "bool",
    options: null,
    group: "Transcription",
    label: "Normalize loudness",
    description:
      "Even out loudness before transcription so alert tones and soft talkers land at a usable level.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "stt_normalize_always",
    value: "false",
    kind: "bool",
    options: null,
    group: "Transcription",
    label: "Normalize twice",
    description: "Run the loudness pass even when the call already has a processed copy from the Audio tab.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "stt_normalize_filter",
    value: "",
    kind: "string",
    options: null,
    group: "Transcription",
    label: "Normalize filter",
    description: "The exact ffmpeg `-af` filter used for that pass. Blank uses the built-in default.",
    default_value: "",
    is_default: true,
  },
  {
    key: "dedupe_window_seconds",
    value: "2.0",
    kind: "float",
    options: null,
    group: "Ingest",
    label: "Dedupe window",
    description: "How far apart two recordings' start times may be and still be matched as one transmission.",
    default_value: "2.0",
    is_default: true,
  },
  {
    key: "dedupe_duration_tolerance",
    value: "0.1",
    kind: "float",
    options: null,
    group: "Ingest",
    label: "Dedupe duration tolerance",
    description: "How much two copies' durations may differ and still count as one call, as a fraction.",
    default_value: "0.1",
    is_default: true,
  },
  {
    key: "reconcile_enabled",
    value: "true",
    kind: "bool",
    options: null,
    group: "Ingest",
    label: "Reconcile recorders",
    description:
      "When two recorders capture the same transmission, store both copies and show the better one (longer, complete audio) as the call; the other is kept as a hidden duplicate. Turn off to go back to first copy wins: a second copy is rejected as a duplicate and its audio is not kept.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "reconcile_clock_skew_seconds",
    value: "2.0",
    kind: "float",
    options: null,
    group: "Ingest",
    label: "Reconcile clock skew",
    description: "How far two recorders' clocks may disagree before their call intervals are lined up.",
    default_value: "2.0",
    is_default: true,
  },
  {
    key: "reconcile_min_overlap",
    value: "0.5",
    kind: "float",
    options: null,
    group: "Ingest",
    label: "Reconcile overlap",
    description: "The fraction of the shorter recording that must overlap the other to be judged one call.",
    default_value: "0.5",
    is_default: true,
  },
  {
    key: "conversation_gap_seconds",
    value: "90",
    kind: "int",
    options: null,
    group: "Ingest",
    label: "Conversation gap",
    description: "Calls on one talkgroup closer together than this are grouped into a single conversation.",
    default_value: "120",
    is_default: false,
  },
  {
    key: "gap_minutes",
    value: "30",
    kind: "int",
    options: null,
    group: "Ingest",
    label: "Recorder gap",
    description:
      "Silence on a monitored talkgroup longer than this is flagged as a possible recorder outage.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "arbiter_enabled",
    value: "false",
    kind: "bool",
    options: null,
    group: "Recorders",
    label: "Promote a standby recorder automatically",
    description:
      "When every hot recorder covering a site stops reporting, enable a warm standby's tuners for it.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "arbiter_stale_seconds",
    value: "60",
    kind: "int",
    options: null,
    group: "Recorders",
    label: "Recorder considered down after",
    description: "Seconds without a status report before the arbiter treats a recorder as down.",
    default_value: "60",
    is_default: true,
  },
  {
    key: "arbiter_recovery_seconds",
    value: "300",
    kind: "int",
    options: null,
    group: "Recorders",
    label: "Stand the spare down after",
    description:
      "How long every hot recorder must report continuously before the standby is switched off again.",
    default_value: "300",
    is_default: true,
  },
  {
    key: "best_source_enabled",
    value: "true",
    kind: "bool",
    options: null,
    group: "Recorders",
    label: "Keep the better receiver's audio",
    description: "When two receivers push the same transmission, keep the one that says it heard it better.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "upload_pause_backlog",
    value: "false",
    kind: "bool",
    options: null,
    group: "Upload",
    label: "Pause backlog ingestion",
    description: "Refuse backlog and bulk uploads while letting live pushes through.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "upload_max_queue_live",
    value: "5000",
    kind: "int",
    options: null,
    group: "Upload",
    label: "Live queue cap",
    description:
      "Above this many waiting live jobs even a live push is refused, so the recorder spools and retries.",
    default_value: "5000",
    is_default: true,
  },
  {
    key: "upload_max_queue_backlog",
    value: "2000",
    kind: "int",
    options: null,
    group: "Upload",
    label: "Backlog queue cap",
    description:
      "The same ceiling for backlog and bulk jobs, so live traffic keeps priority over the migration.",
    default_value: "2000",
    is_default: true,
  },
  {
    key: "upload_backlog_max_eta_seconds",
    value: "1800",
    kind: "float",
    options: null,
    group: "Upload",
    label: "Backlog ETA ceiling",
    description: "Refuse backlog uploads when the transcription queue would need longer than this to drain.",
    default_value: "1800",
    is_default: true,
  },
  {
    key: "upload_backlog_max_concurrency",
    value: "4",
    kind: "int",
    options: null,
    group: "Upload",
    label: "In-flight backlog uploads",
    description: "How many backlog uploads one API process will accept in flight at once.",
    default_value: "4",
    is_default: true,
  },
  {
    key: "retention_keep_days",
    value: "0",
    kind: "int",
    options: null,
    group: "Retention",
    label: "Keep days",
    description:
      "Delete calls older than this many days — the floor beneath any per-scope policy. 0 = forever.",
    default_value: "0",
    is_default: true,
  },
  {
    key: "retention_keep_audio_days",
    value: "365",
    kind: "int",
    options: null,
    group: "Retention",
    label: "Keep audio days",
    description: "Purge just the MP3 after this many days while keeping the metadata and transcript.",
    default_value: "0",
    is_default: false,
  },
  {
    key: "retention_keep_transcripts",
    value: "true",
    kind: "bool",
    options: null,
    group: "Retention",
    label: "Keep transcripts",
    description: "When audio is purged by age, keep the transcript rather than deleting the whole call.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "retention_archive_path",
    value: "",
    kind: "string",
    options: null,
    group: "Retention",
    label: "Archive path",
    description: "If set, purged MP3s are moved here instead of being deleted. Blank deletes them outright.",
    default_value: "",
    is_default: true,
  },
  {
    key: "disk_warn_free_percent",
    value: "15",
    kind: "float",
    options: null,
    group: "Retention",
    label: "Disk warning below %",
    description: "Warn when free space on the audio disk falls under this percentage, or under the GB floor.",
    default_value: "15",
    is_default: true,
  },
  {
    key: "disk_warn_free_gb",
    value: "30",
    kind: "float",
    options: null,
    group: "Retention",
    label: "Disk warning below GB",
    description: "The same warning as an absolute floor in gigabytes.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "disk_critical_free_percent",
    value: "5",
    kind: "float",
    options: null,
    group: "Retention",
    label: "Disk critical below %",
    description: "A second, error-level event under this percentage.",
    default_value: "5",
    is_default: true,
  },
  {
    key: "disk_critical_free_gb",
    value: "10",
    kind: "float",
    options: null,
    group: "Retention",
    label: "Disk critical below GB",
    description: "The critical threshold in gigabytes.",
    default_value: "10",
    is_default: true,
  },
  {
    key: "health_recorder_silent_minutes",
    value: "5",
    kind: "int",
    options: null,
    group: "Health",
    label: "Recorder silent after (minutes)",
    description:
      "Raise a health alert when a hot recorder has sent no status report for this many minutes. 0 = off.",
    default_value: "5",
    is_default: true,
  },
  {
    key: "health_ingest_lag_minutes",
    value: "20",
    kind: "int",
    options: null,
    group: "Health",
    label: "No new calls after (minutes)",
    description: "Raise a health alert when no call has been recorded for this many minutes. 0 = off.",
    default_value: "20",
    is_default: true,
  },
  {
    key: "health_stt_backlog_minutes",
    value: "30",
    kind: "int",
    options: null,
    group: "Health",
    label: "Transcription stalled after (minutes)",
    description: "Raise a health alert when the oldest live transcription job has waited this long. 0 = off.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "health_backup_max_age_hours",
    value: "30",
    kind: "int",
    options: null,
    group: "Health",
    label: "Backup overdue after (hours)",
    description: "Raise a health alert when the newest database backup is older than this. 0 = off.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "radio_events_keep_days",
    value: "30",
    kind: "int",
    options: null,
    group: "Retention",
    label: "Keep radio events days",
    description:
      "How long presence and affiliation events are kept before purging. Short by default, for privacy.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "auth_require_read",
    value: "false",
    kind: "bool",
    options: null,
    group: "Access",
    label: "Require sign-in to read",
    description: "Require a sign-in even to read the archive. Uploads and admin actions always require one.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "auth_session_days",
    value: "14",
    kind: "int",
    options: null,
    group: "Access",
    label: "Sign-in lasts",
    description:
      "The absolute life of a sign-in, in days. Read when the session is created, so it applies to the next sign-in rather than to sessions already open.",
    default_value: "14",
    is_default: true,
  },
  {
    key: "auth_idle_hours",
    value: "12",
    kind: "int",
    options: null,
    group: "Access",
    label: "Sign out after idle",
    description:
      "The sliding half of the same deadline: a session with no request for this many hours is deleted on sight. Re-read on every request, so shortening it takes effect immediately.",
    default_value: "12",
    is_default: true,
  },
  {
    key: "export_max_rows",
    value: "100000",
    kind: "int",
    options: null,
    group: "API",
    label: "Streamed export row cap",
    description: "Cap on the rows a streamed CSV or JSON export may return. 0 = uncapped.",
    default_value: "100000",
    is_default: true,
  },
  {
    key: "export_max_calls",
    value: "5000",
    kind: "int",
    options: null,
    group: "API",
    label: "Bundle call cap",
    description: "The most calls a single audio bundle may contain.",
    default_value: "5000",
    is_default: true,
  },
  {
    key: "export_max_bytes",
    value: "2147483648",
    kind: "int",
    options: null,
    group: "API",
    label: "Bundle byte cap",
    description: "The most bytes of audio a single export bundle may carry.",
    default_value: "2147483648",
    is_default: true,
  },
  {
    key: "export_keep_days",
    value: "7",
    kind: "int",
    options: null,
    group: "API",
    label: "Keep bundles for",
    description: "The nightly vacuum deletes finished export bundles older than this. 0 = keep them.",
    default_value: "7",
    is_default: true,
  },
  {
    key: "shared_agency_code",
    value: "XX0010000",
    kind: "string",
    options: null,
    group: "Directory",
    label: "Shared channels' agency",
    description:
      "The agency code `keyup mark-shared` gives the shared dispatch channels when they have no agency yet. Blank only flags them.",
    default_value: "",
    is_default: false,
  },
  {
    key: "webhook_url",
    value: "https://cad.example.org/hooks/keyup",
    kind: "string",
    options: null,
    group: "Webhooks",
    label: "Webhook URL",
    description: "Where to POST an event when something a consumer can act on happens.",
    default_value: "",
    is_default: false,
  },
  {
    key: "webhook_secret",
    value: "",
    kind: "secret",
    options: null,
    group: "Webhooks",
    label: "Webhook signing secret",
    description: "Shared secret used to sign each delivery as `X-Keyup-Signature: sha256=<hex>`.",
    default_value: "",
    is_default: false,
  },
  {
    key: "webhook_events",
    value: "call.stored,call.transcribed",
    kind: "string",
    options: null,
    group: "Webhooks",
    label: "Webhook events",
    description: "Which events to send, comma separated.",
    default_value: "call.stored",
    is_default: false,
  },
  {
    key: "webhook_call_scope",
    value: "live+linked",
    kind: "enum",
    options: ["all", "live", "linked", "live+linked"],
    group: "Webhooks",
    label: "Send call events for",
    description:
      "Which calls earn a `call.stored`/`call.transcribed` delivery: every one, only live traffic, only calls that already carry an external link, or either. Link events are never filtered by it.",
    default_value: "all",
    is_default: false,
  },
  {
    key: "webhook_max_attempts",
    value: "6",
    kind: "int",
    options: null,
    group: "Webhooks",
    label: "Max delivery attempts",
    description:
      "How many times one event is retried, with a widening backoff, before it is parked as failed.",
    default_value: "6",
    is_default: true,
  },
];

/**
 * The Export group of the settings table, which Admin → Exports renders. Both enable
 * switches are **false** here for the same reason they are false in the product: the
 * demo must never look like a deployment that is uploading.
 */
const EXPORT_SETTINGS: SettingDefinition[] = [
  {
    key: "export_broadcastify_enabled",
    value: "false",
    kind: "bool",
    options: null,
    group: "Export",
    label: "Upload calls to Broadcastify",
    description: "Re-publish this system's live calls to Broadcastify Calls. Off by default.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "broadcastify_environment",
    value: "production",
    kind: "enum",
    options: ["production", "development"],
    group: "Export",
    label: "Broadcastify environment",
    description:
      "production or development. Broadcastify issues the two credential sets separately and each endpoint refuses the other's, so this picks the URL, the API key and the system id together.",
    default_value: "production",
    is_default: true,
  },
  {
    key: "broadcastify_api_key",
    value: "",
    kind: "secret",
    options: null,
    group: "Export",
    label: "Broadcastify API key",
    description: "The per-system key from the Broadcastify Calls dashboard. Write-only.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_system_id",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify system id",
    description: "Broadcastify's own numeric system id, not keyup's.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_slot",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify conventional slot",
    description:
      "The frequency-slot id a conventional Broadcastify Calls system reports in place of a talkgroup. Blank on a trunked system, which this one is.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_url",
    value: "https://api.broadcastify.com/call-upload",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify endpoint",
    description: "The stage-one metadata endpoint; change it only to point a test at a stub.",
    default_value: "https://api.broadcastify.com/call-upload",
    is_default: true,
  },
  {
    key: "broadcastify_dev_url",
    value: "https://api.broadcastify.com/call-upload-dev",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify endpoint (development)",
    description: "Used only while the environment is development; development credentials work only here.",
    default_value: "https://api.broadcastify.com/call-upload-dev",
    is_default: true,
  },
  {
    key: "broadcastify_dev_api_key",
    value: "",
    kind: "secret",
    options: null,
    group: "Export",
    label: "Broadcastify development API key",
    description:
      "The key issued for the development environment. Write-only, and never falls back to the production key.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_dev_system_id",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify development system id",
    description: "The numeric system id that goes with the development API key.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_encoding",
    value: "m4a",
    kind: "enum",
    options: ["m4a", "mp3"],
    group: "Export",
    label: "Broadcastify encoding",
    description:
      "m4a is AAC at 32 kbit/s with an 18 kHz cutoff, which Broadcastify prefers; mp3 is the accepted alternative, CBR mono at 22050 Hz.",
    default_value: "m4a",
    is_default: true,
  },
  {
    key: "broadcastify_mp3_bitrate_kbps",
    value: "32",
    kind: "enum",
    options: ["32", "16"],
    group: "Export",
    label: "Broadcastify MP3 bitrate",
    description: "32 or 16 kbit/s, the only two the spec lists. Ignored while the encoding is m4a.",
    default_value: "32",
    is_default: true,
  },
  {
    key: "export_ntp_server",
    value: "192.0.2.24",
    kind: "string",
    options: null,
    group: "Export",
    label: "NTP server for the clock check",
    description:
      "Step 1 of the Broadcastify Calls workflow. More than 5 s of drift pauses the Calls uploads; blank turns the query off.",
    default_value: "192.0.2.24",
    is_default: true,
  },
  {
    key: "export_broadcastify_talkgroup_mode",
    value: "all",
    kind: "enum",
    options: ["all", "allow", "deny"],
    group: "Export",
    label: "Broadcastify talkgroup selection",
    description: "all = everything; allow = only ticked talkgroups; deny = all but cleared ones.",
    default_value: "all",
    is_default: true,
  },
  {
    key: "export_broadcastify_systems",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Broadcastify systems",
    description: "Comma-separated keyup system ids; empty means every system.",
    default_value: "",
    is_default: true,
  },
  {
    key: "export_broadcastify_feed_enabled",
    value: "false",
    kind: "bool",
    options: null,
    group: "Export",
    label: "Stream live audio to the Broadcastify Feed",
    description:
      "Open the Broadcastify Feed mount and stream this system's live calls to it. Off by default: a mount takes one source at a time.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "broadcastify_feed_server",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Feed server",
    description: "The Server from the feed's Feed Technical Details (Advanced) panel.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_feed_port",
    value: "80",
    kind: "int",
    options: null,
    group: "Export",
    label: "Feed port",
    description: "The Port from the same panel. Broadcastify publishes 80.",
    default_value: "80",
    is_default: true,
  },
  {
    key: "broadcastify_feed_mount",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "Feed mount",
    description: "The Mount from the same panel, e.g. /yourmount.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_feed_password",
    value: "",
    kind: "secret",
    options: null,
    group: "Export",
    label: "Feed password",
    description: "The Password from the same panel — the Icecast source password, not the Calls API key.",
    default_value: "",
    is_default: true,
  },
  {
    key: "broadcastify_feed_username",
    value: "source",
    kind: "string",
    options: null,
    group: "Export",
    label: "Feed username",
    description:
      'Broadcastify: "If you are required to provide a username for your broadcast client, use: source".',
    default_value: "source",
    is_default: true,
  },
  {
    key: "broadcastify_feed_bitrate_kbps",
    value: "16",
    kind: "int",
    options: null,
    group: "Export",
    label: "Feed bitrate (kbit/s)",
    description: "Mono MP3 at 16–32 kbit/s; it must match what the mount was provisioned with.",
    default_value: "16",
    is_default: true,
  },
  {
    key: "broadcastify_feed_sample_rate",
    value: "22050",
    kind: "int",
    options: null,
    group: "Export",
    label: "Feed sample rate (Hz)",
    description: "The stream's sample rate, 22050 by default, mono.",
    default_value: "22050",
    is_default: true,
  },
  {
    key: "broadcastify_feed_max_backlog_s",
    value: "120",
    kind: "float",
    options: null,
    group: "Export",
    label: "Feed maximum backlog",
    description: "Seconds of un-played audio the feed may hold before it drops the oldest queued calls.",
    default_value: "120",
    is_default: true,
  },
  {
    key: "broadcastify_feed_silence",
    value: "true",
    kind: "bool",
    options: null,
    group: "Export",
    label: "Fill gaps with silence",
    description: "Send encoded silence between calls; an Icecast mount drops a source that stops writing.",
    default_value: "true",
    is_default: true,
  },
  {
    key: "export_openmhz_enabled",
    value: "false",
    kind: "bool",
    options: null,
    group: "Export",
    label: "Upload calls to OpenMHz",
    description: "Re-publish this system's live calls to OpenMHz. Off by default.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "openmhz_api_key",
    value: "",
    kind: "secret",
    options: null,
    group: "Export",
    label: "OpenMHz API key",
    description: "The per-system key from your OpenMHz system settings. Write-only.",
    default_value: "",
    is_default: true,
  },
  {
    key: "openmhz_short_name",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "OpenMHz short name",
    description: "The system's shortName — the path segment in the upload URL.",
    default_value: "",
    is_default: true,
  },
  {
    key: "openmhz_url",
    value: "https://api.openmhz.com",
    kind: "string",
    options: null,
    group: "Export",
    label: "OpenMHz API root",
    description: "The API root; the upload path is `/{shortName}/upload` under it.",
    default_value: "https://api.openmhz.com",
    is_default: true,
  },
  {
    key: "export_openmhz_talkgroup_mode",
    value: "all",
    kind: "enum",
    options: ["all", "allow", "deny"],
    group: "Export",
    label: "OpenMHz talkgroup selection",
    description: "all = everything; allow = only ticked talkgroups; deny = all but cleared ones.",
    default_value: "all",
    is_default: true,
  },
  {
    key: "export_openmhz_systems",
    value: "",
    kind: "string",
    options: null,
    group: "Export",
    label: "OpenMHz systems",
    description: "Comma-separated keyup system ids; empty means every system.",
    default_value: "",
    is_default: true,
  },
  {
    key: "export_include_encrypted",
    value: "false",
    kind: "bool",
    options: null,
    group: "Export",
    label: "Publish encrypted calls",
    description: "Off: an encrypted call is a minute of digital noise with no audio worth publishing.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "export_min_duration_s",
    value: "0.5",
    kind: "float",
    options: null,
    group: "Export",
    label: "Minimum duration",
    description: "Recordings shorter than this are squelch tails and are never published.",
    default_value: "0.5",
    is_default: true,
  },
  {
    key: "export_poll_seconds",
    value: "15",
    kind: "float",
    options: null,
    group: "Export",
    label: "Poll interval",
    description: "How often the exporter worker looks for new work, in seconds.",
    default_value: "15",
    is_default: true,
  },
  {
    key: "export_min_interval_s",
    value: "1",
    kind: "float",
    options: null,
    group: "Export",
    label: "Minimum gap between uploads",
    description: "At least this many seconds between two uploads to the same service.",
    default_value: "1",
    is_default: true,
  },
  {
    key: "broadcastify_upload_at",
    value: "keyup",
    kind: "enum",
    options: ["keyup", "recorder"],
    group: "Export",
    label: "Broadcastify Calls uploaded by",
    description:
      "Who POSTs the call. keyup uploads it after ingest; recorder hands the credentials to each trunkd recorder over the desired-state poll and lets it register and PUT its own MP3 at call end, with keyup recording the outcome.",
    default_value: "keyup",
    is_default: true,
  },
  {
    key: "broadcastify_skew_limit_s",
    value: "15",
    kind: "float",
    options: null,
    group: "Export",
    label: "Broadcastify skew window",
    description:
      "How many seconds after a transmission ends Broadcastify Calls still accepts it. A call already past it is skipped here rather than registered.",
    default_value: "15",
    is_default: true,
  },
  {
    key: "export_target_lufs",
    value: "-11",
    kind: "float",
    options: null,
    group: "Export",
    label: "Export loudness",
    description:
      "The integrated loudness every outbound service gets, applied at export time on top of the stored file. Blank or 0 disables it.",
    default_value: "-11",
    is_default: true,
  },
  {
    key: "export_timeout_s",
    value: "30",
    kind: "float",
    options: null,
    group: "Export",
    label: "Request timeout",
    description: "Per-request HTTP timeout for one upload, in seconds.",
    default_value: "30",
    is_default: true,
  },
  {
    key: "export_batch_size",
    value: "20",
    kind: "int",
    options: null,
    group: "Export",
    label: "Uploads per cycle",
    description: "How many ledger rows one worker cycle uploads per service.",
    default_value: "20",
    is_default: true,
  },
  {
    key: "export_max_age_minutes",
    value: "60",
    kind: "int",
    options: null,
    group: "Export",
    label: "Discovery age limit",
    description: "A call older than this the first time the worker sees it never gets a ledger row.",
    default_value: "60",
    is_default: true,
  },
  {
    key: "export_max_attempts",
    value: "5",
    kind: "int",
    options: null,
    group: "Export",
    label: "Max upload attempts",
    description: "How many times one call is retried against one service before it is parked.",
    default_value: "5",
    is_default: true,
  },
];

SETTINGS.push(...EXPORT_SETTINGS);

/**
 * The HTTPS group, which Admin → HTTPS renders (docs/HTTPS.md).
 *
 * **Off**, like the export switches and for the same reason: the demo must not look like a
 * deployment holding somebody's Cloudflare token. The status handler below answers plain
 * mode to match, so the tab shows the state a fresh clone is actually in.
 */
const HTTPS_SETTINGS: SettingDefinition[] = [
  {
    key: "tls_enabled",
    value: "false",
    kind: "bool",
    options: null,
    group: "HTTPS",
    label: "Serve HTTPS",
    description:
      "Ask Let's Encrypt for a certificate covering the domains below, through the ACME DNS-01 challenge answered in Cloudflare. Off by default.",
    default_value: "false",
    is_default: true,
  },
  {
    key: "tls_domains",
    value: "",
    kind: "string",
    options: null,
    group: "HTTPS",
    label: "Domains",
    description: "Hostnames the certificate covers, space separated. Blank is the off switch.",
    default_value: "",
    is_default: true,
  },
  {
    key: "tls_acme_email",
    value: "",
    kind: "string",
    options: null,
    group: "HTTPS",
    label: "ACME contact address",
    description: "The contact address on the Let's Encrypt account. Expiry notices go here.",
    default_value: "",
    is_default: true,
  },
  {
    key: "tls_challenge",
    value: "auto",
    kind: "enum",
    options: ["auto", "dns", "http"],
    group: "HTTPS",
    label: "ACME challenge",
    description:
      "`dns`: DNS-01 through Cloudflare (needs the token; LAN-only names are fine). `http`: HTTP-01 (no token; public names, ports 80/443 open). `auto`: `dns` when a token is stored, `http` otherwise.",
    default_value: "auto",
    is_default: true,
  },
  {
    key: "tls_cloudflare_api_token",
    value: "",
    kind: "secret",
    options: null,
    group: "HTTPS",
    label: "Cloudflare API token",
    description: "Scoped Zone → DNS → Edit on the one zone. Write-only: stored, never shown again.",
    default_value: "",
    is_default: true,
  },
  {
    key: "tls_acme_ca",
    value: "production",
    kind: "enum",
    options: ["production", "staging"],
    group: "HTTPS",
    label: "Certificate authority",
    description: "`staging` has no rate limits and issues an untrusted certificate; rehearse there first.",
    default_value: "production",
    is_default: true,
  },
  {
    key: "tls_redirect_http",
    value: "true",
    kind: "bool",
    options: null,
    group: "HTTPS",
    label: "Redirect plain HTTP to HTTPS",
    description:
      "With HTTPS on, whether plain :80 308s browsers to https or serves the site in the clear as well. /health and /api/* are never redirected.",
    default_value: "true",
    is_default: true,
  },
];

SETTINGS.push(...HTTPS_SETTINGS);

/** `GET /admin/https/status` — the mode a clone with no Cloudflare token is in. */
export const HTTPS_STATUS: HttpsStatus = {
  desired_mode: "plain",
  mode: "plain",
  reachable: true,
  domains: [],
  caddy_domains: [],
  redirect_http: true,
  acme_ca: "production",
  acme_directory: "https://acme-v02.api.letsencrypt.org/directory",
  acme_email: "",
  has_token: false,
  missing: [],
  certificates: [],
  last_applied_at: null,
  last_error: null,
  error: null,
};

/** `GET /admin/exports` — both services off, and an empty ledger to match. */
export const EXPORT_STATUS: ExportStatus = {
  max_attempts: 5,
  services: [
    {
      service: "broadcastify",
      enabled: false,
      configured: false,
      enabled_at: null,
      talkgroup_mode: "all",
      systems: [],
      upload_at: "keyup",
      by_recorder_24h: 0,
      claimed_24h: 0,
      counts: {},
      counts_24h: {},
      // Nothing has been sent, so nothing has been answered — the demo clone is a switched
      // off exporter, and a fabricated duplicate count would read as a claim it published.
      duplicates: 0,
      duplicates_24h: 0,
      duplicate_uploads_24h: 0,
      late_24h: 0,
      responses_24h: {},
      uploaded: 0,
      pending: 0,
      skipped: 0,
      failed: 0,
      queue_depth: 0,
      last_upload_at: null,
      last_error: null,
      last_error_at: null,
      environment: "production",
      status_url: "https://www.broadcastify.com/calls/status/",
      manage_url: "https://www.broadcastify.com/calls/manage/",
      encoding: "m4a",
      clock_offset_s: 0.031,
      clock_ok: true,
      clock_source: "ntp",
    },
    {
      service: "openmhz",
      enabled: false,
      configured: false,
      enabled_at: null,
      talkgroup_mode: "all",
      systems: [],
      upload_at: "keyup",
      by_recorder_24h: 0,
      claimed_24h: 0,
      counts: {},
      counts_24h: {},
      duplicates: 0,
      duplicates_24h: 0,
      duplicate_uploads_24h: 0,
      late_24h: 0,
      responses_24h: {},
      uploaded: 0,
      pending: 0,
      skipped: 0,
      failed: 0,
      queue_depth: 0,
      last_upload_at: null,
      last_error: null,
      last_error_at: null,
      // Null all the way down: the environment, the encoding and the clock belong to
      // Broadcastify Calls, and OpenMHz must not appear to make a claim about any of them.
      environment: null,
      status_url: null,
      manage_url: null,
      encoding: null,
      clock_offset_s: null,
      clock_ok: null,
      clock_source: null,
    },
  ],
  recent: [],
  // Both demo systems that carry traffic are trunked, so Calls has something it could
  // publish and its switch is offered rather than disabled.
  trunked_systems: [
    { id: 1, name: "Example Network" },
    { id: 2, name: "MAPLE VALLEY COMMUNITY SCHOOL DISTRICT" },
  ],
  broadcastify_feed: {
    enabled: false,
    configured: false,
    connected: false,
    since: null,
    enabled_at: null,
    server: "",
    port: 80,
    mount: "",
    username: "source",
    bitrate_kbps: 16,
    sample_rate: 22050,
    max_backlog_s: 120,
    silence: true,
    bytes_sent: 0,
    calls_played: 0,
    dropped: 0,
    queued_s: 0,
    last_error: null,
    last_error_at: null,
  },
};

/** The frequencies catalogued at each site, keyed by site id. */
export const SITE_FREQUENCIES: Record<number, SiteFrequency[]> = {
  1: [
    {
      id: 1,
      site_id: 1,
      frequency_hz: 852_562_500,
      role: "primary_control",
      source: "observed",
      channel_ref: "1-638",
      first_seen_at: iso(NOW - 200 * 86_400_000),
      last_seen_at: iso(NOW - 120_000),
      observation_count: 480,
      grant_count: 0,
      export_broadcastify: true,
      export_openmhz: true,
    },
    {
      id: 2,
      site_id: 1,
      frequency_hz: 853_212_500,
      role: "voice",
      source: "observed",
      channel_ref: "1-640",
      first_seen_at: iso(NOW - 190 * 86_400_000),
      last_seen_at: iso(NOW - 300_000),
      observation_count: 470,
      grant_count: 12_400,
      export_broadcastify: true,
      export_openmhz: true,
    },
  ],
};

/**
 * What each site's control channel has ever said, keyed by site id.
 *
 * Two of Central's are the ones the analysis could not name — a Harris beacon whose
 * bytes changed under observation, and an opcode that turned up last week — because a
 * demo of this panel with nothing unknown in it would show none of what it is for.
 */
export const SITE_MESSAGE_KINDS: Record<number, SiteMessageKind[]> = {
  1: [
    {
      site_id: 1,
      key: "164:34",
      mfid: 164,
      opcode: 34,
      count: 41_208,
      first_seen_at: iso(NOW - 3 * 86_400_000),
      last_seen_at: iso(NOW - 120_000),
      exemplar: "a2a4b1c0deadbeef",
      exemplar_previous: "a2a4b1c000000000",
      exemplar_changed_at: iso(NOW - 86_400_000),
      known: false,
      label: null,
      alerted_at: iso(NOW - 86_400_000),
    },
    {
      site_id: 1,
      key: "0:56",
      mfid: 0,
      opcode: 56,
      count: 512_004,
      first_seen_at: iso(NOW - 200 * 86_400_000),
      last_seen_at: iso(NOW - 60_000),
      exemplar: null,
      exemplar_previous: null,
      exemplar_changed_at: null,
      known: true,
      label: "Adjacent status broadcast",
      alerted_at: null,
    },
    {
      site_id: 1,
      key: "0:2",
      mfid: 0,
      opcode: 2,
      count: 1_204_991,
      first_seen_at: iso(NOW - 200 * 86_400_000),
      last_seen_at: iso(NOW - 30_000),
      exemplar: null,
      exemplar_previous: null,
      exemplar_changed_at: null,
      known: true,
      label: "Group voice channel grant update",
      alerted_at: null,
    },
  ],
};

/**
 * `GET /sites/{id}/profile` for the two sites that have ever been observed.
 *
 * Site 1 is the fully decoded case — a service bitmap, a band plan, a named neighbour
 * and an unnamed one — and site 2 is the thin one, so the panel's "nobody has decoded
 * this" wording is exercised by the demo and not only by a test.
 */
export const SITE_PROFILES: Record<number, SiteProfile> = {
  1: {
    site_id: 1,
    site_name: "Central",
    description: "Example County simulcast",
    notes: null,
    call_count: 240,
    observed_by: [
      {
        instance: "laptop-01",
        observed_at: iso(NOW - 120_000),
        tsbk_rate: 32.1,
        control_frequency_hz: 851_025_000,
      },
      {
        instance: "signal",
        observed_at: iso(NOW - 900_000),
        tsbk_rate: 31.4,
        control_frequency_hz: 851_025_000,
      },
    ],
    identity: {
      system_id: 1,
      system_name: SYSTEM.name,
      system_label: "Example Network",
      wacn: 781_824,
      p25_system_id: 52,
      nac: 0x34,
      nac_hex: "0x34",
      rfss: 10,
      site_number: 1,
      lra: 10,
      rr_site_id: 9001,
      rr_synced_at: "2026-08-31T04:00:00Z",
    },
    control: {
      frequency_hz: 851_025_000,
      channel: "1-3",
      tsbk_rate: 32.1,
      secondary_control: [
        {
          frequency_hz: 851_650_000,
          channel: "1-27",
          tsbk_rate: null,
          service_class: ["control"],
        },
        { frequency_hz: null, channel: "3-14", tsbk_rate: null, service_class: ["control"] },
      ],
      configured_control: [851_025_000, 851_650_000],
    },
    services: {
      available: ["Composite", "Group Voice", "Individual Voice", "Data", "Registration"],
      supported: ["Composite", "Group Voice", "Individual Voice", "Data", "Registration", "Encryption"],
      available_raw: "0x2F1000",
      supported_raw: "0x2F1400",
      request_priority: 2,
      supported_not_available: ["Encryption"],
    },
    bands: [
      {
        id: 1,
        base_hz: 851_006_250,
        spacing_hz: 6_250,
        offset_hz: -45_000_000,
        bandwidth_hz: 12_500,
        tdma: false,
      },
    ],
    traffic: [
      {
        frequency_hz: 851_300_000,
        channel: "1-13",
        grants: 19,
        calls: 17,
        first_seen: iso(NOW - 900_000),
        last_seen: iso(NOW - 60_000),
      },
      {
        frequency_hz: 851_200_000,
        channel: "1-9",
        grants: 4,
        calls: 3,
        first_seen: iso(NOW - 900_000),
        last_seen: iso(NOW - 300_000),
      },
    ],
    adjacent_sites: [
      {
        system_id: 52,
        rfss: 10,
        site_number: 2,
        lra: 10,
        control_frequency_hz: 852_000_000,
        channel: "1-2",
        flags: { valid: true },
        name: "North",
        source: "keyup",
        known: true,
      },
      {
        system_id: 52,
        rfss: 10,
        site_number: 7,
        lra: 10,
        control_frequency_hz: 852_500_000,
        channel: "1-6",
        flags: {},
        // A tower no recorder here has ever heard, named out of the RadioReference cache.
        name: "West Co Simulcast",
        source: "radioreference",
        known: false,
      },
      {
        system_id: 52,
        rfss: 20,
        site_number: 20,
        lra: 20,
        control_frequency_hz: 853_100_000,
        channel: "1-11",
        flags: {},
        name: null,
        source: null,
        known: false,
      },
    ],
    data_channel: { grants: 19, pages: 2, pdus: 3, channels: [858_987_500] },
    lsd_nonzero: 7,
    message_kinds: { known: 2, unknown: 9, noise: 8, total_seen: 11 },
    unknowns: [
      "1 service(s) the site supports but is not currently offering.",
      "1 secondary control channel(s) announced as a channel number whose band plan has not been heard, so the frequency cannot be computed.",
      "1 announced neighbour(s) match no site Keyup or RadioReference knows: RFSS 20 site 20.",
      "1 recurring control-channel message kind(s) nobody has named.",
    ],
  },
  2: {
    site_id: 2,
    site_name: "North",
    description: "North County simulcast",
    notes: null,
    call_count: 0,
    observed_by: [
      {
        instance: "laptop-01",
        observed_at: iso(NOW - 3_600_000),
        tsbk_rate: 29.8,
        control_frequency_hz: 852_000_000,
      },
    ],
    identity: {
      system_id: 1,
      system_name: SYSTEM.name,
      system_label: "Example Network",
      wacn: 781_824,
      p25_system_id: 52,
      nac: 0x34,
      nac_hex: "0x34",
      rfss: 10,
      site_number: 2,
      lra: 10,
      rr_site_id: 9002,
      rr_synced_at: "2026-08-31T04:00:00Z",
    },
    control: {
      frequency_hz: 852_000_000,
      channel: "1-2",
      tsbk_rate: 29.8,
      secondary_control: [],
      configured_control: [],
    },
    services: {
      available: ["Composite", "Group Voice", "Data"],
      supported: ["Composite", "Group Voice", "Data"],
      available_raw: "0x2E0000",
      supported_raw: "0x2E0000",
      request_priority: null,
      supported_not_available: [],
    },
    bands: [],
    traffic: [],
    adjacent_sites: [],
    data_channel: null,
    lsd_nonzero: null,
    message_kinds: { known: 0, unknown: 0, noise: 0, total_seen: 0 },
    unknowns: [
      "No IDEN band plan heard, so channel numbers cannot be turned into frequencies.",
      "No recorder reports a configured control-channel list for this site.",
      "No adjacent sites announced in any stored window.",
    ],
  },
};

export const TOKENS: ApiTokenInfo[] = [
  {
    id: 1,
    label: "sdrtrunk recorder",
    scopes: ["upload"],
    created_by: "alex",
    created_at: iso(NOW - 40 * 86_400_000),
    last_used_at: iso(NOW - 90_000),
    expires_at: null,
    revoked_at: null,
    prefix: "kup_7QK1",
    suffix: "8f2a",
    used_by_recorders: ["attic-trunkd"],
  },
  {
    id: 2,
    label: "CAD integration",
    scopes: ["read"],
    created_by: "alex",
    created_at: iso(NOW - 12 * 86_400_000),
    last_used_at: iso(NOW - 3_600_000),
    expires_at: null,
    revoked_at: null,
    prefix: "kup_MZ4v",
    suffix: "c41d",
    used_by_recorders: [],
  },
  {
    id: 3,
    label: "old laptop",
    scopes: ["read", "write"],
    created_by: "alex",
    created_at: iso(NOW - 200 * 86_400_000),
    last_used_at: iso(NOW - 60 * 86_400_000),
    expires_at: null,
    revoked_at: iso(NOW - 30 * 86_400_000),
    prefix: "kup_0Rt9",
    suffix: "1b09",
    used_by_recorders: [],
  },
];

export const USERS: User[] = [
  {
    id: 1,
    username: "alex",
    display_name: "Alex",
    email: "alex@example.org",
    scopes: ["admin"],
    is_admin: true,
    is_active: true,
    created_at: iso(NOW - 300 * 86_400_000),
    last_login_at: iso(NOW - 7_200_000),
  },
  {
    id: 2,
    username: "dispatch",
    display_name: "Dispatch center (read-only)",
    email: null,
    scopes: ["read"],
    is_admin: false,
    is_active: true,
    created_at: iso(NOW - 60 * 86_400_000),
    last_login_at: iso(NOW - 86_400_000),
  },
];

export const ALERTS: AlertRule[] = [
  {
    id: 1,
    name: "Structure fire keyword",
    kind: "keyword",
    pattern: "structure fire, second alarm, working fire",
    talkgroup_ids: [1, 2, 3],
    radio_ids: [],
    channels: ["ntfy", "discord"],
    target: "https://ntfy.sh/keyup-fire",
    cooldown_s: 300,
    enabled: true,
    last_fired_at: iso(NOW - 5_400_000),
    fire_count: 23,
    created_by: "alex",
    filters: {},
    channel_config: {},
    created_at: iso(NOW - 90 * 86_400_000),
  },
  {
    id: 2,
    name: "Any traffic on Interop 1",
    kind: "talkgroup",
    pattern: "70001",
    talkgroup_ids: [18],
    radio_ids: [],
    channels: ["webhook"],
    target: "http://192.0.2.20:9000/hooks/interop",
    cooldown_s: 900,
    enabled: true,
    last_fired_at: iso(NOW - 20 * 3_600_000),
    fire_count: 4,
    created_by: "alex",
    filters: {},
    channel_config: {},
    created_at: iso(NOW - 30 * 86_400_000),
  },
  {
    id: 3,
    name: "Pursuit / plate regex",
    kind: "regex",
    pattern: "\\b(pursuit|foot bail|10-80)\\b",
    talkgroup_ids: [],
    radio_ids: [],
    channels: ["discord", "email"],
    target: "ops@example.org",
    cooldown_s: 120,
    enabled: false,
    last_fired_at: null,
    fire_count: 0,
    created_by: "alex",
    filters: {},
    channel_config: {},
    created_at: iso(NOW - 8 * 86_400_000),
  },
  {
    id: 4,
    name: "Battalion chief keys up",
    kind: "radio",
    pattern: "5226007",
    talkgroup_ids: [],
    radio_ids: [7],
    channels: ["ntfy"],
    target: "https://ntfy.sh/keyup-bc",
    cooldown_s: 600,
    enabled: true,
    last_fired_at: iso(NOW - 11 * 3_600_000),
    fire_count: 9,
    created_by: "alex",
    filters: {},
    channel_config: {},
    created_at: iso(NOW - 15 * 86_400_000),
  },
];

export const NOTIFICATIONS: NotificationRecord[] = Array.from({ length: 24 }, (_, i) => {
  const rule = ALERTS[i % ALERTS.length] as AlertRule;
  const call = CALLS[i * 5] as MockCall;
  return {
    id: 3000 + i,
    rule_id: rule.id,
    rule_name: rule.name,
    call_id: call.id,
    channel: rule.channels[0] ?? "webhook",
    status: i % 9 === 3 ? "failed" : i % 7 === 5 ? "skipped" : "sent",
    severity: i % 9 === 3 ? "error" : "info",
    subject: rule.name,
    error: i % 9 === 3 ? "webhook returned 503" : null,
    created_at: iso(NOW - i * 2_100_000),
    excerpt: call.transcript_text,
  };
});

export const SAVED_SEARCHES: SavedSearch[] = [
  {
    id: 1,
    name: "Working fires",
    query: "q=%22structure+fire%22&category=fire",
    shared: true,
    created_at: iso(NOW - 20 * 86_400_000),
    created_by: "alex",
  },
  {
    id: 2,
    name: "Overnight law traffic",
    query: "category=law&time_from=23%3A00&time_to=05%3A00",
    shared: false,
    created_at: iso(NOW - 6 * 86_400_000),
    created_by: "alex",
  },
  {
    id: 3,
    name: "Bad transcripts to review",
    query: "quality=noise&quality=suspect",
    shared: false,
    created_at: iso(NOW - 2 * 86_400_000),
    created_by: "alex",
  },
];

/* -------------------------------------------------- site channel audit */

/**
 * The channel audit for the two sites (docs/CHANNEL_AUDIT.md). Central is the
 * interesting one on purpose: it has failed over to the control channel
 * RadioReference calls an alternate, is carrying a voice channel nobody
 * documented, has a documented channel nothing has heard for a month, is
 * configured for a frequency the site never mentions, and announces a
 * neighboring tower Keyup does not know.
 */
const CONTROL_HZ = 851_025_000;
const BACKUP_HZ = 851_787_500;

export const CHANNEL_AUDITS: Record<number, SiteChannelAudit> = {
  1: {
    site: {
      id: 1,
      system_id: 1,
      system: "Example Network",
      name: "Central",
      site_number: 1,
      description: "Example County simulcast",
      nac: 0x34,
      rfss: 10,
      lra: 10,
      rr_site_id: 9001,
    },
    rr_synced_at: iso(NOW - 5 * 3600_000),
    last_observed_at: iso(NOW - 4 * 60_000),
    stale_days: 14,
    control: {
      observed: BACKUP_HZ,
      rr_primary: [CONTROL_HZ],
      rr_alternate: [BACKUP_HZ],
      observed_alternate: [CONTROL_HZ],
      configured: [BACKUP_HZ, 852_000_000],
    },
    undocumented: [
      {
        frequency_hz: 856_712_500,
        role: "voice",
        channel_ref: "1-456",
        first_seen_at: iso(NOW - 3 * 86_400_000),
        last_seen_at: iso(NOW - 6 * 60_000),
        grant_count: 212,
        observation_count: 811,
      },
      {
        frequency_hz: 857_237_500,
        role: "voice",
        channel_ref: "1-498",
        first_seen_at: iso(NOW - 31 * 3600_000),
        last_seen_at: iso(NOW - 52 * 60_000),
        grant_count: 34,
        observation_count: 96,
      },
    ],
    missing: [
      {
        frequency_hz: 856_912_500,
        rr_role: "voice",
        channel_ref: "101",
        last_seen_at: iso(NOW - 30 * 86_400_000),
      },
      { frequency_hz: 857_000_000, rr_role: "voice", channel_ref: "102", last_seen_at: null },
    ],
    control_drift: [
      { frequency_hz: CONTROL_HZ, rr_role: "primary_control", observed_role: "alternate_control" },
      { frequency_hz: BACKUP_HZ, rr_role: "alternate_control", observed_role: "primary_control" },
    ],
    recorder_config: {
      announced_not_configured: [CONTROL_HZ],
      configured_not_announced: [852_000_000],
      suggested_control_frequencies: [BACKUP_HZ, CONTROL_HZ],
    },
    adjacent_sites: [
      {
        system_id_p25: 52,
        rfss: 10,
        site_number: 2,
        lra: 10,
        control_frequency_hz: 851_600_000,
        channel_ref: "1-48",
        flags: { conventional: false, failure: false, valid: true, rfss_connected: true },
        first_seen_at: iso(NOW - 9 * 86_400_000),
        last_seen_at: iso(NOW - 4 * 60_000),
        known_in_rr: true,
        known_site_id: 2,
        known_site_name: "North County simulcast",
        known_county: "North",
        match: "rfss+site_number",
      },
      {
        system_id_p25: 57,
        rfss: 20,
        site_number: 20,
        lra: 20,
        control_frequency_hz: 851_087_500,
        channel_ref: "1-6",
        flags: { conventional: false, failure: false, valid: true, rfss_connected: true },
        first_seen_at: iso(NOW - 2 * 86_400_000),
        last_seen_at: iso(NOW - 4 * 60_000),
        known_in_rr: false,
        known_site_id: null,
        known_site_name: null,
        known_county: null,
        match: null,
      },
    ],
  },
  2: {
    site: {
      id: 2,
      system_id: 1,
      system: "Example Network",
      name: "North",
      site_number: 2,
      description: "North County simulcast",
      nac: 0x34,
      rfss: 10,
      lra: 10,
      rr_site_id: 9002,
    },
    rr_synced_at: iso(NOW - 5 * 3600_000),
    last_observed_at: iso(NOW - 7 * 60_000),
    stale_days: 14,
    control: {
      observed: 851_600_000,
      rr_primary: [851_600_000],
      rr_alternate: [],
      observed_alternate: [],
      configured: [851_600_000],
    },
    undocumented: [],
    missing: [],
    control_drift: [],
    recorder_config: {
      announced_not_configured: [],
      configured_not_announced: [],
      suggested_control_frequencies: [851_600_000],
    },
    adjacent_sites: [
      {
        system_id_p25: 52,
        rfss: 10,
        site_number: 1,
        lra: 10,
        control_frequency_hz: BACKUP_HZ,
        channel_ref: "1-638",
        flags: { conventional: false, failure: false, valid: true, rfss_connected: true },
        first_seen_at: iso(NOW - 9 * 86_400_000),
        last_seen_at: iso(NOW - 7 * 60_000),
        known_in_rr: true,
        known_site_id: 1,
        known_site_name: "Example County simulcast",
        known_county: "Example",
        match: "rfss+site_number",
      },
    ],
  },
};

export const SYSTEM_CHANNEL_AUDIT: SystemChannelAudit = {
  system: { id: 1, name: "Example Network", rr_system_id: 9999, rr_synced_at: iso(NOW - 5 * 3600_000) },
  sites: Object.values(CHANNEL_AUDITS).map((audit) => ({
    site_id: audit.site.id,
    site: audit.site.name,
    last_observed_at: audit.last_observed_at,
    rr_synced_at: audit.rr_synced_at,
    control_observed: audit.control.observed,
    undocumented: audit.undocumented.length,
    missing: audit.missing.length,
    control_drift: audit.control_drift.length,
    announced_not_configured: audit.recorder_config.announced_not_configured.length,
    adjacent_unknown: audit.adjacent_sites.filter((entry) => entry.known_site_id === null).length,
  })),
  totals: {
    undocumented: 2,
    missing: 2,
    control_drift: 2,
    announced_not_configured: 1,
    adjacent_unknown: 1,
  },
};

/* ------------------------------------------------ recorders & tuners (§4.8) */

const recNow = Date.now();
const recIso = (msAgo: number): string => new Date(recNow - msAgo).toISOString();

/**
 * One trunkd recorder with three tuners: a control tuner locked to Central, a voice tuner
 * working a pool of grants, and a data tuner. One tuner is left drifting (its applied
 * version lags the desired one) so the "pending" badge has something to show.
 *
 * It also describes itself the way a current recorder does — a station card and lifetime
 * counters — and its data tuner deliberately reports no hardware description, no gain and
 * no level, because a recorder on an older binary sends none of that and the page has to
 * read correctly either way.
 */
export const RECORDERS: Recorder[] = [
  {
    id: 1,
    instance_id: "trunkd-central-01",
    label: "Central roof",
    version: 4,
    recorder_version: "trunkd 0.9.1",
    stream: { connected: true, since: "2026-09-04T02:59:33Z", client_count: 1 },
    last_seen_at: recIso(8_000),
    reported_at: recIso(8_000),
    priority: 10,
    standby: "hot",
    promoted_at: null,
    // Uploading normally, on its own configuration: keyup has said nothing about it.
    desired_spool_only: null,
    // One recorder-level write it has not taken up yet — the archive flag below. The upload
    // hold is untouched, so `spool_only_pending` stays false while `recorder_pending` is
    // true: two badges reading one counter, each about its own setting.
    desired_recorder_version: 1,
    applied_recorder_version: 0,
    uplink: {
      spool_only: false,
      source: "config",
      spooled_calls: 0,
      spool_bytes: 0,
      url: "https://keyup.example/api",
    },
    spool_only_pending: false,
    // Keyup manages this one's link, and the mock carries a fingerprint so the roof and the
    // garage below can be seen to hold the *same* key — which is the whole reason the
    // fingerprint is on the card rather than only in the dialog.
    desired_event_log_archive: true,
    desired_link: {
      enabled: true,
      bind: "0.0.0.0:9110",
      peers: ["trunkd-central-02:9110"],
      hello_interval_s: 5,
      accept_remote_grants: true,
      publish_grants: true,
      defer_to_claims: true,
      key_set: true,
      key_fingerprint: "3f9a1c07",
    },
    // No Broadcastify block: the shipped `broadcastify_upload_at` is `keyup`, so a
    // recorder is sent none and an operator reading this card sees none.
    desired_broadcastify: null,
    desired_skip_talkgroups: null,
    recorder_pending: true,
    recorder_applied: {
      version: 0,
      spool_only: null,
      event_log_archive: false,
      link: {
        enabled: true,
        bind: "0.0.0.0:9110",
        peers: ["trunkd-central-02:9110"],
        key_set: true,
        key_fingerprint: "3f9a1c07",
      },
    },
    recorder_rejected: null,
    recorder_capabilities: { restart_on: ["link"] },
    // Linked to the garage spare: the peer is alive, and it is the roof box holding the
    // claim on the site — which is the fact the two cards are read against each other for.
    link: {
      peers: [
        {
          instance: "trunkd-central-02",
          alive: true,
          last_seen: recIso(9_000),
          claims: [],
        },
      ],
    },
    // Caught up to yesterday, with today's day still open on the recorder.
    event_log: { pending: 0, last_date: "2026-09-01" },
    station: {
      name: "Central roof",
      location: "Anytown, US",
      latitude: 38.8814,
      longitude: -94.8191,
      elevation_m: 320,
      contact: "N0CALL <ops@example.invalid>",
      notes: "Deaf to the north-west behind the water tower; mast work every second Tuesday.",
    },
    counters: {
      calls_started: 41_221,
      calls_ended: 41_218,
      uploaded: 41_100,
      spooled: 118,
      failed: 3,
      spool_bytes: 9_431_040,
      disk_free_bytes: 12_884_901_888,
    },
    created_at: recIso(72 * 3600 * 1000),
    tuners: [
      {
        id: 1,
        tuner_key: "airspy-0",
        reported: {
          serial: "AIRSPY-0x9a11",
          backend: "airspy",
          center_hz: 855_500_000,
          span_hz: 10_000_000,
          role: "control",
          site: "Central",
          locked: true,
          control_hz: 851_025_000,
          nac: 0x34,
          tsbk_rate: 34.2,
          calls_active: 0,
          applied_version: 2,
          description: {
            receiver: "Airspy R2",
            antenna: "Diamond X50 @ 8m",
            cable: "LMR-400, 12 m",
            amplifier: "None",
            height_m: 8,
            notes: "Shared mast with the voice tuner via a 2-way splitter.",
          },
          gain: { lna_db: 24, vga_db: 20, amp: false },
          // The front end as a current recorder reports it: named stages with real ranges,
          // what each is set to, and which of them cannot be changed without a restart.
          gains: { LNA: 12, MIX: 8, IF: 6 },
          gain_elements: [
            { name: "LNA", min: 0, max: 15, step: 1 },
            { name: "MIX", min: 0, max: 15, step: 1 },
            { name: "IF", min: 0, max: 15, step: 1 },
          ],
          capabilities: {
            runtime_gain: true,
            runtime_ppm: false,
            restart_on: ["sample_rate", "ppm"],
          },
          agc: false,
          ppm: 1.5,
          auto_ppm: true,
          auto_ppm_applied: 1.82,
          bandwidth_hz: 8_000_000,
          rx_port: "LNAW",
          sample_rate: 10_000_000,
          data_follow: false,
          control_frequencies: [851_025_000],
          rssi_dbfs: -38.4,
          freq_error_hz: -137.25,
        },
        reported_at: recIso(8_000),
        desired: {
          role: "control",
          site_id: 1,
          center_frequency: 855_500_000,
          enabled: true,
          spectrum: true,
        },
        desired_version: 2,
        updated_by: "admin (token:web)",
        updated_at: recIso(3600 * 1000),
        drift: false,
        rejected: null,
      },
      {
        id: 2,
        tuner_key: "rtl-1",
        reported: {
          serial: "RTL-0x00000001",
          backend: "rtlsdr",
          center_hz: 852_000_000,
          span_hz: 2_400_000,
          role: "voice",
          site: "Central",
          locked: true,
          pool_in_use: 3,
          pool_size: 6,
          overflow_per_s: 0,
          calls_active: 3,
          applied_version: 1,
          // Its antenna run is 800-only, and keyup is managing that: desired and applied agree.
          bands: ["800"],
          // A full description with the constraint written into the notes, because that is
          // what the operators of the live recorders actually write and what the dialogs
          // have to put in front of whoever is choosing this tuner's bands.
          description: {
            receiver: "RTL-SDR v3",
            antenna: "Antenna A — Diamond X50 @ 8m, 806-960 MHz",
            cable: "RG-58, 6 m",
            height_m: 8,
            notes:
              "USB bus 1 port 5; 850-960 MHz run, via the splitter shared with the control tuner.\nAntenna B on the same mast is 152-470 MHz only — never move this tuner onto it.",
          },
          gain: { gain: 42.1 },
          rssi_dbfs: -18.2,
        },
        reported_at: recIso(9_000),
        desired: {
          role: "voice",
          site_id: 1,
          center_frequency: 852_000_000,
          enabled: true,
          spectrum: false,
          bands: ["800"],
        },
        desired_version: 1,
        updated_by: "admin (token:web)",
        updated_at: recIso(2 * 3600 * 1000),
        drift: false,
        rejected: null,
      },
      {
        id: 3,
        tuner_key: "rtl-2",
        reported: {
          serial: "RTL-0x00000002",
          backend: "rtlsdr",
          center_hz: 858_987_500,
          span_hz: 2_400_000,
          role: "data",
          site: "Central",
          locked: true,
          overflow_per_s: 0.4,
          calls_active: 0,
          applied_version: 0,
          bands: ["800"],
        },
        reported_at: recIso(11_000),
        // Desired has been bumped to 1 but the recorder still reports applied 0 → drift.
        // Its bands are the narrower slice keyup wants and the recorder has not taken up,
        // so the band strip has an outline to draw and not only a shaded block.
        desired: {
          role: "data",
          site_id: 1,
          center_frequency: 858_987_500,
          enabled: true,
          spectrum: false,
          bands: ["858000000-859500000"],
        },
        desired_version: 1,
        updated_by: "admin (token:web)",
        updated_at: recIso(30 * 1000),
        drift: true,
        rejected: null,
      },
    ],
  },
  /**
   * The warm spare: registered, healthy, and doing nothing.
   *
   * Its one tuner reports the same site as the recorder above and carries
   * `enabled: false`, which is exactly the state the arbiter promotes out of — so the
   * standby select, the priority field and the "warm" badge all have something real to
   * render, and the difference between "idle because it is a spare" and "idle because it is
   * broken" is visible on the page rather than only in the schema.
   */
  {
    id: 2,
    instance_id: "trunkd-central-02",
    label: "Garage spare",
    version: 1,
    recorder_version: "trunkd 0.9.1",
    stream: { connected: false, since: null, client_count: 0 },
    last_seen_at: recIso(12_000),
    reported_at: recIso(12_000),
    priority: 50,
    standby: "warm",
    promoted_at: null,
    // Held by keyup, and the recorder has taken it up: the card shows the hold and how much
    // is waiting behind it, which is the number the release dialog quotes back.
    desired_spool_only: true,
    desired_recorder_version: 2,
    applied_recorder_version: 2,
    uplink: {
      spool_only: true,
      source: "keyup",
      spooled_calls: 46,
      spool_bytes: 39_845_888,
      url: "https://keyup.example/api",
    },
    spool_only_pending: false,
    // The same key fingerprint as the roof box: a matching pair, which is what the two cards
    // side by side are for — a mesh whose members' fingerprints differ never comes up.
    desired_event_log_archive: true,
    desired_link: {
      enabled: true,
      bind: "0.0.0.0:9110",
      peers: ["trunkd-central-01:9110"],
      hello_interval_s: 5,
      accept_remote_grants: true,
      publish_grants: true,
      defer_to_claims: true,
      key_set: true,
      key_fingerprint: "3f9a1c07",
    },
    // No Broadcastify block: the shipped `broadcastify_upload_at` is `keyup`, so a
    // recorder is sent none and an operator reading this card sees none.
    desired_broadcastify: null,
    desired_skip_talkgroups: null,
    recorder_pending: false,
    recorder_applied: {
      version: 2,
      spool_only: true,
      event_log_archive: true,
      link: {
        enabled: true,
        bind: "0.0.0.0:9110",
        peers: ["trunkd-central-01:9110"],
        key_set: true,
        key_fingerprint: "3f9a1c07",
      },
    },
    recorder_rejected: null,
    recorder_capabilities: { restart_on: ["link"] },
    link: {
      peers: [
        {
          instance: "trunkd-central-01",
          alive: true,
          last_seen: recIso(9_000),
          claims: ["site:Central"],
        },
      ],
    },
    // Two days behind: it has been holding its uploads, and the archive is behind with it.
    event_log: { pending: 2, last_date: "2026-08-30", bytes_pending: 7_340_032 },
    station: {
      name: "Garage spare",
      location: "Gardner, KS",
      contact: "N0CALL (spare) <ops@example.invalid>",
      notes: "Second receiver on the same site; kept off unless the roof box goes quiet.",
    },
    counters: {
      calls_started: 0,
      calls_ended: 0,
      uploaded: 0,
      spooled: 0,
      failed: 0,
      disk_free_bytes: 214_748_364_800,
    },
    created_at: recIso(40 * 3600 * 1000),
    tuners: [
      {
        id: 4,
        tuner_key: "rtl-0",
        reported: {
          serial: "RTL-0x0000000a",
          backend: "rtlsdr",
          center_hz: 855_500_000,
          span_hz: 2_400_000,
          role: "control",
          site: "Central",
          locked: false,
          applied_version: 1,
          description: { receiver: "RTL-SDR v3", antenna: "Discone in the garage", height_m: 4 },
          gain: { gain: 38.6 },
          rssi_dbfs: -61.5,
        },
        reported_at: recIso(12_000),
        desired: { role: "control", site: "Central", enabled: false, spectrum: false },
        desired_version: 1,
        updated_by: "admin (token:web)",
        updated_at: recIso(20 * 3600 * 1000),
        drift: false,
        rejected: null,
      },
      /**
       * A HackRF, so the edit dialog has a tuner whose gain is two fixed-step stages rather
       * than a list of named elements — and one carrying a refusal, which is the only trace
       * a field the recorder could not use ever leaves.
       */
      {
        id: 5,
        tuner_key: "hackrf-0",
        reported: {
          serial: "HACKRF-0000000457",
          backend: "hackrf",
          center_hz: 855_000_000,
          span_hz: 20_000_000,
          role: "voice",
          site: "Central",
          locked: false,
          applied_version: 3,
          lna_db: 16,
          vga_db: 26,
          amp: false,
          sample_rate: 20_000_000,
          ppm: 0,
          data_follow: true,
          capabilities: { runtime_gain: true, runtime_ppm: true, restart_on: ["sample_rate"] },
          rejected: { lna_db: "the front end is fixed on this board" },
          rssi_dbfs: -52,
        },
        reported_at: recIso(12_000),
        desired: { role: "voice", enabled: true, spectrum: false, lna_db: 32 },
        desired_version: 3,
        updated_by: "admin (token:web)",
        updated_at: recIso(2 * 3600 * 1000),
        drift: false,
        rejected: { lna_db: "the front end is fixed on this board" },
      },
    ],
  },
];

/**
 * The days of raw event log the two mock recorders have shipped.
 *
 * Three for the roof box and one for the spare, so the card's list has something to sort
 * and the per-recorder filter has something to leave out. The digests are decorative — the
 * mock never serves the bytes, and the download link is what the real API answers.
 */
export const EVENT_LOG_ARCHIVES: EventLogArchive[] = [
  {
    id: 1,
    instance: "trunkd-central-01",
    date: "2026-09-01",
    bytes: 6_815_744,
    sha256: "9f2c1a".padEnd(64, "0"),
    received_at: recIso(20 * 3600 * 1000),
  },
  {
    id: 2,
    instance: "trunkd-central-01",
    date: "2026-08-31",
    bytes: 7_340_032,
    sha256: "3b7e42".padEnd(64, "0"),
    received_at: recIso(44 * 3600 * 1000),
  },
  {
    id: 3,
    instance: "trunkd-central-01",
    date: "2026-08-30",
    bytes: 5_242_880,
    sha256: "c10d55".padEnd(64, "0"),
    received_at: recIso(68 * 3600 * 1000),
  },
  {
    id: 4,
    instance: "trunkd-central-02",
    date: "2026-08-30",
    bytes: 1_048_576,
    sha256: "0ea991".padEnd(64, "0"),
    received_at: recIso(68 * 3600 * 1000),
  },
];

/** A band strip per site, folded from the same channels the channel audit knows about. */
export const OCCUPANCY: SiteOccupancy[] = SITES.map((site, index) => {
  const base = 851_000_000 + index * 50_000;
  return {
    site_id: site.id,
    site_name: site.name,
    system_id: site.system_id,
    observed_at: recIso(10_000),
    bands: [{ id: 0, base_hz: base, spacing_hz: 12_500, bandwidth_hz: 12_500 }],
    channels: [
      {
        frequency_hz: base + 50_000,
        channel: "1-3",
        role: "control",
        grants: 0,
        calls: 0,
        last_seen: recIso(5_000),
      },
      {
        frequency_hz: base + 200_000,
        channel: "1-19",
        role: "voice",
        grants: 8,
        calls: 6,
        last_seen: recIso(4_000),
      },
      {
        frequency_hz: base + 500_000,
        channel: "1-43",
        role: "voice",
        grants: 3,
        calls: 2,
        last_seen: recIso(90_000),
      },
      {
        frequency_hz: base + 900_000,
        channel: "1-79",
        role: "voice",
        grants: 1,
        calls: 1,
        last_seen: recIso(20 * 60 * 1000),
      },
      {
        frequency_hz: base + 1_987_500,
        channel: "1-159",
        role: "data",
        grants: 0,
        calls: 0,
        last_seen: recIso(45_000),
      },
    ],
  };
});

/**
 * Where the demo's recordings come from.
 *
 * Four rows because four is what it takes to show the states an operator has to tell
 * apart: a healthy watched directory, one whose mount has gone (red, and still configured),
 * a recorder pushing over a token, and a bulk import that owns no runtime behaviour at all
 * and is here purely to be watched.
 */
export const INGEST_SOURCES: IngestSourceRow[] = [
  {
    id: 1,
    kind: "watch_dir",
    label: "AUDIO_ROOT",
    enabled: true,
    path: "/srv/keyup/audio",
    recursive: true,
    tr_prefer_audio: null,
    tr_sidecar_wait_seconds: null,
    token_id: null,
    token_label: null,
    token_active: null,
    notes: "Seeded from AUDIO_ROOT. The archive root every calls.file_path is relative to.",
    meta: null,
    last_activity_at: secondsAgo(9),
    last_error: null,
    last_error_at: null,
    created_at: hoursAgo(900),
    updated_at: hoursAgo(900),
    updated_by: null,
    path_state: "ok",
    path_detail: null,
  },
  {
    id: 2,
    kind: "watch_dir",
    label: "attic recorder spool",
    enabled: true,
    path: "/mnt/attic/sdrtrunk",
    recursive: true,
    tr_prefer_audio: "m4a",
    tr_sidecar_wait_seconds: null,
    token_id: null,
    token_label: null,
    token_active: null,
    notes: null,
    meta: null,
    last_activity_at: hoursAgo(14),
    last_error: "missing: /mnt/attic/sdrtrunk does not exist",
    last_error_at: hoursAgo(2),
    created_at: hoursAgo(400),
    updated_at: hoursAgo(400),
    updated_by: "operator",
    path_state: "missing",
    path_detail: "/mnt/attic/sdrtrunk does not exist",
  },
  {
    id: 3,
    kind: "push_token",
    label: "rec-02 (garage)",
    enabled: true,
    path: null,
    recursive: true,
    tr_prefer_audio: null,
    tr_sidecar_wait_seconds: null,
    token_id: 2,
    token_label: "recorder-02",
    token_active: true,
    notes: "trunk-recorder, pushes over /api/call-upload",
    meta: null,
    last_activity_at: secondsAgo(31),
    last_error: null,
    last_error_at: null,
    created_at: hoursAgo(300),
    updated_at: hoursAgo(300),
    updated_by: "operator",
    path_state: "n/a",
    path_detail: null,
  },
  {
    id: 4,
    kind: "migration",
    label: "2019 archive",
    enabled: true,
    path: null,
    recursive: true,
    tr_prefer_audio: null,
    tr_sidecar_wait_seconds: null,
    token_id: null,
    token_label: null,
    token_active: null,
    notes: null,
    meta: { state: "running", files_total: 41208, files_done: 30114, files_failed: 3, finished: false },
    last_activity_at: secondsAgo(50),
    last_error: "3 file(s) failed",
    last_error_at: hoursAgo(1),
    created_at: hoursAgo(20),
    updated_at: hoursAgo(20),
    updated_by: null,
    path_state: "n/a",
    path_detail: null,
  },
];

/* ------------------------------- Broadcastify feed statistics (docs/BCFY_STATS.md) */

/**
 * A believable week of feed statistics.
 *
 * The shape is the point rather than the numbers: quiet overnight, a bump at the
 * morning shift change and a clear peak at 17:00, with call volume following the
 * same curve — so the two heatmaps, the hot-hours list and the correlation all say
 * something recognizable in the mock and in tests, without a backend.
 */
const LISTENER_SHAPE = [
  3, 2, 2, 2, 2, 3, 6, 11, 14, 13, 12, 13, 14, 13, 13, 15, 22, 31, 27, 21, 16, 11, 7, 4,
];

/** Calls per hour, deliberately the same curve at a different scale. */
const CALL_SHAPE = LISTENER_SHAPE.map((value) => Math.round(value * 2.4));

function hourOfWeekMatrix(shape: number[], weekendFactor = 0.6): number[][] {
  return Array.from({ length: 7 }, (_, weekday) =>
    shape.map((value) => Math.round(value * (weekday >= 5 ? weekendFactor : 1) * 10) / 10),
  );
}

/**
 * The daily export's rows, as an import of it would have left them.
 *
 * 180 days regardless of the window, sliced to it below — which is the point: the daily
 * history is the only half of this page that covers a long window, and the mock has to
 * show a year-long selection with something in it. One day is a deliberate incident
 * (twenty times a normal peak, and 71% uptime), because the real files have three such
 * days in them and a chart that cannot survive one is a chart that hides them.
 */
export const FEED_DAILY: FeedDailyPoint[] = Array.from({ length: 180 }, (_, index) => {
  const day = new Date(NOW - (179 - index) * 24 * 3600 * 1000);
  const incident = index === 42;
  return {
    day: day.toISOString().slice(0, 10),
    uptime_pct: incident ? 71.4 : index % 17 === 0 ? 99.72 : 100,
    listeners_max: incident ? 479 : 22 + ((index * 7) % 19),
  };
});

export function feedSummary(days: number): FeedSummary {
  const listeners: (number | null)[][] = hourOfWeekMatrix(LISTENER_SHAPE);
  const calls = hourOfWeekMatrix(CALL_SHAPE);
  // Sunday 03:00-06:00 has never been polled, so the mock exercises the thing the real
  // page has to get right: an unmeasured cell is `null` and must not read as a quiet one.
  for (let hour = 3; hour < 6; hour += 1) (listeners[6] as (number | null)[])[hour] = null;
  const cells = listeners.flatMap((row, weekday) =>
    row.map((value, hour) => ({
      weekday,
      hour,
      listeners_avg: value,
      calls: Math.round(calls[weekday]?.[hour] ?? 0),
      samples: value === null ? 0 : 24,
    })),
  );
  const daily = FEED_DAILY.slice(Math.max(0, FEED_DAILY.length - days));
  const peakDay = daily.reduce(
    (best, row) => ((row.listeners_max ?? 0) > (best?.listeners_max ?? 0) ? row : best),
    daily[0],
  );
  const measured = cells.filter((cell) => cell.listeners_avg !== null);
  const hottest = [...measured]
    .sort((a, b) => (b.listeners_avg ?? 0) - (a.listeners_avg ?? 0) || b.calls - a.calls)
    .slice(0, 10);
  const quietest = [...measured]
    .sort((a, b) => (a.listeners_avg ?? 0) - (b.listeners_avg ?? 0) || a.calls - b.calls)
    .slice(0, 10);
  return {
    feed_id: "2211",
    days,
    from: new Date(NOW - days * 24 * 3600 * 1000).toISOString(),
    to: new Date(NOW).toISOString(),
    timezone: "America/Denver",
    current_listeners: 24,
    online: true,
    last_poll_at: new Date(NOW - 40_000).toISOString(),
    // The window's peak is the imported incident day, not anything this deployment
    // polled — which is exactly the case `peak_source` exists to label, and the reason
    // `peak_at` here is a midnight rather than a minute.
    peak_listeners: peakDay?.listeners_max ?? 41,
    peak_at: peakDay ? `${peakDay.day}T00:00:00Z` : new Date(NOW).toISOString(),
    peak_source: "daily" as const,
    hours_sampled: days * 24,
    listeners_hours_covered: days * 24,
    // The oldest day of the window came from the public page's sparkline, which is what
    // the coverage line's "N from the public page" suffix is there to say.
    hours_from_page: 24,
    // …and a week of it came from the dashboard's hourly export, which is the other
    // half of that line.
    hours_from_import: 168,
    daily,
    daily_days_covered: daily.length,
    history_from: FEED_DAILY[0]?.day ?? null,
    first_sample_at: new Date(NOW - days * 24 * 3600 * 1000).toISOString(),
    calls_hours_covered: days * 24,
    window_hours: days * 24,
    listeners_hour_of_week: listeners,
    listeners_max_hour_of_week: listeners.map((row) =>
      row.map((value) => (value === null ? null : Math.round(value * 1.4))),
    ),
    listeners_samples_hour_of_week: listeners.map((row) => row.map((value) => (value === null ? 0 : 24))),
    calls_hour_of_week: calls,
    hottest_hours: hottest,
    quietest_hours: quietest,
    quietest_basis: "listeners" as const,
    quiet_windows: [
      { weekday: 2, start_hour: 2, end_hour: 5, listeners_avg: 3.2, calls_avg: 7.4 },
      { weekday: 6, start_hour: 23, end_hour: 2, listeners_avg: 3.6, calls_avg: 6.1 },
      { weekday: 0, start_hour: 3, end_hour: 5, listeners_avg: 4.1, calls_avg: 8.8 },
    ],
    calls_total: calls.flat().reduce((total, value) => total + Math.round(value), 0) * 4,
    correlation: 0.87,
    correlation_hours: days * 24,
  };
}

export function feedSamples(count: number): FeedSamplePoint[] {
  return Array.from({ length: count }, (_, index) => {
    const bucket = new Date(NOW - (count - 1 - index) * 3600 * 1000);
    const average = LISTENER_SHAPE[bucket.getHours()] ?? 5;
    return {
      bucket: bucket.toISOString(),
      listeners_avg: average,
      listeners_max: average + 3,
      samples: 60,
    };
  });
}

/** Half-hour archive blocks over the last two days, newest first. */
export const FEED_ARCHIVES: FeedArchive[] = Array.from({ length: 12 }, (_, index) => {
  const start = new Date(NOW - (index + 1) * 30 * 60 * 1000);
  return {
    day: start.toISOString().slice(0, 10),
    start_at: start.toISOString(),
    end_at: new Date(start.getTime() + 30 * 60 * 1000).toISOString(),
    url: `https://archive.broadcastify.com/2211/demo-${index}.mp3`,
    size_bytes: 3_600_000,
  };
});
