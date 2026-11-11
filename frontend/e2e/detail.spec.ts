import { expect, test } from "@playwright/test";
import { api, type CallRowShape, expectClean, settle, watchForProblems } from "./helpers";

interface ListPage<T> {
  items: T[];
  total: number;
}

/**
 * The longest transcribed call whose audio is still on disk — the one with the most
 * segments. The audio matters: this page draws the waveform and plays the call, and a
 * call whose recording was discarded or purged has neither (the API answers 410).
 */
async function longestTranscribedCall(page: import("@playwright/test").Page) {
  const result = await api<ListPage<CallRowShape>>(
    page,
    "/calls?transcript_status=done&has_audio=true&page_size=1&sort=-duration_s",
  );
  const call = result.items[0];
  test.skip(!call, "no transcribed call with audio in the archive to test against");
  return call as CallRowShape;
}

test.describe("a call's own page", () => {
  test("shows the transcript, its segments, the waveform and the metadata", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const call = await longestTranscribedCall(page);
    const transcripts = await api<{ segments: { start: number; text: string }[] }[]>(
      page,
      `/calls/${call.id}/transcripts`,
    );
    const segments = transcripts[0]?.segments ?? [];
    expect(segments.length, "the longest call has no segments").toBeGreaterThan(0);

    await page.goto(`/calls/${call.id}`);
    await settle(page);

    // The header carries this call's real numbers.
    await expect(page.getByText(`${call.duration_s.toFixed(1)}s`).first()).toBeVisible();
    // Each segment renders as its own clickable line, stamped with its start time.
    const transcript = page.getByRole("tabpanel").first();
    const lines = transcript.getByRole("listitem");
    expect(await lines.count()).toBe(segments.length);
    for (const [index, segment] of segments.slice(0, 3).entries()) {
      const line = lines.nth(index);
      await expect(line).toContainText(segment.text.trim().slice(0, 40));
      await expect(line).toContainText(`${segment.start.toFixed(1)}s`);
    }
    // The waveform draws the peaks the API computed.
    const waveform = await api<{ peaks: number[] }>(page, `/calls/${call.id}/waveform`);
    expect(waveform.peaks.length).toBeGreaterThan(0);
    const strip = page.getByRole("slider", { name: "Playback position" });
    await expect(strip).toBeVisible();
    // One `<rect>` per peak the API returned, plus the playhead line.
    expect(await strip.locator("rect").count()).toBe(waveform.peaks.length);
    expectClean(problems, "call detail");
  });

  test("clicking a transcript segment seeks the player to it", async ({ page }) => {
    const call = await longestTranscribedCall(page);
    await page.goto(`/calls/${call.id}`);
    await settle(page);
    const segments = page.getByRole("tabpanel").first().getByRole("listitem").getByRole("button");
    expect(await segments.count()).toBeGreaterThan(0);
    await segments.last().click();
    await expect(page.getByRole("region", { name: "Audio player" })).toBeVisible();
    await expect(page.locator("audio")).toHaveAttribute("src", `/api/calls/${call.id}/audio`);
  });

  test("shows the conversation thread from GET /conversations/{id}", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    // A thread whose first call still has its audio, since that call's page is the one
    // opened (see longestTranscribedCall). The newest threads are on talkgroups whose
    // audio is often not kept at all, so look a little further than the first.
    const threads = await api<ListPage<{ id: number; call_count: number }>>(
      page,
      "/conversations?min_calls=3&page_size=25",
    );
    let thread: { calls: CallRowShape[] } | null = null;
    for (const conversation of threads.items) {
      const candidate = await api<{ calls: CallRowShape[] }>(page, `/conversations/${conversation.id}`);
      if (candidate.calls[0]?.audio_state === "present") {
        thread = candidate;
        break;
      }
    }
    test.skip(thread === null, "no multi-call conversation in the archive whose first call has audio");
    const first = thread?.calls[0] as CallRowShape;

    await page.goto(`/calls/${first.id}`);
    await settle(page);

    for (const call of (thread?.calls ?? []).slice(0, 4)) {
      await expect(page.locator(`a[href="/calls/${call.id}"]`)).toBeVisible();
    }
    expectClean(problems, "conversation thread");
  });

  test("previous and next walk the same talkgroup", async ({ page }) => {
    const call = await longestTranscribedCall(page);
    const detail = await api<{ prev_call_id: number | null; next_call_id: number | null }>(
      page,
      `/calls/${call.id}`,
    );
    await page.goto(`/calls/${call.id}`);
    await settle(page);
    if (detail.next_call_id !== null) {
      await page.getByRole("button", { name: /^Next/ }).click();
      await expect(page).toHaveURL(new RegExp(`/calls/${detail.next_call_id}$`));
    }
    if (detail.prev_call_id !== null) {
      await page.goto(`/calls/${call.id}`);
      await settle(page, 400);
      await page.getByRole("button", { name: /^Previous/ }).click();
      await expect(page).toHaveURL(new RegExp(`/calls/${detail.prev_call_id}$`));
    }
  });

  test("the raw metadata tab shows the ID3 frames the ingest read", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const call = await longestTranscribedCall(page);
    const detail = await api<{
      raw: {
        file_name: string;
        sha256: string | null;
        id3: Record<string, string>;
        raw_meta: Record<string, unknown>;
      };
    }>(page, `/calls/${call.id}`);
    await page.goto(`/calls/${call.id}`);
    await settle(page);
    await page.getByRole("tab", { name: "Raw metadata" }).click();
    const panel = page.getByRole("tabpanel", { name: "Raw metadata" });
    // The file's own fields, each in its labelled row. The name is looked for in its
    // row, not anywhere: a recorder's metadata usually repeats it (`audioName`).
    const field = (label: string) =>
      panel.locator(`xpath=.//dt[normalize-space()="${label}"]/following-sibling::dd[1]`);
    await expect(field("Name")).toHaveText(detail.raw.file_name);
    await expect(field("SHA-256")).toHaveText(detail.raw.sha256 ?? "—");

    // Below them, verbatim, whatever the recorder wrote: a sidecar's or trunkd's JSON
    // when the ingest kept it, the ID3 frames otherwise — one row per entry, in order.
    const entries = Object.entries(
      Object.keys(detail.raw.raw_meta).length ? detail.raw.raw_meta : detail.raw.id3,
    );
    const verbatim = panel.locator("dl").nth(1);
    if (entries.length) {
      await expect(verbatim.locator("dd")).toHaveCount(entries.length);
      for (const [index, [, value]] of entries.slice(0, 3).entries()) {
        const text =
          value === null || value === undefined
            ? ""
            : typeof value === "object"
              ? JSON.stringify(value)
              : String(value);
        await expect(verbatim.locator("dd").nth(index)).toHaveText(text || "—");
      }
    } else {
      // The ingest kept nothing verbatim; the panel has to say so rather than leave a
      // heading over nothing.
      await expect(verbatim).toHaveCount(0);
      await expect(panel.getByText(/Not retained for this call/)).toBeVisible();
    }
    expectClean(problems, "raw metadata");
  });

  test("a patched call links to each member channel's own traffic", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    const patched = await api<ListPage<CallRowShape & { patch_member_tgids: number[] }>>(
      page,
      // With its audio, for the same reason as longestTranscribedCall.
      "/calls?call_type=patch&has_audio=true&page_size=1",
    );
    const call = patched.items[0];
    test.skip(!call, "no patched calls in this archive");

    await page.goto(`/calls/${call?.id}`);
    await settle(page);
    for (const tgid of (call?.patch_member_tgids ?? []).slice(0, 3)) {
      await expect(page.locator(`a[href*="talkgroup=${tgid}"]`).first()).toBeVisible();
    }
    expectClean(problems, "patch chips");
  });

  test("space, j and k drive the player from the keyboard", async ({ page }) => {
    await page.goto("/calls?preset=7d");
    await settle(page);
    await page
      .getByRole("button", { name: /^Play the call on / })
      .first()
      .click();
    const player = page.getByRole("region", { name: "Audio player" });
    await expect(player).toBeVisible();
    const first = await page.locator("audio").getAttribute("src");

    await page.locator("body").press("j");
    await expect.poll(() => page.locator("audio").getAttribute("src")).not.toBe(first);
    await page.locator("body").press("k");
    await expect.poll(() => page.locator("audio").getAttribute("src")).toBe(first);

    // Selecting a call starts it, so the first space pauses and the second resumes.
    await expect(player.getByRole("button", { name: "Pause" })).toBeVisible();
    await page.locator("body").press(" ");
    await expect(player.getByRole("button", { name: "Play", exact: true })).toBeVisible();
    await page.locator("body").press(" ");
    await expect(player.getByRole("button", { name: "Pause" })).toBeVisible();
    // …and the page did not scroll out from under the reader.
    expect(await page.evaluate(() => window.scrollY)).toBe(0);
  });

  test("a call that does not exist shows the empty state", async ({ page }) => {
    await page.goto("/calls/999999999");
    await expect(page.getByRole("alert")).toContainText("Could not load this data");
    await expect(page.getByRole("alert")).toContainText("404");
  });
});
