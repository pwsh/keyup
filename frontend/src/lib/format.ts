import { format, formatDistanceToNowStrict, parseISO } from "date-fns";

const NUMBER = new Intl.NumberFormat("en-US");

export const formatNumber = (n: number | null | undefined): string =>
  n === null || n === undefined || !Number.isFinite(n) ? "—" : NUMBER.format(n);

export const formatPercent = (n: number | null | undefined, digits = 1): string =>
  n === null || n === undefined || !Number.isFinite(n) ? "—" : `${(n * 100).toFixed(digits)}%`;

/** `2026-08-29` and nothing else — used to sanity-check dates arriving from the URL. */
export function isIsoDate(value: string | null | undefined): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00`);
  return !Number.isNaN(parsed.getTime());
}

export const parseApiDate = (value: string): Date => parseISO(value);

const safe = (value: string | null | undefined, pattern: string, fallback = "—"): string => {
  if (!value) return fallback;
  const date = parseISO(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return format(date, pattern);
};

/** `Aug 29, 14:03:07` — the calls table's time column. */
export const formatCallTime = (value: string | null | undefined): string => safe(value, "MMM d, HH:mm:ss");

/** `14:03:07` — inside a day that is already established by a heading. */
export const formatClock = (value: string | null | undefined): string => safe(value, "HH:mm:ss");

export const formatDateTime = (value: string | null | undefined): string =>
  safe(value, "yyyy-MM-dd HH:mm:ss");

export const formatDate = (value: string | null | undefined): string => safe(value, "yyyy-MM-dd");

/** UTC rendering of an instant, for the call-detail header's second line. */
export const formatUtc = (value: string | null | undefined): string => {
  if (!value) return "—";
  const date = parseISO(value);
  if (Number.isNaN(date.getTime())) return "—";
  return `${date.toISOString().slice(0, 19).replace("T", " ")}Z`;
};

export const formatRelative = (value: string | null | undefined): string => {
  if (!value) return "—";
  const date = parseISO(value);
  if (Number.isNaN(date.getTime())) return "—";
  // A future instant (a certificate's expiry, a scheduled run) reads "in 2 months", not "ago".
  return formatDistanceToNowStrict(date, { addSuffix: true });
};

/**
 * `12.4s` for sub-minute call lengths where precision matters, `1:24` up to an hour, and
 * `19h 21m` beyond — a channel's total audio, which as minutes read "1161:10".
 */
export const formatDuration = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds >= 3600) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return minutes ? `${formatNumber(hours)}h ${minutes}m` : `${formatNumber(hours)}h`;
  }
  const mins = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${mins}:${String(rest).padStart(2, "0")}`;
};

