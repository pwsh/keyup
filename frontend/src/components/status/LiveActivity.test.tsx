import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActivityEvent } from "../../api/types";
import { renderWithProviders } from "../../test/render";

/**
 * The live stream is mocked rather than driven: `useActivityStream` opens a real
 * `EventSource`, which jsdom does not have, and the thing worth testing here is that
 * whatever the stream hands the panel is merged into the ticker alongside the polled
 * rows — not that `EventSource` works.
 */
let streamed: ActivityEvent[] = [];
vi.mock("../../api/live", () => ({
  useActivityStream: () => ({ events: streamed, state: "open", clear: () => {} }),
}));

import {
  diskSentence,
  healthSentence,
  heartbeatTone,
  ListenLive,
  LiveActivity,
  trendOf,
  worstDisk,
} from "./LiveActivity";

beforeEach(() => {
  streamed = [];
});

describe("the heartbeat thresholds", () => {
  it("is green for a call in the last minute", () => {
    expect(heartbeatTone(0)).toBe("live");
    expect(heartbeatTone(59.9)).toBe("live");
    expect(heartbeatTone(60)).toBe("live");
  });

  it("is amber between a minute and five", () => {
    expect(heartbeatTone(61)).toBe("recent");
    expect(heartbeatTone(299)).toBe("recent");
  });

  it("is red from five minutes on", () => {
    expect(heartbeatTone(300)).toBe("quiet");
    expect(heartbeatTone(6_000)).toBe("quiet");
  });

  it("treats 'never heard' as its own state, not the worst one", () => {
    expect(heartbeatTone(null)).toBe("silent");
    expect(heartbeatTone(undefined)).toBe("silent");
  });
});

describe("the trend against the previous window", () => {
  it("reads the pair the endpoint sends", () => {
    expect(trendOf({ current: 9, previous: 4 })).toBe("up");
    expect(trendOf({ current: 4, previous: 9 })).toBe("down");
    expect(trendOf({ current: 4, previous: 4 })).toBe("flat");
  });
});

describe("the panel", () => {
  it("renders the counters, the recorders and the export services", async () => {
    renderWithProviders(<LiveActivity detail />);

    expect(await screen.findByText("Live activity")).toBeInTheDocument();
    // The fixture's counters (src/api/mock/dataset.ts → liveActivity).
    expect(await screen.findByText("Ingested")).toBeInTheDocument();
    expect(screen.getByText("Merged dupes")).toBeInTheDocument();
    expect(screen.getByText("Transcribed")).toBeInTheDocument();

    const recorders = screen.getByRole("region", { name: "Recorders" });
    expect(within(recorders).getByText("Signal box")).toBeInTheDocument();
    expect(within(recorders).getByText("Attic spare")).toBeInTheDocument();

    const exports = screen.getByRole("region", { name: "Exports" });
    expect(within(exports).getByText("broadcastify")).toBeInTheDocument();
    expect(within(exports).getByText("openmhz")).toBeInTheDocument();
  });

  it("hides the skip reasons from a guest and shows them to an operator", async () => {
    const { unmount } = renderWithProviders(<LiveActivity />);
    const guestExports = await screen.findByRole("region", { name: "Exports" });
    expect(within(guestExports).queryByText(/no speech$/)).not.toBeInTheDocument();
    expect(within(guestExports).getAllByText(/uploaded/).length).toBeGreaterThan(0);
    unmount();

    renderWithProviders(<LiveActivity detail />);
    const adminExports = await screen.findByRole("region", { name: "Exports" });
    expect(within(adminExports).getAllByText(/no speech/).length).toBeGreaterThan(0);
  });

  it("appends events the stream pushes to the polled ticker", async () => {
    streamed = [
      {
        id: 9_999_999,
        at: new Date().toISOString(),
        source: "export",
        level: "error",
        kind: "export.failed",
        message: "broadcastify refused call 4242: 503",
        call_id: 4242,
        meta: null,
      },
    ];
    renderWithProviders(<LiveActivity detail />);

    const ticker = await screen.findByRole("region", { name: "Recent events" });
    await waitFor(() =>
      expect(within(ticker).getByText("broadcastify refused call 4242: 503")).toBeInTheDocument(),
    );
    // It is newest, so it sorts to the top, and it links to the call it names.
    const rows = within(ticker).getAllByRole("listitem");
    expect(within(rows[0] as HTMLElement).getByRole("link")).toHaveAttribute("href", "/calls/4242");
    // The polled rows are still there underneath it.
    expect(rows.length).toBeGreaterThan(1);
  });
});

