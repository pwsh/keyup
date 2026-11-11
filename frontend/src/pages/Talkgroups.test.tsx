import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { apiPost } from "../api/client";
import { AGENCIES, TALKGROUPS } from "../api/mock/dataset";
import { agencyLabel, categoryLabel } from "../lib/format";
import { renderWithProviders } from "../test/render";
import { Talkgroups } from "./Talkgroups";

/** The inline controls need write access, so the fixtures' operator signs in. */
async function signIn(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

const FIRE = TALKGROUPS.find((talkgroup) => talkgroup.alias === "EC FIRE DISPATCH");

/**
 * The directory as an editing surface.
 *
 * The complaint this page answers is "too many clicks for simple edits", so the
 * cases are about what one interaction now costs: a patch carrying exactly the
 * field that changed, the hidden channels kept out of the way until asked for, and
 * an honest count of how much of the directory is actually on screen.
 */
describe("Talkgroups", () => {
  it("sends only the field that changed", async () => {
    // `exclude_unset` is the whole reason a cell can be safe: a whole-row PUT from
    // the alias cell would also rewrite the priority, color and policies that the
    // reader never touched — and stamp on whatever anyone else had just changed.
    await signIn();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const alias = await screen.findByRole("button", {
      name: `Alias for talkgroup ${FIRE?.tgid}: ${FIRE?.alias}. Press Enter to edit`,
    });
    await user.click(alias);
    const input = screen.getByRole("textbox", { name: `Alias for talkgroup ${FIRE?.tgid}` });
    await user.clear(input);
    await user.type(input, "EC FIRE MAIN{Enter}");

    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls[0] ?? [];
    expect(path).toBe(`/talkgroups/${FIRE?.id}`);
    expect(body).toEqual({ alias: "EC FIRE MAIN" });
    patch.mockRestore();
  });

  it("keeps hidden channels out of the list until they are asked for", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    await screen.findByRole("heading", { name: "Talkgroups" });
    const hidden = TALKGROUPS.find((talkgroup) => talkgroup.is_hidden);
    expect(hidden).toBeDefined();
    await waitFor(() => expect(screen.queryByText(hidden?.alias as string)).not.toBeInTheDocument());

    await user.click(screen.getByRole("switch", { name: /Show hidden/ }));
    expect(await screen.findByText(hidden?.alias as string)).toBeInTheDocument();
  });

  it("says how many of the directory it is showing", async () => {
    // The list used to stop at 500 rows and say nothing about the rest.
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const visible = TALKGROUPS.filter((talkgroup) => !talkgroup.is_hidden).length;
    const count = await screen.findByText(/Showing/);
    expect(count).toHaveTextContent(`Showing 1–${visible} of ${visible} talkgroups`);
  });

  it("counts the hidden channels in too once they are shown", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    await screen.findByText(/Showing/);

    await user.click(screen.getByRole("switch", { name: /Show hidden/ }));
    await waitFor(() =>
      expect(screen.getByText(/Showing/)).toHaveTextContent(`of ${TALKGROUPS.length} talkgroups`),
    );
  });

  it("marks a hidden row rather than only omitting it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    await screen.findByRole("heading", { name: "Talkgroups" });
    await user.click(screen.getByRole("switch", { name: /Show hidden/ }));

    const hidden = TALKGROUPS.find((talkgroup) => talkgroup.is_hidden);
    const cell = await screen.findByText(hidden?.alias as string);
    const row = cell.closest("tr") as HTMLElement;
    expect(row).toHaveClass("opacity-60");
    expect(within(row).getByLabelText("Hidden from the default views")).toBeInTheDocument();
  });
  it("patches only the export flag the operator clicked", async () => {
    // The three checkboxes are per service — and Broadcastify's two products have one
    // each. Clearing the Calls one must not send the Feed one or the OpenMHz one, or a
    // bulk-selected talkgroup would quietly lose all three.
    await signIn();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const cell = await screen.findByText(FIRE?.alias as string);
    const row = cell.closest("tr") as HTMLElement;
    // `findBy`, not `getBy`: the export columns exist only once `GET /auth/me` has
    // answered, because a reader without write access is not shown them at all.
    await user.click(
      await within(row).findByRole("switch", {
        name: new RegExp(`^Publish ${FIRE?.alias} to Broadcastify Calls\\.`),
      }),
    );

    await waitFor(() => expect(patch).toHaveBeenCalled());
    const [path, body] = patch.mock.calls.at(-1) ?? [];
    expect(path).toBe(`/talkgroups/${FIRE?.id}`);
    expect(body).toEqual({ export_bcfy_calls: false });

    // …and the Feed's own checkbox is a different column writing a different key.
    await user.click(
      within(row).getByRole("switch", {
        name: new RegExp(`^Carry ${FIRE?.alias} on the Broadcastify Feed\\.`),
      }),
    );
    await waitFor(() => expect(patch.mock.calls.length).toBeGreaterThan(1));
    expect(patch.mock.calls.at(-1)?.[1]).toEqual({ export_broadcastify: false });
  });

  it("shows a column per service", async () => {
    // To the operator: the three export columns are hidden from a read-only viewer.
    await signIn();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    // Broadcastify sells two products off one account, and they are chosen separately.
    expect(await screen.findByRole("columnheader", { name: /BCFY Feed/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /BCFY Calls/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /OpenMHz/ })).toBeInTheDocument();
  });

  it("copies one export flag onto another across the directory", async () => {
    // The move an operator makes once, on the day Broadcastify's single checkbox became
    // one for the Feed and one for Calls: carry the existing selection over rather than
    // reticking several hundred channels by hand.
    await signIn();
    const post = vi.spyOn(client, "apiPost");
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    await user.click(await screen.findByRole("button", { name: /Copy export flags/ }));
    const dialog = await screen.findByRole("dialog");
    // Nothing is selected, so the scope offered is the whole directory.
    await user.click(within(dialog).getByRole("button", { name: "Copy flags" }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/admin/talkgroups/export-flags/copy", {
        source: "export_broadcastify",
        destination: "export_bcfy_calls",
        all: true,
      }),
    );
  });
});

