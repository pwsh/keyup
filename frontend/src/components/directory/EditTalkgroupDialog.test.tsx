import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { apiPost } from "../../api/client";
import { TALKGROUPS } from "../../api/mock/dataset";
import type { Talkgroup } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { EditTalkgroupDialog } from "./EditTalkgroupDialog";

const talkgroup = TALKGROUPS[0] as Talkgroup;

describe("EditTalkgroupDialog", () => {
  it("offers RadioReference's tags, and 'none' as a real choice", async () => {
    // Opening the list is the point: the blank entry carries a named sentinel
    // rather than the empty string a Radix select keeps for "nothing selected",
    // and only rendering the list proves the option is really there to pick.
    const user = userEvent.setup();
    renderWithProviders(<EditTalkgroupDialog talkgroup={talkgroup} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("combobox", { name: "Service tag" }));
    const options = screen.getByRole("listbox");
    expect(within(options).getByRole("option", { name: "— none —" })).toBeInTheDocument();
    expect(within(options).getByRole("option", { name: "Fire Dispatch" })).toBeInTheDocument();
  });

  it("offers every category the app knows how to draw", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EditTalkgroupDialog talkgroup={talkgroup} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("combobox", { name: "Category" }));
    const options = screen.getByRole("listbox");
    expect(within(options).getByRole("option", { name: "Corrections" })).toBeInTheDocument();
    expect(within(options).getByRole("option", { name: "Schools" })).toBeInTheDocument();
  });

  it("sends the shared flag with the rest of the patch", async () => {
    // "Shared" is the one field on this dialog nobody in the building needs — it exists
    // so an outside consumer can discover the county channels instead of hardcoding
    // fourteen tgids. That makes it exactly the field most likely to be added to the form
    // and forgotten in the request body, so the assertion is on what was sent.
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    const owned = TALKGROUPS.find((tg) => !tg.shared) as Talkgroup;
    renderWithProviders(<EditTalkgroupDialog talkgroup={owned} open onOpenChange={vi.fn()} />);

    await user.click(screen.getByRole("switch", { name: "Shared county channel" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = patch.mock.calls.find(([path]) => path === `/talkgroups/${owned.id}`);
      expect(call).toBeDefined();
      expect((call ?? [])[1]).toMatchObject({ shared: true, alias: owned.alias });
    });
    patch.mockRestore();
  });

  it("assigns an audio profile as the row id the API wants, not the string in the DOM", async () => {
    // A select's value is a string, and `audio_profile_id: "2"` is a 422 nobody can act
    // on — so the assertion is on the type as well as the number.
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    const plain = TALKGROUPS.find((tg) => tg.audio_profile_id === null) as Talkgroup;
    renderWithProviders(<EditTalkgroupDialog talkgroup={plain} open onOpenChange={vi.fn()} />);

    await user.click(await screen.findByRole("combobox", { name: "Audio profile" }));
    const options = screen.getByRole("listbox");
    expect(within(options).getByRole("option", { name: "— none —" })).toBeInTheDocument();
    await user.click(within(options).getByRole("option", { name: "Aircraft cabin" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = patch.mock.calls.find(([path]) => path === `/talkgroups/${plain.id}`);
      expect(call).toBeDefined();
      expect((call ?? [])[1]).toMatchObject({ audio_profile_id: 2 });
    });
    patch.mockRestore();
  });

  it("clears a channel back to the global chain with a null, not an omitted key", async () => {
    // "— none —" is a real choice, not just an initial state: a channel put on the wrong
    // profile has to be able to come off it, and only an explicit null does that.
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    const assigned = TALKGROUPS.find((tg) => tg.audio_profile_id !== null) as Talkgroup;
    renderWithProviders(<EditTalkgroupDialog talkgroup={assigned} open onOpenChange={vi.fn()} />);

    await user.click(await screen.findByRole("combobox", { name: "Audio profile" }));
    await user.click(within(screen.getByRole("listbox")).getByRole("option", { name: "— none —" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const call = patch.mock.calls.find(([path]) => path === `/talkgroups/${assigned.id}`);
      expect(call).toBeDefined();
      expect((call ?? [])[1]).toMatchObject({ audio_profile_id: null });
    });
    patch.mockRestore();
  });
});
