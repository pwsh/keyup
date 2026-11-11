import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  activeFilterCount,
  DURATION_MAX,
  defaultFilters,
  parseFilters,
  presetRange,
  serializeFilters,
  toApiParams,
  useApiParams,
} from "./filters";

const parse = (query: string) => parseFilters(new URLSearchParams(query));
const serialize = (query: string) => serializeFilters(parse(query)).toString();

describe("parseFilters", () => {
  it("returns the defaults for an empty URL", () => {
    expect(parse("")).toEqual(defaultFilters());
  });

  it("reads repeated array params", () => {
    const filters = parse("talkgroup=1&talkgroup=6&category=fire&category=ems&dow=1&dow=5");
    expect(filters.talkgroup).toEqual([1, 6]);
    expect(filters.category).toEqual(["fire", "ems"]);
    expect(filters.dow).toEqual([1, 5]);
  });

  it("keeps a time-of-day window only when both ends are present", () => {
    expect(parse("time_from=23:00&time_to=05:00").time_from).toBe("23:00");
    // Half a window would silently hide half the day.
    expect(parse("time_from=23:00").time_from).toBeNull();
    expect(parse("time_to=05:00").time_to).toBeNull();
  });

  it("falls back rather than throwing on junk", () => {
    expect(parse("preset=next-tuesday").preset).toBe(defaultFilters().preset);
    expect(parse("date_from=yesterday&preset=custom").date_from).toBe(defaultFilters().date_from);
    expect(parse("min_duration=abc").min_duration).toBe(0);
    expect(parse("call_type=telepathy").call_type).toBeNull();
    expect(parse("kind=telepathy").kind).toBeNull();
    expect(parse("quality=excellent&quality=noise").quality).toEqual(["noise"]);
    expect(parse("transcript_status=bogus&transcript_status=done").transcript_status).toEqual(["done"]);
    expect(parse("dow=9&dow=2").dow).toEqual([2]);
    expect(parse("time_from=99:99&time_to=05:00").time_from).toBeNull();
  });
});

describe("the kind filter", () => {
  it("defaults to both, because a timeline that hides private traffic is not one", () => {
    expect(defaultFilters().kind).toBeNull();
    expect(toApiParams(defaultFilters()).kind).toBeUndefined();
  });

  it("goes to the API as a repeatable list, the way the endpoint takes it", () => {
    expect(toApiParams(parse("kind=unit")).kind).toEqual(["unit"]);
    expect(toApiParams(parse("kind=group")).kind).toEqual(["group"]);
  });
});

describe("serializeFilters", () => {
  it("writes nothing for a default view", () => {
    expect(serializeFilters(defaultFilters()).toString()).toBe("");
  });

  it("round-trips every filter", () => {
    const query =
      "agency=XX0010100&call_type=patch&category=fire&dow=1&include_duplicates=true&kind=unit&max_duration=30&min_duration=2&preset=7d&q=structure+fire&quality=suspect&radio=3&sort=duration_s&starred=true&tag=review&talkgroup=1&time_from=07%3A00&time_to=19%3A00&transcript_status=done&unit=E1";
    const filters = parse(query);
    // Re-parsing what we wrote must give the identical filter object.
    expect(parseFilters(serializeFilters(filters))).toEqual(filters);
  });

  it("is stable — the same filters always produce the same link", () => {
    expect(serialize("talkgroup=6&talkgroup=1&q=fire")).toBe(serialize("q=fire&talkgroup=6&talkgroup=1"));
  });

  it("only writes explicit dates for a custom range", () => {
    expect(serialize("preset=7d&date_from=2026-01-01&date_to=2026-01-02")).not.toContain("date_from");
    expect(serialize("preset=custom&date_from=2026-01-01&date_to=2026-01-02")).toContain(
      "date_from=2026-01-01",
    );
  });
});

describe("presetRange", () => {
  const now = new Date("2026-08-29T14:30:00Z");

  it("has no range of its own for a custom window", () => {
    expect(presetRange("custom", now)).toBeNull();
  });

  it("starts the rolling preset 24 hours back and leaves its end open", () => {
    const range = presetRange("24h", now) as { from: string; to: string | null };
    expect(now.getTime() - Date.parse(range.from)).toBe(86_400_000);
    expect(range.to).toBeNull();
  });

  it("floors a rolling start to the minute, so every render in that minute agrees", () => {
    const early = presetRange("24h", new Date("2026-08-29T14:30:01.250Z"));
    const late = presetRange("24h", new Date("2026-08-29T14:30:59.999Z"));
    expect(early?.from).toBe("2026-08-28T14:30:00.000Z");
    expect(late).toEqual(early);
  });

  it("ends yesterday's window before today begins", () => {
    const yesterday = presetRange("yesterday", now) as { from: string; to: string };
    const today = presetRange("today", now) as { from: string; to: string | null };
    expect(Date.parse(yesterday.to)).toBeLessThan(Date.parse(today.from));
    expect(today.to).toBeNull();
  });
});

