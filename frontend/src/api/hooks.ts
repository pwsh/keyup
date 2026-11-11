/**
 * TanStack Query hooks — one per API resource, and the only place a component
 * names an endpoint. Reconciling the app with the real OpenAPI schema means
 * touching this file, `client.ts`, `types.ts` and `mock/` and nothing else.
 */

import {
  type InfiniteData,
  keepPreviousData,
  type QueryClient,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { agencyLabel } from "../lib/format";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPostBlob,
  apiPut,
  apiUrl,
  type QueryParams,
  toQueryParams,
} from "./client";
import { nextSearchPageParam, type SearchPageParam } from "./searchScroll";
import type {
  ActivityEvent,
  ActivityResponse,
  AdminAgency,
  AdminIngestResponse,
  Agency,
  AgencyIn,
  AgencyPatch,
  AlertRule,
  AlertRuleInput,
  ApiTokenCreated,
  ApiTokenInfo,
  ApiTokenInput,
  AudioPreview,
  AudioProfile,
  AudioProfileApplyInput,
  AudioProfileApplyResult,
  AudioProfileCatalog,
  AudioProfileInput,
  AudioProfilePatch,
  AudioProfilePreviewInput,
  AudioReprocessInput,
  AudioReprocessResult,
  AudioStatus,
  Call,
  CallDetail,
  CallFilterParams,
  CallPatch,
  Conversation,
  ConversationThread,
  EventLogArchive,
  ExportJob,
  ExportLedgerRow,
  ExportService,
  ExportStatus,
  ExportTestResult,
  Facets,
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
  IngestFile,
  IngestLag,
  IngestSourceInput,
  IngestSourceRow,
  IngestSourcesView,
  LiveActivitySnapshot,
  LoginRequest,
  MeResponse,
  NotificationRecord,
  Paginated,
  Patch,
  Person,
  PersonBulkResult,
  PersonDetail,
  PersonInput,
  PersonPatch,
  Radio,
  RadioAffiliation,
  RadioAssignment,
  RadioAssignmentInput,
  RadioBulkResult,
  RadioDetail,
  RadioHistory,
  RadioPatch,
  RadioStat,
  Recorder,
  RecorderGap,
  RecorderLinkKey,
  RecorderPolicyInput,
  RetentionPolicy,
  RetentionPolicyInput,
  RetentionRunResult,
  SavedSearch,
  SearchResponse,
  SettingDefinition,
  SiteChannelAudit,
  SiteFrequency,
  SiteFrequencyPatch,
  SiteInfo,
  SiteMergeResult,
  SiteMessageKind,
  SiteMessageKindPatch,
  SiteOccupancy,
  SitePatch,
  SiteProfile,
  StatsOverview,
  SttBackend,
  SttBackendConfigInput,
  SttBackendDeleteResult,
  SttBackendProbe,
  SttBackendStat,
  SttBackendsView,
  Suggestion,
  SyndicationFeed,
  SyndicationFeedInput,
  SyndicationFeedPatch,
  SystemChannelAudit,
  SystemInfo,
  SystemMergeResult,
  SystemPatch,
  SystemStatus,
  Talkgroup,
  TalkgroupBulkResult,
  TalkgroupDetail,
  TalkgroupExportFlag,
  TalkgroupExportFlagCopyResult,
  TalkgroupPatch,
  TalkgroupStat,
  TalkgroupStatsResponse,
  TimelinePoint,
  TranscribeAccepted,
  Transcript,
  TranscriptionJob,
  TranscriptionStats,
  Tuner,
  TunerDesiredInput,
  Unit,
  UnitBulkResult,
  UnitDetail,
  UnitInput,
  UnitPatch,
  User,
  UserInput,
  UserPatch,
  Vocabulary,
  Waveform,
} from "./types";

/** Buckets used as query-key prefixes, so a mutation can invalidate a whole area. */
export const KEYS = {
  calls: ["calls"] as const,
  call: (id: number) => ["calls", id] as const,
  facets: ["calls", "facets"] as const,
  search: ["search"] as const,
  talkgroups: ["talkgroups"] as const,
  radios: ["radios"] as const,
  units: ["units"] as const,
  personnel: ["personnel"] as const,
  stats: ["stats"] as const,
  admin: ["admin"] as const,
  recorders: ["recorders"] as const,
  eventLogs: ["event-logs"] as const,
  occupancy: ["occupancy"] as const,
  activity: ["activity"] as const,
  alerts: ["alerts"] as const,
  auth: ["auth", "me"] as const,
  feedStats: ["admin", "bcfy-stats"] as const,
  /** Nested under the Audio tab's own key, so one invalidation refreshes both. */
  audioProfiles: ["admin", "audio", "profiles"] as const,
} as const;

/** How long a list stays fresh: long enough to make navigation instant. */
const LIST_STALE = 30_000;

/* ------------------------------------------------------------------ calls */

export interface CallsPage extends Paginated<Call> {}

