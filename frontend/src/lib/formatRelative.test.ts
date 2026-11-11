import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatRelative } from "./format";

describe("formatRelative", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("says ago for the past", () => {
    expect(formatRelative("2026-10-15T09:00:00Z")).toBe("3 hours ago");
  });

  it("says in for the future, so a certificate's expiry does not read as expired", () => {
    expect(formatRelative("2026-12-03T15:31:03Z")).toBe("in 2 months");
  });

  it("is a dash for nothing or nonsense", () => {
    expect(formatRelative(null)).toBe("—");
    expect(formatRelative("not a date")).toBe("—");
  });
});
