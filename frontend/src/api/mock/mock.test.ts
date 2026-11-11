import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../client";
import type {
  Call,
  CallDetail,
  ConversationThread,
  Facets,
  Paginated,
  StatsOverview,
  Waveform,
} from "../types";
import { CALLS, CONVERSATIONS, PATCHES, RADIOS, TALKGROUPS } from "./dataset";
import { matchesQuery } from "./filter";

describe("the fake corpus", () => {
  it("covers roughly 600 calls over two days on 22 talkgroups, two of them hidden", () => {
    expect(CALLS.length).toBeGreaterThan(450);
    expect(CALLS.length).toBeLessThanOrEqual(650);
    expect(TALKGROUPS).toHaveLength(22);
    // A real directory always has a few channels somebody has hidden, and the
    // directory's "show hidden" toggle has nothing to prove without them.
    expect(TALKGROUPS.filter((talkgroup) => talkgroup.is_hidden)).toHaveLength(2);
    expect(RADIOS.length).toBeGreaterThan(30);
    const span =
      Date.parse(CALLS[0]?.started_at as string) - Date.parse(CALLS[CALLS.length - 1]?.started_at as string);
    expect(span).toBeGreaterThan(36 * 3600_000);
    expect(span).toBeLessThan(49 * 3600_000);
  });

  it("carries the aliases the operators actually use", () => {
    const aliases = TALKGROUPS.map((talkgroup) => talkgroup.alias);
    expect(aliases).toContain("EC FIRE DISPATCH");
    expect(aliases).toContain("DPS LAW 2D");
    expect(aliases).toContain("SHUTTLE OPS");
    expect(PATCHES.map((patch) => `PATCH ${patch.patch_id}`)).toContain("PATCH 9238");
  });

  it("matches the corpus's shape: short calls, a long tail, some unresolved radios", () => {
    const durations = CALLS.map((call) => call.duration_s).sort((a, b) => a - b);
    const median = durations[Math.floor(durations.length / 2)] as number;
    expect(median).toBeLessThan(6);
    expect(Math.max(...durations)).toBeLessThanOrEqual(45);
    const subSecond = durations.filter((duration) => duration < 1).length / durations.length;
    expect(subSecond).toBeGreaterThan(0.05);
    expect(CALLS.some((call) => !call.radio_resolved)).toBe(true);
  });

  it("has patches whose member talkgroups are all on the call", () => {
    const patched = CALLS.filter((call) => call.patch_id !== null);
    expect(patched.length).toBeGreaterThan(0);
    for (const call of patched) {
      expect(call.talkgroup_ids.length).toBeGreaterThan(1);
      expect(call.call_type).toBe("patch");
    }
  });

  it("has transcripts of every quality, and some without one at all", () => {
    const qualities = new Set(CALLS.map((call) => call.transcript_quality));
    expect(qualities).toContain("good");
    expect(qualities).toContain("suspect");
    expect(qualities).toContain("noise");
    expect(CALLS.some((call) => call.transcript_status === "skipped")).toBe(true);
    expect(CALLS.some((call) => call.transcript_status === "failed")).toBe(true);
  });

  it("groups consecutive traffic into conversations", () => {
    expect(CONVERSATIONS.length).toBeGreaterThan(10);
    for (const conversation of CONVERSATIONS.slice(0, 10)) {
      expect(conversation.call_count).toBeGreaterThan(1);
    }
  });

  it("keeps the counters on the directory in step with the calls", () => {
    const talkgroup = TALKGROUPS.find((entry) => entry.call_count > 0) as (typeof TALKGROUPS)[number];
    const actual = CALLS.filter((call) => call.talkgroup_ids.includes(talkgroup.tgid)).length;
    expect(talkgroup.call_count).toBe(actual);
  });
});

describe("matchesQuery", () => {
  it("requires every bare term", () => {
    expect(matchesQuery("engine one responding", "engine responding")).toBe(true);
    expect(matchesQuery("engine one responding", "engine ladder")).toBe(false);
  });

  it("requires a quoted phrase verbatim", () => {
    expect(matchesQuery("we have a structure fire", '"structure fire"')).toBe(true);
    expect(matchesQuery("fire in a structure", '"structure fire"')).toBe(false);
  });

  it("excludes a negated term", () => {
    expect(matchesQuery("engine one responding", "engine -training")).toBe(true);
    expect(matchesQuery("training burn engine one", "engine -training")).toBe(false);
  });
});

