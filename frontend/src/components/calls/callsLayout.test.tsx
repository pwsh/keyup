/**
 * Batch 2 of the 2026-10-29 UI review, Calls layout: the View menu (#4), the merged
 * flags column (#4), the phone toolbar (#22) and the memoised rows (#44).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CALLS } from "../../api/mock/dataset";
import { toCall } from "../../api/mock/filter";
import type { Call } from "../../api/types";
import { Calls } from "../../pages/Calls";
import { renderWithProviders } from "../../test/render";
import { CallCards, CallsTable, callFlags, normalizeColumns } from "./CallsTable";

// `useMediaQuery` caches each query's MediaQueryList for the life of the module; this
// answers from the current `innerWidth` so a case can switch to a phone.
vi.mock("../../lib/useMediaQuery", () => ({
  useMediaQuery: (query: string) => {
    const min = /\(min-width:\s*(\d+)px\)/.exec(query);
    return min ? window.innerWidth >= Number(min[1]) : false;
  },
}));

// Counts how many times a row (or card) rendered: each renders exactly one QualityIcon.
const renders = vi.hoisted(() => ({ count: 0 }));
vi.mock("../ui/QualityIcon", () => ({
  QualityIcon: () => {
    renders.count += 1;
    return <span data-testid="quality" />;
  },
}));

const base = toCall(CALLS[0] as (typeof CALLS)[number]);
const call = (overrides: Partial<Call>): Call => ({
  ...base,
  emergency: false,
  encrypted: false,
  tone_detected: false,
  alert: false,
  audio_meta: null,
  ...overrides,
});

describe("the merged flags column", () => {
  it("names each flag, in a fixed order, and none for a plain call", () => {
    expect(
      callFlags(call({ encrypted: true, tone_detected: true, alert: true })).map((f) => f.label),
    ).toEqual(["encrypted", "page"]);
    expect(callFlags(call({ tone_detected: true })).map((f) => f.label)).toEqual(["tone"]);
    expect(callFlags(call({ audio_meta: { version: 1, silent: true } })).map((f) => f.label)).toEqual([
      "silent call",
    ]);
    expect(callFlags(call({}))).toEqual([]);
  });

  it("puts the flags into the row's accessible name and into one column", () => {
    renderWithProviders(
      <CallsTable
        // A transcript of its own, so the mock's first call (which may have none, and then
        // shows "encrypted" in the transcript column too) cannot change what is found.
        calls={[
          call({
            id: 1,
            encrypted: true,
            tone_detected: true,
            alert: true,
            transcript_status: "done",
            transcript_text: "Engine 1 en route.",
          }),
        ]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite={false}
        onToggleStar={vi.fn()}
      />,
    );
    const list = screen.getByRole("list", { name: "Calls" });
    const row = within(list).getAllByRole("button")[0] as HTMLElement;
    // Named by its content, not an aria-label that says something else (WCAG 2.5.3):
    // the flags are in the name as words.
    expect(row).not.toHaveAttribute("aria-label");
    expect(row).toHaveAccessibleName(/encrypted\s*page/);
    // Each glyph carries its words for anyone not using the button's name.
    expect(within(row).getByText("encrypted")).toHaveClass("sr-only");
    expect(row.querySelectorAll("[data-flag]")).toHaveLength(2);
    // One header for the three old ones.
    expect(screen.getByText("Flags")).toBeInTheDocument();
    for (const old of ["Emg", "Enc", "Tone"]) expect(screen.queryByText(old)).not.toBeInTheDocument();
  });

  it("carries a saved column choice from before the merge over to the flags", () => {
    expect(normalizeColumns(["time", "emergency", "transcript"])).toEqual(["time", "flags", "transcript"]);
    expect(normalizeColumns(["time", "tone", "encrypted", "nonsense"])).toEqual(["time", "flags"]);
    expect(normalizeColumns([])).toContain("flags");
    expect(normalizeColumns(null)).toContain("transcript");
  });
});

describe("the desktop toolbar's View menu", () => {
  afterEach(() => localStorage.clear());

  it("holds the transcript-row switch and the column picker", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    // Neither is a toolbar control of its own any more.
    expect(screen.queryByRole("switch", { name: /Transcript row/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Columns/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /View/ }));
    expect(await screen.findByRole("switch", { name: /Transcript row/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Flags/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Frequency" })).not.toBeChecked();
  });

  it("puts the transcript on its own row by default from 1280 px", async () => {
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    // No "Transcript" column header in the strip: the text is on the row below.
    expect(screen.queryByRole("button", { name: "Sort by Transcript" })).not.toBeInTheDocument();
    const toolbar = screen.getByTestId("calls-toolbar");
    expect(within(toolbar).getByRole("button", { name: /Export/ })).toBeInTheDocument();
  });
});

describe("the phone toolbar", () => {
  beforeEach(() => {
    window.innerWidth = 390;
  });
  afterEach(() => {
    window.innerWidth = 1440;
    localStorage.clear();
  });

  it("is one row of Filters, the count, Live and More; the rest is behind More", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    const toolbar = screen.getByTestId("calls-toolbar");
    expect(within(toolbar).getByRole("button", { name: /^Filters/ })).toBeInTheDocument();
    expect(within(toolbar).getByText(/^\s*calls$/)).toBeInTheDocument();
    expect(within(toolbar).getByRole("status", { name: /Live updates/ })).toBeInTheDocument();
    expect(within(toolbar).queryByRole("switch", { name: /Audio only/ })).not.toBeInTheDocument();
    expect(within(toolbar).queryByRole("button", { name: /Export/ })).not.toBeInTheDocument();
    await user.click(within(toolbar).getByRole("button", { name: "More options" }));
    expect(await screen.findByRole("switch", { name: /Audio only/ })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /transcript quality/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Export/ })).toBeInTheDocument();
  });
});

/**
 * A live update hands the table a new array with one call added and the rest the same
 * objects. Only the new (and any changed) rows may render again.
 */