/**
 * The same page seen without an account.
 *
 * A guest can change nothing, so a column that is only a control is not "disabled" for
 * them — it is not there. What is left is what the channel *is* and how busy it has been,
 * which is also the half that has to survive a phone's width.
 */
describe("Talkgroups without an account", () => {
  const rowFor = async (alias: string): Promise<HTMLElement> =>
    (await screen.findByText(alias)).closest("tr") as HTMLElement;

  it("shows no control column at all", async () => {
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const row = await rowFor(FIRE?.alias as string);

    // The tick boxes, the pencil, the inline selects and the policy switches. Scoped to
    // a row: the filter row above the body is a *reading* control and a guest gets it,
    // which is what the last assertion in this test pins.
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(within(row).queryAllByRole("combobox")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add .* to favorites/ })).not.toBeInTheDocument();
    for (const name of [/Broadcastify/, /OpenMHz/, /Keep the audio of/, /Transcribe/, /shared county/]) {
      expect(screen.queryByRole("switch", { name })).not.toBeInTheDocument();
    }
    // …and the toolbar's two write actions, and the admin-only link beside them.
    expect(screen.queryByRole("button", { name: /Copy export flags/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Import playlist/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Manage agencies/ })).not.toBeInTheDocument();
    // What a guest *does* keep: the filters over the columns they can still read.
    expect(screen.getByRole("combobox", { name: "Filter by category" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by agency" })).toBeInTheDocument();
    // …and none over the columns they cannot.
    expect(screen.queryByRole("combobox", { name: "Filter by Transcribe" })).not.toBeInTheDocument();
  });

  it("drops the export, policy and color headers and keeps the rest", async () => {
    renderWithProviders(<Talkgroups />, "/talkgroups");
    await screen.findByText(FIRE?.alias as string);

    for (const label of [
      /BCFY Feed/,
      /BCFY Calls/,
      /OpenMHz/,
      /Record/,
      /Transcribe/,
      /Hidden/,
      /Shared/,
      /Color/,
      /Priority/,
    ]) {
      expect(screen.queryByRole("columnheader", { name: label })).not.toBeInTheDocument();
    }
    for (const label of [
      /Alias/,
      /TGID/,
      /Agency/,
      /Category/,
      /Service tag/,
      /Calls/,
      /Last 24h/,
      /Last heard/,
    ]) {
      expect(screen.getByRole("columnheader", { name: label })).toBeInTheDocument();
    }
  });

  it("still says what the channel is", async () => {
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const row = await rowFor(FIRE?.alias as string);

    // Agency, category and service tag as plain values rather than pickers — in words,
    // not as the codes the API stores.
    const agency = AGENCIES.find((row) => row.code === FIRE?.agency_code);
    expect(within(row).getByText(categoryLabel(FIRE?.category))).toBeInTheDocument();
    expect(await within(row).findByText(agencyLabel(FIRE?.agency_code, agency))).toBeInTheDocument();
    expect(within(row).queryAllByRole("combobox")).toHaveLength(0);
    // The read-only filters are still offered.
    expect(screen.getByRole("switch", { name: /Show hidden/ })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search talkgroups" })).toBeInTheDocument();
  });

  it("fits a phone in far fewer columns than the operator's view", async () => {
    // The complaint the guest view answers: eleven control columns pushed the last-heard
    // time off the right of a phone. Nothing here scrolls sideways because nothing here
    // is wider than the values themselves.
    renderWithProviders(<Talkgroups />, "/talkgroups");
    await screen.findByText(FIRE?.alias as string);
    const guestColumns = screen.getAllByRole("columnheader").length;
    cleanup();

    await signIn();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    await screen.findByRole("button", {
      name: `Alias for talkgroup ${FIRE?.tgid}: ${FIRE?.alias}. Press Enter to edit`,
    });
    expect(screen.getAllByRole("columnheader").length).toBeGreaterThan(guestColumns);
  });

  it("gives the operator every control back", async () => {
    await signIn();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const row = (await screen.findByText(FIRE?.alias as string)).closest("tr") as HTMLElement;

    expect(within(row).getByRole("checkbox", { name: `Select ${FIRE?.alias}` })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: `Edit ${FIRE?.alias}` })).toBeInTheDocument();
    expect(
      within(row).getByRole("switch", { name: new RegExp(`^Carry ${FIRE?.alias} on the Broadcastify Feed`) }),
    ).toBeInTheDocument();
    expect(within(row).getAllByRole("combobox").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Copy export flags/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Import playlist/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /BCFY Feed/ })).toBeInTheDocument();
  });

  it("offers Record beside Transcribe, and the demo channel with it off", async () => {
    // The two halves of one decision — keep the audio, keep the words — and the radio-check
    // channel is the one the mock turns Record off on.
    await signIn();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const row = (await screen.findByText(FIRE?.alias as string)).closest("tr") as HTMLElement;

    const record = within(row).getByRole("switch", {
      name: new RegExp(`^Keep the audio of ${FIRE?.alias}`),
    });
    expect(record).toHaveAttribute("aria-checked", "true");
    expect(
      within(row).getByRole("switch", { name: new RegExp(`^Transcribe ${FIRE?.alias}`) }),
    ).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Record/ })).toBeInTheDocument();
  });
});

