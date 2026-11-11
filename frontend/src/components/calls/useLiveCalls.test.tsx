/**
 * The anchoring rules, driven through the hook with a stubbed feed.
 *
 * The real `useLiveFeed` owns a socket and a random mock replayer, neither of which
 * a test should be waiting on, so it is replaced by a plain array the test appends
 * to. The scroll container is stubbed too: jsdom lays nothing out, so the element's
 * heights are scripted — one read before the insertion, one after — which is exactly
 * what the hook does with them.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Call, CallFilterParams, LiveEvent } from "../../api/types";
import { LIVE_PAUSE_KEY, useLiveCalls } from "./useLiveCalls";

const feed = vi.hoisted(() => ({ events: [] as unknown[] }));

vi.mock("../../api/live", () => ({
  // The real hook keeps its buffer across a pause — the socket closes, the events it
  // already had do not vanish — and the backlog rule depends on that, so the stub
  // hands back the same array either way.
  useLiveFeed: (_params: unknown, enabled: boolean) => ({
    events: feed.events,
    state: enabled ? "open" : "closed",
    lastEventAt: null,
    clear: () => {},
  }),
}));

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

const arrival = (c: Call): LiveEvent => ({ type: "call.new", call: c }) as LiveEvent;
const superseded = (id: number, by: number): LiveEvent => ({ type: "call.superseded", id, by }) as LiveEvent;
const transcribed = (c: Call): LiveEvent => ({ type: "call.transcribed", call: c }) as LiveEvent;

/**
 * A scroll container whose height is read from a script: the first read is the
 * anchor taken before the rows go in, the second is the layout pass afterwards.
 */
function scroller(scrollTop: number, heights: number[]) {
  let read = 0;
  return {
    scrollTop,
    scrollTo: vi.fn(),
    get scrollHeight(): number {
      const height = heights[Math.min(read, heights.length - 1)] as number;
      read += 1;
      return height;
    },
  };
}

const PARAMS: CallFilterParams = { sort: "-started_at" } as CallFilterParams;

function mount(base: Call[], selectedId: number | null = null) {
  return renderHook(
    (props: { base: Call[]; selectedId: number | null }) =>
      useLiveCalls({ base: props.base, params: PARAMS, eligible: true, selectedId: props.selectedId }),
    { initialProps: { base, selectedId } },
  );
}

/** Push events onto the stub feed and let the hook's effects see them. */
function emit(rerender: () => void, ...events: LiveEvent[]) {
  act(() => {
    feed.events = [...[...events].reverse(), ...feed.events];
    rerender();
  });
}

