import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiPost } from "../../api/client";
import { AGENCIES } from "../../api/mock/dataset";
import { renderWithProviders } from "../../test/render";
import { AgencySelect } from "./AgencySelect";

/**
 * The agency picker exists because the API refuses a code it does not know, and a
 * free-text box only found that out on Save. So the two things worth proving are
 * that the real codes are offered, and that a code the table has since dropped is
 * not quietly turned into "none" behind the reader's back.
 */
describe("AgencySelect", () => {
  it("offers the agencies the API knows, and 'none' as a real choice", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AgencySelect value={null} onValueChange={vi.fn()} tip="Which agency" />);

    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    const options = screen.getByRole("listbox");
    expect(within(options).getByRole("option", { name: "— none —" })).toBeInTheDocument();
    const first = AGENCIES[0];
    expect(
      within(options).getByRole("option", { name: `${first?.name} (${first?.code})` }),
    ).toBeInTheDocument();
  });

  it("reports null, not the sentinel, when the reader picks 'none'", async () => {
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    renderWithProviders(
      <AgencySelect value={AGENCIES[0]?.code ?? null} onValueChange={onValueChange} tip="Which agency" />,
    );

    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "— none —" }));

    expect(onValueChange).toHaveBeenCalledWith(null);
  });

  it("does not offer a deactivated agency, but still shows one already chosen", async () => {
    // Hiding it is what deactivating was for; hiding the *current* value would
    // silently move the channel somewhere else on the next save.
    const retired = AGENCIES.find((agency) => !agency.active);
    expect(retired).toBeDefined();
    const user = userEvent.setup();

    const plain = renderWithProviders(<AgencySelect value={null} onValueChange={vi.fn()} tip="Which" />);
    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    expect(
      within(screen.getByRole("listbox")).queryByRole("option", { name: new RegExp(retired?.code ?? "") }),
    ).toBeNull();
    plain.unmount();

    renderWithProviders(<AgencySelect value={retired?.code ?? null} onValueChange={vi.fn()} tip="Which" />);
    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    expect(
      within(screen.getByRole("listbox")).getByRole("option", { name: /— inactive$/ }),
    ).toBeInTheDocument();
  });

  it("offers 'New agency…' to an administrator and nobody else", async () => {
    const user = userEvent.setup();
    const signedOut = renderWithProviders(<AgencySelect value={null} onValueChange={vi.fn()} tip="Which" />);
    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    expect(within(screen.getByRole("listbox")).queryByRole("option", { name: "New agency…" })).toBeNull();
    await user.keyboard("{Escape}");
    signedOut.unmount();

    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    renderWithProviders(<AgencySelect value={null} onValueChange={vi.fn()} tip="Which" />);
    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "New agency…" }));

    // Picking it opens the add dialog rather than selecting a value called that.
    expect(await screen.findByRole("dialog", { name: "New agency" })).toBeInTheDocument();
  });

  it("selects the agency it just created, without leaving the row", async () => {
    // The seam between the picker and the dialog: an admin who finds a channel
    // belonging to an agency Keyup has no code for adds it here, and the point of
    // adding it *here* is that the channel is then mapped to it in the same gesture.
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const user = userEvent.setup();
    const onValueChange = vi.fn();
    renderWithProviders(<AgencySelect value={null} onValueChange={onValueChange} tip="Which agency" />);

    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "New agency…" }));
    await user.type(await screen.findByLabelText("Code"), "NORTH_SO");
    await user.type(screen.getByLabelText("Name"), "North County Sheriff");
    await user.click(screen.getByRole("button", { name: "Add agency" }));

    await waitFor(() => expect(onValueChange).toHaveBeenCalledWith("NORTH_SO"));
  });

  it("keeps a code the agency table no longer has, and says so", async () => {
    // Showing "— none —" for it would erase a real mapping on the next save.
    const user = userEvent.setup();
    renderWithProviders(<AgencySelect value="ZZ9999999" onValueChange={vi.fn()} tip="Which agency" />);

    await user.click(await screen.findByRole("combobox", { name: "Agency" }));
    expect(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: "ZZ9999999 (not in the agency table)",
      }),
    ).toBeInTheDocument();
  });
});
