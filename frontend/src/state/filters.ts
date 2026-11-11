/**
 * The shared call-filter state.
 *
 * It lives entirely in the URL search params, so every view is linkable and
 * shareable and the browser's back button steps through filter changes. The
 * page's own state (which call is selected, which panel is open) is deliberately
 * *not* here — only what changes the result set.
 */
import { useCallback, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import type {
  CallFilterParams,
  CallKind,
  CallType,
  IngestPriorityClass,
  TranscriptQuality,
  TranscriptStatus,
} from "../api/types";
import {
  CALL_KINDS,
  CALL_TYPES,
  INGEST_PRIORITY_CLASSES,
  QUALITIES,
  TRANSCRIPT_STATUSES,
} from "../api/types";
import { clockToMinutes, isIsoDate } from "../lib/format";

/**
 * Every date range a URL may name. Each page offers its own subset (`CALLS_PRESETS`,
 * `SEARCH_PRESETS`), but every page *reads* all of them, so a link copied from one page
 * means the same thing pasted into another.
 */
export const DATE_PRESETS = ["today", "yesterday", "24h", "7d", "30d", "1y", "all", "custom"] as const;
export type DatePreset = (typeof DATE_PRESETS)[number];

export const PRESET_LABELS: Record<DatePreset, string> = {
  today: "Today",
  yesterday: "Yesterday",
  "24h": "Last 24h",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "1y": "Last year",
  all: "All time",
  custom: "Custom",
};

/** The default range of every filtered page except Search. */
export const DEFAULT_PRESET: DatePreset = "24h";

/**
 * Search's default: a search is a question about the archive, not about the last shift, so
 * it looks back a month. Only the Search page uses it — a bare `/search?q=…` link means
 * the last 30 days, while a bare `/calls` link still means the last 24 hours.
 */
export const SEARCH_DEFAULT_PRESET: DatePreset = "30d";

/**
 * The ranges the Calls rail offers. The long ones are left off on purpose: the rail's
 * breakdown counts every call in the window with no query to narrow it, and over a year
 * that is 17-26 s of aggregates on production (2026-10). A Calls URL that names one still
 * works, and the rail shows it as the selected chip.
 */
export const CALLS_PRESETS: readonly DatePreset[] = ["today", "yesterday", "24h", "7d"];

/** The ranges the Search page offers; `custom` appears only when a URL asked for it. */
export const SEARCH_PRESETS: readonly DatePreset[] = ["24h", "7d", "30d", "1y", "all"];
export const DURATION_MIN = 0;
/** The corpus tops out at 45 s; 60 gives the slider a round upper stop. */
export const DURATION_MAX = 60;

/**
 * Newest first. Named because it is more than a default: it is the one order a live
 * arrival can be inserted into without lying about where the call belongs, so the
 * Calls page compares against it to decide whether the feed may run.
 */
export const DEFAULT_SORT = "-started_at";

export interface CallFilters {
  q: string;
  /**
   * Also match the spellings of `q`'s words the transcripts actually contain (speech-to-text
   * writes "Milrose" for Melrose). A search option rather than a filter: off by default,
   * not counted in the active-filter badge, and cleared with the rest by `reset`.
   */
  fuzzy: boolean;
  preset: DatePreset;
  /** Only meaningful while `preset` is `custom`. */
  date_from: string;
  date_to: string;
  time_from: string | null;
  time_to: string | null;
  dow: number[];
  /** On-air tgids (`talkgroups.tgid`), which is what `?talkgroup=` means — never row ids. */
  talkgroup: number[];
  radio: number[];
  unit: string[];
  agency: string[];
  category: string[];
  call_type: CallType | null;
  /**
   * Who the traffic was addressed to: `group` (a talkgroup, patched or not) or `unit` (a
   * private call between two radios). Null is "both", which is what a reader scanning the
   * archive wants — private calls belong in the timeline, marked, not hidden from it.
   */
  kind: CallKind | null;
  /**
   * Only calls a recorder flagged as an emergency. Tri-state on the wire (an sdrtrunk
   * file carries no flag at all and matches neither true nor false), but the rail only
   * ever offers "on", because "show me the calls nobody marked urgent" is not a question
   * anybody asks.
   */
  emergency: boolean;
  encrypted: boolean;
  /**
   * Only pages: a two-tone or alert pattern in the call's first seconds, as the audio
   * pass heard it. Deliberately separate from `emergency`, which is what the radio
   * asserted — a fire page is almost never flagged emergency, and an emergency almost
   * never opens with tones.
   */
  alert: boolean;
  /** Only calls with a sustained tone of any kind — the wider set `alert` sits inside. */
  tone_detected: boolean;
  /**
   * Hide the metadata-only rows — encrypted calls and anything aged past retention have
   * no recording to play. On by default, because they are noise in a list you scan to
   * listen; turning it off widens to the whole archive. Sent as `has_audio` to the API.
   */
  audio_only: boolean;
  min_duration: number;
  max_duration: number;
  transcript_status: TranscriptStatus[];
  quality: TranscriptQuality[];
  /**
   * How the call reached the archive. Empty means "any", which includes the calls the
   * file watcher picked up off disk — those carry no class at all and match none of the
   * three, so ticking every box is *not* the same view as ticking none.
   */
  priority: IngestPriorityClass[];
  tag: string[];
  starred: boolean;
  include_duplicates: boolean;
  sort: string;
}

const pad = (n: number): string => String(n).padStart(2, "0");
const toIsoDate = (date: Date): string =>
  `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

/** The filters of a clean URL. `preset` is the page's own default range. */
export function defaultFilters(preset: DatePreset = DEFAULT_PRESET): CallFilters {
  const today = toIsoDate(new Date());
  return {
    q: "",
    fuzzy: false,
    preset,
    date_from: today,
    date_to: today,
    time_from: null,
    time_to: null,
    dow: [],
    talkgroup: [],
    radio: [],
    unit: [],
    agency: [],
    category: [],
    call_type: null,
    kind: null,
    emergency: false,
    encrypted: false,
    alert: false,
    tone_detected: false,
    audio_only: true,
    min_duration: DURATION_MIN,
    max_duration: DURATION_MAX,
    transcript_status: [],
    quality: [],
    priority: [],
    tag: [],
    starred: false,
    include_duplicates: false,
    sort: DEFAULT_SORT,
  };
}

const numbers = (values: string[]): number[] => values.map(Number).filter((value) => Number.isFinite(value));

const oneOf = <T extends string>(value: string | null, allowed: readonly T[]): T | null =>
  value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;

const clockOrNull = (value: string | null): string | null => (clockToMinutes(value) === null ? null : value);

const num = (value: string | null, fallback: number): number => {
  if (value === null) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

/**
 * Read the filters out of a URLSearchParams; anything malformed falls back. `defaults` is
 * what an absent parameter means — the page's {@link defaultFilters}.
 */
export function parseFilters(sp: URLSearchParams, defaults: CallFilters = defaultFilters()): CallFilters {
  const base = defaults;
  const timeFrom = clockOrNull(sp.get("time_from"));
  const timeTo = clockOrNull(sp.get("time_to"));
  return {
    q: sp.get("q") ?? base.q,
    fuzzy: sp.get("fuzzy") === "true",
    preset: oneOf(sp.get("preset"), DATE_PRESETS) ?? base.preset,
    date_from: isIsoDate(sp.get("date_from")) ? (sp.get("date_from") as string) : base.date_from,
    date_to: isIsoDate(sp.get("date_to")) ? (sp.get("date_to") as string) : base.date_to,
    // A half-set time-of-day window would silently drop half the day, so both
    // ends have to be present for either to count.
    time_from: timeFrom !== null && timeTo !== null ? timeFrom : null,
    time_to: timeFrom !== null && timeTo !== null ? timeTo : null,
    dow: numbers(sp.getAll("dow")).filter((day) => day >= 0 && day <= 6),
    talkgroup: numbers(sp.getAll("talkgroup")),
    radio: numbers(sp.getAll("radio")),
    unit: sp.getAll("unit"),
    agency: sp.getAll("agency"),
    category: sp.getAll("category"),
    call_type: oneOf(sp.get("call_type"), CALL_TYPES),
    kind: oneOf(sp.get("kind"), CALL_KINDS),
    emergency: sp.get("emergency") === "true" || sp.get("emergency") === "1",
    encrypted: sp.get("encrypted") === "true" || sp.get("encrypted") === "1",
    alert: sp.get("alert") === "true" || sp.get("alert") === "1",
    tone_detected: sp.get("tone_detected") === "true" || sp.get("tone_detected") === "1",
    // Default on; `audio=all` in the URL is the only way to turn it off, so a plain
    // /calls link is audio-only and the toggle round-trips.
    audio_only: sp.get("audio") !== "all",
    min_duration: Math.max(DURATION_MIN, num(sp.get("min_duration"), DURATION_MIN)),
    max_duration: Math.min(DURATION_MAX, num(sp.get("max_duration"), DURATION_MAX)),
    transcript_status: sp
      .getAll("transcript_status")
      .filter((value): value is TranscriptStatus =>
        (TRANSCRIPT_STATUSES as readonly string[]).includes(value),
      ),
    quality: sp
      .getAll("quality")
      .filter((value): value is TranscriptQuality => (QUALITIES as readonly string[]).includes(value)),
    priority: sp
      .getAll("priority")
      .filter((value): value is IngestPriorityClass =>
        (INGEST_PRIORITY_CLASSES as readonly string[]).includes(value),
      ),
    tag: sp.getAll("tag"),
    starred: sp.get("starred") === "true" || sp.get("starred") === "1",
    include_duplicates: sp.get("include_duplicates") === "true",
    sort: sp.get("sort") ?? base.sort,
  };
}

/**
 * The inverse: only non-default values are written, so a default view has a
 * clean URL and two identical filter sets always produce the same link.
 */
export function serializeFilters(
  filters: CallFilters,
  defaults: CallFilters = defaultFilters(),
): URLSearchParams {
  const base = defaults;
  const sp = new URLSearchParams();
  const setIf = (key: string, value: string, fallback: string) => {
    if (value !== fallback && value !== "") sp.set(key, value);
  };
  const appendAll = (key: string, values: (string | number)[]) => {
    for (const value of values) sp.append(key, String(value));
  };

  setIf("q", filters.q, base.q);
  setIf("preset", filters.preset, base.preset);
  if (filters.preset === "custom") {
    sp.set("date_from", filters.date_from);
    sp.set("date_to", filters.date_to);
  }
  if (filters.time_from && filters.time_to) {
    sp.set("time_from", filters.time_from);
    sp.set("time_to", filters.time_to);
  }
  appendAll("dow", filters.dow);
  appendAll("talkgroup", filters.talkgroup);
  appendAll("radio", filters.radio);
  appendAll("unit", filters.unit);
  appendAll("agency", filters.agency);
  appendAll("category", filters.category);
  if (filters.call_type) sp.set("call_type", filters.call_type);
  if (filters.kind) sp.set("kind", filters.kind);
  if (filters.emergency) sp.set("emergency", "true");
  if (filters.encrypted) sp.set("encrypted", "true");
  if (filters.alert) sp.set("alert", "true");
  if (filters.tone_detected) sp.set("tone_detected", "true");
  if (!filters.audio_only) sp.set("audio", "all");
  if (filters.min_duration !== base.min_duration) sp.set("min_duration", String(filters.min_duration));
  if (filters.max_duration !== base.max_duration) sp.set("max_duration", String(filters.max_duration));
  appendAll("transcript_status", filters.transcript_status);
  appendAll("quality", filters.quality);
  appendAll("priority", filters.priority);
  appendAll("tag", filters.tag);
  if (filters.starred) sp.set("starred", "true");
  if (filters.include_duplicates) sp.set("include_duplicates", "true");
  if (filters.fuzzy) sp.set("fuzzy", "true");
  setIf("sort", filters.sort, base.sort);
  return sp;
}

/**
 * The `date_from` / `date_to` a preset stands for, as ISO instants.
 *
 * A rolling preset — today, the last 24 h, the last 7 days — runs *until now*, so its
 * `to` is `null` and nothing is sent for it. A fixed end taken when the params were built
 * is what used to hide every call recorded after page load: the live filter frame
 * carried it, the server compared `started_at <= date_to`, and Live and Calls went quiet
 * until a reload. Its start is floored to the minute so the params — the React Query key,
 * and the server's facet cache, which is keyed on the normalised filter — come out the
 * same for every render inside that minute instead of changing every millisecond.
 *
 * `yesterday` is a closed window of its own and keeps both ends. `30d` and `1y` roll like
 * `24h` (a year is 365 days). `all` and `custom` have no preset range and return `null`:
 * `all` sends no dates at all, `custom` sends the dates the reader typed.
 */
export function presetRange(
  preset: DatePreset,
  now = new Date(),
): { from: string; to: string | null } | null {
  const startOfDay = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const minute = Math.floor(now.getTime() / 60_000) * 60_000;
  switch (preset) {
    case "today": {
      return { from: startOfDay(now).toISOString(), to: null };
    }
    case "yesterday": {
      const start = startOfDay(new Date(now.getTime() - 86_400_000));
      return { from: start.toISOString(), to: new Date(start.getTime() + 86_399_999).toISOString() };
    }
    case "24h":
      return { from: new Date(minute - 86_400_000).toISOString(), to: null };
    case "7d":
      return { from: new Date(minute - 7 * 86_400_000).toISOString(), to: null };
    case "30d":
      return { from: new Date(minute - 30 * 86_400_000).toISOString(), to: null };
    case "1y":
      return { from: new Date(minute - 365 * 86_400_000).toISOString(), to: null };
    default:
      return null;
  }
}

/**
 * Turn the UI's filter object into the query the API takes.
 *
 * `now` anchors a rolling preset's start (see {@link presetRange}); a custom range is
 * passed through exactly as the reader typed it.
 */
export function toApiParams(
  filters: CallFilters,
  extra: Partial<CallFilterParams> = {},
  now = new Date(),
): CallFilterParams {
  const range = presetRange(filters.preset, now);
  const params: CallFilterParams = {
    ...(filters.q ? { q: filters.q } : {}),
    // Meaningless without `q`, so never sent alone.
    ...(filters.q && filters.fuzzy ? { fuzzy: true } : {}),
    ...(range
      ? { date_from: range.from, ...(range.to !== null ? { date_to: range.to } : {}) }
      : filters.preset === "all"
        ? {}
        : { date_from: filters.date_from, date_to: filters.date_to }),
    ...(filters.time_from && filters.time_to
      ? { time_from: filters.time_from, time_to: filters.time_to }
      : {}),
    ...(filters.dow.length ? { dow: filters.dow } : {}),
    ...(filters.talkgroup.length ? { talkgroup: filters.talkgroup } : {}),
    ...(filters.radio.length ? { radio: filters.radio } : {}),
    ...(filters.unit.length ? { unit: filters.unit } : {}),
    ...(filters.agency.length ? { agency: filters.agency } : {}),
    ...(filters.category.length ? { category: filters.category } : {}),
    ...(filters.call_type ? { call_type: filters.call_type } : {}),
    ...(filters.kind ? { kind: [filters.kind] } : {}),
    ...(filters.emergency ? { emergency: true } : {}),
    ...(filters.encrypted ? { encrypted: true } : {}),
    ...(filters.alert ? { alert: true } : {}),
    // `alert` already implies a tone, so the narrower filter is not sent alongside it.
    ...(filters.tone_detected && !filters.alert ? { tone_detected: true } : {}),
    ...(filters.audio_only ? { has_audio: true } : {}),
    ...(filters.min_duration > DURATION_MIN ? { min_duration: filters.min_duration } : {}),
    ...(filters.max_duration < DURATION_MAX ? { max_duration: filters.max_duration } : {}),
    ...(filters.transcript_status.length ? { transcript_status: filters.transcript_status } : {}),
    ...(filters.quality.length ? { quality: filters.quality } : {}),
    ...(filters.priority.length ? { priority: filters.priority } : {}),
    ...(filters.tag.length ? { tag: filters.tag } : {}),
    ...(filters.starred ? { starred: true } : {}),
    ...(filters.include_duplicates ? { include_duplicates: true } : {}),
    sort: filters.sort,
    ...extra,
  };
  return params;
}

export interface ApiParams {
  params: CallFilterParams;
  /**
   * Re-read a rolling window's start from the clock. `true` when that moved it — the
   * params, and so the query key, are new and the fetch follows by itself; `false` when
   * nothing changed (a custom range, or the same minute) and the caller must refetch.
   * Stable across renders, so an effect can depend on it without re-running.
   */
  refresh: () => boolean;
}

/**
 * {@link toApiParams} for a page that stays open.
 *
 * The params are memoised on the filters, so the query key and the live filter frame hold
 * still between renders. A rolling window's start is taken when the filters are chosen;
 * with no end that loses nothing new, but a page that slept for an hour would keep asking
 * for 25 hours. The pages call `refresh()` when the feed comes back from a suspension, so
 * the window they refetch is the one the reader asked for, measured from now.
 */
export function useApiParams(filters: CallFilters, extra: Partial<CallFilterParams> = {}): ApiParams {
  // Serialised so a caller can pass an object literal without re-deriving every render.
  const extraKey = JSON.stringify(extra);
  // The moment the window was last re-read, and for which filters: a new filter set is a
  // new question and takes the clock afresh.
  const [anchor, setAnchor] = useState<{ filters: CallFilters; at: number } | null>(null);
  const params = useMemo(() => {
    const now = anchor !== null && anchor.filters === filters ? new Date(anchor.at) : new Date();
    return toApiParams(filters, JSON.parse(extraKey) as Partial<CallFilterParams>, now);
  }, [filters, extraKey, anchor]);

  const latest = useRef({ filters, extraKey, params });
  latest.current = { filters, extraKey, params };
  const refresh = useCallback(() => {
    const { filters: current, extraKey: currentExtra, params: before } = latest.current;
    const at = Date.now();
    const next = toApiParams(current, JSON.parse(currentExtra) as Partial<CallFilterParams>, new Date(at));
    if (JSON.stringify(next) === JSON.stringify(before)) return false;
    setAnchor({ filters: current, at });
    return true;
  }, []);

  return { params, refresh };
}

/** How many filters are actually narrowing the view — the badge on the rail. */
export function activeFilterCount(filters: CallFilters, defaults: CallFilters = defaultFilters()): number {
  const base = defaults;
  let count = 0;
  if (filters.q) count += 1;
  if (filters.preset !== base.preset) count += 1;
  if (filters.time_from && filters.time_to) count += 1;
  if (filters.dow.length) count += 1;
  if (filters.talkgroup.length) count += 1;
  if (filters.radio.length) count += 1;
  if (filters.unit.length) count += 1;
  if (filters.agency.length) count += 1;
  if (filters.category.length) count += 1;
  if (filters.call_type) count += 1;
  if (filters.kind) count += 1;
  if (filters.emergency) count += 1;
  if (filters.encrypted) count += 1;
  if (filters.alert) count += 1;
  if (filters.tone_detected) count += 1;
  if (filters.min_duration > base.min_duration || filters.max_duration < base.max_duration) count += 1;
  if (filters.transcript_status.length) count += 1;
  if (filters.quality.length) count += 1;
  if (filters.priority.length) count += 1;
  if (filters.tag.length) count += 1;
  if (filters.starred) count += 1;
  if (filters.include_duplicates) count += 1;
  return count;
}

export interface FilterController {
  filters: CallFilters;
  /** Change some filters, keeping the rest. */
  patch: (changes: Partial<CallFilters>) => void;
  reset: () => void;
  activeCount: number;
  /** The query string that recreates this view. */
  query: string;
}

/**
 * The hook every filtered page uses. Filter changes `replace` the history entry
 * rather than pushing one, so the back button leaves the page instead of
 * unwinding a dozen keystrokes — except for the query text, which is a
 * deliberate act worth a history entry.
 *
 * `defaultPreset` is the range a URL without `preset` means on this page: the Search page
 * passes {@link SEARCH_DEFAULT_PRESET}, every other page keeps {@link DEFAULT_PRESET}.
 */
export function useCallFilters(defaultPreset: DatePreset = DEFAULT_PRESET): FilterController {
  const [searchParams, setSearchParams] = useSearchParams();
  const filters = useMemo(
    () => parseFilters(searchParams, defaultFilters(defaultPreset)),
    [searchParams, defaultPreset],
  );

  const patch = useCallback(
    (changes: Partial<CallFilters>) => {
      const defaults = defaultFilters(defaultPreset);
      const next = { ...parseFilters(searchParams, defaults), ...changes };
      // Any new filter starts the reader at the top of the results.
      setSearchParams(serializeFilters(next, defaults), { replace: true });
    },
    [searchParams, setSearchParams, defaultPreset],
  );

  const reset = useCallback(() => {
    const defaults = defaultFilters(defaultPreset);
    setSearchParams(serializeFilters(defaults, defaults), { replace: true });
  }, [setSearchParams, defaultPreset]);

  const defaults = defaultFilters(defaultPreset);
  return {
    filters,
    patch,
    reset,
    activeCount: activeFilterCount(filters, defaults),
    query: serializeFilters(filters, defaults).toString(),
  };
}
