/** Filtering, sorting and faceting over the fake corpus — the mock's query planner. */

import { DOW_LABELS } from "../../lib/format";
import type { QueryParams } from "../client";
import type {
  Call,
  CallType,
  Facets,
  IngestPriorityClass,
  TranscriptQuality,
  TranscriptStatus,
} from "../types";
import { priorityClass } from "../types";
import { CALLS, type MockCall, RADIOS, TALKGROUPS } from "./dataset";

const str = (params: QueryParams, key: string): string | undefined => {
  const value = params[key];
  if (value === undefined || value === null || value === "") return undefined;
  return String(value);
};

const num = (params: QueryParams, key: string): number | undefined => {
  const value = str(params, key);
  if (value === undefined) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const bool = (params: QueryParams, key: string): boolean | undefined => {
  const value = str(params, key);
  if (value === undefined) return undefined;
  return value === "true" || value === "1";
};

/** Repeatable params arrive as an array from the hooks, or as one bare value. */
export const list = (params: QueryParams, key: string): string[] => {
  const value = params[key];
  if (value === undefined || value === null || value === "") return [];
  if (Array.isArray(value)) return value.map(String).filter((v) => v !== "");
  return [String(value)];
};

/** Minutes past midnight of a naive local timestamp. */
const localMinutes = (call: Call): number =>
  Number(call.started_local.slice(11, 13)) * 60 + Number(call.started_local.slice(14, 16));

/**
 * Postgres `websearch_to_tsquery` in miniature: quoted phrases must appear
 * verbatim, a leading `-` excludes, everything else is an AND of terms.
 */
export function matchesQuery(haystack: string, query: string): boolean {
  const text = haystack.toLowerCase();
  const terms = query.toLowerCase().match(/"[^"]*"|\S+/g) ?? [];
  for (const raw of terms) {
    if (raw === "or" || raw === "and") continue;
    const negated = raw.startsWith("-");
    const bare = (negated ? raw.slice(1) : raw).replace(/^"|"$/g, "");
    if (!bare) continue;
    const hit = text.includes(bare);
    if (negated ? hit : !hit) return false;
  }
  return true;
}

/** Everything a call's free-text search covers: transcript, notes and aliases. */
const searchable = (call: MockCall): string =>
  [
    call.transcript_text ?? "",
    call.notes,
    call.talkgroup_alias ?? "",
    call.talkgroup_alias_at_record ?? "",
    call.radio_label ?? "",
    call.unit_id ?? "",
    call.tags.join(" "),
    call.patch_label ?? "",
  ].join(" ");

export function filterCalls(params: QueryParams): MockCall[] {
  const q = str(params, "q");
  const dateFrom = str(params, "date_from");
  const dateTo = str(params, "date_to");
  const timeFrom = str(params, "time_from");
  const timeTo = str(params, "time_to");
  const dow = list(params, "dow").map(Number);
  const talkgroups = list(params, "talkgroup").map(Number);
  const radios = list(params, "radio").map(Number);
  const units = list(params, "unit");
  const agencies = list(params, "agency");
  const categories = list(params, "category");
  const callType = str(params, "call_type") as CallType | undefined;
  const kinds = list(params, "kind");
  const emergency = bool(params, "emergency");
  const encrypted = bool(params, "encrypted");
  const hasAudio = bool(params, "has_audio");
  const audioTypes = list(params, "audio_type");
  const minDuration = num(params, "min_duration");
  const maxDuration = num(params, "max_duration");
  const statuses = list(params, "transcript_status") as TranscriptStatus[];
  const qualities = list(params, "quality") as TranscriptQuality[];
  const priorities = list(params, "priority") as IngestPriorityClass[];
  const tags = list(params, "tag");
  const starred = bool(params, "starred");
  const patch = num(params, "patch");
  const conversation = num(params, "conversation");
  const includeDuplicates = bool(params, "include_duplicates") ?? false;
  const site = num(params, "site");

  // Talkgroup filters are given as on-air tgids — not row ids — and the corpus matches
  // on tgid so patched traffic counts, exactly as `talkgroup_ids[]` does in Postgres.
  const wantedTgids = new Set(talkgroups);

  const fromMs = dateFrom ? Date.parse(`${dateFrom}T00:00:00`) : undefined;
  const toMs = dateTo ? Date.parse(`${dateTo}T23:59:59.999`) : undefined;
  const fromMinutes = timeFrom ? Number(timeFrom.slice(0, 2)) * 60 + Number(timeFrom.slice(3, 5)) : undefined;
  const toMinutes = timeTo ? Number(timeTo.slice(0, 2)) * 60 + Number(timeTo.slice(3, 5)) : undefined;

  return CALLS.filter((call) => {
    if (!includeDuplicates && call.duplicate_of !== null) return false;
    const started = Date.parse(call.started_at);
    if (fromMs !== undefined && started < fromMs) return false;
    if (toMs !== undefined && started > toMs) return false;
    if (fromMinutes !== undefined && toMinutes !== undefined) {
      const minutes = localMinutes(call);
      // A window that wraps past midnight (23:00 → 05:00) is a union, not a range.
      const inside =
        fromMinutes <= toMinutes
          ? minutes >= fromMinutes && minutes <= toMinutes
          : minutes >= fromMinutes || minutes <= toMinutes;
      if (!inside) return false;
    }
    if (dow.length && !dow.includes(new Date(`${call.started_local}Z`).getUTCDay())) return false;
    if (site !== undefined && call.site_id !== site) return false;
    if (wantedTgids.size && !call.talkgroup_ids.some((tgid) => wantedTgids.has(tgid))) return false;
    if (radios.length && (call.radio_id === null || !radios.includes(call.radio_id))) return false;
    if (units.length && (call.unit_id === null || !units.includes(call.unit_id))) return false;
    if (agencies.length && (call.agency_code === null || !agencies.includes(call.agency_code))) return false;
    if (categories.length && (call.category === null || !categories.includes(call.category))) return false;
    if (callType && call.call_type !== callType) return false;
    if (kinds.length && !kinds.includes(call.kind)) return false;
    // `!==` and not a falsy check: both flags are nullable, and "not an emergency" must
    // not quietly include the calls whose recorder never said either way, as the API's
    // `IS` comparison does not.
    if (emergency !== undefined && call.emergency !== emergency) return false;
    if (encrypted !== undefined && call.encrypted !== encrypted) return false;
    if (hasAudio !== undefined && (call.audio_state === "present") !== hasAudio) return false;
    if (audioTypes.length && (call.audio_type === null || !audioTypes.includes(call.audio_type)))
      return false;
    if (minDuration !== undefined && call.duration_s < minDuration) return false;
    if (maxDuration !== undefined && call.duration_s > maxDuration) return false;
    if (statuses.length && !statuses.includes(call.transcript_status)) return false;
    if (
      qualities.length &&
      (call.transcript_quality === null || !qualities.includes(call.transcript_quality))
    )
      return false;
    // A call with no class is in none of the three, exactly as in Postgres: NULL is not
    // quietly folded into "live", it means nobody recorded an admission class at all.
    if (priorities.length) {
      const name = priorityClass(call);
      if (name === null || !priorities.includes(name)) return false;
    }
    if (tags.length && !tags.some((tag) => call.tags.includes(tag))) return false;
    if (starred !== undefined && call.starred !== starred) return false;
    if (patch !== undefined && call.patch_id !== patch) return false;
    if (conversation !== undefined && call.conversation_id !== conversation) return false;
    if (q && !matchesQuery(searchable(call), q)) return false;
    return true;
  });
}

/** `sort=-started_at` style ordering; unknown keys fall back to newest first. */
export function sortCalls(calls: MockCall[], sort: string | undefined): MockCall[] {
  const key = (sort ?? "-started_at").replace(/^-/, "");
  const descending = (sort ?? "-started_at").startsWith("-");
  const value = (call: MockCall): number | string => {
    switch (key) {
      case "duration_s":
        return call.duration_s;
      case "talkgroup":
        return call.talkgroup_alias ?? "";
      case "radio":
        return call.radio_label ?? "";
      case "quality":
        return call.transcript_quality ?? "";
      default:
        return Date.parse(call.started_at);
    }
  };
  return [...calls].sort((a, b) => {
    const left = value(a);
    const right = value(b);
    const cmp =
      typeof left === "number" ? left - (right as number) : String(left).localeCompare(String(right));
    return descending ? -cmp : cmp;
  });
}

const bucketize = (
  calls: MockCall[],
  keyOf: (call: MockCall) => string | null,
  labelOf: (key: string) => string,
) => {
  const counts = new Map<string, number>();
  for (const call of calls) {
    const key = keyOf(call);
    if (key === null) continue;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, label: labelOf(key), count }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
};

export function buildFacets(calls: MockCall[]): Facets {
  const tgLabel = (key: string) => TALKGROUPS.find((tg) => String(tg.tgid) === key)?.alias ?? key;
  const radioLabelFor = (key: string) => {
    const radio = RADIOS.find((r) => String(r.id) === key);
    return radio ? (radio.unit_id ?? radio.alias ?? String(radio.radio_id)) : key;
  };
  return {
    // Keyed by the on-air tgid, which is what the panel hands back to `?talkgroup=`.
    talkgroup: bucketize(calls, (c) => (typeof c.tgid === "number" ? String(c.tgid) : null), tgLabel),
    radio: bucketize(calls, (c) => (c.radio_id === null ? null : String(c.radio_id)), radioLabelFor).slice(
      0,
      25,
    ),
    agency: bucketize(
      calls,
      (c) => c.agency_code,
      (key) => key,
    ),
    category: bucketize(
      calls,
      (c) => c.category,
      (key) => key,
    ),
    hour: Array.from({ length: 24 }, (_, hour) => {
      const key = String(hour).padStart(2, "0");
      return {
        key,
        label: `${key}:00`,
        count: calls.filter((c) => Number(c.started_local.slice(11, 13)) === hour).length,
      };
    }),
    transcript_status: bucketize(
      calls,
      (c) => c.transcript_status,
      (key) => key,
    ),
    quality: bucketize(
      calls,
      (c) => c.transcript_quality,
      (key) => key,
    ),
    dow: Array.from({ length: 7 }, (_, day) => ({
      key: String(day),
      label: DOW_LABELS[day] as string,
      count: calls.filter((c) => new Date(`${c.started_local}Z`).getUTCDay() === day).length,
    })),
    tag: bucketize(
      calls.flatMap((c) => c.tags.map((tag) => ({ ...c, tags: [tag] }))),
      (c) => c.tags[0] ?? null,
      (key) => key,
    ),
    total: calls.length,
    // The mock never times out, so every dimension is always there.
    incomplete: false,
  };
}

/** Strip the mock-only fields so a list row looks exactly like the API's. */
export function toCall(call: MockCall): Call {
  const { raw: _raw, transcripts: _transcripts, ...rest } = call;
  return rest;
}

/** The page a mock cursor (`p<N>`, what `paginate` hands out as `next_cursor`) points at. */
const cursorPage = (params: QueryParams): number | null => {
  const match = /^p(\d+)$/.exec(String(params.cursor ?? ""));
  return match ? Number(match[1]) : null;
};

export const page = (params: QueryParams): { page: number; pageSize: number; cursor: boolean } => {
  const fromCursor = cursorPage(params);
  return {
    page: Math.max(1, fromCursor ?? num(params, "page") ?? 1),
    pageSize: Math.min(500, Math.max(1, num(params, "page_size") ?? 50)),
    cursor: fromCursor !== null,
  };
};
