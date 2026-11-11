/**
 * The contract test between the fixtures and the real API.
 *
 * `generated.ts` is produced from the API's own OpenAPI document, so anything
 * that assigns a mock object to a generated schema type is checked, at compile
 * time, against the shape the server actually sends. The runtime assertions below
 * only exist so `vitest` has something to run — the value of this file is that
 * `npm run typecheck` fails when a fixture drifts from the schema.
 */
import { describe, expect, it } from "vitest";
import type { components } from "./generated";
import {
  AFFILIATIONS,
  AGENCIES,
  ALERTS,
  BACKENDS,
  CALLS,
  CONVERSATIONS,
  GAPS,
  INGEST_FILES,
  INGEST_RUNS,
  JOBS,
  PATCHES,
  RADIOS,
  RETENTION_POLICIES,
  SETTINGS,
  SITES,
  SYSTEM,
  TALKGROUPS,
  TOKENS,
  USERS,
} from "./mock/dataset";
import { handleMock } from "./mock/index";
import type {
  ALERT_KINDS,
  AlertKind,
  CALL_TYPES,
  CallDetail,
  CallFilterKey,
  CallFilterParams,
  CallType,
  ConversationThread,
  INGEST_FILE_STATUSES,
  IngestFileStatus,
  JOB_STATUSES,
  JobStatus,
  LINK_CONFIDENCES,
  LinkConfidence,
  Paginated,
  QUALITIES,
  RADIO_KINDS,
  RadioKind,
  RETENTION_SCOPES,
  RetentionScope,
  STT_ENGINES,
  SttEngine,
  TIME_SOURCES,
  TimeSource,
  TOKEN_SCOPES,
  TokenScope,
  TRANSCRIPT_STATUSES,
  TranscriptQuality,
  TranscriptStatus,
  Waveform,
} from "./types";

type Schemas = components["schemas"];

/**
 * `A` and `B` describe exactly the same set of strings. Used below to prove the
 * runtime `const` arrays have not fallen behind the enums the schema declares:
 * every entry of `ENUM_COVERAGE` is typed `true`, so a drifted array makes the
 * type `false` and `npm run typecheck` fails on the assignment.
 */
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;

const ENUM_COVERAGE = {
  call_type: true as Same<(typeof CALL_TYPES)[number], CallType>,
  transcript_status: true as Same<(typeof TRANSCRIPT_STATUSES)[number], TranscriptStatus>,
  quality: true as Same<(typeof QUALITIES)[number], TranscriptQuality>,
  time_source: true as Same<(typeof TIME_SOURCES)[number], TimeSource>,
  radio_kind: true as Same<(typeof RADIO_KINDS)[number], RadioKind>,
  job_status: true as Same<(typeof JOB_STATUSES)[number], JobStatus>,
  ingest_file_status: true as Same<(typeof INGEST_FILE_STATUSES)[number], IngestFileStatus>,
  stt_engine: true as Same<(typeof STT_ENGINES)[number], SttEngine>,
  link_confidence: true as Same<(typeof LINK_CONFIDENCES)[number], LinkConfidence>,
  alert_kind: true as Same<(typeof ALERT_KINDS)[number], AlertKind>,
  token_scope: true as Same<(typeof TOKEN_SCOPES)[number], TokenScope>,
  retention_scope: true as Same<(typeof RETENTION_SCOPES)[number], RetentionScope>,
} satisfies Record<string, true>;

/* -------------------------------------------------- filter keys are real */

/**
 * Every key of `CallFilterParams` is a query parameter `GET /calls` declares.
 * A typo, or a filter the API dropped, makes this `never` and fails the build.
 */
const FILTER_KEYS_ARE_REAL: keyof CallFilterParams extends CallFilterKey ? true : never = true;

/* ------------------------------------------- fixtures against the schema */

