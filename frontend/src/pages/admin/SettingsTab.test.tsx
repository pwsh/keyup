import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { SettingsTab } from "./SettingsTab";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const render = () => renderWithProviders(<SettingsTab canWrite />, "/admin?tab=settings");

/** The card for one group, found by the heading the index links to. */
const groupCard = async (name: string) =>
  (await screen.findByRole("heading", { name: new RegExp(`^${name}`) })).closest("section") as HTMLElement;

/**
 * Forty settings on one screen, and the whole point of the tab is being able to find one.
 *
 * The cases below are the three ways an operator does that — read the index, read a group,
 * or search — plus the two ways they undo a mistake, which is what makes the page safe to
 * poke at: reset one row to its default, or discard the lot.
 */
describe("SettingsTab", () => {
  it("indexes every group it renders, with a count", async () => {
    render();
    const index = await screen.findByRole("navigation", { name: /settings groups/i });
    // Twelve transcription settings in the mock table, counted out loud in the index.
    expect(within(index).getByRole("link", { name: /Transcription/ })).toHaveTextContent(
      /Transcription\s*12/,
    );
    // A group with a page of its own is not on this tab at all.
    expect(within(index).queryByRole("link", { name: /Broadcastify/ })).not.toBeInTheDocument();
    expect(within(index).queryByRole("link", { name: /^Audio/ })).not.toBeInTheDocument();
  });

  it("breaks a group into sub-sections rather than one run of fields", async () => {
    render();
    const card = await groupCard("Transcription");
    for (const title of ["Policy", "Prompting", "Audio conditioning", "Backends/queue"]) {
      expect(within(card).getByRole("heading", { name: title })).toBeInTheDocument();
    }
    // And the settings really are inside the card, under their own key.
    expect(within(card).getByText("stt_prompt_extra")).toBeInTheDocument();
  });

  it("still points at the tabs that own the groups it holds back", async () => {
    render();
    expect(await screen.findByRole("link", { name: "Exports tab" })).toHaveAttribute(
      "href",
      "/admin?tab=exports",
    );
    expect(screen.getByRole("link", { name: "Audio tab" })).toHaveAttribute("href", "/admin?tab=audio");
    expect(screen.getByRole("link", { name: "Feed Stats page" })).toHaveAttribute("href", "/feed-stats");
  });

  it("filters across every group at once and narrows the index with it", async () => {
    render();
    await screen.findByRole("navigation", { name: /settings groups/i });

    await userEvent.type(screen.getByRole("searchbox", { name: /filter settings/i }), "reconcile");

    expect(screen.getByText("reconcile_min_overlap")).toBeInTheDocument();
    expect(screen.queryByText("stt_prompt_extra")).not.toBeInTheDocument();
    const index = screen.getByRole("navigation", { name: /settings groups/i });
    const links = within(index).getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveTextContent(/Ingest\s*3/);
  });

  it("says so when the filter matches nothing, and offers the way back", async () => {
    render();
    await screen.findByRole("navigation", { name: /settings groups/i });

    await userEvent.type(screen.getByRole("searchbox", { name: /filter settings/i }), "zzzznothing");
    expect(await screen.findByText(/no setting matches that/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /clear filter/i }));
    expect(await screen.findByText("stt_prompt_extra")).toBeInTheDocument();
  });

  it("shows the save bar only once something is dirty, and counts the changes", async () => {
    render();
    const gap = await screen.findByLabelText("Conversation gap");
    expect(screen.queryByRole("button", { name: /save changes/i })).not.toBeInTheDocument();

    await userEvent.clear(gap);
    await userEvent.type(gap, "45");
    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();

    const attempts = screen.getByLabelText("Max delivery attempts");
    await userEvent.clear(attempts);
    await userEvent.type(attempts, "9");
    expect(await screen.findByText("2 unsaved changes")).toBeInTheDocument();

    // And the index marks which groups are holding an edit.
    const index = screen.getByRole("navigation", { name: /settings groups/i });
    expect(within(index).getAllByTitle(/unsaved changes in this group/i)).toHaveLength(2);
  });

  it("takes a field back out of the draft when it is typed back to what is stored", async () => {
    render();
    const gap = await screen.findByLabelText("Conversation gap");

    await userEvent.clear(gap);
    await userEvent.type(gap, "45");
    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();

    await userEvent.clear(gap);
    await userEvent.type(gap, "90");
    await waitFor(() => expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument());
  });

  it("discards every edit at once", async () => {
    render();
    const gap = await screen.findByLabelText("Conversation gap");
    await userEvent.clear(gap);
    await userEvent.type(gap, "45");

    await userEvent.click(await screen.findByRole("button", { name: /discard/i }));

    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
    expect(await screen.findByLabelText("Conversation gap")).toHaveValue(90);
  });

  it("offers a reset only on a setting that is off its default, and it puts it back", async () => {
    render();
    // `conversation_gap_seconds` is 90 against a default of 120 in the mock table.
    const reset = await screen.findByRole("button", { name: /reset conversation gap to default/i });
    // A setting still at its default has nothing to put back.
    expect(
      screen.queryByRole("button", { name: /reset max delivery attempts to default/i }),
    ).not.toBeInTheDocument();

    await userEvent.click(reset);
    expect(await screen.findByLabelText("Conversation gap")).toHaveValue(120);
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
  });

  it("never offers a reset on a secret, whose stored value it cannot know", async () => {
    render();
    await screen.findByText("webhook_secret");
    expect(
      screen.queryByRole("button", { name: /reset webhook signing secret to default/i }),
    ).not.toBeInTheDocument();
  });

  it("saves only the settings that were touched", async () => {
    await signInAsAdmin();
    render();

    const gap = await screen.findByLabelText("Conversation gap");
    await userEvent.clear(gap);
    await userEvent.type(gap, "45");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Settings saved"));
    await waitFor(() => expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument());
  });

  it("cannot be edited while the archive is read-only", async () => {
    renderWithProviders(<SettingsTab canWrite={false} />, "/admin?tab=settings");
    expect(await screen.findByLabelText("Conversation gap")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /save changes/i })).not.toBeInTheDocument();
  });
});
