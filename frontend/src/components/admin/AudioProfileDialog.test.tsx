import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { apiPost } from "../../api/client";
import { AUDIO_PROFILES, CALLS } from "../../api/mock/dataset";
import type { AudioProfile } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { AudioProfileDialog } from "./AudioProfileDialog";

const aircraft = AUDIO_PROFILES.find((profile) => profile.name === "Aircraft cabin") as AudioProfile;

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/** The steps as the reader sees them, in order — each row leads with its position. */
const stepOrder = (): string[] =>
  screen.getAllByRole("button", { name: /^Remove / }).map((button) => {
    const name = button.getAttribute("aria-label") ?? "";
    return name.slice("Remove ".length);
  });

/**
 * The chain editor.
 *
 * Its whole job is to produce a list of steps in an order, from fields it did not know
 * about until the server described them — so the cases below are the three ways that list
 * changes (add, remove, reorder), and the one thing the preview exists for: hearing the
 * difference. Nothing here asserts on a hardcoded range or label, because nothing in the
 * component holds one; the catalog fixture is the server's own table.
 */
describe("AudioProfileDialog", () => {
  it("builds its fields from the server's catalog rather than from anything local", async () => {
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} />);

    // "Band gain" is `equalizer`'s label in the backend's whitelist, and its three
    // parameters are that entry's, with the bounds it declares.
    const gain = await screen.findByLabelText("Band gain gain_db");
    expect(gain).toHaveValue(-40);
    expect(gain).toHaveAttribute("min", "-60");
    expect(gain).toHaveAttribute("max", "20");
    // `nr` has no default in the whitelist, so a step that omits it shows "off" rather
    // than a number nobody chose — this profile does set it, and shows the number.
    expect(screen.getByLabelText("FFT denoise nr")).toHaveValue(12);
    expect(screen.getByLabelText("FFT denoise nr")).toHaveAttribute("placeholder", "off");
  });

  it("adds a step with the whitelist's defaults already in it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={null} />);

    expect(await screen.findByText(/^0 of 12$/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Step to add" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Noise gate" }));
    await user.click(screen.getByRole("button", { name: /Add step/ }));

    // `agate`'s four defaults, filled from the catalog — the operator adjusts numbers
    // rather than inventing a chain that does not render.
    expect(await screen.findByLabelText("Noise gate threshold")).toHaveValue(-50);
    expect(screen.getByLabelText("Noise gate ratio")).toHaveValue(4);
    expect(screen.getByText(/^1 of 12$/)).toBeInTheDocument();
  });

  it("removes a step and leaves the rest of the chain alone", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} />);

    // Waiting on the *labelled* button, not on the count: until the catalog arrives a
    // step can only be named by its ffmpeg kind, and asserting the order before then
    // would be asserting on the loading state.
    const remove = await screen.findByRole("button", { name: "Remove Band gain" });
    expect(screen.getByText(/^3 of 12$/)).toBeInTheDocument();
    expect(stepOrder()).toEqual(["High-pass", "Band gain", "FFT denoise"]);

    await user.click(remove);

    await waitFor(() => expect(stepOrder()).toEqual(["High-pass", "FFT denoise"]));
    // The survivors keep their numbers: a removal is not a reset.
    expect(screen.getByLabelText("High-pass f")).toHaveValue(250);
    expect(screen.getByLabelText("FFT denoise nf")).toHaveValue(-28);
  });

  it("reorders the chain, and will not move an end step off either end", async () => {
    // Order is the substance of a filter graph — a high-pass after a denoiser is a
    // different sound — so the buttons have to move exactly one place and stop.
    const user = userEvent.setup();
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} />);

    expect(await screen.findByRole("button", { name: "Move High-pass earlier" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move FFT denoise later" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Move Band gain earlier" }));
    await waitFor(() => expect(stepOrder()).toEqual(["Band gain", "High-pass", "FFT denoise"]));

    await user.click(screen.getByRole("button", { name: "Move Band gain later" }));
    await waitFor(() => expect(stepOrder()).toEqual(["High-pass", "Band gain", "FFT denoise"]));
  });

  it("stops offering Add step at the whitelist's cap", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} />);

    const add = await screen.findByRole("button", { name: /Add step/ });
    // Three already, so nine more reach the twelve the server allows.
    for (let index = 0; index < 9; index += 1) await user.click(add);

    expect(await screen.findByText(/^12 of 12$/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Add step/ })).toBeDisabled());
  });

  it("renders a preview and switches the ear between the two versions", async () => {
    await signIn();
    const user = userEvent.setup();
    const callId = (CALLS[0] as { id: number }).id;
    renderWithProviders(
      <AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} callId={String(callId)} />,
    );

    await user.click(await screen.findByRole("button", { name: /Render/ }));

    // The chain the server says it ran, from the `X-Keyup-Filter` header — the preview is
    // a reading rather than a black box.
    expect(await screen.findByText(/equalizer=f=1950:w=500:g=-40:t=h/)).toBeInTheDocument();

    // Both players are mounted, so the toggle swaps which is on screen rather than
    // reloading a source and losing the reader's place in the clip.
    const processed = screen.getByLabelText("With this chain");
    const original = screen.getByLabelText("Original");
    expect(processed).toBeVisible();
    expect(original).not.toBeVisible();
    expect(processed).toHaveAttribute("src", expect.stringContaining("blob:"));
    expect(original).toHaveAttribute("src", expect.stringContaining(`/calls/${callId}/audio?raw=1`));

    await user.click(screen.getByRole("button", { name: "Original" }));
    await waitFor(() => expect(screen.getByLabelText("Original")).toBeVisible());
    expect(screen.getByLabelText("With this chain")).not.toBeVisible();
  });

  it("prefills the preview from the call the Tune… link carried", async () => {
    renderWithProviders(
      <AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} callId="1103864" />,
    );
    expect(await screen.findByLabelText(/call id/i)).toHaveValue(1103864);
  });

  it("shows the server's own sentence when it refuses a chain", async () => {
    // A 422 names the step and the parameter it objects to, which is a thing to fix and
    // therefore belongs beside the field rather than in a toast that scrolls away.
    await signIn();
    const user = userEvent.setup();
    vi.spyOn(client, "apiPost").mockRejectedValueOnce(
      new client.ApiError(
        "422",
        422,
        "/admin/audio/profiles",
        "equalizer.gain_db must be between -60 and 20 dB (got -400)",
      ),
    );
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={null} />);

    await user.type(await screen.findByLabelText("Name"), "Too deep");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must be between -60 and 20 dB/);
    vi.mocked(client.apiPost).mockRestore();
  });

  it("stores the chain it was given, in the order it is shown", async () => {
    await signIn();
    const user = userEvent.setup();
    const patch = vi.spyOn(client, "apiPatch");
    renderWithProviders(<AudioProfileDialog open onOpenChange={vi.fn()} profile={aircraft} />);

    await user.click(await screen.findByRole("button", { name: "Move Band gain earlier" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = patch.mock.calls.find(([path]) => path === `/admin/audio/profiles/${aircraft.id}`);
      expect(call).toBeDefined();
      const body = (call ?? [])[1] as { chain: { kind: string }[] };
      expect(body.chain.map((step) => step.kind)).toEqual(["equalizer", "highpass", "afftdn"]);
    });
    patch.mockRestore();
  });
});
