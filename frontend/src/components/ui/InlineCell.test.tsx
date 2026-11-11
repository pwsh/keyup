import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { renderWithProviders } from "../../test/render";
import { InlineColor, InlineNumber, InlineSelect, InlineSwitch, InlineText } from "./InlineCell";

/**
 * The contract of an inline cell, exercised through the keyboard and the mouse.
 *
 * Every case here is about a promise the family makes and a table full of live
 * inputs would break: it saves once and on purpose, it does not send an edit that
 * changed nothing, Escape costs nothing, and a refusal is reported *in the cell*
 * with the server's own words rather than as a toast that has already scrolled by.
 */
describe("InlineText", () => {
  it("commits on blur, and only what changed", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineText value="E1" onSave={onSave} label="Callsign" tip="The unit's callsign" />);

    await user.click(screen.getByRole("button", { name: /Callsign: E1/ }));
    const input = screen.getByRole("textbox", { name: "Callsign" });
    await user.clear(input);
    await user.type(input, "E2");
    await user.tab();

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("E2"));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("commits on Enter without waiting for focus to move", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineText value="E1" onSave={onSave} label="Callsign" tip="The callsign" />);

    await user.click(screen.getByRole("button", { name: /Callsign: E1/ }));
    await user.clear(screen.getByRole("textbox", { name: "Callsign" }));
    await user.type(screen.getByRole("textbox", { name: "Callsign" }), "E7{Enter}");

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("E7"));
  });

  it("sends nothing when the text is left as it was", async () => {
    // Tabbing across a row must be free: a blur that saves an unchanged value
    // would write every cell the reader merely walked past.
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineText value="E1" onSave={onSave} label="Callsign" tip="The callsign" />);

    await user.click(screen.getByRole("button", { name: /Callsign: E1/ }));
    await user.tab();

    expect(onSave).not.toHaveBeenCalled();
  });

  it("puts the old value back on Escape and saves nothing", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineText value="E1" onSave={onSave} label="Callsign" tip="The callsign" />);

    await user.click(screen.getByRole("button", { name: /Callsign: E1/ }));
    await user.clear(screen.getByRole("textbox", { name: "Callsign" }));
    await user.type(screen.getByRole("textbox", { name: "Callsign" }), "wrong{Escape}");

    expect(onSave).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: /Callsign: E1/ })).toBeInTheDocument();
  });

  it("sends null rather than an empty string when the column means 'not set'", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineText value="XX0010100" onSave={onSave} label="Agency" tip="The agency" emptyAsNull />,
    );

    await user.click(screen.getByRole("button", { name: /Agency: XX0010100/ }));
    await user.clear(screen.getByRole("textbox", { name: "Agency" }));
    await user.tab();

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(null));
  });

  it("shows the API's own reason in the cell and reverts", async () => {
    const user = userEvent.setup();
    const onSave = vi
      .fn()
      .mockRejectedValue(new ApiError("409 Conflict", 409, "/units/1", "callsign 'E2' already exists"));
    renderWithProviders(<InlineText value="E1" onSave={onSave} label="Callsign" tip="The callsign" />);

    await user.click(screen.getByRole("button", { name: /Callsign: E1/ }));
    await user.clear(screen.getByRole("textbox", { name: "Callsign" }));
    await user.type(screen.getByRole("textbox", { name: "Callsign" }), "E2{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("callsign 'E2' already exists");
    // The value the server refused is not left sitting on screen as if it took.
    expect(screen.getByRole("button", { name: /Callsign: E1/ })).toBeInTheDocument();
  });

  it("shows an empty cell as an em dash rather than as the example", async () => {
    // The bug: `placeholder="E1"` was rendered in the resting cell, so every radio
    // without a unit number read as the unit "E1" — in a column where some of the
    // neighbours genuinely are E1. The example belongs in the open input and nowhere
    // else; the invitation is a `title`.
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineText
        value={null}
        onSave={onSave}
        label="Unit"
        tip="The unit this radio answers to"
        placeholder="E1"
        hint="Assign a unit"
        emptyAsNull
      />,
    );

    const cell = screen.getByRole("button", { name: /Unit: not set/ });
    expect(cell).toHaveTextContent("—");
    expect(cell).not.toHaveTextContent("E1");
    expect(cell).toHaveAttribute("title", "Assign a unit");

    // …and the example is still there for the person actually typing into it.
    await user.click(cell);
    expect(screen.getByRole("textbox", { name: "Unit" })).toHaveAttribute("placeholder", "E1");
  });

  it("keeps no title on a cell that has a value", () => {
    // A tip that repeats the word under the pointer is noise on every filled row.
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineText value="E1" onSave={onSave} label="Unit" tip="The unit" hint="Assign a unit" />,
    );
    expect(screen.getByRole("button", { name: /Unit: E1/ })).not.toHaveAttribute("title");
  });

  it("is plain text, not a control, without write access", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineText value="E1" onSave={onSave} label="Callsign" tip="The callsign" disabled />,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByText("E1")).toBeInTheDocument();
  });
});

