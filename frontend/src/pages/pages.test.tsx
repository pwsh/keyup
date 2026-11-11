import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { apiPost } from "../api/client";
import { CALLS, PERSONNEL, RADIOS, SOURCE_QUALITY_CALL_ID, TALKGROUPS, UNITS } from "../api/mock/dataset";
import { renderWithProviders } from "../test/render";
import { Activity } from "./Activity";
import { Admin } from "./Admin";
import { Alerts } from "./Alerts";
import { CallDetail, SourceFact } from "./CallDetail";
import { Help } from "./Help";
import { Live } from "./Live";
import { Login } from "./Login";
import { PersonDetail } from "./PersonDetail";
import { Personnel } from "./Personnel";
import { RadioDetail } from "./RadioDetail";
import { Radios } from "./Radios";
import { SearchPage } from "./SearchPage";
import { Sites } from "./Sites";
import { Stats } from "./Stats";
import { TalkgroupDetail } from "./TalkgroupDetail";
import { Talkgroups } from "./Talkgroups";
import { UnitDetail } from "./UnitDetail";
import { Units } from "./Units";

/**
 * Every page, rendered against the fake corpus. These are smoke tests on purpose:
 * a page that throws on mount, asks for a hook it does not have, or renders no
 * heading at all is a whole route nobody can reach, and that is worth catching
 * before any assertion about its contents is.
 */
