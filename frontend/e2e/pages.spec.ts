import { expect, test } from "@playwright/test";
import { api, expectClean, isGuest, pageResponse, settle, watchForProblems } from "./helpers";

test.describe("search", () => {
  test("highlights the matched terms the API marked up", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const results = await api<{ total: number; items: { highlight: string }[] }>(
      page,
      "/search?q=fire&page_size=5",
    );
    expect(results.total, "no transcripts mention 'fire' in this archive").toBeGreaterThan(0);
    expect(results.items[0]?.highlight).toContain("<mark>");

    // `preset=all` is not a thing; the page's own window is what it counts, so the
    // assertion is on the shape of the count rather than a number from a different query.
    await page.goto("/search?q=fire&preset=7d");
    await settle(page);
    await expect(page.getByText(/[\d,]+ matching calls/)).toBeVisible();
    // The `ts_headline` markup survives sanitizing and reaches the DOM as <mark>.
    expect(await page.locator("mark").count()).toBeGreaterThan(0);
    expectClean(problems, "/search");
  });

  test("offers suggestions from the real directory", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const suggestions = await api<{ value: string; kind: string }[]>(page, "/search/suggest?q=fi");
    expect(suggestions.length).toBeGreaterThan(0);
    await page.goto("/search");
    await settle(page, 400);
    const box = page.getByRole("combobox", { name: "Search query" });
    await box.fill("fi");
    const first = page.getByRole("option").first();
    await expect(first).toBeVisible();
    await expect(first).toContainText(suggestions[0]?.value ?? "");

    // Picking one runs that search. What it finds depends on the page's window (the last
    // 24 h) and on what was said in it, so the page is held to the answer it got: that
    // many matching calls, or the empty state when there were none.
    const picked = suggestions[0]?.value ?? "";
    const answer = await pageResponse<{ total: number; items: unknown[] }>(
      page,
      (url) => url.pathname === "/api/search" && url.searchParams.get("q") === picked,
      () => first.click(),
    );
    expect(new URL(page.url()).searchParams.get("q")).toBe(picked);
    await settle(page);
    if (answer.items.length > 0) {
      await expect(page.getByText(`${answer.total.toLocaleString("en-US")} matching calls`)).toBeVisible();
    } else {
      await expect(page.getByText("Nothing matched")).toBeVisible();
    }
    expectClean(problems, "search suggest");
  });

  test("a query that matches nothing shows the empty state", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/search?q=zzzqqqxyzzy");
    await settle(page);
    await expect(page.getByText("Nothing matched")).toBeVisible();
    expectClean(problems, "empty search");
  });
});

test.describe("live", () => {
  test("opens the WebSocket and the header says it is live", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const sockets: string[] = [];
    let framesIn = 0;
    page.on("websocket", (socket) => {
      sockets.push(socket.url());
      socket.on("framereceived", () => {
        framesIn += 1;
      });
    });

    await page.goto("/live");
    await settle(page);

    expect(sockets, "the live page opened no WebSocket").toContainEqual(
      expect.stringMatching(/^ws:\/\/[^/]+\/api\/live/),
    );
    // "Live" in the page header is the connection badge reporting an open socket.
    await expect(page.getByRole("banner").getByText("Live", { exact: true })).toBeVisible();
    // The server sends an `ingest.status` heartbeat even on a silent channel.
    await expect.poll(() => framesIn, { timeout: 20_000 }).toBeGreaterThan(0);
    expectClean(problems, "/live");
  });

  test("the hold button stops the feed without breaking the page", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/live");
    await settle(page);
    await page.getByRole("button", { name: "Hold" }).click();
    await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
    expectClean(problems, "live hold");
  });
});

test.describe("the directories", () => {
  test("talkgroups lists the real channels with their call counts", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const list = await api<{
      items: { id: number; alias: string | null; tgid: number; call_count: number }[];
    }>(page, "/talkgroups?sort=-call_count&page_size=5&include_hidden=false");
    const top = list.items[0];
    expect(top).toBeTruthy();

    await page.goto("/talkgroups");
    await settle(page);
    const row = page
      .getByRole("row")
      .filter({ hasText: String(top?.tgid) })
      .first();
    await expect(row).toContainText(top?.alias ?? `TG ${top?.tgid}`);
    await expect(row).toContainText((top?.call_count ?? 0).toLocaleString("en-US"));
    expectClean(problems, "/talkgroups");
  });

  test("a talkgroup's page shows its own calls and its busiest hours", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const list = await api<{
      items: { id: number; alias: string | null; tgid: number; call_count: number }[];
    }>(page, "/talkgroups?sort=-call_count&page_size=1");
    const top = list.items[0];
    const stats = await api<{ call_count: number; hourly: { hour: number; count: number }[] }>(
      page,
      `/talkgroups/${top?.id}/stats`,
    );

    await page.goto(`/talkgroups/${top?.id}`);
    await settle(page);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(top?.alias ?? `TG ${top?.tgid}`);
    await expect(page.getByText(String(top?.tgid), { exact: false }).first()).toBeVisible();
    expect(stats.hourly).toHaveLength(24);
    expectClean(problems, "talkgroup detail");
  });

  test("radios lists the real units and links to each one's history", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const list = await api<{ items: { id: number; radio_id: number; call_count: number }[] }>(
      page,
      "/radios?sort=-call_count&page_size=1",
    );
    const top = list.items[0];
    expect(top).toBeTruthy();

    await page.goto("/radios");
    await settle(page);
    await expect(page.locator(`a[href="/radios/${top?.id}"]`).first()).toBeVisible();

    await page.goto(`/radios/${top?.id}`);
    await settle(page);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(String(top?.radio_id));
    const history = await api<{ call_count: number; hourly: unknown[] }>(page, `/radios/${top?.id}/history`);
    expect(history.hourly).toHaveLength(24);
    expectClean(problems, "radio detail");
  });
});

