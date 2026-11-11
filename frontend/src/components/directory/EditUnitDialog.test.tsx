import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiPost } from "../../api/client";
import { AGENCIES, UNITS } from "../../api/mock/dataset";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { EditUnitDialog } from "./EditUnitDialog";

describe("EditUnitDialog", () => {
  it("shows the API's own reason a save was refused, not a fixed string", async () => {
    // The callsign is the unique key, so this dialog is where a 409 lands. "Could
    // not save the unit" is true and useless; "unit E1 already exists" is the
    // whole answer, and it was being thrown away.
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const user = userEvent.setup();
    // A callsign is unique *per agency*, so the clash needs both halves.
    const taken = UNITS.find((unit) => unit.agency_code !== null);
    const agency = AGENCIES.find((row) => row.code === taken?.agency_code);
    renderWithProviders(<EditUnitDialog unit={null} open onOpenChange={vi.fn()} />);

    await user.type(screen.getByRole("textbox", { name: "Callsign" }), taken?.callsign as string);
    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    await user.click(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: `${agency?.name} (${agency?.code})`,
      }),
    );
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const toasts = useToastStore.getState().toasts;
      expect(toasts.at(-1)?.title).toBe("Could not save the unit");
      expect(toasts.at(-1)?.body).toBe(`unit ${taken?.callsign} already exists`);
    });
  });

  it("picks the agency from the ones that exist rather than taking free text", async () => {
    renderWithProviders(<EditUnitDialog unit={null} open onOpenChange={vi.fn()} />);
    expect(await screen.findByRole("combobox", { name: "Agency" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Agency/ })).not.toBeInTheDocument();
  });
});
