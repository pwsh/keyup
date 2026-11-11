import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CALLS } from "../../api/mock/dataset";
import { toCall } from "../../api/mock/filter";
import type { Call } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { CallCards } from "./CallsTable";

const base = toCall(CALLS[0] as (typeof CALLS)[number]);

const call = (overrides: Partial<Call>): Call => ({ ...base, id: 900_001, ...overrides });

function render(one: Call) {
  renderWithProviders(<CallCards calls={[one]} selectedId={null} onSelect={vi.fn()} />);
  return within(screen.getByRole("list", { name: "Calls" }));
}

/**
 * The badges answer "is this row worth opening" at a glance, and the three states are
 * mutually exclusive on purpose — a silent call has no tones to mark, and a page is
 * already a tone.  Each case below is one of those states, plus the one that must *not*
 * appear: `alert` is not `emergency`, and neither may be drawn as the other.
 */
describe("the tone badges on a call row", () => {
  it("marks a page with a filled bell", () => {
    const list = render(call({ tone_detected: true, alert: true, emergency: null }));
    expect(list.getByText("page")).toBeInTheDocument();
    expect(list.queryByText("tone")).not.toBeInTheDocument();
  });

  it("marks a plain tone without calling it a page", () => {
    const list = render(call({ tone_detected: true, alert: false }));
    expect(list.getByText("tone")).toBeInTheDocument();
    expect(list.queryByText("page")).not.toBeInTheDocument();
  });

  it("marks a call that measured as silence", () => {
    const list = render(
      call({ tone_detected: false, alert: false, audio_meta: { version: 1, silent: true } }),
    );
    expect(list.getByText("silent call")).toBeInTheDocument();
  });

  it("puts nothing in the gutter for an ordinary call", () => {
    const list = render(call({ tone_detected: false, alert: false, audio_meta: null }));
    expect(list.queryByText("tone")).not.toBeInTheDocument();
    expect(list.queryByText("page")).not.toBeInTheDocument();
    expect(list.queryByText("silent call")).not.toBeInTheDocument();
  });

  it("does not treat a page as an emergency", () => {
    const list = render(call({ tone_detected: true, alert: true, emergency: null }));
    expect(list.getByText("page")).toBeInTheDocument();
    expect(list.queryByText("Emergency call")).not.toBeInTheDocument();
  });

  it("shows both when a call is flagged emergency and carries tones", () => {
    const list = render(call({ tone_detected: true, alert: true, emergency: true }));
    expect(list.getByText("Emergency call")).toBeInTheDocument();
    expect(list.getByText("page")).toBeInTheDocument();
  });
});
