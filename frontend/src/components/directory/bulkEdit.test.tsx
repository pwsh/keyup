import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { apiPost } from "../../api/client";
import { PERSONNEL, TALKGROUPS } from "../../api/mock/dataset";
import { Personnel } from "../../pages/Personnel";
import { Talkgroups } from "../../pages/Talkgroups";
import { renderWithProviders } from "../../test/render";
import { BulkEditDrawer, BulkEditTalkgroupsDrawer, type BulkField, changesFrom } from "./BulkEditDrawer";
import { toggleInSet } from "./SelectionBar";

/** The checkboxes and the bar need write access, so the fixtures' operator signs in. */
async function signIn(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * The row checkboxes, without the header's.
 *
 * Both are named "Select …", and the header is first in the DOM — so a plain
 * `findAllByRole(/^Select /)[0]` is the "tick everything" box rather than the
 * first row, and a test written that way passes for the wrong reason.
 */
async function rowBoxes(): Promise<HTMLElement[]> {
  const all = await screen.findAllByRole("checkbox");
  return all.filter((box) => !/this page/.test(box.getAttribute("aria-label") ?? ""));
}

/**
 * Assert the selection bar's running count.
 *
 * Through `toHaveTextContent` rather than a text query, because the number sits
 * in its own `<span>` and `getByText` only sees an element's *direct* text nodes
 * — "4 people selected" is never one of them.
 */
async function expectSelectionCount(count: number, noun: string): Promise<void> {
  const line = await screen.findByText(new RegExp(`${noun} selected$`));
  expect(line).toHaveTextContent(`${count} ${noun} selected`);
}

/**
 * Editing forty channels at once.
 *
 * Three things have to hold, and each is a way the feature could quietly destroy
 * data instead of saving time:
 *
 * 1. The selection is the page's, and it dies when the question changes — a tick
 *    that outlives its filter is an edit aimed at rows nobody is looking at.
 * 2. The drawer sends *only* the fields moved off "leave unchanged". A form that
 *    posted its blank boxes would empty every column the reader never touched.
 * 3. A failure says what the server said, rather than "could not save".
 */
describe("the selection model", () => {
  it("ticks and unticks without disturbing the rest of the set", () => {
    let picked = toggleInSet(new Set([1, 2]), [3], true);
    expect([...picked].sort()).toEqual([1, 2, 3]);
    picked = toggleInSet(picked, [2], false);
    expect([...picked].sort()).toEqual([1, 3]);
    // Ticking a whole page adds the ones missing and leaves the ones already in.
    picked = toggleInSet(picked, [1, 2, 3, 4], true);
    expect([...picked].sort()).toEqual([1, 2, 3, 4]);
  });

  it("is owned by the page, so it survives scrolling and shows a running count", async () => {
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const first = TALKGROUPS.find((tg) => !tg.is_hidden);
    expect(first).toBeDefined();
    const boxes = await rowBoxes();
    await user.click(boxes[0] as HTMLElement);
    await expectSelectionCount(1, "talkgroups");

    await user.click(boxes[1] as HTMLElement);
    await expectSelectionCount(2, "talkgroups");
  });

  it("marks the header box indeterminate while only some of the page is ticked", async () => {
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const header = await screen.findByRole("checkbox", { name: /every row on this page/ });
    expect((header as HTMLInputElement).indeterminate).toBe(false);

    const boxes = await rowBoxes();
    await user.click(boxes[0] as HTMLElement);
    await waitFor(() => expect((header as HTMLInputElement).indeterminate).toBe(true));
    expect((header as HTMLInputElement).checked).toBe(false);

    // Ticking the header takes the whole page, and the box stops being ambiguous.
    await user.click(header);
    await waitFor(() => expect((header as HTMLInputElement).checked).toBe(true));
    expect((header as HTMLInputElement).indeterminate).toBe(false);
  });

  it("drops the selection when the filters change", async () => {
    // A tick belongs to the question that produced it. Keeping it across a new
    // search would leave "Edit 12" pointing at rows that are no longer on screen.
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const boxes = await rowBoxes();
    await user.click(boxes[0] as HTMLElement);
    await expectSelectionCount(1, "talkgroups");

    await user.click(screen.getByRole("switch", { name: /Show hidden/ }));
    await waitFor(() => expect(screen.queryByText(/talkgroups selected/)).not.toBeInTheDocument());
  });

  it("clears everything from the bar", async () => {
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<Talkgroups />, "/talkgroups");

    const header = await screen.findByRole("checkbox", { name: /every row on this page/ });
    await user.click(header);
    expect(await screen.findByText(/talkgroups selected/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Clear/ }));
    await waitFor(() => expect(screen.queryByText(/talkgroups selected/)).not.toBeInTheDocument());
  });
});

