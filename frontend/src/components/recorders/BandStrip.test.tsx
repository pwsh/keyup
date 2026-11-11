import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { OccupancyChannel, SiteOccupancy } from "../../api/types";
import { BandStrip } from "./BandStrip";

/**
 * The site band strip's scale.
 *
 * The cases worth writing are the ones where the naive scale — min to max of everything the
 * fold carried — lies about where the site actually transmits: an IDEN `base_hz` that is a
 * numbering origin rather than a frequency, and a corrupt frequency announced once by the
 * control channel. Both used to squeeze every real channel into a sliver.
 */
function channel(frequencyHz: number, extra: Partial<OccupancyChannel> = {}): OccupancyChannel {
  return {
    frequency_hz: frequencyHz,
    channel: null,
    role: "voice",
    grants: 10,
    calls: 5,
    last_seen: null,
    ...extra,
  };
}

function occupancy(channels: OccupancyChannel[], bases: number[] = []): SiteOccupancy {
  return {
    site_id: 1,
    site_name: "Central",
    system_id: 1,
    observed_at: null,
    bands: bases.map((base_hz, i) => ({ id: i, base_hz, spacing_hz: 12_500, bandwidth_hz: 12_500 })),
    channels,
  };
}

/** The 800 MHz channels of the site that prompted this, evenly spread across 851–860. */
const eightHundred = [
  851_012_500, 851_512_500, 852_262_500, 853_737_500, 855_012_500, 856_487_500, 857_737_500, 858_512_500,
  859_987_500, 860_000_000,
].map((hz) => channel(hz));

/** The edge labels the strip prints under each segment, in order. */
function edgeLabels(): string[] {
  return screen
    .getAllByTestId("band-strip-labels")
    .flatMap((group) => [...group.children].map((el) => el.textContent ?? ""));
}

describe("the site band strip's scale", () => {
  it("ignores an IDEN base_hz far below the channels it numbers", () => {
    // Band 0 bases at 769 MHz while every channel is 851–860: the base is a numbering
    // origin, not a transmitted frequency, and must not widen the scale.
    render(<BandStrip occupancy={occupancy(eightHundred, [769_000_000])} />);
    expect(screen.getAllByTestId("band-strip-segment")).toHaveLength(1);
    const labels = edgeLabels();
    expect(labels[0]).toBe("850.8328 MHz");
    expect(labels[1]).toBe("860.1798 MHz");
    expect(screen.queryByTestId("band-strip-outside")).not.toBeInTheDocument();
  });

  it("pins an announced-but-corrupt outlier at the edge and names it in a tooltip", () => {
    const channels = [...eightHundred, channel(885_587_500, { grants: 0, calls: 0 })];
    render(<BandStrip occupancy={occupancy(channels, [769_000_000])} />);
    // One segment: the stray frequency carries no activity, so the 800 cluster alone
    // already holds well over 90% of it.
    expect(screen.getAllByTestId("band-strip-segment")).toHaveLength(1);
    expect(edgeLabels()[1]).toBe("860.1798 MHz");
    // …and it is still on the strip, hollow, at the end nearest to it.
    const marker = screen.getByTestId("band-strip-outside");
    expect(marker).toHaveAttribute("fill", "none");
    expect(marker.querySelector("title")?.textContent).toBe("outside band: 885.5875 MHz");
  });

  it("draws one segment per band on a genuine 700+800 site", () => {
    const sevenHundred = [773_006_250, 773_506_250, 774_256_250, 775_500_000, 776_993_750].map((hz) =>
      channel(hz),
    );
    render(<BandStrip occupancy={occupancy([...sevenHundred, ...eightHundred], [769_000_000])} />);
    // Neither band clears 90% alone, so both are drawn — with their own edge labels.
    const segments = screen.getAllByTestId("band-strip-segment");
    expect(segments).toHaveLength(2);
    const labels = edgeLabels();
    expect(labels[0]).toBe("772.9265 MHz");
    expect(labels[3]).toBe("860.1798 MHz");
    expect(screen.queryByTestId("band-strip-outside")).not.toBeInTheDocument();
  });
});
