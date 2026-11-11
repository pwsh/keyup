import { describe, expect, it } from "vitest";
import { TONE_DEPTH_DB, toneGainPoints } from "./normalize";

/**
 * The arithmetic behind the player's tone dip.
 *
 * Normalize's compressor and its 18 dB of make-up gain undo what the server's §5.5 pass
 * did: production call 2396243 is a 2 s console attention tone the pass took down to about
 * -33 dB in the stored file, and a call whose only loud thing is that tone comes back out
 * of the compressor at full blast. The dip is scheduled on a gain node of its own, and this
 * is the pure half — where the points land and how deep they go.
 */

const dbToGain = (db: number) => 10 ** (-db / 20);

describe("toneGainPoints", () => {
  it("dips over a span and comes back up after it", () => {
    const points = toneGainPoints([{ start_s: 0.168, end_s: 2.248, pattern: "single", attenuate_db: 30 }]);

    expect(points).toEqual([
      { at: 0.168, gain: dbToGain(30) },
      { at: 2.248, gain: 1 },
    ]);
  });

  it("uses the depth the pass actually applied", () => {
    const dip = toneGainPoints([{ start_s: 0, end_s: 1, attenuate_db: 12 }])[0];
    expect(dip?.gain).toBeCloseTo(dbToGain(12), 6);
  });

  it("falls back to the pattern when a span predates the stored depth", () => {
    const page = toneGainPoints([{ start_s: 0, end_s: 1, pattern: "two_tone" }])[0];
    const lone = toneGainPoints([{ start_s: 0, end_s: 1, pattern: "single" }])[0];
    expect(page?.gain).toBeCloseTo(dbToGain(TONE_DEPTH_DB.page), 6);
    expect(lone?.gain).toBeCloseTo(dbToGain(TONE_DEPTH_DB.single), 6);
    // …and an unlabelled span is treated as the lone tone, the deeper of the two.
    const bare = toneGainPoints([{ start_s: 0, end_s: 1 }])[0];
    expect(bare?.gain).toBeCloseTo(dbToGain(TONE_DEPTH_DB.single), 6);
  });

  it("moves the spans by the head trim when the processed copy is playing", () => {
    const points = toneGainPoints([{ start_s: 0.5, end_s: 2.0, attenuate_db: 18 }], {
      offsetS: 0.3,
    });
    expect(points.map((p) => p.at)).toEqual([0.2, 1.7]);
  });

  it("never puts a point before the start of the file", () => {
    const points = toneGainPoints([{ start_s: 0.1, end_s: 1.0, attenuate_db: 18 }], {
      offsetS: 0.5,
    });
    expect(points[0]?.at).toBe(0);
  });

  it("drops spans the playhead is already past", () => {
    const points = toneGainPoints(
      [
        { start_s: 0, end_s: 1, attenuate_db: 18 },
        { start_s: 4, end_s: 5, attenuate_db: 18 },
      ],
      { from: 2 },
    );
    expect(points.map((p) => p.at)).toEqual([4, 5]);
  });

  it("dips immediately when a seek lands inside a tone", () => {
    const points = toneGainPoints([{ start_s: 1, end_s: 5, attenuate_db: 24 }], { from: 3 });
    expect(points).toEqual([
      { at: 3, gain: dbToGain(24) },
      { at: 5, gain: 1 },
    ]);
  });

  it("orders several spans and keeps each one's own depth", () => {
    const points = toneGainPoints([
      { start_s: 3, end_s: 4, pattern: "two_tone", attenuate_db: 18 },
      { start_s: 0, end_s: 2, pattern: "single", attenuate_db: 30 },
    ]);
    expect(points.map((p) => p.at)).toEqual([0, 2, 3, 4]);
    expect(points[0]?.gain).toBeCloseTo(dbToGain(30), 6);
    expect(points[2]?.gain).toBeCloseTo(dbToGain(18), 6);
  });

  it("has nothing to say about a call with no tones", () => {
    expect(toneGainPoints(null)).toEqual([]);
    expect(toneGainPoints([])).toEqual([]);
    // …or about a span that is not a span.
    expect(toneGainPoints([{ start_s: 2, end_s: 1 }])).toEqual([]);
    expect(toneGainPoints([{ start_s: Number.NaN, end_s: 1 }])).toEqual([]);
  });
});
