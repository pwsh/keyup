import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HIGHLIGHT_MS, useFreshIds } from "./useFreshIds";

describe("useFreshIds", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("keeps each arrival fresh for its own four seconds", () => {
    const { result } = renderHook(() => useFreshIds());
    act(() => result.current.mark([1]));
    act(() => vi.advanceTimersByTime(3000));
    // A second arrival does not cut the first one short, nor extend it.
    act(() => result.current.mark([2]));
    expect([...result.current.freshIds].sort()).toEqual([1, 2]);

    act(() => vi.advanceTimersByTime(HIGHLIGHT_MS - 3000));
    expect([...result.current.freshIds]).toEqual([2]);

    act(() => vi.advanceTimersByTime(3000));
    expect(result.current.freshIds.size).toBe(0);
  });

  it("forgets everything on clear", () => {
    const { result } = renderHook(() => useFreshIds());
    act(() => result.current.mark([1, 2]));
    act(() => result.current.clear());
    expect(result.current.freshIds.size).toBe(0);
  });
});
