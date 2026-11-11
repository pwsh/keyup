import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";
import {
  CALLS_PRESETS,
  DATE_PRESETS,
  type DatePreset,
  DEFAULT_PRESET,
  defaultFilters,
  PRESET_LABELS,
  parseFilters,
  presetRange,
  SEARCH_DEFAULT_PRESET,
  SEARCH_PRESETS,
  serializeFilters,
  toApiParams,
  useCallFilters,
} from "./filters";

const DAY = 86_400_000;
const now = new Date("2026-10-26T14:30:42.123Z");
const minute = Date.parse("2026-10-26T14:30:00.000Z");

describe("the longer presets", () => {
  it("are known to every page and labelled", () => {
    expect(DATE_PRESETS).toEqual(expect.arrayContaining(["30d", "1y", "all"]));
    expect(PRESET_LABELS["30d"]).toBe("Last 30 days");
    expect(PRESET_LABELS["1y"]).toBe("Last year");
    expect(PRESET_LABELS.all).toBe("All time");
  });

  it.each([
    ["30d", 30],
    ["1y", 365],
  ] as const)("roll %s back from the minute, with no end", (preset, days) => {
    const range = presetRange(preset, now) as { from: string; to: string | null };
    expect(Date.parse(range.from)).toBe(minute - days * DAY);
    expect(range.to).toBeNull();
    // Floored, so every render inside the minute is the same query key.
    expect(presetRange(preset, new Date("2026-10-26T14:30:59.999Z"))).toEqual(range);
    const params = toApiParams(parseFilters(new URLSearchParams(`preset=${preset}`)), {}, now);
    expect(params.date_from).toBe(range.from);
    expect(params).not.toHaveProperty("date_to");
  });

  it("sends no dates at all for all time", () => {
    expect(presetRange("all", now)).toBeNull();
    const params = toApiParams(parseFilters(new URLSearchParams("q=fire&preset=all")), {}, now);
    expect(params).not.toHaveProperty("date_from");
    expect(params).not.toHaveProperty("date_to");
    expect(params.q).toBe("fire");
  });

  it("still sends a custom range exactly as typed", () => {
    const params = toApiParams(
      parseFilters(new URLSearchParams("preset=custom&date_from=2025-01-01&date_to=2025-01-31")),
    );
    expect(params.date_from).toBe("2025-01-01");
    expect(params.date_to).toBe("2025-01-31");
  });
});

describe("per-page defaults", () => {
  const search = defaultFilters(SEARCH_DEFAULT_PRESET);

  it("Search defaults to 30 days, everything else stays at 24 hours", () => {
    expect(SEARCH_DEFAULT_PRESET).toBe("30d");
    expect(DEFAULT_PRESET).toBe("24h");
    expect(defaultFilters().preset).toBe("24h");
    expect(parseFilters(new URLSearchParams("q=fire")).preset).toBe("24h");
    expect(parseFilters(new URLSearchParams("q=fire"), search).preset).toBe("30d");
  });

  it.each(["today", "yesterday", "24h", "7d", "30d", "1y", "all"])(
    "round-trips preset=%s on both pages",
    (preset) => {
      for (const defaults of [defaultFilters(), search]) {
        const filters = parseFilters(new URLSearchParams(`q=fire&preset=${preset}`), defaults);
        expect(filters.preset).toBe(preset);
        const again = parseFilters(serializeFilters(filters, defaults), defaults);
        expect(again).toEqual(filters);
      }
    },
  );

  it("keeps each page's own default out of its URL, and writes the other one", () => {
    const thirty = parseFilters(new URLSearchParams("q=fire&preset=30d"), search);
    expect(serializeFilters(thirty, search).toString()).toBe("q=fire");
    expect(serializeFilters(thirty).toString()).toBe("q=fire&preset=30d");
    const day = parseFilters(new URLSearchParams("q=fire&preset=24h"), search);
    expect(serializeFilters(day, search).toString()).toBe("q=fire&preset=24h");
    expect(serializeFilters(day).toString()).toBe("q=fire");
  });

  it("old links keep their meaning: an explicit preset wins on any page", () => {
    for (const preset of ["24h", "7d", "today", "yesterday"]) {
      expect(parseFilters(new URLSearchParams(`preset=${preset}`), search).preset).toBe(preset);
      expect(parseFilters(new URLSearchParams(`preset=${preset}`)).preset).toBe(preset);
    }
  });

  it("offers the long ranges on Search and not in the Calls rail", () => {
    expect(SEARCH_PRESETS).toEqual(expect.arrayContaining(["30d", "1y", "all"]));
    expect(CALLS_PRESETS).toEqual(["today", "yesterday", "24h", "7d"]);
  });
});

describe("useCallFilters with a page default", () => {
  function setup(route: string, preset: DatePreset = DEFAULT_PRESET) {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(MemoryRouter, { initialEntries: [route] }, children);
    return renderHook(() => ({ controller: useCallFilters(preset), location: useLocation() }), { wrapper });
  }

  it("reads a bare Search URL as 30 days and does not count it as a filter", () => {
    const { result } = setup("/search?q=fire", "30d");
    expect(result.current.controller.filters.preset).toBe("30d");
    expect(result.current.controller.activeCount).toBe(1); // just the query
    expect(toApiParams(result.current.controller.filters, {}, now).date_from).toBe(
      new Date(minute - 30 * DAY).toISOString(),
    );
  });

  it("reads a bare Calls URL as 24 hours, as before", () => {
    const { result } = setup("/calls");
    expect(result.current.controller.filters.preset).toBe("24h");
    expect(result.current.controller.activeCount).toBe(0);
  });

  it("writes the Search range into the URL only when it is not the default", () => {
    const { result } = setup("/search?q=fire", "30d");
    act(() => result.current.controller.patch({ preset: "all" }));
    expect(result.current.location.search).toBe("?q=fire&preset=all");
    expect(result.current.controller.filters.preset).toBe("all");
    act(() => result.current.controller.patch({ preset: "30d" }));
    expect(result.current.location.search).toBe("?q=fire");
    act(() => result.current.controller.patch({ preset: "24h" }));
    expect(result.current.location.search).toBe("?q=fire&preset=24h");
    act(() => result.current.controller.reset());
    expect(result.current.location.search).toBe("");
    expect(result.current.controller.filters.preset).toBe("30d");
  });
});