/** The whole point: each of these is a compile-time assignability check. */
const contract = {
  system: SYSTEM satisfies Schemas["SystemOut"],
  site: SITES[0] satisfies Schemas["SiteOut"] | undefined,
  talkgroup: TALKGROUPS[0] satisfies Schemas["TalkgroupOut"] | undefined,
  radio: RADIOS[0] satisfies Schemas["RadioOut"] | undefined,
  patch: PATCHES[0] satisfies Schemas["PatchOut"] | undefined,
  affiliation: AFFILIATIONS[0] satisfies Schemas["RadioAffiliationOut"] | undefined,
  agency: AGENCIES[0] satisfies Schemas["AgencyOut"] | undefined,
  conversation: CONVERSATIONS[0] satisfies Schemas["ConversationOut"] | undefined,
  ingestRun: INGEST_RUNS[0] satisfies Schemas["IngestRunOut"] | undefined,
  ingestFile: INGEST_FILES[0] satisfies Schemas["IngestFileOut"] | undefined,
  job: JOBS[0] satisfies Schemas["TranscriptionJobOut"] | undefined,
  backend: BACKENDS[0] satisfies Schemas["SttBackendOut"] | undefined,
  retention: RETENTION_POLICIES[0] satisfies Schemas["RetentionPolicyOut"] | undefined,
  gap: GAPS[0] satisfies Schemas["RecorderGapOut"] | undefined,
  setting: SETTINGS[0] satisfies Schemas["SettingDefinition"] | undefined,
  token: TOKENS[0] satisfies Schemas["ApiTokenInfo"] | undefined,
  user: USERS[0] satisfies Schemas["UserOut"] | undefined,
  alert: ALERTS[0] satisfies Schemas["AlertRuleOut"] | undefined,
};

describe("mock fixtures match the generated OpenAPI schema", () => {
  it("keeps every runtime enum list level with the schema", () => {
    for (const [name, covered] of Object.entries(ENUM_COVERAGE)) {
      expect(covered, `${name} list has drifted from the schema`).toBe(true);
    }
  });

  it("only sends filter keys the API declares", () => {
    expect(FILTER_KEYS_ARE_REAL).toBe(true);
  });

  it("has a fixture for every shape the contract covers", () => {
    for (const [name, value] of Object.entries(contract)) {
      expect(value, `${name} fixture is missing`).toBeDefined();
    }
  });

  it("serves calls that satisfy CallOut", async () => {
    const { data } = await handleMock<Paginated<Schemas["CallOut"]>>("GET", "/calls", { page_size: 3 });
    expect(data.items.length).toBe(3);
    const row = data.items[0] as Schemas["CallOut"];
    // Required-on-the-wire fields the UI reads without a guard.
    for (const key of ["id", "started_at", "started_local", "ended_at", "duration_s", "audio_url"]) {
      expect(row, key).toHaveProperty(key);
    }
    expect(Array.isArray(row.talkgroup_ids)).toBe(true);
    expect(Array.isArray(row.tags)).toBe(true);
  });

  it("serves a call detail that satisfies CallDetail, conversation record and all", async () => {
    const first = CALLS.find((call) => call.conversation_id !== null);
    const { data } = await handleMock<CallDetail>("GET", `/calls/${first?.id}`);
    const detail: Schemas["CallDetail"] = data;
    expect(detail.raw.file_name).toContain(".mp3");
    // The thing this whole pass exists for: a record plus ids, never a call array.
    expect(Array.isArray(detail.conversation)).toBe(false);
    expect(detail.conversation?.call_count).toBeGreaterThan(1);
    expect(detail.conversation_call_ids?.length).toBeGreaterThan(1);
  });

  it("serves a conversation thread that satisfies ConversationThread", async () => {
    const id = CONVERSATIONS[0]?.id;
    const { data } = await handleMock<ConversationThread>("GET", `/conversations/${id}`);
    const thread: Schemas["ConversationThread"] = data;
    expect(thread.calls?.length).toBe(thread.conversation.call_count);
  });

  it("serves waveform peaks that satisfy WaveformOut", async () => {
    const id = CALLS[0]?.id;
    const { data } = await handleMock<Waveform>("GET", `/calls/${id}/waveform`);
    const waveform: Schemas["WaveformOut"] = data;
    expect(waveform.peaks.length).toBe(waveform.buckets);
  });

  it("serves facets, stats and status that satisfy their schemas", async () => {
    const facets = (await handleMock<Schemas["FacetsOut"]>("GET", "/calls/facets")).data;
    expect(facets.hour).toHaveLength(24);
    expect(facets.dow).toHaveLength(7);

    const overview = (await handleMock<Schemas["StatsOverview"]>("GET", "/stats/overview")).data;
    expect(overview.calls_total).toBeGreaterThan(0);

    const status = (await handleMock<Schemas["SystemStatusOut"]>("GET", "/status")).data;
    expect(status.ok).toBe(true);
    expect(status.tz).toBe("America/Denver");

    const activity = (await handleMock<Schemas["ActivityResponse"]>("GET", "/stats/activity")).data;
    expect(activity.cells.length).toBeGreaterThan(0);
    expect(activity.matrix?.length).toBe(activity.rows?.length);
  });
});
