import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { BarChart, niceTicks } from "./BarChart";

const HOURS = Array.from({ length: 24 }, (_, hour) => `${String(hour).padStart(2, "0")}:00`);
const COUNTS = HOURS.map((_, hour) => (hour === 14 ? 37 : hour % 3));

function renderChart(horizontal = false) {
  const labels = horizontal ? ["E1", "M7", "Ladder 4"] : HOURS;
  const values = horizontal ? [12, 7, 3] : COUNTS;
  return renderWithProviders(
    <BarChart
      title="Calls per hour of the day"
      tip="How busy this channel is by hour"
      labels={labels}
      values={values}
      color="#2a78d6"
      seriesName="Calls"
      horizontal={horizontal}
      filename="test"
      data={{ headers: ["Label", "Calls"], rows: labels.map((label, index) => [label, values[index] ?? 0]) }}
    />,
  );
}

describe("niceTicks", () => {
  it("rounds the top up to a 1-2-5 step", () => {
    expect(niceTicks(37)).toEqual([0, 10, 20, 30, 40]);
    expect(niceTicks(40)).toEqual([0, 10, 20, 30, 40]);
    expect(niceTicks(1234)).toEqual([0, 500, 1000, 1500]);
  });

  it("never splits a count into fractions", () => {
    expect(niceTicks(2)).toEqual([0, 1, 2]);
    expect(niceTicks(0)).toEqual([0, 1]);
  });
});

describe("BarChart", () => {
  it("draws one bar per value, inside the chart card, without ECharts", () => {
    const { container } = renderChart();
    const plot = screen.getByRole("img", { name: /Calls per hour of the day/ });
    expect(plot.querySelectorAll("path")).toHaveLength(24);
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("names the bar under the pointer", () => {
    renderChart();
    const bands = screen.getByRole("img", { name: /Calls per hour/ }).querySelectorAll("rect");
    expect(bands).toHaveLength(24);
    fireEvent.mouseEnter(bands[14] as Element);
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("14:00");
    expect(tooltip).toHaveTextContent("Calls37");
    fireEvent.mouseLeave(bands[14] as Element);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("labels each row and its value when horizontal", () => {
    renderChart(true);
    for (const text of ["E1", "M7", "Ladder 4", "12", "7", "3"]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
  });

  it("still has the table view", async () => {
    renderChart(true);
    await userEvent.click(screen.getByRole("button", { name: "Show the numbers as a table" }));
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Ladder 4" })).toBeInTheDocument();
  });
});