describe("every page mounts against the mock API", () => {
  it("renders Search", async () => {
    renderWithProviders(<SearchPage />, "/search?q=fire");
    expect(await screen.findByRole("heading", { name: "Search" })).toBeInTheDocument();
    await waitFor(() => expect(document.querySelectorAll("mark").length).toBeGreaterThan(0));
  });

  it("renders Live", async () => {
    renderWithProviders(<Live />, "/live");
    expect(await screen.findByRole("heading", { name: "Live" })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Auto-play/ })).toBeInTheDocument();
  });

  it("renders Talkgroups with a row per channel", async () => {
    // The alias is now an inline edit rather than the row's link — and this render
    // is signed out, so it is plain text — while the link beside it opens the
    // channel. Both halves matter: the name has to be readable without an account.
    renderWithProviders(<Talkgroups />, "/talkgroups");
    expect(await screen.findByRole("heading", { name: "Talkgroups" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open EC FIRE DISPATCH" })).toBeInTheDocument();
    expect(screen.getByText("EC FIRE DISPATCH")).toBeInTheDocument();
  });

  it("renders a talkgroup's detail with its policies, once signed in", async () => {
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const talkgroup = TALKGROUPS[0];
    renderWithProviders(<TalkgroupDetail />, `/talkgroups/${talkgroup?.id}`, "/talkgroups/:id");
    expect(await screen.findByRole("heading", { name: talkgroup?.alias as string })).toBeInTheDocument();
    expect(await screen.findByText("Priority")).toBeInTheDocument();
    expect(screen.getByText("Transcription")).toBeInTheDocument();
  });

  it("keeps queue priority and retention from a guest on a talkgroup", async () => {
    const talkgroup = TALKGROUPS[0];
    renderWithProviders(<TalkgroupDetail />, `/talkgroups/${talkgroup?.id}`, "/talkgroups/:id");
    expect(await screen.findByRole("heading", { name: talkgroup?.alias as string })).toBeInTheDocument();
    expect(screen.getByText("Category")).toBeInTheDocument();
    expect(screen.queryByText("Priority")).not.toBeInTheDocument();
    expect(screen.queryByText("Retention")).not.toBeInTheDocument();
  });

  it("renders Radios with a row per radio", async () => {
    renderWithProviders(<Radios />, "/radios");
    expect(await screen.findByRole("heading", { name: "Radios" })).toBeInTheDocument();
    const unit = RADIOS.find((radio) => radio.unit_id === "E1");
    expect(await screen.findByRole("link", { name: `Open ${unit?.unit_id}` })).toBeInTheDocument();
  });

  it("renders a radio's detail with the channels it is heard on", async () => {
    const radio = RADIOS[0];
    renderWithProviders(<RadioDetail />, `/radios/${radio?.id}`, "/radios/:id");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(
      radio?.unit_id ?? (radio?.alias as string),
    );
    expect(await screen.findByRole("heading", { name: "Heard on" })).toBeInTheDocument();
  });

  it("renders a radio's assignment card with who holds it", async () => {
    // The point of the card is that a radio has two independent holders — the unit it
    // lives on and the person carrying it — so both have to be on the page.
    // Signed in, because the unit and person are links to pages a guest cannot open —
    // to a guest the same two names are there as plain text (see the read-only cases).
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    const radio = RADIOS[0];
    renderWithProviders(<RadioDetail />, `/radios/${radio?.id}`, "/radios/:id");
    expect(await screen.findByRole("heading", { name: "Assignment" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /RC FIRE ENGINE 1/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: PERSONNEL[0]?.name as string })).toBeInTheDocument();
  });

  it("renders Units with a row per callsign", async () => {
    renderWithProviders(<Units />, "/units");
    expect(await screen.findByRole("heading", { name: "Units" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "E1" })).toBeInTheDocument();
  });

  it("renders a unit's detail with the radios assigned to it", async () => {
    const unit = UNITS[0];
    renderWithProviders(<UnitDetail />, `/units/${unit?.id}`, "/units/:id");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(unit?.callsign as string);
    expect(await screen.findByRole("heading", { name: "Radios assigned" })).toBeInTheDocument();
  });

  it("renders Personnel with a row per person", async () => {
    renderWithProviders(<Personnel />, "/personnel");
    expect(await screen.findByRole("heading", { name: "Personnel" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: `Open ${PERSONNEL[0]?.name}` })).toBeInTheDocument();
  });

  it("renders a person's detail with what they are carrying", async () => {
    const person = PERSONNEL[0];
    renderWithProviders(<PersonDetail />, `/personnel/${person?.id}`, "/personnel/:id");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(person?.name as string);
    expect(await screen.findByRole("heading", { name: "Radios assigned" })).toBeInTheDocument();
  });

  it("renders the channel audit for a site, with the drift and the copy box", async () => {
    // The audit is a comparison, so the page is only useful if it shows both sides:
    // the control channel that is live and the one RadioReference documents.
    renderWithProviders(<Sites />, "/sites?site=1");
    expect(await screen.findByRole("heading", { name: "Sites" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Control channel" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Undocumented (2)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Missing (2)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Adjacent sites (2)" })).toBeInTheDocument();
    // 851.7875 is live; 851.0250 is what RadioReference calls the primary.
    expect(screen.getAllByText("851.7875 MHz").length).toBeGreaterThan(0);
    expect(screen.getAllByText("851.0250 MHz").length).toBeGreaterThan(0);
    expect(screen.getByText("851.7875, 851.0250")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy the suggested control frequencies" })).toBeEnabled();
  });

  it("renders a call's detail with its transcript and its exchange", async () => {
    const call = CALLS.find((entry) => entry.transcript_status === "done");
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    expect(await screen.findByRole("heading", { name: "Transcript" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "This exchange" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Playback position" })).toBeInTheDocument();
  });

  it("lists who keyed up on a trunk-recorder call, with each offset", async () => {
    // A trunked call is a short conversation; the sidecar says who spoke when, and the
    // point of showing it is being able to jump to "the second unit".
    const call = CALLS.find((entry) => entry.ingest_source === "trunk_recorder" && entry.sources.length > 1);
    expect(call, "the mock corpus should contain a multi-source trunk-recorder call").toBeDefined();
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");

    expect(await screen.findByRole("heading", { name: "Who keyed up" })).toBeInTheDocument();

    // One row per source, in order, each named by the offset it seeks to. Anchored,
    // because "1.4s" is also a substring of the row above's "0.0s … 1.4s" length.
    const list = within(screen.getByRole("list", { name: "Radios that keyed up" }));
    const offsets = (call?.sources ?? []).map((source) => source.pos_s.toFixed(1));
    for (const offset of offsets) {
      expect(list.getByRole("button", { name: new RegExp(`^${offset}s`) })).toBeInTheDocument();
    }
  });

  it("asks for no waveform when the call's recording is not there", async () => {
    // A purged/discarded/missing recording answers 410 from the waveform route, so the
    // page must not ask at all — the row's `audio_state` already says so.
    const call = CALLS.find((entry) => entry.audio_state === "purged");
    expect(call, "the mock corpus should contain a call without audio").toBeDefined();
    const get = vi.spyOn(client, "apiGet");
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    await screen.findByRole("slider", { name: "Playback position" });

    const asked = get.mock.calls.map(([path]) => path);
    expect(asked).toContain(`/calls/${call?.id}`);
    expect(asked).not.toContain(`/calls/${call?.id}/waveform`);
    get.mockRestore();
  });

  it("still asks for the waveform when the recording is on hand", async () => {
    const call = CALLS.find((entry) => entry.audio_state === "present");
    const get = vi.spyOn(client, "apiGet");
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    await waitFor(() =>
      expect(get.mock.calls.map(([path]) => path)).toContain(`/calls/${call?.id}/waveform`),
    );
    get.mockRestore();
  });

  it("says which receiver recorded the call, when the recorder said, once signed in", async () => {
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    renderWithProviders(<CallDetail />, `/calls/${SOURCE_QUALITY_CALL_ID}`, "/calls/:id");
    expect(await screen.findByText("Recorded by trunkd-central-02")).toBeInTheDocument();
    expect(screen.getByText("1,234 voice frames")).toBeInTheDocument();
    expect(screen.getByText("7 bad frames")).toBeInTheDocument();
    expect(screen.getByText("Signal -38.4 dBFS")).toBeInTheDocument();
  });

  it("keeps the recorder, time source and audio chain from a guest", async () => {
    renderWithProviders(<CallDetail />, `/calls/${SOURCE_QUALITY_CALL_ID}`, "/calls/:id");
    await screen.findByRole("tab", { name: "Raw metadata" });
    expect(screen.queryByText(/Recorded by/)).not.toBeInTheDocument();
    expect(screen.queryByText("Time source")).not.toBeInTheDocument();
    expect(screen.queryByText("Audio profile")).not.toBeInTheDocument();
  });

  it("says nothing at all about the source when no recorder said", async () => {
    const call = CALLS.find((entry) => entry.id !== SOURCE_QUALITY_CALL_ID);
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    await screen.findByRole("tab", { name: "Raw metadata" });
    expect(screen.queryByText(/Recorded by/)).not.toBeInTheDocument();
  });

  it("renders the source line from whatever the quality report happens to carry", () => {
    // A recorder that counts only good frames sends no bad-frame count, and a line that
    // invented "0 bad" would be reporting a number nobody measured.
    renderWithProviders(<SourceFact instance="rec-01" quality={{ voice_frames: 90 }} />);
    expect(screen.getByText("Recorded by rec-01")).toBeInTheDocument();
    expect(screen.getByText("90 voice frames")).toBeInTheDocument();
    expect(screen.queryByText(/bad frames/)).not.toBeInTheDocument();
  });

  it("shows the recorder's own metadata for either dialect", async () => {
    const call = CALLS.find((entry) => entry.ingest_source === "trunk_recorder");
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    await screen.findByRole("tab", { name: "Raw metadata" });
    (await screen.findByRole("tab", { name: "Raw metadata" })).click();
    // trunk-recorder writes a JSON sidecar, not ID3 frames, so the panel says so
    expect(await screen.findByText(/Sidecar metadata/)).toBeInTheDocument();
    expect(await screen.findByText("talkgroup_group_tag")).toBeInTheDocument();
  });

  it("still calls an sdrtrunk call's metadata ID3 frames", async () => {
    const call = CALLS.find((entry) => entry.ingest_source === "file");
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    (await screen.findByRole("tab", { name: "Raw metadata" })).click();
    expect(await screen.findByText(/ID3 frames/)).toBeInTheDocument();
    // the panel used to be permanently empty; raw_meta is where its contents come from
    expect(await screen.findByText("TIT2")).toBeInTheDocument();
  });

  it("renders Statistics with its headline tiles", async () => {
    renderWithProviders(<Stats />, "/stats");
    expect(await screen.findByRole("heading", { name: "Statistics" })).toBeInTheDocument();
    expect(await screen.findByText("Calls today")).toBeInTheDocument();
    expect(screen.getByText("Transcript coverage")).toBeInTheDocument();
    expect(screen.getByText("Ingest lag")).toBeInTheDocument();
  });

  it("renders Alerts with the seeded rules", async () => {
    renderWithProviders(<Alerts />, "/alerts");
    expect(await screen.findByRole("heading", { name: "Alerts" })).toBeInTheDocument();
    // The rule appears in the table and again in the notification log.
    expect((await screen.findAllByText("Structure fire keyword")).length).toBeGreaterThan(0);
  });

  it("renders Admin, defaulting to the ingest tab", async () => {
    renderWithProviders(<Admin />, "/admin");
    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Ingest" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "Watcher" })).toBeInTheDocument();
  });

  it("opens the Admin tab named in the URL", async () => {
    renderWithProviders(<Admin />, "/admin?tab=tokens");
    expect(await screen.findByRole("heading", { name: "API tokens" })).toBeInTheDocument();
    expect(await screen.findByText("sdrtrunk recorder")).toBeInTheDocument();
  });

  it("still opens the Agencies tab for a ?tab=directory link", async () => {
    // The tab was called "Directory" until agencies got one of their own, and that
    // spelling is in saved links and older help text; landing on Ingest instead
    // would read as the page having lost the agency table.
    renderWithProviders(<Admin />, "/admin?tab=directory");
    expect(await screen.findByRole("tab", { name: "Agencies" })).toHaveAttribute("aria-selected", "true");
  });

  it("renders Activity with the backfilled log", async () => {
    renderWithProviders(<Activity />, "/activity");
    expect(await screen.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(await screen.findByText(/accepted/)).toBeInTheDocument();
  });

  it("renders Login", async () => {
    renderWithProviders(<Login />, "/login");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
  });
});

describe("the Help page", () => {
  it("renders the whole registry, grouped by area", async () => {
    renderWithProviders(<Help />, "/help");
    expect(await screen.findByRole("heading", { name: "Help" })).toBeInTheDocument();
    for (const area of ["Calls", "Player", "Live", "Admin"]) {
      expect(screen.getByRole("heading", { name: area })).toBeInTheDocument();
    }
    // One <dt>/<dd> pair per registry entry.
    expect(document.querySelectorAll("dd").length).toBeGreaterThan(200);
  });

  it("explains the three talkgroup fields from the API's own vocabulary", async () => {
    // The registry answers "what does this control do"; this section answers
    // "what do I put in it", which is the question people arrive here with.
    renderWithProviders(<Help />, "/help");

    expect(await screen.findByRole("heading", { name: "Talkgroup fields" })).toBeInTheDocument();
    for (const heading of ["Agencies", "Categories", "Service tags"]) {
      expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    }
    // Every RadioReference tag is listed with the category it maps to.
    expect(await screen.findByRole("rowheader", { name: "Fire Dispatch" })).toBeInTheDocument();
    expect(document.querySelector("#help-categories")).not.toBeNull();
    expect(document.querySelector("#help-service-tags")).not.toBeNull();
    expect(document.querySelector("#help-agencies")).not.toBeNull();
  });
});

/**
 * What is left of a page when the reader can change nothing.
 *
 * The rule is the same everywhere: a column or a card that is only a control is not shown
 * greyed out, it is not shown — and nothing behind it is fetched, so a guest's screen
 * never asks a question the server would answer with a 401.
 */
describe("the read-only viewer's pages", () => {
  const signInAs = async (): Promise<void> => {
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
  };

  it("gives Radios no checkbox, pencil, picker or import", async () => {
    renderWithProviders(<Radios />, "/radios");
    await screen.findByRole("heading", { name: "Radios" });
    await screen.findByText(RADIOS[0]?.alias ?? RADIOS[0]?.unit_id ?? "");

    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /^Edit radio / })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Import/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /Actions/ })).not.toBeInTheDocument();
    // The Activity filter is a read-only question about the list, so it stays.
    expect(screen.getByRole("combobox", { name: "Filter by activity" })).toBeInTheDocument();
    // …and every column that is a value is still there.
    // (Columns with nothing in them on the page are hidden from a guest; the mock corpus fills all of these.)
    for (const label of [/Unit/, /Alias/, /Radio ID/, /System/, /Kind/, /Agency/, /Calls/, /Last heard/]) {
      expect(screen.getByRole("columnheader", { name: label })).toBeInTheDocument();
    }
  });

  it("gives Radios everything back to an operator", async () => {
    await signInAs();
    renderWithProviders(<Radios />, "/radios");
    await screen.findByRole("heading", { name: "Radios" });
    expect(await screen.findByRole("columnheader", { name: /Select every row/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Import/ })).toBeInTheDocument();
  });

  it("names a radio's unit and holder without linking a guest into the roster", async () => {
    const radio = RADIOS[0];
    renderWithProviders(<RadioDetail />, `/radios/${radio?.id}`, "/radios/:id");
    await screen.findByRole("heading", { name: "Assignment" });
    expect(await screen.findByText(/RC FIRE ENGINE 1/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /RC FIRE ENGINE 1/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Unit to assign" })).not.toBeInTheDocument();
  });

  it("keeps the recorder's own health off a guest's Statistics", async () => {
    const get = vi.spyOn(client, "apiGet");
    renderWithProviders(<Stats />, "/stats");
    await screen.findByText("Calls today");

    expect(screen.queryByText("Speech-to-text backends")).not.toBeInTheDocument();
    expect(screen.queryByText("Recorder gaps")).not.toBeInTheDocument();
    // Not merely hidden: the gaps request behind that card is never made. The
    // transcription response is still fetched because its quality slices are a
    // guest-visible chart; only the per-backend card is withheld.
    await waitFor(() => expect(get).toHaveBeenCalled());
    const asked = get.mock.calls.map(([path]) => path);
    expect(asked).toContain("/stats/transcription");
    expect(asked).not.toContain("/stats/gaps");
    // The volume the page is actually for is untouched.
    expect(screen.getByText("Transcript coverage")).toBeInTheDocument();
    expect(await screen.findByText("Transcript quality")).toBeInTheDocument();
    get.mockRestore();
  });

  it("shows both cards to an operator", async () => {
    await signInAs();
    renderWithProviders(<Stats />, "/stats");
    expect(await screen.findByText("Speech-to-text backends")).toBeInTheDocument();
    expect((await screen.findAllByText("Recorder gaps")).length).toBeGreaterThan(0);
  });
});
