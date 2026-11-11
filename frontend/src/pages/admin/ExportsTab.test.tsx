import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import type { SettingDefinition } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { SettingsTab } from "../Admin";
import { ExportsTab } from "./ExportsTab";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const settingValue = async (key: string): Promise<string> => {
  const rows = await apiGet<SettingDefinition[]>("/admin/settings");
  const row = rows.find((entry) => entry.key === key);
  if (!row) throw new Error(`no setting ${key}`);
  return row.value;
};

/** The switches, in the order the page renders them. */
const FEED = 0;
const CALLS = 1;

/**
 * The one rule this tab must never break: it may not be the thing that starts
 * publishing. Everything below is either a check that a service is shown as off, or a
 * check that turning one on takes a deliberate second action.
 */
describe("ExportsTab", () => {
  // Which cards are open is remembered per browser, so each test starts from the shipped
  // state rather than from whatever the last one left behind.
  beforeEach(() => window.localStorage.clear());

  it("shows all three destinations switched off", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    expect(await screen.findByText("Broadcastify Feed (live audio stream)")).toBeInTheDocument();
    expect(screen.getByText("Broadcastify Calls (per-call upload)")).toBeInTheDocument();
    expect(screen.getByText("OpenMHz")).toBeInTheDocument();
    expect(screen.getAllByText("off")).toHaveLength(3);
    expect(screen.queryByText("uploading")).not.toBeInTheDocument();
    expect(screen.queryByText("streaming")).not.toBeInTheDocument();
  });

  it("names the feed's fields the way Broadcastify's own page names them", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    // The five values an operator copies off the feed's management page…
    await screen.findByLabelText(/Feed server/);
    for (const label of ["Feed server", "Feed port", "Feed mount", "Feed password", "Feed username"]) {
      expect(screen.getByLabelText(new RegExp(label))).toBeInTheDocument();
    }
    // …under the heading that page gives them.
    expect(screen.getAllByText(/Feed Technical Details \(Advanced\)/).length).toBeGreaterThan(0);
  });

  it("says the talkgroup selection covers both Broadcastify products", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("Broadcastify Feed (live audio stream)");
    // The feed's card points at the shared selection, and the Calls card says it is shared.
    expect(screen.getByText(/one set of checkboxes for both Broadcastify products/)).toBeInTheDocument();
    expect(screen.getByText("both Broadcastify products")).toBeInTheDocument();
  });

  it("keeps the feed's mount password out of the form's readable value", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByLabelText(/Feed password/);
    const field = document.getElementById("export-setting-broadcastify_feed_password");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveValue("");
  });

  it("warns about running two uploaders on one system", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    expect(await screen.findByText("Run exactly one uploader per system.")).toBeInTheDocument();
  });

  it("asks to confirm before it starts uploading, and does nothing if you cancel", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Calls (per-call upload)");
    await user.click(screen.getAllByRole("switch")[CALLS] as HTMLElement);

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/Start uploading to Broadcastify Calls \(per-call upload\)\?/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await settingValue("export_broadcastify_enabled")).toBe("false");
  });

  it("turns the switch on only after the confirm", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Calls (per-call upload)");
    await user.click(screen.getAllByRole("switch")[CALLS] as HTMLElement);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Turn it on" }));

    await waitFor(async () => {
      expect(await settingValue("export_broadcastify_enabled")).toBe("true");
    });
    // …and neither the other service nor the feed is touched.
    expect(await settingValue("export_openmhz_enabled")).toBe("false");
    expect(await settingValue("export_broadcastify_feed_enabled")).toBe("false");
  });

  it("warns that a mount takes one source before it will start the feed", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Feed (live audio stream)");
    await user.click(screen.getAllByRole("switch")[FEED] as HTMLElement);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Start streaming to the Broadcastify Feed\?/)).toBeInTheDocument();
    expect(within(dialog).getByText(/source at a time/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await settingValue("export_broadcastify_feed_enabled")).toBe("false");
    // Cancelling the feed's dialog must not have started an upload either.
    expect(await settingValue("export_broadcastify_enabled")).toBe("false");
  });

  it("starts the feed only after the confirm", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Feed (live audio stream)");
    await user.click(screen.getAllByRole("switch")[FEED] as HTMLElement);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Start streaming" }));

    await waitFor(async () => {
      expect(await settingValue("export_broadcastify_feed_enabled")).toBe("true");
    });
    expect(await settingValue("export_broadcastify_enabled")).toBe("false");
  });

  it("offers the three talkgroup selection modes", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    const select = await screen.findByLabelText("Broadcastify talkgroup selection");
    expect(select).toHaveTextContent("all");
  });

  it("says what a credential test actually proved", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Calls (per-call upload)");
    await user.click(screen.getAllByRole("button", { name: /Test credentials/ })[0] as HTMLElement);

    // Broadcastify's documented `test=1` check: it names the parameter it used and says,
    // in the same breath, that no call was registered — the claim an operator has to be
    // able to read before trusting a green result on a service that must stay off.
    expect(await screen.findByText(/test=1 parameter/)).toBeInTheDocument();
    expect(await screen.findByText(/no call is registered/)).toBeInTheDocument();
  });

  it("shows the environment, its monitoring links and the host clock", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Calls (per-call upload)");
    // The environment reads at a glance: production here, and a loud badge when it is not.
    // "production" also appears in the Environment select's own options, so the badge is
    // picked out by the label beside it rather than by its text alone.
    expect(await screen.findByText("Environment")).toBeInTheDocument();
    expect(screen.getAllByText("production").length).toBeGreaterThan(0);
    expect(await screen.findByText("Status page")).toBeInTheDocument();
    expect(await screen.findByText("Node management")).toBeInTheDocument();
    // Step 1 of the Calls workflow, on the card.
    expect(await screen.findByText("Host clock")).toBeInTheDocument();
    expect(await screen.findByText(/\+0\.031 s/)).toBeInTheDocument();
  });

  it("says what the mount test actually did", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");

    await screen.findByText("Broadcastify Feed (live audio stream)");
    await user.click(screen.getByRole("button", { name: /Test mount/ }));

    expect(await screen.findByText(/the server, mount or password is blank/)).toBeInTheDocument();
  });

  it("shows an empty ledger rather than pretending", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    expect(await screen.findByText("Nothing has been exported")).toBeInTheDocument();
  });

  /* ------------------------------------------------ status first, settings behind it */

  it("shows each service's status and folds its settings away", async () => {
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("OpenMHz");

    // The numbers an operator came for are on the card, unfolded…
    expect(screen.getAllByText("Queue depth").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Last upload").length).toBeGreaterThan(0);
    // …and the form they set up once is not.
    expect(document.getElementById("export-settings-openmhz")).toHaveAttribute("hidden");
    expect(document.getElementById("export-settings-broadcastify")).toHaveAttribute("hidden");
    expect(document.getElementById("export-settings-feed")).toHaveAttribute("hidden");
  });

  it("opens one card's settings and remembers it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("OpenMHz");

    const panel = document.getElementById("export-settings-openmhz") as HTMLElement;
    const toggle = panel.previousElementSibling as HTMLElement;
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);

    expect(panel).not.toHaveAttribute("hidden");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // Per browser, and only ever a convenience: the server is never told.
    expect(window.localStorage.getItem("keyup.exports.open.openmhz")).toBe("true");
  });

  /* --------------------------------------------------------------- the save bar */

  it("shows the save bar only once something has changed, and can discard it", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("OpenMHz");

    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();

    const panel = document.getElementById("export-settings-openmhz") as HTMLElement;
    await user.click(panel.previousElementSibling as HTMLElement);
    await user.type(screen.getByLabelText(/OpenMHz short name/), "examplecty");

    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.queryByText("1 unsaved change")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
    // Nothing was written on the way past.
    expect(await settingValue("openmhz_short_name")).toBe("");
  });

  it("will not let a card fold an unsaved change out of sight", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("OpenMHz");

    const panel = document.getElementById("export-settings-openmhz") as HTMLElement;
    const toggle = panel.previousElementSibling as HTMLElement;
    await user.click(toggle);
    await user.type(screen.getByLabelText(/OpenMHz short name/), "x");

    await user.click(toggle); // …asking for it to close
    expect(panel).not.toHaveAttribute("hidden");
    // …and the disclosure says why it stayed open.
    expect(within(toggle).getByText("1 unsaved")).toBeInTheDocument();
  });

  /* ------------------------------------------- trunked systems and the draft test */

  it("names the systems Broadcastify Calls is allowed to publish", async () => {
    await signInAsAdmin();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("Broadcastify Calls (per-call upload)");

    expect(await screen.findByText("Trunked systems")).toBeInTheDocument();
    // The names arrive with the status read, so this is the assertion that waits.
    expect(await screen.findByText(/Example Network, /)).toBeInTheDocument();
    // With one to publish, the switch is offered rather than held disabled.
    expect(screen.getAllByRole("switch")[CALLS]).toBeEnabled();
  });

  it("offers only trunked systems in the Calls system list", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("Broadcastify Calls (per-call upload)");

    const panel = document.getElementById("export-settings-broadcastify") as HTMLElement;
    await user.click(panel.previousElementSibling as HTMLElement);

    // The two trunked demo systems are offered; the conventional one is not, because a
    // call from it would be refused by the gate however it was ticked here.
    expect(await within(panel).findByLabelText(/Example Network/)).toBeInTheDocument();
    expect(within(panel).queryByLabelText(/Example County Fire/)).not.toBeInTheDocument();
    // …and the key is still rendered, which is what says this page is its only home.
    expect(within(panel).getByText("export_broadcastify_systems")).toBeInTheDocument();
  });

  it("tests the values on the screen rather than the saved ones", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("OpenMHz");

    const panel = document.getElementById("export-settings-openmhz") as HTMLElement;
    await user.click(panel.previousElementSibling as HTMLElement);
    await user.type(screen.getByLabelText(/OpenMHz short name/), "typed-just-now");
    await user.click(screen.getAllByRole("button", { name: /Test credentials/ })[1] as HTMLElement);

    // The result says so, and the draft is still unsaved afterwards.
    expect(await screen.findByText(/nothing was saved/)).toBeInTheDocument();
    expect(await settingValue("openmhz_short_name")).toBe("");
  });
});

