import { describe, expect, it } from "vitest";
import { flattenSearchPages, nextSearchPageParam, SEARCH_LIST_CAP } from "./searchScroll";
import type { SearchHit, SearchResponse } from "./types";

const hit = (id: number): SearchHit => ({ call: { id } as SearchHit["call"], highlight: "", rank: 1 });

function page(n: number, ids: number[], over: Partial<SearchResponse> = {}): SearchResponse {
  return {
    items: ids.map(hit),
    total: 250,
    total_capped: false,
    page: n,
    page_size: 100,
    facets: null,
    fuzzy: null,
    ...over,
  };
}

const range = (from: number, count: number) => Array.from({ length: count }, (_, i) => from + i);
const WIDENED = { automatic: true, widened: true, terms: [{ term: "kelbuck", variants: ["kelbuk"] }] };

describe("nextSearchPageParam", () => {
  it("asks for the next page after a full one", () => {
    const first = page(1, range(1, 100));
    expect(nextSearchPageParam(first, [first])).toEqual({ page: 2 });
  });

  it("stops after a short page, or when the pages reach the total", () => {
    const short = page(3, range(201, 50));
    expect(nextSearchPageParam(short, [page(1, []), page(2, []), short])).toBeUndefined();
    const exact = page(1, range(1, 100), { total: 100 });
    expect(nextSearchPageParam(exact, [exact])).toBeUndefined();
  });

  it("keeps going past a capped count", () => {
    const first = page(1, range(1, 100), { total: 100, total_capped: true });
    expect(nextSearchPageParam(first, [first])).toEqual({ page: 2 });
  });

  it("stops at the list's cap", () => {
    const pages = range(1, 10).map((n) =>
      page(n, range(n * 100, 100), { total: 50_000, total_capped: true }),
    );
    expect(SEARCH_LIST_CAP).toBe(1000);
    expect(nextSearchPageParam(pages[8] as SearchResponse, pages.slice(0, 9))).toEqual({ page: 10 });
    expect(nextSearchPageParam(pages[9] as SearchResponse, pages)).toBeUndefined();
  });

  it("asks later pages for the widened search page 1 fell back to", () => {
    const first = page(1, range(1, 100), { fuzzy: WIDENED });
    expect(nextSearchPageParam(first, [first])).toEqual({ page: 2, fuzzy: true });
    // Page 2 itself reports an explicit fuzzy search; page 3 must still follow page 1.
    const second = page(2, range(101, 100), { fuzzy: { ...WIDENED, automatic: false } });
    expect(nextSearchPageParam(second, [first, second])).toEqual({ page: 3, fuzzy: true });
  });

  it("does not widen later pages of an exact search, or of a fallback that found no variant", () => {
    const exact = page(1, range(1, 100));
    expect(nextSearchPageParam(exact, [exact])).not.toHaveProperty("fuzzy");
    const none = page(1, range(1, 100), { fuzzy: { ...WIDENED, widened: false } });
    expect(nextSearchPageParam(none, [none])).not.toHaveProperty("fuzzy");
  });
});

describe("flattenSearchPages", () => {
  it("keeps each call once, in first-seen order", () => {
    // An arrival shifted call 100 from page 1 onto page 2.
    const list = flattenSearchPages([page(1, range(1, 100)), page(2, range(100, 100))]);
    expect(list.hits.map((h) => h.call.id)).toEqual(range(1, 199));
    expect(list.total).toBe(250);
    expect(list.capped).toBe(false);
  });

  it("holds at most the cap, and says so when more are left", () => {
    const pages = range(1, 11).map((n) => page(n, range((n - 1) * 100 + 1, 100), { total: 5000 }));
    const list = flattenSearchPages(pages);
    expect(list.hits).toHaveLength(SEARCH_LIST_CAP);
    expect(list.capped).toBe(true);
  });

  it("is not capped when the cap is exactly everything", () => {
    const pages = range(1, 10).map((n) => page(n, range((n - 1) * 100 + 1, 100), { total: 1000 }));
    expect(flattenSearchPages(pages).capped).toBe(false);
  });

  it("carries the first page's capped count", () => {
    const list = flattenSearchPages([page(1, range(1, 100), { total: 50_000, total_capped: true })]);
    expect(list).toMatchObject({ total: 50_000, totalCapped: true, capped: false });
  });

  it("is empty before anything loads", () => {
    expect(flattenSearchPages(undefined)).toEqual({ hits: [], total: 0, totalCapped: false, capped: false });
  });
});
