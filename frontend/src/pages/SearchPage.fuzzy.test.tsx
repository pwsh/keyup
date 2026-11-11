import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import type { SearchResponse } from "../api/types";
import { renderWithProviders } from "../test/render";
import { SearchPage } from "./SearchPage";

/**
 * The Search page says when its results include spelling variants, and which.
 * The mock corpus has no vocabulary, so `/search` answers with the mock's own hits and
 * whatever `fuzzy`/`total` the API would send in each case.
 */
const realGet = client.apiGet;

function answerSearch(override: Partial<SearchResponse>) {
  return vi.spyOn(client, "apiGet").mockImplementation((path, params, signal) => {
    if (path === "/search") {
      return (realGet(path, params, signal) as Promise<SearchResponse>).then((body) => ({
        ...body,
        ...override,
      }));
    }
    return realGet(path, params, signal);
  });
}

afterEach(() => vi.restoreAllMocks());

describe("Search page fuzzy matching", () => {
  it("asks the API to fall back when nothing matches exactly", async () => {
    const get = answerSearch({ items: [], total: 0 });
    renderWithProviders(<SearchPage />, "/search?q=kelbuck");
    await screen.findByText("Nothing matched");
    const search = get.mock.calls.find(([path]) => path === "/search");
    expect(search?.[1]).toMatchObject({ q: "kelbuck", fallback: true });
    expect(search?.[1]).not.toHaveProperty("fuzzy");
  });

  it("says when it widened by itself, lists the variants, and counts the facets the same way", async () => {
    const get = answerSearch({
      fuzzy: {
        automatic: true,
        widened: true,
        terms: [{ term: "fyre", variants: ["fire", "fyer"] }],
      },
    });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    const notice = (await screen.findByText(/No exact matches for/)).closest("[role=status]") as HTMLElement;
    expect(within(notice).getByText(/Showing fuzzy matches/)).toBeInTheDocument();
    expect(within(notice).getByText("fyre")).toBeInTheDocument();
    expect(within(notice).getByText(/fire, fyer/)).toBeInTheDocument();
    await waitFor(() =>
      expect(
        get.mock.calls.some(([path, params]) => path === "/calls/facets" && params?.fuzzy === true),
      ).toBe(true),
    );
  });

  it("offers widening when there are only a few exact matches, and never widens by itself", async () => {
    const user = userEvent.setup();
    const get = answerSearch({ total: 3 });
    renderWithProviders(<SearchPage />, "/search?q=fire");
    const offer = await screen.findByRole("button", { name: "Include spelling variants" });
    expect(screen.queryByText(/spelling variants:/)).not.toBeInTheDocument();
    await user.click(offer);
    await waitFor(() =>
      expect(get.mock.calls.some(([path, params]) => path === "/search" && params?.fuzzy === true)).toBe(
        true,
      ),
    );
    expect(screen.getByRole("switch", { name: /Spelling variants/ })).toBeChecked();
  });

  it("says what an explicit fuzzy search included and lets the reader go back to exact", async () => {
    const user = userEvent.setup();
    const get = answerSearch({
      fuzzy: { automatic: false, widened: true, terms: [{ term: "fire", variants: ["fyre"] }] },
    });
    renderWithProviders(<SearchPage />, "/search?q=fire&fuzzy=true");
    const notice = (await screen.findByText("Including spelling variants:")).closest(
      "[role=status]",
    ) as HTMLElement;
    await user.click(within(notice).getByRole("button", { name: "Exact matches only" }));
    await waitFor(() => {
      const last = get.mock.calls.filter(([path]) => path === "/search").at(-1);
      expect(last?.[1]).not.toHaveProperty("fuzzy");
    });
  });
});
