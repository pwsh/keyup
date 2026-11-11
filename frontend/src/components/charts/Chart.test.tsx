import { screen } from "@testing-library/react";
import type { HTMLAttributes } from "react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { Chart } from "./Chart";

// ECharts draws to a canvas jsdom does not have; the wrapper's own container div is
// what carries the role and the name, so stand the library in with that div.
vi.mock("echarts-for-react/lib/core", () => ({
  default: ({
    echarts: _echarts,
    option: _option,
    notMerge: _notMerge,
    lazyUpdate: _lazyUpdate,
    onEvents: _onEvents,
    ...rest
  }: HTMLAttributes<HTMLDivElement> & Record<string, unknown>) => <div {...rest} />,
}));

describe("Chart", () => {
  it("is one image with a name, not a role-less div with an aria-label", () => {
    renderWithProviders(
      <Chart
        title="Calls per hour"
        tip="How many calls arrived each hour"
        option={{}}
        data={{ headers: ["hour", "calls"], rows: [[0, 1]] }}
        filename="calls-per-hour"
      />,
    );
    expect(
      screen.getByRole("img", { name: "Calls per hour. How many calls arrived each hour" }),
    ).toBeInTheDocument();
  });
});
