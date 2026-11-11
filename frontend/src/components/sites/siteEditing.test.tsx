import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import { SITES } from "../../api/mock/dataset";
import type { SiteInfo } from "../../api/types";
import { Sites } from "../../pages/Sites";
import { renderWithProviders } from "../../test/render";
import { EditSiteDialog } from "./EditSiteDialog";
import { MergeSiteDialog } from "./MergeSiteDialog";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/** Example Network's Central — the one holding every call in the fixture. */
const CENTRAL = SITES[0] as SiteInfo;
/** The school district's North — same name as Example Network's, different system, nothing filed under it. */
const OTHER_NORTH = SITES[2] as SiteInfo;

const rowFor = async (label: string): Promise<HTMLElement> => {
  const button = await screen.findByRole("button", { name: `Audit ${label}` });
  const row = button.closest("tr");
  if (!row) throw new Error(`no row for ${label}`);
  return row;
};

/**
 * A site name does not identify a site: `sites` is unique on `(system_id, name)`, so two
 * systems can each have one called the same thing — which is how a school district's calls
 * ended up filed under the county network in the first place.  The mock dataset has that
 * shape on purpose (two `North`s), and these are the cases that keep the UI from ever
 * showing one of them on its own.
 */
describe("site names are never shown alone", () => {
  it("puts the system under every site name on the Sites page", async () => {
    renderWithProviders(<Sites />, "/sites?site=1");
    // Two rows called North — and the only thing telling them apart is the system, which
    // is on screen as a muted second line and in the button's accessible name.
    const norths = await screen.findAllByRole("button", { name: /^Audit .* \/ North$/ });
    expect(norths).toHaveLength(2);

    const systems = norths.map((button) => button.closest("tr")?.textContent ?? "");
    expect(systems.some((text) => text.includes("Example Network"))).toBe(true);
    expect(systems.some((text) => text.includes("MAPLE VALLEY"))).toBe(true);
  });

  it("labels every row action with the system as well as the site", async () => {
    renderWithProviders(<Sites />, "/sites?site=1");
    // The accessible name is what a screen reader announces, so it is the one place the
    // ambiguity would be invisible; `siteLabel` is what keeps it out.
    expect(await screen.findByRole("button", { name: "Edit Example Network / North" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Edit MAPLE VALLEY COMMUNITY SCHOOL DISTRICT / North",
      }),
    ).toBeInTheDocument();
  });
});

describe("the Sites page row actions", () => {
  it("keeps every action disabled while signed out", async () => {
    renderWithProviders(<Sites />, "/sites?site=1");
    expect(await screen.findByRole("button", { name: "Edit Example Network / Central" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Merge Example Network / Central" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete Example Network / Central" })).toBeDisabled();
  });

  it("only enables Delete on a site the API says nothing references", async () => {
    await signIn();
    renderWithProviders(<Sites />, "/sites?site=1");

    // Central holds every call in the archive, so `deletable` is false and the button is
    // dead: the operator finds out here rather than from a 409.
    const central = await rowFor("Example Network / Central");
    expect(within(central).getByRole("button", { name: /^Delete/ })).toBeDisabled();

    // The school district's empty North is the one row that can go.
    const free = await screen.findByRole("button", {
      name: "Delete MAPLE VALLEY COMMUNITY SCHOOL DISTRICT / North",
    });
    await waitFor(() => expect(free).toBeEnabled());
  });

  it("confirms before deleting, and the site is gone afterwards", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<Sites />, "/sites?site=1");

    const remove = await screen.findByRole("button", {
      name: "Delete MAPLE VALLEY COMMUNITY SCHOOL DISTRICT / North",
    });
    await waitFor(() => expect(remove).toBeEnabled());
    await user.click(remove);

    // Nothing has happened yet — the dialog is the confirmation.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/cannot be undone/i)).toBeInTheDocument();
    expect((await apiGet<SiteInfo[]>("/sites")).length).toBe(SITES.length);

    await user.click(within(dialog).getByRole("button", { name: "Delete site" }));
    await waitFor(async () => expect((await apiGet<SiteInfo[]>("/sites")).length).toBe(SITES.length - 1));
  });
});

describe("EditSiteDialog", () => {
  it("edits the three fields that are ours, and shows the system read-only", async () => {
    const user = userEvent.setup();
    await signIn();
    const onOpenChange = vi.fn();
    renderWithProviders(<EditSiteDialog site={CENTRAL} open onOpenChange={onOpenChange} />);

    // The system is on the dialog so the operator can see which Central this is, but it is
    // not editable: moving a site between systems is a re-filing, not a rename.
    expect(screen.getByLabelText("System")).toHaveValue("Example Network");
    expect(screen.getByLabelText("System")).toBeDisabled();

    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Example County");
    await user.type(screen.getByLabelText("Notes"), "Simulcast.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const saved = (await apiGet<SiteInfo[]>("/sites")).find((site) => site.id === CENTRAL.id);
    expect(saved?.name).toBe("Example County");
    expect(saved?.notes).toBe("Simulcast.");
  });

  it("will not save a site with no name at all", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EditSiteDialog site={CENTRAL} open onOpenChange={vi.fn()} />);

    await user.clear(screen.getByLabelText("Name"));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("renders nothing at all when there is no site to edit", () => {
    const { container } = renderWithProviders(<EditSiteDialog site={null} open onOpenChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("MergeSiteDialog", () => {
  it("offers only sites on the same system", async () => {
    const user = userEvent.setup();
    const sites = await apiGet<SiteInfo[]>("/sites");
    renderWithProviders(<MergeSiteDialog site={CENTRAL} sites={sites} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("combobox", { name: "Merge into" }));
    const options = screen.getByRole("listbox");
    // Example Network's own North is offered; the school district's North is not, because moving calls
    // between systems is a re-filing (`keyup refile-calls`), not a merge.
    expect(within(options).getByRole("option", { name: /North/ })).toBeInTheDocument();
    expect(within(options).getAllByRole("option", { name: /North/ })).toHaveLength(1);
  });

  it("merges into the chosen site and drops the source", async () => {
    const user = userEvent.setup();
    await signIn();
    const sites = await apiGet<SiteInfo[]>("/sites");
    const onOpenChange = vi.fn();
    const exampleNet = sites.find((site) => site.id === 2) as SiteInfo;
    renderWithProviders(<MergeSiteDialog site={exampleNet} sites={sites} open onOpenChange={onOpenChange} />);

    // Merge is destructive, so the button says so and asks for a target first.
    const confirm = screen.getByRole("button", { name: "Merge and delete" });
    expect(confirm).toBeDisabled();

    await user.click(screen.getByRole("combobox", { name: "Merge into" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: /Central/ }));
    await user.click(screen.getByRole("button", { name: "Merge and delete" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const after = await apiGet<SiteInfo[]>("/sites");
    expect(after.some((site) => site.id === exampleNet.id)).toBe(false);
  });

  it("says so when there is nothing to merge into", async () => {
    const sites = await apiGet<SiteInfo[]>("/sites");
    renderWithProviders(<MergeSiteDialog site={OTHER_NORTH} sites={sites} open onOpenChange={vi.fn()} />);
    expect(screen.getByText(/only site on its system/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Merge into" })).not.toBeInTheDocument();
  });
});
