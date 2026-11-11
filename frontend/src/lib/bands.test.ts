import { describe, expect, it } from "vitest";
import {
  BAND_LABELS,
  bandSpan,
  bandsReachSpan,
  canonicalBand,
  formatBand,
  formatBands,
  hzFromMhz,
  mhz,
  sameBands,
} from "./bands";

describe("band entries", () => {
  it("reads every label the editor offers", () => {
    for (const label of BAND_LABELS) {
      expect(bandSpan(label)).not.toBeNull();
    }
    // Case and spacing are the operator's, not the contract's.
    expect(canonicalBand(" VHF ")).toBe("vhf");
    expect(bandSpan("VHF")).toEqual([136_000_000, 174_000_000]);
  });

  it("reads an explicit hertz range and normalizes its spacing", () => {
    expect(canonicalBand("856000000 - 866000000")).toBe("856000000-866000000");
    expect(bandSpan("856000000-866000000")).toEqual([856_000_000, 866_000_000]);
  });

  it("refuses to read a range that runs backwards or is not one", () => {
    expect(bandSpan("866000000-856000000")).toBeNull();
    expect(bandSpan("-5-10")).toBeNull();
    expect(bandSpan("800000000-")).toBeNull();
    expect(bandSpan("ham-2m")).toBeNull();
  });

  it("shows a band a recorder configured that keyup cannot read, verbatim", () => {
    // The reported list is the recorder's own configuration talking. "A band I do not
    // recognize" is still an answer to what the tuner is restricted to, and dropping it
    // would say the tuner is less restricted than it is.
    expect(formatBand("ham-2m")).toBe("ham-2m");
    expect(canonicalBand("Ham-2m")).toBe("ham-2m");
  });
});

describe("band display", () => {
  it("says megahertz without trailing zeros", () => {
    expect(mhz(856_000_000)).toBe("856");
    expect(mhz(851_012_500)).toBe("851.0125");
    expect(formatBand("856000000-866500000")).toBe("856–866.5 MHz");
    expect(formatBand("800")).toBe("800");
    expect(formatBand("vhf")).toBe("VHF");
  });

  it("calls an empty list a state rather than a list", () => {
    expect(formatBands([])).toBe("unrestricted");
    expect(formatBands(["800", "856000000-866000000"])).toBe("800 · 856–866 MHz");
  });
});

describe("megahertz typed into the editor", () => {
  it("becomes whole hertz", () => {
    expect(hzFromMhz("856")).toBe(856_000_000);
    expect(hzFromMhz("866.5")).toBe(866_500_000);
    expect(hzFromMhz(" 851.0125 ")).toBe(851_012_500);
  });

  it("is null when it is not a frequency", () => {
    expect(hzFromMhz("")).toBeNull();
    expect(hzFromMhz("abc")).toBeNull();
    expect(hzFromMhz("-5")).toBeNull();
  });
});

describe("comparing two lists", () => {
  it("ignores order, spelling and repetition", () => {
    expect(sameBands(["800", "VHF"], ["vhf", "800"])).toBe(true);
    expect(sameBands(["800", "800"], ["800"])).toBe(true);
    expect(sameBands(["800"], ["900"])).toBe(false);
    expect(sameBands(null, [])).toBe(true);
    expect(sameBands(null, ["800"])).toBe(false);
  });
});

describe("reaching the capture span", () => {
  const lo = 850_800_000;
  const hi = 853_200_000;

  it("is true for no restriction at all", () => {
    expect(bandsReachSpan([], lo, hi)).toBe(true);
  });

  it("is true when any one band overlaps", () => {
    expect(bandsReachSpan(["vhf", "800"], lo, hi)).toBe(true);
  });

  it("is false only when every readable band misses it", () => {
    expect(bandsReachSpan(["vhf"], lo, hi)).toBe(false);
    expect(bandsReachSpan(["900000000-910000000"], lo, hi)).toBe(false);
  });

  it("stays quiet about a list it cannot read", () => {
    // trunkd is the one that decides; a warning under a list that is going to work is
    // worse than no warning at all.
    expect(bandsReachSpan(["ham-2m"], lo, hi)).toBe(true);
  });
});