describe("InlineNumber", () => {
  it("commits a number in range", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineNumber value={100} onSave={onSave} label="Priority" tip="Queue order" min={1} max={499} />,
    );

    await user.click(screen.getByRole("button", { name: /Priority: 100/ }));
    await user.clear(screen.getByRole("textbox", { name: "Priority" }));
    await user.type(screen.getByRole("textbox", { name: "Priority" }), "20{Enter}");

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(20));
  });

  it("refuses an out-of-range number in the cell rather than sending it", async () => {
    // The API's 422 for this is a paragraph of Pydantic; the bound is knowable
    // here, so the round trip is not worth making.
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineNumber value={100} onSave={onSave} label="Priority" tip="Queue order" min={1} max={499} />,
    );

    await user.click(screen.getByRole("button", { name: /Priority: 100/ }));
    await user.clear(screen.getByRole("textbox", { name: "Priority" }));
    await user.type(screen.getByRole("textbox", { name: "Priority" }), "900{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("between 1 and 499");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Priority: 100/ })).toBeInTheDocument();
  });

  it("treats an emptied box as a change of mind, not as zero", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineNumber value={100} onSave={onSave} label="Priority" tip="Queue order" />);

    await user.click(screen.getByRole("button", { name: /Priority: 100/ }));
    await user.clear(screen.getByRole("textbox", { name: "Priority" }));
    await user.tab();

    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Priority: 100/ })).toBeInTheDocument();
  });
});

describe("InlineSelect", () => {
  it("saves the choice as soon as it is made", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineSelect
        value="fire"
        onSave={onSave}
        label="Category"
        tip="What kind of channel"
        options={[
          { value: "fire", label: "fire" },
          { value: "law", label: "law" },
        ]}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "Category" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "law" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("law"));
  });

  it("reports a refusal beside the control", async () => {
    const user = userEvent.setup();
    const onSave = vi
      .fn()
      .mockRejectedValue(new ApiError("422", 422, "/talkgroups/1", "unknown category 'law'"));
    renderWithProviders(
      <InlineSelect
        value="fire"
        onSave={onSave}
        label="Category"
        tip="What kind of channel"
        options={[
          { value: "fire", label: "fire" },
          { value: "law", label: "law" },
        ]}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "Category" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "law" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("unknown category 'law'");
  });
});

describe("InlineSwitch", () => {
  it("saves the flip", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(
      <InlineSwitch value onSave={onSave} label="Transcribe this channel" tip="Send it for transcription" />,
    );

    await user.click(screen.getByRole("switch"));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(false));
  });
});

describe("InlineColor", () => {
  it("sends the picked color once, when the picker is done", async () => {
    // A color input fires while the picker is being dragged; saving per event
    // would be one request per shade the reader passed through.
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderWithProviders(<InlineColor value="#2a78d6" onSave={onSave} label="Color" tip="Chart color" />);

    // `userEvent` has no gesture for a native color picker, so the two events a
    // picker produces — a stream of changes, then a blur when it closes — are
    // fired directly. Three changes and one blur must still be one save.
    const input = screen.getByLabelText("Color") as HTMLInputElement;
    await user.click(input);
    for (const shade of ["#aa0000", "#dd0000", "#ff0000"]) {
      fireEvent.change(input, { target: { value: shade } });
    }
    fireEvent.blur(input);

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("#ff0000"));
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});