describe("memoised rows", () => {
  const calls = CALLS.slice(0, 20).map(toCall);
  let setList: (next: Call[]) => void = () => {};
  let setSelected: (next: number | null) => void = () => {};
  const onSelect = (_call: Call) => {};
  const onToggleStar = (_call: Call) => {};
  const onSortChange = (_sort: string) => {};

  function TableHarness() {
    const [list, set] = useState(calls);
    const [selected, select] = useState<number | null>(null);
    setList = set;
    setSelected = select;
    return (
      <div style={{ height: 800 }}>
        <CallsTable
          calls={list}
          selectedId={selected}
          onSelect={onSelect}
          sort="-started_at"
          onSortChange={onSortChange}
          canWrite
          onToggleStar={onToggleStar}
        />
      </div>
    );
  }

  function CardsHarness() {
    const [list, set] = useState(calls);
    setList = set;
    return <CallCards calls={list} selectedId={null} onSelect={onSelect} />;
  }

  it("re-renders only the arriving row and the one whose call changed", async () => {
    renderWithProviders(<TableHarness />);
    await waitFor(() => expect(renders.count).toBeGreaterThan(0));
    const mounted = screen.getAllByTestId("quality").length;
    expect(mounted).toBeGreaterThan(10);

    renders.count = 0;
    const arrival = { ...(calls[0] as Call), id: 99_000_001 };
    const changed = { ...(calls[3] as Call), transcript_text: "a transcript just landed" };
    act(() => setList([arrival, ...calls.slice(0, 3), changed, ...calls.slice(4)]));
    // Before memo: every mounted row rendered again (≈ `mounted`).
    expect(renders.count).toBe(2);

    renders.count = 0;
    act(() => setSelected(calls[5]?.id ?? null));
    expect(renders.count).toBe(1);
  });

  it("does the same for the phone's cards", () => {
    renderWithProviders(<CardsHarness />);
    renders.count = 0;
    act(() => setList([{ ...(calls[0] as Call), id: 99_000_002 }, ...calls]));
    expect(renders.count).toBe(1);
  });
});

describe("row and card names (WCAG 2.5.3, review batch 2 follow-up)", () => {
  const urgent = call({ id: 7, emergency: true, encrypted: true, tone_detected: true, alert: true });

  // jsdom joins the cells' text with no space between; a browser blockifies each flex
  // item and separates them, which the axe run checks for real.
  it("names a table row by its visible text, emergency first", () => {
    renderWithProviders(
      <CallsTable
        calls={[urgent]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite={false}
        onToggleStar={vi.fn()}
      />,
    );
    const row = within(screen.getByRole("list", { name: "Calls" })).getAllByRole("button")[0] as HTMLElement;
    expect(row).not.toHaveAttribute("aria-label");
    expect(row).toHaveAccessibleName(/^Emergency call.*encrypted\s*page/);
    // The visible tgid is part of the name now, which the old aria-label left out.
    expect(row).toHaveAccessibleName(new RegExp(String(urgent.tgid)));
  });

  it("puts a card's emergency and flags into the card button's own name", () => {
    renderWithProviders(<CallCards calls={[urgent]} selectedId={null} onSelect={vi.fn()} />);
    const card = within(screen.getByRole("list", { name: "Calls" })).getAllByRole("button")[0] as HTMLElement;
    expect(card).toHaveAccessibleName(/^Emergency call\s*encrypted\s*page/);
    // No focusable tooltip anchors nested inside the button.
    expect(card.querySelectorAll("[tabindex]")).toHaveLength(0);
  });
});

describe("Skip to calls", () => {
  afterEach(() => localStorage.clear());

  it("is the page's first focusable element and targets the list", async () => {
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    const focusables = document.querySelectorAll<HTMLElement>("a[href], button, input, [tabindex]");
    const skip = focusables[0] as HTMLElement;
    expect(skip).toHaveTextContent("Skip to calls");
    expect(skip).toHaveAttribute("href", "#calls-list");
    const target = document.getElementById("calls-list") as HTMLElement;
    expect(within(target).getByRole("list", { name: "Calls" })).toBeInTheDocument();
    expect(target).toHaveAttribute("tabindex", "-1");
  });
});