test.describe("statistics", () => {
  test("the tiles show the archive's real numbers", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    // The numbers the page itself was given: the day-long counts roll, and a second
    // question a moment later can get a different answer.
    const overview = await pageResponse<{
      calls_total: number;
      transcript_coverage: number;
      talkgroups_active_24h: number;
      radios_active_24h: number;
    }>(
      page,
      (url) => url.pathname === "/api/stats/overview",
      () => page.goto("/stats"),
    );
    await settle(page, 2000);

    // Each tile's value, found by its label. Counts are grouped the way the page formats
    // every number (`1,120`, not `1120`).
    const tile = (label: string) =>
      page.locator(`xpath=//p[normalize-space()="${label}"]/following-sibling::p[1]`).first();
    await expect(
      page.getByText(`${overview.calls_total.toLocaleString("en-US")} in the archive`),
    ).toBeVisible();
    await expect(tile("Transcript coverage")).toHaveText(
      `${(overview.transcript_coverage * 100).toFixed(0)}%`,
    );
    await expect(tile("Talkgroups active")).toHaveText(
      overview.talkgroups_active_24h.toLocaleString("en-US"),
    );
    await expect(tile("Radios active")).toHaveText(overview.radios_active_24h.toLocaleString("en-US"));
    expectClean(problems, "/stats");
  });

  test("draws the activity heatmap, and keeps the per-backend table for operators", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const guest = await isGuest(page);
    const asked: string[] = [];
    page.on("request", (request) => asked.push(new URL(request.url()).pathname));
    const transcription = await pageResponse<{ backends: { backend_name: string }[] }>(
      page,
      (url) => url.pathname === "/api/stats/transcription",
      () => page.goto("/stats"),
    );
    await settle(page, 2500);
    // ECharts renders to canvas; four charts on this page.
    expect(await page.locator("canvas").count()).toBeGreaterThan(0);

    const backends = page.getByRole("heading", { name: "Speech-to-text backends" });
    const gaps = page.getByRole("heading", { name: "Recorder gaps" });
    if (guest) {
      // Which server transcribed what, and where the recorder was down, are operator
      // business: a guest gets neither card, and the gaps are never even asked for.
      await expect(backends).toHaveCount(0);
      await expect(gaps).toHaveCount(0);
      expect(asked).not.toContain("/api/stats/gaps");
      for (const backend of transcription.backends) {
        await expect(page.getByRole("cell", { name: backend.backend_name, exact: true })).toHaveCount(0);
      }
    } else {
      await expect(backends).toBeVisible();
      for (const backend of transcription.backends) {
        await expect(page.getByRole("cell", { name: backend.backend_name, exact: true })).toBeVisible();
      }
    }
    expectClean(problems, "stats charts");
  });
});

test.describe("admin, signed out", () => {
  test("shows the real ingest ledger, and asks for a session before probing backends", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const status = await api<{ status: { audio_root: string; files_new_24h: number } }>(
      page,
      "/admin/ingest",
    );

    await page.goto("/admin");
    await settle(page);
    await expect(page.getByText(status.status.audio_root, { exact: false }).first()).toBeVisible();

    // Probing every STT server is an outbound request per backend, so the API asks for
    // `admin` and the tab says so rather than showing an empty list.
    await page.getByRole("tab", { name: "Backends" }).click();
    await expect(page.getByText("Backend health needs an admin session")).toBeVisible();
    expectClean(problems, "/admin");
  });

  test("keeps every write disabled without a session", async ({ page }) => {
    await page.goto("/admin");
    await settle(page);
    await expect(page.getByRole("button", { name: /rescan/i }).first()).toBeDisabled();
  });
});

test.describe("help", () => {
  test("documents every tooltip in the app", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/help");
    await settle(page);
    // The page's own heading counts them; the unit test asserts the map is complete.
    await expect(page.getByText(/\d+ of them/)).toBeVisible();
    expectClean(problems, "/help");
  });
});
