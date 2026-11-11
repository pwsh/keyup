import { describe, expect, it } from "vitest";
import { SETTINGS } from "../../api/mock/dataset";
import { DEDICATED_GROUPS } from "./SettingsTab";
import {
  GROUP_BLURB,
  GROUP_ORDER,
  layoutGroup,
  layoutSettings,
  mappedKeys,
  OTHER_SUBSECTION,
  SUBSECTIONS,
  settingMatches,
} from "./settingsLayout";

const HIDDEN = new Set(Object.keys(DEDICATED_GROUPS));
/** The settings this tab actually renders: the mock table minus the groups with a page of their own. */
const SHOWN = SETTINGS.filter((setting) => !HIDDEN.has(setting.group));

/**
 * The map is hand-written, which is the only way to say "these four knobs answer one
 * question" — and the price of hand-writing it is that a rename in `SETTINGS_SPEC` leaves
 * a key here pointing at nothing, silently, because an unmatched key is simply skipped.
 * These cases are that safety net, in both directions.
 */
describe("the settings layout map", () => {
  it("names only keys that exist", () => {
    const known = new Set(SETTINGS.map((setting) => setting.key));
    expect(mappedKeys().filter((key) => !known.has(key))).toEqual([]);
  });

  it("names each key exactly once", () => {
    const keys = mappedKeys();
    const twice = keys.filter((key, index) => keys.indexOf(key) !== index);
    expect(twice).toEqual([]);
  });

  it("places every setting the tab renders, so nothing sits in Other", () => {
    const unplaced: string[] = [];
    for (const { group, subsections } of layoutSettings(SETTINGS, HIDDEN)) {
      for (const section of subsections) {
        if (section.title !== OTHER_SUBSECTION) continue;
        unplaced.push(...section.items.map((item) => `${group}/${item.key}`));
      }
    }
    expect(unplaced).toEqual([]);
  });

  it("sub-divides any group big enough to need it", () => {
    // A one-setting group ("Access") is a heading over a heading and is left alone; the
    // moment a group is more than a glance, it owes the reader sub-sections.
    const wanting = [...new Set(SHOWN.map((setting) => setting.group))].filter(
      (group) => !(group in SUBSECTIONS) && SHOWN.filter((setting) => setting.group === group).length > 2,
    );
    expect(wanting).toEqual([]);
  });

  it("gives every group it renders a name in the order and a blurb", () => {
    const groups = [...new Set(SHOWN.map((setting) => setting.group))];
    expect(groups.filter((group) => !GROUP_ORDER.includes(group))).toEqual([]);
    expect(groups.filter((group) => !(group in GROUP_BLURB))).toEqual([]);
  });

  it("only describes groups that exist, so the blurbs cannot rot", () => {
    const groups = new Set(SETTINGS.map((setting) => setting.group));
    expect(Object.keys(GROUP_BLURB).filter((group) => !groups.has(group))).toEqual([]);
    expect(GROUP_ORDER.filter((group) => !groups.has(group))).toEqual([]);
  });

  it("keeps a key nobody mapped rather than dropping it", () => {
    const known = SHOWN.filter((setting) => setting.key === "transcribe_default");
    const invented = { ...(known[0] as (typeof SHOWN)[number]), key: "brand_new_knob" };
    const sections = layoutGroup("Transcription", [...known, invented]);
    expect(sections.map((section) => section.title)).toEqual(["Policy", OTHER_SUBSECTION]);
    expect(sections.at(-1)?.items.map((item) => item.key)).toEqual(["brand_new_knob"]);
  });

  it("leaves a group nobody mapped as one untitled run of fields", () => {
    const invented = { ...(SHOWN[0] as (typeof SHOWN)[number]), group: "Brand new group" };
    expect(layoutGroup("Brand new group", [invented])).toEqual([{ title: "", items: [invented] }]);
  });

  it("skips a sub-section whose keys this deployment does not have", () => {
    const policyOnly = SHOWN.filter((setting) => setting.key === "transcribe_default");
    expect(layoutGroup("Transcription", policyOnly).map((section) => section.title)).toEqual(["Policy"]);
  });
});

describe("the settings layout", () => {
  it("hides every group that has a page of its own", () => {
    const groups = layoutSettings(SETTINGS, HIDDEN).map((entry) => entry.group);
    expect(groups).not.toContain("Export");
    expect(groups).not.toContain("Audio");
    expect(groups).not.toContain("Feed stats");
  });

  it("stacks the groups in the order the map declares", () => {
    const groups = layoutSettings(SETTINGS, HIDDEN).map((entry) => entry.group);
    expect(groups).toEqual(GROUP_ORDER.filter((group) => groups.includes(group)));
  });

  it("sorts a group nobody ordered after the ones somebody did", () => {
    const stray = { ...(SHOWN[0] as (typeof SHOWN)[number]), key: "zzz", group: "Zebra" };
    const groups = layoutSettings([...SETTINGS, stray], HIDDEN).map((entry) => entry.group);
    expect(groups.at(-1)).toBe("Zebra");
  });

  it("counts the settings in each group, not the sub-sections", () => {
    const ingest = layoutSettings(SETTINGS, HIDDEN).find((entry) => entry.group === "Ingest");
    expect(ingest?.count).toBe(SHOWN.filter((setting) => setting.group === "Ingest").length);
  });
});

describe("the settings filter", () => {
  const bySearch = (query: string) => SHOWN.filter((setting) => settingMatches(setting, query));

  it("matches nothing away when it is empty", () => {
    expect(bySearch("   ")).toHaveLength(SHOWN.length);
  });

  it("matches on the key", () => {
    expect(bySearch("reconcile_min_overlap").map((setting) => setting.key)).toEqual([
      "reconcile_min_overlap",
    ]);
  });

  it("matches on the label, case-insensitively", () => {
    expect(bySearch("keep days").map((setting) => setting.key)).toContain("retention_keep_days");
  });

  it("matches on the description, which is where the memorable words are", () => {
    // Nothing is *labelled* "hallucinated"; it is the word an operator remembers.
    expect(bySearch("hallucinated").map((setting) => setting.key)).toEqual(["stt_vad"]);
  });
});
