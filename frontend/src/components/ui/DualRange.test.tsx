import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderWithProviders } from "../../test/render";
import { DualRange, RangeSlider, THUMB } from "./DualRange";

/** WCAG 2.5.8: a slider's thumb is the whole target, so it is the one that must be 24 px. */
describe("range inputs are 24 px targets", () => {
  it("thumbs are 24 px across in both engines", () => {
    for (const engine of ["webkit-slider-thumb", "moz-range-thumb"]) {
      expect(THUMB).toContain(`[&::-${engine}]:h-6`);
      expect(THUMB).toContain(`[&::-${engine}]:w-6`);
      expect(THUMB).toContain(`[&::-${engine}]:box-border`);
    }
  });

  it("the two-thumb range uses them, on a 24 px input", () => {
    renderWithProviders(
      <DualRange
        min={0}
        max={60}
        low={0}
        high={60}
        onChange={() => {}}
        labelLow="Shortest"
        labelHigh="Longest"
        tip="Call length"
        format={String}
      />,
    );
    for (const name of ["Shortest", "Longest"]) {
      const input = screen.getByRole("slider", { name });
      expect(input).toHaveClass("h-6", "[&::-webkit-slider-thumb]:h-6", "[&::-moz-range-thumb]:w-6");
    }
  });

  it("the single slider is 24 px tall, not the 4 px track it used to be", () => {
    renderWithProviders(
      <RangeSlider min={0} max={2} value={1} onChange={() => {}} label="Speed" tip="Playback speed" />,
    );
    const input = screen.getByRole("slider", { name: "Speed" });
    expect(input).toHaveClass("h-6", "[&::-webkit-slider-thumb]:h-6");
    expect(input).not.toHaveClass("h-1");
  });
});
