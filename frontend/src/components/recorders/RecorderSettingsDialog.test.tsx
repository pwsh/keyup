import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import type { Recorder } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { RecorderTab } from "./RecorderTab";

/**
 * The recorder settings dialog — what keyup tells a recorder as a whole.
 *
 * The cases worth writing are the ones the dialog exists to get right rather than the ones
 * a form gets right by being a form: that the shared key is never in the list the page
 * already loaded, that "leave it to the recorder's config" is a real third state and not a
 * dressed-up "off", and that a save sends only what changed — because every field here is
 * one keyup takes *over* from the recorder's own TOML, and taking one over by accident is
 * how a setting stops being fixable on the box.
 */
async function openSettings(instance: string): Promise<HTMLElement> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
  const user = userEvent.setup();
  renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
  await user.click(await screen.findByRole("button", { name: `Settings for ${instance}` }));
  return await screen.findByRole("dialog");
}

const recorderNamed = async (instance: string): Promise<Recorder> => {
  const recorders = await apiGet<Recorder[]>("/admin/recorders");
  return recorders.find((r) => r.instance_id === instance) as Recorder;
};

describe("the recorder settings dialog", () => {
  it("shows the key's fingerprint and never the key itself", async () => {
    const dialog = await openSettings("trunkd-central-01");
    // The fingerprint is on the card *and* in the dialog: matching two peers is the one
    // question about a key a page may answer, and it takes both of them on screen.
    expect(within(dialog).getAllByText("3f9a1c07").length).toBeGreaterThan(0);
    const key = within(dialog).getByLabelText("Shared key") as HTMLInputElement;
    expect(key.type).toBe("password");
    expect(key.value).not.toContain("3f9a1c07");
  });

  it("fetches the key only when the operator asks for it", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    const key = within(dialog).getByLabelText("Shared key") as HTMLInputElement;
    expect(key.value).toBe("••••••••••••••••");

    await user.click(within(dialog).getByRole("button", { name: /Reveal/ }));
    await screen.findByDisplayValue(/^3f9a1c07/);
    expect((within(dialog).getByLabelText("Shared key") as HTMLInputElement).type).toBe("text");
  });

  it("generates a key without saving one", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    await user.click(within(dialog).getByRole("button", { name: /Generate/ }));
    const key = (await screen.findByLabelText("Shared key")) as HTMLInputElement;
    expect(key.value).toHaveLength(64);
    // Nothing is stored until the dialog is saved: a key pressed and thought better of must
    // not have broken a working link.
    expect((await recorderNamed("trunkd-central-01")).desired_link?.key_fingerprint).toBe("3f9a1c07");
  });

  it("offers 'leave it to the recorder's config' as a state of its own", async () => {
    const dialog = await openSettings("trunkd-central-01");
    const hold = within(dialog).getByLabelText("Upload hold");
    // The roof box is on its own configuration, and the control says so rather than reading
    // as "not holding" — which is a different fact with a different consequence.
    expect(hold).toHaveTextContent("Leave it to the recorder's config");
  });

  it("lays the settings out in sections, with the link's plumbing folded away", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    for (const title of ["Instance", "Uplink hold", "Event log", "Link"]) {
      expect(within(dialog).getByRole("heading", { name: title })).toBeInTheDocument();
    }
    // The bind address and the hello interval are transport plumbing nobody sets twice, so
    // they start folded — but they are one click away, not gone.
    expect(within(dialog).queryByLabelText("Bind address")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /Show/ }));
    expect(within(dialog).getByLabelText("Bind address")).toHaveValue("0.0.0.0:9110");
    expect(within(dialog).getByLabelText("Hello interval (s)")).toBeInTheDocument();
  });

  it("puts the pending badge on the field's own label line", async () => {
    const dialog = await openSettings("trunkd-central-01");
    const badge = within(dialog).getByText(/pending → Ship the daily log here/);
    // The label row, not a line of its own under the control: a badge on its own row reads
    // as another control and costs a row of height in a form that had too many.
    const row = badge.closest("span.mb-1") as HTMLElement;
    expect(row).not.toBeNull();
    expect(within(row).getByText("Event-log archive")).toBeInTheDocument();
  });

  it("says what the recorder actually applied, beside what keyup wants", async () => {
    const dialog = await openSettings("trunkd-central-01");
    // The fixture's roof box has applied version 0 of 1: the archive flag is saved here and
    // not yet taken up, and the badge says which way it is going.
    expect(within(dialog).getByText(/pending → Ship the daily log here/)).toBeInTheDocument();
    expect(within(dialog).getByTestId("link-applied")).toHaveTextContent("3f9a1c07");
  });

  it("saves only what changed, and takes nothing over by being opened", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    // Opening and saving an untouched dialog must not bump the version the recorder polls:
    // every bump costs the recorder a re-apply of the whole block.
    const after = await recorderNamed("trunkd-central-01");
    expect(after.desired_recorder_version).toBe(1);
  });

  it("hands the whole link back to the recorder when it stops being managed", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    await user.click(within(dialog).getByRole("switch", { name: /Keyup manages the link/ }));
    expect(within(dialog).getByTestId("link-unmanaged")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    const after = await recorderNamed("trunkd-central-01");
    expect(after.desired_link).toBeNull();
    expect(after.desired_recorder_version).toBe(2);
  });

  it("renames the recorder, and refuses a name another one already holds", async () => {
    const user = userEvent.setup();
    const dialog = await openSettings("trunkd-central-01");
    const name = within(dialog).getByLabelText("Instance name");
    await user.clear(name);
    await user.type(name, "trunkd-central-02");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    // The dialog stays open on a refusal — the operator needs the field to fix it — and the
    // recorder keeps the name it had.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect((await recorderNamed("trunkd-central-01")).id).toBe(1);

    await user.clear(within(dialog).getByLabelText("Instance name"));
    await user.type(within(dialog).getByLabelText("Instance name"), "signal");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect((await recorderNamed("signal")).id).toBe(1);
  });
});

describe("the reported station", () => {
  it("shows what the operator wrote about the site, read-only, above the settings", async () => {
    const dialog = await openSettings("trunkd-central-01");
    expect(within(dialog).getByRole("heading", { name: "Station" })).toBeInTheDocument();
    expect(within(dialog).getByText("Anytown, US")).toBeInTheDocument();
    // The notes are the reason the block is here: "deaf to the north-west" is a fact that
    // changes what an operator does on this form, and it must not be a hover away.
    const notes = within(dialog).getByTestId("station-notes");
    expect(notes).toHaveTextContent("mast work every second Tuesday");
    expect(notes).toHaveClass("whitespace-pre-wrap");
    // Read-only: no input claims any of it.
    expect(within(dialog).queryByLabelText("Location")).not.toBeInTheDocument();
  });
});
