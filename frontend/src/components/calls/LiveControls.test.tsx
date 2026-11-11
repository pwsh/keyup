import { act, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Providers, renderWithProviders } from "../../test/render";
import { ANNOUNCE_INTERVAL_MS, LiveIndicator, NewCallsPill, useThrottledAnnouncement } from "./LiveControls";

describe("the new-calls pill", () => {
  it("says nothing while nothing has arrived", () => {
    renderWithProviders(<NewCallsPill count={0} onClick={() => {}} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("counts the arrivals, in the singular for one", () => {
    renderWithProviders(<NewCallsPill count={1} onClick={() => {}} />);
    expect(screen.getByRole("button", { name: /1 new call$/ })).toBeInTheDocument();
  });

  it("counts them in the plural, with a thousands separator", () => {
    renderWithProviders(<NewCallsPill count={1234} onClick={() => {}} />);
    expect(screen.getByRole("button", { name: /1,234 new calls/ })).toBeInTheDocument();
  });

  it("asks to go to the top when pressed", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    renderWithProviders(<NewCallsPill count={3} onClick={onClick} />);
    await user.click(screen.getByRole("button", { name: /3 new calls/ }));
    expect(onClick).toHaveBeenCalledOnce();
  });
});

describe("the live indicator", () => {
  it("shows the connection state and offers the pause", async () => {
    const user = userEvent.setup();
    const onPausedChange = vi.fn();
    renderWithProviders(
      <LiveIndicator status="live" paused={false} onPausedChange={onPausedChange} reason={null} />,
    );
    expect(screen.getByLabelText("Live updates: Live")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pause live updates" }));
    expect(onPausedChange).toHaveBeenCalledWith(true);
  });

  it("reads as paused, and offers the resume, once it is", () => {
    renderWithProviders(<LiveIndicator status="paused" paused onPausedChange={() => {}} reason={null} />);
    expect(screen.getByLabelText("Live updates: Paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume live updates" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("says it is reconnecting while the socket is coming back", () => {
    renderWithProviders(
      <LiveIndicator status="reconnecting" paused={false} onPausedChange={() => {}} reason={null} />,
    );
    expect(screen.getByLabelText("Live updates: Reconnecting")).toBeInTheDocument();
  });
});

describe("the new-calls announcement", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps one status region mounted, empty while nothing has arrived", () => {
    const { rerender } = renderWithProviders(<NewCallsPill count={0} onClick={() => {}} />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent("");
    rerender(
      <Providers>
        <NewCallsPill count={2} onClick={() => {}} />
      </Providers>,
    );
    // The same node, now carrying the words: a region that mounts with its text
    // already in it is not reliably read.
    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent("2 new calls");
  });

  it("says the count at most once every ten seconds, and then the latest one", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ count }) => useThrottledAnnouncement(count), {
      initialProps: { count: 1 },
    });
    expect(result.current).toBe("1 new call");

    rerender({ count: 2 });
    act(() => vi.advanceTimersByTime(3_000));
    rerender({ count: 5 });
    act(() => vi.advanceTimersByTime(3_000));
    expect(result.current).toBe("1 new call");

    act(() => vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS - 6_000));
    expect(result.current).toBe("5 new calls");

    rerender({ count: 6 });
    act(() => vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS - 1));
    expect(result.current).toBe("5 new calls");
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe("6 new calls");
  });

  it("goes quiet without a word when the reader catches up", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ count }) => useThrottledAnnouncement(count), {
      initialProps: { count: 3 },
    });
    rerender({ count: 0 });
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(ANNOUNCE_INTERVAL_MS * 2));
    expect(result.current).toBe("");
  });
});

describe("the new-calls bar on a phone", () => {
  it("is a sticky bar in the flow below lg, floating only on the wide table", () => {
    renderWithProviders(<NewCallsPill count={1} onClick={() => {}} />);
    const bar = screen.getByTestId("new-calls-bar");
    expect(bar).toHaveClass("sticky", "top-0");
    expect(bar.className).not.toMatch(/(^|\s)absolute(\s|$)/);
    expect(bar).toHaveClass("lg:absolute");
  });
});

describe("the pause button", () => {
  it("is a 24 px target", () => {
    renderWithProviders(
      <LiveIndicator status="live" paused={false} onPausedChange={() => {}} reason={null} />,
    );
    expect(screen.getByRole("button", { name: "Pause live updates" })).toHaveClass("size-6");
  });
});