describe("toApiParams", () => {
  it("drops everything that is at its default", () => {
    const params = toApiParams(defaultFilters());
    expect(params.q).toBeUndefined();
    expect(params.talkgroup).toBeUndefined();
    expect(params.starred).toBeUndefined();
    expect(params.max_duration).toBeUndefined();
    // The range and the sort are always sent.
    expect(params.date_from).toBeTruthy();
    expect(params.sort).toBe("-started_at");
  });

  it("turns a preset into a concrete range", () => {
    const now = new Date("2026-08-29T14:30:00Z");
    const params = toApiParams(parse("preset=7d"), {}, now);
    expect(now.getTime() - Date.parse(params.date_from as string)).toBe(7 * 86_400_000);
  });

  // A `date_to` of "the moment the params were built" hid every call recorded after
  // page load — from the list's refetches and from the live filter frame alike.
  it.each(["24h", "today", "7d"])("sends no end for the rolling %s preset", (preset) => {
    const params = toApiParams(parse(`preset=${preset}`), {}, new Date("2026-08-29T14:30:42.123Z"));
    expect(params).not.toHaveProperty("date_to");
    expect(Date.parse(params.date_from as string) % 60_000).toBe(0);
  });

  it("keeps both ends of yesterday, which is a closed window", () => {
    const params = toApiParams(parse("preset=yesterday"), {}, new Date("2026-08-29T14:30:00Z"));
    expect(params.date_from).toBeTruthy();
    expect(params.date_to).toBeTruthy();
  });

  it("passes a custom range through as given", () => {
    const params = toApiParams(parse("preset=custom&date_from=2026-01-01&date_to=2026-01-31"));
    expect(params.date_from).toBe("2026-01-01");
    expect(params.date_to).toBe("2026-01-31");
  });

  it("sends a duration bound only when it narrows anything", () => {
    expect(toApiParams(parse("min_duration=0")).min_duration).toBeUndefined();
    expect(toApiParams(parse(`max_duration=${DURATION_MAX}`)).max_duration).toBeUndefined();
    expect(toApiParams(parse("min_duration=1.5")).min_duration).toBe(1.5);
  });

  it("merges the caller's extra params last", () => {
    expect(toApiParams(defaultFilters(), { page_size: 500 }).page_size).toBe(500);
  });
});

describe("useApiParams", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("holds the params still between renders", () => {
    const filters = parse("preset=24h");
    const { result, rerender } = renderHook(() => useApiParams(filters, { page_size: 500 }));
    const first = result.current.params;
    rerender();
    expect(result.current.params).toBe(first);
    expect(first.page_size).toBe(500);
    expect(first).not.toHaveProperty("date_to");
  });

  it("measures a rolling window from now again on refresh", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-29T14:30:10Z"));
    const filters = parse("preset=24h");
    const { result } = renderHook(() => useApiParams(filters));
    expect(result.current.params.date_from).toBe("2026-08-28T14:30:00.000Z");

    // Same minute: nothing to move, so the caller is told to refetch.
    vi.setSystemTime(new Date("2026-08-29T14:30:50Z"));
    let moved = true;
    act(() => {
      moved = result.current.refresh();
    });
    expect(moved).toBe(false);

    // An hour asleep: the window follows the clock, and a new key fetches by itself.
    vi.setSystemTime(new Date("2026-08-29T15:31:05Z"));
    act(() => {
      moved = result.current.refresh();
    });
    expect(moved).toBe(true);
    expect(result.current.params.date_from).toBe("2026-08-28T15:31:00.000Z");
    expect(result.current.params).not.toHaveProperty("date_to");
  });

  it("never moves a custom range", () => {
    const filters = parse("preset=custom&date_from=2026-01-01&date_to=2026-01-31");
    const { result } = renderHook(() => useApiParams(filters));
    let moved = true;
    act(() => {
      moved = result.current.refresh();
    });
    expect(moved).toBe(false);
    expect(result.current.params.date_from).toBe("2026-01-01");
    expect(result.current.params.date_to).toBe("2026-01-31");
  });
});

describe("activeFilterCount", () => {
  it("is zero for the default view", () => {
    expect(activeFilterCount(defaultFilters())).toBe(0);
  });

  it("counts a duration range once, not twice", () => {
    expect(activeFilterCount(parse("min_duration=2&max_duration=30"))).toBe(1);
  });

  it("counts each distinct filter", () => {
    expect(activeFilterCount(parse("q=fire&starred=true&talkgroup=1&talkgroup=2"))).toBe(3);
  });
});

describe("fuzzy (spelling variants)", () => {
  it("is off by default and absent from a default URL", () => {
    expect(defaultFilters().fuzzy).toBe(false);
    expect(serializeFilters(defaultFilters()).toString()).toBe("");
  });

  it("round-trips through the URL", () => {
    expect(parse("q=melrose&fuzzy=true").fuzzy).toBe(true);
    expect(serialize("q=melrose&fuzzy=true")).toBe("q=melrose&fuzzy=true");
  });

  it("is sent only with a query", () => {
    expect(toApiParams(parse("q=melrose&fuzzy=true")).fuzzy).toBe(true);
    expect(toApiParams(parse("fuzzy=true")).fuzzy).toBeUndefined();
    expect(toApiParams(parse("q=melrose")).fuzzy).toBeUndefined();
  });

  it("is a search option, not a filter, in the badge count", () => {
    expect(activeFilterCount(parse("q=melrose&fuzzy=true"))).toBe(activeFilterCount(parse("q=melrose")));
  });
});
