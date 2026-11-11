import { describe, expect, it } from "vitest";
import type { Call } from "../../api/types";
import { compareNewestFirst, isOlderCopy, mergeCalls } from "./liveCalls";

/** A call at a given minute of a fixed day; `id` doubles as the minute. */
function call(id: number, extra: Partial<Call> = {}): Call {
  return {
    id,
    started_at: new Date(1_800_000_000_000 + id * 60_000).toISOString(),
    talkgroup_alias: `TG ${id}`,
    duration_s: 4,
    transcript_status: "pending",
    transcript_text: null,
    ...extra,
  } as unknown as Call;
}

/** Newest first, as the page shows them. */
const list = (...ids: number[]): Call[] => ids.map((id) => call(id));

describe("mergeCalls", () => {
  it("puts a call recorded just now at the top", () => {
    const merged = mergeCalls(list(5, 4, 3), [call(6)]);
    expect(merged.items.map((c) => c.id)).toEqual([6, 5, 4, 3]);
    expect(merged.inserted).toEqual([6]);
    expect(merged.updated).toEqual([]);
    expect(merged.changed).toBe(true);
  });

  it("keeps the list in order when arrivals come out of order", () => {
    const merged = mergeCalls(list(5, 4, 3), [call(7), call(6)]);
    expect(merged.items.map((c) => c.id)).toEqual([7, 6, 5, 4, 3]);
  });

  it("leaves the array alone when nothing is new", () => {
    const current = list(5, 4);
    const merged = mergeCalls(current, [current[0] as Call]);
    expect(merged.changed).toBe(false);
    expect(merged.items).toBe(current);
    expect(merged.inserted).toEqual([]);
  });

  it("updates a row in place when a transcript lands on it, without moving it", () => {
    const current = list(5, 4, 3);
    const transcribed = call(4, { transcript_status: "done", transcript_text: "engine 12 responding" });
    const merged = mergeCalls(current, [transcribed]);
    expect(merged.items.map((c) => c.id)).toEqual([5, 4, 3]);
    expect(merged.items[1]?.transcript_text).toBe("engine 12 responding");
    expect(merged.updated).toEqual([4]);
    expect(merged.inserted).toEqual([]);
  });

  it("does not re-insert a call the list already has, however it arrives", () => {
    const merged = mergeCalls(list(5, 4), [call(5), call(5), call(4)]);
    expect(merged.items.map((c) => c.id)).toEqual([5, 4]);
    expect(merged.inserted).toEqual([]);
  });

  it("drops the oldest rows past the cap", () => {
    const merged = mergeCalls(list(5, 4, 3), [call(6)], { cap: 3 });
    expect(merged.items.map((c) => c.id)).toEqual([6, 5, 4]);
    expect(merged.dropped).toEqual([3]);
  });

  it("never drops the call the reader has open", () => {
    const merged = mergeCalls(list(5, 4, 3), [call(6)], { cap: 3, protectedId: 3 });
    expect(merged.items.map((c) => c.id)).toEqual([6, 5, 4, 3]);
    expect(merged.dropped).toEqual([]);
  });

  it("ignores a call older than everything a full list holds", () => {
    const merged = mergeCalls(list(5, 4, 3), [call(1)], { cap: 3 });
    expect(merged.items.map((c) => c.id)).toEqual([5, 4, 3]);
    expect(merged.changed).toBe(false);
  });

  it("leaves a call older than everything held to a later page, even with room for it", () => {
    // The backlog re-transcribes months-old calls all day; each used to be appended here.
    const merged = mergeCalls(list(5, 4, 3), [call(1)], { cap: 10 });
    expect(merged.items.map((c) => c.id)).toEqual([5, 4, 3]);
    expect(merged.changed).toBe(false);
  });

  it("still slots an older call in between the rows it falls among, and fills an empty list", () => {
    expect(mergeCalls(list(5, 3), [call(4)], { cap: 10 }).items.map((c) => c.id)).toEqual([5, 4, 3]);
    expect(mergeCalls([], [call(1)], { cap: 10 }).items.map((c) => c.id)).toEqual([1]);
  });

  it("orders two calls recorded in the same instant by id", () => {
    const at = new Date(1_800_000_000_000).toISOString();
    const older = call(1, { started_at: at });
    const newer = call(2, { started_at: at });
    expect(compareNewestFirst(newer, older)).toBeLessThan(0);
    expect(mergeCalls([older], [newer]).items.map((c) => c.id)).toEqual([2, 1]);
  });
});

/**
 * Two writers race into one list: a refetch requested before an edit can
 * land after the edit's live frame, and it used to put the pre-edit row back.
 */
describe("mergeCalls and the row's age", () => {
  const at = (minute: number) => new Date(1_800_000_000_000 + minute * 60_000).toISOString();

  it("does not let an older copy replace a newer one", () => {
    const live = call(5, { updated_at: at(10), transcript_text: "engine 1 responding" });
    const stale = call(5, { updated_at: at(9), transcript_text: null });
    const merged = mergeCalls([live], [stale]);
    expect(merged.changed).toBe(false);
    expect(merged.items[0]).toBe(live);
    expect(merged.updated).toEqual([]);
  });

  it("takes a newer copy, a tie, or a copy that does not say", () => {
    const held = call(5, { updated_at: at(10) });
    expect(mergeCalls([held], [call(5, { updated_at: at(11) })]).updated).toEqual([5]);
    expect(mergeCalls([held], [call(5, { updated_at: at(10) })]).updated).toEqual([5]);
    expect(mergeCalls([held], [call(5)]).updated).toEqual([5]);
    expect(mergeCalls([call(5)], [call(5, { updated_at: at(1) })]).updated).toEqual([5]);
  });

  it("isOlderCopy compares only when both rows carry a time", () => {
    expect(isOlderCopy(call(1, { updated_at: at(1) }), call(1, { updated_at: at(2) }))).toBe(true);
    expect(isOlderCopy(call(1, { updated_at: at(2) }), call(1, { updated_at: at(1) }))).toBe(false);
    expect(isOlderCopy(call(1, { updated_at: "not a date" }), call(1, { updated_at: at(2) }))).toBe(false);
  });
});
