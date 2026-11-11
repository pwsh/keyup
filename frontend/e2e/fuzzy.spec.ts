import { expect, test } from "@playwright/test";
import { api, expectClean, settle, watchForProblems } from "./helpers";

interface FuzzyAnswer {
  total: number;
  fuzzy: { automatic: boolean; widened: boolean; terms: { term: string; variants: string[] }[] } | null;
}

/** A comma-separated list from the environment, or the fallback. */
function wordList(name: string, fallback: string[]): string[] {
  const raw = process.env[name]?.trim();
  return raw
    ? raw
        .split(",")
        .map((word) => word.trim().toLowerCase())
        .filter(Boolean)
    : fallback;
}

/**
 * Words the archive is likely to hold, each with near spellings in the vocabulary — local
 * place names work best, so set E2E_FUZZY_WORDS to a few of yours. Not everyday words: a
 * word in 10 000 calls or more is searched exactly even with the switch on (`COMMON` in
 * app/services/fuzzy.py), so "street" has nothing to list.
 */
const WORDS = wordList("E2E_FUZZY_WORDS", ["boulevard", "township", "overpass", "apartments"]);

/**
 * Misspellings that should match nothing exactly and something fuzzily
 * (E2E_FUZZY_MISSPELLINGS; misspell the local place names in E2E_FUZZY_WORDS).
 */
const MISSPELLINGS = wordList("E2E_FUZZY_MISSPELLINGS", ["boulevrd", "townshp", "overpas", "apartmints"]);

/** The whole archive: the page's default is the last 24 h, the API's probes below have no window. */
const ALL_TIME = "preset=custom&date_from=2000-01-01&date_to=2100-01-01";

/**
 * The Search page's "Spelling variants" switch widens each 4+-letter word to its near
 * spellings in the archive's vocabulary, says so, and lists them; a search with no exact
 * match widens by itself. The unit tests fake the API's answer; this runs the real one.
 */
test.describe("search with spelling variants", () => {
  test("the switch widens the search, lists the variants and puts them back when it is off", async ({
    page,
  }) => {
    const problems = watchForProblems(page, { allow401: true });

    // A word the archive has near spellings of, straight from the API.
    let word: string | null = null;
    let variants: string[] = [];
    for (const candidate of WORDS) {
      const answer = await api<FuzzyAnswer>(
        page,
        `/search?q=${candidate}&fuzzy=true&page_size=1&${ALL_TIME}`,
      );
      const widened = answer.fuzzy?.terms.flatMap((term) => term.variants) ?? [];
      if (answer.fuzzy?.widened && widened.length > 0) {
        word = candidate;
        variants = widened;
        break;
      }
    }
    test.skip(word === null, "no candidate word has spelling variants in this archive");

    await page.goto(`/search?q=${word}&${ALL_TIME}`);
    await settle(page);
    const toggle = page.getByRole("switch", { name: "Spelling variants" });
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(page.getByText(/[\d,]+ matching calls/)).toBeVisible();
    await expect(page.getByRole("list", { name: "Spelling variants searched" })).toHaveCount(0);

    await toggle.click();
    await expect(page).toHaveURL(/[?&]fuzzy=true/);
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    const notice = page.getByRole("status").filter({ hasText: "Including spelling variants" });
    await expect(notice).toBeVisible();
    // The notice names the variants the API searched — the first one at least, whichever
    // order it prints them in.
    const listed = page.getByRole("list", { name: "Spelling variants searched" });
    await expect(listed).toContainText(variants[0] as string);
    await expect(page.getByText(/[\d,]+ matching calls/)).toBeVisible();

    // Off again: the URL, the switch and the notice all go back.
    await toggle.click();
    await expect(page).not.toHaveURL(/fuzzy=/);
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(listed).toHaveCount(0);
    expectClean(problems, "/search fuzzy switch");
  });

  test("a word with no exact match widens by itself and says so", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });

    let word: string | null = null;
    for (const candidate of MISSPELLINGS) {
      const exact = await api<FuzzyAnswer>(page, `/search?q=${candidate}&page_size=1&${ALL_TIME}`);
      if (exact.total !== 0) continue;
      const answer = await api<FuzzyAnswer>(
        page,
        `/search?q=${candidate}&fallback=true&page_size=1&${ALL_TIME}`,
      );
      if (answer.fuzzy?.automatic && answer.total > 0) {
        word = candidate;
        break;
      }
    }
    test.skip(word === null, "none of the candidate misspellings widens to a match in this archive");

    await page.goto(`/search?q=${word}&${ALL_TIME}`);
    await settle(page);
    const notice = page.getByRole("status").filter({ hasText: "No exact matches for" });
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(word as string);
    await expect(notice).toContainText("Showing fuzzy matches");
    await expect(page.getByText(/[\d,]+ matching calls/)).toBeVisible();
    // Widening by itself is not the switch being turned on: the URL stays the reader's.
    await expect(page).not.toHaveURL(/fuzzy=/);
    expectClean(problems, "/search fallback");
  });
});
