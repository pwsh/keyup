import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { Drawer } from "./Drawer";
import { Popover } from "./Popover";

/**
 * The Export menu's shape: a tooltip on the trigger *and* a tooltip on the first item,
 * which takes focus as the popover opens. The first Escape used to go to that item's
 * tooltip and leave the popover up with focus inside it.
 */
function ExportLike({
  controlled = false,
  onOpenChange,
}: {
  controlled?: boolean;
  onOpenChange?: (o: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = <Button tip="Export these calls">Export</Button>;
  const items = (
    <div className="flex flex-col">
      <Button tip="Metadata and transcripts as CSV">CSV</Button>
      <Button tip="Just the audio">Audio</Button>
    </div>
  );
  return controlled ? (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        onOpenChange?.(next);
      }}
      trigger={trigger}
    >
      {items}
    </Popover>
  ) : (
    <Popover trigger={trigger}>{items}</Popover>
  );
}

describe("Escape on a popover whose trigger and items have tooltips", () => {
  for (const controlled of [false, true]) {
    it(`closes on the first Escape and returns focus to the trigger (${controlled ? "controlled" : "uncontrolled"})`, async () => {
      const user = userEvent.setup();
      const onOpenChange = vi.fn();
      renderWithProviders(<ExportLike controlled={controlled} onOpenChange={onOpenChange} />);
      const trigger = screen.getByRole("button", { name: "Export" });
      trigger.focus();
      await user.keyboard("{Enter}");
      const first = await screen.findByRole("button", { name: "CSV" });
      await waitFor(() => expect(first).toHaveFocus());
      // The item's tooltip is up: it opened when the item took focus.
      expect(await screen.findByRole("tooltip")).toHaveTextContent("Metadata and transcripts as CSV");

      await user.keyboard("{Escape}");

      await waitFor(() => expect(screen.queryByRole("button", { name: "CSV" })).not.toBeInTheDocument());
      await waitFor(() => expect(trigger).toHaveFocus());
      if (controlled) {
        // Told once, not twice.
        expect(onOpenChange.mock.calls.filter(([next]) => next === false)).toHaveLength(1);
      }
    });
  }

  it("still closes on Escape, once, when no tooltip is in the way", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    renderWithProviders(
      <Popover
        trigger={
          <button type="button" aria-label="Columns">
            Columns
          </button>
        }
        onOpenChange={onOpenChange}
      >
        <input aria-label="Filter columns" />
      </Popover>,
    );
    const trigger = screen.getByRole("button", { name: "Columns" });
    trigger.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Filter columns" })).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(onOpenChange.mock.calls.filter(([next]) => next === false)).toHaveLength(1);
  });
});

function DialogFromState({ kind }: { kind: "dialog" | "drawer" }) {
  const [open, setOpen] = useState(false);
  const Overlay = kind === "dialog" ? Dialog : Drawer;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Filters
      </button>
      <button type="button">Something else</button>
      <Overlay open={open} onOpenChange={setOpen} title="Filters">
        <Button tip="Clear every filter">Reset</Button>
      </Overlay>
    </>
  );
}

describe.each(["dialog", "drawer"] as const)("a %s opened from state", (kind) => {
  it("returns focus to its opener when closed with Escape", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DialogFromState kind={kind} />);
    const opener = screen.getByRole("button", { name: "Filters" });
    await user.click(opener);
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("returns focus to its opener when closed with its close button", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DialogFromState kind={kind} />);
    const opener = screen.getByRole("button", { name: "Filters" });
    await user.click(opener);
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: /Close/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("closes on the first Escape even while a tooltip inside it is open", async () => {
    const user = userEvent.setup();
    renderWithProviders(<DialogFromState kind={kind} />);
    const opener = screen.getByRole("button", { name: "Filters" });
    opener.focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog");
    const reset = screen.getByRole("button", { name: "Reset" });
    reset.focus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Clear every filter");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
