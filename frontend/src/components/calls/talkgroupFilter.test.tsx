/**
 * `?talkgroup=` is the on-air tgid, and the SPA speaks it.
 *
 * The API used to accept a `talkgroups.id` as a second spelling and matched both readings
 * at once. The picker sent the primary key, and because one talkgroup's row id is very
 * often another talkgroup's tgid (43 such pairs on production), picking a channel quietly
 * pulled an unrelated one in with it. The parameter now means the tgid only, so every
 * place in the SPA that builds a calls filter has to send that number — these are the
 * three that used to send the row id.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiGet } from "../../api/client";
import { TALKGROUPS } from "../../api/mock/dataset";
import type { Call, Facets, Paginated } from "../../api/types";
import { TalkgroupDetail } from "../../pages/TalkgroupDetail";
import { defaultFilters } from "../../state/filters";
import { renderWithProviders } from "../../test/render";
import { FilterRail } from "./FilterRail";

/** A channel whose row id is not its tgid — which is every channel in the fixture. */
const CHANNEL = TALKGROUPS.find((tg) => tg.alias === "RC LAW 2") as (typeof TALKGROUPS)[number];

describe("the talkgroup filter speaks on-air tgids", () => {
  it("has the rail's picker write the tgid, not the row id", async () => {
    const user = userEvent.setup();
    const patch = vi.fn();
    renderWithProviders(
      <FilterRail filters={defaultFilters()} patch={patch} reset={vi.fn()} activeCount={0} />,
      "/calls",
    );

    await user.click(await screen.findByRole("button", { name: /^Talkgroups/ }));
    const option = await screen.findByRole("button", { name: new RegExp(`${CHANNEL.alias} `) });
    await user.click(option);

    expect(patch).toHaveBeenCalledWith({ talkgroup: [CHANNEL.tgid] });
    expect(CHANNEL.id).not.toBe(CHANNEL.tgid);
  });

  it("has the talkgroup page ask for its own tgid", async () => {
    // The route carries the row id; what goes out on the wire must not.
    renderWithProviders(<TalkgroupDetail />, `/talkgroups/${CHANNEL.id}`, "/talkgroups/:id");

    const link = await screen.findByRole("link", { name: "Filter the calls page" });
    expect(link).toHaveAttribute("href", `/calls?talkgroup=${CHANNEL.tgid}`);

    // And the list it renders is the channel's traffic, which it only gets by asking with
    // the tgid: the mock corpus matches `talkgroup=` against `talkgroup_ids` exactly as
    // Postgres does, so a row id would come back empty.
    await screen.findByText("Recent calls");
    await waitFor(() => {
      expect(screen.queryByText("Nothing recorded on this channel yet")).not.toBeInTheDocument();
      expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0);
    });
  });

  it("keys every facet bucket by the tgid, so clicking one reproduces its count", async () => {
    const facets = await apiGet<Facets>("/calls/facets");
    const bucket = facets.talkgroup[0] as Facets["talkgroup"][number];
    const tgid = Number(bucket.key);
    expect(TALKGROUPS.some((tg) => tg.tgid === tgid)).toBe(true);

    const page = await apiGet<Paginated<Call>>("/calls", { talkgroup: [tgid], page_size: 500 });
    expect(page.items.length).toBeGreaterThan(0);
    for (const call of page.items) expect(call.talkgroup_ids).toContain(tgid);
  });
});
