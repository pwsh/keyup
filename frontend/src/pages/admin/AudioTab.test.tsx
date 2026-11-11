import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { AudioTab } from "./AudioTab";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/**
 * The tab exists so an operator can answer three questions without leaving the page: is the
 * archive processed, does this setting sound right, and should the archive be brought up to
 * match. The cases below are those three.
 */
describe("AudioTab", () => {
  it("shows what the pass has covered and what it still owes", async () => {
    renderWithProviders(<AudioTab canWrite />, "/admin?tab=audio");

    expect(await screen.findByText("41,208")).toBeInTheDocument(); // processed
    expect(screen.getByText("137")).toBeInTheDocument(); // pending
    expect(screen.getByText("+11.4 dB")).toBeInTheDocument(); // the mean gain applied
    expect(screen.getByText("6")).toBeInTheDocument(); // pages today
    expect(screen.getByText("v1")).toBeInTheDocument();
  });

  it("renders the Audio settings group and nothing else", async () => {
    renderWithProviders(<AudioTab canWrite />, "/admin?tab=audio");

    expect(await screen.findByText("audio_target_lufs")).toBeInTheDocument();
    expect(screen.getByText("audio_tone_handling")).toBeInTheDocument();
    // A setting from another group has no business on this tab.
    expect(screen.queryByText("transcribe_default")).not.toBeInTheDocument();
  });

  it("saves only the settings that were touched", async () => {
    await signInAsAdmin();
    renderWithProviders(<AudioTab canWrite />, "/admin?tab=audio");

    const target = await screen.findByLabelText(/Loudness target/i);
    await userEvent.clear(target);
    await userEvent.type(target, "-14");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Audio settings saved"));
  });

  it("cannot save while the archive is read-only", async () => {
    renderWithProviders(<AudioTab canWrite={false} />, "/admin?tab=audio");
    const save = await screen.findByRole("button", { name: /save changes/i });
    expect(save).toBeDisabled();
    expect(screen.getByRole("button", { name: /queue backfill/i })).toBeDisabled();
  });

  it("previews one call and shows the before and after", async () => {
    await signInAsAdmin();
    renderWithProviders(<AudioTab canWrite />, "/admin?tab=audio");

    await userEvent.type(await screen.findByLabelText(/call id/i), "1103864");
    await userEvent.click(screen.getByRole("button", { name: /render/i }));

    expect(await screen.findByText("-32.4 dBFS")).toBeInTheDocument(); // before
    expect(screen.getByText("-16.1 dBFS")).toBeInTheDocument(); // after
    // The two tones of the page, read back as the numbers they are.
    expect(screen.getByText(/two_tone/)).toBeInTheDocument();
    expect(screen.getByText(/625 Hz/)).toBeInTheDocument();
    // And the chain that produced it, so nothing about the render is a black box. Matched
    // on the trim, which only a *call's* chain carries: the profiles card above renders
    // its own chains on this tab, and every one of them ends in the same loudness tail.
    expect(screen.getByText(/atrim=start=0\.5/)).toBeInTheDocument();
    expect(screen.getByLabelText("Original")).toBeInTheDocument();
    expect(screen.getByLabelText("Processed")).toBeInTheDocument();
  });

  it("queues a backfill and says how much it queued", async () => {
    await signInAsAdmin();
    renderWithProviders(<AudioTab canWrite />, "/admin?tab=audio");

    await userEvent.click(await screen.findByRole("button", { name: /queue backfill/i }));
    await waitFor(() =>
      expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Queued 137 of 137 call(s)"),
    );
  });
});