/** `0:00` / `1:07` — the player's elapsed and total readouts. */
export const formatTimecode = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "0:00";
  const mins = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${mins}:${String(rest).padStart(2, "0")}`;
};

export const formatBytes = (bytes: number | null | undefined): string => {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
};

/** `852.1375 MHz` from the raw Hz the recorder reports. */
export const formatFrequency = (hz: number | null | undefined): string =>
  hz === null || hz === undefined || !Number.isFinite(hz) ? "—" : `${(hz / 1e6).toFixed(4)} MHz`;

/** Seconds of ingest lag as a short human phrase. */
export const formatLag = (seconds: number | null | undefined): string => {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
};

/**
 * A talkgroup's display name. The alias comes from the recorder's playlist and is
 * genuinely missing for channels nobody has named yet, so every list falls back to
 * the number rather than rendering a blank cell.
 */
export const talkgroupLabel = (talkgroup: { alias: string | null; tgid: number | null }): string =>
  talkgroup.alias?.trim() || (talkgroup.tgid != null ? `TG ${talkgroup.tgid}` : "—");

/** A radio's display name: its unit, then its alias, then the bare id. */
export const radioLabel = (radio: {
  unit_id: string | null;
  alias: string | null;
  radio_id: number;
}): string => radio.unit_id?.trim() || radio.alias?.trim() || String(radio.radio_id);

/** The same, for a call row — which carries the alias under a different name. */
export const callTalkgroupLabel = (call: { talkgroup_alias: string | null; tgid: number | null }): string =>
  talkgroupLabel({ alias: call.talkgroup_alias, tgid: call.tgid });

/**
 * A unit-to-unit call's two ends, as `caller → callee`.
 *
 * This is what stands where a talkgroup name would: a private call is on no talkgroup, and
 * the pair of radios is the only thing that says what the row *is*. Either end falls back
 * to a question mark rather than to nothing, because "we did not resolve that radio" and
 * "there was no radio" are different, and an arrow with one side missing shows which.
 */
export const unitCallLabel = (call: {
  radio_label?: string | null;
  radio_raw?: string | null;
  target_radio_label?: string | null;
}): string => `${call.radio_label ?? call.radio_raw ?? "?"} → ${call.target_radio_label ?? "?"}`;

/**
 * A site's display name, as `System / Site`.
 *
 * A site name on its own does not identify a site: `sites` is unique on
 * `(system_id, name)`, so two systems can both have a `Springfield` — and production
 * has exactly that, which is how a school district's calls ended up filed under
 * the county network. Anywhere a site is named as flowing text this is what to
 * render; anywhere there is room for two lines, put the system in a muted line
 * above or below the name instead.
 *
 * The system falls back to nothing rather than to "unknown": a label reading
 * "— / Springfield" is noise, and the caller that has no system to show is usually one
 * already inside a single system's own view.
 */
export const siteLabel = (site: { name: string; system_name?: string | null } | null): string => {
  if (!site) return "—";
  const system = site.system_name?.trim();
  return system ? `${system} / ${site.name}` : site.name;
};

/* ------------------------------------------------------------ display labels */
/*
 * The API speaks in codes — `pw`, `STATE_UNIVERSITY`, `file_write` — that are right for a
 * URL or a filter and wrong on a page. Everything a reader sees goes through one of these.
 * Each falls back to a tidied form of the code rather than to nothing, because the code
 * sets are open-ended (categories and agencies are free text in the database).
 */

/** What each talkgroup category means. Keys mirror `TALKGROUP_CATEGORIES` in `api/types.ts`. */
export const CATEGORY_LABELS: Readonly<Record<string, string>> = {
  law: "Law enforcement",
  fire: "Fire",
  ems: "EMS",
  multi: "Multi-service",
  interop: "Interoperability",
  pw: "Public works",
  utility: "Utilities",
  transit: "Transportation",
  rail: "Railroad",
  aviation: "Aircraft",
  schools: "Schools",
  corrections: "Corrections",
  security: "Security",
  federal: "Federal",
  military: "Military",
  media: "Media",
  business: "Business",
  ham: "Amateur radio",
  data: "Data",
  other: "Other",
};

/** Words kept in capitals when a SHOUTED name is put into title case. */
const ACRONYMS = new Set(["PD", "DPS", "EMS", "FD", "ISP", "ECC", "CAD", "UI", "US", "RR", "STT", "TG"]);
const MINOR_WORDS = new Set(["of", "and", "the", "for", "in", "at", "on"]);

/**
 * `FAIRVIEW, CITY OF` → `City of Fairview`; `SHERIFF'S OFFICE` → `Sheriff's Office`;
 * `RIVER_CITY` → `River City`. Text that is already mixed case is left as written.
 */
export function humanizeName(value: string): string {
  const text = value.replace(/_/g, " ").replace(/\s+/g, " ").trim();
  // A lower-case code (`vehicle`, `file_write`) reads as a sentence-case phrase.
  if (text && text === text.toLowerCase()) return text.charAt(0).toUpperCase() + text.slice(1);
  if (!text || text !== text.toUpperCase()) return text;
  const inverted = /^(.*), (CITY|TOWN|COUNTY|STATE) OF$/.exec(text);
  const ordered = inverted ? `${inverted[2]} OF ${inverted[1]}` : text;
  return ordered
    .split(" ")
    .map((word, index) => {
      if (ACRONYMS.has(word)) return word;
      const lower = word.toLowerCase();
      if (index > 0 && MINOR_WORDS.has(lower)) return lower;
      return lower.replace(/(^|[-/])([a-z])/g, (_, sep: string, ch: string) => sep + ch.toUpperCase());
    })
    .join(" ");
}

/** A talkgroup category for display; `null` reads "Uncategorized". */
export const categoryLabel = (category: string | null | undefined): string =>
  !category ? "Uncategorized" : (CATEGORY_LABELS[category] ?? humanizeName(category));

/**
 * An agency for display, from its directory row when the caller has it.
 *
 * Agency codes are identifiers (`XX0010000` is an ORI number, `EXAMPLEFD` a fire district),
 * so the row's name is what to show. Without a row: a code with digits in it is opaque
 * and is shown as is; one made of words (`STATE_UNIVERSITY`) is tidied into them.
 */
export function agencyLabel(
  code: string | null | undefined,
  agency?: { name?: string | null } | null,
): string {
  const name = agency?.name?.trim();
  if (name) return humanizeName(name);
  if (!code) return "—";
  return /\d/.test(code) ? code : humanizeName(code);
}

/** The kinds of agency (`AGENCY_KINDS` in `api/types.ts`). */
export const AGENCY_KIND_LABELS: Readonly<Record<string, string>> = {
  dispatch: "Dispatch",
  police: "Police",
  sheriff: "Sheriff",
  university: "University",
  fire: "Fire",
  ems: "EMS",
};

export const agencyKindLabel = (kind: string | null | undefined): string =>
  !kind ? "—" : (AGENCY_KIND_LABELS[kind] ?? humanizeName(kind));

/** How a call's start time was established (`TIME_SOURCES` in `api/types.ts`). */
export const TIME_SOURCE_LABELS: Readonly<Record<string, string>> = {
  file_write: "File time",
  upload: "Upload time",
  event_log: "Recorder log",
  sidecar: "Recorder metadata",
};

export const timeSourceLabel = (source: string | null | undefined): string =>
  !source ? "—" : (TIME_SOURCE_LABELS[source] ?? humanizeName(source));

/** How a call was granted (`call_type`): the filter chips' words for the API's codes. */
export const CALL_TYPE_LABELS: Readonly<Record<"group" | "patch" | "individual", string>> = {
  group: "Talkgroup",
  patch: "Patch",
  individual: "Unit to unit",
};

/** The audio chain a call went through: a named profile, or the default every channel gets. */
export const audioProfileLabel = (profile: string | null | undefined): string =>
  profile?.trim() || "Standard processing";

export const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

export const hourLabel = (hour: number): string => `${String(hour).padStart(2, "0")}:00`;

/** Minutes past midnight as `HH:MM`, for the time-of-day slider. */
export const minutesToClock = (minutes: number): string =>
  `${String(Math.floor(minutes / 60) % 24).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;

/** `HH:MM` back to minutes past midnight; `null` when it is not a time. */
export function clockToMinutes(value: string | null | undefined): number | null {
  if (!value) return null;
  const match = /^(\d{1,2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const hours = Number(match[1]);
  const mins = Number(match[2]);
  if (hours > 23 || mins > 59) return null;
  return hours * 60 + mins;
}