/**
 * The Settings tab used to render group "Export" as well, which meant two forms with two
 * drafts over one table: type a credential in one, save in the other, lose it. The Export
 * group now belongs to this page alone, so this page has to render *all* of it — a knob
 * added to `SETTINGS_SPEC` that matched none of the filters here would otherwise become
 * unreachable rather than merely misplaced.
 */
describe("Export settings have exactly one home", () => {
  /** The three switches rendered as card headers rather than as labelled fields. */
  const ENABLE_KEYS = [
    "export_broadcastify_enabled",
    "export_openmhz_enabled",
    "export_broadcastify_feed_enabled",
  ];

  const groupKeys = async (...groups: string[]): Promise<string[]> => {
    const rows = await apiGet<SettingDefinition[]>("/admin/settings");
    return rows.filter((row) => groups.includes(row.group)).map((row) => row.key);
  };

  /**
   * Every key the rendered form shows.
   *
   * Two shapes to read, because the two tabs lay a setting out differently: Exports (and
   * Audio) stack the key inside the field's own label, while the Settings tab puts the
   * label and the control on one row and tags the row with `data-setting`.
   */
  const renderedKeys = (): Set<string> =>
    new Set([
      ...[...document.querySelectorAll("label .font-mono")].map((node) => node.textContent ?? ""),
      ...[...document.querySelectorAll("[data-setting]")].map(
        (node) => node.getAttribute("data-setting") ?? "",
      ),
    ]);

  it("renders every Export-group key on the Exports tab", async () => {
    const keys = await groupKeys("Export");
    expect(keys.length).toBeGreaterThan(20);

    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("Broadcastify Feed (live audio stream)");
    await waitFor(() => expect(renderedKeys().size).toBeGreaterThan(0));

    const shown = renderedKeys();
    expect(keys.filter((key) => !shown.has(key) && !ENABLE_KEYS.includes(key))).toEqual([]);
    // The three enable switches are on the cards rather than in the field grid.
    expect(screen.getAllByRole("switch").length).toBeGreaterThanOrEqual(ENABLE_KEYS.length);
  });

  it("counts duplicates on the Calls card, and only there", async () => {
    // "Uploaded 63, failed 1 711" reads as a broken uploader until the fourth number is on
    // the card: 3 485 calls Broadcastify already had. It is a Calls answer, so OpenMHz —
    // whose protocol has no such reply — must not appear to make the same claim.
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    await screen.findByText("Broadcastify Calls (per-call upload)");

    expect(screen.getAllByText("Duplicates (24 h)")).toHaveLength(1);
    // Nothing has been answered in the demo clone, and an empty histogram is not rendered
    // as an empty row: "no responses" is not the same as "no attempts".
    expect(screen.queryByText("Responses (24 h)")).not.toBeInTheDocument();
  });

  it("renders none of them on the Settings tab, and says where they went", async () => {
    const keys = await groupKeys("Export", "Feed stats", "Audio");

    renderWithProviders(<SettingsTab canWrite={false} />, "/admin?tab=settings");
    await screen.findByText("Runtime settings");
    await waitFor(() => expect(renderedKeys().size).toBeGreaterThan(0));

    const shown = renderedKeys();
    expect(keys.filter((key) => shown.has(key))).toEqual([]);
    expect(screen.getByRole("link", { name: "Exports tab" })).toHaveAttribute("href", "/admin?tab=exports");
    expect(screen.getByRole("link", { name: "Feed Stats page" })).toHaveAttribute("href", "/feed-stats");
    expect(screen.getByRole("link", { name: "Audio tab" })).toHaveAttribute("href", "/admin?tab=audio");
  });
});