/** The query string, so a test can read what the page put in the URL. */
function Where() {
  return <output data-testid="where">{useLocation().search}</output>;
}

const where = (): string => screen.getByTestId("where").textContent ?? "";

/** The page, with the URL it produced rendered next to it. */
function renderDirectory(route = "/talkgroups"): void {
  renderWithProviders(
    <>
      <Talkgroups />
      <Where />
    </>,
    route,
  );
}

/**
 * Filtering and sorting the directory.
 *
 * Both are the server's — a page caps at 500 rows — so what these cases actually pin is
 * that the page asks the right question and says which one it is asking: the controls
 * write the API's own parameter names into the URL, a pasted URL comes back as the same
 * view, and every filter in force is on screen as something the reader can take off
 * again. A filter that is applied but invisible is a directory that looks broken.
 */
describe("Talkgroups filtering", () => {
  it("puts a column's filter in the URL under the API's own name", async () => {
    const user = userEvent.setup();
    renderDirectory();
    await screen.findByText(FIRE?.alias as string);

    await user.click(screen.getByRole("combobox", { name: "Filter by category" }));
    await user.click(await screen.findByRole("option", { name: /fire/i }));

    await waitFor(() => expect(where()).toContain("category=fire"));
    // …and it is the question being asked, not a note about one: the law channels go.
    await waitFor(() => {
      for (const talkgroup of TALKGROUPS.filter((row) => row.category === "law")) {
        expect(screen.queryByText(talkgroup.alias as string)).not.toBeInTheDocument();
      }
    });
  });

  it("tells 'off' apart from 'not asked' on a policy column", async () => {
    // The reason these are selects and not checkboxes: "which channels are *not*
    // transcribed" is the audit an operator actually runs, and a checkbox cannot ask it.
    await signIn();
    const user = userEvent.setup();
    renderDirectory();
    await screen.findByText(FIRE?.alias as string);

    await user.click(await screen.findByRole("combobox", { name: "Filter by Transcribe" }));
    await user.click(await screen.findByRole("option", { name: "off" }));

    await waitFor(() => expect(where()).toContain("transcribe=false"));
    await waitFor(() => expect(screen.queryAllByRole("row").length).toBeLessThan(TALKGROUPS.length));
  });

  it("comes back from a pasted URL with the filters in force and on screen", async () => {
    renderDirectory("/talkgroups?category=fire&export_openmhz=false");
    await screen.findByRole("heading", { name: "Talkgroups" });

    expect(await screen.findByRole("button", { name: "Remove filter Category: Fire" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove filter OpenMHz: off" })).toBeInTheDocument();
  });

  it("takes one filter back off from its chip and leaves the rest alone", async () => {
    const user = userEvent.setup();
    renderDirectory("/talkgroups?category=fire&q=52");
    await screen.findByRole("heading", { name: "Talkgroups" });

    await user.click(await screen.findByRole("button", { name: "Remove filter Category: Fire" }));

    await waitFor(() => expect(where()).not.toContain("category=fire"));
    expect(where()).toContain("q=52");
    expect(screen.getByRole("button", { name: "Remove filter Search: 52" })).toBeInTheDocument();
  });

  it("clears every filter at once without throwing away the sort", async () => {
    // Clearing the filters is "show me the whole directory", not "and forget which
    // column I was reading it by".
    const user = userEvent.setup();
    renderDirectory("/talkgroups?category=fire&q=52&sort=tgid");
    await screen.findByRole("heading", { name: "Talkgroups" });

    await user.click(await screen.findByRole("button", { name: "Clear all" }));

    await waitFor(() => expect(where()).toBe("?sort=tgid"));
    expect(screen.queryByRole("button", { name: /^Remove filter/ })).not.toBeInTheDocument();
  });

  it("removes a chip that came from a filter whose column this reader cannot see", async () => {
    // A guest handed a link filtered on a write-only column has no control for it in the
    // table — the chip is the only way back out, so it is not conditional on the column.
    const user = userEvent.setup();
    renderDirectory("/talkgroups?transcribe=false");
    await screen.findByRole("heading", { name: "Talkgroups" });

    expect(screen.queryByRole("combobox", { name: "Filter by Transcribe" })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Remove filter Transcribe: off" }));
    await waitFor(() => expect(where()).not.toContain("transcribe"));
  });

  it("sorts by any column the table shows, and turns it around on a second click", async () => {
    // Four of nineteen headers used to respond to a click. The rest are now the server's
    // sort keys, so the order is over the whole directory rather than over this page.
    const user = userEvent.setup();
    renderDirectory();
    await screen.findByText(FIRE?.alias as string);

    await user.click(screen.getByRole("button", { name: "Sort by Category" }));
    await waitFor(() => expect(where()).toContain("sort=-category"));
    await user.click(screen.getByRole("button", { name: "Sort by Category" }));
    await waitFor(() => expect(where()).toContain("sort=category"));

    const header = screen.getByRole("columnheader", { name: /Category/ });
    expect(header).toHaveAttribute("aria-sort", "ascending");
  });

  it("orders by the last 24 hours, which is a count no column holds", async () => {
    const user = userEvent.setup();
    renderDirectory();
    await screen.findByText(FIRE?.alias as string);

    await user.click(screen.getByRole("button", { name: "Sort by Last 24h" }));
    await waitFor(() => expect(where()).toContain("sort=-calls_24h"));
  });

  it("starts a filtered view at page 1", async () => {
    // Page 4 of the old question is not page 4 of the new one, and is usually past the
    // end of it — which showed as an empty table under a filter that had just matched.
    const user = userEvent.setup();
    renderDirectory("/talkgroups?page=3");
    await screen.findByRole("heading", { name: "Talkgroups" });

    // The "Show hidden" switch, because page 3 of a 22-channel directory has no table
    // to reach a column's filter through — which is the state this bug produced.
    await user.click(screen.getByRole("switch", { name: /Show hidden/ }));
    await waitFor(() => expect(where()).toContain("hidden=1"));
    expect(where()).not.toContain("page=");
  });

  it("offers an admin the way to the table the agency picker reads", async () => {
    await signIn();
    renderDirectory();
    const link = await screen.findByRole("link", { name: /Manage agencies/ });
    expect(link).toHaveAttribute("href", "/admin?tab=agencies");
  });
});