/* --------------------------------------------------------------- the drawer */

const FIELDS: BulkField[] = [
  { key: "rank", label: "Rank", help: "roster.personnel.edit.rank", kind: "text" },
  {
    key: "active",
    label: "On roster",
    help: "roster.personnel.edit.active",
    kind: "switch",
    clearable: false,
  },
  {
    key: "kind",
    label: "Kind",
    help: "roster.units.edit.kind",
    kind: "select",
    clearable: false,
    options: [
      { value: "vehicle", label: "vehicle" },
      { value: "station", label: "station" },
    ],
  },
];

describe("changesFrom", () => {
  it("sends nothing for a field left unchanged", () => {
    const state = {
      rank: { mode: "keep" as const, value: "Captain", on: false },
      active: { mode: "keep" as const, value: "", on: true },
      kind: { mode: "keep" as const, value: "station", on: false },
    };
    expect(changesFrom(FIELDS, state)).toEqual({});
  });

  it("sends a null for a field explicitly cleared, which is not the same as unchanged", () => {
    const state = {
      rank: { mode: "clear" as const, value: "Captain", on: false },
      active: { mode: "keep" as const, value: "", on: false },
      kind: { mode: "keep" as const, value: "vehicle", on: false },
    };
    // The distinction the whole tri-state exists for: `{}` writes nothing,
    // `{rank: null}` empties the column on every selected row.
    expect(changesFrom(FIELDS, state)).toEqual({ rank: null });
  });

  it("sends only the fields actually chosen", () => {
    const state = {
      rank: { mode: "set" as const, value: "Captain", on: false },
      active: { mode: "set" as const, value: "", on: false },
      kind: { mode: "keep" as const, value: "station", on: false },
    };
    expect(changesFrom(FIELDS, state)).toEqual({ rank: "Captain", active: false });
  });

  /**
   * Agency, category and service tag: the three an operator actually retags in bulk.
   *
   * Each goes up a different way — the agency picker's "— none —" and Clear are the
   * same request, a select sends its value as the string the API's enum wants — and
   * each has a Clear, because all three columns are nullable and "this channel has no
   * agency" is a thing an operator means.
   */
  it("sends the three retag fields as the API spells them", () => {
    const fields: BulkField[] = [
      { key: "agency_code", label: "Agency", help: "talkgroups.edit.agency", kind: "agency" },
      {
        key: "category",
        label: "Category",
        help: "talkgroups.edit.category",
        kind: "select",
        options: [
          { value: "fire", label: "Fire" },
          { value: "law", label: "Law enforcement" },
        ],
      },
      {
        key: "service_tag",
        label: "Service tag",
        help: "talkgroups.edit.serviceTag",
        kind: "select",
        options: [{ value: "Fire Dispatch", label: "Fire Dispatch" }],
      },
    ];

    expect(
      changesFrom(fields, {
        agency_code: { mode: "set", value: "EXAMPLEFD", on: false },
        category: { mode: "set", value: "fire", on: false },
        service_tag: { mode: "set", value: "Fire Dispatch", on: false },
      }),
    ).toEqual({ agency_code: "EXAMPLEFD", category: "fire", service_tag: "Fire Dispatch" });

    // Clear on each is a null, which is what empties the column — not an omission,
    // which would leave whatever was there.
    expect(
      changesFrom(fields, {
        agency_code: { mode: "clear", value: "EXAMPLEFD", on: false },
        category: { mode: "clear", value: "fire", on: false },
        service_tag: { mode: "clear", value: "Fire Dispatch", on: false },
      }),
    ).toEqual({ agency_code: null, category: null, service_tag: null });

    // The agency picker's own "nothing selected" is the same request as Clear, so a
    // reader who chose "Set to… — none —" does not silently send an empty string.
    expect(
      changesFrom(fields, {
        agency_code: { mode: "set", value: "", on: false },
        category: { mode: "keep", value: "fire", on: false },
        service_tag: { mode: "keep", value: "", on: false },
      }),
    ).toEqual({ agency_code: null });
  });

  it("treats a blank number as unchosen rather than sending NaN", () => {
    const fields: BulkField[] = [
      { key: "priority", label: "Priority", help: "talkgroups.edit.priority", kind: "number" },
    ];
    expect(changesFrom(fields, { priority: { mode: "set", value: "", on: false } })).toEqual({});
    expect(changesFrom(fields, { priority: { mode: "set", value: "7", on: false } })).toEqual({
      priority: 7,
    });
  });
});

