import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { HourOfWeekHeatmap } from "./HourOfWeekHeatmap";

/** A 7x24 matrix whose only hot cell is Wednesday 17:00. */
function matrix(hot = 40, quiet = 2): (number | null)[][] {
  return Array.from({ length: 7 }, (_, weekday) =>
    Array.from({ length: 24 }, (_, hour) => (weekday === 2 && hour === 17 ? hot : quiet)),
  );
}

describe("the hour-of-week heatmap", () => {
  it("renders a full week of hours as a real table", () => {
    render(<HourOfWeekHeatmap matrix={matrix()} label="Listeners" unit="listeners" />);
    // Seven weekday rows, each with 24 value cells plus its row header.
    expect(screen.getAllByRole("row")).toHaveLength(8); // 7 days + the hour header
    expect(screen.getAllByRole("cell")).toHaveLength(7 * 24);
    expect(screen.getByRole("rowheader", { name: "Mon" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Sun" })).toBeInTheDocument();
  });

  it("names every hour column, including the ones whose label is not drawn", () => {
    render(<HourOfWeekHeatmap matrix={matrix()} label="Listeners" unit="listeners" />);
    const headers = screen.getAllByRole("columnheader").map((header) => header.textContent);
    expect(headers).toHaveLength(25); // the day column + 24 hours
    expect(headers).not.toContain("");
    expect(screen.getByRole("columnheader", { name: "01" })).toBeInTheDocument();
  });

  it("names every cell with its day, hour and value, so color is never the only cue", () => {
    render(<HourOfWeekHeatmap matrix={matrix()} label="Listeners" unit="listeners" />);
    expect(screen.getByLabelText("Wed 17:00 — 40 listeners")).toBeInTheDocument();
    expect(screen.getByLabelText("Mon 00:00 — 2 listeners")).toBeInTheDocument();
  });

  it("scales to the matrix's own maximum and says what it is", () => {
    // Two heatmaps on one page measure different things; a shared scale would make
    // one of them lie, so the legend states the maximum this grid is scaled to.
    render(<HourOfWeekHeatmap matrix={matrix(9)} label="Calls" unit="calls" />);
    expect(screen.getByText("9 calls")).toBeInTheDocument();
  });

  it("colors the hot cell and leaves a zero cell uncolored", () => {
    const grid = matrix(40, 0);
    render(<HourOfWeekHeatmap matrix={grid} label="Listeners" unit="listeners" />);
    const hot = screen.getByLabelText("Wed 17:00 — 40 listeners");
    const cold = screen.getByLabelText("Mon 00:00 — 0 listeners");
    expect(hot.getAttribute("style")).toMatch(/background-color/);
    expect(cold.getAttribute("style") ?? "").not.toMatch(/background-color/);
  });

  it("survives an empty matrix rather than throwing, and calls it no data", () => {
    // The page renders before the first request resolves, and a feed with no
    // history at all is a legitimate steady state — not an error. Absent is
    // "no data", though, never "0 listeners": the grid must not answer a
    // question it has never been asked.
    render(<HourOfWeekHeatmap matrix={[]} label="Listeners" unit="listeners" />);
    expect(screen.getAllByRole("cell")).toHaveLength(7 * 24);
    expect(screen.getByLabelText("Mon 00:00 — no data")).toBeInTheDocument();
  });

  it("tells an unpolled hour apart from a measured zero", () => {
    // The distinction the whole listener grid turns on. A `null` cell is an hour
    // nobody looked at; a 0 cell is an hour that was looked at and was empty, and
    // only the second is evidence about when people listen.
    const grid: (number | null)[][] = Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 5));
    (grid[0] as (number | null)[])[0] = null;
    (grid[0] as (number | null)[])[1] = 0;
    render(<HourOfWeekHeatmap matrix={grid} label="Listeners" unit="listeners" />);

    const unmeasured = screen.getByLabelText("Mon 00:00 — no data");
    const measuredZero = screen.getByLabelText("Mon 01:00 — 0 listeners");
    expect(unmeasured.className).toMatch(/repeating-linear-gradient/);
    expect(measuredZero.className).not.toMatch(/repeating-linear-gradient/);
    // …and the legend says the hatching means something, rather than leaving it
    // to be guessed from the pattern.
    expect(screen.getByText("no data")).toBeInTheDocument();
  });

  it("puts the peak and the sample count in the tooltip when it has them", () => {
    // An average of 40 built from two polls and one built from two hundred are
    // different claims, and the cell looks identical either way.
    const grid = matrix();
    const peaks = matrix(61, 4);
    const samples = matrix(180, 12).map((row) => row.map((cell) => cell ?? 0));
    render(
      <HourOfWeekHeatmap
        matrix={grid}
        maxMatrix={peaks}
        samplesMatrix={samples}
        label="Listeners"
        unit="listeners"
      />,
    );
    expect(screen.getByLabelText("Wed 17:00 — 40 listeners, peak 61, 180 samples")).toBeInTheDocument();
    // A peak that is not above the average adds nothing and is left out.
    render(<HourOfWeekHeatmap matrix={grid} maxMatrix={grid} label="L" unit="listeners" />);
    expect(screen.getAllByLabelText("Mon 00:00 — 2 listeners").length).toBeGreaterThan(0);
  });

  it("uses the caption as the table's accessible name", () => {
    render(<HourOfWeekHeatmap matrix={matrix()} label="Average listeners" unit="listeners" />);
    expect(screen.getByRole("table", { name: "Average listeners" })).toBeInTheDocument();
  });
});
