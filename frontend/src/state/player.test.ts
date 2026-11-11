import { beforeEach, describe, expect, it } from "vitest";
import type { Call } from "../api/types";
import { clampSpeed, currentIndex, MAX_SPEED, MIN_SPEED, resetPlayer, usePlayerStore } from "./player";

const call = (id: number): Call =>
  ({
    id,
    talkgroup_alias: `TG ${id}`,
    duration_s: 4,
    started_at: new Date(1_800_000_000_000 - id * 1000).toISOString(),
    transcript_status: "done",
    transcript_quality: "good",
  }) as unknown as Call;

const QUEUE = [call(1), call(2), call(3)];
const store = () => usePlayerStore.getState();

beforeEach(() => {
  resetPlayer();
  store().setQueue(QUEUE);
});

describe("selection", () => {
  it("starts playing the selected call from the top", () => {
    store().select(QUEUE[0] as Call);
    expect(store().current?.id).toBe(1);
    expect(store().playing).toBe(true);
    expect(store().currentTime).toBe(0);
    expect(store().pendingSeek).toBe(0);
  });

  it("can load a call without starting it", () => {
    store().select(QUEUE[0] as Call, { play: false });
    expect(store().playing).toBe(false);
  });

  it("clearing stops playback and empties the player", () => {
    store().select(QUEUE[0] as Call);
    store().clear();
    expect(store().current).toBeNull();
    expect(store().playing).toBe(false);
  });

  it("does nothing when told to toggle with nothing loaded", () => {
    store().toggle();
    expect(store().playing).toBe(false);
  });
});

describe("walking the queue", () => {
  it("moves down the list for next and up for previous", () => {
    store().select(QUEUE[1] as Call);
    store().next();
    expect(store().current?.id).toBe(3);
    store().previous();
    expect(store().current?.id).toBe(2);
  });

  it("stops at both ends rather than wrapping", () => {
    store().select(QUEUE[0] as Call);
    store().previous();
    expect(store().current?.id).toBe(1);
    store().select(QUEUE[2] as Call);
    store().next();
    expect(store().current?.id).toBe(3);
  });

  it("reports where the current call sits in the queue", () => {
    store().select(QUEUE[2] as Call);
    expect(currentIndex(usePlayerStore.getState())).toBe(2);
    store().clear();
    expect(currentIndex(usePlayerStore.getState())).toBe(-1);
  });
});

describe("when a call ends", () => {
  it("stops unless continuous playback is on", () => {
    store().select(QUEUE[0] as Call);
    store().ended();
    expect(store().playing).toBe(false);
    expect(store().current?.id).toBe(1);
  });

  it("rolls on to the next call with continuous playback on", () => {
    store().setContinuous(true);
    store().select(QUEUE[0] as Call);
    store().ended();
    expect(store().current?.id).toBe(2);
    expect(store().playing).toBe(true);
  });

  it("stops at the end of the queue even when continuous", () => {
    store().setContinuous(true);
    store().select(QUEUE[2] as Call);
    store().ended();
    expect(store().current?.id).toBe(3);
    expect(store().playing).toBe(false);
  });
});

describe("the live toggles", () => {
  it("ignores arrivals while the live feed is off", () => {
    store().liveArrived(call(9));
    expect(store().queue).toHaveLength(3);
  });

  it("puts a new call at the top of the queue without playing it", () => {
    store().setLive(true);
    store().liveArrived(call(9));
    expect(store().queue[0]?.id).toBe(9);
    expect(store().current).toBeNull();
  });

  it("never queues the same call twice", () => {
    store().setLive(true);
    store().liveArrived(call(9));
    store().liveArrived(call(9));
    expect(store().queue.filter((entry) => entry.id === 9)).toHaveLength(1);
  });

  it("plays an arrival when auto-play-new is on and nothing is playing", () => {
    store().setAutoPlayNew(true);
    store().liveArrived(call(9));
    expect(store().current?.id).toBe(9);
    expect(store().playing).toBe(true);
  });

  it("does not interrupt what is already playing", () => {
    store().setAutoPlayNew(true);
    store().select(QUEUE[0] as Call);
    store().liveArrived(call(9));
    expect(store().current?.id).toBe(1);
  });

  it("turning auto-play-new on turns the feed on too", () => {
    expect(store().live).toBe(false);
    store().setAutoPlayNew(true);
    expect(store().live).toBe(true);
  });

  it("turning the feed off turns auto-play-new off with it", () => {
    store().setAutoPlayNew(true);
    store().setLive(false);
    expect(store().autoPlayNew).toBe(false);
  });
});

describe("speed and volume", () => {
  it("clamps the speed to the range the control offers", () => {
    expect(clampSpeed(0.1)).toBe(MIN_SPEED);
    expect(clampSpeed(9)).toBe(MAX_SPEED);
    expect(clampSpeed(Number.NaN)).toBe(1);
    store().setSpeed(4);
    expect(store().speed).toBe(MAX_SPEED);
  });

  it("clamps the volume to 0..1", () => {
    store().setVolume(2);
    expect(store().volume).toBe(1);
    store().setVolume(-1);
    expect(store().volume).toBe(0);
  });

  it("remembers the preferences across a reload", () => {
    store().setSpeed(1.5);
    store().setContinuous(true);
    store().setNormalize(true);
    const stored = JSON.parse(localStorage.getItem("keyup-player") as string);
    expect(stored.speed).toBe(1.5);
    expect(stored.continuous).toBe(true);
    expect(stored.normalize).toBe(true);
  });

  it("levels loudness by default and remembers being switched off", () => {
    expect(store().normalize).toBe(true);
    store().setNormalize(false);
    expect(store().normalize).toBe(false);
    store().setNormalize(true);
    expect(store().normalize).toBe(true);
  });
});

describe("seeking", () => {
  it("hands the element a pending seek and clears it once applied", () => {
    store().select(QUEUE[0] as Call);
    store().seek(2.5);
    expect(store().pendingSeek).toBe(2.5);
    expect(store().currentTime).toBe(2.5);
    store().seekApplied();
    expect(store().pendingSeek).toBeNull();
  });
});

describe("a reconciled copy", () => {
  it("leaves the queue when another recorder's copy takes its place", () => {
    store().superseded(2);
    expect(store().queue.map((c) => c.id)).toEqual([1, 3]);
  });

  it("keeps the call that is actually loaded, because somebody is listening to it", () => {
    store().select(QUEUE[1] as Call);
    store().superseded(2);
    expect(store().queue.map((c) => c.id)).toEqual([1, 2, 3]);
    expect(store().current?.id).toBe(2);
  });
});