export function useCalls(filters: CallFilterParams, enabled = true) {
  const params = toQueryParams(filters);
  return useQuery<CallsPage>({
    queryKey: [...KEYS.calls, "list", params],
    queryFn: ({ signal }) => apiGet<CallsPage>("/calls", params, signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

/**
 * The sorts a keyset cursor can continue — the API's `CURSOR_SORTS`. Any other sort is a
 * 422 with a cursor, so those pages go by `page` instead.
 */
const CURSOR_SORTS: ReadonlySet<string> = new Set(["started_at", "started_local"]);

/** What a scroll page adds to the list's params: nothing for the first, then a cursor or a page. */
type CallsPageParam = { cursor?: string; page?: number };

/**
 * The Calls page's list: one page of `page_size` rows, and the next one when the reader
 * scrolls towards the end (`fetchNextPage`).
 *
 * The first page is what the reader waits for, so it is kept small; asking for 500 rows
 * up front was ~0.7 MB of JSON for a screen that shows thirty. Under a time sort the next
 * page continues from the last row's keyset cursor — an index range on the server, and
 * immune to calls arriving on top while the reader scrolls. Under any other sort it is
 * the next `page`, and the list dedupes the odd row an arrival shifts across the boundary.
 */
export function useCallsScroll(filters: CallFilterParams, enabled = true) {
  const params = toQueryParams(filters);
  const keyset = CURSOR_SORTS.has((filters.sort ?? "-started_at").replace(/^-/, ""));
  return useInfiniteQuery<
    CallsPage,
    Error,
    InfiniteData<CallsPage, CallsPageParam>,
    readonly unknown[],
    CallsPageParam
  >({
    queryKey: [...KEYS.calls, "scroll", params],
    queryFn: ({ pageParam, signal }) => apiGet<CallsPage>("/calls", { ...params, ...pageParam }, signal),
    initialPageParam: {},
    getNextPageParam: (last) => {
      if (last.items.length < last.page_size) return undefined;
      if (keyset) return last.next_cursor ? { cursor: last.next_cursor } : undefined;
      return { page: last.page + 1 };
    },
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

/**
 * How long the facet params settle before the sidebar asks for counts.
 *
 * The facets are nine aggregate queries over `calls`, and the filter bar fires a change
 * per click: ticking seven categories in the picker used to be seven requests and
 * sixty-three heavy queries, of which only the last one's answer was ever drawn. The list
 * itself is *not* debounced — the rows are what the reader is looking at, and they must
 * move the instant the filter does — only the counts beside it, which are a hint.
 */
const FACET_DEBOUNCE_MS = 400;

export function useCallFacets(filters: CallFilterParams, enabled = true) {
  const params = toQueryParams(filters);
  // Serialised so the effect compares by value: `toQueryParams` builds a fresh object on
  // every render, and an identity comparison would restart the timer forever.
  const serialised = JSON.stringify(params);
  // Seeded with the first render's params so a cold page asks immediately; the mount
  // effect then settles on an equal value, and TanStack hashes query keys structurally,
  // so that is not a second request.
  const [settled, setSettled] = useState<QueryParams>(params);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(JSON.parse(serialised) as QueryParams), FACET_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [serialised]);
  return useQuery<Facets>({
    queryKey: [...KEYS.facets, settled],
    queryFn: ({ signal }) => apiGet<Facets>("/calls/facets", settled, signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

export function useCall(id: number | null) {
  return useQuery<CallDetail>({
    queryKey: KEYS.call(id ?? 0),
    queryFn: ({ signal }) => apiGet<CallDetail>(`/calls/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

export function useCallTranscripts(id: number | null) {
  return useQuery<Transcript[]>({
    queryKey: [...KEYS.call(id ?? 0), "transcripts"],
    queryFn: ({ signal }) => apiGet<Transcript[]>(`/calls/${id}/transcripts`, undefined, signal),
    enabled: id !== null,
  });
}

export function useWaveform(id: number | null, enabled = true) {
  return useQuery<Waveform>({
    queryKey: [...KEYS.call(id ?? 0), "waveform"],
    queryFn: ({ signal }) => apiGet<Waveform>(`/calls/${id}/waveform`, undefined, signal),
    enabled: id !== null && enabled,
    staleTime: Number.POSITIVE_INFINITY,
  });
}

export function useUpdateCall() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: CallPatch }) =>
      apiPatch<CallDetail>(`/calls/${id}`, patch),
    onSuccess: (detail) => {
      queryClient.setQueryData(KEYS.call(detail.id), detail);
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

export function useRetranscribe() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, backend, force }: { id: number; backend?: string; force?: boolean }) =>
      apiPost<TranscribeAccepted>(`/calls/${id}/transcribe`, {
        ...(backend ? { backend } : {}),
        ...(force ? { force } : {}),
      }),
    onSuccess: (_result, variables) => {
      void queryClient.invalidateQueries({ queryKey: KEYS.call(variables.id) });
      void queryClient.invalidateQueries({ queryKey: KEYS.admin });
    },
  });
}

/**
 * `POST /calls/{id}/make-primary` — force this copy to be the primary of its pair.
 *
 * The operator overruling reconciliation's score (docs/RECONCILIATION.md). Both calls in
 * the pair change, and so does every listing that hid one of them, so the call cache is
 * invalidated wholesale rather than patched.
 */
export function useMakePrimary() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiPost<CallDetail>(`/calls/${id}/make-primary`, {}),
    onSuccess: (detail) => {
      queryClient.setQueryData(KEYS.call(detail.id), detail);
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

export function useDeleteCall() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<null>(`/calls/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.calls }),
  });
}

/* ----------------------------------------------------------------- export */

export function useCreateExport() {
  return useMutation({
    mutationFn: (filters: CallFilterParams) => apiPost<ExportJob>("/exports", {}, toQueryParams(filters)),
  });
}

export function useExportJob(id: string | null) {
  return useQuery<ExportJob>({
    queryKey: ["exports", id],
    queryFn: ({ signal }) => apiGet<ExportJob>(`/exports/${id}`, undefined, signal),
    enabled: id !== null,
    // Poll while the zip is being built; stop the moment it is finished.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "done" || status === "failed" ? false : 1200;
    },
  });
}

/* ---------------------------------------------------------------- search */

/**
 * The Search page's results: one page of hits, and the next when the reader scrolls
 * towards the end (`fetchNextPage`) — by `page`, since a ranked search has no cursor; see
 * ./searchScroll.ts for the paging rules and the cap.
 */
export function useSearchScroll(filters: CallFilterParams, enabled = true) {
  // No facets here: they are a second pass over the match set (nine aggregates, 0.1–6 s on
  // a first look) and the hits must not wait for them. The Search page asks
  // `/calls/facets` for the same filter beside this — see `useCallFacets`.
  // `fallback`: when the exact query matches nothing, the API retries it with spelling
  // variants and says so in `fuzzy`. A query with any exact hit comes back exact.
  const params = { ...toQueryParams(filters), facets: false, fallback: true };
  return useInfiniteQuery<
    SearchResponse,
    Error,
    InfiniteData<SearchResponse, SearchPageParam>,
    readonly unknown[],
    SearchPageParam
  >({
    queryKey: [...KEYS.search, "scroll", params],
    queryFn: ({ pageParam, signal }) =>
      apiGet<SearchResponse>("/search", { ...params, ...pageParam }, signal),
    initialPageParam: {},
    getNextPageParam: (last, pages) => nextSearchPageParam(last, pages),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

export function useSuggestions(q: string) {
  return useQuery<Suggestion[]>({
    queryKey: [...KEYS.search, "suggest", q],
    // `fuzzy`: after every substring match, aliases spelled like `q` (marked `fuzzy`).
    queryFn: ({ signal }) => apiGet<Suggestion[]>("/search/suggest", { q, fuzzy: true }, signal),
    enabled: q.trim().length >= 2,
    staleTime: 300_000,
  });
}

export function useSavedSearches() {
  return useQuery<SavedSearch[]>({
    queryKey: ["saved-searches"],
    queryFn: ({ signal }) => apiGet<SavedSearch[]>("/saved-searches", undefined, signal),
  });
}

export function useSaveSearch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string; query: string; shared: boolean }) =>
      apiPost<SavedSearch>("/saved-searches", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["saved-searches"] }),
  });
}

export function useDeleteSavedSearch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<null>(`/saved-searches/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["saved-searches"] }),
  });
}

/* --------------------------------------------------------------- feeds */

/**
 * Syndication feeds (docs/FEEDS.md). Every one of these carries `rss_url`/`atom_url`,
 * which contain the feed's token and are therefore secrets: show them behind a copy
 * button, never in a shareable link.
 */
export function useFeeds() {
  return useQuery<SyndicationFeed[]>({
    queryKey: ["feeds"],
    queryFn: ({ signal }) => apiGet<SyndicationFeed[]>("/feeds", undefined, signal),
  });
}

function useFeedMutation<TInput, TResult>(fn: (input: TInput) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["feeds"] }),
  });
}

export function useCreateFeed() {
  return useFeedMutation((input: SyndicationFeedInput) => apiPost<SyndicationFeed>("/feeds", input));
}

export function useUpdateFeed() {
  return useFeedMutation(({ id, patch }: { id: number; patch: SyndicationFeedPatch }) =>
    apiPatch<SyndicationFeed>(`/feeds/${id}`, patch),
  );
}

/** Mints a new token. Every copy of the old URL stops working immediately. */
export function useRotateFeed() {
  return useFeedMutation((id: number) => apiPost<SyndicationFeed>(`/feeds/${id}/rotate`));
}

/**
 * The built-in feeds. They need no account and no write scope, so this runs for a guest
 * too — and the list is derived from the directory, so it changes only when a talkgroup
 * changes category. Kept fresh for an hour rather than per mount.
 */
export function useFeedPresets() {
  return useQuery<FeedPreset[]>({
    queryKey: ["feed-presets"],
    queryFn: ({ signal }) => apiGet<FeedPreset[]>("/feeds/presets", undefined, signal),
    staleTime: 60 * 60 * 1000,
  });
}

export function useDeleteFeed() {
  return useFeedMutation((id: number) => apiDelete<null>(`/feeds/${id}`));
}

/* -------------------------------------------------------------- directory */

export interface TalkgroupsParams {
  q?: string;
  category?: string[];
  /** RadioReference's own spelling; the API 422s an unknown one. */
  service_tag?: string[];
  agency?: string[];
  system_id?: number;
  /**
   * API sort key, `-` for descending. Every column the directory table has a
   * header for: `tgid`, `alias`, `category`, `service_tag`, `agency_code`,
   * `priority`, `calls_24h`, `call_count`, `last_seen_at`, `record`,
   * `transcribe`, `is_hidden`, `shared` and the three `export_*` flags.
   */
  sort?: string;
  /**
   * The API defaults this to `true`. The directory passes `false` unless the
   * reader asks for hidden channels, which is what "hidden" is supposed to mean.
   */
  include_hidden?: boolean;
  /**
   * The policy and export filters, all three-valued: leaving one out is "either",
   * and `false` is a question with an answer ("what is *not* transcribed") rather
   * than the absence of the filter.
   */
  shared?: boolean;
  record?: boolean;
  transcribe?: boolean;
  export_broadcastify?: boolean;
  export_bcfy_calls?: boolean;
  export_openmhz?: boolean;
  page?: number;
  page_size?: number;
}

export function useTalkgroups(params: TalkgroupsParams = {}) {
  return useQuery<Paginated<Talkgroup>>({
    queryKey: [...KEYS.talkgroups, "list", params as QueryParams],
    queryFn: ({ signal }) => apiGet<Paginated<Talkgroup>>("/talkgroups", toQueryParams(params), signal),
    // The page number changes under the reader's cursor; keeping the previous
    // page on screen while the next one loads is what stops the table flashing.
    placeholderData: keepPreviousData,
    staleTime: 300_000,
  });
}

export function useTalkgroup(id: number | null) {
  return useQuery<TalkgroupDetail>({
    queryKey: [...KEYS.talkgroups, id],
    queryFn: ({ signal }) => apiGet<TalkgroupDetail>(`/talkgroups/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

export function useTalkgroupStats(id: number | null) {
  return useQuery<TalkgroupStatsResponse>({
    queryKey: [...KEYS.talkgroups, id, "stats"],
    queryFn: ({ signal }) => apiGet<TalkgroupStatsResponse>(`/talkgroups/${id}/stats`, undefined, signal),
    enabled: id !== null,
  });
}

export function useUpdateTalkgroup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: TalkgroupPatch }) =>
      apiPatch<Talkgroup>(`/talkgroups/${id}`, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.talkgroups });
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

/**
 * One patch, many talkgroups.
 *
 * The same invalidations as `useUpdateTalkgroup`, for the same reason: the alias and
 * the color a talkgroup carries are rendered into every row of the calls list, so a
 * directory edit that only refreshed the directory would leave the page the operator
 * actually watches showing yesterday's names.
 */
export function useBulkUpdateTalkgroups() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, changes }: { ids: number[]; changes: TalkgroupPatch }) =>
      apiPost<TalkgroupBulkResult>("/talkgroups/bulk", { ids, changes }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.talkgroups });
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