describe("the link to a recorder's own live monitor", () => {
  it("offers one for the recorder that reported a panel and none for the one that did not", async () => {
    renderWithProviders(<LiveActivity detail />);

    const recorders = await screen.findByRole("region", { name: "Recorders" });
    const rows = within(recorders).getAllByRole("listitem");
    const signal = rows.find((row) => within(row).queryByText("Signal box"));
    const attic = rows.find((row) => within(row).queryByText("Attic spare"));

    const link = within(signal as HTMLElement).getByRole("link", { name: "Listen live" });
    // The recorder's own panel, opened away from keyup — which does not carry this audio.
    expect(link).toHaveAttribute("href", "http://192.0.2.30:8095#monitor");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(within(signal as HTMLElement).queryByText("(on that machine)")).not.toBeInTheDocument();

    expect(within(attic as HTMLElement).queryByRole("link", { name: "Listen live" })).not.toBeInTheDocument();
  });

  it("says so when the panel is on the recorder's own loopback", () => {
    renderWithProviders(
      <ListenLive
        monitor={{
          enabled: true,
          url: "http://127.0.0.1:8095",
          stream_url: "http://127.0.0.1:8095/api/monitor/stream",
          token_required: false,
          local_only: true,
        }}
      />,
    );
    expect(screen.getByRole("link", { name: "Listen live" })).toHaveAttribute(
      "href",
      "http://127.0.0.1:8095#monitor",
    );
    expect(screen.getByText("(on that machine)")).toBeInTheDocument();
  });

  it("offers nothing when the monitor is off or has no address", () => {
    const { unmount } = renderWithProviders(
      <ListenLive
        monitor={{
          enabled: false,
          url: "http://192.0.2.30:8095",
          stream_url: null,
          token_required: false,
          local_only: false,
        }}
      />,
    );
    expect(screen.queryByRole("link", { name: "Listen live" })).not.toBeInTheDocument();
    unmount();

    renderWithProviders(
      <ListenLive
        monitor={{
          enabled: true,
          url: null,
          stream_url: null,
          token_required: true,
          local_only: false,
        }}
      />,
    );
    expect(screen.queryByRole("link", { name: "Listen live" })).not.toBeInTheDocument();
  });
});

describe("the disk banner's choice", () => {
  const volume = (level: "ok" | "warn" | "critical", path: string) => ({
    path,
    free_bytes: 28 * 1024 ** 3,
    total_bytes: 250 * 1024 ** 3,
    free_percent: 11.2,
    level,
    growth_bytes_per_day: 4 * 1024 ** 3,
    days_to_full: 6.7,
  });

  it("says nothing while every disk is fine", () => {
    expect(worstDisk([volume("ok", "/a")])).toBeNull();
    expect(worstDisk(undefined)).toBeNull();
  });

  it("names the disk in the worst shape, with its days to full", () => {
    const worst = worstDisk([volume("warn", "/a"), volume("critical", "/b"), volume("ok", "/c")]);
    expect(worst?.path).toBe("/b");
    expect(diskSentence(volume("warn", "/srv/keyup/audio"))).toMatch(
      /free \(11 %\) on \/srv\/keyup\/audio — full in about 7 days$/,
    );
  });
});

describe("the health banner's wording", () => {
  it("names the check and the subject, then says what the worker saw", () => {
    expect(
      healthSentence({
        key: "recorder_silent:signal",
        check: "recorder_silent",
        message: "recorder signal has not reported for 12 min (threshold 5 min)",
      }),
    ).toBe("Recorder silent (signal): recorder signal has not reported for 12 min (threshold 5 min)");
    expect(healthSentence({ key: "backup_stale", check: "backup_stale", message: null })).toBe(
      "Backup overdue: raised",
    );
  });
});
