import { expect, type Response, test } from "@playwright/test";
import { api, type CallRowShape, expectClean, settle, watchForProblems } from "./helpers";

interface CallsPage {
  items: (CallRowShape & { started_at: string })[];
  page_size: number;
  total: number | null;
  next_cursor: string | null;
}

const isCallsList = (response: Response): boolean => {
  const url = new URL(response.url());
  return url.pathname === "/api/calls" && url.searchParams.get("page_size") === "100";
};

/**
 * The Calls list asks for 100 rows, then the next 100 by keyset cursor as the reader
 * scrolls towards the end. The unit tests run in jsdom, where nothing scrolls and the
 * virtualizer measures nothing, so this is the only place the real thing is exercised.
 */
test.describe("the calls list scrolls into older calls", () => {
  test("reaching the end asks for the next page by cursor and renders its rows", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const pages: { cursor: string | null; status: number; body: CallsPage }[] = [];
    page.on("response", async (response) => {
      if (!isCallsList(response)) return;
      const body = (await response.json().catch(() => null)) as CallsPage | null;
      if (body === null) return;
      pages.push({
        cursor: new URL(response.url()).searchParams.get("cursor"),
        status: response.status(),
        body,
      });
    });

    await page.goto("/calls?preset=7d");
    await settle(page);

    const first = pages.find((entry) => entry.cursor === null);
    expect(first, "the first page never arrived").toBeTruthy();
    const firstPage = (first as { body: CallsPage }).body;
    expect(firstPage.items.length, "a week of this archive has fewer than 100 calls").toBe(100);
    expect(firstPage.next_cursor, "a full first page carries a cursor").toBeTruthy();
    // Nothing is fetched past the first page until the reader scrolls: the load-ahead
    // margin is 800 px and 100 rows are far taller than that.
    expect(pages.filter((entry) => entry.cursor !== null)).toEqual([]);

    // The table's own scroll container: the list is virtualized, so its parent is what moves.
    const scroller = page.getByRole("list", { name: "Calls" }).locator("xpath=..");
    const nextPage = page.waitForResponse(
      (response) => isCallsList(response) && new URL(response.url()).searchParams.has("cursor"),
    );
    await scroller.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    const response = await nextPage;
    expect(response.status()).toBe(200);
    const url = new URL(response.url());
    expect(url.searchParams.get("cursor")).toBe(firstPage.next_cursor);

    const second = (await response.json()) as CallsPage;
    expect(second.items.length).toBeGreaterThan(0);
    // A cursor page continues strictly after the first page's last row: older (or the same
    // instant with a smaller id), and never a row the first page already had.
    const last = firstPage.items[firstPage.items.length - 1] as CallsPage["items"][number];
    const firstIds = new Set(firstPage.items.map((call) => call.id));
    for (const call of second.items) {
      expect(firstIds.has(call.id), `call ${call.id} came back on both pages`).toBe(false);
      expect(Date.parse(call.started_at)).toBeLessThanOrEqual(Date.parse(last.started_at));
    }

    // …and the rows land in the list: past index 99 now exists and scrolls into view.
    await expect
      .poll(async () => {
        await scroller.evaluate((element) => {
          element.scrollTop = element.scrollHeight;
        });
        const indexes = await page
          .locator("[data-index]")
          .evaluateAll((rows) => rows.map((row) => Number((row as HTMLElement).dataset.index)));
        return Math.max(-1, ...indexes);
      })
      .toBeGreaterThanOrEqual(100);
    expectClean(problems, "/calls scrolled");
  });

  test("a list that fits in one page never asks for a second, however far it is scrolled", async ({
    page,
  }) => {
    const problems = watchForProblems(page, { allow401: true });
    // A talkgroup with a few dozen calls this week: one short page, so no cursor. The margin
    // below 100 absorbs the page's own week starting a minute or two later than this one.
    const since = new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString();
    const facets = await api<{ talkgroup: { key: string; count: number }[] }>(
      page,
      `/calls/facets?limit=200&date_from=${encodeURIComponent(since)}`,
    );
    const quiet = facets.talkgroup.find((facet) => facet.count >= 5 && facet.count <= 60);
    test.skip(!quiet, "no talkgroup with 5-60 calls this week");

    const cursorRequests: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/api/calls" && url.searchParams.has("cursor")) cursorRequests.push(url.search);
    });
    await page.goto(`/calls?preset=7d&talkgroup=${quiet?.key}`);
    await settle(page);
    const scroller = page.getByRole("list", { name: "Calls" }).locator("xpath=..");
    await scroller.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await settle(page, 1000);
    expect(cursorRequests).toEqual([]);
    expectClean(problems, "/calls short list");
  });
});
