import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { apiPost } from "../../api/client";
import { ADMIN_AGENCIES } from "../../api/mock/dataset";
import { renderWithProviders } from "../../test/render";
import { AgenciesTab, deleteBlocker, mappedCount } from "./AgenciesTab";

async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

const local = (mapped: number) => ({
  ...(ADMIN_AGENCIES[0] as (typeof ADMIN_AGENCIES)[number]),
  code: "EXAMPLE_RR",
  source: "local",
  talkgroup_count: mapped,
  radio_count: 0,
  unit_count: 0,
  person_count: 0,
});

/**
 * The delete guard is the part worth pinning: the API refuses both cases with a
 * 409, and a page that offered the button anyway would be teaching the operator
 * that Delete sometimes just fails.
 */
describe("deleteBlocker", () => {
  it("refuses a seeded row and points at deactivating instead", () => {
    const seeded = { ...local(0), source: "seed" };
    expect(deleteBlocker(seeded)).toMatch(/seeded agency list/);
    expect(deleteBlocker(seeded)).toMatch(/inactive/);
  });

  it("refuses a local row something still points at", () => {
    expect(deleteBlocker(local(3))).toMatch(/3 talkgroups, radios, units or people/);
  });

  it("allows a local row nothing points at", () => {
    expect(deleteBlocker(local(0))).toBeNull();
  });

  it("counts every table that can point at an agency", () => {
    expect(mappedCount({ ...local(1), radio_count: 2, unit_count: 3, person_count: 4 })).toBe(10);
  });
});

describe("the Agencies tab", () => {
  it("lists every agency with its source and counts", async () => {
    await signInAsAdmin();
    renderWithProviders(<AgenciesTab canWrite />);

    const table = await screen.findByRole("table");
    for (const agency of ADMIN_AGENCIES) {
      expect(within(table).getByRole("rowheader", { name: agency.code })).toBeInTheDocument();
    }
    // Both provenances are drawn, because the badge is how the two are told apart.
    expect(within(table).getAllByText("seeded").length).toBeGreaterThan(0);
    expect(within(table).getAllByText("local").length).toBeGreaterThan(0);
  });

  it("turns the delete button off for the rows the API would refuse", async () => {
    await signInAsAdmin();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    // A seeded row: never deletable, however little points at it.
    expect(screen.getByRole("button", { name: "Delete XX0010000" })).toBeDisabled();
    // A local row with nothing mapped: the one case that deletes.
    expect(screen.getByRole("button", { name: "Delete EXAMPLE_RR" })).toBeEnabled();
  });

  it("adds an agency and shows it in the table", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: /New agency/ }));
    await user.type(screen.getByLabelText("Code"), "NORTH_SO");
    await user.type(screen.getByLabelText("Name"), "North County Sheriff");
    await user.click(screen.getByRole("button", { name: "Add agency" }));

    await waitFor(() => expect(screen.getByRole("rowheader", { name: "NORTH_SO" })).toBeInTheDocument());
  });

  it("deletes a local agency nothing points at", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Delete EXAMPLE_RR" }));
    await user.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.queryByRole("rowheader", { name: "EXAMPLE_RR" })).toBeNull());
  });

  it("offers nothing to change when the reader cannot write", async () => {
    renderWithProviders(<AgenciesTab canWrite={false} />);
    await screen.findByRole("table");

    expect(screen.queryByRole("button", { name: /New agency/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Delete / })).toBeNull();
  });
});

/**
 * The row is the edit.
 *
 * The three cells that save themselves are the point of the tab, so what is worth
 * pinning is that each one sends exactly its own field — `exclude_unset` on the
 * server means a one-key body writes one column — and that a seeded name is *not*
 * one of them, because `keyup seed-agencies` owns that spelling until the pencil
 * takes it over.
 */
describe("editing an agency in the row", () => {
  const LOCAL = ADMIN_AGENCIES.find((agency) => agency.code === "EXAMPLE_RR");
  const SEEDED = ADMIN_AGENCIES.find((agency) => agency.source === "seed");

  it("renames a local agency and sends only the name", async () => {
    await signInAsAdmin();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    await user.click(
      screen.getByRole("button", { name: `Name for EXAMPLE_RR: ${LOCAL?.name}. Press Enter to edit` }),
    );
    const input = screen.getByRole("textbox", { name: "Name for EXAMPLE_RR" });
    await user.clear(input);
    await user.type(input, "Example Railway Co{Enter}");

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls.at(-1)?.[0]).toBe("/admin/agencies/EXAMPLE_RR");
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ name: "Example Railway Co" });
  });

  it("changes the kind from the row", async () => {
    await signInAsAdmin();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("combobox", { name: "Kind for EXAMPLE_RR" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "sheriff" }));

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ kind: "sheriff" });
  });

  it("retires an agency from the row without touching anything else", async () => {
    await signInAsAdmin();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("switch", { name: /^Active for EXAMPLE_RR\./ }));

    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ active: false });
  });

  it("leaves a seeded name as text while still editing its kind and active", async () => {
    // The seeded list spells it; overriding that stamps `edited_at` and stops
    // the next re-seed, which is a consequence the dialog states and a cell cannot.
    await signInAsAdmin();
    renderWithProviders(<AgenciesTab canWrite />);
    const table = await screen.findByRole("table");

    expect(
      within(table).queryByRole("button", { name: new RegExp(`^Name for ${SEEDED?.code}:`) }),
    ).toBeNull();
    expect(within(table).getByText(SEEDED?.name as string)).toBeInTheDocument();
    expect(within(table).getByRole("combobox", { name: `Kind for ${SEEDED?.code}` })).toBeEnabled();
    expect(
      within(table).getByRole("switch", { name: new RegExp(`^Active for ${SEEDED?.code}\\.`) }),
    ).toBeEnabled();
    // The pencil is still the way to override the name.
    expect(within(table).getByRole("button", { name: `Edit ${SEEDED?.code}` })).toBeEnabled();
  });

  it("says why a delete is off rather than offering one that fails", async () => {
    await signInAsAdmin();
    renderWithProviders(<AgenciesTab canWrite />);
    await screen.findByRole("table");

    const seeded = screen.getByRole("button", { name: `Delete ${SEEDED?.code}` });
    expect(seeded).toBeDisabled();
    expect(deleteBlocker(SEEDED as (typeof ADMIN_AGENCIES)[number])).toMatch(/seeded agency list/);

    // The other refusal — a local row something still points at — has no fixture
    // row of its own (the mock's two local agencies are exactly the deletable and
    // the seeded-source cases), so it is pinned on the guard the button asks.
    expect(deleteBlocker({ ...(LOCAL as (typeof ADMIN_AGENCIES)[number]), radio_count: 3 })).toMatch(
      /3 talkgroups, radios, units or people/,
    );
    expect(mappedCount({ ...(LOCAL as (typeof ADMIN_AGENCIES)[number]), radio_count: 3 })).toBeGreaterThan(0);
  });

  it("offers no inline control to a reader who cannot write", async () => {
    renderWithProviders(<AgenciesTab canWrite={false} />);
    const table = await screen.findByRole("table");

    expect(within(table).queryAllByRole("combobox")).toHaveLength(0);
    expect(within(table).queryAllByRole("switch")).toHaveLength(0);
    expect(within(table).queryByRole("button", { name: /^Name for / })).toBeNull();
  });
});
