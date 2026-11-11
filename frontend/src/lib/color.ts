/**
 * Contrast arithmetic (WCAG 2.x relative luminance) and the one place a colour that
 * came from the data — a talkgroup's own colour, set in RadioReference or by an
 * operator — is made legible before it is drawn.
 *
 * The chart palette in `colors.ts` is tuned by hand for both themes; a talkgroup colour
 * is not. `#0000ff` on the dark surface is 2.03:1 and a SHUTTLE yellow on the light one
 * about 1.05:1, so a dot in that colour is a mark nobody can see. A mark needs 3:1
 * (WCAG 1.4.11), so `legibleOnSurface` keeps the hue and moves only the lightness, as
 * little as it takes to get there.
 */
import { CHART_INK, categoryColor, type ThemeMode } from "./colors";

type Rgb = [number, number, number];

/** `#rgb` or `#rrggbb` (the `#` optional) to 0–255 channels; `null` for anything else. */
export function parseHex(hex: string): Rgb | null {
  const raw = hex.trim().replace(/^#/, "");
  const full =
    raw.length === 3
      ? raw
          .split("")
          .map((c) => c + c)
          .join("")
      : raw;
  if (!/^[0-9a-f]{6}$/i.test(full)) return null;
  const n = Number.parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const toHex = ([r, g, b]: Rgb): string =>
  `#${[r, g, b]
    .map((c) =>
      Math.round(Math.min(255, Math.max(0, c)))
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;

const channel = (c: number): number => {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};

/** WCAG relative luminance, 0 (black) to 1 (white). */
export function relativeLuminance(hex: string): number {
  const rgb = parseHex(hex);
  if (!rgb) return 0;
  return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
}

/** WCAG contrast ratio between two colours, 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

function rgbToHsl([r, g, b]: Rgb): [number, number, number] {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h =
    max === rn
      ? ((gn - bn) / d + (gn < bn ? 6 : 0)) / 6
      : max === gn
        ? ((bn - rn) / d + 2) / 6
        : ((rn - gn) / d + 4) / 6;
  return [h, s, l];
}

function hslToRgb([h, s, l]: [number, number, number]): Rgb {
  if (s === 0) return [l * 255, l * 255, l * 255];
  const hue = (p: number, q: number, t: number): number => {
    const tt = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
    if (tt < 1 / 6) return p + (q - p) * 6 * tt;
    if (tt < 1 / 2) return q;
    if (tt < 2 / 3) return p + (q - p) * (2 / 3 - tt) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [hue(p, q, h + 1 / 3) * 255, hue(p, q, h) * 255, hue(p, q, h - 1 / 3) * 255];
}

/** The minimum a non-text mark needs against what it sits on (WCAG 1.4.11). */
export const MARK_CONTRAST = 3;

/**
 * `hex`, or the nearest colour of the same hue and saturation that reaches 3:1 against
 * the theme's surface: darker on the light theme, lighter on the dark one. A colour
 * that already clears 3:1 comes back unchanged (lower-cased), and anything that is not
 * a hex colour comes back as given so the caller's own fallback still applies.
 *
 * A binary search on HSL lightness: luminance is monotonic in it for a fixed hue and
 * saturation, so the first lightness that passes is the smallest change that does.
 */
export function legibleOnSurface(
  hex: string,
  theme: ThemeMode,
  surface: string = CHART_INK[theme].surface,
): string {
  const rgb = parseHex(hex);
  if (!rgb) return hex;
  const original = toHex(rgb);
  if (contrastRatio(original, surface) >= MARK_CONTRAST) return original;
  const [h, s, l] = rgbToHsl(rgb);
  // Search between the colour's own lightness and the far end (black or white), which
  // always passes against these surfaces.
  let near = l;
  let far = theme === "light" ? 0 : 1;
  let best = toHex(hslToRgb([h, s, far]));
  for (let i = 0; i < 24; i++) {
    const mid = (near + far) / 2;
    const candidate = toHex(hslToRgb([h, s, mid]));
    if (contrastRatio(candidate, surface) >= MARK_CONTRAST) {
      best = candidate;
      far = mid;
    } else {
      near = mid;
    }
  }
  return best;
}

/**
 * The colour of a call row's key-up edge: the channel's category hue (neutral for an
 * unknown category) — the same tuned palette as the category icon, so law reads blue
 * and fire red on every row — clamped to 3:1 on the theme's surface like every other
 * data colour. A talkgroup's own colour is deliberately not used: operator- and
 * RadioReference-set colours run from `#0000ff` to SHUTTLE yellow, and a list of them
 * read as noise rather than as kinds of traffic.
 */
export function channelEdgeColor(category: string | null | undefined, theme: ThemeMode): string {
  const key = `${theme}:${category ?? ""}`;
  let shown = edgeCache.get(key);
  if (shown === undefined) {
    shown = legibleOnSurface(categoryColor(category, theme), theme);
    edgeCache.set(key, shown);
  }
  return shown;
}

/** Twenty categories, two themes: every row of a list asks for the same few colours. */
const edgeCache = new Map<string, string>();
