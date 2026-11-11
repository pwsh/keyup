import { describe, expect, it } from "vitest";
import { type CallSummaryInput, callSummary } from "./callSummary";

const FIXTURE: CallSummaryInput = {
  id: 41,
  started_local: "2026-08-29T14:03:07",
  started_at: "2026-08-29T19:03:07Z",
  system_name: "Example Network",
  site_name: "Central",
  tgid: 1101,
  talkgroup_alias: "EC LAW 1",
  category: "law",
  service_tag: "Law Dispatch",
  frequency_hz: 852137500,
  radio_label: "RC ENG 1",
  radio_raw: "5250041",
  talker_alias: "RC ENG 1",
  duration_s: 12.4,
  transcript_text: "Engine 1 responding to Fairview, en route from station 3.",
};

describe("callSummary", () => {
  it("writes every field a reader needs, in a fixed order", () => {
    expect(callSummary(FIXTURE)).toBe(
      [
        "Call 41 — 2026-08-29 14:03:07",
        "System: Example Network / Central",
        "Talkgroup: 1101 (EC LAW 1)",
        "Category: Law enforcement",
        "Service tag: Law Dispatch",
        "Frequency: 852.1375 MHz",
        "Radio: 5250041 (RC ENG 1)",
        "Talker alias: RC ENG 1",
        "Duration: 12.4s",
        "",
        "Engine 1 responding to Fairview, en route from station 3.",
      ].join("\n"),
    );
  });

  it("drops what is not known rather than printing blanks", () => {
    expect(callSummary({ id: 7, tgid: 1101, duration_s: 1.2 })).toBe(
      ["Call 7", "Talkgroup: 1101", "Duration: 1.2s"].join("\n"),
    );
  });

  it("falls back to the UTC start when there is no local clock", () => {
    const summary = callSummary({ ...FIXTURE, started_local: null });
    expect(summary.startsWith("Call 41 — ")).toBe(true);
    expect(summary).not.toContain("undefined");
  });

  it("names every radio that keyed up, once each, in order", () => {
    const summary = callSummary({
      ...FIXTURE,
      radio_label: "RC ENG 1",
      radio_raw: "5250041",
      sources: [
        { radio_label: "RC ENG 1", src: 5250041 },
        { radio_label: null, src: 5250099 },
        { radio_label: "EC MEDIC 2", src: 5238119 },
      ],
    });
    // The primary and the first source are one radio, not two: they are merged on the id.
    expect(summary).toContain("Radios: 5250041 (RC ENG 1), 5250099, 5238119 (EC MEDIC 2)");
  });

  it("merges a source that knows only the number with a row that knows only the name", () => {
    const summary = callSummary({
      ...FIXTURE,
      radio_raw: null,
      radio_label: "RC ENG 1",
      sources: [{ radio_label: "RC ENG 1", src: 5250041 }],
    });
    expect(summary).toContain("Radio: 5250041 (RC ENG 1)");
  });

  it("does not print the radio id twice when there is no alias for it", () => {
    expect(callSummary({ id: 3, radio_raw: "5250041", radio_label: "5250041" })).toContain("Radio: 5250041");
    expect(callSummary({ id: 3, radio_raw: "5250041", radio_label: "5250041" })).not.toContain("(5250041)");
  });

  it("names the far end of a private call, which has no talkgroup to name instead", () => {
    const summary = callSummary({
      id: 91,
      kind: "unit",
      radio_raw: "5238001",
      radio_label: "5238001",
      target_radio_label: "EC MEDIC 2",
      duration_s: 6,
    });
    expect(summary).toContain("Radio: 5238001");
    expect(summary).toContain("To radio: EC MEDIC 2");
    expect(summary).not.toContain("Talkgroup:");
  });

  it("keeps the transcript in its own paragraph, and omits it when there is none", () => {
    expect(callSummary(FIXTURE)).toContain("\n\nEngine 1 responding");
    expect(callSummary({ ...FIXTURE, transcript_text: "   " })).not.toContain("\n\n");
  });
});
