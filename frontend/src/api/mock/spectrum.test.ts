import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SpectrumFrame } from "../types";
import { mockSpectrumFrame, subscribeMockSpectrum } from "./spectrum";

/** Every frame must honour the wire contract the real ingest validates against. */
function assertValidFrame(frame: SpectrumFrame): void {
  expect(frame.db).toHaveLength(frame.bins);
  expect(frame.bins).toBeLessThanOrEqual(2048);
  expect(frame.span_hz).toBeGreaterThan(0);
  for (const value of frame.db) {
    expect(value).toBeGreaterThanOrEqual(-128);
    expect(value).toBeLessThanOrEqual(127);
    expect(Number.isInteger(value)).toBe(true);
  }
}

describe("the mock spectrum feed", () => {
  it("produces a snapshot frame that matches the wire contract", () => {
    const frame = mockSpectrumFrame("airspy-0", "trunkd-central-01");
    expect(frame.instance_id).toBe("trunkd-central-01");
    expect(frame.tuner_key).toBe("airspy-0");
    assertValidFrame(frame);
  });

  describe("with fake timers", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("emits valid frames on an interval and stops when unsubscribed", () => {
      const frames: SpectrumFrame[] = [];
      const stop = subscribeMockSpectrum("rec-1", "rtl-0", (frame) => frames.push(frame));

      vi.advanceTimersByTime(800);
      expect(frames.length).toBeGreaterThanOrEqual(2);
      for (const frame of frames) {
        expect(frame.instance_id).toBe("rec-1");
        expect(frame.tuner_key).toBe("rtl-0");
        assertValidFrame(frame);
      }

      const seen = frames.length;
      stop();
      vi.advanceTimersByTime(2000);
      expect(frames.length).toBe(seen); // nothing more after unsubscribe
    });
  });
});
