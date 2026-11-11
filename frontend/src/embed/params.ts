/**
 * The embed's URL contract (docs/EMBED.md): what `/embed/transcripts?…` accepts, how it is
 * read, and how a set of options is written back into a URL.
 *
 * Deliberately free of imports. The Feeds page uses `embedUrl` to build its `<iframe>`
 * snippet, and the embed bundle itself must not drag the app's client, fixtures or help
 * registry in with it.
 */

/** The `GET /calls` filters the embed passes through, under the Calls page's own names. */
export const FILTER_KEYS = [
  "category",
  "talkgroup",
  "agency",
  "radio",
  "unit",
  "q",
  "system",
  "site",
  "kind",
  "emergency",
] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];

/** The built-in feed an embed starts from when its URL names no filter at all. */
export const DEFAULT_PRESET = "emergency";
export const DEFAULT_MINUTES = 15;
export const MAX_MINUTES = 240;
export const DEFAULT_LIMIT = 100;
export const MAX_LIMIT = 500;

export type EmbedTheme = "light" | "dark" | "auto";

export interface EmbedOptions {
  /** A built-in feed slug (`emergency`, `all`, a category), or null for "just the filters". */
  preset: string | null;
  /** Filter values by name; every value is a string, as it is in a URL. */
  filters: Partial<Record<FilterKey, string[]>>;
  minutes: number;
  /** The filter bar; `controls=0` hides it for a clean embed. */
  controls: boolean;
  theme: EmbedTheme;
  compact: boolean;
  /** How many rows are kept on screen; the oldest fall off the bottom. */
  limit: number;
  /** Show calls before their transcript lands (and ones whose transcript was rejected). */
  showPending: boolean;
}

const FALSE_WORDS = new Set(["0", "false", "no", "off"]);

function flag(params: URLSearchParams, name: string, fallback: boolean): boolean {
  const raw = params.get(name);
  if (raw === null) return fallback;
  return !FALSE_WORDS.has(raw.trim().toLowerCase());
}

function bounded(raw: string | null, fallback: number, max: number): number {
  const value = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(value) || value < 1) return fallback;
  return Math.min(value, max);
}

/** Read an embed URL's query string. Unknown parameters are ignored. */
export function parseEmbedParams(search: string): EmbedOptions {
  const params = new URLSearchParams(search);
  const filters: EmbedOptions["filters"] = {};
  for (const key of FILTER_KEYS) {
    const values = params
      .getAll(key)
      .flatMap((value) => (key === "q" ? [value] : value.split(",")))
      .map((value) => value.trim())
      .filter(Boolean);
    if (values.length > 0) filters[key] = values;
  }
  const named = params.get("preset")?.trim().toLowerCase();
  // An explicit filter replaces the default feed rather than narrowing it: somebody who
  // wrote `?talkgroup=1101` wants that channel, whatever category it is filed under.
  const preset = named ? named : Object.keys(filters).length > 0 ? null : DEFAULT_PRESET;
  const theme = params.get("theme");
  return {
    preset,
    filters,
    minutes: bounded(params.get("minutes"), DEFAULT_MINUTES, MAX_MINUTES),
    controls: flag(params, "controls", true),
    theme: theme === "light" || theme === "dark" ? theme : "auto",
    compact: flag(params, "compact", false),
    limit: bounded(params.get("limit"), DEFAULT_LIMIT, MAX_LIMIT),
    showPending: flag(params, "show_pending", false),
  };
}

/** The query string `GET /api/transcripts/recent` takes for these options. */
export function recentQuery(options: EmbedOptions, withPresets: boolean): string {
  const params = new URLSearchParams();
  if (options.preset) params.set("preset", options.preset);
  for (const key of FILTER_KEYS) {
    for (const value of options.filters[key] ?? []) params.append(key, value);
  }
  params.set("minutes", String(options.minutes));
  params.set("limit", String(options.limit));
  if (options.showPending) params.set("show_pending", "true");
  if (withPresets) params.set("presets", "true");
  return params.toString();
}

/**
 * The embed URL's query string for these options, leaving out every default so a
 * snippet stays short and keeps following the defaults if they ever change.
 */
export function embedSearch(options: EmbedOptions): string {
  const params = new URLSearchParams();
  const hasFilters = FILTER_KEYS.some((key) => (options.filters[key] ?? []).length > 0);
  // The default preset only applies to a URL with no filters, so it must be spelled out
  // whenever there are some — and `all` is how "no preset" survives a round trip then.
  if (options.preset && (options.preset !== DEFAULT_PRESET || hasFilters))
    params.set("preset", options.preset);
  if (!options.preset && !hasFilters) params.set("preset", "all");
  for (const key of FILTER_KEYS) {
    for (const value of options.filters[key] ?? []) params.append(key, value);
  }
  if (options.minutes !== DEFAULT_MINUTES) params.set("minutes", String(options.minutes));
  if (!options.controls) params.set("controls", "0");
  if (options.theme !== "auto") params.set("theme", options.theme);
  if (options.compact) params.set("compact", "1");
  if (options.limit !== DEFAULT_LIMIT) params.set("limit", String(options.limit));
  if (options.showPending) params.set("show_pending", "1");
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The absolute URL of the embed for these options, on `origin`. */
export function embedUrl(origin: string, options: EmbedOptions): string {
  return `${origin.replace(/\/+$/, "")}/embed/transcripts${embedSearch(options)}`;
}

/** The defaults, as an options object — what `/embed/transcripts` with no query shows. */
export function defaultEmbedOptions(): EmbedOptions {
  return parseEmbedParams("");
}
