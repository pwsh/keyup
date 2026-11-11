import { describe, expect, it } from "vitest";
import { HELP_TEXT as HELP, HELP_AREAS, type HelpId, idsForArea, labelForId } from "./text";

/**
 * Source of every `.ts`/`.tsx` file in the app, keyed by path. `import.meta.glob`
 * runs at build time, so this is the *shipped* source, not a filesystem walk —
 * which means it works identically in CI and needs no node:fs typings.
 */
const SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const APP_SOURCES = Object.entries(SOURCES).filter(
  // The registry itself (`./text.ts`, the text) and its loader (`./help.ts`, whose comments
  // quote `HELP["x.y"]`) are the definitions, not uses.
  ([path]) =>
    !/^\.\/(help|text)\.ts$/.test(path) && !path.endsWith(".test.ts") && !path.endsWith(".test.tsx"),
);

/**
 * Every opening tag for `<Tag …>` in a source file, brace-aware.
 *
 * A naive `<Tag[^>]*>` stops at the first `>` in the tag, which in JSX is very
 * often inside an expression — `tip={a > b ? …}` or a nested element in a prop —
 * so it would report half a tag and miss the prop that was actually there.
 */
function openingTags(source: string, tag: string): string[] {
  const tags: string[] = [];
  const opener = new RegExp(`<${tag}(?=[\\s/>])`, "g");
  let match = opener.exec(source);
  while (match !== null) {
    let depth = 0;
    let index = match.index + tag.length + 1;
    for (; index < source.length; index++) {
      const char = source[index];
      if (char === "{") depth += 1;
      else if (char === "}") depth -= 1;
      else if (char === ">" && depth === 0) break;
    }
    tags.push(source.slice(match.index, index + 1));
    opener.lastIndex = index;
    match = opener.exec(source);
  }
  return tags;
}

/** Every id the app actually asks for: `HELP["x.y"]`, `helpText("x.y")`, `id="x.y"`. */
function referencedIds(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  const patterns = [
    /HELP\[\s*"([a-z][\w.]*)"\s*\]/g,
    /helpText\(\s*"([a-z][\w.]*)"\s*\)/g,
    /\bhelp=\{?"([a-z][\w.]*)"/g,
    /<InfoDot\s+id="([a-z][\w.]*)"/g,
  ];
  for (const [path, source] of APP_SOURCES) {
    for (const pattern of patterns) {
      pattern.lastIndex = 0;
      let match = pattern.exec(source);
      while (match !== null) {
        const id = match[1] as string;
        found.set(id, [...(found.get(id) ?? []), path]);
        match = pattern.exec(source);
      }
    }
  }
  return found;
}

describe("the help registry", () => {
  const ids = Object.keys(HELP) as HelpId[];

  it("has an entry for every id the app references", () => {
    const missing: string[] = [];
    for (const [id, paths] of referencedIds()) {
      if (!(id in HELP)) missing.push(`${id} (used in ${paths.join(", ")})`);
    }
    expect(missing).toEqual([]);
  });

  it("actually references a good share of what it defines", () => {
    // Not every id can be found by a regex (a few are looked up through a map),
    // so this is a floor against the registry rotting into dead text, not an
    // exact-use assertion.
    const used = referencedIds();
    const unused = ids.filter((id) => !used.has(id));
    expect(unused.length).toBeLessThan(ids.length * 0.25);
  });

  it("gives every entry a real sentence", () => {
    for (const id of ids) {
      const text = HELP[id];
      expect(text, id).toBeTypeOf("string");
      // Long enough to say something, short enough to be a tooltip.
      expect(text.length, id).toBeGreaterThan(15);
      expect(text.length, id).toBeLessThan(260);
      expect(text.trim(), id).toBe(text);
      expect(text, id).not.toMatch(/^TODO|TBD|FIXME/i);
      // "Click here" and friends describe the mouse, not the control.
      expect(text.toLowerCase(), id).not.toContain("click here");
      expect(text.charAt(0), id).toBe(text.charAt(0).toUpperCase());
    }
  });

  it("puts every id in exactly one area the help page renders", () => {
    const prefixes = new Set(HELP_AREAS.map((area) => area.prefix));
    for (const id of ids) {
      expect(prefixes.has(id.split(".")[0] as string), id).toBe(true);
    }
    const covered = HELP_AREAS.flatMap((area) => idsForArea(area.prefix));
    expect(new Set(covered).size).toBe(ids.length);
  });

  it("derives a readable label from every id", () => {
    for (const id of ids) {
      const label = labelForId(id);
      expect(label, id).not.toBe("");
      expect(label, id).not.toContain(".");
    }
  });
});

describe("Tip usages", () => {
  it("never passes an empty tooltip", () => {
    const offenders: string[] = [];
    for (const [path, source] of APP_SOURCES) {
      // An explanation that explains nothing. `label=""` is only checked on `Tip`
      // itself: on a `Switch` an absent label is legitimate (a table cell is named
      // by its column header), and the tooltip is what names the control there.
      if (/\btip=(""|\{""\}|\{``\})/.test(source)) offenders.push(path);
      if (openingTags(source, "Tip").some((open) => /\blabel=(""|\{""\}|\{``\})/.test(open))) {
        offenders.push(path);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("gives every Button a tip prop", () => {
    const offenders: string[] = [];
    for (const [path, source] of APP_SOURCES) {
      for (const open of openingTags(source, "Button")) {
        if (!/\btip=/.test(open)) offenders.push(`${path}: ${open.slice(0, 60)}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("gives every Chip, Switch and InfoDot its explanation", () => {
    const offenders: string[] = [];
    for (const [path, source] of APP_SOURCES) {
      for (const tag of ["Chip", "StatusChip", "Switch", "InfoDot"]) {
        for (const open of openingTags(source, tag)) {
          const prop = tag === "InfoDot" ? /\bid=/ : /\btip=/;
          if (!prop.test(open)) offenders.push(`${path}: ${open.slice(0, 80)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
