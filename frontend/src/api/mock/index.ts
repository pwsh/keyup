/**
 * In-memory mock API (`VITE_MOCK=1`, and the dev fallback when the real API is
 * unreachable). Routed from `client.ts`, so every hook, page and test exercises
 * exactly the same interface as the real backend — including its errors.
 */

import { talkgroupLabel } from "../../lib/format";
import { ApiError, type ApiResponse, type BlobResponse, type HttpMethod, type QueryParams } from "../client";
import type {
  ActivityEvent,
  ActivityResponse,
  AdminAgency,
  AdminIngestResponse,
  AlertRule,
  ApiTokenCreated,
  ApiTokenInfo,
  AssignedRadio,
  AudioProfile,
  AudioProfileStep,
  CallDetail,
  ConversationThread,
  DiskStatus,
  ExportJob,
  ExportStatus,
  ExportTestResult,
  Favorite,
  FeedArchive,
  FeedImportReport,
  FeedPreset,
  FeedProbeResult,
  FeedPublicPageResult,
  FeedSamplePoint,
  FeedSummary,
  HttpsApplyResult,
  HttpsDnsTestResult,
  HttpsStatus,
  ImportResult,
  IngestLag,
  IngestSourceRow,
  IngestSourceStatus,
  IngestSourcesView,
  LiveActivitySnapshot,
  LoginRequest,
  MeResponse,
  Paginated,
  Person,
  PersonDetail,
  RadioAssignment,
  RadioDetail,
  RadioHistory,
  RadioStat,
  Recorder,
  RecorderPolicyInput,
  RetentionPolicy,
  RetentionRunResult,
  SavedSearch,
  SearchResponse,
  SettingDefinition,
  SiteChannelAudit,
  SiteFrequency,
  SiteInfo,
  SiteMessageKind,
  SiteOccupancy,
  SiteProfile,
  StatsOverview,
  SttBackendDeleteResult,
  SttBackendProbe,
  SttBackendStat,
  SttBackendsView,
  Suggestion,
  SyndicationFeed,
  SystemChannelAudit,
  SystemInfo,
  SystemStatus,
  Talkgroup,
  TalkgroupExportFlagCopyResult,
  TalkgroupStat,
  TimelinePoint,
  TokenScope,
  TranscribeAccepted,
  Transcript,
  TranscriptionStats,
  TunerDesiredInput,
  Unit,
  UnitDetail,
  User,
  Waveform,
} from "../types";
import { AGENCY_CODE_RE } from "../types";
import {
  ADMIN_AGENCIES,
  AFFILIATIONS,
  AGENCIES,
  ALERTS,
  ASSIGNMENTS,
  AUDIO_PROFILES,
  AUDIO_STATUS,
  AUDIO_STEP_CATALOG,
  activityByDayHour,
  activityByTalkgroupHour,
  activityLog,
  BACKENDS,
  CALLS,
  CHANNEL_AUDITS,
  CONVERSATIONS,
  EVENT_LOG_ARCHIVES,
  EXPORT_STATUS,
  FEED_ARCHIVES,
  FEED_DAILY,
  feedSamples,
  feedSummary,
  GAPS,
  HTTPS_STATUS,
  INGEST_FILES,
  INGEST_RUNS,
  INGEST_SOURCES,
  JOBS,
  LINKS,
  liveActivity,
  type MockCall,
  mockPeaks,
  NOTIFICATIONS,
  OCCUPANCY,
  PATCHES,
  PERSONNEL,
  RADIO_EVENTS,
  RADIOS,
  RECORDERS,
  RETENTION_POLICIES,
  radioLabel,
  SAVED_SEARCHES,
  SETTINGS,
  SITE_FREQUENCIES,
  SITE_MESSAGE_KINDS,
  SITE_PROFILES,
  SITES,
  SOURCE_QUALITY,
  SOURCE_QUALITY_CALL_ID,
  STT_BACKEND_STATS,
  STT_QUEUE,
  SYSTEM,
  SYSTEM_CHANNEL_AUDIT,
  SYSTEMS,
  sparkFor,
  TALKGROUPS,
  TOKENS,
  UNITS,
  USERS,
  VOCABULARY,
} from "./dataset";
import { buildFacets, filterCalls, list, page, sortCalls, toCall } from "./filter";

/* ---------------------------------------------------------- mutable state */

let session: User | null = null;
let favorites: Favorite[] = [
  { talkgroup_id: 1, tgid: 1101, alias: "EC FIRE DISPATCH", muted: false },
  { talkgroup_id: 6, tgid: 50051, alias: "RC LAW DISPATCH", muted: false },
];
let savedSearches: SavedSearch[] = [...SAVED_SEARCHES];
/** The audio disk as the demo reports it: comfortable, filling at a few GB a day. */
const MOCK_DISK: DiskStatus = {
  path: "/data/audio",
  free_bytes: 412 * 1024 ** 3,
  total_bytes: 1024 * 1024 ** 3,
  free_percent: 40.23,
  level: "ok",
  growth_bytes_per_day: 3.2 * 1024 ** 3,
  days_to_full: 128.75,
};
/** Published feeds. The tokens here are obvious fakes — mock mode must never look like a
 *  place a real credential could be pasted. */
let feeds: SyndicationFeed[] = [];
/** The built-in feeds, which hold no token at all and are the same on every deployment
 *  that has these categories in its directory. */
const FEED_PRESETS: FeedPreset[] = [
  {
    slug: "all",
    name: "Keyup · All calls",
    description: "Every call in the archive, newest first — the whole thing, as it is transcribed.",
    query: {},
    talkgroups: 12,
    calls_7d: 412,
    top_talkgroups: [
      { tgid: 1101, alias: "EC EVENT 6", calls_7d: 168 },
      { tgid: 1001, alias: "EC LAW DISPATCH", calls_7d: 121 },
      { tgid: 1003, alias: "EC FIRE DISPATCH", calls_7d: 74 },
    ],
    rss_url: "http://localhost/api/feeds/preset/all.rss",
    atom_url: "http://localhost/api/feeds/preset/all.atom",
  },
  {
    slug: "emergency",
    name: "Keyup · Emergency dispatch",
    description:
      "The majority of emergency traffic in one feed: law enforcement, fire, EMS, the multi-service event channels and the interoperability channels the agencies share on an incident.",
    query: { category: ["law", "fire", "ems", "multi", "interop"] },
    talkgroups: 9,
    calls_7d: 380,
    top_talkgroups: [
      { tgid: 1101, alias: "EC EVENT 6", calls_7d: 168 },
      { tgid: 1001, alias: "EC LAW DISPATCH", calls_7d: 121 },
    ],
    rss_url: "http://localhost/api/feeds/preset/emergency.rss",
    atom_url: "http://localhost/api/feeds/preset/emergency.atom",
  },
  {
    slug: "law",
    name: "Keyup · Law enforcement",
    description:
      "Police, sheriff and campus-police traffic — dispatch, car-to-car and tactical channels alike.",
    query: { category: ["law"] },
    talkgroups: 7,
    calls_7d: 194,
    top_talkgroups: [
      { tgid: 1001, alias: "EC LAW DISPATCH", calls_7d: 121 },
      { tgid: 1002, alias: "EC LAW TAC", calls_7d: 73 },
    ],
    rss_url: "http://localhost/api/feeds/preset/law.rss",
    atom_url: "http://localhost/api/feeds/preset/law.atom",
  },
  {
    slug: "fire",
    name: "Keyup · Fire",
    description: "Fire dispatch, fireground and department talk channels.",
    query: { category: ["fire"] },
    talkgroups: 4,
    calls_7d: 96,
    top_talkgroups: [
      { tgid: 1101, alias: "EC EVENT 6", calls_7d: 62 },
      { tgid: 1003, alias: "EC FIRE DISPATCH", calls_7d: 34 },
    ],
    rss_url: "http://localhost/api/feeds/preset/fire.rss",
    atom_url: "http://localhost/api/feeds/preset/fire.atom",
  },
];
let alerts: AlertRule[] = [...ALERTS];
let tokens: ApiTokenInfo[] = [...TOKENS];
let agencies: AdminAgency[] = ADMIN_AGENCIES.map((agency) => ({ ...agency }));
let users: User[] = [...USERS];
let settings: SettingDefinition[] = SETTINGS.map((s) => ({ ...s }));
let retention: RetentionPolicy[] = RETENTION_POLICIES.map((p) => ({ ...p }));
// Copied rather than shared: the enable/disable switch writes to these, and a test that
// turns a backend off must not change what the next test sees.
const sttBackends: SttBackendStat[] = STT_BACKEND_STATS.map((b) => ({ ...b }));
let sttConfigVersion = 4;
//: deliberately behind ``sttConfigVersion`` after a save, so the demo shows the page's
//: "the worker has not picked this up yet" state rather than pretending reloads are instant
let sttAppliedVersion = 4;
const ingestSources: IngestSourceRow[] = INGEST_SOURCES.map((source: IngestSourceRow) => ({ ...source }));
let ingestConfigVersion = 3;
let nextSourceId = 5;

/** A backend row from a create/edit body, with the counters a new backend has: none. */
function backendRowFrom(name: string, input: Record<string, unknown>): SttBackendStat {
  const enabled = input.enabled !== false;
  return {
    name,
    kind: String(input.kind ?? "openai_compatible"),
    model: String(input.model ?? ""),
    base_url: String(input.base_url ?? "") || null,
    priority: Number(input.priority ?? 100),
    concurrency: Number(input.concurrency ?? 2),
    enabled,
    config_enabled: enabled,
    admin_enabled: null,
    admin_changed_at: null,
    admin_changed_by: null,
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
    worker_instance: null,
    started_at: null,
    updated_at: null,
    stale: true,
    has_api_key: Boolean(input.api_key),
    config_source: "db",
    device: (input.device as string) || null,
    compute_type: (input.compute_type as string) || null,
    cpu_threads: Number(input.cpu_threads ?? 4),
    cache_dir: (input.cache_dir as string) || null,
    use_prompt: input.use_prompt !== false,
    use_hotwords: input.use_hotwords === true,
    timeout: Number(input.timeout ?? 300),
    notes: (input.notes as string) ?? null,
    deleted_at: null,
    editable: true,
    config_version: null,
  };
}

/** A source row from a create/edit body; the demo's paths are all "ok" but /mnt/attic. */
function sourceRowFrom(id: number, input: Record<string, unknown>): IngestSourceRow {
  const path = (input.path as string) || null;
  const missing = path?.startsWith("/mnt/") ?? false;
  const now = new Date().toISOString();
  const tokenId = (input.token_id as number | null) ?? null;
  return {
    id,
    kind: (input.kind as IngestSourceRow["kind"]) ?? "watch_dir",
    label: String(input.label ?? ""),
    enabled: input.enabled !== false,
    path,
    recursive: input.recursive !== false,
    tr_prefer_audio: (input.tr_prefer_audio as string) ?? null,
    tr_sidecar_wait_seconds: (input.tr_sidecar_wait_seconds as number) ?? null,
    token_id: tokenId,
    token_label: tokenId === null ? null : `token-${tokenId}`,
    token_active: tokenId === null ? null : true,
    notes: (input.notes as string) ?? null,
    meta: null,
    last_activity_at: null,
    last_error: null,
    last_error_at: null,
    created_at: now,
    updated_at: now,
    updated_by: "operator",
    path_state: path === null ? "n/a" : missing ? "missing" : "ok",
    path_detail: missing ? `${path} does not exist` : null,
  };
}
let recorders: Recorder[] = RECORDERS.map((r) => ({
  ...r,
  tuners: r.tuners.map((t) => ({ ...t })),
}));
// Copied rather than shared, for the same reason the STT backends are: the site
// editor writes to these, and a test that renames or deletes one must not change
// what the next test sees.
let sites: SiteInfo[] = SITES.map((site) => ({ ...site, blocking: { ...site.blocking } }));
let systems: SystemInfo[] = SYSTEMS.map((system) => ({ ...system, blocking: { ...system.blocking } }));
const exports = new Map<string, ExportJob>();
let nextId = 90_000;

/** The profiles, as a mutable copy: the editor creates, renames and deletes them. */
let audioProfiles: AudioProfile[] = AUDIO_PROFILES.map((profile) => ({ ...profile }));

/** `GET /auth/me` — the same envelope for a signed-in and an anonymous caller. */
const me = (user: User | null): MeResponse => ({
  user,
  scopes: user?.scopes ?? [],
  auth_kind: user ? "session" : null,
  auth_required_read: false,
});

/** Tests start each case signed out with the fixtures untouched. */
export function resetMockState(): void {
  session = null;
  favorites = [
    { talkgroup_id: 1, tgid: 1101, alias: "EC FIRE DISPATCH", muted: false },
    { talkgroup_id: 6, tgid: 50051, alias: "RC LAW DISPATCH", muted: false },
  ];
  savedSearches = [...SAVED_SEARCHES];
  feeds = [];
  alerts = [...ALERTS];
  tokens = [...TOKENS];
  agencies = ADMIN_AGENCIES.map((agency) => ({ ...agency }));
  users = [...USERS];
  settings = SETTINGS.map((s) => ({ ...s }));
  retention = RETENTION_POLICIES.map((p) => ({ ...p }));
  audioProfiles = AUDIO_PROFILES.map((profile) => ({ ...profile }));
  recorders = RECORDERS.map((r) => ({ ...r, tuners: r.tuners.map((t) => ({ ...t })) }));
  sites = SITES.map((site) => ({ ...site, blocking: { ...site.blocking } }));
  sttBackends.splice(0, sttBackends.length, ...STT_BACKEND_STATS.map((b) => ({ ...b })));
  ingestSources.splice(
    0,
    ingestSources.length,
    ...INGEST_SOURCES.map((source: IngestSourceRow) => ({ ...source })),
  );
  sttConfigVersion = 4;
  sttAppliedVersion = 4;
  ingestConfigVersion = 3;
  nextSourceId = 5;
  exports.clear();
}

/** True while someone is signed in — the mock's stand-in for a session cookie. */
export const mockSignedIn = (): boolean => session !== null;

/* ------------------------------------------------------------- helpers */

const ok = <T>(data: T, extraHeaders?: Record<string, string>): ApiResponse<T> => ({
  data,
  headers: new Headers({ "Content-Type": "application/json", ...extraHeaders }),
});

const notFound = (path: string): never => {
  throw new ApiError("404 Not Found", 404, path, `No such resource: ${path}`);
};

/** Mutations need a session, exactly as the real API requires one. */
const requireSession = (path: string): User => {
  if (!session) throw new ApiError("401 Unauthorized", 401, path, "Sign in to change anything.");
  return session;
};