export function useImportPlaylist(kind: "talkgroups" | "radios" | "units" | "personnel") {
  const queryClient = useQueryClient();
  return useMutation({
    // The API takes the file as multipart *or* this JSON body; the browser already
    // has the text, so posting it avoids a second read as a Blob.
    mutationFn: (input: { content: string; format: "xml" | "csv" }) =>
      apiPost<ImportResult>(`/${kind}/import`, input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [kind] }),
  });
}

export interface RadiosParams {
  q?: string;
  /** API sort key, e.g. `-call_count`, `radio_id`, `unit_id`, `last_seen_at`. */
  sort?: string;
  /** `transmitted` = keyed up; `seen` = present but silent; omit for all. */
  activity?: "transmitted" | "seen";
  page?: number;
  page_size?: number;
}

export function useRadios(params: RadiosParams = {}) {
  return useQuery<Paginated<Radio>>({
    queryKey: [...KEYS.radios, "list", params as QueryParams],
    queryFn: ({ signal }) => apiGet<Paginated<Radio>>("/radios", toQueryParams(params), signal),
    placeholderData: keepPreviousData,
    staleTime: 300_000,
  });
}

export function useRadio(id: number | null) {
  return useQuery<RadioDetail>({
    queryKey: [...KEYS.radios, id],
    queryFn: ({ signal }) => apiGet<RadioDetail>(`/radios/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

export function useRadioAffiliations(id: number | null) {
  return useQuery<RadioAffiliation[]>({
    queryKey: [...KEYS.radios, id, "affiliations"],
    queryFn: ({ signal }) => apiGet<RadioAffiliation[]>(`/radios/${id}/affiliations`, undefined, signal),
    enabled: id !== null,
  });
}

export function useRadioHistory(id: number | null) {
  return useQuery<RadioHistory>({
    queryKey: [...KEYS.radios, id, "history"],
    queryFn: ({ signal }) => apiGet<RadioHistory>(`/radios/${id}/history`, undefined, signal),
    enabled: id !== null,
  });
}

export function useUpdateRadio() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: RadioPatch }) => apiPatch<Radio>(`/radios/${id}`, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.radios }),
  });
}

/** One patch, many radios. `alias` and `unit_id` are refused by the endpoint. */
export function useBulkUpdateRadios() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, changes }: { ids: number[]; changes: RadioPatch }) =>
      apiPost<RadioBulkResult>("/radios/bulk", { ids, changes }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.radios });
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

/* ----------------------------------------------------------------- roster */

export interface RosterParams {
  q?: string;
  sort?: string;
  page?: number;
  page_size?: number;
}

/** The roster needs a credential (`GET /units` is 401 to a guest), hence `enabled`. */
export function useUnits(params: RosterParams = {}, enabled = true) {
  return useQuery<Paginated<Unit>>({
    queryKey: [...KEYS.units, "list", params as QueryParams],
    queryFn: ({ signal }) => apiGet<Paginated<Unit>>("/units", toQueryParams(params), signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

export function useUnit(id: number | null) {
  return useQuery<UnitDetail>({
    queryKey: [...KEYS.units, id],
    queryFn: ({ signal }) => apiGet<UnitDetail>(`/units/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

export function useCreateUnit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: UnitInput) => apiPost<Unit>("/units", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.units }),
  });
}

export function useUpdateUnit() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: UnitPatch }) => apiPatch<Unit>(`/units/${id}`, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.units });
      // A renamed callsign is written onto the radios assigned to it.
      void queryClient.invalidateQueries({ queryKey: KEYS.radios });
    },
  });
}

/** One patch, many units. `callsign` is refused by the endpoint: it is the unique key. */
export function useBulkUpdateUnits() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, changes }: { ids: number[]; changes: UnitPatch }) =>
      apiPost<UnitBulkResult>("/units/bulk", { ids, changes }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.units });
      void queryClient.invalidateQueries({ queryKey: KEYS.radios });
    },
  });
}

