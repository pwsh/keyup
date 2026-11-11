import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { INGEST_SOURCES } from "../../api/mock/dataset";
import type { IngestSourceRow } from "../../api/types";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { IngestSourcesCard, sourceState } from "./IngestSourcesCard";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const item = async (label: string): Promise<HTMLElement> => {
  const cell = await screen.findByText(label);
  const listItem = cell.closest("li");
  if (!listItem) throw new Error(`no row for ${label}`);
  return listItem;
};

const source = (overrides: Partial<IngestSourceRow>): IngestSourceRow => ({
  ...(INGEST_SOURCES[0] as IngestSourceRow),
  ...overrides,
});

/**
 * Where recordings come from, as a list an operator can change.
 *
 * The state a directory is in is the point of the card: a spool whose mount went away is
 * otherwise completely silent, and used to require reading the watcher's log to find.
 */
describe("IngestSourcesCard", () => {
  it("lists every source with what it actually is", async () => {
    renderWithProviders(<IngestSourcesCard canWrite={false} />, "/admin?tab=ingest");
    expect(within(await item("AUDIO_ROOT")).getByText("/srv/keyup/audio")).toBeInTheDocument();
    expect(within(await item("rec-02 (garage)")).getByText(/token: recorder-02/)).toBeInTheDocument();
    expect(within(await item("2019 archive")).getByText("30114 / 41208 files")).toBeInTheDocument();
  });

  it("shows a directory whose mount has gone as broken, with the reason", async () => {
    renderWithProviders(<IngestSourcesCard canWrite={false} />, "/admin?tab=ingest");
    const attic = await item("attic recorder spool");
    expect(within(attic).getByText("down")).toBeInTheDocument();
    expect(within(attic).getByText(/does not exist/)).toBeInTheDocument();
  });

  it("calls a switched-off source off, not broken", () => {
    // A red dot nobody can act on is noise; "disabled" is a thing the operator did.
    expect(sourceState(source({ enabled: false, path_state: "ok" }))).toBe("disabled");
    expect(sourceState(source({ enabled: true, path_state: "ok" }))).toBe("healthy");
    expect(sourceState(source({ enabled: true, path_state: "missing" }))).toBe("down");
    expect(sourceState(source({ kind: "push_token", path_state: "n/a", token_active: false }))).toBe("down");
  });

  it("leaves every control alone for a reader", async () => {
    renderWithProviders(<IngestSourcesCard canWrite={false} />, "/admin?tab=ingest");
    expect(await screen.findByRole("button", { name: /new source/i })).toBeDisabled();
    expect(within(await item("AUDIO_ROOT")).getByRole("button", { name: /edit AUDIO_ROOT/i })).toBeDisabled();
  });

  it("asks for a directory for a watched source and a token for a push one", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<IngestSourcesCard canWrite />, "/admin?tab=ingest");
    await user.click(await screen.findByRole("button", { name: /new source/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^name$/i), "second spool");
    const save = within(dialog).getByRole("button", { name: /add source/i });
    expect(save).toBeDisabled();
    expect(within(dialog).getByText(/a directory is required/i)).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText(/^directory$/i), "/srv/recorder-2/audio");
    expect(save).toBeEnabled();

    // A push source has no directory at all; it wants a token instead.
    await user.click(within(dialog).getByRole("combobox", { name: /source kind/i }));
    await user.click(await screen.findByRole("option", { name: /recorder push/i }));
    expect(within(dialog).queryByLabelText(/^directory$/i)).not.toBeInTheDocument();
    expect(within(dialog).getByText(/pick the upload token/i)).toBeInTheDocument();
  });

  it("adds a watched directory and shows it in the list", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<IngestSourcesCard canWrite />, "/admin?tab=ingest");
    await user.click(await screen.findByRole("button", { name: /new source/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^name$/i), "second spool");
    await user.type(within(dialog).getByLabelText(/^directory$/i), "/srv/recorder-2/audio");
    await user.click(within(dialog).getByRole("button", { name: /add source/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(within(await item("second spool")).getByText("/srv/recorder-2/audio")).toBeInTheDocument();
    expect(useToastStore.getState().toasts.at(-1)?.title).toBe("second spool added");
  });

  it("saves a directory that is not there yet, and shows it red", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<IngestSourcesCard canWrite />, "/admin?tab=ingest");
    await user.click(await screen.findByRole("button", { name: /new source/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^name$/i), "not yet mounted");
    await user.type(within(dialog).getByLabelText(/^directory$/i), "/mnt/later/audio");
    await user.click(within(dialog).getByRole("button", { name: /add source/i }));

    // Configuring a spool before its mount comes up is normal — it is saved, and shown as
    // broken, rather than refused.
    const row = await item("not yet mounted");
    expect(within(row).getByText("down")).toBeInTheDocument();
  });

  it("cannot turn one kind of source into another", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<IngestSourcesCard canWrite />, "/admin?tab=ingest");
    await user.click(within(await item("AUDIO_ROOT")).getByRole("button", { name: /edit AUDIO_ROOT/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: /source kind/i })).toBeDisabled();
  });

  it("removes a source after saying what a removal does not touch", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<IngestSourcesCard canWrite />, "/admin?tab=ingest");
    await user.click(
      within(await item("attic recorder spool")).getByRole("button", {
        name: /remove attic recorder spool/i,
      }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/nothing already ingested is affected/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /^remove$/i }));

    await waitFor(() => expect(screen.queryByText("attic recorder spool")).not.toBeInTheDocument());
  });
});
