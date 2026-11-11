import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CALLS } from "../../api/mock/dataset";
import { toCall } from "../../api/mock/filter";
import type { Call } from "../../api/types";
import { noTranscriptReason } from "../../lib/transcriptReason";
import { renderWithProviders } from "../../test/render";
import { CallCards, CallsTable } from "./CallsTable";

const rows: Call[] = CALLS.slice(0, 40).map(toCall);

function renderTable(overrides: Partial<Parameters<typeof CallsTable>[0]> = {}) {
  const onSelect = vi.fn();
  const onToggleStar = vi.fn();
  const onSortChange = vi.fn();
  renderWithProviders(
    <div style={{ height: 800 }}>
      <CallsTable
        calls={rows}
        selectedId={null}
        onSelect={onSelect}
        sort="-started_at"
        onSortChange={onSortChange}
        canWrite
        onToggleStar={onToggleStar}
        {...overrides}
      />
    </div>,
  );
  return { onSelect, onToggleStar, onSortChange };
}

describe("CallsTable", () => {
  it("renders the mock calls as a list", () => {
    renderTable();
    const list = screen.getByRole("list", { name: "Calls" });
    // The virtualizer measures nothing in jsdom, so it renders its overscan window.
    expect(within(list).getAllByRole("listitem").length).toBeGreaterThan(0);
  });

  it("shows the talkgroup alias, radio and length of a call", () => {
    const first = rows[0] as Call;
    renderTable();
    const list = screen.getByRole("list", { name: "Calls" });
    expect(
      within(list).getAllByText(first.patch_label ?? (first.talkgroup_alias as string))[0],
    ).toBeInTheDocument();
    expect(within(list).getAllByText(String(first.tgid))[0]).toBeInTheDocument();
  });

  it("marks each row with what kind of channel the call was on", () => {
    const fire = { ...(rows[0] as Call), id: 9_999_010, category: "fire", service_tag: "Fire-Tac" };
    renderWithProviders(
      <CallsTable
        calls={[fire]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getByText("Category: Fire — Fire-Tac")).toBeInTheDocument();
  });

  it("leaves an unclassified call with the neutral mark rather than none", () => {
    const plain = { ...(rows[0] as Call), id: 9_999_011, category: null, service_tag: null };
    renderWithProviders(
      <CallsTable
        calls={[plain]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getByText("Category: Uncategorized")).toBeInTheDocument();
  });

  it("shows a unit-to-unit call as the pair of radios, marked as one", () => {
    // A private call is on no talkgroup, so the cell that names the channel would
    // otherwise be blank — which reads as missing data rather than as different traffic.
    const unit: Call = {
      ...(rows[0] as Call),
      id: 9_999_020,
      kind: "unit",
      talkgroup_id: null,
      talkgroup_alias: null,
      tgid: null,
      talkgroup_ids: [],
      patch_label: null,
      radio_label: "5238001",
      target_radio_id: 4242,
      target_radio_label: "5238109",
    };
    renderWithProviders(
      <CallsTable
        calls={[unit]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getByText("Unit call")).toBeInTheDocument();
    expect(screen.getByText("5238001 → 5238109")).toBeInTheDocument();
  });

  it("leaves a group call showing its talkgroup", () => {
    const group = { ...(rows[0] as Call), id: 9_999_021, kind: "group" as const };
    renderWithProviders(
      <CallsTable
        calls={[group]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.queryByText("Unit call")).not.toBeInTheDocument();
  });

  it("marks a call an archive migration pushed long after it happened", () => {
    const late = { ...(rows[0] as Call), id: 9_999_012, ingest_priority: "backlog" };
    renderWithProviders(
      <CallsTable
        calls={[late]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getByText("backfilled")).toBeInTheDocument();
  });

  it("leaves live traffic and unclassified rows unmarked", () => {
    const calls = [
      { ...(rows[0] as Call), id: 9_999_013, ingest_priority: "live" },
      // No class at all: the file watcher found it on disk, which says nothing either way.
      { ...(rows[1] as Call), id: 9_999_014, ingest_priority: null },
    ];
    renderWithProviders(
      <CallsTable
        calls={calls}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.queryByText("backfilled")).not.toBeInTheDocument();
  });

  it("names each row for a screen reader with its whole content", () => {
    const first = rows[0] as Call;
    renderTable();
    const row = screen.getAllByRole("button", {
      name: new RegExp(first.patch_label ?? (first.talkgroup_alias as string), "i"),
    })[0];
    expect(row).toBeDefined();
  });

  it("selects a call when its row is activated", async () => {
    const user = userEvent.setup();
    const { onSelect } = renderTable();
    const list = screen.getByRole("list", { name: "Calls" });
    const [firstRow] = within(list).getAllByRole("button");
    await user.click(firstRow as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith(rows[0]);
  });

  it("marks the selected row for assistive tech, not only with color", () => {
    const target = rows[2] as Call;
    renderTable({ selectedId: target.id });
    const current = screen
      .getAllByRole("button")
      .filter((button) => button.getAttribute("aria-current") === "true");
    expect(current).toHaveLength(1);
  });

  it("sorts on a column header, and reverses on a second click", async () => {
    const user = userEvent.setup();
    const { onSortChange } = renderTable();
    await user.click(screen.getByRole("button", { name: "Sort by Length" }));
    expect(onSortChange).toHaveBeenCalledWith("-duration_s");
  });

  it("reverses the column that is already sorted", async () => {
    const user = userEvent.setup();
    const onSortChange = vi.fn();
    renderWithProviders(
      <CallsTable
        calls={rows}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-duration_s"
        onSortChange={onSortChange}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Sort by Length" }));
    expect(onSortChange).toHaveBeenCalledWith("duration_s");
  });

  it("offers no star at all to a reader who cannot write", () => {
    renderTable({ canWrite: false });
    // The star's own names, not /star/: a row button is named by its transcript, and the
    // mock traffic (generated relative to now) sometimes says "start me another unit".
    expect(screen.queryAllByRole("button", { name: /^(Star this call|Remove the star)$/ })).toHaveLength(0);
    // The rest of the row's actions are still there.
    expect(screen.getAllByRole("button", { name: /^Play the call on/ }).length).toBeGreaterThan(0);
  });

  it("offers the star to a reader who can", () => {
    renderTable({ canWrite: true });
    expect(screen.getAllByRole("button", { name: /Star this call|Remove the star/ })[0]).toBeEnabled();
  });

  it("flags an emergency call in the row", () => {
    const emergency = { ...(rows[0] as Call), id: 9_999_001, emergency: true };
    renderWithProviders(
      <CallsTable
        calls={[emergency]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getByText("Emergency call")).toBeInTheDocument();
  });

  it("leaves an ordinary call unflagged", () => {
    const plain = { ...(rows[0] as Call), id: 9_999_002, emergency: false };
    renderWithProviders(
      <CallsTable
        calls={[plain]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.queryByText("Emergency call")).not.toBeInTheDocument();
  });

  it("highlights the query inside the transcript snippet", () => {
    const withText = rows.find((call) => (call.transcript_text ?? "").length > 12) as Call;
    const word = (withText.transcript_text as string).split(" ")[0] as string;
    renderWithProviders(
      <CallsTable
        calls={[withText]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
        query={word}
      />,
    );
    const marks = document.querySelectorAll("mark");
    expect(marks.length).toBeGreaterThan(0);
  });

  it("says why a call has no transcript, in words rather than the raw status", () => {
    const without = rows.find((call) => call.transcript_text === null);
    if (!without) return;
    renderWithProviders(
      <CallsTable
        calls={[without]}
        selectedId={null}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
      />,
    );
    expect(screen.getAllByText(noTranscriptReason(without))[0]).toHaveClass("italic");
  });
});

describe("the export selection", () => {
  it("has no tick column at all until the page offers one", () => {
    renderTable();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("reports a row being ticked", async () => {
    const user = userEvent.setup();
    const onCheck = vi.fn();
    renderTable({ checked: new Set<number>(), onCheck });

    await user.click(screen.getAllByRole("checkbox", { name: /Select the call on/ })[0] as HTMLElement);
    expect(onCheck).toHaveBeenCalledWith(rows[0] as Call, true);
  });

  it("reports a ticked row being cleared", async () => {
    const user = userEvent.setup();
    const first = rows[0] as Call;
    const onCheck = vi.fn();
    renderTable({ checked: new Set([first.id]), onCheck });

    await user.click(screen.getAllByRole("checkbox", { name: /Select the call on/ })[0] as HTMLElement);
    expect(onCheck).toHaveBeenCalledWith(first, false);
  });

  it("shows a ticked row as ticked", () => {
    const first = rows[0] as Call;
    renderTable({ checked: new Set([first.id]), onCheck: vi.fn() });
    const boxes = screen.getAllByRole("checkbox", { name: /Select the call on/ });
    expect(boxes[0]).toBeChecked();
    expect(boxes[1]).not.toBeChecked();
  });

  it("offers select-all over the loaded page", async () => {
    const user = userEvent.setup();
    const onCheckAll = vi.fn();
    renderTable({ checked: new Set<number>(), onCheck: vi.fn(), onCheckAll });
    await user.click(screen.getByRole("checkbox", { name: "Select every loaded call" }));
    expect(onCheckAll).toHaveBeenCalledWith(true);
  });

  it("turns select-all into clear once everything on the page is ticked", async () => {
    const user = userEvent.setup();
    const onCheckAll = vi.fn();
    renderTable({ checked: new Set(rows.map((call) => call.id)), onCheck: vi.fn(), onCheckAll });
    await user.click(screen.getByRole("checkbox", { name: "Clear the selection" }));
    expect(onCheckAll).toHaveBeenCalledWith(false);
  });

  it("marks a partial selection as indeterminate rather than as none", () => {
    renderTable({ checked: new Set([(rows[0] as Call).id]), onCheck: vi.fn(), onCheckAll: vi.fn() });
    const all = screen.getByRole("checkbox", { name: "Select every loaded call" }) as HTMLInputElement;
    expect(all.checked).toBe(false);
    expect(all.indeterminate).toBe(true);
  });
});

describe("the row copy action", () => {
  it("copies a plain-text summary of the call", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn((_text: string) => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

    const first = rows[0] as Call;
    renderTable();
    await user.click(
      screen.getAllByRole("button", { name: /Copy a summary of the call on/ })[0] as HTMLElement,
    );

    expect(writeText).toHaveBeenCalledTimes(1);
    const text = writeText.mock.calls[0]?.[0] ?? "";
    expect(text).toContain(`Call ${first.id}`);
    expect(text).toContain(String(first.tgid));
  });
});

describe("CallCards", () => {
  it("renders one card per call with a link to its detail page", () => {
    renderWithProviders(<CallCards calls={rows.slice(0, 5)} selectedId={null} onSelect={vi.fn()} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getAllByRole("link")[0]).toHaveAttribute("href", `/calls/${rows[0]?.id}`);
  });

  it("selects a call when its card is activated", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    renderWithProviders(<CallCards calls={rows.slice(0, 3)} selectedId={null} onSelect={onSelect} />);
    await user.click(screen.getAllByRole("button")[0] as HTMLElement);
    expect(onSelect).toHaveBeenCalledWith(rows[0]);
  });
});
