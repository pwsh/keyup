import { expect, type Page, test } from "@playwright/test";
import { api, expectClean, settle, watchForProblems } from "./helpers";

interface RecentRow {
  id: number;
  started_at: string;
  started_local: string;
  tgid: number | null;
  transcript_text: string | null;
}

interface RecentAnswer {
  items: RecentRow[];
  next_cursor: string | null;
}

/** The embed's first screen reaches back at most this far (`MAX_MINUTES`). */
const MINUTES = 240;

const recent = (page: Page, query: string) =>
  api<RecentAnswer>(page, `/transcripts/recent?minutes=${MINUTES}&${query}`);

const days = (rows: RecentRow[]) => new Set(rows.map((row) => row.started_local.slice(0, 10)));

/** The embed's list, its rows and the row at its end. */
function feed(page: Page) {
  const list = page.getByRole("list", { name: "Transcribed calls, newest first" });
  return {
    list,
    rows: list.locator("li[data-id]"),
    dividers: list.locator("li.kt-day"),
    end: list.locator("li.kt-end"),
    toEnd: () =>
      list.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      }),
  };
}

test.describe("the sidebar's Transcripts link", () => {
  test("opens the live transcript page in a new tab, with no handle back", async ({ page, context }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/calls");
    await settle(page, 400);

    const link = page.getByRole("navigation").getByRole("link", { name: "Transcripts", exact: true }).first();
    await expect(link).toHaveAttribute("href", "/embed/transcripts");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);

    const [tab] = await Promise.all([context.waitForEvent("page"), link.click()]);
    await tab.waitForLoadState();
    expect(new URL(tab.url()).pathname).toBe("/embed/transcripts");
    const tabProblems = watchForProblems(tab);
    // The embed's own page: the connection state, the filter bar and the list (or its empty
    // state) — never "Offline", which is the first screen failing to load.
    await expect(tab.getByRole("form", { name: "Filter the transcripts" })).toBeVisible();
    await expect(tab.locator(".kt-state")).toHaveText(/^(Live|Connecting…|Reconnecting…)$/);
    await expect(
      tab
        .getByRole("list", { name: "Transcribed calls, newest first" })
        .or(tab.getByText(/No transcribed calls in the last/)),
    ).toBeVisible();
    // The app tab stayed where it was.
    expect(new URL(page.url()).pathname).toBe("/calls");
    await settle(tab, 800);
    expectClean(tabProblems, "/embed/transcripts (new tab)");
    expectClean(problems, "/calls with the Transcripts link");
  });
});

