import { expect, test } from "@playwright/test";
import { type CallRowShape, callRows, expectClean, pageResponse, settle, watchForProblems } from "./helpers";

const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

interface Page<T> {
  items: T[];
  total: number;
  total_capped: boolean;
}

interface Bucket {
  key: string;
  label: string;
  count: number;
}

/**
 * Yesterday, as the page's own preset: a closed window, so nothing recorded while a test
 * runs can land in it. With a rolling window the busiest talkgroup gains calls between the
 * test's question and the page's, and a call recorded mid-test becomes the newest row.
 */
const CLOSED = "preset=yesterday";

/** The first page of the calls list, as the Calls page asks for it (not the next page, not
 * the one-row "how many are hidden" count the Audio only toggle makes). */
const firstCallsPage = (url: URL): boolean =>
  url.pathname === "/api/calls" &&
  !url.searchParams.has("cursor") &&
  url.searchParams.get("has_audio") !== "false" &&
  Number(url.searchParams.get("page_size")) > 1;

const facetsRequest = (url: URL): boolean => url.pathname === "/api/calls/facets";

/**
 * Opens the calls page on the closed window and returns its busiest talkgroup there, from
 * the facet counts the page itself fetched. Skips when the window is empty.
 */
async function busiestTalkgroupYesterday(page: import("@playwright/test").Page): Promise<Bucket> {
  const facets = await pageResponse<{ talkgroup: Bucket[] }>(page, facetsRequest, () =>
    page.goto(`/calls?${CLOSED}`),
  );
  const top = facets.talkgroup[0];
  test.skip(!top, "nothing was recorded yesterday");
  return top as Bucket;
}

test.describe("the calls page, against the real archive", () => {
  test("lists real calls with times, talkgroups and transcripts", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const expected = await pageResponse<Page<CallRowShape>>(page, firstCallsPage, () =>
      page.goto(`/calls?${CLOSED}`),
    );
    test.skip(expected.items.length === 0, "nothing was recorded yesterday");
    const newest = expected.items[0] as CallRowShape;
    await settle(page);

    // The count in the toolbar is the API's own total, formatted.
    await expect(
      page.getByText(`${expected.total.toLocaleString("en-US")} calls`, { exact: true }).first(),
    ).toBeVisible();
    const rows = callRows(page);
    if (expected.items.length > 20) expect(await rows.count()).toBeGreaterThan(20);
    else await expect(rows).toHaveCount(expected.items.length);

    // The newest call the API answered with is the first row.
    const label = newest.talkgroup_alias ?? `TG ${newest.tgid}`;
    await expect(rows.first()).toContainText(label);
    if (newest.transcript_text) {
      await expect(rows.first()).toContainText(newest.transcript_text.slice(0, 24));
    }
    expectClean(problems, "/calls");
  });

  test("filtering by talkgroup narrows the rows to that talkgroup", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const top = await busiestTalkgroupYesterday(page);

    const listed = await pageResponse<Page<CallRowShape>>(page, firstCallsPage, () =>
      page.goto(`/calls?${CLOSED}&talkgroup=${top.key}`),
    );
    await settle(page);

    const rows = callRows(page);
    const count = await rows.count();
    expect(count, "filtering to the busiest talkgroup returned nothing").toBeGreaterThan(0);
    // Every visible row belongs to the talkgroup that was filtered to.
    for (const row of (await rows.all()).slice(0, 15)) {
      await expect(row).toContainText(top.label);
    }
    // …the toolbar count is the filtered list's total, and that is the count the facet
    // showed for the same window before it was clicked.
    await expect(
      page.getByText(`${listed.total.toLocaleString("en-US")} calls`, { exact: true }).first(),
    ).toBeVisible();
    if (!listed.total_capped) expect(listed.total).toBe(top.count);
    expectClean(problems, "/calls?talkgroup=");
  });

  test("the breakdown counts the same calls, and clicking one filters to it", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const top = await busiestTalkgroupYesterday(page);
    await settle(page);

    const breakdown = page.getByRole("complementary", { name: "Facet counts" });
    await expect(breakdown).toBeVisible();
    const facet = breakdown.getByRole("button", { name: new RegExp(escapeRegExp(top.label)) }).first();
    await expect(facet).toHaveAttribute("aria-pressed", "false");
    await expect(facet).toContainText(top.count.toLocaleString("en-US"));
    await facet.click();

    await expect(page).toHaveURL(new RegExp(`talkgroup=${top.key}`));
    await settle(page, 800);
    const rows = callRows(page);
    expect(await rows.count()).toBeGreaterThan(0);
    await expect(rows.first()).toContainText(top.label);

    // Clicking it again is how the reader takes the filter off.
    await breakdown
      .getByRole("button", { name: new RegExp(escapeRegExp(top.label)) })
      .first()
      .click();
    await expect(page).not.toHaveURL(/talkgroup=/);
    expectClean(problems, "facet click");
  });

  test("a transcript search narrows the list and highlights the term in the rows", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/calls?preset=7d&q=fire");
    await settle(page);
    const rows = page.getByRole("button", { name: /^\w{3} \d+, \d\d:\d\d:\d\d,/ });
    expect(await rows.count()).toBeGreaterThan(0);
    // `highlightTerms` wraps the matched word wherever it appears in a row.
    expect(await page.locator("mark").count()).toBeGreaterThan(0);
    expectClean(problems, "/calls?q=fire");
  });

  test("an impossible filter shows the empty state rather than a blank page", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/calls?preset=custom&date_from=1999-01-01&date_to=1999-01-02");
    await settle(page);
    await expect(page.getByText(/no calls match/i)).toBeVisible();
    expectClean(problems, "empty filter");
  });
});

test.describe("the player", () => {
  test("streams real audio: the element gets a src and the range request succeeds", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const audio: { status: number; range: string | undefined }[] = [];
    page.on("response", (response) => {
      if (!/\/api\/calls\/\d+\/audio/.test(response.url())) return;
      audio.push({ status: response.status(), range: response.request().headers().range });
    });

    await page.goto("/calls?preset=7d");
    await settle(page);
    await page
      .getByRole("button", { name: /^Play the call on / })
      .first()
      .click();

    const player = page.getByRole("region", { name: "Audio player" });
    await expect(player).toBeVisible();
    const element = page.locator("audio");
    await expect(element).toHaveAttribute("src", /\/api\/calls\/\d+\/audio$/);

    await expect
      .poll(() => audio.length, { message: "the audio element never fetched anything" })
      .toBeGreaterThan(0);
    // A media element always asks for a range; the API has to answer 206.
    expect(audio.some((request) => request.range?.startsWith("bytes="))).toBe(true);
    expect(audio.every((request) => request.status === 206 || request.status === 200)).toBe(true);

    // Metadata loaded means the bytes really were playable audio.
    await expect.poll(() => element.evaluate((el: HTMLAudioElement) => el.duration)).toBeGreaterThan(0);
    expectClean(problems, "audio playback");
  });

  test("previous and next walk the queue the page is showing", async ({ page }) => {
    await page.goto("/calls?preset=7d");
    await settle(page);
    await page
      .getByRole("button", { name: /^Play the call on / })
      .first()
      .click();
    const player = page.getByRole("region", { name: "Audio player" });
    const first = await page.locator("audio").getAttribute("src");
    await player.getByRole("button", { name: "Next call" }).click();
    await expect.poll(() => page.locator("audio").getAttribute("src")).not.toBe(first);
    await player.getByRole("button", { name: "Previous call" }).click();
    await expect.poll(() => page.locator("audio").getAttribute("src")).toBe(first);
  });
});
