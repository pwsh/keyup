import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SIDEBAR_PANEL, usePanelOpen, useStoredOpen } from "./panels";

/**
 * The sidebar and the player both read `keyup-sidebar`. Each used to keep its own copy,
 * so collapsing the sidebar with `[` left the player laid out for the old width.
 */
describe("the shared panel store", () => {
  it("updates every reader of a panel when one of them changes it", () => {
    const sidebar = renderHook(() => usePanelOpen(SIDEBAR_PANEL));
    const player = renderHook(() => usePanelOpen(SIDEBAR_PANEL));
    expect(sidebar.result.current[0]).toBe(true);
    expect(player.result.current[0]).toBe(true);

    act(() => sidebar.result.current[1](false));

    expect(sidebar.result.current[0]).toBe(false);
    expect(player.result.current[0]).toBe(false);
    expect(localStorage.getItem(SIDEBAR_PANEL)).toBe("closed");
  });

  it("starts from what localStorage remembered", () => {
    localStorage.setItem("keyup-test-section", "closed");
    const { result } = renderHook(() => useStoredOpen("keyup-test-section", true));
    expect(result.current[0]).toBe(false);
    localStorage.removeItem("keyup-test-section");
  });

  it("keeps panels independent of one another", () => {
    const a = renderHook(() => useStoredOpen("keyup-test-a", true));
    const b = renderHook(() => useStoredOpen("keyup-test-b", true));
    act(() => a.result.current[1](false));
    expect(a.result.current[0]).toBe(false);
    expect(b.result.current[0]).toBe(true);
    localStorage.removeItem("keyup-test-a");
  });
});