/** The mock's stand-in for the server's `describe_filters` — enough to prove the summary
 *  reaches the page, not a second implementation of the filter language. */
const describeMockFilters = (query: Record<string, unknown>): string => {
  const parts: string[] = [];
  if (query.q) parts.push(`matching "${String(query.q)}"`);
  const talkgroups = query.talkgroup as unknown[] | undefined;
  if (talkgroups?.length) parts.push(`talkgroups ${talkgroups.join(", ")}`);
  const agencies_ = query.agency as unknown[] | undefined;
  if (agencies_?.length) parts.push(`agencies ${agencies_.join(", ")}`);
  if (query.emergency) parts.push("emergency only");
  return parts.length ? parts.join(" \u00b7 ") : "Every call in the archive";
};

const paginate = <T>(items: T[], params: QueryParams): Paginated<T> => {
  const { page: current, pageSize, cursor } = page(params);
  const start = (current - 1) * pageSize;
  return {
    items: items.slice(start, start + pageSize),
    // Like the API: a cursor page is a scroll, and a scroll is not counted.
    total: cursor ? null : items.length,
    page: current,
    page_size: pageSize,
    next_cursor: start + pageSize < items.length ? `p${current + 1}` : null,
  };
};

const findCall = (id: number): MockCall | undefined => CALLS.find((call) => call.id === id);

/** Wrap the query's terms in `<mark>` the way `ts_headline` would. */
function highlight(text: string, query: string | undefined): string {
  if (!query) return text;
  const terms = (query.match(/"[^"]*"|\S+/g) ?? [])
    .filter((term) => !term.startsWith("-") && term !== "or" && term !== "and")
    .map((term) => term.replace(/^"|"$/g, ""))
    .filter(Boolean)
    .map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return text;
  return text.replace(new RegExp(`(${terms.join("|")})`, "gi"), "<mark>$1</mark>");
}

const detailFor = (call: MockCall): CallDetail => {
  const sameTalkgroup = CALLS.filter((c) => c.tgid === call.tgid).sort(
    (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at),
  );
  const index = sameTalkgroup.findIndex((c) => c.id === call.id);
  const thread = call.conversation_id
    ? CALLS.filter((c) => c.conversation_id === call.conversation_id).sort(
        (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at),
      )
    : sameTalkgroup.slice(Math.max(0, index - 2), index + 3);
  const conversation = CONVERSATIONS.find((c) => c.id === call.conversation_id) ?? null;
  return {
    ...toCall(call),
    system: SYSTEM,
    site: sites.find((site) => site.id === call.site_id) ?? null,
    talkgroup: TALKGROUPS.find((tg) => tg.id === call.talkgroup_id) ?? null,
    radio: RADIOS.find((radio) => radio.id === call.radio_id) ?? null,
    target_radio: RADIOS.find((radio) => radio.id === call.target_radio_id) ?? null,
    patch: PATCHES.find((patch) => patch.id === call.patch_id) ?? null,
    raw: call.raw,
    sources: call.sources,
    // Reconciliation (docs/RECONCILIATION.md): the demo archive has one recorder, so no
    // call in it has a second copy.
    duplicates: [],
    reconcile_reason: null,
    reconciled_at: null,
    // One call was heard by the second receiver, so the detail page's source line renders.
    source_instance: call.id === SOURCE_QUALITY_CALL_ID ? (SOURCE_QUALITY.instance ?? null) : null,
    source_quality: call.id === SOURCE_QUALITY_CALL_ID ? SOURCE_QUALITY : null,
    transcript: call.transcripts.find((t) => t.is_primary) ?? null,
    transcript_count: call.transcripts.length,
    prev_call_id: sameTalkgroup[index - 1]?.id ?? null,
    next_call_id: sameTalkgroup[index + 1]?.id ?? null,
    // The record, not the calls: the thread is `GET /conversations/{id}`.
    conversation,
    conversation_call_ids: thread.map((c) => c.id),
    links: LINKS.filter((link) => link.call_id === call.id),
  };
};

const talkgroupStats = (): TalkgroupStat[] =>
  TALKGROUPS.map((tg) => {
    const own = CALLS.filter((c) => c.talkgroup_ids.includes(tg.tgid));
    return {
      talkgroup_id: tg.id,
      tgid: tg.tgid,
      alias: talkgroupLabel(tg),
      category: tg.category,
      agency_code: tg.agency_code,
      call_count: own.length,
      audio_seconds: Math.round(own.reduce((sum, c) => sum + c.duration_s, 0)),
      last_heard_at: tg.last_seen_at,
      spark: sparkFor((call) => call.talkgroup_ids.includes(tg.tgid)),
    };
  }).sort((a, b) => b.call_count - a.call_count);

const radioStats = (): RadioStat[] =>
  RADIOS.map((radio) => {
    const own = CALLS.filter((c) => c.radio_id === radio.id);
    const byTalkgroup = new Map<string, number>();
    for (const call of own) {
      const alias = call.talkgroup_alias ?? "";
      byTalkgroup.set(alias, (byTalkgroup.get(alias) ?? 0) + 1);
    }
    const top = [...byTalkgroup.entries()].sort((a, b) => b[1] - a[1])[0];
    return {
      radio_id: radio.id,
      radio_id_raw: radio.radio_id,
      alias: radio.alias,
      unit_id: radio.unit_id,
      kind: radio.kind,
      agency_code: radio.agency_code,
      call_count: own.length,
      audio_seconds: Math.round(own.reduce((sum, c) => sum + c.duration_s, 0)),
      last_heard_at: radio.last_seen_at,
      top_talkgroup: top?.[0] ?? null,
      spark: sparkFor((call) => call.radio_id === radio.id),
    };
  }).sort((a, b) => b.call_count - a.call_count);

const ingestStatus = () => {
  const newest = CALLS[0];
  const failed = INGEST_FILES.filter((file) => file.status === "failed").length;
  return {
    watching: true,
    audio_root: "/data/audio",
    last_run_at: INGEST_RUNS[0]?.finished_at ?? null,
    last_run_files_new: INGEST_RUNS[0]?.files_new ?? 0,
    files_seen_24h: INGEST_RUNS.reduce((sum, run) => sum + run.files_seen, 0),
    files_new_24h: INGEST_RUNS.reduce((sum, run) => sum + run.files_new, 0),
    files_duplicate_24h: INGEST_RUNS.reduce((sum, run) => sum + run.files_duplicate, 0),
    files_failed_24h: failed,
    lag_s: newest ? Math.round((Date.now() - Date.parse(newest.ended_at)) / 1000) : null,
    pending_files: 0,
    calls: CALLS.length,
    duplicates: CALLS.filter((call) => call.duplicate_of !== null).length,
    ledger: { ingested: CALLS.length, failed },
    jobs: {
      queued: JOBS.filter((job) => job.status === "queued").length,
      running: JOBS.filter((job) => job.status === "running").length,
      done: JOBS.filter((job) => job.status === "done").length,
    },
    // The API recomputes the scanned half of this block at most once a minute; the demo
    // has nothing to scan, so it reports itself as measured just now.
    as_of: new Date().toISOString(),
    cached_s: 0,
  };
};

const overview = (): StatsOverview => {
  const dayAgo = Date.now() - 86_400_000;
  const today = CALLS.filter((call) => Date.parse(call.started_at) >= dayAgo);
  const eligible = CALLS.filter((call) => call.transcript_status !== "skipped");
  const done = eligible.filter((call) => call.transcript_status === "done");
  return {
    calls_total: CALLS.length,
    calls_today: today.length,
    calls_24h: today.length,
    // A subset of `calls_24h`, not an addition to it.
    unit_calls_24h: today.filter((call) => call.kind === "unit").length,
    audio_hours_today: Number((today.reduce((s, c) => s + c.duration_s, 0) / 3600).toFixed(2)),
    audio_hours_total: Number((CALLS.reduce((s, c) => s + c.duration_s, 0) / 3600).toFixed(2)),
    audio_discarded_24h: 0,
    no_speech_24h: 0,
    static_24h: 0,
    transcript_coverage: eligible.length ? done.length / eligible.length : 0,
    transcripts_total: done.length,
    stt_rtf: 0.14,
    queue_depth: JOBS.filter((job) => job.status === "queued").length,
    running_jobs: JOBS.filter((job) => job.status === "running").length,
    failed_jobs: JOBS.filter((job) => job.status === "failed").length,
    ingest_lag_s: ingestStatus().lag_s,
    recorder_gaps_24h: GAPS.filter((gap) => Date.parse(gap.started_at) >= dayAgo).length,
    talkgroups_active_24h: new Set(today.map((call) => call.talkgroup_id)).size,
    radios_active_24h: new Set(today.map((call) => call.radio_id).filter(Boolean)).size,
    disk_free_bytes: 412 * 1024 ** 3,
    disk_total_bytes: 1024 * 1024 ** 3,
    last_call_at: CALLS[0]?.started_at ?? null,
  };
};

const transcriptionStats = (): TranscriptionStats => {
  const count = <T extends string>(values: (T | null)[]) => {
    const map = new Map<T, number>();
    for (const value of values) if (value) map.set(value, (map.get(value) ?? 0) + 1);
    return map;
  };
  const quality = count(CALLS.map((call) => call.transcript_quality));
  const status = count(CALLS.map((call) => call.transcript_status));
  return {
    queue_depth: JOBS.filter((job) => job.status === "queued").length,
    running: JOBS.filter((job) => job.status === "running").length,
    failed_24h: JOBS.filter((job) => job.status === "failed").length,
    quality: [...quality.entries()].map(([q, c]) => ({ quality: q, count: c })),
    status: [...status.entries()].map(([s, c]) => ({ status: s, count: c })),
    backends: BACKENDS.filter((backend) => backend.enabled).map((backend) => ({
      backend_name: backend.name,
      engine: backend.engine,
      calls: backend.calls_24h,
      audio_seconds: backend.calls_24h * 3.4,
      processing_seconds: backend.calls_24h * 3.4 * (backend.rtf ?? 1),
      rtf: backend.rtf ?? 0,
    })),
  };
};

/* -------------------------------------------------------------- roster */

/** The radios a unit or a person holds right now, busiest first. */
const assignedRadios = ({ unitId, personId }: { unitId?: number; personId?: number }): AssignedRadio[] =>
  ASSIGNMENTS.filter(
    (assignment) =>
      assignment.effective_to === null &&
      (unitId === undefined ? assignment.person?.id === personId : assignment.unit?.id === unitId),
  )
    .map((assignment) => {
      const radio = RADIOS.find((row) => row.id === assignment.radio_pk);
      if (!radio) return null;
      return {
        radio_pk: radio.id,
        radio_id: radio.radio_id,
        alias: radio.alias,
        unit_id: radio.unit_id,
        kind: radio.kind,
        call_count: radio.call_count,
        last_seen_at: radio.last_seen_at,
        assignment_id: assignment.id,
        effective_from: assignment.effective_from,
        note: assignment.note,
      } satisfies AssignedRadio;
    })
    .filter((row): row is AssignedRadio => row !== null)
    .sort((a, b) => b.call_count - a.call_count);

/** Close a radio's current assignment of one kind, the way the API does. */
const closeCurrent = (radioId: number, kind: "unit" | "person"): RadioAssignment | null => {
  const current = ASSIGNMENTS.find(
    (assignment) =>
      assignment.radio_pk === radioId && assignment.effective_to === null && assignment.kind === kind,
  );
  if (!current) return null;
  current.effective_to = new Date().toISOString();
  current.current = false;
  return current;
};

/* ------------------------------------------------------------- the router */

/**
 * `POST /{resource}/bulk` against the fixture arrays.
 *
 * Kept faithful to the two rules the drawer depends on: only the keys actually
 * present in `changes` are written (so a mock cannot hide a form that sends
 * `{rank: undefined}`), and more than 500 ids is the 422 the server gives — the
 * selection bar disables Edit on that, and this is what would catch it if it
 * stopped.
 */
function bulkPatch<T extends { id: number }>(
  rows: T[],
  path: string,
  payload: unknown,
): { updated: number; items: T[] } {
  const body = (payload ?? {}) as { ids?: number[]; changes?: Record<string, unknown> };
  const ids = [...new Set(body.ids ?? [])];
  const changes = body.changes ?? {};
  if (ids.length === 0 || ids.length > 500) {
    throw new ApiError("422 Unprocessable Entity", 422, path, "ids: 1 to 500 rows");
  }
  if (Object.keys(changes).length === 0) {
    throw new ApiError("422 Unprocessable Entity", 422, path, "changes must set at least one field");
  }
  const items: T[] = [];
  for (const id of ids) {
    const row = rows.find((candidate) => candidate.id === id);
    if (!row) throw new ApiError("404 Not Found", 404, path, `no row ${id}`);
    Object.assign(row, changes);
    items.push(row);
  }
  return { updated: items.length, items };
}

/**
 * One flat routing table on purpose: it reads, and diffs against the OpenAPI
 * spec, far better than a dozen indirections would.
 */

/**
 * The non-blank keys of a test request's `overrides` — the card's unsaved values. A blank
 * one means "leave the stored value alone", exactly as it does on the real endpoint, so
 * the mock's `used_draft` says the same thing the server's would.
 */
const draftKeys = (payload: Record<string, unknown>): Record<string, string> => {
  const overrides = (payload.overrides ?? {}) as Record<string, unknown>;
  return Object.fromEntries(
    Object.entries(overrides)
      .filter(([, value]) => String(value ?? "").trim() !== "")
      .map(([key, value]) => [key, String(value)]),
  );
};

/* ------------------------------------------------------- audio profiles */

/**
 * The mock's copy of `app.audioproc.profiles`, small enough to be worth having twice.
 *
 * The demo's editor is only honest if a chain it builds comes back rendered the way the
 * server would render it and refused the way the server would refuse it — a fixture that
 * accepted anything would let the form ship with a field the API rejects. So the two
 * functions below mirror the whitelist's arithmetic: the option names that differ from the
 * parameter names, the fixed suffixes, `agate`'s dBFS-to-linear threshold, and the
 * loudness tail no profile can remove. The whitelist itself is not duplicated — both read
 * `AUDIO_STEP_CATALOG`, which is the server's own table.
 */
const FILTER_NAME: Record<string, string> = { notch: "equalizer" };
const FIXED_PARTS: Record<string, string[]> = {
  bandreject: ["t=h"],
  equalizer: ["t=h"],
  notch: ["t=q"],
};
const OPTION_NAMES: Record<string, Record<string, string>> = {
  bandreject: { width: "w" },
  equalizer: { width: "w", gain_db: "g" },
  notch: { q: "w", gain_db: "g" },
};

/** A filter-graph number: fixed point, no exponent, no trailing zeros. */
const filterNumber = (value: number, places = 3): string => {
  const text = value.toFixed(places);
  return (text.includes(".") ? text.replace(/0+$/, "").replace(/\.$/, "") : text) || "0";
};

/** Validate a chain and fill in its defaults, exactly as `normalize_chain` does. */
function normalizeMockChain(steps: AudioProfileStep[], path: string): AudioProfileStep[] {
  const refuse = (detail: string): never => {
    throw new ApiError("422 Unprocessable Entity", 422, path, detail);
  };
  if (steps.length > AUDIO_STEP_CATALOG.max_steps) {
    refuse(`a chain may hold at most ${AUDIO_STEP_CATALOG.max_steps} steps (got ${steps.length})`);
  }
  return steps.map((step, index) => {
    const spec = AUDIO_STEP_CATALOG.steps.find((candidate) => candidate.kind === step.kind);
    if (!spec) {
      const known = AUDIO_STEP_CATALOG.steps.map((candidate) => candidate.kind).sort();
      refuse(`step ${index + 1}: unknown filter '${step.kind}' (known kinds: ${known.join(", ")})`);
      throw new Error("unreachable");
    }
    const params: Record<string, number> = {};
    for (const param of spec.params) {
      const given = step.params?.[param.name];
      const value = given ?? param.default;
      if (value === null || value === undefined) continue;
      if (!Number.isFinite(value) || value < param.low || value > param.high) {
        const unit = param.unit ? ` ${param.unit}` : "";
        refuse(
          `${spec.kind}.${param.name} must be between ${param.low} and ${param.high}${unit} ` +
            `(got ${value})`,
        );
      }
      params[param.name] = value;
    }
    return { kind: spec.kind, params };
  });
}

/** The whole standalone `-af` string for a chain, loudness tail included. */
function renderMockChain(steps: AudioProfileStep[]): string {
  const stages = steps.map((step) => {
    const spec = AUDIO_STEP_CATALOG.steps.find((candidate) => candidate.kind === step.kind);
    if (!spec) return step.kind;
    const parts: string[] = [];
    for (const param of spec.params) {
      const value = step.params?.[param.name];
      if (value === undefined) continue;
      if (spec.kind === "agate" && param.name === "threshold") {
        parts.push(`threshold=${filterNumber(10 ** (value / 20), 6)}`);
        continue;
      }
      const places = spec.kind === "agate" && (param.name === "attack" || param.name === "release") ? 0 : 3;
      parts.push(`${OPTION_NAMES[spec.kind]?.[param.name] ?? param.name}=${filterNumber(value, places)}`);
    }
    parts.push(...(FIXED_PARTS[spec.kind] ?? []));
    const name = FILTER_NAME[spec.kind] ?? spec.kind;
    return parts.length ? `${name}=${parts.join(":")}` : name;
  });
  stages.push("loudnorm=I=-16:TP=-4:LRA=11", "alimiter=limit=0.630957:level=disabled:attack=5:release=50");
  return stages.join(",");
}

/**
 * A few bytes with an MP3 frame sync on the front.
 *
 * The demo has no audio to render and no ffmpeg to render it with, so the preview hands
 * back something an `<audio>` element will accept and refuse to play rather than nothing
 * at all: what the editor is being tested on here is that it makes the object URL, shows
 * the chain and swaps the two players over, none of which needs a real recording.
 */
const MOCK_MP3 = new Uint8Array([0xff, 0xfb, 0x90, 0x64, 0x00, 0x00, 0x00, 0x00]);

/**
 * The one route whose answer is a file rather than a document (`apiPostBlob`).
 *
 * It validates and renders the chain through the same two functions the stored profiles
 * go through, so a chain the editor previews successfully is one the fixtures would also
 * have stored — and a bad one comes back 422 with the server's own sentence.
 */
export async function handleMockBlob(path: string, body: unknown): Promise<BlobResponse> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (path !== "/admin/audio/profiles/preview") return notFound(path);
  requireSession(path);
  const { call_id: callId, chain } = (body ?? {}) as { call_id?: number; chain?: AudioProfileStep[] };
  if (!CALLS.some((call) => call.id === callId)) {
    throw new ApiError("404 Not Found", 404, path, `no call ${callId}`);
  }
  const filter = renderMockChain(normalizeMockChain(chain ?? [], path));
  return {
    blob: new Blob([MOCK_MP3], { type: "audio/mpeg" }),
    headers: new Headers({ "Content-Type": "audio/mpeg", "X-Keyup-Filter": filter }),
  };
}

