import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueryParams } from "../api/client";
import * as client from "../api/client";
import type { SearchHit, SearchResponse } from "../api/types";
import { renderWithProviders } from "../test/render";
import { SearchPage } from "./SearchPage";

/**
 * The Search page loads more hits as the reader nears the end of the list (as Calls does),
 * by `page` — a ranked search has no cursor — and keeps one question across the pages.
 */
const realGet = client.apiGet;
let template: SearchHit["call"];
let scrollTop = 0;
let scrollHeight = 5000;

beforeEach(async () => {
  const body = (await realGet("/search", { page_size: 1 })) as SearchResponse;
  template = (body.items[0] as SearchHit).call;
  scrollTop = 0;
  scrollHeight = 5000;
  // jsdom lays nothing out; give the list a viewport so "near the end" means something.
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(500);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(() => scrollHeight);
  vi.spyOn(HTMLElement.prototype, "scrollTop", "get").mockImplementation(() => scrollTop);
});

afterEach(() => vi.restoreAllMocks());

interface Corpus {
  total: number;
  totalCapped?: boolean;
  /** The ids on a page; defaults to a contiguous run. */
  ids?: (page: number) => number[];
  fuzzy?: (params: QueryParams) => SearchResponse["fuzzy"];
}

function answer(corpus: Corpus) {
  return vi.spyOn(client, "apiGet").mockImplementation((path, params, signal) => {
    if (path !== "/search") return realGet(path, params, signal);
    const p = params ?? {};
    const pageNo = Number(p.page ?? 1);
    const size = Number(p.page_size ?? 50);
    const start = (pageNo - 1) * size;
    const ids =
      corpus.ids?.(pageNo) ??
      Array.from({ length: Math.max(0, Math.min(size, corpus.total - start)) }, (_, i) => start + i + 1);
    const body: SearchResponse = {
      items: ids.map((id) => ({ call: { ...template, id }, highlight: `hit ${id}`, rank: 1 })),
      total: corpus.total,
      total_capped: corpus.totalCapped ?? false,
      page: pageNo,
      page_size: size,
      facets: null,
      fuzzy: corpus.fuzzy?.(p) ?? null,
    };
    return Promise.resolve(body as never);
  });
}

const searches = (get: ReturnType<typeof answer>) =>
  get.mock.calls.filter(([path]) => path === "/search").map(([, params]) => params ?? {});

describe("Search page scrolling", () => {
  it("asks for page 2 only once the reader nears the end", async () => {
    const get = answer({ total: 250 });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    expect(await screen.findByText("100 of 250 loaded")).toBeInTheDocument();
    expect(searches(get)).toHaveLength(1);
    expect(searches(get)[0]).not.toHaveProperty("page");

    const list = screen.getByText("hit 1").closest("ul") as HTMLElement;
    scrollTop = 4000; // 500 px from the end
    fireEvent.scroll(list);
    expect(await screen.findByText("200 of 250 loaded")).toBeInTheDocument();
    expect(searches(get)[1]).toMatchObject({ q: "fire", page: 2, page_size: 100, fallback: true });
    expect(searches(get)[1]).not.toHaveProperty("cursor");
  });

  it("shows each call once when an arrival shifts a row across the page boundary", async () => {
    scrollHeight = 600; // everything loaded is near the end
    answer({ total: 199, ids: (n) => (n === 1 ? range(1, 100) : range(100, 100)) });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    expect(await screen.findByText("199 of 199 loaded")).toBeInTheDocument();
    expect(screen.getAllByText("hit 100")).toHaveLength(1);
  });

  it("keeps a search that widened by itself widened on every later page", async () => {
    scrollHeight = 600;
    const widened = { automatic: true, widened: true, terms: [{ term: "kelbuck", variants: ["kelbuk"] }] };
    const get = answer({
      total: 250,
      // The API's own rule: an explicit fuzzy search is not automatic.
      fuzzy: (p) => (p.fuzzy ? { ...widened, automatic: false } : widened),
    });
    renderWithProviders(<SearchPage />, "/search?q=kelbuck");
    expect(await screen.findByText("250 of 250 loaded")).toBeInTheDocument();
    const sent = searches(get);
    expect(sent).toHaveLength(3);
    expect(sent[0]).not.toHaveProperty("fuzzy");
    expect(sent[1]).toMatchObject({ page: 2, fuzzy: true });
    expect(sent[2]).toMatchObject({ page: 3, fuzzy: true });
    // The notice is page 1's: still "no exact matches", not page 2's explicit search.
    expect(screen.getByText(/No exact matches for/)).toBeInTheDocument();
  });

  it("stops at 1,000 rows and says so", async () => {
    scrollHeight = 600;
    const get = answer({ total: 50_000, totalCapped: true });
    renderWithProviders(<SearchPage />, "/search?q=fire&preset=all");
    expect(
      await screen.findByText("1,000 of 50,000+ loaded", undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Showing the best 1,000 matches/)).toBeInTheDocument();
    const sent = searches(get);
    expect(sent).toHaveLength(10);
    expect(sent.map((p) => p.page ?? 1)).toEqual(range(1, 10));
    // All time sends no dates at all.
    for (const params of sent) expect(params).not.toHaveProperty("date_from");
  });

  it("searches the last 30 days by default", async () => {
    const get = answer({ total: 5 });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    await screen.findByText("5 of 5 loaded");
    const from = Date.parse(String(searches(get)[0]?.date_from));
    const days = (Date.now() - from) / 86_400_000;
    expect(days).toBeGreaterThan(29.99);
    expect(days).toBeLessThan(30.01);
    expect(screen.getByRole("button", { name: "Last 30 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("switches the range from the chips", async () => {
    const get = answer({ total: 5 });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    await screen.findByText("5 of 5 loaded");
    fireEvent.click(screen.getByRole("button", { name: "All time" }));
    await waitFor(() => expect(searches(get).at(-1)).not.toHaveProperty("date_from"));
    expect(screen.getByRole("button", { name: "All time" })).toHaveAttribute("aria-pressed", "true");
  });
});

function range(from: number, count: number): number[] {
  return Array.from({ length: count }, (_, i) => from + i);
}