beforeEach(() => {
  feed.events = [];
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("useLiveCalls", () => {
  it("replaces the list with the next page after a filter change, rather than folding it in", () => {
    // `useCalls` hands the previous page back as placeholder data while the next loads,
    // so the base is still the old answer at the moment the params change.
    const old = [call(1), call(2)];
    const fresh = [call(9)];
    const { result, rerender } = renderHook(
      (props: { base: Call[]; params: CallFilterParams }) =>
        useLiveCalls({ base: props.base, params: props.params, eligible: true, selectedId: null }),
      { initialProps: { base: old, params: PARAMS } },
    );
    expect(result.current.items.map((c) => c.id)).toEqual([1, 2]);
    const narrowed = { ...PARAMS, talkgroup: [8237] } as CallFilterParams;
    rerender({ base: old, params: narrowed });
    // Nothing new yet: the old rows stay rather than the list blanking.
    expect(result.current.items.map((c) => c.id)).toEqual([1, 2]);
    rerender({ base: fresh, params: narrowed });
    // The first page fetched for the new filter is the whole list — no leftovers.
    expect(result.current.items.map((c) => c.id)).toEqual([9]);
  });

  it("folds a moved rolling window in like a refetch when the question is unchanged", () => {
    // After a suspension the Calls page re-reads its rolling window from the clock,
    // which changes `date_from` and so the params — but not the question it is asking.
    const old = [call(2), call(1)];
    const { result, rerender } = renderHook(
      (props: { base: Call[]; params: CallFilterParams }) =>
        useLiveCalls({
          base: props.base,
          params: props.params,
          questionKey: "preset=24h",
          eligible: true,
          selectedId: null,
        }),
      {
        initialProps: {
          base: old,
          params: { ...PARAMS, date_from: "2026-08-28T14:30:00.000Z" } as CallFilterParams,
        },
      },
    );
    const moved = { ...PARAMS, date_from: "2026-08-28T15:31:00.000Z" } as CallFilterParams;
    rerender({ base: old, params: moved });
    rerender({ base: [call(3), call(2)], params: moved });
    // Merged, newest first, with the row the new page no longer carries still in place.
    expect(result.current.items.map((c) => c.id)).toEqual([3, 2, 1]);
  });

  it("bumps the list down when the reader is at the top with nothing selected", () => {
    const { result, rerender } = mount([call(5), call(4)]);
    const element = scroller(0, [400, 440]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(() => rerender({ base: [call(5), call(4)], selectedId: null }), arrival(call(6)));

    expect(result.current.items.map((c) => c.id)).toEqual([6, 5, 4]);
    // Nothing to hold: the rows simply arrive, marked for a moment.
    expect(result.current.newCount).toBe(0);
    expect([...result.current.freshIds]).toEqual([6]);
    expect(element.scrollTop).toBe(0);
  });

  it("holds the reader's place, and counts the arrivals, when they have scrolled up", () => {
    const { result, rerender } = mount([call(5), call(4)]);
    const element = scroller(200, [400, 440]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(() => rerender({ base: [call(5), call(4)], selectedId: null }), arrival(call(6)));

    expect(result.current.items.map((c) => c.id)).toEqual([6, 5, 4]);
    // The list grew by 40px above them, so their scroll position gains 40px.
    expect(element.scrollTop).toBe(240);
    expect(result.current.newCount).toBe(1);
    expect(result.current.freshIds.size).toBe(0);
  });

  it("holds the place of a selected row even at the very top of the list", () => {
    const { result, rerender } = mount([call(5), call(4)], 5);
    const element = scroller(0, [400, 440]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(() => rerender({ base: [call(5), call(4)], selectedId: 5 }), arrival(call(6)));

    expect(element.scrollTop).toBe(40);
    expect(result.current.newCount).toBe(1);
    // The selection is data the page owns; the feed never touches it.
    expect(result.current.items.some((c) => c.id === 5)).toBe(true);
  });

  it("clears the counter and goes back to the top when the pill is pressed", () => {
    const { result, rerender } = mount([call(5)]);
    const element = scroller(200, [400, 440]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(() => rerender({ base: [call(5)], selectedId: null }), arrival(call(6)));
    expect(result.current.newCount).toBe(1);

    act(() => result.current.jumpToTop());
    expect(result.current.newCount).toBe(0);
    expect(element.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
  });

  it("puts a transcript on the row it belongs to without moving or counting anything", () => {
    const { result, rerender } = mount([call(5), call(4)], 4);
    const element = scroller(200, [400, 400]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(
      () => rerender({ base: [call(5), call(4)], selectedId: 4 }),
      transcribed(call(4, { transcript_status: "done", transcript_text: "engine 12 responding" })),
    );

    expect(result.current.items.map((c) => c.id)).toEqual([5, 4]);
    expect(result.current.items[1]?.transcript_text).toBe("engine 12 responding");
    expect(result.current.newCount).toBe(0);
    expect(element.scrollTop).toBe(200);
  });

  it("adds nothing while paused, and remembers the choice", () => {
    const { result, rerender } = mount([call(5)]);
    act(() => result.current.setPaused(true));
    expect(result.current.status).toBe("paused");

    emit(() => rerender({ base: [call(5)], selectedId: null }), arrival(call(6)));
    expect(result.current.items.map((c) => c.id)).toEqual([5]);
    expect(localStorage.getItem(LIVE_PAUSE_KEY)).toBe("true");
  });

  it("does not dump the backlog when the feed is resumed", () => {
    const { result, rerender } = mount([call(5)]);
    act(() => result.current.setPaused(true));
    emit(() => rerender({ base: [call(5)], selectedId: null }), arrival(call(6)));

    act(() => result.current.setPaused(false));
    expect(result.current.items.map((c) => c.id)).toEqual([5]);
    expect(result.current.status).toBe("live");

    // …but the next call to arrive does land.
    emit(() => rerender({ base: [call(5)], selectedId: null }), arrival(call(7)));
    expect(result.current.items.map((c) => c.id)).toEqual([7, 5]);
  });

  it("drops a row reconciliation has demoted", () => {
    const { result, rerender } = mount([call(5), call(4)]);
    const element = scroller(0, [400, 360]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    // The winner arrives as its own `call.new`; the loser only ever gets this frame,
    // because a `call.new` for a row stored as a duplicate is filtered out server-side.
    emit(() => rerender({ base: [call(5), call(4)], selectedId: null }), arrival(call(6)), superseded(5, 6));

    expect(result.current.items.map((c) => c.id)).toEqual([6, 4]);
    expect(result.current.supersededIds.size).toBe(0);
  });

  it("keeps the row that is playing, marked, rather than pulling it out from under the reader", () => {
    const { result, rerender } = mount([call(5), call(4)], 5);
    const element = scroller(0, [400, 400]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    emit(() => rerender({ base: [call(5), call(4)], selectedId: 5 }), superseded(5, 6));

    expect(result.current.items.map((c) => c.id)).toEqual([5, 4]);
    expect([...result.current.supersededIds]).toEqual([5]);
  });

  it("ignores a demotion for a row it is not showing", () => {
    const { result, rerender } = mount([call(5)]);
    emit(() => rerender({ base: [call(5)], selectedId: null }), superseded(99, 100));
    expect(result.current.items.map((c) => c.id)).toEqual([5]);
    expect(result.current.supersededIds.size).toBe(0);
  });

  it("says it is paused, with a reason, on a sort live cannot honour", () => {
    const { result } = renderHook(() =>
      useLiveCalls({ base: [call(5)], params: PARAMS, eligible: false, selectedId: null }),
    );
    expect(result.current.status).toBe("paused");
    expect(result.current.pausedReason).toMatch(/newest-first/);
  });
});

/** What the socket delivers must survive the REST answers around it. */
describe("useLiveCalls and the REST page", () => {
  const at = (minute: number) => new Date(1_800_000_000_000 + minute * 60_000).toISOString();

  it("re-merges arrivals that came in during a filter change over the new page", () => {
    const old = [call(2), call(1)];
    const { result, rerender } = renderHook(
      (props: { base: Call[]; params: CallFilterParams }) =>
        useLiveCalls({ base: props.base, params: props.params, eligible: true, selectedId: null }),
      { initialProps: { base: old, params: PARAMS } },
    );
    const narrowed = { ...PARAMS, talkgroup: [8237] } as CallFilterParams;
    rerender({ base: old, params: narrowed });

    // The new filter's socket is up before its first page is: a call arrives in between.
    emit(() => rerender({ base: old, params: narrowed }), arrival(call(20)));
    // The page was requested before that call existed, so it does not have it.
    rerender({ base: [call(9)], params: narrowed });

    expect(result.current.items.map((c) => c.id)).toEqual([20, 9]);
  });

  it("does not let a refetch put back a row older than the live one", () => {
    const base = [call(3, { updated_at: at(1) }), call(2, { updated_at: at(1) })];
    const { result, rerender } = renderHook(
      (props: { base: Call[] }) =>
        useLiveCalls({ base: props.base, params: PARAMS, eligible: true, selectedId: null }),
      { initialProps: { base } },
    );
    const edited = call(3, { updated_at: at(5), transcript_text: "engine 1 on scene" });
    emit(() => rerender({ base }), transcribed(edited));
    expect(result.current.items[0]?.transcript_text).toBe("engine 1 on scene");

    // A refetch that was in flight when the transcript landed answers with the old row.
    rerender({ base: [call(3, { updated_at: at(1) }), call(2, { updated_at: at(1) })] });

    expect(result.current.items[0]?.transcript_text).toBe("engine 1 on scene");
  });
});

/** The Calls page asks for 100 rows, and the next page only as the reader scrolls. */
describe("useLiveCalls and the pages after the first", () => {
  function mountPaged(base: Call[], more: Call[] = [], sort = "-started_at") {
    return renderHook(
      (props: { base: Call[]; more: Call[] }) =>
        useLiveCalls({
          base: props.base,
          more: props.more,
          params: { sort } as CallFilterParams,
          eligible: sort === "-started_at",
          selectedId: null,
        }),
      { initialProps: { base, more } },
    );
  }

  it("appends the next page below the list without counting, flashing or anchoring it", () => {
    const base = [call(9), call(8)];
    const { result, rerender } = mountPaged(base);
    // Scrolled well down the list: an arrival here would be anchored and counted.
    const element = scroller(600, [800, 880]);
    result.current.scrollRef.current = element as unknown as HTMLDivElement;

    rerender({ base, more: [call(7), call(6)] });

    expect(result.current.items.map((c) => c.id)).toEqual([9, 8, 7, 6]);
    expect(result.current.newCount).toBe(0);
    expect(result.current.freshIds.size).toBe(0);
    expect(element.scrollTop).toBe(600);
  });

  it("keeps the server's order under a sort live cannot insert into", () => {
    // Longest first: nothing about this order is newest-first, and appending must not re-sort.
    const base = [call(2), call(9)];
    const { result, rerender } = mountPaged(base, [], "-duration_s");
    rerender({ base, more: [call(5), call(1)] });
    expect(result.current.items.map((c) => c.id)).toEqual([2, 9, 5, 1]);
  });

  it("updates a row a later page repeats where it stands instead of listing it twice", () => {
    const base = [call(9), call(8)];
    const { result, rerender } = mountPaged(base);
    // An offset page shifted by one arrival hands back the last row of the page before.
    rerender({ base, more: [call(8, { transcript_text: "fresh" }), call(7)] });
    expect(result.current.items.map((c) => c.id)).toEqual([9, 8, 7]);
    expect(result.current.items[1]?.transcript_text).toBe("fresh");
  });

  it("drops the scrolled pages when the question changes", () => {
    const { result, rerender } = renderHook(
      (props: { base: Call[]; more: Call[]; params: CallFilterParams }) =>
        useLiveCalls({
          base: props.base,
          more: props.more,
          params: props.params,
          eligible: true,
          selectedId: null,
        }),
      { initialProps: { base: [call(9), call(8)], more: [call(7)], params: PARAMS } },
    );
    rerender({ base: [call(9), call(8)], more: [call(7), call(6)], params: PARAMS });
    expect(result.current.items.map((c) => c.id)).toEqual([9, 8, 7, 6]);

    const narrowed = { ...PARAMS, talkgroup: [8237] } as CallFilterParams;
    rerender({ base: [call(3)], more: [], params: narrowed });
    expect(result.current.items.map((c) => c.id)).toEqual([3]);
  });
});