export async function handleMock<T>(
  method: HttpMethod,
  path: string,
  params: QueryParams = {},
  body?: unknown,
): Promise<ApiResponse<T>> {
  // A small delay keeps loading states real in dev without making tests slow.
  await new Promise((resolve) => setTimeout(resolve, 0));
  const as = <R>(data: R): ApiResponse<T> => ok(data) as unknown as ApiResponse<T>;
  const payload = (body ?? {}) as Record<string, unknown>;

  /* ---------------------------------------------------------------- auth */
  if (path === "/auth/me") {
    return as<MeResponse>(me(session));
  }
  if (path === "/auth/login" && method === "POST") {
    const { username, password } = payload as unknown as LoginRequest;
    const user = users.find((candidate) => candidate.username === username);
    if (!user || password.length < 4) {
      throw new ApiError("401 Unauthorized", 401, path, "Wrong username or password.");
    }
    session = user;
    return as<MeResponse>(me(user));
  }
  if (path === "/auth/logout" && method === "POST") {
    session = null;
    return as<MeResponse>(me(null));
  }

  /* --------------------------------------------------------------- calls */
  if (path === "/calls") {
    const filtered = sortCalls(filterCalls(params), params.sort as string | undefined);
    const result = paginate(filtered.map(toCall), params);
    return ok({ ...result } as unknown as T, { "X-Total-Count": String(filtered.length) });
  }
  if (path === "/calls/facets") {
    return as(buildFacets(filterCalls(params)));
  }
  if (path === "/calls/playlist.m3u") {
    const filtered = sortCalls(filterCalls(params), params.sort as string | undefined);
    const lines = ["#EXTM3U"];
    for (const call of filtered.slice(0, 500)) {
      lines.push(
        `#EXTINF:${Math.round(call.duration_s)},${call.talkgroup_alias} — ${call.radio_label ?? "unknown"}`,
      );
      lines.push(`${window.location.origin}/api/calls/${call.id}/audio`);
    }
    return as(lines.join("\n"));
  }
  if (path === "/calls/export") {
    const filtered = sortCalls(filterCalls(params), params.sort as string | undefined);
    return as(filtered.map(toCall));
  }

  const callMatch = /^\/calls\/(\d+)(\/.*)?$/.exec(path);
  if (callMatch) {
    const call = findCall(Number(callMatch[1]));
    if (!call) return notFound(path);
    const sub = callMatch[2] ?? "";
    if (sub === "" && method === "GET") return as<CallDetail>(detailFor(call));
    if (sub === "" && method === "PATCH") {
      requireSession(path);
      if (typeof payload.notes === "string") call.notes = payload.notes;
      if (Array.isArray(payload.tags)) call.tags = payload.tags.map(String);
      if (typeof payload.starred === "boolean") call.starred = payload.starred;
      if (typeof payload.transcript_text === "string") {
        call.transcript_text = payload.transcript_text;
        const primary = call.transcripts.find((t) => t.is_primary);
        if (primary) {
          primary.edited_by_user = true;
          primary.edited_text = payload.transcript_text;
        }
      }
      call.updated_at = new Date().toISOString();
      return as<CallDetail>(detailFor(call));
    }
    if (sub === "" && method === "DELETE") {
      requireSession(path);
      return as<null>(null);
    }
    if (sub === "/transcripts") return as<Transcript[]>(call.transcripts);
    if (sub === "/waveform") {
      const peaks = mockPeaks(call.id, call.duration_s);
      return as<Waveform>({
        call_id: call.id,
        duration_s: call.duration_s,
        peaks,
        buckets: peaks.length,
      });
    }
    if (sub === "/transcribe" && method === "POST") {
      requireSession(path);
      call.transcript_status = "queued";
      return as<TranscribeAccepted>({
        call_id: call.id,
        job_id: ++nextId,
        status: "queued",
        detail: null,
      });
    }
    return notFound(path);
  }

  /* -------------------------------------------------------------- search */
  if (path === "/search") {
    const filtered = sortCalls(filterCalls(params), params.sort as string | undefined);
    const { page: current, pageSize } = page(params);
    const start = (current - 1) * pageSize;
    const q = params.q === undefined ? undefined : String(params.q);
    return as<SearchResponse>({
      items: filtered.slice(start, start + pageSize).map((call, index) => ({
        call: toCall(call),
        highlight: highlight(call.transcript_text ?? call.notes ?? "", q),
        rank: Number((1 - index / Math.max(1, pageSize)).toFixed(3)),
      })),
      total: filtered.length,
      total_capped: false,
      page: current,
      page_size: pageSize,
      facets: buildFacets(filtered),
      fuzzy: null,
    });
  }
  if (path === "/search/suggest") {
    const prefix = String(params.q ?? "").toLowerCase();
    const pool: Omit<Suggestion, "fuzzy">[] = [
      ...TALKGROUPS.map((tg) => ({
        value: talkgroupLabel(tg),
        kind: "talkgroup" as const,
        count: tg.call_count,
        label: tg.group_name,
      })),
      ...RADIOS.filter((r) => r.unit_id).map((r) => ({
        value: r.unit_id as string,
        kind: "unit" as const,
        count: r.call_count,
        label: r.alias,
      })),
      ...["structure fire", "traffic stop", "en route", "transporting", "ten-four"].map((term) => ({
        value: term,
        kind: "term" as const,
        count: CALLS.filter((c) => (c.transcript_text ?? "").toLowerCase().includes(term)).length,
        label: null,
      })),
    ];
    return as<Suggestion[]>(
      pool
        .filter((item) => !prefix || item.value.toLowerCase().includes(prefix))
        .slice(0, 12)
        .map((item) => ({ ...item, fuzzy: false })),
    );
  }

  /* ----------------------------------------------------------- directory */
  if (path === "/systems" && method === "GET") return as(systems);
  // System editing mirrors the site editing below it: PATCH needs a write scope, DELETE
  // and merge are admin, and the mock refuses a delete from `blocking` the way the API does.
  const systemMergeMatch = /^\/systems\/(\d+)\/merge-into\/(\d+)$/.exec(path);
  if (systemMergeMatch && method === "POST") {
    requireSession(path);
    const source = systems.find((row) => row.id === Number(systemMergeMatch[1]));
    const target = systems.find((row) => row.id === Number(systemMergeMatch[2]));
    if (!source || !target) return notFound(path);
    if (source.id === target.id) {
      throw new ApiError("409 Conflict", 409, path, "A system cannot be merged into itself.");
    }
    const moved = source.call_count;
    target.call_count += moved;
    target.site_count += source.site_count;
    systems = systems.filter((row) => row.id !== source.id);
    sites = sites.map((site) =>
      site.system_id === source.id ? { ...site, system_id: target.id, system_name: target.name } : site,
    );
    return as({
      source: { id: source.id, name: source.name },
      target,
      reparented: { sites: source.site_count },
      merged: {},
      rows_repointed: { "calls.system_id": moved },
      duplicates_dropped: {},
      deleted: { systems: 1 },
    });
  }
  const systemRrMatch = /^\/admin\/systems\/(\d+)\/radioreference$/.exec(path);
  if (systemRrMatch && method === "PUT") {
    requireSession(path);
    const system = systems.find((row) => row.id === Number(systemRrMatch[1]));
    if (!system) return notFound(path);
    system.rr_system_id = (payload.rr_id as number | null) ?? null;
    return as(system);
  }
  const systemMatch = /^\/systems\/(\d+)$/.exec(path);
  if (systemMatch && (method === "PATCH" || method === "DELETE")) {
    const system = systems.find((row) => row.id === Number(systemMatch[1]));
    if (!system) return notFound(path);
    requireSession(path);
    if (method === "PATCH") {
      if (
        typeof payload.name === "string" &&
        systems.some((row) => row.id !== system.id && row.name === payload.name)
      ) {
        throw new ApiError("409 Conflict", 409, path, "A system with that name already exists.");
      }
      Object.assign(system, payload);
      return as(system);
    }
    if (!system.deletable) {
      throw new ApiError("409 Conflict", 409, path, "Something still belongs to this system.");
    }
    systems = systems.filter((row) => row.id !== system.id);
    return as(null);
  }
  if (path === "/sites") {
    const system = params.system === undefined ? undefined : Number(params.system);
    const rows = sites.filter((site) => system === undefined || site.system_id === system);
    return ok(rows, { "X-Total-Count": String(rows.length) }) as ApiResponse<T>;
  }
  // Site editing: PATCH is a write scope, DELETE and merge are admin, exactly as the
  // real API has them — and the mock refuses a delete the same way, from `blocking`.
  const siteMergeMatch = /^\/sites\/(\d+)\/merge-into\/(\d+)$/.exec(path);
  if (siteMergeMatch && method === "POST") {
    requireSession(path);
    const source = sites.find((site) => site.id === Number(siteMergeMatch[1]));
    const target = sites.find((site) => site.id === Number(siteMergeMatch[2]));
    if (!source || !target) return notFound(path);
    if (source.id === target.id || source.system_id !== target.system_id) {
      throw new ApiError("409 Conflict", 409, path, "Sites must be different and on one system.");
    }
    const moved = source.call_count;
    target.call_count += moved;
    sites = sites.filter((site) => site.id !== source.id);
    return as({
      source: { id: source.id, name: source.name, system_id: source.system_id },
      target,
      moved: { calls: moved },
      batches: moved > 0 ? 1 : 0,
    });
  }
  const siteMatch = /^\/sites\/(\d+)$/.exec(path);
  if (siteMatch) {
    const site = sites.find((row) => row.id === Number(siteMatch[1]));
    if (!site) return notFound(path);
    if (method === "PATCH") {
      requireSession(path);
      Object.assign(site, payload);
      return as(site);
    }
    if (method === "DELETE") {
      requireSession(path);
      if (!site.deletable) {
        throw new ApiError("409 Conflict", 409, path, "Something still references this site.");
      }
      sites = sites.filter((row) => row.id !== site.id);
      return as(null);
    }
    return as(site);
  }
  const siteAuditMatch = /^\/directory\/sites\/(\d+)\/channel-audit$/.exec(path);
  if (siteAuditMatch) {
    const audit = CHANNEL_AUDITS[Number(siteAuditMatch[1])];
    if (!audit) return notFound(path);
    return as<SiteChannelAudit>(audit);
  }
  const siteProfileMatch = /^\/sites\/(\d+)\/profile$/.exec(path);
  if (siteProfileMatch) {
    const profile = SITE_PROFILES[Number(siteProfileMatch[1])];
    if (!profile) return notFound(path);
    return as<SiteProfile>(profile);
  }
  const siteFrequenciesMatch = /^\/sites\/(\d+)\/frequencies$/.exec(path);
  if (siteFrequenciesMatch) {
    return as<SiteFrequency[]>(SITE_FREQUENCIES[Number(siteFrequenciesMatch[1])] ?? []);
  }
  const siteFrequencyPatchMatch = /^\/site-frequencies\/(\d+)$/.exec(path);
  if (siteFrequencyPatchMatch && method === "PATCH") {
    requireSession(path);
    const id = Number(siteFrequencyPatchMatch[1]);
    for (const rows of Object.values(SITE_FREQUENCIES)) {
      const row = rows.find((entry) => entry.id === id);
      if (row) {
        Object.assign(row, payload as Partial<SiteFrequency>);
        return as<SiteFrequency>(row);
      }
    }
    return notFound(path);
  }
  const messageKindsMatch = /^\/sites\/(\d+)\/message-kinds$/.exec(path);
  if (messageKindsMatch) {
    const rows = SITE_MESSAGE_KINDS[Number(messageKindsMatch[1])] ?? [];
    return as<SiteMessageKind[]>(
      params?.unknown_only === true || params?.unknown_only === "true"
        ? rows.filter((row) => !row.known)
        : rows,
    );
  }
  const messageKindPatchMatch = /^\/admin\/sites\/(\d+)\/message-kinds\/(\d+)\/(\d+)$/.exec(path);
  if (messageKindPatchMatch && method === "PATCH") {
    requireSession(path);
    const rows = SITE_MESSAGE_KINDS[Number(messageKindPatchMatch[1])] ?? [];
    const row = rows.find(
      (entry) =>
        entry.mfid === Number(messageKindPatchMatch[2]) && entry.opcode === Number(messageKindPatchMatch[3]),
    );
    if (!row) return notFound(path);
    Object.assign(row, payload as Partial<SiteMessageKind>);
    return as<SiteMessageKind>(row);
  }
  const systemAuditMatch = /^\/directory\/systems\/(\d+)\/channel-audit$/.exec(path);
  if (systemAuditMatch) {
    if (Number(systemAuditMatch[1]) !== SYSTEM.id) return notFound(path);
    return as<SystemChannelAudit>(SYSTEM_CHANNEL_AUDIT);
  }
  if (path === "/patches") return as(paginate(PATCHES, { ...params, page_size: params.page_size ?? 200 }));
  if (path === "/agencies") {
    // Built from the admin list rather than the fixture, so an agency added on the
    // Agencies tab turns up in the pickers exactly as it does against the real API.
    const detail = new Map(AGENCIES.map((agency) => [agency.code, agency]));
    return as(
      agencies.map((agency) => ({
        ...(detail.get(agency.code) ?? {
          talkgroup_ids: [],
          talkgroups: [],
          shared_talkgroup_ids: detail.values().next().value?.shared_talkgroup_ids ?? [],
          radio_count: 0,
          call_count: 0,
        }),
        code: agency.code,
        name: agency.name,
        short_name: agency.short_name,
        kind: agency.kind,
        source: agency.source,
        active: agency.active,
      })),
    );
  }
  if (path === "/directory/vocabulary") return as(VOCABULARY);
  if (path === "/conversations") {
    // The on-air tgid, as the API reads it — not the row id.
    const talkgroup = params.talkgroup === undefined ? undefined : Number(params.talkgroup);
    const items = CONVERSATIONS.filter((conv) => talkgroup === undefined || conv.tgid === talkgroup).sort(
      (a, b) => Date.parse(b.started_at) - Date.parse(a.started_at),
    );
    return as(paginate(items, params));
  }
  const conversationMatch = /^\/conversations\/(\d+)$/.exec(path);
  if (conversationMatch) {
    const id = Number(conversationMatch[1]);
    const conversation = CONVERSATIONS.find((conv) => conv.id === id);
    if (!conversation) return notFound(path);
    const calls = CALLS.filter((call) => call.conversation_id === id)
      .sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at))
      .map(toCall);
    return as<ConversationThread>({ conversation, calls, total: calls.length });
  }

  if (path === "/talkgroups/bulk" && method === "POST") {
    requireSession(path);
    return as(bulkPatch(TALKGROUPS, path, payload));
  }
  if (path === "/radios/bulk" && method === "POST") {
    requireSession(path);
    return as(bulkPatch(RADIOS, path, payload));
  }
  if (path === "/units/bulk" && method === "POST") {
    requireSession(path);
    return as(bulkPatch(UNITS, path, payload));
  }
  if (path === "/personnel/bulk" && method === "POST") {
    requireSession(path);
    return as(bulkPatch(PERSONNEL, path, payload));
  }
  if (path === "/talkgroups" && method === "GET") {
    const q = String(params.q ?? "").toLowerCase();
    // The API defaults `include_hidden` to true, so the mock has to as well:
    // a directory that passes `false` must be the thing that narrows the list,
    // and every other caller must keep seeing every channel.
    const includeHidden = params.include_hidden === undefined || params.include_hidden !== false;
    // The three-valued filters, as the API has them: absent is "either", and `false` is
    // a filter rather than the absence of one — so `!== undefined`, never a falsy test.
    const flag = (name: string, of: (tg: Talkgroup) => boolean) => {
      const wanted = params[name];
      return (tg: Talkgroup) =>
        wanted === undefined || wanted === "" || of(tg) === (wanted === true || wanted === "true");
    };
    const flags = [
      flag("shared", (tg) => tg.shared),
      flag("record", (tg) => tg.record),
      flag("transcribe", (tg) => tg.transcribe),
      flag("export_broadcastify", (tg) => tg.export_broadcastify),
      flag("export_bcfy_calls", (tg) => tg.export_bcfy_calls),
      flag("export_openmhz", (tg) => tg.export_openmhz),
    ];
    const system = params.system_id ?? params.system;
    const items = TALKGROUPS.filter(
      (tg) =>
        (!q || talkgroupLabel(tg).toLowerCase().includes(q) || String(tg.tgid).includes(q)) &&
        (params.category === undefined || list(params, "category").includes(tg.category ?? "")) &&
        (params.service_tag === undefined || list(params, "service_tag").includes(tg.service_tag ?? "")) &&
        (params.agency === undefined ||
          list(params, "agency")
            .map((code) => code.toUpperCase())
            .includes((tg.agency_code ?? "").toUpperCase())) &&
        (system === undefined || Number(system) === tg.system_id) &&
        flags.every((keep) => keep(tg)) &&
        (includeHidden || !tg.is_hidden),
    );
    const sort = String(params.sort ?? "-call_count");
    const descending = sort.startsWith("-");
    const key = descending ? sort.slice(1) : sort;
    const value = (tg: Talkgroup): string | number => {
      switch (key) {
        case "tgid":
          return tg.tgid;
        case "alias":
          return talkgroupLabel(tg);
        case "description":
          return tg.description ?? "";
        case "category":
          return tg.category ?? "";
        case "service_tag":
          return tg.service_tag ?? "";
        case "agency_code":
          return tg.agency_code ?? "";
        case "priority":
          return tg.priority;
        case "record":
          return Number(tg.record);
        case "transcribe":
          return Number(tg.transcribe);
        case "is_hidden":
          return Number(tg.is_hidden);
        case "shared":
          return Number(tg.shared);
        case "export_broadcastify":
          return Number(tg.export_broadcastify);
        case "export_bcfy_calls":
          return Number(tg.export_bcfy_calls);
        case "export_openmhz":
          return Number(tg.export_openmhz);
        case "calls_24h":
          // The fixtures' own 24-hour count, so the "Last 24h" header sorts by the
          // same number the sparkline beside it is drawn from.
          return (
            talkgroupStats()
              .find((stat) => stat.talkgroup_id === tg.id)
              ?.spark.reduce((total, hour) => total + hour, 0) ?? 0
          );
        case "first_seen_at":
          return tg.first_seen_at ? Date.parse(tg.first_seen_at) : 0;
        case "last_seen_at":
          return tg.last_seen_at ? Date.parse(tg.last_seen_at) : 0;
        default:
          return tg.call_count;
      }
    };
    const sorted = [...items].sort((a, b) => {
      const left = value(a);
      const right = value(b);
      const cmp = typeof left === "number" ? left - (right as number) : left.localeCompare(right as string);
      return descending ? -cmp : cmp;
    });
    return as(paginate(sorted, { ...params, page_size: params.page_size ?? 200 }));
  }
  if (path === "/talkgroups/import" && method === "POST") {
    requireSession(path);
    return as<ImportResult>({
      created: 4,
      updated: 16,
      skipped: 2,
      source: "playlist",
      aliases: 20,
      skipped_service_tags: 0,
      skipped_agency_codes: 0,
    });
  }
  const talkgroupMatch = /^\/talkgroups\/(\d+)(\/stats)?$/.exec(path);
  if (talkgroupMatch) {
    const id = Number(talkgroupMatch[1]);
    const talkgroup = TALKGROUPS.find((tg) => tg.id === id);
    if (!talkgroup) return notFound(path);
    if (talkgroupMatch[2] === "/stats") {
      const own = CALLS.filter((call) => call.talkgroup_ids.includes(talkgroup.tgid));
      const byRadio = new Map<number, number>();
      for (const call of own) {
        if (call.radio_id === null) continue;
        byRadio.set(call.radio_id, (byRadio.get(call.radio_id) ?? 0) + 1);
      }
      return as({
        talkgroup_id: id,
        call_count: own.length,
        audio_seconds: Math.round(own.reduce((sum, call) => sum + call.duration_s, 0)),
        hourly: Array.from({ length: 24 }, (_, hour) => ({
          hour,
          count: own.filter((call) => Number(call.started_local.slice(11, 13)) === hour).length,
        })),
        spark: sparkFor((call) => call.talkgroup_ids.includes(talkgroup.tgid)),
        top_radios: [...byRadio.entries()]
          .sort((a, b) => b[1] - a[1])
          .slice(0, 10)
          .map(([radioId, count]) => {
            const radio = RADIOS.find((r) => r.id === radioId);
            return { radio_id: radioId, label: radio ? radioLabel(radio) : String(radioId), count };
          }),
      });
    }
    if (method === "PATCH") {
      requireSession(path);
      Object.assign(talkgroup, payload);
      return as(talkgroup);
    }
    return as(talkgroup);
  }

  if (path === "/radios" && method === "GET") {
    const q = String(params.q ?? "").toLowerCase();
    const activity = params.activity as string | undefined;
    let items = RADIOS.filter(
      (radio) =>
        !q ||
        (radio.alias ?? "").toLowerCase().includes(q) ||
        (radio.unit_id ?? "").toLowerCase().includes(q) ||
        String(radio.radio_id).includes(q),
    );
    if (activity === "transmitted") items = items.filter((radio) => radio.call_count > 0);
    else if (activity === "seen") items = items.filter((radio) => radio.call_count === 0);
    const sort = String(params.sort ?? "-call_count");
    const descending = sort.startsWith("-");
    const key = descending ? sort.slice(1) : sort;
    const value = (radio: (typeof RADIOS)[number]): string | number => {
      switch (key) {
        case "radio_id":
          return radio.radio_id;
        case "unit_id":
          return radio.unit_id ?? "";
        case "last_seen_at":
          return radio.last_seen_at ? Date.parse(radio.last_seen_at) : 0;
        default:
          return radio.call_count;
      }
    };
    items = [...items].sort((a, b) => {
      const left = value(a);
      const right = value(b);
      const cmp = typeof left === "number" ? left - (right as number) : left.localeCompare(right as string);
      return descending ? -cmp : cmp;
    });
    return as(paginate(items, { ...params, page_size: params.page_size ?? 200 }));
  }
  if (path === "/radios/import" && method === "POST") {
    requireSession(path);
    return as<ImportResult>({
      created: 2,
      updated: 38,
      skipped: 0,
      source: "playlist",
      aliases: 40,
      skipped_service_tags: 0,
      skipped_agency_codes: 0,
    });
  }
  const radioMatch = /^\/radios\/(\d+)(\/affiliations|\/history)?$/.exec(path);
  if (radioMatch) {
    const id = Number(radioMatch[1]);
    const radio = RADIOS.find((candidate) => candidate.id === id);
    if (!radio) return notFound(path);
    if (radioMatch[2] === "/affiliations") {
      return as(
        AFFILIATIONS.filter((aff) => aff.radio_id === id).sort(
          (a, b) => Date.parse(b.last_heard_at ?? "") - Date.parse(a.last_heard_at ?? ""),
        ),
      );
    }
    if (radioMatch[2] === "/history") {
      const own = CALLS.filter((call) => call.radio_id === id);
      const byTalkgroup = new Map<string, number>();
      for (const call of own) {
        const key = call.talkgroup_alias ?? String(call.tgid ?? "");
        byTalkgroup.set(key, (byTalkgroup.get(key) ?? 0) + 1);
      }
      return as<RadioHistory>({
        radio_id: id,
        call_count: own.length,
        spark: sparkFor((call) => call.radio_id === id),
        hourly: Array.from({ length: 24 }, (_, hour) => ({
          hour,
          count: own.filter((call) => Number(call.started_local.slice(11, 13)) === hour).length,
        })),
        talkgroups: [...byTalkgroup.entries()]
          .sort((a, b) => b[1] - a[1])
          .map(([key, count]) => ({ key, label: key, count })),
      });
    }
    if (method === "PATCH") {
      requireSession(path);
      Object.assign(radio, payload);
      return as(radio);
    }
    const events = RADIO_EVENTS.filter((row) => row.radio_pk === id);
    const affiliations = AFFILIATIONS.filter((aff) => aff.radio_id === id);
    return as<RadioDetail>({
      ...radio,
      system_name: "Example Network",
      agency_name: null,
      audio_seconds: CALLS.filter((call) => call.radio_id === id).reduce(
        (total, call) => total + call.duration_s,
        0,
      ),
      talkgroup_count: new Set(affiliations.map((aff) => aff.talkgroup_id)).size,
      last_call_id: CALLS.find((call) => call.radio_id === id)?.id ?? null,
      presence: {
        last_seen_at: radio.last_seen_at,
        last_registered_at: events.find((row) => row.kind === "unit_registration")?.at ?? null,
        last_deregistered_at: null,
        affiliations: affiliations.map((aff) => ({
          talkgroup_id: aff.talkgroup_id,
          tgid: aff.tgid,
          talkgroup_alias: aff.talkgroup_alias,
          source: aff.source,
          last_heard_at: aff.last_heard_at,
          last_affiliated_at: aff.last_affiliated_at,
          call_count: aff.call_count,
        })),
      },
      events: events.slice(0, 50),
      event_count: events.length,
    });
  }
  if (/^\/radios\/(\d+)\/events$/.test(path)) {
    const id = Number(/^\/radios\/(\d+)\/events$/.exec(path)?.[1]);
    const rows = RADIO_EVENTS.filter((row) => row.radio_pk === id);
    return as(paginate(rows, params));
  }

  /* -------------------------------------------------------------- roster */
  if (path === "/units" && method === "GET") {
    const q = String(params.q ?? "").toLowerCase();
    const items = UNITS.filter(
      (unit) =>
        !q ||
        unit.callsign.toLowerCase().includes(q) ||
        (unit.name ?? "").toLowerCase().includes(q) ||
        (unit.notes ?? "").toLowerCase().includes(q),
    ).sort((a, b) => a.callsign.localeCompare(b.callsign));
    return as(paginate(items, { ...params, page_size: params.page_size ?? 200 }));
  }
  if (path === "/units" && method === "POST") {
    requireSession(path);
    const body = payload as Partial<Unit>;
    // A callsign is unique per agency (`uq_units_agency_callsign`), and the real
    // API answers a collision with a 409 naming it. A mock that accepted the
    // duplicate would let the dialog ship with no way for that sentence to reach
    // the reader — which is exactly why the callsign is not an inline cell.
    const callsign = (body.callsign ?? "").trim();
    const clash = UNITS.some(
      (row) =>
        row.callsign.toLowerCase() === callsign.toLowerCase() &&
        (row.agency_code ?? null) === (body.agency_code ?? null),
    );
    if (callsign && clash) {
      throw new ApiError("409 Conflict", 409, path, `unit ${callsign} already exists`);
    }
    const unit: Unit = {
      id: Math.max(0, ...UNITS.map((row) => row.id)) + 1,
      callsign: body.callsign ?? "NEW",
      name: body.name ?? null,
      agency_code: body.agency_code ?? null,
      kind: body.kind ?? "other",
      notes: body.notes ?? null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      radio_count: 0,
    };
    UNITS.push(unit);
    return as(unit);
  }
  if (path === "/units/import" && method === "POST") {
    requireSession(path);
    return as<ImportResult>({
      created: 3,
      updated: 9,
      skipped: 1,
      source: "roster",
      aliases: 13,
      skipped_service_tags: 0,
      skipped_agency_codes: 0,
    });
  }
  const unitMatch = /^\/units\/(\d+)(\/radios)?$/.exec(path);
  if (unitMatch) {
    const id = Number(unitMatch[1]);
    const unit = UNITS.find((row) => row.id === id);
    if (!unit) return notFound(path);
    if (unitMatch[2] === "/radios") return as(assignedRadios({ unitId: id }));
    if (method === "PATCH") {
      requireSession(path);
      Object.assign(unit, payload);
      // A renamed callsign follows through to the radios wearing it, as the API does.
      for (const assignment of ASSIGNMENTS) {
        if (assignment.unit?.id !== id || assignment.effective_to !== null) continue;
        assignment.unit = { id: unit.id, callsign: unit.callsign, name: unit.name };
        const radio = RADIOS.find((row) => row.id === assignment.radio_pk);
        if (radio) {
          radio.unit_id = unit.callsign;
          radio.current_unit = assignment.unit;
        }
      }
      return as(unit);
    }
    const radios = assignedRadios({ unitId: id });
    return as<UnitDetail>({
      ...unit,
      radio_count: radios.length,
      agency_name: AGENCIES.find((agency) => agency.code === unit.agency_code)?.name ?? null,
      radios,
      call_count: radios.reduce((total, row) => total + row.call_count, 0),
      last_call_at: radios[0]?.last_seen_at ?? null,
    });
  }

  if (path === "/personnel" && method === "GET") {
    const q = String(params.q ?? "").toLowerCase();
    let items = PERSONNEL.filter(
      (person) =>
        !q ||
        person.name.toLowerCase().includes(q) ||
        (person.rank ?? "").toLowerCase().includes(q) ||
        (person.badge ?? "").toLowerCase().includes(q),
    );
    if (params.active !== undefined) {
      const wanted = String(params.active) === "true";
      items = items.filter((person) => person.active === wanted);
    }
    items = [...items].sort((a, b) => a.name.localeCompare(b.name));
    return as(paginate(items, { ...params, page_size: params.page_size ?? 200 }));
  }
  if (path === "/personnel" && method === "POST") {
    requireSession(path);
    const body = payload as Partial<Person>;
    const person: Person = {
      id: Math.max(0, ...PERSONNEL.map((row) => row.id)) + 1,
      name: body.name ?? "Unnamed",
      rank: body.rank ?? null,
      badge: body.badge ?? null,
      agency_code: body.agency_code ?? null,
      active: body.active ?? true,
      notes: body.notes ?? null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      radio_count: 0,
    };
    PERSONNEL.push(person);
    return as(person);
  }
  if (path === "/personnel/import" && method === "POST") {
    requireSession(path);
    return as<ImportResult>({
      created: 2,
      updated: 6,
      skipped: 0,
      source: "roster",
      aliases: 8,
      skipped_service_tags: 0,
      skipped_agency_codes: 0,
    });
  }
  const personMatch = /^\/personnel\/(\d+)(\/radios)?$/.exec(path);
  if (personMatch) {
    const id = Number(personMatch[1]);
    const person = PERSONNEL.find((row) => row.id === id);
    if (!person) return notFound(path);
    if (personMatch[2] === "/radios") return as(assignedRadios({ personId: id }));
    if (method === "PATCH") {
      requireSession(path);
      Object.assign(person, payload);
      return as(person);
    }
    const radios = assignedRadios({ personId: id });
    return as<PersonDetail>({
      ...person,
      radio_count: radios.length,
      agency_name: AGENCIES.find((agency) => agency.code === person.agency_code)?.name ?? null,
      radios,
      call_count: radios.reduce((total, row) => total + row.call_count, 0),
      last_call_at: radios[0]?.last_seen_at ?? null,
    });
  }

  const assignmentsMatch = /^\/radios\/(\d+)\/assignments$/.exec(path);
  if (assignmentsMatch) {
    const radioId = Number(assignmentsMatch[1]);
    const radio = RADIOS.find((row) => row.id === radioId);
    if (!radio) return notFound(path);
    if (method === "GET") {
      return as(
        ASSIGNMENTS.filter((row) => row.radio_pk === radioId).sort(
          (a, b) =>
            Number(b.current) - Number(a.current) ||
            Date.parse(b.effective_from) - Date.parse(a.effective_from),
        ),
      );
    }
    requireSession(path);
    const body = payload as { unit_id?: number | null; person_id?: number | null; note?: string | null };
    const unit = body.unit_id ? UNITS.find((row) => row.id === body.unit_id) : undefined;
    const person = body.person_id ? PERSONNEL.find((row) => row.id === body.person_id) : undefined;
    if (!unit && !person) throw new ApiError("send exactly one of unit_id or person_id", 422, path);
    closeCurrent(radioId, unit ? "unit" : "person");
    const assignment: RadioAssignment = {
      id: Math.max(0, ...ASSIGNMENTS.map((row) => row.id)) + 1,
      radio_pk: radioId,
      kind: unit ? "unit" : "person",
      unit: unit ? { id: unit.id, callsign: unit.callsign, name: unit.name } : null,
      person: person ? { id: person.id, name: person.name } : null,
      effective_from: new Date().toISOString(),
      effective_to: null,
      current: true,
      note: body.note ?? null,
    };
    ASSIGNMENTS.push(assignment);
    if (unit) {
      radio.unit_id = unit.callsign;
      radio.current_unit = assignment.unit;
    } else if (person) {
      radio.current_person = assignment.person;
    }
    return as(assignment);
  }
  const endAssignmentMatch = /^\/radios\/(\d+)\/assignments\/current$/.exec(path);
  if (endAssignmentMatch && method === "DELETE") {
    requireSession(path);
    const radioId = Number(endAssignmentMatch[1]);
    const radio = RADIOS.find((row) => row.id === radioId);
    if (!radio) return notFound(path);
    const kind = String(params.kind ?? "unit") as "unit" | "person";
    const ended = closeCurrent(radioId, kind);
    if (!ended) return notFound(path);
    if (kind === "unit") {
      if (radio.unit_id === ended.unit?.callsign) radio.unit_id = null;
      radio.current_unit = null;
    } else {
      radio.current_person = null;
    }
    return as(ended);
  }

  /* --------------------------------------------------------------- stats */
  if (path === "/stats/overview") return as(overview());
  if (path === "/stats/activity") {
    const grouped = String(params.group_by ?? "day");
    return as<ActivityResponse>(grouped === "talkgroup" ? activityByTalkgroupHour() : activityByDayHour());
  }
  if (path === "/activity" && method === "GET") {
    let rows = activityLog();
    if (params.source) rows = rows.filter((row) => row.source === params.source);
    if (params.level) rows = rows.filter((row) => row.level === params.level);
    if (params.before_id) rows = rows.filter((row) => row.id < Number(params.before_id));
    const limit = Math.min(500, Number(params.limit ?? 100));
    return as<ActivityEvent[]>(rows.slice(0, limit));
  }
  if (path === "/stats/talkgroups") return as<TalkgroupStat[]>(talkgroupStats());
  if (path === "/stats/radios") return as<RadioStat[]>(radioStats());
  if (path === "/stats/transcription") return as(transcriptionStats());
  if (path === "/stats/gaps") return as(GAPS);
  if (path === "/timeline") {
    const from = Date.parse(String(params.from ?? CALLS[CALLS.length - 1]?.started_at));
    const to = Date.parse(String(params.to ?? CALLS[0]?.started_at));
    const points: TimelinePoint[] = [];
    for (let minute = from; minute <= to; minute += 60_000) {
      const inBucket = CALLS.filter((call) => {
        const started = Date.parse(call.started_at);
        return started >= minute && started < minute + 60_000;
      });
      points.push({
        minute: new Date(minute).toISOString(),
        count: inBucket.length,
        seconds: Number(inBucket.reduce((sum, call) => sum + call.duration_s, 0).toFixed(3)),
      });
    }
    return as(points);
  }

  /* ---------------------------------------------------------- operations */
  if (path === "/status") {
    return as<SystemStatus>({
      status: "ok",
      ok: true,
      version: "0.1.0-mock",
      db_ok: true,
      database: { ok: true, detail: null },
      audio_root: "/data/audio",
      tz: "America/Denver",
      counts: {
        calls: CALLS.length,
        talkgroups: TALKGROUPS.length,
        radios: RADIOS.length,
        patches: PATCHES.length,
      },
      queue_depth: JOBS.filter((job) => job.status === "queued").length,
      // The admission gauges the push path publishes (docs/RECORDERS.md, priority and
      // flow control); the demo shows a queue with room in it.
      queue: {
        depth: JOBS.filter((job) => job.status === "queued").length,
        running: JOBS.filter((job) => job.status === "running").length,
        accepting: true,
      },
      disk_free_bytes: 412 * 1024 ** 3,
      disk_total_bytes: 1024 * 1024 ** 3,
      disk: MOCK_DISK,
      disk_volumes: [MOCK_DISK],
      uptime_s: 92_400,
      ingest: ingestStatus(),
      // Nothing raised: the demo archive is healthy.
      health: [],
      server_time: new Date().toISOString(),
    });
  }
  if (path === "/status/lag") {
    const lag = ingestStatus().lag_s;
    return as<IngestLag>({
      ingest_lag_s: lag,
      last_recorded_at: lag === null ? null : new Date(Date.now() - lag * 1000).toISOString(),
      server_time: new Date().toISOString(),
    });
  }
  if (path === "/status/activity") {
    const seconds = Math.min(3600, Math.max(10, Number(params.window ?? 300)));
    return as<LiveActivitySnapshot>(liveActivity(seconds));
  }
  if (path === "/admin/ingest") {
    return as<AdminIngestResponse>({ status: ingestStatus(), runs: INGEST_RUNS });
  }
  if (path === "/admin/ingest/rescan" && method === "POST") {
    requireSession(path);
    return as({ queued: true, files_seen: 12 });
  }
  if (path === "/admin/ingest/files") {
    const status = String(params.status ?? "");
    return as(
      paginate(
        INGEST_FILES.filter((file) => !status || file.status === status),
        params,
      ),
    );
  }
  const retryMatch = /^\/admin\/ingest\/files\/(\d+)\/retry$/.exec(path);
  if (retryMatch && method === "POST") {
    requireSession(path);
    const file = INGEST_FILES.find((candidate) => candidate.id === Number(retryMatch[1]));
    if (!file) return notFound(path);
    file.status = "ingested";
    file.error = null;
    file.attempts += 1;
    file.last_attempt_at = new Date().toISOString();
    return as(file);
  }
  if (path === "/admin/jobs") {
    const status = list(params, "status");
    return as(
      paginate(
        JOBS.filter((job) => !status.length || status.includes(job.status)),
        params,
      ),
    );
  }
  if (path === "/admin/jobs/requeue" && method === "POST") {
    requireSession(path);
    const ids = Array.isArray(payload.job_ids) ? payload.job_ids.map(Number) : [];
    let requeued = 0;
    for (const job of JOBS) {
      if (!ids.length || ids.includes(job.id)) {
        if (job.status === "failed") {
          job.status = "queued";
          job.last_error = null;
          requeued += 1;
        }
      }
    }
    return as({ requeued });
  }
  if (path === "/admin/backends") return as(BACKENDS);
  if (path === "/admin/stt/backends" && method === "GET") {
    return as<SttBackendsView>({
      backends: sttBackends,
      queue: STT_QUEUE,
      reporting: sttBackends.some((backend) => !backend.stale),
      worker_instance: "keyup-worker:1",
      worker_started_at: sttBackends[0]?.started_at ?? null,
      last_report_at: sttBackends[0]?.updated_at ?? null,
      config_version: sttConfigVersion,
      // One behind after a save, until the fake "worker" catches up — which is the state
      // the page's "waiting for the worker" line exists to show.
      applied_config_version: sttAppliedVersion,
      from_environment: false,
    });
  }
  if (path === "/admin/stt/backends" && method === "POST") {
    requireSession(path);
    const input = payload as Record<string, unknown>;
    const name = String(input.name ?? "");
    if (sttBackends.some((candidate) => candidate.name === name)) {
      throw new ApiError("409 Conflict", 409, path, `A backend named ${name} already exists.`);
    }
    sttConfigVersion += 1;
    const created = backendRowFrom(name, input);
    sttBackends.push(created);
    return as(created);
  }
  if (path.endsWith("/probe") && path.startsWith("/admin/stt/backends/") && method === "POST") {
    requireSession(path);
    const name = decodeURIComponent(path.slice("/admin/stt/backends/".length, -"/probe".length));
    const backend = sttBackends.find((candidate) => candidate.name === name);
    if (!backend) return notFound(path);
    // A backend that is failing every attempt does not suddenly probe clean: the demo's
    // degraded row is the one worth being able to click Probe on.
    const ok = backend.healthy !== false && backend.consecutive_errors < 3;
    return as<SttBackendProbe>({
      name,
      ok,
      detail: ok ? "3 model(s) installed" : "connection refused",
      kind: backend.kind,
      model: backend.model,
      health_s: 0.041,
      clip_call_id: ok ? 1 : null,
      audio_s: ok ? 3.2 : null,
      latency_s: ok ? 0.38 : null,
      rtf: ok ? 8.42 : null,
      text: ok ? "engine one responding, structure fire on cherry court" : null,
      benchmark_skipped: ok ? null : "the health check failed",
      checked_at: new Date().toISOString(),
    });
  }
  if (path.endsWith("/enabled") && path.startsWith("/admin/stt/backends/") && method === "PUT") {
    requireSession(path);
    const name = decodeURIComponent(path.slice("/admin/stt/backends/".length, -"/enabled".length));
    const backend = sttBackends.find((candidate) => candidate.name === name);
    if (!backend) return notFound(path);
    const enabled = (payload.enabled ?? null) as boolean | null;
    backend.admin_enabled = enabled;
    backend.enabled = enabled === null ? backend.config_enabled : enabled;
    backend.admin_changed_at = new Date().toISOString();
    backend.admin_changed_by = session?.username ?? "operator";
    return as(backend);
  }
  if (path.startsWith("/admin/stt/backends/") && method === "PUT") {
    requireSession(path);
    const name = decodeURIComponent(path.slice("/admin/stt/backends/".length));
    const index = sttBackends.findIndex((candidate) => candidate.name === name);
    if (index < 0) return notFound(path);
    const previous = sttBackends[index] as SttBackendStat;
    sttConfigVersion += 1;
    // The counters belong to the work that was actually done; an edit re-points the
    // backend, it does not un-transcribe anything.
    const updated: SttBackendStat = {
      ...backendRowFrom(name, payload as Record<string, unknown>),
      attempts: previous.attempts,
      successes: previous.successes,
      errors: previous.errors,
      healthy: previous.healthy,
    };
    sttBackends[index] = updated;
    return as(updated);
  }
  if (path.startsWith("/admin/stt/backends/") && method === "DELETE") {
    requireSession(path);
    const name = decodeURIComponent(path.slice("/admin/stt/backends/".length));
    const index = sttBackends.findIndex((candidate) => candidate.name === name);
    if (index < 0) return notFound(path);
    sttConfigVersion += 1;
    // The demo's first backend has transcripts behind it, so it is retained rather than
    // removed — which is the half of this endpoint worth showing.
    const retained = index === 0;
    const row = sttBackends[index] as SttBackendStat;
    if (retained) {
      row.deleted_at = new Date().toISOString();
      row.enabled = false;
      row.editable = false;
    } else {
      sttBackends.splice(index, 1);
    }
    return as<SttBackendDeleteResult>({
      name,
      removed: !retained,
      retained,
      transcripts: retained ? 4168 : 0,
      config_version: sttConfigVersion,
    });
  }
  if (path === "/admin/ingest/sources" && method === "GET") {
    return as<IngestSourcesView>({ sources: ingestSources, config_version: ingestConfigVersion });
  }
  if (path === "/admin/ingest/sources" && method === "POST") {
    requireSession(path);
    ingestConfigVersion += 1;
    const created = sourceRowFrom(nextSourceId++, payload as Record<string, unknown>);
    ingestSources.push(created);
    return as(created);
  }
  if (path.startsWith("/admin/ingest/sources/") && path.endsWith("/status") && method === "GET") {
    const id = Number(path.slice("/admin/ingest/sources/".length, -"/status".length));
    const source = ingestSources.find((candidate) => candidate.id === id);
    if (!source) return notFound(path);
    return as<IngestSourceStatus>({
      id: source.id,
      kind: source.kind,
      label: source.label,
      enabled: source.enabled,
      path_state: source.path_state,
      path_detail: source.path_detail,
      files_new_24h: source.path_state === "ok" ? 1284 : 0,
      files_failed_24h: source.path_state === "ok" ? 2 : 0,
      calls_24h: source.kind === "push_token" ? 341 : 0,
      last_activity_at: source.last_activity_at,
      last_error: source.last_error,
      last_error_at: source.last_error_at,
      meta: source.meta,
    });
  }
  if (path.startsWith("/admin/ingest/sources/") && method === "PUT") {
    requireSession(path);
    const id = Number(path.slice("/admin/ingest/sources/".length));
    const index = ingestSources.findIndex((candidate) => candidate.id === id);
    if (index < 0) return notFound(path);
    ingestConfigVersion += 1;
    ingestSources[index] = sourceRowFrom(id, payload as Record<string, unknown>);
    return as(ingestSources[index]);
  }
  if (path.startsWith("/admin/ingest/sources/") && method === "DELETE") {
    requireSession(path);
    const id = Number(path.slice("/admin/ingest/sources/".length));
    const index = ingestSources.findIndex((candidate) => candidate.id === id);
    if (index < 0) return notFound(path);
    ingestConfigVersion += 1;
    ingestSources.splice(index, 1);
    return as(null);
  }
  if (path === "/admin/audio" && method === "GET") return as(AUDIO_STATUS);
  if (path === "/admin/audio/reprocess" && method === "POST") {
    requireSession(path);
    const body = (payload ?? {}) as { force?: boolean; priority?: number };
    const matched = body.force ? AUDIO_STATUS.processed + AUDIO_STATUS.pending : AUDIO_STATUS.pending;
    return as({
      matched,
      queued: matched,
      already_open: 0,
      priority: body.priority ?? 600,
      force: body.force ?? false,
    });
  }
  if (path === "/admin/audio/preview" && method === "POST") {
    requireSession(path);
    const { call_id: callId } = (payload ?? {}) as { call_id: number };
    return as({
      call_id: callId,
      filter:
        "atrim=start=0.5:end=25.7,asetpts=N/SR/TB,highpass=f=300,lowpass=f=3800,afftdn=nf=-35:nr=18,loudnorm=I=-16:TP=-4,alimiter=limit=0.630957:level=disabled:attack=5:release=50",
      meta: {
        version: 1,
        mean_db: -32.4,
        mean_db_after: -16.1,
        peak_db_after: -1.5,
        head_trim_s: 0.5,
        tail_trim_s: 0.12,
        gain_db: 16.3,
        tone_pattern: "two_tone",
        tones: [
          { start_s: 1.15, end_s: 2.11, hz: 625 },
          { start_s: 2.18, end_s: 5.09, hz: 750 },
        ],
        silent: false,
      },
      raw_url: `/api/calls/${callId}/audio?raw=1`,
      processed_url: `/api/admin/audio/preview/mock${callId}`,
    });
  }
  /* ------------------------------------------------------ audio profiles */
  if (path === "/admin/audio/profiles/steps" && method === "GET") return as(AUDIO_STEP_CATALOG);
  if (path === "/admin/audio/profiles" && method === "GET") {
    // Builtins first, then by name — the order the real endpoint sorts in.
    return as(
      [...audioProfiles].sort(
        (a, b) => Number(b.builtin) - Number(a.builtin) || a.name.localeCompare(b.name),
      ),
    );
  }
  if (path === "/admin/audio/profiles" && method === "POST") {
    requireSession(path);
    const body = payload as { name?: string; description?: string | null; chain?: AudioProfileStep[] };
    const name = String(body.name ?? "").trim();
    if (audioProfiles.some((profile) => profile.name === name)) {
      throw new ApiError("409 Conflict", 409, path, `an audio profile named '${name}' already exists`);
    }
    const chain = normalizeMockChain(body.chain ?? [], path);
    const row: AudioProfile = {
      id: Math.max(0, ...audioProfiles.map((profile) => profile.id)) + 1,
      name,
      description: body.description ?? null,
      chain,
      builtin: false,
      rendered: renderMockChain(chain),
      talkgroup_count: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    audioProfiles.push(row);
    return as(row);
  }
  if (path === "/admin/audio/profiles/preview" && method === "POST") {
    requireSession(path);
    // Reachable only through `apiPostBlob`, which routes to `handleMockBlob`; a JSON
    // request for this path is a caller that forgot, and saying so beats an empty player.
    throw new ApiError("406 Not Acceptable", 406, path, "the preview answers with audio, not JSON");
  }
  if (path.startsWith("/admin/audio/profiles/") && path.endsWith("/apply") && method === "POST") {
    requireSession(path);
    const id = Number(path.slice("/admin/audio/profiles/".length, -"/apply".length));
    const profile = audioProfiles.find((row) => row.id === id);
    if (!profile) return notFound(path);
    const body = (payload ?? {}) as { talkgroup_ids?: number[]; hours?: number };
    // The assignment really happens in the fixtures, so the count on the card and the
    // profile named on each channel move together exactly as they do against the API.
    let assigned = 0;
    for (const talkgroupId of body.talkgroup_ids ?? []) {
      const talkgroup = TALKGROUPS.find((row) => row.id === talkgroupId);
      if (!talkgroup || talkgroup.audio_profile_id === id) continue;
      talkgroup.audio_profile_id = id;
      assigned += 1;
    }
    const talkgroups = TALKGROUPS.filter((row) => row.audio_profile_id === id);
    const hours = body.hours ?? 24;
    const since = Date.now() - hours * 3600_000;
    // By row id rather than tgid: two systems can carry the same tgid, and the profile is
    // a property of the row, which is what the real query joins on.
    const rows = new Set<number | null>(talkgroups.map((row) => row.id));
    const matched = CALLS.filter(
      (call) => rows.has(call.talkgroup_id) && Date.parse(call.started_at) >= since,
    ).length;
    for (const row of audioProfiles) {
      row.talkgroup_count = TALKGROUPS.filter((tg) => tg.audio_profile_id === row.id).length;
    }
    return as({
      profile_id: id,
      profile_name: profile.name,
      assigned,
      talkgroups: talkgroups.length,
      matched,
      queued: matched,
      already_open: 0,
      hours,
    });
  }
  if (path.startsWith("/admin/audio/profiles/") && method === "PATCH") {
    requireSession(path);
    const id = Number(path.slice("/admin/audio/profiles/".length));
    const profile = audioProfiles.find((row) => row.id === id);
    if (!profile) return notFound(path);
    const body = payload as {
      name?: string | null;
      description?: string | null;
      chain?: AudioProfileStep[] | null;
    };
    if (typeof body.name === "string") {
      const name = body.name.trim();
      if (audioProfiles.some((row) => row.name === name && row.id !== id)) {
        throw new ApiError("409 Conflict", 409, path, `an audio profile named '${name}' already exists`);
      }
      profile.name = name;
    }
    if ("description" in body) profile.description = body.description ?? null;
    if (body.chain != null) {
      profile.chain = normalizeMockChain(body.chain, path);
      profile.rendered = renderMockChain(profile.chain);
    }
    profile.updated_at = new Date().toISOString();
    return as(profile);
  }
  if (path.startsWith("/admin/audio/profiles/") && method === "DELETE") {
    requireSession(path);
    const id = Number(path.slice("/admin/audio/profiles/".length));
    const index = audioProfiles.findIndex((row) => row.id === id);
    if (index < 0) return notFound(path);
    const profile = audioProfiles[index] as AudioProfile;
    if (profile.builtin) {
      throw new ApiError(
        "409 Conflict",
        409,
        path,
        `'${profile.name}' is a built-in profile and cannot be deleted`,
      );
    }
    const used = TALKGROUPS.filter((row) => row.audio_profile_id === id).length;
    if (used) {
      throw new ApiError(
        "409 Conflict",
        409,
        path,
        `'${profile.name}' is still assigned to ${used} talkgroup(s); move them to another profile (or to none) first`,
      );
    }
    audioProfiles.splice(index, 1);
    return as(null);
  }
  if (path === "/admin/settings" && method === "GET") return as(settings);
  if (path === "/admin/settings" && method === "PUT") {
    requireSession(path);
    const updates = payload as Record<string, string>;
    settings = settings.map((setting) =>
      setting.key in updates
        ? { ...setting, value: String(updates[setting.key]), is_default: false }
        : setting,
    );
    return as(settings);
  }
  // The HTTPS edge. The demo has no proxy to talk to and no Cloudflare token, so the
  // status is derived from the settings rows the same way the real one is derived from
  // the settings table — and every action reports the honest "nothing is configured".
  if (path === "/admin/https/status" && method === "GET") {
    const value = (key: string) => settings.find((row) => row.key === key)?.value ?? "";
    const domains = value("tls_domains").split(/\s+/).filter(Boolean);
    const hasToken = settings.find((row) => row.key === "tls_cloudflare_api_token")?.is_default === false;
    // Same resolution as the backend's tls_challenge(): auto is DNS-01 with a token, HTTP-01 without.
    const challenge = value("tls_challenge");
    const needsToken = challenge === "dns" || (challenge !== "http" && hasToken);
    const wantsTls =
      value("tls_enabled") === "true" &&
      domains.length > 0 &&
      !!value("tls_acme_email") &&
      (hasToken || !needsToken);
    const missing =
      value("tls_enabled") === "true"
        ? [
            ...(domains.length ? [] : ["tls_domains"]),
            ...(value("tls_acme_email") ? [] : ["tls_acme_email"]),
            ...(hasToken || !needsToken ? [] : ["tls_cloudflare_api_token"]),
          ]
        : [];
    return as<HttpsStatus>({
      ...HTTPS_STATUS,
      desired_mode: wantsTls ? "tls" : "plain",
      domains,
      redirect_http: value("tls_redirect_http") !== "false",
      acme_ca: value("tls_acme_ca") || "production",
      acme_email: value("tls_acme_email"),
      has_token: hasToken,
      missing,
    });
  }
  if (path === "/admin/https/apply" && method === "POST") {
    requireSession(path);
    return as<HttpsApplyResult>({
      ok: false,
      mode: "plain",
      domains: [],
      detail: "there is no edge container in the sample archive",
    });
  }
  if (path === "/admin/https/test-dns" && method === "POST") {
    requireSession(path);
    return as<HttpsDnsTestResult>({
      ok: false,
      detail: "no Cloudflare API token is set",
      zones: [],
    });
  }
  // Exporters. The demo keeps both services off and the ledger empty; a "test" here
  // reports exactly what the real endpoint reports when nothing is configured.
  if (path === "/admin/exports" && method === "GET") {
    const modeOf = (service: string) =>
      (settings.find((row) => row.key === `export_${service}_talkgroup_mode`)?.value ??
        "all") as ExportStatus["services"][number]["talkgroup_mode"];
    return as<ExportStatus>({
      ...EXPORT_STATUS,
      services: EXPORT_STATUS.services.map((service) => ({
        ...service,
        enabled: settings.find((row) => row.key === `export_${service.service}_enabled`)?.value === "true",
        talkgroup_mode: modeOf(service.service),
      })),
      broadcastify_feed: {
        ...EXPORT_STATUS.broadcastify_feed,
        enabled: settings.find((row) => row.key === "export_broadcastify_feed_enabled")?.value === "true",
        server: settings.find((row) => row.key === "broadcastify_feed_server")?.value ?? "",
        mount: settings.find((row) => row.key === "broadcastify_feed_mount")?.value ?? "",
      },
    });
  }
  if (path === "/admin/exports/broadcastify-feed/test" && method === "POST") {
    requireSession(path);
    return as<ExportTestResult>({
      service: "broadcastify_feed",
      ok: false,
      checked: "nothing: the server, mount or password is blank",
      detail: "set the Server, Mount and Password from the feed's Feed Technical Details panel",
      environment: null,
      clock_offset_s: null,
      clock_ok: null,
      clock_source: null,
      used_draft: Object.keys(draftKeys(payload)).length > 0,
      draft_keys: Object.keys(draftKeys(payload)),
    });
  }
  const exportTestMatch = /^\/admin\/exports\/([a-z]+)\/test$/.exec(path);
  if (exportTestMatch && method === "POST") {
    requireSession(path);
    return as<ExportTestResult>({
      service: exportTestMatch[1] ?? "",
      ok: false,
      checked:
        "the production credentials against the configured endpoint with the documented test=1 parameter; no call is registered and no audio is uploaded",
      detail: "not set: no credentials are configured in this demo",
      environment: "production",
      clock_offset_s: 0.031,
      clock_ok: true,
      clock_source: "ntp",
      used_draft: Object.keys(draftKeys(payload)).length > 0,
      draft_keys: Object.keys(draftKeys(payload)),
    });
  }
  // Copying one per-talkgroup export flag onto another. The demo answers with the shape
  // rather than rewriting its dataset: what matters here is that the page reports what
  // came back, including how many rows the trunked rule held.
  if (path === "/admin/talkgroups/export-flags/copy" && method === "POST") {
    requireSession(path);
    const body = (payload ?? {}) as {
      source?: string;
      destination?: string;
      ids?: number[];
      all?: boolean;
    };
    const considered = body.all ? TALKGROUPS.length : (body.ids?.length ?? 0);
    return as<TalkgroupExportFlagCopyResult>({
      source: String(body.source ?? ""),
      destination: String(body.destination ?? ""),
      considered,
      updated: considered,
      unchanged: 0,
      skipped_not_trunked: 0,
      items: [],
    });
  }
  const exportRetryMatch = /^\/admin\/exports\/([a-z]+)\/ledger\/(\d+)\/retry$/.exec(path);
  if (exportRetryMatch && method === "POST") {
    requireSession(path);
    return notFound(path);
  }
  /* ------------------------------------------- Broadcastify feed statistics */
  // A demo feed with a believable week in it: quiet overnight, busy at the 17:00
  // shift change, and call volume that follows the same shape — which is what makes
  // the two heatmaps and the correlation readable in the mock without a backend.
  if (path === "/admin/bcfy-stats/summary" && method === "GET") {
    return as<FeedSummary>(feedSummary(Number(params.days ?? 30)));
  }
  if (path === "/admin/bcfy-stats/samples" && method === "GET") {
    return as<FeedSamplePoint[]>(feedSamples(String(params.step ?? "hour") === "day" ? 14 : 168));
  }
  if (path === "/admin/bcfy-stats/archives" && method === "GET") {
    const day = params.day ? String(params.day) : undefined;
    return as<FeedArchive[]>(FEED_ARCHIVES.filter((row) => !day || row.day === day));
  }
  if (path === "/admin/bcfy-stats/probe" && method === "POST") {
    requireSession(path);
    return as<FeedProbeResult>({
      ok: true,
      feed_id: "2211",
      error: null,
      raw: { feed: { feedId: 2211, descr: "Demo feed", currentListeners: 24, status: 1 } },
      extracted: { listeners: 24, online: true, name: "Demo feed" },
      unmapped_fields: ["feedId"],
    });
  }
  if (path === "/admin/bcfy-stats/poll" && method === "POST") {
    requireSession(path);
    return as<FeedProbeResult>({
      ok: true,
      feed_id: "2211",
      error: null,
      raw: { feed: { currentListeners: 24 } },
      extracted: { listeners: 24 },
      unmapped_fields: [],
    });
  }
  if (path === "/admin/bcfy-stats/poll-page" && method === "POST") {
    requireSession(path);
    // A day of bars, of which most hours had already been polled here — which is what the
    // merge rule makes the ordinary case once the poller has been running.
    const bars = Array.from({ length: 24 }, (_, hour) => 8 + ((hour * 5) % 17));
    return as<FeedPublicPageResult>({
      ok: true,
      feed_id: "2211",
      error: null,
      bars,
      hours_written: 3,
      online: true,
      since: new Date(Date.now() - 6 * 60_000).toISOString(),
      listeners: 24,
    });
  }
  if (path === "/admin/bcfy-stats/import" && method === "POST") {
    requireSession(path);
    // The real route decides the format from the file's header and nothing else, so the
    // mock reads the same first line rather than inventing a parameter that does not
    // exist. A file that is neither is a 422 here too, which is what the page's error
    // path is written against.
    const content = String((payload as { content?: unknown })?.content ?? "");
    const header = content.trimStart().split("\n", 1)[0]?.toLowerCase() ?? "";
    if (!header.startsWith("hour,") && !header.startsWith("day,")) {
      throw new ApiError(
        "422 Unprocessable Entity",
        422,
        path,
        "not a Broadcastify dashboard export: expected a Hour,… or Day,… header",
      );
    }
    const hourly = header.startsWith("hour,");
    const rows = content.trim().split("\n").length - 1;
    const first = FEED_DAILY[0]?.day ?? null;
    const last = FEED_DAILY[FEED_DAILY.length - 1]?.day ?? null;
    return as<FeedImportReport>({
      format: hourly ? "hourly" : "daily",
      rows_parsed: rows,
      // A couple of hours had already been polled here, which is the ordinary case once
      // the poller has been running: an import never overwrites a poll.
      rows_written: hourly ? Math.max(0, rows - 2) : rows,
      rows_skipped: 0,
      first: hourly ? new Date(Date.now() - rows * 3600 * 1000).toISOString() : first,
      last: hourly ? new Date().toISOString() : last,
      skipped_reasons: {},
    });
  }
  if (path === "/admin/bcfy-stats/sync-archives" && method === "POST") {
    requireSession(path);
    return as({ days_listed: 7, rows: FEED_ARCHIVES.length, errors: [] });
  }

  if (path === "/admin/retention" && method === "GET") return as(retention);
  if (path === "/admin/retention" && method === "POST") {
    requireSession(path);
    const input = payload as unknown as Partial<RetentionPolicy>;
    if (input.id) {
      retention = retention.map((policy) => (policy.id === input.id ? { ...policy, ...input } : policy));
    } else {
      retention = [
        ...retention,
        {
          scope: "global",
          scope_id: null,
          keep_days: 0,
          keep_audio_days: 0,
          keep_transcripts: true,
          archive_path: null,
          enabled: true,
          ...input,
          id: ++nextId,
          scope_label: input.scope_label ?? "New policy",
        },
      ];
    }
    return as(retention);
  }
  if (path === "/admin/retention/run" && method === "POST") {
    requireSession(path);
    return as<RetentionRunResult>({
      audio_deleted: 1284,
      audio_archived: 96,
      calls_purged: 0,
      radio_events_purged: 4128,
      bytes_freed: 3.1 * 1024 ** 3,
      dry_run: false,
    });
  }
  /* ------------------------------------------------------ admin: agencies */
  if (path === "/admin/agencies" && method === "GET") return as(agencies);
  if (path === "/admin/agencies" && method === "POST") {
    requireSession(path);
    const code = String(payload.code ?? "")
      .trim()
      .toUpperCase();
    if (!AGENCY_CODE_RE.test(code)) {
      throw new ApiError("422 Unprocessable", 422, path, `code: ${code} is not a valid agency code`);
    }
    if (agencies.some((agency) => agency.code === code)) {
      throw new ApiError("409 Conflict", 409, path, `agency '${code}' already exists`);
    }
    const created: AdminAgency = {
      code,
      name: String(payload.name ?? code).trim(),
      // The API defaults the short name from the code rather than asking twice.
      short_name: String(payload.short_name ?? "").trim() || code,
      kind: String(payload.kind ?? "police"),
      source: "local",
      active: payload.active === undefined ? true : Boolean(payload.active),
      edited_at: new Date().toISOString(),
      talkgroup_count: 0,
      radio_count: 0,
      unit_count: 0,
      person_count: 0,
    };
    agencies = [...agencies, created];
    return as(created);
  }
  const agencyMatch = /^\/admin\/agencies\/([^/]+)$/.exec(path);
  if (agencyMatch && (method === "PATCH" || method === "DELETE")) {
    requireSession(path);
    const code = decodeURIComponent(agencyMatch[1] as string);
    const existing = agencies.find((agency) => agency.code === code);
    if (!existing) throw new ApiError("404 Not Found", 404, path, `no agency '${code}'`);
    if (method === "PATCH") {
      const updated: AdminAgency = {
        ...existing,
        name: payload.name === undefined ? existing.name : String(payload.name).trim(),
        short_name:
          payload.short_name === undefined
            ? existing.short_name
            : String(payload.short_name ?? "").trim() || existing.code,
        kind: payload.kind === undefined ? existing.kind : String(payload.kind),
        active: payload.active === undefined ? existing.active : Boolean(payload.active),
        edited_at: new Date().toISOString(),
      };
      agencies = agencies.map((agency) => (agency.code === code ? updated : agency));
      return as(updated);
    }
    // The two guards the real API applies, in the same order, so the page's error
    // handling is exercised rather than assumed.
    if (existing.source !== "local") {
      throw new ApiError(
        "409 Conflict",
        409,
        path,
        `${code} came from the seeded agency list and cannot be deleted; set active to false instead`,
      );
    }
    const mapped =
      existing.talkgroup_count + existing.radio_count + existing.unit_count + existing.person_count;
    if (mapped > 0) {
      throw new ApiError(
        "409 Conflict",
        409,
        path,
        `${code} is still mapped to ${mapped} records; reassign them or set active to false instead`,
      );
    }
    agencies = agencies.filter((agency) => agency.code !== code);
    return as<null>(null);
  }

  if (path === "/admin/tokens" && method === "GET") return as(tokens);
  if (path === "/admin/tokens" && method === "POST") {
    requireSession(path);
    const scopes = (payload.scopes as TokenScope[] | undefined) ?? ["read"];
    const secret = `kup_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
    const created: ApiTokenCreated = {
      id: ++nextId,
      label: String(payload.label ?? "New token"),
      scopes,
      created_by: session?.username ?? null,
      created_at: new Date().toISOString(),
      last_used_at: null,
      expires_at: (payload.expires_at as string | null) ?? null,
      revoked_at: null,
      used_by_recorders: [],
      prefix: secret.slice(0, 8),
      suffix: secret.slice(-4),
      token: secret,
    };
    const { token: _token, ...info } = created;
    tokens = [info, ...tokens];
    return as(created);
  }
  const revokeMatch = /^\/admin\/tokens\/(\d+)\/revoke$/.exec(path);
  if (revokeMatch && method === "POST") {
    requireSession(path);
    tokens = tokens.map((token) =>
      token.id === Number(revokeMatch[1]) ? { ...token, revoked_at: new Date().toISOString() } : token,
    );
    return as<null>(null);
  }
  const tokenMatch = /^\/admin\/tokens\/(\d+)$/.exec(path);
  if (tokenMatch && method === "DELETE") {
    requireSession(path);
    // Deleting is for revoked rows only — the API answers 409 on a live token, and a mock
    // that swallowed it would let the UI ship a Delete button on an active row.
    const id = Number(tokenMatch[1]);
    const doomed = tokens.find((token) => token.id === id);
    if (!doomed) throw new ApiError("404 Not Found", 404, path, `no token ${id}`);
    if (doomed.revoked_at === null) {
      throw new ApiError("409 Conflict", 409, path, `token ${id} is still active; revoke it first`);
    }
    tokens = tokens.filter((token) => token.id !== id);
    return as<null>(null);
  }
  if (path === "/admin/users" && method === "GET") return as(users);
  if (path === "/admin/users" && method === "POST") {
    requireSession(path);
    // `create_user` refuses a passwordless account with a 422, and the mock used
    // to accept one — so the Users tab shipped a create form that could not
    // create anything, and the tests agreed with it. The mock says no too now.
    if (!payload.password) {
      throw new ApiError("422 Unprocessable Entity", 422, path, "password is required");
    }
    const user: User = {
      id: ++nextId,
      username: String(payload.username ?? "new-user"),
      display_name: (payload.display_name as string | null) ?? null,
      email: (payload.email as string | null) ?? null,
      scopes: payload.is_admin ? ["admin"] : ["read"],
      is_admin: Boolean(payload.is_admin),
      is_active: true,
      created_at: new Date().toISOString(),
      last_login_at: null,
    };
    users = [...users, user];
    return as(user);
  }
  const userMatch = /^\/admin\/users\/(\d+)$/.exec(path);
  if (userMatch && method === "PATCH") {
    requireSession(path);
    // A password is written to the hash and never read back, so it must not be
    // splatted onto the row the way the other keys are — `UserOut` has no such
    // field, and echoing one would put a plaintext password in the query cache.
    const { password: _password, ...visible } = payload;
    users = users.map((user) => (user.id === Number(userMatch[1]) ? { ...user, ...visible } : user));
    return as(users.find((user) => user.id === Number(userMatch[1])) as User);
  }

  /* -------------------------------------------------------------- alerts */
  if (path === "/alerts" && method === "GET") return as(alerts);
  if (path === "/alerts" && method === "POST") {
    requireSession(path);
    const rule = {
      ...(payload as unknown as AlertRule),
      id: ++nextId,
      last_fired_at: null,
      fire_count: 0,
      created_at: new Date().toISOString(),
    };
    alerts = [rule, ...alerts];
    return as(rule);
  }
  const alertMatch = /^\/alerts\/(\d+)$/.exec(path);
  if (alertMatch) {
    requireSession(path);
    const id = Number(alertMatch[1]);
    if (method === "DELETE") {
      alerts = alerts.filter((rule) => rule.id !== id);
      return as<null>(null);
    }
    if (method === "PATCH") {
      alerts = alerts.map((rule) => (rule.id === id ? { ...rule, ...payload } : rule));
      return as(alerts.find((rule) => rule.id === id) as AlertRule);
    }
  }
  if (path === "/notifications") return as(paginate(NOTIFICATIONS, params));

  /* ----------------------------------------------------------- feeds */
  if (path === "/feeds/presets" && method === "GET") return as(FEED_PRESETS);
  if (path === "/feeds" && method === "GET") return as(feeds);
  if (path === "/feeds" && method === "POST") {
    requireSession(path);
    const now = new Date().toISOString();
    const query = (payload.query ?? {}) as Record<string, unknown>;
    const token = `mock-feed-token-${++nextId}`;
    const feed: SyndicationFeed = {
      id: nextId,
      name: String(payload.name ?? "Untitled feed"),
      query,
      summary: describeMockFilters(query),
      enabled: payload.enabled === undefined ? true : Boolean(payload.enabled),
      include_audio: Boolean(payload.include_audio),
      max_items: Math.min(Number(payload.max_items ?? 50), 200),
      rss_url: `http://localhost/api/feeds/${token}.rss`,
      atom_url: `http://localhost/api/feeds/${token}.atom`,
      owner: null,
      created_at: now,
      updated_at: now,
      last_fetched_at: null,
      fetch_count: 0,
    };
    feeds = [feed, ...feeds];
    return as(feed);
  }
  const feedRotate = /^\/feeds\/(\d+)\/rotate$/.exec(path);
  if (feedRotate && method === "POST") {
    requireSession(path);
    const id = Number(feedRotate[1]);
    const token = `mock-feed-token-${++nextId}`;
    feeds = feeds.map((feed) =>
      feed.id === id
        ? {
            ...feed,
            rss_url: `http://localhost/api/feeds/${token}.rss`,
            atom_url: `http://localhost/api/feeds/${token}.atom`,
          }
        : feed,
    );
    const rotated = feeds.find((feed) => feed.id === id);
    if (!rotated) notFound(path);
    return as(rotated as SyndicationFeed);
  }
  const feedMatch = /^\/feeds\/(\d+)$/.exec(path);
  if (feedMatch) {
    requireSession(path);
    const id = Number(feedMatch[1]);
    if (method === "DELETE") {
      feeds = feeds.filter((feed) => feed.id !== id);
      return as<null>(null);
    }
    if (method === "PATCH") {
      feeds = feeds.map((feed) => (feed.id === id ? { ...feed, ...payload } : feed));
      const patched = feeds.find((feed) => feed.id === id);
      if (!patched) notFound(path);
      return as(patched as SyndicationFeed);
    }
  }

  /* ------------------------------------------------- searches, favorites */
  if (path === "/saved-searches" && method === "GET") return as(savedSearches);
  if (path === "/saved-searches" && method === "POST") {
    requireSession(path);
    const saved: SavedSearch = {
      id: ++nextId,
      name: String(payload.name ?? "Untitled search"),
      query: String(payload.query ?? ""),
      shared: Boolean(payload.shared),
      created_at: new Date().toISOString(),
      created_by: session?.username ?? null,
    };
    savedSearches = [saved, ...savedSearches];
    return as(saved);
  }
  const savedMatch = /^\/saved-searches\/(\d+)$/.exec(path);
  if (savedMatch && method === "DELETE") {
    requireSession(path);
    savedSearches = savedSearches.filter((saved) => saved.id !== Number(savedMatch[1]));
    return as<null>(null);
  }
  if (path === "/favorites" && method === "GET") return as(favorites);
  if (path === "/favorites" && method === "PUT") {
    requireSession(path);
    const talkgroupId = Number(payload.talkgroup_id);
    const talkgroup = TALKGROUPS.find((tg) => tg.id === talkgroupId) ?? null;
    const existing = favorites.find((fav) => fav.talkgroup_id === talkgroupId);
    favorites = existing
      ? favorites.map((fav) =>
          fav.talkgroup_id === talkgroupId ? { ...fav, muted: Boolean(payload.muted) } : fav,
        )
      : [
          ...favorites,
          {
            talkgroup_id: talkgroupId,
            tgid: talkgroup?.tgid ?? null,
            alias: talkgroup ? talkgroupLabel(talkgroup) : null,
            muted: Boolean(payload.muted),
          },
        ];
    return as(favorites);
  }
  const favoriteMatch = /^\/favorites\/(\d+)$/.exec(path);
  if (favoriteMatch && method === "DELETE") {
    requireSession(path);
    favorites = favorites.filter((fav) => fav.talkgroup_id !== Number(favoriteMatch[1]));
    return as<null>(null);
  }

  /* ------------------------------------------------------------- exports */
  if (path === "/exports" && method === "POST") {
    const id = `exp_${++nextId}`;
    const job: ExportJob = {
      id,
      status: "queued",
      progress: 0,
      call_count: filterCalls(params).length,
      bytes: null,
      download_url: null,
      error: null,
      filters: { ...params },
      created_by: session?.username ?? null,
      created_at: new Date().toISOString(),
      finished_at: null,
    };
    exports.set(id, job);
    // Advance it over a few seconds so the UI's polling has something to show.
    setTimeout(() => {
      const running = exports.get(id);
      if (running) exports.set(id, { ...running, status: "running", progress: 0.45 });
    }, 1200);
    setTimeout(() => {
      const running = exports.get(id);
      if (running) {
        exports.set(id, {
          ...running,
          status: "done",
          progress: 1,
          bytes: running.call_count * 42_000,
          download_url: `/api/exports/${id}/download`,
          finished_at: new Date().toISOString(),
        });
      }
    }, 3600);
    return as(job);
  }
  const exportMatch = /^\/exports\/([\w-]+)$/.exec(path);
  if (exportMatch) {
    const job = exports.get(exportMatch[1] as string);
    if (!job) return notFound(path);
    return as(job);
  }

  /* ------------------------------------------------ recorders & tuners */
  if (path === "/admin/recorders" && method === "GET") return as(recorders);
  const policyMatch = /^\/admin\/recorders\/(\d+)$/.exec(path);
  if (policyMatch && method === "PUT") {
    const recorder = recorders.find((r) => r.id === Number(policyMatch[1]));
    if (!recorder) return notFound(path);
    // Merge like the backend: an omitted field is left alone. Turning a recorder cold
    // disowns any promotion, because a cold box is one the arbiter stops looking at.
    const changes = payload as unknown as RecorderPolicyInput;
    // A *new* object, never a mutation in place: React Query's structural sharing compares
    // the fetched data with what it already cached, and editing the cached object makes the
    // two identical — the list would refetch and re-render nothing.
    const next: Recorder = { ...recorder };
    // One counter for the three recorder-level settings, bumped once however many of them a
    // save changed: the recorder applies the block as a whole and echoes one number back.
    let bumped = false;
    if (changes.label !== undefined) next.label = changes.label?.trim() || null;
    if (changes.priority != null) next.priority = changes.priority;
    if (changes.standby != null) {
      if (changes.standby === "cold") next.promoted_at = null;
      next.standby = changes.standby;
    }
    // `spool_only` is the one field whose explicit null is a value — "keyup has no opinion" —
    // so the key's presence decides, and a real change bumps the version the recorder polls.
    if ("spool_only" in changes && changes.spool_only !== next.desired_spool_only) {
      next.desired_spool_only = changes.spool_only ?? null;
      next.desired_recorder_version += 1;
      // The recorder has not polled yet, so the write reads as pending and the `uplink` it
      // reports is left exactly as it was: that is the state the real API answers a PUT in.
      next.spool_only_pending = next.desired_spool_only !== null;
      bumped = true;
    }
    if ("event_log_archive" in changes && changes.event_log_archive !== next.desired_event_log_archive) {
      next.desired_event_log_archive = changes.event_log_archive ?? null;
      bumped = true;
    }
    if ("link" in changes) {
      const link = changes.link ?? null;
      if (link === null) {
        if (next.desired_link !== null) {
          next.desired_link = null;
          bumped = true;
        }
      } else {
        // The key never comes back out: the mock stores the fingerprint the same way the API
        // does, so a page that expected to read `key` off a saved recorder fails here too.
        const before = recorder.desired_link;
        next.desired_link = {
          enabled: link.enabled,
          bind: link.bind,
          peers: link.peers,
          hello_interval_s: link.hello_interval_s,
          accept_remote_grants: link.accept_remote_grants,
          publish_grants: link.publish_grants,
          defer_to_claims: link.defer_to_claims,
          key_set: link.key !== undefined ? link.key.length > 0 : (recorder.desired_link?.key_set ?? false),
          key_fingerprint:
            link.key !== undefined ? link.key.slice(0, 8) : (recorder.desired_link?.key_fingerprint ?? null),
        };
        // Only a real change bumps the counter the recorder polls, as the API does: a form
        // that resubmitted itself must not leave the recorder pending for ever.
        if (JSON.stringify(before) !== JSON.stringify(next.desired_link)) bumped = true;
      }
    }
    // The rename carries nothing here — the mock has no child tables to rewrite — but it does
    // have to refuse a name another recorder holds, which is the case the dialog handles.
    if (changes.instance_id != null && changes.instance_id.trim() !== recorder.instance_id) {
      const wanted = changes.instance_id.trim();
      const clash = recorders.find((r) => r.instance_id === wanted && r.id !== recorder.id);
      if (clash) {
        throw new ApiError(
          "409 Conflict",
          409,
          path,
          `another recorder (#${clash.id}) is already called '${wanted}'. Delete it with DELETE /api/admin/recorders/${clash.id} and try again.`,
        );
      }
      next.instance_id = wanted;
    }
    if (bumped) {
      next.desired_recorder_version += 1;
      next.recorder_pending =
        next.desired_spool_only !== null ||
        next.desired_event_log_archive !== null ||
        next.desired_link !== null;
    }
    recorders = recorders.map((r) => (r.id === next.id ? next : r));
    return as(next);
  }
  const linkKeyMatch = /^\/admin\/recorders\/(\d+)\/link\/key$/.exec(path);
  if (linkKeyMatch && method === "GET") {
    const recorder = recorders.find((r) => r.id === Number(linkKeyMatch[1]));
    if (!recorder) return notFound(path);
    const fingerprint = recorder.desired_link?.key_fingerprint ?? null;
    // The mock has no real secret to hold, so it answers with one that *starts* with the
    // stored fingerprint: a page that copies the revealed key into the other recorder ends
    // up with matching fingerprints, which is the behaviour worth reproducing.
    return as({
      key: fingerprint ? `${fingerprint}${"0".repeat(56)}` : null,
      key_set: recorder.desired_link?.key_set ?? false,
      key_fingerprint: fingerprint,
    });
  }
  const genKeyMatch = /^\/admin\/recorders\/(\d+)\/link\/generate-key$/.exec(path);
  if (genKeyMatch && method === "POST") {
    const recorder = recorders.find((r) => r.id === Number(genKeyMatch[1]));
    if (!recorder) return notFound(path);
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    return as({
      key: [...bytes].map((b) => b.toString(16).padStart(2, "0")).join(""),
    });
  }
  if (policyMatch && method === "DELETE") {
    requireSession(path);
    const recorder = recorders.find((r) => r.id === Number(policyMatch[1]));
    if (!recorder) return notFound(path);
    // The API refuses to forget a recorder that reported within the last hour, because its
    // next poll would create it again; `force` is what gets past that.
    const reported = recorder.reported_at ?? recorder.last_seen_at;
    const live = reported !== null && Date.now() - Date.parse(reported) < 3_600_000;
    if (live && params.force !== "true") {
      throw new ApiError(
        "409 Conflict",
        409,
        path,
        `${recorder.instance_id} is still reporting and would come straight back on its next poll.`,
      );
    }
    recorders = recorders.filter((r) => r.id !== recorder.id);
    return as(null);
  }
  const tunerMatch = /^\/admin\/recorders\/(\d+)\/tuners\/(.+)$/.exec(path);
  if (tunerMatch && method === "PUT") {
    const recorder = recorders.find((r) => r.id === Number(tunerMatch[1]));
    if (!recorder) return notFound(path);
    const key = decodeURIComponent(tunerMatch[2] as string);
    let tuner = recorder.tuners.find((t) => t.tuner_key === key);
    if (!tuner) {
      tuner = {
        id: ++nextId,
        tuner_key: key,
        reported: null,
        reported_at: null,
        desired: null,
        desired_version: 0,
        updated_by: null,
        updated_at: null,
        drift: false,
        rejected: null,
      };
      recorder.tuners = [...recorder.tuners, tuner];
    }
    // Merge like the backend: an omitted field is kept, a value sets it, null clears it.
    const changes = payload as unknown as TunerDesiredInput;
    const merged: Record<string, unknown> = { ...(tuner.desired ?? {}) };
    for (const [field, value] of Object.entries(changes)) {
      if (value === null) delete merged[field];
      else merged[field] = value;
    }
    tuner.desired = merged;
    tuner.desired_version += 1;
    tuner.updated_by = session ? `${session.username} (session)` : "operator";
    tuner.updated_at = new Date().toISOString();
    // Drift only once edited and the recorder has reported an applied_version that differs.
    const applied = tuner.reported?.applied_version ?? null;
    tuner.drift = tuner.desired_version > 0 && applied !== null && applied !== tuner.desired_version;
    recorder.version += 1;
    return as(tuner);
  }
  if (path === "/admin/event-logs" && method === "GET") {
    // Newest first, exactly as the API orders it, optionally narrowed to one instance.
    const instance = params.instance === undefined ? null : String(params.instance);
    const rows = EVENT_LOG_ARCHIVES.filter((row) => instance === null || row.instance === instance);
    return as([...rows].sort((a, b) => b.date.localeCompare(a.date)));
  }
  if (path === "/admin/occupancy" && method === "GET") {
    const siteId = params.site === undefined ? null : Number(params.site);
    const rows: SiteOccupancy[] =
      siteId === null ? OCCUPANCY : OCCUPANCY.filter((row) => row.site_id === siteId);
    return as(rows);
  }

  return notFound(path);
}
