/**
 * A tuner's band list: the downlink ranges it is allowed to record inside its capture span.
 *
 * An antenna and its bandpass may cover several disjoint ranges — a 700/800 stack that is
 * deaf at 900, a filtered 856–866 slice of a wideband capture — and `bands` is how an
 * operator says so from keyup rather than by editing TOML on a headless box. Each entry is
 * either one of {@link BAND_LABELS} or an explicit `"lo-hi"` range in hertz.
 *
 * Everything here works on the raw wire strings rather than a parsed object, because the
 * *reported* list comes from the recorder's own configuration and may say something keyup
 * cannot read (`"ham-2m"`). Those are shown verbatim rather than dropped: the page's job is
 * to say what the tuner is restricted to, and "a band I do not recognize" is still an
 * answer. `keyup` refuses only on the way *in*, where an operator is there to fix it.
 *
 * The label ranges mirror trunkd's `resolve_band`. They are coarse on purpose — they exist
 * to keep a grant off a tuner that physically cannot reach it, not to model a channel plan.
 */

/** The band labels a `bands` entry may name, in the order the editor offers them. */
export const BAND_LABELS = ["vhf", "uhf", "700", "800", "900"] as const;

export type BandLabel = (typeof BAND_LABELS)[number];

/** Each label's downlink range in hertz — trunkd's `resolve_band`, kept in step by hand. */
export const BAND_RANGES: Record<BandLabel, readonly [number, number]> = {
  vhf: [136_000_000, 174_000_000],
  uhf: [380_000_000, 520_000_000],
  700: [758_000_000, 806_000_000],
  800: [806_000_000, 869_000_000],
  900: [896_000_000, 941_000_000],
};

/** An explicit `"lo-hi"` range in hertz, with the spacing trunkd tolerates either side. */
const RANGE_RE = /^(\d+)\s*-\s*(\d+)$/;

function isLabel(value: string): value is BandLabel {
  return (BAND_LABELS as readonly string[]).includes(value);
}

/**
 * The comparison and wire form of one entry: a label lowercased, a range as bare digits
 * either side of one hyphen. Anything keyup cannot read is trimmed and lowercased and left
 * as it is, so two spellings of the same unreadable band still compare equal.
 */
export function canonicalBand(raw: string): string {
  const entry = raw.trim().toLowerCase();
  if (isLabel(entry)) return entry;
  const match = RANGE_RE.exec(entry);
  if (!match) return entry;
  const lo = Number(match[1]);
  const hi = Number(match[2]);
  if (!Number.isSafeInteger(lo) || !Number.isSafeInteger(hi) || lo > hi) return entry;
  return `${lo}-${hi}`;
}

/** The hertz range one entry covers, or `null` when keyup cannot read it. */
export function bandSpan(raw: string): [number, number] | null {
  const entry = raw.trim().toLowerCase();
  if (isLabel(entry)) return [...BAND_RANGES[entry]];
  const match = RANGE_RE.exec(entry);
  if (!match) return null;
  const lo = Number(match[1]);
  const hi = Number(match[2]);
  if (!Number.isSafeInteger(lo) || !Number.isSafeInteger(hi) || lo > hi) return null;
  return [lo, hi];
}

/** Hertz as megahertz with no trailing zeros: 856000000 → "856", 851012500 → "851.0125". */
export function mhz(hz: number): string {
  const text = (hz / 1e6).toFixed(4);
  return text.replace(/\.?0+$/, "");
}

/** One entry as the page shows it: "800" for a label, "851.05–854 MHz" for a range. */
export function formatBand(raw: string): string {
  const entry = raw.trim().toLowerCase();
  if (isLabel(entry)) return entry.toUpperCase();
  const span = bandSpan(entry);
  return span ? `${mhz(span[0])}–${mhz(span[1])} MHz` : raw.trim();
}

/** A whole list for a one-line summary; an empty list is not a list but a state. */
export function formatBands(bands: readonly string[]): string {
  return bands.length === 0 ? "unrestricted" : bands.map(formatBand).join(" · ");
}

/** Whether two lists say the same thing, order and spelling aside. */
export function sameBands(a: readonly string[] | null, b: readonly string[] | null): boolean {
  const left = [...new Set((a ?? []).map(canonicalBand))].sort();
  const right = [...new Set((b ?? []).map(canonicalBand))].sort();
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

/**
 * Whether any entry keyup can read reaches into `[lo, hi]`.
 *
 * `true` for an empty list (no restriction reaches everything) and `true` when nothing in
 * the list is readable — the page warns only about a list it is *sure* misses the span,
 * because trunkd is the one that decides, and guessing on its behalf would put a warning
 * under a list that is going to work.
 */
export function bandsReachSpan(bands: readonly string[], lo: number, hi: number): boolean {
  if (bands.length === 0) return true;
  const spans = bands.map(bandSpan).filter((span): span is [number, number] => span !== null);
  if (spans.length === 0) return true;
  return spans.some(([bandLo, bandHi]) => bandLo <= hi && lo <= bandHi);
}

/** A megahertz figure typed into the editor, as whole hertz — `null` when it is not one. */
export function hzFromMhz(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.round(value * 1e6);
}

/**
 * A `bands` value off the wire as a list of strings, or `null` when it is not one.
 *
 * `null` and `[]` are different instructions in the desired-state contract — "keyup is not
 * managing this tuner's bands" against "this tuner is unrestricted" — so a reader that
 * flattened a missing key to an empty list would silently turn one into the other. Hence a
 * function that distinguishes them, rather than `?? []` at each call site.
 */
export function bandList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const bands = value.filter((entry): entry is string => typeof entry === "string");
  return bands.length === value.length ? bands : null;
}
