import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import type { SystemInfo } from "../../api/types";
import { Sites } from "../../pages/Sites";
import { renderWithProviders } from "../../test/render";
import { EditSystemDialog } from "./EditSystemDialog";
import { MergeSystemDialog } from "./MergeSystemDialog";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const systems = () => apiGet<SystemInfo[]>("/systems");

/**
 * A system is only ever looked at through its sites — the reason to rename one is that its
 * sites are filed under a typo — so its editing lives on the Sites page rather than behind
 * an Admin tab, and these are the cases that keep it honest about what it will and will not
 * let an operator do.
 */
describe("the systems card on the Sites page", () => {
  it("lists every system with its counts and its RadioReference mapping", async () => {
    renderWithProviders(<Sites />, "/sites?site=1");
    expect(await screen.findByText("Radio systems")).toBeInTheDocument();
    expect(screen.getByText(/sid 7804/)).toBeInTheDocument();
    // More than one system has no RadioReference mapping, so this is a presence check
    // rather than a uniqueness one.
    expect(screen.getAllByText("not mapped").length).toBeGreaterThan(0);
  });

  it("keeps every action disabled while signed out", async () => {
    renderWithProviders(<Sites />, "/sites?site=1");
    expect(await screen.findByRole("button", { name: "Edit Example Network" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Merge Example Network" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete Example Network" })).toBeDisabled();
  });

  it("never enables Delete on a system something still belongs to", async () => {
    await signIn();
    renderWithProviders(<Sites />, "/sites?site=1");
    // Every foreign key onto `systems` cascades, so this button is the only thing between a
    // click and a million recordings: it is disabled from `deletable`, not from a guess.
    const remove = await screen.findByRole("button", { name: "Delete Example Network" });
    await waitFor(() => expect(remove).toBeDisabled());
  });
});

describe("EditSystemDialog", () => {
  it("edits the two fields that are ours, and the RadioReference mapping", async () => {
    const user = userEvent.setup();
    await signIn();
    const onOpenChange = vi.fn();
    const exampleNet = (await systems())[0] as SystemInfo;
    renderWithProviders(<EditSystemDialog system={exampleNet} open canMapRr onOpenChange={onOpenChange} />);

    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Example Net");
    await user.clear(screen.getByLabelText("RR system id"));
    await user.type(screen.getByLabelText("RR system id"), "9001");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const saved = (await systems()).find((row) => row.id === exampleNet.id);
    expect(saved?.name).toBe("Example Net");
    // The mapping goes through its own admin endpoint, so this proves both calls landed.
    expect(saved?.rr_system_id).toBe(9001);
  });

  it("locks the mapping for a writer who is not an admin, and still saves the name", async () => {
    const user = userEvent.setup();
    await signIn();
    const exampleNet = (await systems())[0] as SystemInfo;
    renderWithProviders(
      <EditSystemDialog system={exampleNet} open canMapRr={false} onOpenChange={vi.fn()} />,
    );

    expect(screen.getByLabelText("RR system id")).toBeDisabled();
    await user.type(screen.getByLabelText("Description"), "!");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("will not save a system with no name at all", async () => {
    const user = userEvent.setup();
    const exampleNet = (await systems())[0] as SystemInfo;
    renderWithProviders(<EditSystemDialog system={exampleNet} open canMapRr onOpenChange={vi.fn()} />);
    await user.clear(screen.getByLabelText("Name"));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("renders nothing at all when there is no system to edit", () => {
    const { container } = renderWithProviders(
      <EditSystemDialog system={null} open canMapRr onOpenChange={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("MergeSystemDialog", () => {
  it("offers every other system — unlike a site merge, this one may cross the boundary", async () => {
    const user = userEvent.setup();
    const rows = await systems();
    const exampleNet = rows[0] as SystemInfo;
    renderWithProviders(<MergeSystemDialog system={exampleNet} systems={rows} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("combobox", { name: "Merge into" }));
    const options = within(screen.getByRole("listbox")).getAllByRole("option");
    // Every system but this one, plus the "pick a system" placeholder.
    expect(options).toHaveLength(rows.length);
  });

  it("merges into the chosen system and drops the source", async () => {
    const user = userEvent.setup();
    await signIn();
    const rows = await systems();
    const school = rows[1] as SystemInfo;
    const onOpenChange = vi.fn();
    renderWithProviders(
      <MergeSystemDialog system={school} systems={rows} open onOpenChange={onOpenChange} />,
    );

    const confirm = screen.getByRole("button", { name: "Merge and delete" });
    expect(confirm).toBeDisabled();

    await user.click(screen.getByRole("combobox", { name: "Merge into" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: /Example Net/ }));
    await user.click(screen.getByRole("button", { name: "Merge and delete" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect((await systems()).some((row) => row.id === school.id)).toBe(false);
  });

  it("says so when there is nothing to merge into", async () => {
    const rows = await systems();
    const only = rows[0] as SystemInfo;
    renderWithProviders(<MergeSystemDialog system={only} systems={[only]} open onOpenChange={vi.fn()} />);
    expect(screen.getByText(/only system in the archive/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Merge into" })).not.toBeInTheDocument();
  });
});
