import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderWithProviders } from "../../test/render";
import { MultiSelect, type MultiSelectOption, orderOptions } from "./MultiSelect";

const now = Date.parse("2026-11-01T12:00:00Z");
const ago = (minutes: number) => new Date(now - minutes * 60_000).toISOString();

/** Review #42: the local county was buried under the alphabet. */
const TALKGROUPS: MultiSelectOption[] = [
  { value: "1", label: "ASH FIRE", group: "Ash County", lastHeard: ago(600) },
  { value: "2", label: "BIRCH LAW", group: "Birch County", lastHeard: null },
  { value: "3", label: "EC FIRE", group: "Example County", lastHeard: ago(10) },
  { value: "4", label: "EC LAW 1", group: "Example County", lastHeard: ago(1) },
  { value: "5", label: "NORTH EMS", group: "North County", lastHeard: ago(60) },
];

describe("MultiSelect ordering", () => {
  it("puts recently heard groups and options first, never-heard last", () => {
    const { recent, groups } = orderOptions(TALKGROUPS, 400);
    expect(recent).toBe(true);
    expect(groups.map(([group]) => group)).toEqual([
      "Example County",
      "North County",
      "Ash County",
      "Birch County",
    ]);
    expect(groups[0]?.[1].map((o) => o.label)).toEqual(["EC LAW 1", "EC FIRE"]);
  });

  it("cuts the stalest options, not the latest, at maxVisible", () => {
    const { groups } = orderOptions(TALKGROUPS, 2);
    expect(groups.flatMap(([, items]) => items.map((o) => o.value))).toEqual(["4", "3"]);
  });

  it("stays alphabetical by group when nothing says when it was heard", () => {
    const plain = TALKGROUPS.map(({ lastHeard: _, ...rest }) => rest).reverse();
    const { recent, groups } = orderOptions(plain, 400);
    expect(recent).toBe(false);
    expect(groups.map(([group]) => group)).toEqual([
      "Ash County",
      "Birch County",
      "Example County",
      "North County",
    ]);
  });

  it("renders the picker in that order and says so", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <MultiSelect
        label="Talkgroups"
        tip="Pick channels."
        options={TALKGROUPS}
        selected={[]}
        onChange={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Talkgroups" }));
    const dialog = await screen.findByText(/Most recently heard first/);
    const list = dialog.closest("p")?.parentElement as HTMLElement;
    const labels = within(list)
      .getAllByRole("button", { pressed: false })
      .map((button) => button.textContent);
    expect(labels).toEqual(["EC LAW 1", "EC FIRE", "NORTH EMS", "ASH FIRE", "BIRCH LAW"]);
  });
});
