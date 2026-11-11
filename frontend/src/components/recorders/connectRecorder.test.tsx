import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import type { ApiTokenInfo, Recorder } from "../../api/types";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { ConnectRecorderCard, snippetFor } from "./ConnectRecorderCard";
import { RecorderPolicyStrip, RecorderTab } from "./RecorderTab";

async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * Getting a recorder onto the page at all.
 *
 * The empty state used to say a recorder "appears the first time it polls", which does not
 * say that polling needs a key, that the key needs the `upload` scope, or what to paste
 * where — three facts that lived in a token dialog and a Markdown file and never appeared
 * on the same screen as each other.
 */
describe("the connect-a-recorder card", () => {
  it("shows a placeholder until a key is minted, and never a half-filled snippet", async () => {
    renderWithProviders(<ConnectRecorderCard canAdmin={false} />);
    expect(screen.getByText(/paste an upload-scoped token/)).toBeInTheDocument();
    // Nothing to mint with: minting is admin-only, and the button says so rather than 403-ing.
    expect(screen.getByRole("button", { name: "Mint an upload token" })).toBeDisabled();
  });

  it("mints an upload-scoped key and puts it straight into the configuration", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<ConnectRecorderCard canAdmin />);

    await user.type(screen.getByLabelText("Token label"), "attic trunkd");
    await user.click(screen.getByRole("button", { name: "Mint an upload token" }));

    const shown = await screen.findByTestId("minted-token");
    const secret = shown.textContent ?? "";
    expect(secret).toMatch(/^kup_/);

    // The snippet carries the real key, which is the whole point: an operator copies one
    // block rather than reading a key out of a dialog and into a file by hand.
    const snippet = document.querySelector("pre")?.textContent ?? "";
    expect(snippet).toContain(secret);
    expect(snippet).toContain("[uplink]");
    expect(snippet).not.toContain("paste an upload-scoped token");

    // …and it really was minted, with the one scope a recorder needs and no more.
    const tokens = await apiGet<ApiTokenInfo[]>("/admin/tokens");
    const minted = tokens.find((token) => token.label === "attic trunkd");
    expect(minted?.scopes).toEqual(["upload"]);
  });

  it("switches the snippet to the recorder the operator actually runs", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ConnectRecorderCard canAdmin />);

    expect(document.querySelector("pre")?.textContent).toContain("trunkd.toml");

    await user.click(screen.getByRole("combobox", { name: "Recorder software" }));
    await user.click(await screen.findByRole("option", { name: /trunk-recorder/ }));
    await waitFor(() => expect(document.querySelector("pre")?.textContent).toContain("rdioscanner_uploader"));
  });

  it("points every recorder at this deployment's own upload endpoint", () => {
    // The one piece of the configuration the server cannot know: behind a proxy it has no
    // idea what address the outside world uses, and the operator is looking at it.
    for (const kind of ["trunkd", "sdrtrunk", "trunk-recorder"] as const) {
      expect(snippetFor(kind, "kup_x", "http://192.0.2.60:8096")).toContain(
        "http://192.0.2.60:8096/api/call-upload",
      );
    }
  });

  it("is offered on the Recorder tab even before any recorder exists", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect(await screen.findByText("Connect a recorder")).toBeInTheDocument();
  });
});

describe("editing a recorder's name", () => {
  it("saves the label on blur, through the same policy PUT as the rest of the strip", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    const input = (await screen.findByLabelText("Name for trunkd-central-01")) as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "Attic recorder");
    await user.tab();

    await waitFor(async () => {
      const rows = await apiGet<Recorder[]>("/admin/recorders");
      expect(rows.find((row) => row.instance_id === "trunkd-central-01")?.label).toBe("Attic recorder");
    });
  });

  it("is read-only for someone who is not an admin", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect(await screen.findByLabelText("Name for trunkd-central-01")).toBeDisabled();
  });
});

describe("forgetting a recorder", () => {
  it("confirms first, and says what is and is not lost", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    const recorders = await apiGet<Recorder[]>("/admin/recorders");
    const recorder = recorders[0] as Recorder;
    const name = recorder.label ?? recorder.instance_id;
    renderWithProviders(<RecorderPolicyStrip recorder={recorder} canAdmin />);

    await user.click(screen.getByRole("button", { name: `Forget ${name}` }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Nothing this recorder recorded is touched/)).toBeInTheDocument();
    // Nothing has happened yet: the dialog is the confirmation.
    expect((await apiGet<Recorder[]>("/admin/recorders")).length).toBe(recorders.length);
  });

  it("surfaces the API's refusal when the recorder is still reporting", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    const recorders = await apiGet<Recorder[]>("/admin/recorders");
    const recorder = recorders[0] as Recorder;
    const name = recorder.label ?? recorder.instance_id;
    renderWithProviders(<RecorderPolicyStrip recorder={recorder} canAdmin />);

    await user.click(screen.getByRole("button", { name: `Forget ${name}` }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Forget it" }));

    // The 409's own sentence, not a generic failure: "it would come straight back on its
    // next poll" is the entire answer, and the recorder is still there.
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.body).toMatch(/still reporting/));
    expect((await apiGet<Recorder[]>("/admin/recorders")).length).toBe(recorders.length);
  });

  it("is disabled for someone who is not an admin", async () => {
    const recorders = await apiGet<Recorder[]>("/admin/recorders");
    const recorder = recorders[0] as Recorder;
    renderWithProviders(<RecorderPolicyStrip recorder={recorder} canAdmin={false} />);
    expect(
      screen.getByRole("button", { name: `Forget ${recorder.label ?? recorder.instance_id}` }),
    ).toBeDisabled();
  });
});