/** As `useUnits`: the personnel list is not public, so a guest's screen must not ask. */
export function usePersonnel(params: RosterParams & { active?: boolean } = {}, enabled = true) {
  return useQuery<Paginated<Person>>({
    queryKey: [...KEYS.personnel, "list", params as QueryParams],
    queryFn: ({ signal }) => apiGet<Paginated<Person>>("/personnel", toQueryParams(params), signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

export function usePerson(id: number | null) {
  return useQuery<PersonDetail>({
    queryKey: [...KEYS.personnel, id],
    queryFn: ({ signal }) => apiGet<PersonDetail>(`/personnel/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

export function useCreatePerson() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PersonInput) => apiPost<Person>("/personnel", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.personnel }),
  });
}

export function useUpdatePerson() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: PersonPatch }) =>
      apiPatch<Person>(`/personnel/${id}`, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.personnel }),
  });
}

/** One patch, many people — how a transfer or a shift change is actually entered. */
export function useBulkUpdatePersonnel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, changes }: { ids: number[]; changes: PersonPatch }) =>
      apiPost<PersonBulkResult>("/personnel/bulk", { ids, changes }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.personnel }),
  });
}

export function useRadioAssignments(id: number | null) {
  return useQuery<RadioAssignment[]>({
    queryKey: [...KEYS.radios, id, "assignments"],
    queryFn: ({ signal }) => apiGet<RadioAssignment[]>(`/radios/${id}/assignments`, undefined, signal),
    enabled: id !== null,
  });
}

/**
 * Assigning touches three areas: the radio (its label and resolved identity), the
 * roster (radio counts) and the call lists that render the label — so all three are
 * invalidated rather than the assignment list alone.
 */
function useAssignmentInvalidation() {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: KEYS.radios });
    void queryClient.invalidateQueries({ queryKey: KEYS.units });
    void queryClient.invalidateQueries({ queryKey: KEYS.personnel });
    void queryClient.invalidateQueries({ queryKey: KEYS.calls });
  };
}

export function useAssignRadio(radioId: number) {
  const invalidate = useAssignmentInvalidation();
  return useMutation({
    mutationFn: (input: RadioAssignmentInput) =>
      apiPost<RadioAssignment>(`/radios/${radioId}/assignments`, input),
    onSuccess: invalidate,
  });
}

export function useEndAssignment(radioId: number) {
  const invalidate = useAssignmentInvalidation();
  return useMutation({
    mutationFn: (kind: "unit" | "person") =>
      apiDelete<RadioAssignment>(`/radios/${radioId}/assignments/current`, { kind }),
    onSuccess: invalidate,
  });
}

/**
 * Systems and sites go stale like every other list.
 *
 * Both held their answer for ever until a recorder proved they should not: a
 * site is created by the *pipeline*, not by this app — the first call or
 * observation from a tower keyup has never heard of makes one — so a list
 * pinned at `Infinity` shows an operator an archive that stopped growing the
 * moment they opened the tab, and the Sites page is exactly where somebody
 * watches a new site appear. `LIST_STALE` is the same 30 seconds every other
 * directory list uses; the explicit invalidations below stay, because a rename
 * must land immediately rather than within half a minute.
 */
export function useSystems(enabled = true) {
  return useQuery<SystemInfo[]>({
    queryKey: ["systems"],
    queryFn: ({ signal }) => apiGet<SystemInfo[]>("/systems", undefined, signal),
    staleTime: LIST_STALE,
    enabled,
  });
}

/**
 * The site list is the one directory list that is *expensive*: every row carries a call
 * count and a "what would block deleting this" answer, both counted across the whole
 * archive. It is therefore held for a minute rather than the usual thirty seconds — a
 * mount inside that window is served from cache and costs the API nothing. The explicit
 * invalidations in `invalidateSites` still land a rename immediately, which is what the
 * shorter window was ever for.
 */
const SITES_STALE = 60_000;

export function useSites() {
  return useQuery<SiteInfo[]>({
    queryKey: ["sites"],
    queryFn: ({ signal }) => apiGet<SiteInfo[]>("/sites", undefined, signal),
    staleTime: SITES_STALE,
  });
}

/**
 * Everything a site or system edit has to refresh. The staleness above would
 * get there on its own within half a minute; this is what makes a rename land
 * on the row the moment the dialog closes. The rest are the places a site or
 * system name is *shown* — calls carry a denormalized `site_name`, the audit
 * summary and the occupancy strips are keyed by site, and a tuner row looks its
 * focus site up in the same list.
 */
function invalidateSites(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: ["sites"] });
  void queryClient.invalidateQueries({ queryKey: ["systems"] });
  void queryClient.invalidateQueries({ queryKey: KEYS.calls });
  void queryClient.invalidateQueries({ queryKey: KEYS.occupancy });
  void queryClient.invalidateQueries({ queryKey: KEYS.recorders });
  void queryClient.invalidateQueries({ queryKey: KEYS.activity });
}

export function useUpdateSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: SystemPatch }) =>
      apiPatch<SystemInfo>(`/systems/${id}`, patch),
    onSuccess: () => invalidateSites(queryClient),
  });
}

export function useDeleteSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<void>(`/systems/${id}`),
    onSuccess: () => invalidateSites(queryClient),
  });
}

export function useMergeSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, into }: { id: number; into: number }) =>
      apiPost<SystemMergeResult>(`/systems/${id}/merge-into/${into}`),
    onSuccess: () => invalidateSites(queryClient),
  });
}

/**
 * The RadioReference mapping for a system, which is a different endpoint from
 * the rest of the system's fields because it is an admin escape hatch rather
 * than an edit: it overrides the `RR_SYSTEMS` environment mapping and decides
 * what the nightly sync pulls. A site's mapping rides along on `PATCH /sites`
 * instead, since there it is one field among several the operator owns.
 */
export function useSetSystemRr() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, rrId }: { id: number; rrId: number | null }) =>
      apiPut<SystemInfo>(`/admin/systems/${id}/radioreference`, { rr_id: rrId }),
    onSuccess: () => invalidateSites(queryClient),
  });
}

export function useUpdateSite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: SitePatch }) =>
      apiPatch<SiteInfo>(`/sites/${id}`, patch),
    onSuccess: () => invalidateSites(queryClient),
  });
}

export function useDeleteSite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<void>(`/sites/${id}`),
    onSuccess: () => invalidateSites(queryClient),
  });
}

export function useMergeSite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, into }: { id: number; into: number }) =>
      apiPost<SiteMergeResult>(`/sites/${id}/merge-into/${into}`),
    onSuccess: () => invalidateSites(queryClient),
  });
}

/**
 * One site's channel audit. Computed by the API on every request — it is a
 * comparison between the air, RadioReference and the recorder's configuration,
 * and a cached comparison is wrong the moment either side moves — so this is
 * refetched rather than held for ever like the site list.
 */
export function useSiteChannelAudit(siteId: number | null) {
  return useQuery<SiteChannelAudit>({
    queryKey: ["sites", siteId, "channel-audit"],
    queryFn: ({ signal }) =>
      apiGet<SiteChannelAudit>(`/directory/sites/${siteId}/channel-audit`, undefined, signal),
    enabled: siteId !== null,
    staleTime: 60_000,
  });
}

/**
 * The frequencies catalogued at one site, with their two export checkboxes.
 * Only fetched when a site is open — the Sites page shows one at a time.
 */
export function useSiteFrequencies(siteId: number | null) {
  return useQuery<SiteFrequency[]>({
    queryKey: ["sites", siteId, "frequencies"],
    queryFn: ({ signal }) => apiGet<SiteFrequency[]>(`/sites/${siteId}/frequencies`, undefined, signal),
    enabled: siteId !== null,
    staleTime: LIST_STALE,
  });
}

/**
 * Everything known about one site, fetched only when its row is expanded.
 *
 * Deliberately not folded into `useSites`: the profile is a fold of the stored
 * observation windows and nobody wants N of them to render a table. The Sites
 * card shows the services column the list route already carries, and this is
 * what the chevron opens.
 */
export function useSiteProfile(siteId: number | null) {
  return useQuery<SiteProfile>({
    queryKey: ["sites", siteId, "profile"],
    queryFn: ({ signal }) => apiGet<SiteProfile>(`/sites/${siteId}/profile`, undefined, signal),
    enabled: siteId !== null,
    staleTime: LIST_STALE,
  });
}

/** Tick or clear one frequency's export checkbox. */
export function useSaveSiteFrequency(siteId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: SiteFrequencyPatch }) =>
      apiPatch<SiteFrequency>(`/site-frequencies/${id}`, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sites", siteId, "frequencies"] }),
  });
}

/**
 * Every `mfid:opcode` one site's control channel has ever transmitted.
 *
 * Not the same thing as the message *census* (`/message-mix`), which is a rolling
 * histogram: this is the archive's memory, and it is what says a kind is new.
 */
export function useSiteMessageKinds(siteId: number | null) {
  return useQuery<SiteMessageKind[]>({
    queryKey: ["sites", siteId, "message-kinds"],
    queryFn: ({ signal }) => apiGet<SiteMessageKind[]>(`/sites/${siteId}/message-kinds`, undefined, signal),
    enabled: siteId !== null,
    staleTime: LIST_STALE,
  });
}

/** Name a message kind — admin only; a named kind stops being news. */
export function useSaveSiteMessageKind(siteId: number | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ mfid, opcode, patch }: { mfid: number; opcode: number; patch: SiteMessageKindPatch }) =>
      apiPatch<SiteMessageKind>(`/admin/sites/${siteId}/message-kinds/${mfid}/${opcode}`, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sites", siteId, "message-kinds"] }),
  });
}

/** Per-site counts for a whole system, for the summary table above the panel. */
export function useSystemChannelAudit(systemId: number | null) {
  return useQuery<SystemChannelAudit>({
    queryKey: ["systems", systemId, "channel-audit"],
    queryFn: ({ signal }) =>
      apiGet<SystemChannelAudit>(`/directory/systems/${systemId}/channel-audit`, undefined, signal),
    enabled: systemId !== null,
    staleTime: 60_000,
  });
}

export function usePatches() {
  return useQuery<Paginated<Patch>>({
    queryKey: ["patches"],
    queryFn: ({ signal }) => apiGet<Paginated<Patch>>("/patches", { page_size: 200 }, signal),
    staleTime: 300_000,
  });
}

export function useAgencies() {
  return useQuery<Agency[]>({
    queryKey: ["agencies"],
    queryFn: ({ signal }) => apiGet<Agency[]>("/agencies", undefined, signal),
    // Not `Infinity` any more: the Admin page can add an agency, and the picker
    // in a dialog somebody left open would otherwise never learn about it.
    staleTime: 60_000,
  });
}

/**
 * A function from agency code to display name, on the shared `["agencies"]` cache.
 *
 * Codes are identifiers (`XX0010000`, `STATE_UNIVERSITY`); every page that names an agency
 * shows the directory row's name instead. Until the list arrives the code is tidied
 * (`agencyLabel`'s fallback), so nothing waits on it.
 */
export function useAgencyLabel(): (code: string | null | undefined) => string {
  const agencies = useAgencies();
  const byCode = useMemo(
    () => new Map((agencies.data ?? []).map((agency) => [agency.code, agency])),
    [agencies.data],
  );
  return useCallback(
    (code: string | null | undefined) => agencyLabel(code, code ? byCode.get(code) : null),
    [byCode],
  );
}

/**
 * What every category, service tag and agency kind means.
 *
 * Static on the server — it reads no database — so it is fetched once and kept.
 * The Help page renders it rather than repeating the descriptions in
 * `help/help.ts`: a category added to the backend must not need a frontend
 * release before the app can say what it is for.
 */
export function useVocabulary() {
  return useQuery<Vocabulary>({
    queryKey: ["directory", "vocabulary"],
    queryFn: ({ signal }) => apiGet<Vocabulary>("/directory/vocabulary", undefined, signal),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/** The agency table with the counts a delete checks. Admin scope; the Agencies tab. */
export function useAdminAgencies() {
  return useQuery<AdminAgency[]>({
    queryKey: [...KEYS.admin, "agencies"],
    queryFn: ({ signal }) => apiGet<AdminAgency[]>("/admin/agencies", undefined, signal),
  });
}

/**
 * The three agency mutations, sharing one invalidation: every one of them can
 * change what the pickers show, so `["agencies"]` is refreshed alongside the
 * admin list rather than left to go stale behind an open dialog.
 */
function useAgencyMutation<TArgs>(run: (args: TArgs) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "agencies"] });
      queryClient.invalidateQueries({ queryKey: ["agencies"] });
    },
  });
}

export function useCreateAgency() {
  return useAgencyMutation((body: AgencyIn) => apiPost<AdminAgency>("/admin/agencies", body));
}

export function useUpdateAgency() {
  return useAgencyMutation(({ code, patch }: { code: string; patch: AgencyPatch }) =>
    apiPatch<AdminAgency>(`/admin/agencies/${encodeURIComponent(code)}`, patch),
  );
}

export function useDeleteAgency() {
  return useAgencyMutation((code: string) => apiDelete<null>(`/admin/agencies/${encodeURIComponent(code)}`));
}

/** `GET /conversations` — `tgid` is the on-air number, as on `/calls`; never `talkgroups.id`. */
export function useConversations(tgid?: number) {
  return useQuery<Paginated<Conversation>>({
    queryKey: ["conversations", tgid ?? null],
    queryFn: ({ signal }) =>
      apiGet<Paginated<Conversation>>(
        "/conversations",
        tgid !== undefined ? { talkgroup: tgid } : undefined,
        signal,
      ),
  });
}

/**
 * One conversation and its calls. `CallDetail.conversation` is only the
 * conversation *record*; the thread itself comes from here, which is also what
 * the detail page uses as the player's queue.
 */
export function useConversation(id: number | null) {
  return useQuery<ConversationThread>({
    queryKey: ["conversations", "thread", id],
    queryFn: ({ signal }) => apiGet<ConversationThread>(`/conversations/${id}`, undefined, signal),
    enabled: id !== null,
  });
}

/* ------------------------------------------------------------------ stats */

export function useStatsOverview() {
  return useQuery<StatsOverview>({
    queryKey: [...KEYS.stats, "overview"],
    queryFn: ({ signal }) => apiGet<StatsOverview>("/stats/overview", undefined, signal),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

export function useActivity(groupBy: "day" | "talkgroup", filters: CallFilterParams = {}) {
  const params = { ...toQueryParams(filters), group_by: groupBy };
  return useQuery<ActivityResponse>({
    queryKey: [...KEYS.stats, "activity", params],
    queryFn: ({ signal }) => apiGet<ActivityResponse>("/stats/activity", params, signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
  });
}

export interface ActivityLogFilters {
  source?: string;
  level?: string;
  limit?: number;
  before_id?: number;
}

/** A page of the operational activity log (Admin → Activity). Admin only. */
export function useActivityLog(filters: ActivityLogFilters = {}, enabled = true) {
  const params = toQueryParams(filters);
  return useQuery<ActivityEvent[]>({
    queryKey: [...KEYS.activity, params],
    queryFn: ({ signal }) => apiGet<ActivityEvent[]>("/activity", params, signal),
    placeholderData: keepPreviousData,
    staleTime: LIST_STALE,
    enabled,
  });
}

/**
 * `limit` is how many of the busiest the server ranks (its default is 100, its cap 500). A
 * directory page showing 200 rows asks for more, or every row past the hundredth shows
 * no 24-hour trend at all.
 */
export function useTalkgroupStatsList(filters: CallFilterParams = {}, limit?: number) {
  const params = { ...toQueryParams(filters), ...(limit === undefined ? {} : { limit }) };
  return useQuery<TalkgroupStat[]>({
    queryKey: [...KEYS.stats, "talkgroups", params],
    queryFn: ({ signal }) => apiGet<TalkgroupStat[]>("/stats/talkgroups", params, signal),
    staleTime: LIST_STALE,
  });
}

export function useRadioStatsList(filters: CallFilterParams = {}, limit?: number) {
  const params = { ...toQueryParams(filters), ...(limit === undefined ? {} : { limit }) };
  return useQuery<RadioStat[]>({
    queryKey: [...KEYS.stats, "radios", params],
    queryFn: ({ signal }) => apiGet<RadioStat[]>("/stats/radios", params, signal),
    staleTime: LIST_STALE,
  });
}

export function useTranscriptionStats(enabled = true) {
  return useQuery<TranscriptionStats>({
    queryKey: [...KEYS.stats, "transcription"],
    queryFn: ({ signal }) => apiGet<TranscriptionStats>("/stats/transcription", undefined, signal),
    staleTime: 30_000,
    enabled,
  });
}

export function useGaps(enabled = true) {
  return useQuery<RecorderGap[]>({
    queryKey: [...KEYS.stats, "gaps"],
    queryFn: ({ signal }) => apiGet<RecorderGap[]>("/stats/gaps", undefined, signal),
    staleTime: 300_000,
    enabled,
  });
}

export function useTimeline(from: string | null, to: string | null) {
  return useQuery<TimelinePoint[]>({
    queryKey: ["timeline", from, to],
    queryFn: ({ signal }) => apiGet<TimelinePoint[]>("/timeline", { from, to }, signal),
    enabled: from !== null && to !== null,
  });
}

/* ------------------------------------------------------------- operations */

/**
 * The Live activity panel: one request for a window of counters, the queue depths and
 * the recorder heartbeats. Polled rather than streamed — the numbers are aggregates and
 * a five-second tick is both cheap and as fresh as anyone can read — and `enabled` is
 * how the panel stops polling when the tab is hidden.
 */
export function useLiveActivity(windowSeconds: number, enabled = true) {
  return useQuery<LiveActivitySnapshot>({
    queryKey: ["status", "activity", windowSeconds],
    queryFn: ({ signal }) =>
      apiGet<LiveActivitySnapshot>("/status/activity", { window: windowSeconds }, signal),
    refetchInterval: enabled ? 5_000 : false,
    refetchIntervalInBackground: false,
    enabled,
    staleTime: 2_000,
  });
}

/**
 * `GET /status/lag` — the header badge on every page. One indexed `max(recorded_at)`;
 * the badge used to poll `/stats/overview`, a dozen aggregates, for this one number.
 */
export function useIngestLag() {
  return useQuery<IngestLag>({
    queryKey: ["status", "lag"],
    queryFn: ({ signal }) => apiGet<IngestLag>("/status/lag", undefined, signal),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
}

export function useSystemStatus() {
  return useQuery<SystemStatus>({
    queryKey: ["status"],
    queryFn: ({ signal }) => apiGet<SystemStatus>("/status", undefined, signal),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function useAdminIngest() {
  return useQuery<AdminIngestResponse>({
    queryKey: [...KEYS.admin, "ingest"],
    queryFn: ({ signal }) => apiGet<AdminIngestResponse>("/admin/ingest", undefined, signal),
    refetchInterval: 30_000,
  });
}

export function useIngestFiles(status: string) {
  return useQuery<Paginated<IngestFile>>({
    queryKey: [...KEYS.admin, "ingest", "files", status],
    queryFn: ({ signal }) =>
      apiGet<Paginated<IngestFile>>("/admin/ingest/files", { status, page_size: 100 }, signal),
  });
}

export function useRescan() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<{ queued: boolean }>("/admin/ingest/rescan"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.admin }),
  });
}

export function useRetryIngestFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiPost<IngestFile>(`/admin/ingest/files/${id}/retry`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.admin }),
  });
}

export function useJobs(status: string[]) {
  return useQuery<Paginated<TranscriptionJob>>({
    queryKey: [...KEYS.admin, "jobs", status],
    queryFn: ({ signal }) =>
      apiGet<Paginated<TranscriptionJob>>("/admin/jobs", { status, page_size: 100 }, signal),
    refetchInterval: 15_000,
  });
}

export function useRequeueJobs() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobIds: number[]) =>
      apiPost<{ requeued: number }>("/admin/jobs/requeue", { job_ids: jobIds }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.admin }),
  });
}

export function useBackends() {
  return useQuery<SttBackend[]>({
    queryKey: [...KEYS.admin, "backends"],
    queryFn: ({ signal }) => apiGet<SttBackend[]>("/admin/backends", undefined, signal),
    refetchInterval: 60_000,
  });
}

/**
 * The backends page: the worker's published counters and the queue, in one response.
 *
 * Polled every five seconds because it is a *live* view of work in progress — in-flight
 * attempts and a queue draining — and the tab only mounts when the operator is looking at
 * it, so nothing polls while it is not on screen.
 */
/* -------------------------------------------------- speech-to-text backend CRUD */

/**
 * Add or edit a backend.
 *
 * One hook for both, because the dialog is one dialog: the only difference is whether the
 * name already exists, and making the caller pick between two hooks would put that decision
 * in two places. Not optimistic — unlike the on/off switch, a save can be rejected (a name
 * that is taken, a kind that needs a URL) and pretending otherwise would show the operator a
 * row that is about to disappear.
 */
export function useSaveBackend() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, input }: { name?: string; input: SttBackendConfigInput }) =>
      name
        ? apiPut<SttBackendStat>(`/admin/stt/backends/${encodeURIComponent(name)}`, input)
        : apiPost<SttBackendStat>("/admin/stt/backends", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "stt-backends"] }),
  });
}

export function useDeleteBackend() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      apiDelete<SttBackendDeleteResult>(`/admin/stt/backends/${encodeURIComponent(name)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "stt-backends"] }),
  });
}

/**
 * Ask a backend, right now, whether it works — and time it on a real clip.
 *
 * Deliberately not cached: the answer is about this second, and a stale "healthy" is exactly
 * the lie the whole backends page was rebuilt to stop telling.
 */
export function useProbeBackend() {
  return useMutation({
    mutationFn: (name: string) =>
      apiPost<SttBackendProbe>(`/admin/stt/backends/${encodeURIComponent(name)}/probe`),
  });
}

/* ------------------------------------------------------------- ingest sources */

export function useIngestSources(enabled = true) {
  return useQuery<IngestSourcesView>({
    queryKey: [...KEYS.admin, "ingest-sources"],
    queryFn: ({ signal }) => apiGet<IngestSourcesView>("/admin/ingest/sources", undefined, signal),
    enabled,
  });
}

/** Add or edit a source; `id` decides which. See `useSaveBackend` for why it is one hook. */
export function useSaveIngestSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id?: number; input: IngestSourceInput }) =>
      id
        ? apiPut<IngestSourceRow>(`/admin/ingest/sources/${id}`, input)
        : apiPost<IngestSourceRow>("/admin/ingest/sources", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "ingest-sources"] }),
  });
}

