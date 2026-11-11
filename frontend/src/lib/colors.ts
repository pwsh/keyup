/**
 * Chart color system, taken from the `dataviz` reference palette, so a chart
 * reads the same way here as in other apps built on it.
 *
 * Category hues are assigned by *identity* (a hash of the key), never by rank, so
 * a talkgroup keeps its color when the filter changes what is on screen.
 */

export type ThemeMode = "light" | "dark";

export const STATUS_COLORS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

export const CHART_INK = {
  // The chrome around a chart follows the app's "Console log" tokens (index.css):
  // bezel surface, readout and dial ink, hairline grid. The series hues below do not.
  light: {
    surface: "#ffffff",
    primary: "#15191b",
    secondary: "#5b625f",
    muted: "#5b625f",
    grid: "#e3e6e2",
    axis: "#c8ccc9",
  },
  dark: {
    surface: "#171b1e",
    primary: "#e8ebe6",
    secondary: "#8b938f",
    muted: "#8b938f",
    grid: "#262c2f",
    axis: "#353c40",
  },
} as const;

/** Sequential ramp (one hue, light -> dark) for the activity heatmap. */
export const SEQUENTIAL_BLUE: readonly string[] = [
  "#cde2fb",
  "#b7d3f6",
  "#9ec5f4",
  "#86b6ef",
  "#6da7ec",
  "#5598e7",
  "#3987e5",
  "#2a78d6",
  "#256abf",
  "#1c5cab",
  "#184f95",
  "#104281",
  "#0d366b",
];

/** The palette's neutral, for a series the hue slots do not name. */
export const SERIES_FALLBACK = { light: "#6f6e69", dark: "#a3a29a" } as const;

const HUE_SLOTS: { light: string; dark: string }[] = [
  { light: "#2a78d6", dark: "#3987e5" },
  { light: "#eb6834", dark: "#d95926" },
  { light: "#1baf7a", dark: "#199e70" },
  { light: "#eda100", dark: "#c98500" },
  { light: "#e87ba4", dark: "#d55181" },
  { light: "#008300", dark: "#008300" },
  { light: "#4a3aa7", dark: "#9085e9" },
  { light: "#e34948", dark: "#e66767" },
];

/** The eight categorical hues in order, for a legend or a fixed series list. */
export const seriesPalette = (mode: ThemeMode): string[] => HUE_SLOTS.map((slot) => slot[mode]);

const hash = (value: string): number => {
  let h = 0;
  for (let i = 0; i < value.length; i++) h = (h * 31 + value.charCodeAt(i)) >>> 0;
  return h;
};

/**
 * Stable colors for arbitrary series keys (talkgroup aliases, radios, backends):
 * the slot comes from a hash of the key, so a series keeps its color when other
 * series come and go. "Other" is always the neutral slot.
 */
export function assignSeriesColors(keys: string[], mode: ThemeMode): Map<string, string> {
  const used = new Set<number>();
  const result = new Map<string, string>();
  for (const key of [...keys].sort()) {
    if (key === "Other" || key === "(none)") {
      result.set(key, SERIES_FALLBACK[mode]);
      continue;
    }
    let slot = hash(key) % HUE_SLOTS.length;
    let guard = 0;
    while (used.has(slot) && guard < HUE_SLOTS.length) {
      slot = (slot + 1) % HUE_SLOTS.length;
      guard += 1;
    }
    used.add(slot);
    result.set(key, HUE_SLOTS[slot]?.[mode] ?? SERIES_FALLBACK[mode]);
  }
  return result;
}

/** One stable hue for a single key, without building a whole map. */
export const seriesColor = (key: string, mode: ThemeMode): string =>
  HUE_SLOTS[hash(key) % HUE_SLOTS.length]?.[mode] ?? SERIES_FALLBACK[mode];

/**
 * Sequential blue sampled to `steps` entries, low -> high. The anchor flips per
 * mode so the step nearest zero always recedes toward the surface.
 */
export function sequentialRamp(steps: number, mode: ThemeMode = "light"): string[] {
  const ramp = mode === "dark" ? [...SEQUENTIAL_BLUE].reverse() : [...SEQUENTIAL_BLUE];
  if (steps <= 1) return [ramp[ramp.length - 1] as string];
  return Array.from({ length: steps }, (_, i) => {
    const idx = Math.round((i / (steps - 1)) * (ramp.length - 1));
    return ramp[idx] as string;
  });
}

/**
 * A hue slot per talkgroup category, for the icon that marks what kind of channel
 * a call was on.
 *
 * Deliberate, not hashed: the categories are a fixed list the reader learns, and
 * a stable, *meaningful* color (fire red, law blue, transport green) is worth
 * more than twenty merely distinguishable ones. There are eight hue slots and
 * twenty categories, so related kinds share a slot — corrections and security sit
 * with law, rail and aviation with transit — which is what the icon's glyph is
 * for: the color says which family, the glyph says which member. `other`, and
 * any value the list does not name, take the neutral.
 */
const CATEGORY_HUE_SLOT: Record<string, number> = {
  law: 0,
  corrections: 0,
  security: 0,
  fire: 7,
  ems: 1,
  multi: 6,
  interop: 6,
  pw: 3,
  utility: 3,
  transit: 2,
  rail: 2,
  aviation: 2,
  schools: 4,
  media: 4,
  federal: 5,
  military: 5,
  business: 4,
};

/** The color for one talkgroup category in the current theme; neutral if unknown. */
export function categoryColor(category: string | null | undefined, mode: ThemeMode): string {
  const slot = category ? CATEGORY_HUE_SLOT[category] : undefined;
  return (slot === undefined ? undefined : HUE_SLOTS[slot]?.[mode]) ?? SERIES_FALLBACK[mode];
}

/** Transcript-quality hues; `good` reads as a mark, not as text. */
export const QUALITY_COLORS: Record<string, string> = {
  good: STATUS_COLORS.good,
  suspect: STATUS_COLORS.warning,
  noise: STATUS_COLORS.critical,
  unknown: "#898781",
};
