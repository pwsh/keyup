import { describe, expect, it } from "vitest";
import { contrastRatio, legibleOnSurface, MARK_CONTRAST, parseHex, relativeLuminance } from "./color";
import { CHART_INK } from "./colors";

describe("contrast arithmetic", () => {
  it("parses short and long hex, and refuses anything else", () => {
    expect(parseHex("#fff")).toEqual([255, 255, 255]);
    expect(parseHex("1c5cab")).toEqual([28, 92, 171]);
    expect(parseHex("red")).toBeNull();
    expect(parseHex("#12345")).toBeNull();
  });

  it("matches the WCAG reference points", () => {
    expect(relativeLuminance("#000000")).toBe(0);
    expect(relativeLuminance("#ffffff")).toBeCloseTo(1, 6);
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 6);
    expect(contrastRatio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
    // Symmetric: order does not matter.
    expect(contrastRatio("#ffffff", "#2a78d6")).toBeCloseTo(contrastRatio("#2a78d6", "#ffffff"), 10);
  });
});

describe("legibleOnSurface", () => {
  for (const theme of ["light", "dark"] as const) {
    const surface = CHART_INK[theme].surface;

    it(`leaves a colour that already reaches 3:1 alone (${theme})`, () => {
      const fine = theme === "light" ? "#1c5cab" : "#a8ccf5";
      expect(contrastRatio(fine, surface)).toBeGreaterThanOrEqual(MARK_CONTRAST);
      expect(legibleOnSurface(fine.toUpperCase(), theme)).toBe(fine);
    });

    it(`lifts every failing hue to at least 3:1 (${theme})`, () => {
      for (const hex of [
        "#0000ff",
        "#ffff00",
        "#f5e663",
        "#00ff00",
        "#ff0000",
        "#808080",
        "#ffffff",
        "#000000",
      ]) {
        const shown = legibleOnSurface(hex, theme);
        expect(contrastRatio(shown, surface), `${hex} -> ${shown}`).toBeGreaterThanOrEqual(MARK_CONTRAST);
      }
    });
  }

  it("fixes the two reported cases: pure blue on dark, SHUTTLE yellow on light", () => {
    expect(contrastRatio("#0000ff", CHART_INK.dark.surface)).toBeLessThan(2.1);
    const blue = legibleOnSurface("#0000ff", "dark");
    expect(contrastRatio(blue, CHART_INK.dark.surface)).toBeGreaterThanOrEqual(3);
    // Still blue: the hue is kept, only the lightness moved.
    const [r, g, b] = parseHex(blue) ?? [0, 0, 0];
    expect(b).toBeGreaterThan(r);
    expect(b).toBeGreaterThan(g);

    const yellow = legibleOnSurface("#f5e663", "light");
    expect(contrastRatio(yellow, CHART_INK.light.surface)).toBeGreaterThanOrEqual(3);
    const [yr, yg, yb] = parseHex(yellow) ?? [0, 0, 0];
    expect(yr).toBeGreaterThan(yb);
    expect(yg).toBeGreaterThan(yb);
  });

  it("moves no further than it has to", () => {
    // Just short of the line: a small nudge, not a jump to black.
    const shown = legibleOnSurface("#9a9890", "light");
    expect(contrastRatio(shown, CHART_INK.light.surface)).toBeLessThan(3.2);
  });

  it("hands back anything that is not a hex colour untouched", () => {
    expect(legibleOnSurface("rebeccapurple", "dark")).toBe("rebeccapurple");
  });
});

/**
 * The tokens in index.css, checked against the pairs WCAG holds them to. A token edit
 * that drops a pair under its line fails here rather than in an audit.
 */
// Vitest hands a stylesheet import back empty (CSS is not processed under test), and the
// project carries no node typings: the module names are assembled so TypeScript types the
// imports as `any`, and the shapes are declared here. Vitest runs from the frontend root.
const node = (name: string) => import(/* @vite-ignore */ ["node", name].join(":"));
const fs: { readFileSync(path: string, encoding: "utf8"): string } = await node("fs");
const proc: { cwd(): string } = await node("process");
const css = fs.readFileSync(`${proc.cwd()}/src/index.css`, "utf8");

describe("index.css colour tokens", () => {
  const block = (selector: string): Record<string, string> => {
    const start = css.indexOf(`${selector} {`);
    const body = css.slice(start, css.indexOf("\n}", start));
    return Object.fromEntries(
      [...body.matchAll(/--([\w-]+):\s*(#[0-9a-f]{3,6})/gi)].map((m) => [m[1], m[2]]),
    );
  };
  const themes = { light: block(":root"), dark: block('[data-theme="dark"]') };

  for (const [name, tokens] of Object.entries(themes)) {
    const t = (key: string): string => {
      const value = tokens[key];
      if (value === undefined) throw new Error(`--${key} is missing from the ${name} theme`);
      return value;
    };
    it(`white text clears 4.5:1 on the fill and its hover (${name})`, () => {
      expect(contrastRatio("#ffffff", t("accent-fill"))).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio("#ffffff", t("accent-hover"))).toBeGreaterThanOrEqual(4.5);
    });

    it(`control borders clear 3:1 on every background a control sits on (${name})`, () => {
      for (const bg of ["page", "surface", "surface-2"]) {
        expect(contrastRatio(t("control-border"), t(bg)), bg).toBeGreaterThanOrEqual(3);
      }
    });

    it(`secondary ink on a selected or tinted row clears 4.5:1 (${name})`, () => {
      for (const fill of ["accent-soft", "good-soft", "critical-soft", "warning-soft"]) {
        expect(contrastRatio(t("ink-2"), t(fill)), fill).toBeGreaterThanOrEqual(4.5);
        // The console palette makes --muted the same dial grey, so it clears the line too;
        // the re-pointing rule below stays as the guard for a future, quieter --muted.
        expect(contrastRatio(t("muted"), t(fill)), fill).toBeGreaterThanOrEqual(4.5);
      }
    });

    it(`dial (secondary) and readout text clear 4.5:1 on every surface (${name})`, () => {
      for (const bg of ["page", "surface", "surface-2"]) {
        for (const ink of ["ink", "ink-2", "muted", "accent-ink", "squelch-ink"]) {
          expect(contrastRatio(t(ink), t(bg)), `${ink} on ${bg}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    });

    it(`squelch, the live mark, clears 3:1 on the page and the bezel (${name})`, () => {
      for (const bg of ["page", "surface"]) {
        expect(contrastRatio(t("squelch"), t(bg)), bg).toBeGreaterThanOrEqual(3);
      }
    });
  }

  it("re-points --muted to --ink-2 inside a selected row", () => {
    const rule = css.match(/((?:[.[][^{,]+,\s*)+\[data-selected="true"\])\s*\{\s*--muted:\s*var\(--ink-2\);/);
    expect(rule).not.toBeNull();
    for (const fill of [".bg-accent-soft", ".bg-good-soft", ".bg-critical-soft", ".bg-warning-soft"]) {
      expect(rule?.[1]).toContain(fill);
    }
  });

  it("underlines links in running text but not boxed links", () => {
    const rule = css.match(/:where\(p[^{]*\)\s*a:where\(([^{]*)\)\s*\{([^}]*)\}/);
    expect(rule).not.toBeNull();
    expect(rule?.[1]).toContain('[class*="flex"]');
    expect(rule?.[1]).toContain('[class*="block"]');
    expect(rule?.[2]).toContain("text-decoration-line: underline");
  });
});
