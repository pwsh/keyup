/**
 * The facet request is debounced; the list is not.
 *
 * Every dimension in the Calls sidebar is an aggregate over the whole `calls` table, and
 * the filter bar fires a change per click — ticking seven categories used to be seven
 * requests and sixty-three heavy queries, of which only the last answer was ever drawn.
 * (It is also what starved the connection pool the afternoon ingestion stopped.) The rows
 * themselves must still move on the click, so only the counts wait.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./client")>();
  return { ...actual, apiGet: vi.fn(actual.apiGet) };
});

import { apiGet } from "./client";
import { useCallFacets, useCalls } from "./hooks";
import type { CallFilterParams } from "./types";

const spy = vi.mocked(apiGet);
const facetCalls = () => spy.mock.calls.filter(([path]) => path === "/calls/facets");

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 700));

describe("useCallFacets", () => {
  // Braces matter: a hook that *returns* a function registers it as a teardown, and
  // `mockClear()` returns the mock — which Vitest would then call as one.
  beforeEach(() => {
    spy.mockClear();
  });

  it("asks straight away on a cold page", async () => {
    renderHook(() => useCallFacets({ category: ["fire"] }), { wrapper });
    await waitFor(() => expect(facetCalls()).toHaveLength(1));
  });

  it("collapses a burst of picker clicks into one request", async () => {
    const { rerender } = renderHook(({ f }: { f: CallFilterParams }) => useCallFacets(f), {
      wrapper,
      initialProps: { f: { category: ["law"] } as CallFilterParams },
    });
    await waitFor(() => expect(facetCalls()).toHaveLength(1));

    for (const categories of [
      ["law", "fire"],
      ["law", "fire", "ems"],
      ["law", "fire", "ems", "multi"],
    ]) {
      rerender({ f: { category: categories } as CallFilterParams });
    }
    await settle();

    // Two in all: the first paint, and the set the reader stopped on. The intermediate
    // two-and three-category selections were never asked for.
    expect(facetCalls()).toHaveLength(2);
    const asked = facetCalls().map(([, params]) => (params as Record<string, unknown>).category);
    expect(asked).toEqual([["law"], ["law", "fire", "ems", "multi"]]);
  });

  it("does not fire again when the filter comes back to where it was", async () => {
    const { rerender } = renderHook(({ f }: { f: CallFilterParams }) => useCallFacets(f), {
      wrapper,
      initialProps: { f: { category: ["law"] } as CallFilterParams },
    });
    await waitFor(() => expect(facetCalls()).toHaveLength(1));
    rerender({ f: { category: ["law"] } as CallFilterParams });
    await settle();
    expect(facetCalls()).toHaveLength(1);
  });
});

describe("useCalls", () => {
  // Braces matter: a hook that *returns* a function registers it as a teardown, and
  // `mockClear()` returns the mock — which Vitest would then call as one.
  beforeEach(() => {
    spy.mockClear();
  });

  it("is not debounced — the rows are what the reader is looking at", async () => {
    const { rerender } = renderHook(({ f }: { f: CallFilterParams }) => useCalls(f), {
      wrapper,
      initialProps: { f: { category: ["law"] } as CallFilterParams },
    });
    await waitFor(() => expect(spy.mock.calls.filter(([p]) => p === "/calls")).toHaveLength(1));
    rerender({ f: { category: ["law", "fire"] } as CallFilterParams });
    await waitFor(() => expect(spy.mock.calls.filter(([p]) => p === "/calls")).toHaveLength(2));
  });
});
