import { describe, expect, it } from "vitest";
import { TALKGROUP_CATEGORIES, TIME_SOURCES } from "../api/types";
import {
  agencyLabel,
  CATEGORY_LABELS,
  categoryLabel,
  formatDuration,
  humanizeName,
  TIME_SOURCE_LABELS,
  timeSourceLabel,
} from "./format";

/** Review 2026-10-29 #29: no raw code reaches the page. */
describe("display labels", () => {
  it("has words for every category and time source the API knows", () => {
    for (const category of TALKGROUP_CATEGORIES) expect(CATEGORY_LABELS[category]).toBeTruthy();
    for (const source of TIME_SOURCES) expect(TIME_SOURCE_LABELS[source]).toBeTruthy();
  });

  it("spells out category slugs and falls back gracefully", () => {
    expect(categoryLabel("pw")).toBe("Public works");
    expect(categoryLabel("multi")).toBe("Multi-service");
    expect(categoryLabel(null)).toBe("Uncategorized");
    expect(categoryLabel("search_rescue")).toBe("Search rescue");
  });

  it("names a time source instead of showing its code", () => {
    expect(timeSourceLabel("upload")).toBe("Upload time");
    expect(timeSourceLabel("file_write")).toBe("File time");
    expect(timeSourceLabel(null)).toBe("—");
  });

  it("names an agency from its row, or tidies a code made of words", () => {
    expect(agencyLabel("STATE_UNIVERSITY", { name: "STATE UNIVERSITY" })).toBe("State University");
    expect(agencyLabel("FAIRVIEW", { name: "FAIRVIEW, CITY OF" })).toBe("City of Fairview");
    expect(agencyLabel("XX0010400", { name: "STATE UNIVERSITY DPS" })).toBe("State University DPS");
    expect(agencyLabel("STATE_UNIVERSITY")).toBe("State University");
    expect(agencyLabel("XX0010000")).toBe("XX0010000");
    expect(agencyLabel(null)).toBe("—");
  });

  it("leaves mixed-case names alone and keeps known acronyms", () => {
    expect(humanizeName("Example County")).toBe("Example County");
    expect(humanizeName("SHERIFF'S OFFICE")).toBe("Sheriff's Office");
    expect(humanizeName("ECC")).toBe("ECC");
    expect(humanizeName("vehicle")).toBe("Vehicle");
  });

  it("shows a long total as hours, not 1161:10", () => {
    expect(formatDuration(69_670)).toBe("19h 21m");
    expect(formatDuration(7200)).toBe("2h");
    expect(formatDuration(84)).toBe("1:24");
    expect(formatDuration(12.4)).toBe("12.4s");
  });
});