describe("BulkEditDrawer", () => {
  it("keeps Apply dead until something is actually chosen", async () => {
    const user = userEvent.setup();
    const submit = vi.fn(() => Promise.resolve());
    renderWithProviders(
      <BulkEditDrawer
        open
        onOpenChange={vi.fn()}
        ids={[1, 2, 3]}
        noun="people"
        title="Edit selected people"
        fields={FIELDS}
        submit={submit}
      />,
    );

    const apply = screen.getByRole("button", { name: /Apply to 3/ });
    expect(apply).toBeDisabled();
    expect(screen.getByText(/Nothing chosen yet/)).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "What to do with Rank" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.type(screen.getByRole("textbox", { name: "New rank" }), "Captain");

    await waitFor(() => expect(screen.getByRole("button", { name: /Apply to 3/ })).toBeEnabled());
  });

  it("posts one patch carrying only the chosen field", async () => {
    const user = userEvent.setup();
    // Typed, so the recorded call can be inspected key by key below.
    const submit = vi.fn((changes: Record<string, unknown>) => Promise.resolve(changes));
    renderWithProviders(
      <BulkEditDrawer
        open
        onOpenChange={vi.fn()}
        ids={[1, 2]}
        noun="people"
        title="Edit selected people"
        fields={FIELDS}
        submit={submit}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "What to do with Rank" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.type(screen.getByRole("textbox", { name: "New rank" }), "Captain");
    await user.click(screen.getByRole("button", { name: /Apply to 2/ }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({ rank: "Captain" }));
    // The two fields nobody touched are not in the body at all.
    expect(Object.keys(submit.mock.calls[0]?.[0] ?? {})).toEqual(["rank"]);
  });

  it("offers Clear only where a column can actually hold nothing", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <BulkEditDrawer
        open
        onOpenChange={vi.fn()}
        ids={[1]}
        noun="people"
        title="Edit selected people"
        fields={FIELDS}
        submit={vi.fn(() => Promise.resolve())}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "What to do with Rank" }));
    expect(within(screen.getByRole("listbox")).getByRole("option", { name: "Clear" })).toBeInTheDocument();
    await user.keyboard("{Escape}");

    // A switch is on or off; "clear" would have no meaning the database could store.
    await user.click(screen.getByRole("combobox", { name: "What to do with On roster" }));
    expect(within(screen.getByRole("listbox")).queryByRole("option", { name: "Clear" })).toBeNull();
  });

  it("starts every opening from 'change nothing'", async () => {
    // The second bulk edit of a session is the dangerous one if the form remembers.
    const user = userEvent.setup();
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setOpen((current) => !current)}>
            toggle
          </button>
          <BulkEditDrawer
            open={open}
            onOpenChange={setOpen}
            ids={[1]}
            noun="people"
            title="Edit selected people"
            fields={FIELDS}
            submit={vi.fn(() => Promise.resolve())}
          />
        </>
      );
    }
    renderWithProviders(<Harness />);

    await user.click(screen.getByRole("combobox", { name: "What to do with Rank" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.type(screen.getByRole("textbox", { name: "New rank" }), "Captain");
    expect(screen.getByText(/1 setting will be written/)).toBeInTheDocument();

    // Close it the way a reader would — the drawer is modal, so nothing outside it
    // is reachable until it is shut.
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText(/Nothing chosen yet/)).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "toggle" }));
    expect(await screen.findByText(/Nothing chosen yet/)).toBeInTheDocument();
  });
});

/* ------------------------------------------------------- end to end on a page */

