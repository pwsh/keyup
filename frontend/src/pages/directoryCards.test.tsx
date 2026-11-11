/**
 * Review 2026-10-29 #8: below `md` the four directories are cards (the key fields of
 * each row, like `CallCards`), not a 44–48 rem table scrolled sideways.
 */
import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiPost } from "../api/client";
import { PERSONNEL, RADIOS, TALKGROUPS, UNITS } from "../api/mock/dataset";
import { talkgroupLabel } from "../lib/format";
import { renderWithProviders } from "../test/render";
import { Personnel } from "./Personnel";
import { Radios } from "./Radios";
import { Talkgroups } from "./Talkgroups";
import { Units } from "./Units";

// See `uiReview.test.tsx`: the real hook caches its first answer per query.
vi.mock("../lib/useMediaQuery", () => ({
  useMediaQuery: (query: string) => {
    const min = /\(min-width:\s*(\d+)px\)/.exec(query);
    return min ? window.innerWidth >= Number(min[1]) : false;
  },
}));

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

describe("the directories on a phone", () => {
  beforeEach(() => {
    window.innerWidth = 390;
  });
  afterEach(() => {
    window.innerWidth = 1440;
  });

  it("shows Talkgroups as cards with the channel, its TGID and its traffic", async () => {
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const list = await screen.findByRole("list", { name: "Talkgroups" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const first = TALKGROUPS.find((talkgroup) => !talkgroup.is_hidden) as (typeof TALKGROUPS)[number];
    const link = within(list).getAllByRole("link", { name: talkgroupLabel(first) })[0] as HTMLElement;
    const card = link.closest("li") as HTMLElement;
    expect(within(card).getByText(String(first.tgid))).toBeInTheDocument();
    expect(within(card).getByText(/calls$/)).toBeInTheDocument();
    // A guest has nothing to edit, so no pencil and no star.
    expect(within(list).queryByRole("button", { name: /^Edit / })).not.toBeInTheDocument();
  });

  it("gives an operator the star and the pencil on each talkgroup card", async () => {
    await signIn();
    renderWithProviders(<Talkgroups />, "/talkgroups");
    const list = await screen.findByRole("list", { name: "Talkgroups" });
    expect((await within(list).findAllByRole("button", { name: /^Edit / })).length).toBeGreaterThan(0);
    expect(within(list).getAllByRole("button", { name: /favorites$/ }).length).toBeGreaterThan(0);
  });

  it("shows Radios as cards that link to each radio", async () => {
    renderWithProviders(<Radios />, "/radios");
    const list = await screen.findByRole("list", { name: "Radios" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const unit = RADIOS.find((radio) => radio.unit_id === "E1");
    const link = within(list).getByRole("link", { name: `Open ${unit?.unit_id}` });
    expect(within(link.closest("li") as HTMLElement).getByText(String(unit?.radio_id))).toBeInTheDocument();
  });

  it("shows Units as cards with callsign, name and radio count", async () => {
    await signIn();
    renderWithProviders(<Units />, "/units");
    const list = await screen.findByRole("list", { name: "Units" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const unit = UNITS[0] as (typeof UNITS)[number];
    const card = within(list).getByRole("link", { name: unit.callsign }).closest("li") as HTMLElement;
    if (unit.name) expect(within(card).getByText(unit.name)).toBeInTheDocument();
    expect(within(card).getByText(/radios?$/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: `Edit unit ${unit.callsign}` })).toBeInTheDocument();
  });

  it("shows Personnel as cards with name, rank and radio count", async () => {
    await signIn();
    renderWithProviders(<Personnel />, "/personnel");
    const list = await screen.findByRole("list", { name: "Personnel" });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    const person = PERSONNEL.find((row) => row.active && row.rank) as (typeof PERSONNEL)[number];
    const card = within(list).getByRole("link", { name: person.name }).closest("li") as HTMLElement;
    expect(within(card).getByText(person.rank as string)).toBeInTheDocument();
    expect(within(card).getByText(/radios?$/)).toBeInTheDocument();
  });

  it("keeps the tables from `md` up", async () => {
    window.innerWidth = 1024;
    renderWithProviders(<Radios />, "/radios");
    expect(await screen.findByRole("table")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Radios" })).not.toBeInTheDocument();
  });
});