test.describe("the live transcript page reads on into older calls", () => {
  test("the end row loads the next page, below the first screen", async ({ page }) => {
    const problems = watchForProblems(page);
    const probe = await recent(page, "preset=all&limit=20");
    test.skip(probe.items.length === 0, `no transcribed calls in the last ${MINUTES} minutes`);

    // Every answer the page gets, first screen and cursor pages alike. A short first screen
    // can put the end row in view at once, and the scroll observer then asks before any
    // test step could, so nothing here waits for one particular request.
    const answers: { cursor: string | null; status: number; body: RecentAnswer }[] = [];
    page.on("response", async (response) => {
      if (!response.url().includes("/api/transcripts/recent")) return;
      const body = (await response.json().catch(() => null)) as RecentAnswer | null;
      if (body)
        answers.push({
          cursor: new URL(response.url()).searchParams.get("cursor"),
          status: response.status(),
          body,
        });
    });

    await page.goto(`/embed/transcripts?preset=all&minutes=${MINUTES}&limit=20`);
    const { rows, end, toEnd } = feed(page);
    await expect(rows.first()).toBeVisible();
    await expect.poll(() => answers.some((answer) => answer.cursor === null)).toBe(true);
    const first = answers.find((answer) => answer.cursor === null)?.body as RecentAnswer;
    expect(first.next_cursor, "a first screen with rows always continues").toBeTruthy();

    await expect
      .poll(async () => {
        await toEnd();
        const button = end.getByRole("button", { name: "Load older calls" });
        if (answers.every((answer) => answer.cursor === null) && (await button.isVisible()))
          await button.click().catch(() => {});
        return answers.filter((answer) => answer.cursor !== null).length;
      })
      .toBeGreaterThan(0);
    const older = answers.find((answer) => answer.cursor !== null) as (typeof answers)[number];
    expect(older.status).toBe(200);
    expect(older.cursor).toBe(first.next_cursor);

    // The rows it added are below the ones that were there, newest first throughout.
    await expect.poll(() => rows.count()).toBeGreaterThan(first.items.length);
    const stamps = await rows.evaluateAll((items) =>
      items.map((item) => Date.parse(item.querySelector("time")?.getAttribute("datetime") ?? "")),
    );
    for (let index = 1; index < stamps.length; index += 1) {
      expect(stamps[index] as number).toBeLessThanOrEqual(stamps[index - 1] as number);
    }
    const lastOfFirst = first.items[first.items.length - 1] as RecentRow;
    const ids = await rows.evaluateAll((items) =>
      items.map((item) => Number((item as HTMLElement).dataset.id)),
    );
    expect(ids.indexOf(lastOfFirst.id)).toBeLessThan(ids.length - 1);
    expect(new Set(ids).size, "a row appears twice").toBe(ids.length);
    expectClean(problems, "/embed/transcripts load older");
  });

  test("scrolling past midnight marks the change of day with a divider", async ({ page }) => {
    test.setTimeout(180_000);
    const problems = watchForProblems(page);
    // A talkgroup quiet enough that its first screen plus one older page of 200 spans two
    // local days. Found from the API rather than named, so it works on any archive.
    const screen = await recent(page, "preset=all&limit=200");
    const candidates = [...new Set(screen.items.map((row) => row.tgid).filter((tgid) => tgid !== null))];
    let chosen: number | null = null;
    for (const tgid of candidates.slice(-12)) {
      const head = await recent(page, `talkgroup=${tgid}&limit=200`);
      if (head.items.length === 0 || head.next_cursor === null) continue;
      if (days(head.items).size > 1) continue; // already crosses midnight on the first screen
      const next = await recent(page, `talkgroup=${tgid}&limit=200&cursor=${head.next_cursor}`);
      if (days([...head.items, ...next.items]).size > 1) {
        chosen = tgid;
        break;
      }
    }
    test.skip(chosen === null, "no recently active talkgroup crosses a day within one older page");

    await page.goto(`/embed/transcripts?talkgroup=${chosen}&minutes=${MINUTES}&limit=200`);
    const { rows, dividers, toEnd } = feed(page);
    await expect(rows.first()).toBeVisible();
    await expect(dividers).toHaveCount(0);

    // No click: reaching the end of the list is what reads the next page.
    await expect
      .poll(
        async () => {
          await toEnd();
          return dividers.count();
        },
        { timeout: 30_000 },
      )
      .toBeGreaterThan(0);
    const divider = dividers.first();
    await expect(divider).toHaveText(/^\w{3}, \d{1,2} \w{3} \d{4}$|^\w{3}, \w{3} \d{1,2}, \d{4}$/);
    // The divider sits between the last row of one day and the first of the day before.
    const around = await divider.evaluate((element) => {
      const stamp = (node: Element | null) => node?.querySelector("time")?.getAttribute("datetime") ?? "";
      return { above: stamp(element.previousElementSibling), below: stamp(element.nextElementSibling) };
    });
    expect(Date.parse(around.below)).toBeLessThan(Date.parse(around.above));
    expectClean(problems, "/embed/transcripts day divider");
  });

  test("says so when the archive runs out", async ({ page }) => {
    const problems = watchForProblems(page);
    // A filter with a handful of calls in all: a recent call's talkgroup, narrowed to the
    // longest word of its own transcript.
    const screen = await recent(page, "preset=all&limit=100");
    let filter: string | null = null;
    for (const row of screen.items.slice(0, 25)) {
      const word = (row.transcript_text ?? "")
        .split(/[^A-Za-z]+/)
        .filter((token) => token.length >= 6)
        .sort((a, b) => b.length - a.length)[0];
      if (!word || row.tgid === null) continue;
      const query = `talkgroup=${row.tgid}&q=${encodeURIComponent(word.toLowerCase())}`;
      const head = await recent(page, `${query}&limit=100`);
      if (head.items.length === 0 || head.next_cursor === null) continue;
      const next = await recent(page, `${query}&limit=100&cursor=${head.next_cursor}`);
      if (next.next_cursor === null) {
        filter = query;
        break;
      }
    }
    test.skip(filter === null, "no recent transcript narrows to fewer than a page of calls");

    await page.goto(`/embed/transcripts?${filter}&minutes=${MINUTES}&limit=100`);
    const { rows, end, toEnd } = feed(page);
    await expect(rows.first()).toBeVisible();
    await expect
      .poll(
        async () => {
          await toEnd();
          const button = end.getByRole("button", { name: "Load older calls" });
          if (await button.isVisible().catch(() => false)) await button.click().catch(() => {});
          return (await end.textContent())?.trim();
        },
        { timeout: 30_000 },
      )
      .toBe("No older calls");
    expectClean(problems, "/embed/transcripts end of archive");
  });
});