describe("the mock API", () => {
  it("serves a page of calls with a total", async () => {
    const page = await apiGet<Paginated<Call>>("/calls", { page_size: 25 });
    expect(page.items).toHaveLength(25);
    expect(page.total ?? 0).toBeGreaterThan(25);
    expect(page.items[0]).toHaveProperty("talkgroup_alias");
    // The list rows must not carry the detail-only payloads.
    expect(page.items[0]).not.toHaveProperty("raw");
  });

  it("continues a page from its cursor, and does not count a cursor page", async () => {
    const first = await apiGet<Paginated<Call>>("/calls", { page_size: 3 });
    const whole = await apiGet<Paginated<Call>>("/calls", { page_size: 6 });
    expect(first.next_cursor).not.toBeNull();
    const second = await apiGet<Paginated<Call>>("/calls", {
      page_size: 3,
      cursor: first.next_cursor as string,
    });
    expect([...first.items, ...second.items].map((call) => call.id)).toEqual(
      whole.items.map((call) => call.id),
    );
    expect(second.total).toBeNull();
  });

  it("hides duplicates unless they are asked for", async () => {
    const withoutDuplicates = await apiGet<Paginated<Call>>("/calls", { page_size: 500 });
    const withDuplicates = await apiGet<Paginated<Call>>("/calls", {
      page_size: 500,
      include_duplicates: true,
    });
    // Offset paging always carries a total; only a cursor page leaves it null.
    expect(withDuplicates.total ?? 0).toBeGreaterThan(withoutDuplicates.total ?? 0);
  });

  it("filters by talkgroup, including patched traffic", async () => {
    const talkgroup = TALKGROUPS.find((entry) => entry.alias === "RC LAW 2") as (typeof TALKGROUPS)[number];
    const page = await apiGet<Paginated<Call>>("/calls", {
      talkgroup: [talkgroup.tgid],
      page_size: 500,
    });
    expect(page.items.length).toBeGreaterThan(0);
    for (const call of page.items) expect(call.talkgroup_ids).toContain(talkgroup.tgid);
    expect(page.items.some((call) => call.tgid !== talkgroup.tgid)).toBe(true);
  });

  it("sorts by any column, in both directions", async () => {
    const longest = await apiGet<Paginated<Call>>("/calls", { sort: "-duration_s", page_size: 5 });
    const shortest = await apiGet<Paginated<Call>>("/calls", { sort: "duration_s", page_size: 5 });
    expect(longest.items[0]?.duration_s).toBeGreaterThan(shortest.items[0]?.duration_s as number);
  });

  it("returns facets that add up to the result count", async () => {
    const facets = await apiGet<Facets>("/calls/facets", { quality: ["good"] });
    const total = facets.hour.reduce((sum, bucket) => sum + bucket.count, 0);
    expect(total).toBe(facets.total);
  });

  it("returns a call's detail with its transcript, neighbors and raw file", async () => {
    const page = await apiGet<Paginated<Call>>("/calls", { transcript_status: ["done"], page_size: 1 });
    const id = page.items[0]?.id as number;
    const detail = await apiGet<CallDetail>(`/calls/${id}`);
    expect(detail.raw.sha256).toHaveLength(64);
    expect(detail.transcript?.segments.length).toBeGreaterThan(0);
    expect(detail.conversation_call_ids.length).toBeGreaterThan(0);
  });

  it("returns the conversation as a record, with its calls behind /conversations/{id}", async () => {
    const inThread = CALLS.find((call) => call.conversation_id !== null) as (typeof CALLS)[number];
    const detail = await apiGet<CallDetail>(`/calls/${inThread.id}`);
    // Not an array of calls: the record, plus the ids of the calls in it.
    expect(Array.isArray(detail.conversation)).toBe(false);
    expect(detail.conversation?.call_count).toBeGreaterThan(1);
    const thread = await apiGet<ConversationThread>(`/conversations/${detail.conversation?.id}`);
    expect(thread.calls.map((call) => call.id)).toEqual(detail.conversation_call_ids);
  });

  it("returns waveform peaks bounded to 0..1 and stable per call", async () => {
    const id = CALLS[0]?.id as number;
    const first = await apiGet<Waveform>(`/calls/${id}/waveform`);
    const again = await apiGet<Waveform>(`/calls/${id}/waveform`);
    expect(first.peaks).toEqual(again.peaks);
    for (const peak of first.peaks) {
      expect(peak).toBeGreaterThan(0);
      expect(peak).toBeLessThanOrEqual(1);
    }
  });

  it("answers 404 for something that is not there", async () => {
    await expect(apiGet("/calls/999999999")).rejects.toMatchObject({ status: 404 });
    await expect(apiGet("/nonsense")).rejects.toMatchObject({ status: 404 });
  });

  it("gates writes behind a session", async () => {
    const id = CALLS[0]?.id as number;
    await expect(apiPost(`/calls/${id}/transcribe`)).rejects.toMatchObject({ status: 401 });
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    await expect(apiPost(`/calls/${id}/transcribe`)).resolves.toBeDefined();
  });

  it("computes an overview whose coverage is a fraction", async () => {
    const overview = await apiGet<StatsOverview>("/stats/overview");
    expect(overview.transcript_coverage).toBeGreaterThan(0);
    expect(overview.transcript_coverage).toBeLessThanOrEqual(1);
    expect(overview.calls_total).toBe(CALLS.length);
  });
});