describe("bulk editing from a page", () => {
  it("sends the endpoint one request for the whole selection", async () => {
    await signIn();
    const post = vi.spyOn(client, "apiPost");
    const user = userEvent.setup();
    renderWithProviders(<Personnel />, "/personnel");

    const header = await screen.findByRole("checkbox", { name: /every row on this page/ });
    await user.click(header);
    const active = PERSONNEL.filter((person) => person.active).length;
    await expectSelectionCount(active, "people");

    await user.click(screen.getByRole("button", { name: new RegExp(`Edit ${active}`) }));
    await user.click(await screen.findByRole("combobox", { name: "What to do with Rank" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.type(screen.getByRole("textbox", { name: "New rank" }), "Captain");
    await user.click(screen.getByRole("button", { name: new RegExp(`Apply to ${active}`) }));

    await waitFor(() => {
      const call = post.mock.calls.find(([path]) => path === "/personnel/bulk");
      expect(call).toBeDefined();
      const body = (call ?? [])[1] as { ids: number[]; changes: unknown };
      expect(body).toEqual({ ids: expect.any(Array), changes: { rank: "Captain" } });
      // One request for the whole selection, not one per row.
      expect(body.ids).toHaveLength(active);
      expect(post.mock.calls.filter(([path]) => path === "/personnel/bulk")).toHaveLength(1);
    });
    post.mockRestore();
  });

  it("says what the server said when the edit is refused", async () => {
    const user = userEvent.setup();
    const submit = vi.fn(() =>
      Promise.reject(new client.ApiError("422", 422, "/units/bulk", "callsign is per-row")),
    );
    renderWithProviders(
      <BulkEditDrawer
        open
        onOpenChange={vi.fn()}
        ids={[1, 2]}
        noun="units"
        title="Edit selected units"
        fields={FIELDS}
        submit={submit}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "What to do with Rank" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.type(screen.getByRole("textbox", { name: "New rank" }), "Captain");
    await user.click(screen.getByRole("button", { name: /Apply to 2/ }));

    const { useToastStore } = await import("../../state/toasts");
    await waitFor(() => {
      const toasts = useToastStore.getState().toasts;
      expect(toasts.some((entry) => entry.body === "callsign is per-row")).toBe(true);
    });
  });
});

/* ------------------------------------------------- the talkgroup retag fields */

/**
 * Agency, category and service tag through the real talkgroups drawer.
 *
 * `changesFrom` above proves the payload shape; this proves the *spec* — that the
 * three are actually offered, that their options come from the server's vocabulary
 * rather than a hand-kept list (so a category reads "Law enforcement", not `law`),
 * and that the one combination worth a second look gets one.
 */
describe("BulkEditTalkgroupsDrawer", () => {
  /** Move one field off "leave unchanged" and pick a value for it. */
  async function choose(user: ReturnType<typeof userEvent.setup>, field: string, option: string) {
    await user.click(await screen.findByRole("combobox", { name: `What to do with ${field}` }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.click(await screen.findByRole("combobox", { name: `New ${field.toLowerCase()}` }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: option }));
  }

  it("offers the three retag fields and labels the categories from the vocabulary", async () => {
    await signIn();
    renderWithProviders(<BulkEditTalkgroupsDrawer open onOpenChange={vi.fn()} ids={[1, 2]} />);

    for (const field of ["Agency", "Category", "Service tag"]) {
      expect(await screen.findByRole("combobox", { name: `What to do with ${field}` })).toBeInTheDocument();
    }

    const user = userEvent.setup();
    await user.click(screen.getByRole("combobox", { name: "What to do with Category" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Set to…" }));
    await user.click(await screen.findByRole("combobox", { name: "New category" }));
    // The server's prose label, not the wire key — the same word the filter row shows.
    await waitFor(() =>
      expect(
        within(screen.getByRole("listbox")).getByRole("option", { name: "Law enforcement" }),
      ).toBeInTheDocument(),
    );
  });

  it("posts the three as the patch model spells them", async () => {
    await signIn();
    const post = vi.spyOn(client, "apiPost");
    const user = userEvent.setup();
    renderWithProviders(<BulkEditTalkgroupsDrawer open onOpenChange={vi.fn()} ids={[1, 2]} />);

    await choose(user, "Category", "Fire");
    await choose(user, "Service tag", "Fire Dispatch");
    await user.click(screen.getByRole("button", { name: /Apply to 2/ }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/talkgroups/bulk", {
        ids: [1, 2],
        changes: { category: "fire", service_tag: "Fire Dispatch" },
      }),
    );
    post.mockRestore();
  });

  it("clears an agency with a null rather than an empty string", async () => {
    await signIn();
    const post = vi.spyOn(client, "apiPost");
    const user = userEvent.setup();
    renderWithProviders(<BulkEditTalkgroupsDrawer open onOpenChange={vi.fn()} ids={[7]} />);

    await user.click(await screen.findByRole("combobox", { name: "What to do with Agency" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "Clear" }));
    await user.click(screen.getByRole("button", { name: /Apply to 1/ }));

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/talkgroups/bulk", {
        ids: [7],
        changes: { agency_code: null },
      }),
    );
    post.mockRestore();
  });

  it("says so when the service tag belongs to another category, without blocking it", async () => {
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<BulkEditTalkgroupsDrawer open onOpenChange={vi.fn()} ids={[1, 2]} />);

    await choose(user, "Category", "Law enforcement");
    await choose(user, "Service tag", "Fire Dispatch");

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/RadioReference files/);
    expect(notice).toHaveTextContent(/Fire Dispatch/);
    // A soft warning: Apply is still live, because a fire channel filed under law is
    // a real arrangement and the operator is the one who knows.
    expect(screen.getByRole("button", { name: /Apply to 2/ })).toBeEnabled();
  });

  it("says nothing when the two agree", async () => {
    await signIn();
    const user = userEvent.setup();
    renderWithProviders(<BulkEditTalkgroupsDrawer open onOpenChange={vi.fn()} ids={[1]} />);

    await choose(user, "Category", "Fire");
    await choose(user, "Service tag", "Fire Dispatch");

    await waitFor(() => expect(screen.getByRole("button", { name: /Apply to 1/ })).toBeEnabled());
    expect(screen.queryByRole("status")).toBeNull();
  });
});