export function useDeleteIngestSource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<null>(`/admin/ingest/sources/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "ingest-sources"] }),
  });
}

export function useSttBackends(enabled = true) {
  return useQuery<SttBackendsView>({
    queryKey: [...KEYS.admin, "stt-backends"],
    queryFn: ({ signal }) => apiGet<SttBackendsView>("/admin/stt/backends", undefined, signal),
    enabled,
    refetchInterval: 5_000,
  });
}

/**
 * Turn one backend on or off at runtime; `null` clears the override and follows the config.
 *
 * Optimistic, because the switch is a switch: it has to move under the finger. The worker
 * picks the change up within a few seconds, so the refetch that follows is what confirms it.
 */
export function useSetBackendEnabled() {
  const queryClient = useQueryClient();
  const key = [...KEYS.admin, "stt-backends"];
  return useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean | null }) =>
      apiPut<SttBackendStat>(`/admin/stt/backends/${encodeURIComponent(name)}/enabled`, {
        enabled,
      }),
    onMutate: async ({ name, enabled }: { name: string; enabled: boolean | null }) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<SttBackendsView>(key);
      if (previous) {
        queryClient.setQueryData<SttBackendsView>(key, {
          ...previous,
          backends: previous.backends.map((backend) =>
            backend.name === name
              ? {
                  ...backend,
                  admin_enabled: enabled,
                  enabled: enabled === null ? backend.config_enabled : enabled,
                }
              : backend,
          ),
        });
      }
      return { previous };
    },
    onError: (_error, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: KEYS.admin }),
  });
}

/* ------------------------------------------------ recorders & tuners (§4.8) */

export function useRecorders(enabled = true) {
  return useQuery<Recorder[]>({
    queryKey: KEYS.recorders,
    queryFn: ({ signal }) => apiGet<Recorder[]>("/admin/recorders", undefined, signal),
    enabled,
    refetchInterval: 30_000,
  });
}

/**
 * The recorder's own policy — standby class and preference — rather than a tuner's desired
 * state. Separate mutation because it is a different endpoint answering a different
 * question: what keyup should do *about* this recorder, not what the recorder should do
 * about a tuner.
 */
export function useSaveRecorderPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ recorderId, policy }: { recorderId: number; policy: RecorderPolicyInput }) =>
      apiPut(`/admin/recorders/${recorderId}`, policy),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.recorders }),
  });
}

/**
 * The recorder's shared link key in clear — the one read that returns it.
 *
 * Off by default and fetched only when the operator presses "reveal", which is the whole
 * point of it being a route of its own: every list and detail of a recorder carries
 * `key_set` and `key_fingerprint` instead, so the secret is never in a payload a page
 * merely happened to load. `staleTime: 0` and no caching across dialogs, because a key that
 * lingers in the query cache is a key on screen after the dialog that asked for it closed.
 */
export function useRecorderLinkKey(recorderId: number | null, enabled: boolean) {
  return useQuery<RecorderLinkKey>({
    queryKey: ["recorder-link-key", recorderId],
    queryFn: ({ signal }) =>
      apiGet<RecorderLinkKey>(`/admin/recorders/${recorderId}/link/key`, undefined, signal),
    enabled: enabled && recorderId !== null,
    staleTime: 0,
    gcTime: 0,
  });
}

/**
 * A fresh 32-byte key for the link, which nothing has stored yet.
 *
 * Generating is not saving: the operator pastes the same key into every recorder in the
 * mesh and each becomes a member when its own settings are saved. So this invalidates
 * nothing — there is nothing yet to invalidate.
 */
export function useGenerateRecorderLinkKey() {
  return useMutation({
    mutationFn: ({ recorderId }: { recorderId: number }) =>
      apiPost<{ key: string }>(`/admin/recorders/${recorderId}/link/generate-key`),
  });
}

/**
 * Forget a recorder that has been retired. Nothing it recorded is touched —
 * calls carry a recorder's name, not a foreign key onto the row — so this loses
 * the label and standby policy and nothing else. `force` is what gets past the
 * API's refusal to forget a recorder still reporting, which would otherwise come
 * straight back on its next poll.
 */
export function useForgetRecorder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, force }: { id: number; force?: boolean }) =>
      apiDelete<void>(`/admin/recorders/${id}`, force ? { force: "true" } : undefined),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.recorders }),
  });
}

export function useSaveTunerDesired() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      recorderId,
      tunerKey,
      desired,
    }: {
      recorderId: number;
      tunerKey: string;
      desired: TunerDesiredInput;
      // Typed: the answer is the tuner as it now stands, and the edit dialog reads
      // `rejected` off it to say which of the fields just written the recorder refused.
    }) => apiPut<Tuner>(`/admin/recorders/${recorderId}/tuners/${encodeURIComponent(tunerKey)}`, desired),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.recorders }),
  });
}

/**
 * The days of raw event log a recorder has shipped here, newest first.
 *
 * Read per recorder (`instance`) rather than all at once: the list lives on a recorder's
 * card, and a deployment with several recorders would otherwise render each one's card from
 * a list it had to filter itself.
 */
export function useEventLogs(instance?: string | null, enabled = true) {
  return useQuery<EventLogArchive[]>({
    queryKey: [...KEYS.eventLogs, instance ?? "all"],
    queryFn: ({ signal }) =>
      apiGet<EventLogArchive[]>("/admin/event-logs", instance ? { instance } : undefined, signal),
    enabled,
    staleTime: LIST_STALE,
  });
}

/**
 * Where the browser fetches one archived day from.
 *
 * A plain `href`, not a `fetch`: the file is tens of megabytes of gzip and the browser
 * downloads it far better than any JavaScript could — with a progress bar, resumable, and
 * without holding it in memory first. The session cookie rides along as it does for the
 * call audio.
 */
export function eventLogDownloadUrl(id: number): string {
  return apiUrl(`/admin/event-logs/${id}/download`);
}

/** Per-site band occupancy, folded from the latest observations. `site` filters to one. */
export function useOccupancy(site?: number | null, enabled = true) {
  return useQuery<SiteOccupancy[]>({
    queryKey: [...KEYS.occupancy, site ?? "all"],
    queryFn: ({ signal }) => apiGet<SiteOccupancy[]>("/admin/occupancy", site ? { site } : undefined, signal),
    enabled,
    staleTime: LIST_STALE,
    // Refetch periodically so BandStrip's "active now" coloring keeps moving rather than
    // freezing at whatever recency it had when the page first loaded.
    refetchInterval: 30_000,
  });
}

export function useSettings() {
  return useQuery<SettingDefinition[]>({
    queryKey: [...KEYS.admin, "settings"],
    queryFn: ({ signal }) => apiGet<SettingDefinition[]>("/admin/settings", undefined, signal),
  });
}

export function useSaveSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, string>) => apiPut<SettingDefinition[]>("/admin/settings", values),
    onSuccess: (data) => queryClient.setQueryData([...KEYS.admin, "settings"], data),
  });
}

/** `GET /admin/audio` — the processing queue, coverage, today's tones (§5.5). */
export function useAudioStatus() {
  return useQuery<AudioStatus>({
    queryKey: [...KEYS.admin, "audio"],
    queryFn: ({ signal }) => apiGet<AudioStatus>("/admin/audio", undefined, signal),
    // The coverage counts are full scans of `calls`, cached 60 s on the server; polling
    // faster than that would only re-read the cache. Queuing a backfill refreshes it.
    refetchInterval: 60_000,
  });
}

/** `POST /admin/audio/reprocess` — queue a backfill. */
export function useAudioReprocess() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AudioReprocessInput) => apiPost<AudioReprocessResult>("/admin/audio/reprocess", body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "audio"] }),
  });
}

/** `POST /admin/audio/preview` — run the chain on one call without storing anything. */
export function useAudioPreview() {
  return useMutation({
    mutationFn: (callId: number) => apiPost<AudioPreview>("/admin/audio/preview", { call_id: callId }),
  });
}

/* ------------------------------------------------------- audio profiles */

/**
 * The named chains talkgroups can be processed through (docs/AUDIO.md).
 *
 * One list for the whole feature: the admin card lists it, the talkgroup dialogs turn it
 * into a select, and every mutation below invalidates the Audio tab's key, which this one
 * sits under — so a rename shows up on the channel that carries the profile without any
 * of those places knowing about the others.
 */
export function useAudioProfiles(enabled = true) {
  return useQuery<AudioProfile[]>({
    queryKey: KEYS.audioProfiles,
    queryFn: ({ signal }) => apiGet<AudioProfile[]>("/admin/audio/profiles", undefined, signal),
    enabled,
    staleTime: LIST_STALE,
  });
}

/**
 * The step whitelist — every kind, its parameters and their ranges.
 *
 * Fetched rather than written out in TypeScript, and cached hard: it changes when the
 * server is deployed and at no other moment, and the editor's whole form is built from it.
 */
export function useAudioStepCatalog(enabled = true) {
  return useQuery<AudioProfileCatalog>({
    queryKey: [...KEYS.audioProfiles, "steps"],
    queryFn: ({ signal }) => apiGet<AudioProfileCatalog>("/admin/audio/profiles/steps", undefined, signal),
    enabled,
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/**
 * Everything a profile mutation has to refresh.
 *
 * The Audio tab's key covers the profile list and the queue counts; the talkgroups key is
 * there because a profile carries its `talkgroup_count` and a channel names the profile it
 * is on, so an edit that refreshed only the admin card would leave the directory showing a
 * name the profile no longer has.
 */
function invalidateAudioProfiles(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "audio"] });
  void queryClient.invalidateQueries({ queryKey: KEYS.talkgroups });
}

/** `POST /admin/audio/profiles` — 409 on a duplicate name, 422 on a chain that will not render. */
export function useCreateAudioProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AudioProfileInput) => apiPost<AudioProfile>("/admin/audio/profiles", body),
    onSuccess: () => invalidateAudioProfiles(queryClient),
  });
}

/** `PATCH /admin/audio/profiles/{id}` — only the keys present are written. */
export function useUpdateAudioProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: number; patch: AudioProfilePatch }) =>
      apiPatch<AudioProfile>(`/admin/audio/profiles/${id}`, patch),
    onSuccess: () => invalidateAudioProfiles(queryClient),
  });
}

/** `DELETE /admin/audio/profiles/{id}` — refused for a builtin, and for one still in use. */
export function useDeleteAudioProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<void>(`/admin/audio/profiles/${id}`),
    onSuccess: () => invalidateAudioProfiles(queryClient),
  });
}

/** What the preview hands back: the audio itself, and the chain that produced it. */
export interface AudioProfilePreview {
  blob: Blob;
  /** From the `X-Keyup-Filter` response header — the full `-af` string the server ran. */
  filter: string;
}

/**
 * `POST /admin/audio/profiles/preview` — run a chain over one call and hear the result.
 *
 * Alone among the mutations in this file it answers with bytes rather than JSON, so it
 * goes through `apiPostBlob`. Nothing is stored by it: no row is touched and the profile
 * need not exist, which is what lets a chain be tuned before it is a profile at all.
 *
 * The caller owns the blob. It becomes an object URL in the component that plays it and is
 * revoked there, because this hook has no idea when the audio element is done with it.
 */
export function useAudioProfilePreview() {
  return useMutation({
    mutationFn: async (body: AudioProfilePreviewInput): Promise<AudioProfilePreview> => {
      const { blob, headers } = await apiPostBlob("/admin/audio/profiles/preview", body);
      return { blob, filter: headers.get("X-Keyup-Filter") ?? "" };
    },
  });
}

/**
 * `POST /admin/audio/profiles/{id}/apply` — assign the profile and requeue recent calls.
 *
 * Also invalidates the calls, because the point of pressing it is that those recordings
 * are about to sound different.
 */
export function useApplyAudioProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: AudioProfileApplyInput }) =>
      apiPost<AudioProfileApplyResult>(`/admin/audio/profiles/${id}/apply`, body),
    onSuccess: () => {
      invalidateAudioProfiles(queryClient);
      void queryClient.invalidateQueries({ queryKey: KEYS.calls });
    },
  });
}

/* ------------------------------------------------- outbound exporters */

/**
 * `GET /admin/exports` — both services, their ledgers and the last 50 rows.
 * Polled while the tab is open: the interesting numbers (queue depth, the last
 * error) move on the worker's cycle, not on a user action.
 */
export function useExportStatus(enabled = true) {
  return useQuery<ExportStatus>({
    queryKey: [...KEYS.admin, "exports"],
    queryFn: ({ signal }) => apiGet<ExportStatus>("/admin/exports", undefined, signal),
    enabled,
    refetchInterval: 15_000,
  });
}

/**
 * Check one service's configuration. Uploads nothing — read `checked` in the
 * result for what was actually proven, which for Broadcastify is not the key.
 */
export function useTestExport() {
  return useMutation({
    // The card's **unsaved** values go with the request: an operator presses Test to find
    // out whether what is on the screen works, and testing what is stored instead answers
    // a question nobody asked. The server validates every key against that service's own
    // settings and stores none of them.
    mutationFn: ({ service, overrides }: { service: ExportService; overrides?: Record<string, string> }) =>
      apiPost<ExportTestResult>(`/admin/exports/${service}/test`, { overrides: overrides ?? {} }),
  });
}

/**
 * Open the Broadcastify **Feed** mount, write about two seconds of silence, close it.
 * The only honest check there is — Icecast has no dry run — and it publishes no call
 * audio. It does briefly occupy the mount, so it fails while another broadcasting
 * client holds it.
 */
export function useTestBroadcastifyFeed() {
  return useMutation({
    mutationFn: (overrides?: Record<string, string>) =>
      apiPost<ExportTestResult>("/admin/exports/broadcastify-feed/test", {
        overrides: overrides ?? {},
      }),
  });
}

/**
 * Copy one per-talkgroup export checkbox onto another — the move an operator makes once,
 * when Broadcastify's single checkbox became one for the Feed and one for Calls. A
 * talkgroup on a system that is not trunked never gets a ticked Calls flag out of it; the
 * result says how many were held back that way.
 */
export function useCopyTalkgroupExportFlags() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      source: TalkgroupExportFlag;
      destination: TalkgroupExportFlag;
      ids?: number[];
      all?: boolean;
    }) => apiPost<TalkgroupExportFlagCopyResult>("/admin/talkgroups/export-flags/copy", body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEYS.talkgroups });
    },
  });
}

/** Put one parked ledger row back in the queue. An uploaded row is refused (409). */
export function useRetryExport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ service, id }: { service: string; id: number }) =>
      apiPost<ExportLedgerRow>(`/admin/exports/${service}/ledger/${id}/retry`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "exports"] }),
  });
}

/* ------------------------------------------------- the HTTPS edge (docs/HTTPS.md) */

/**
 * `GET /admin/https/status` — what the edge is serving, and the certificate on the
 * wire for each domain.
 *
 * Admin-only on the server, so it is disabled for anyone else rather than putting a
 * 403 in the console. The poll is slow on purpose: each one makes the API open a TLS
 * connection per domain to read the certificate, which is cheap but not free, and
 * nothing here changes faster than an ACME issuance (tens of seconds).
 */
export function useHttpsStatus(enabled = true) {
  return useQuery<HttpsStatus>({
    queryKey: [...KEYS.admin, "https"],
    queryFn: ({ signal }) => apiGet<HttpsStatus>("/admin/https/status", undefined, signal),
    enabled,
    refetchInterval: 30_000,
  });
}

/**
 * `POST /admin/https/apply` — push the current settings into the running proxy.
 *
 * Saving an HTTPS setting already does this; the button is for retrying after a
 * failure, or after the edge has been restarted out from under the API.
 */
export function useApplyHttps() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<HttpsApplyResult>("/admin/https/apply"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "https"] }),
  });
}

/** `POST /admin/https/test-dns` — prove the Cloudflare token before an issuance. */
export function useTestCloudflareToken() {
  return useMutation({
    mutationFn: () => apiPost<HttpsDnsTestResult>("/admin/https/test-dns"),
  });
}

/* --------------------------------- Broadcastify feed statistics (docs/BCFY_STATS.md) */

/**
 * `GET /admin/bcfy-stats/summary` — the Feed stats page in one request.
 *
 * Admin-only on the server, so the query is disabled for anyone who is not one:
 * firing it anyway would put a 403 in the console on every page load for a reader
 * who can still legitimately see the rest of the site.
 */
export function useFeedSummary(days = 30, enabled = true) {
  return useQuery<FeedSummary>({
    queryKey: [...KEYS.feedStats, "summary", days],
    queryFn: ({ signal }) => apiGet<FeedSummary>("/admin/bcfy-stats/summary", { days }, signal),
    enabled,
    // The poller writes at most once a minute; a slow refetch keeps the status
    // card honest without making an open tab a load on the API.
    refetchInterval: 60_000,
  });
}

/** `GET /admin/bcfy-stats/samples` — the listeners chart. */
export function useFeedSamples(
  params: { from?: string; to?: string; step?: "hour" | "day" },
  enabled = true,
) {
  return useQuery<FeedSamplePoint[]>({
    queryKey: [...KEYS.feedStats, "samples", params],
    queryFn: ({ signal }) =>
      apiGet<FeedSamplePoint[]>("/admin/bcfy-stats/samples", params as QueryParams, signal),
    enabled,
  });
}

/** `GET /admin/bcfy-stats/archives` — the listings, all days or one. */
export function useFeedArchives(day: string | undefined, enabled = true) {
  return useQuery<FeedArchive[]>({
    queryKey: [...KEYS.feedStats, "archives", day ?? "all"],
    queryFn: ({ signal }) => apiGet<FeedArchive[]>("/admin/bcfy-stats/archives", { day }, signal),
    enabled,
  });
}

/**
 * One live `a=feed` request, printed rather than stored.
 *
 * This is the button that turns the guessed field names in `FIELD_ALIASES` into
 * observations — see docs/BCFY_STATS.md. It never writes a sample.
 */
export function useProbeFeed() {
  return useMutation({
    mutationFn: () => apiPost<FeedProbeResult>("/admin/bcfy-stats/probe"),
  });
}

/** The same request, stored — a sample plus its hourly rollup. */
export function usePollFeed() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<FeedProbeResult>("/admin/bcfy-stats/poll"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.feedStats }),
  });
}

/**
 * Fetch the feed's **public** listen page and backfill from its 24-hour sparkline.
 *
 * The only listener history Broadcastify publishes anywhere: the owner API answers
 * with one instant's count. Fills hours nobody polled and never overwrites a polled
 * one, so `hours_written` shrinks towards zero as the poller catches up.
 */
export function useFetchFeedPublicPage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<FeedPublicPageResult>("/admin/bcfy-stats/poll-page"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.feedStats }),
  });
}

/**
 * Import one of the feed dashboard's two CSV exports.
 *
 * The only listener backfill that reaches back **months**: the hourly export carries
 * about a week and the daily one about 180 days, where the owner API carries nothing
 * and the public page carries a day. The format is decided by the file's header on the
 * server, so nothing here has to know which of the two the operator picked.
 *
 * The text is posted as JSON rather than as a multipart body, the way the playlist
 * importers do it: the browser has already read the file, and the API accepts either.
 */
export function useImportFeedStats() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => apiPost<FeedImportReport>("/admin/bcfy-stats/import", { content }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.feedStats }),
  });
}

/**
 * Run the archive sync now, ignoring its switch and its interval.
 *
 * `full` lifts the per-pass backfill budget for this one run, which is the one-time
 * catch-up over a feed whose archives predate this deployment. The report says how
 * many days are still to fetch, so a second run finishes what a first could not.
 */
export function useSyncFeedArchives() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ full = false }: { full?: boolean } = {}) =>
      apiPost<Record<string, unknown>>("/admin/bcfy-stats/sync-archives", undefined, { full }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.feedStats }),
  });
}

export function useRetentionPolicies() {
  return useQuery<RetentionPolicy[]>({
    queryKey: [...KEYS.admin, "retention"],
    queryFn: ({ signal }) => apiGet<RetentionPolicy[]>("/admin/retention", undefined, signal),
  });
}

export function useSaveRetentionPolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (policy: RetentionPolicyInput) => apiPost<RetentionPolicy[]>("/admin/retention", policy),
    onSuccess: (data) => queryClient.setQueryData([...KEYS.admin, "retention"], data),
  });
}

export function useRunRetention() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<RetentionRunResult>("/admin/retention/run"),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.admin }),
  });
}

export function useTokens() {
  return useQuery<ApiTokenInfo[]>({
    queryKey: [...KEYS.admin, "tokens"],
    queryFn: ({ signal }) => apiGet<ApiTokenInfo[]>("/admin/tokens", undefined, signal),
  });
}

export function useCreateToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ApiTokenInput) => apiPost<ApiTokenCreated>("/admin/tokens", input),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "tokens"] }),
  });
}

export function useRevokeToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiPost<null>(`/admin/tokens/${id}/revoke`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "tokens"] }),
  });
}

/** Removes the row of an already-revoked token; the API refuses an active one with a 409. */
export function useDeleteToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<null>(`/admin/tokens/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "tokens"] }),
  });
}

export function useUsers() {
  return useQuery<User[]>({
    queryKey: [...KEYS.admin, "users"],
    queryFn: ({ signal }) => apiGet<User[]>("/admin/users", undefined, signal),
  });
}

export function useSaveUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id?: number; patch: UserInput | UserPatch }) =>
      id === undefined
        ? apiPost<User>("/admin/users", patch as UserInput)
        : apiPatch<User>(`/admin/users/${id}`, patch as UserPatch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...KEYS.admin, "users"] }),
  });
}

/* ----------------------------------------------------------------- alerts */

export function useAlerts() {
  return useQuery<AlertRule[]>({
    queryKey: KEYS.alerts,
    queryFn: ({ signal }) => apiGet<AlertRule[]>("/alerts", undefined, signal),
  });
}

export function useSaveAlert() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, rule }: { id?: number; rule: AlertRuleInput }) =>
      id === undefined ? apiPost<AlertRule>("/alerts", rule) : apiPatch<AlertRule>(`/alerts/${id}`, rule),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.alerts }),
  });
}

export function useDeleteAlert() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiDelete<null>(`/alerts/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEYS.alerts }),
  });
}

export function useNotifications() {
  return useQuery<Paginated<NotificationRecord>>({
    queryKey: ["notifications"],
    queryFn: ({ signal }) =>
      apiGet<Paginated<NotificationRecord>>("/notifications", { page_size: 50 }, signal),
  });
}

/* -------------------------------------------------------------- favorites */

/**
 * The pinned channels. `enabled` is for the screens that hide the star from a read-only
 * viewer altogether: with nothing on the page able to use the answer, asking for it is a
 * request made for nobody.
 */
export function useFavorites(enabled = true) {
  return useQuery<Favorite[]>({
    queryKey: ["favorites"],
    queryFn: ({ signal }) => apiGet<Favorite[]>("/favorites", undefined, signal),
    enabled,
  });
}

export function useSetFavorite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { talkgroup_id: number; muted: boolean }) => apiPut<Favorite[]>("/favorites", input),
    onSuccess: (data) => queryClient.setQueryData(["favorites"], data),
  });
}

export function useRemoveFavorite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (talkgroupId: number) => apiDelete<null>(`/favorites/${talkgroupId}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["favorites"] }),
  });
}

/* ------------------------------------------------------------------- auth */

export function useCurrentUser() {
  const query = useQuery<MeResponse>({
    queryKey: KEYS.auth,
    queryFn: ({ signal }) => apiGet<MeResponse>("/auth/me", undefined, signal),
    staleTime: 300_000,
    retry: false,
  });
  return {
    user: query.data?.user ?? null,
    /** Reads are open by default; this is what turns the whole UI read-only. */
    canWrite: query.data?.user != null,
    /** Admin-scoped actions (recorder edits, tokens, users) need this, not just canWrite. */
    isAdmin: (query.data?.scopes ?? []).includes("admin"),
    isLoading: query.isLoading,
  };
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (credentials: LoginRequest) => apiPost<MeResponse>("/auth/login", credentials),
    onSuccess: (me) => {
      queryClient.setQueryData(KEYS.auth, me);
      void queryClient.invalidateQueries();
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<MeResponse>("/auth/logout"),
    onSuccess: (me) => {
      queryClient.setQueryData(KEYS.auth, me);
      void queryClient.invalidateQueries();
    },
  });
}
