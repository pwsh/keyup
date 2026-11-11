import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import { RECORDERS } from "../../api/mock/dataset";
import type { Recorder } from "../../api/types";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import {
  CountersStrip,
  RecorderPolicyStrip,
  RecorderTab,
  ReportedAge,
  StandbyBadge,
  StationPanel,
  UplinkStrip,
} from "./RecorderTab";

/** The recorder policy controls need the admin scope, so the fixtures' admin signs in. */
async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * The recorder self-description, rendered against the fixtures.
 *
 * The point of every case here is the same one: keyup shows what the *recorder* said and
 * nothing more. A station it has never described, a tuner with no hardware and no level —
 * those are what a recorder on an older binary sends, and the page has to read correctly
 * rather than showing a row of blanks or crashing on a missing key.
 */
describe("RecorderTab", () => {
  it("shows the station the operator described, with a map link", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect(await screen.findByText("Anytown, US")).toBeInTheDocument();
    expect(screen.getByText("N0CALL <ops@example.invalid>")).toBeInTheDocument();
    expect(screen.getByText(/water tower/)).toBeInTheDocument();
    expect(screen.getByText("320 m ASL")).toBeInTheDocument();

    // Coordinates are a plain OpenStreetMap link — no embedded map, no third-party script.
    const link = screen.getByRole("link", { name: /38\.8814/ });
    expect(link).toHaveAttribute("href", "https://www.openstreetmap.org/?mlat=38.8814&mlon=-94.8191");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("shows the recorder's counters and no low-disk badge at 12 GiB free", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect(await screen.findByText("41,221")).toBeInTheDocument(); // calls started
    // Two recorders in the fixtures, so the label is not unique; the figures are.
    expect(screen.getAllByText("uploaded").length).toBeGreaterThan(0);
    expect(screen.getByText("9.0 MiB")).toBeInTheDocument();
    expect(screen.getByText("12.0 GiB")).toBeInTheDocument();
    expect(screen.queryByText("low disk")).not.toBeInTheDocument();
  });

  it("shows each tuner's hardware, gain and level, and leaves the undescribed one blank", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect(await screen.findByText("Airspy R2")).toBeInTheDocument();
    // Both described tuners name their antenna run; only the fixtures' wording differs.
    expect(screen.getByText("Diamond X50 @ 8m")).toBeInTheDocument();
    expect(screen.getByText("Antenna A — Diamond X50 @ 8m, 806-960 MHz")).toBeInTheDocument();
    expect(screen.getByText("LNA 24 · VGA 20 · amp off")).toBeInTheDocument();
    expect(screen.getByText("gain 42.1")).toBeInTheDocument();
    expect(screen.getByText("-38.4")).toBeInTheDocument();
    // Only the control tuner tracks a control channel, so only it has a frequency error.
    expect(screen.getByText(/Δf -137 Hz/)).toBeInTheDocument();

    // The data tuner reports none of it — the row still renders, with em dashes.
    const row = screen.getByText("rtl-2").closest("tr") as HTMLElement;
    expect(within(row).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows the whole recorder even when nothing is described", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    // The tuner table is the page's actual job; the self-description is additive to it.
    expect((await screen.findAllByRole("columnheader", { name: "Hardware" })).length).toBe(2);
    expect(screen.getAllByRole("columnheader", { name: "Level (dBFS)" }).length).toBe(2);
    for (const tuner of RECORDERS[0]?.tuners ?? []) {
      expect(screen.getByText(tuner.tuner_key)).toBeInTheDocument();
    }
  });
});

describe("the hardware cell", () => {
  it("shows the first line of the operator's notes without a hover", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const row = (await screen.findByText("rtl-1")).closest("tr") as HTMLElement;
    // The note is where the constraints are written, so the table shows a line of it rather
    // than hiding all of it behind the tooltip.
    const note = within(row).getByText(/USB bus 1 port 5/);
    expect(note).toBeInTheDocument();
    // Clamped to one line — and the rest is on `title`, not lost.
    expect(note).toHaveClass("truncate");
    expect(note.getAttribute("title")).toContain("never move this tuner onto it");
  });

  it("says nothing extra for a tuner that described no hardware", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const row = (await screen.findByText("rtl-2")).closest("tr") as HTMLElement;
    expect(within(row).queryByText(/USB bus/)).not.toBeInTheDocument();
  });
});

describe("the station panel and counters strip", () => {
  it("render nothing at all when the recorder has not reported them", () => {
    // A recorder on an older binary sends neither. Absent is absent: no empty card, no row
    // of em dashes pretending the recorder answered a question it was never asked.
    const { container } = renderWithProviders(
      <>
        <StationPanel station={null} reportedAt={null} />
        <CountersStrip counters={null} />
      </>,
    );
    expect(container.textContent).toBe("");
  });

  it("render nothing for an object with nothing readable in it", () => {
    const { container } = renderWithProviders(
      <>
        <StationPanel station={{ name: "  ", notes: null }} reportedAt={null} />
        <CountersStrip counters={{}} />
      </>,
    );
    expect(container.textContent).toBe("");
  });

  it("badges a recorder that is nearly out of disk", () => {
    renderWithProviders(<CountersStrip counters={{ failed: 12, disk_free_bytes: 2 * 1024 ** 3 }} />);
    expect(screen.getByText("low disk")).toBeInTheDocument();
    expect(screen.getByText("2.0 GiB")).toBeInTheDocument();
    // A non-zero failure count is called out rather than left as one number among seven.
    expect(screen.getByText("12")).toHaveClass("text-critical-ink");
  });

  it("shows only the station fields the recorder actually sent", () => {
    renderWithProviders(<StationPanel station={{ name: "Garage shelf" }} reportedAt={null} />);
    expect(screen.getByText("Garage shelf")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument(); // no coordinates, no map link
  });
});

describe("the recorder standby controls", () => {
  it("shows each recorder's standby class, and the warm spare's is warm", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const badges = await screen.findAllByTestId("standby-badge");
    expect(badges.map((badge) => badge.textContent)).toEqual(["hot", "warm"]);
  });

  it("saves a new standby class through the admin PUT", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    const select = await screen.findByLabelText("Standby class for trunkd-central-01");
    await user.click(select);
    await user.click(await screen.findByRole("option", { name: "cold" }));

    // The mock backend merges the policy exactly as the API does, so the badge is the
    // round trip: it only says "cold" because the PUT was accepted and the list refetched.
    await waitFor(() => expect(screen.getAllByTestId("standby-badge")[0]).toHaveTextContent("cold"));
  });

  it("saves a priority on blur, and only when it has actually changed", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    const input = (await screen.findByLabelText("Priority for trunkd-central-02")) as HTMLInputElement;
    expect(input.value).toBe("50");
    await user.clear(input);
    await user.type(input, "5");
    await user.tab();
    // The save is the round trip: the list is invalidated and refetched, and the field only
    // still reads 5 because the value that came back is 5.
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Recorder policy saved"));
    expect(input.value).toBe("5");

    // A blur that changed nothing saves nothing: the priority is left alone.
    const before = useToastStore.getState().toasts.length;
    await user.click(input);
    await user.tab();
    expect(useToastStore.getState().toasts.length).toBe(before);
  });

  it("leaves the controls read-only for someone who is not an admin", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    // Signed out: the page still renders every recorder, and nothing on it can be changed.
    expect(await screen.findByLabelText("Priority for trunkd-central-01")).toBeDisabled();
    expect(screen.getByLabelText("Standby class for trunkd-central-01")).toBeDisabled();
  });

  it("badges a spare the arbiter promoted", () => {
    const { container } = renderWithProviders(
      <StandbyBadge standby="warm" promotedAt={new Date().toISOString()} />,
    );
    expect(screen.getByText("promoted")).toBeInTheDocument();
    expect(container.textContent).toContain("warm");
  });

  it("says nothing about promotion for a recorder nobody promoted", () => {
    // An operator's own manual enable has no promoted_at, and must not be dressed up as
    // keyup's doing: the arbiter will never switch that recorder off again.
    renderWithProviders(<StandbyBadge standby="warm" promotedAt={null} />);
    expect(screen.queryByText("promoted")).not.toBeInTheDocument();
  });
});

/**
 * A recorder that is holding its uploads is still recording — it is filling a spool instead
 * of a bucket — and the whole point of the strip is that an operator can tell the two apart
 * and get the calls moving again. The fixtures ship one of each: the roof box uploading on
 * its own configuration, the garage spare held from keyup with a spool behind it.
 */
describe("the upload hold", () => {
  /** The fixture recorder that is holding, and the one that is not. */
  const held = (): Recorder => RECORDERS[1] as Recorder;
  const uploading = (): Recorder => RECORDERS[0] as Recorder;

  it("says which recorders are uploading and which are holding, and how much is behind it", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const states = await screen.findAllByTestId("uplink-state");
    expect(states[0]).toHaveTextContent("Uploading");
    expect(states[1]).toHaveTextContent("Holding uploads (46 calls, 38.0 MiB spooled)");
    // Where the setting came from, because neither of those is fixed from this page.
    expect(screen.getByText("from its own configuration")).toBeInTheDocument();
    expect(screen.getByText("set from keyup")).toBeInTheDocument();
  });

  it("renders nothing for a recorder that neither reports an uplink nor has been given one", () => {
    // An older binary sends no uplink and would not apply a hold; a dead button on its card
    // would be a promise the software cannot keep.
    const { container } = renderWithProviders(
      <UplinkStrip recorder={{ ...uploading(), uplink: null, desired_spool_only: null }} canAdmin={true} />,
    );
    expect(container.textContent).toBe("");
  });

  it("holds a recorder's uploads in one click, and shows which way it is pending", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    const buttons = await screen.findAllByRole("button", { name: "Hold uploads" });
    await user.click(buttons[0] as HTMLElement);

    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Uploads held"));
    // The recorder has not polled yet, so it is still reported as uploading — and the badge
    // says which way it is about to go rather than only that something is outstanding.
    expect(await screen.findByText("pending → hold")).toBeInTheDocument();
    expect(screen.getAllByTestId("uplink-state")[0]).toHaveTextContent("Uploading");
  });

  it("confirms a release, quoting the number of calls that would upload", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    await user.click(await screen.findByRole("button", { name: "Release hold" }));
    expect(await screen.findByText(/Releasing will upload 46 spooled calls/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Release it" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Hold released"));
    expect(await screen.findByText("pending → upload")).toBeInTheDocument();
  });

  it("backs out of a release without changing anything", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");

    await user.click(await screen.findByRole("button", { name: "Release hold" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Release it" })).not.toBeInTheDocument());
    expect(screen.getAllByTestId("uplink-state")[1]).toHaveTextContent("Holding uploads");
  });

  it("leaves the control read-only for someone who is not an admin", () => {
    renderWithProviders(<UplinkStrip recorder={held()} canAdmin={false} />);
    expect(screen.getByRole("button", { name: "Release hold" })).toBeDisabled();
  });

  it("trusts the recorder's own report over what keyup asked for", () => {
    // keyup wants a hold and the recorder has not taken it up: the strip reports what is
    // actually happening — uploading — and the badge carries the difference.
    renderWithProviders(
      <UplinkStrip
        recorder={{
          ...uploading(),
          desired_spool_only: true,
          desired_recorder_version: 3,
          applied_recorder_version: 2,
          spool_only_pending: true,
        }}
        canAdmin={true}
      />,
    );
    expect(screen.getByTestId("uplink-state")).toHaveTextContent("Uploading");
    expect(screen.getByText("pending → hold")).toBeInTheDocument();
  });

  it("shows a hold with no spool figures without inventing any", () => {
    renderWithProviders(
      <UplinkStrip recorder={{ ...held(), uplink: { spool_only: true, source: "panel" } }} canAdmin={true} />,
    );
    expect(screen.getByTestId("uplink-state")).toHaveTextContent("Holding uploads");
    expect(screen.getByTestId("uplink-state").textContent).not.toContain("(");
    expect(screen.getByText("set on the recorder")).toBeInTheDocument();
  });
});

describe("the staleness indicator", () => {
  it("calls out a recorder that has not reported inside the arbiter's window", () => {
    renderWithProviders(<ReportedAge reportedAt={new Date(Date.now() - 600_000).toISOString()} />);
    expect(screen.getByText(/^reported/)).toHaveClass("text-critical-ink");
  });

  it("does not call out one that reported a moment ago", () => {
    renderWithProviders(<ReportedAge reportedAt={new Date(Date.now() - 5_000).toISOString()} />);
    expect(screen.getByText(/^reported/)).not.toHaveClass("text-critical-ink");
  });

  it("says 'never' — not 'just now' — for a recorder that has never reported", () => {
    renderWithProviders(<ReportedAge reportedAt={null} />);
    expect(screen.getByText("reported never")).toHaveClass("text-critical-ink");
  });
});

describe("the policy strip on its own", () => {
  it("refuses a priority that is not a whole number and puts the old one back", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    const recorder = RECORDERS[1];
    if (!recorder) throw new Error("the fixtures lost the warm spare");
    renderWithProviders(<RecorderPolicyStrip recorder={recorder} canAdmin />);

    const input = (await screen.findByLabelText(`Priority for ${recorder.instance_id}`)) as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "not a number");
    await user.tab();
    expect(input.value).toBe(String(recorder.priority));
    expect(useToastStore.getState().toasts.at(-1)?.tone).toBe("error");
  });

  it("reads an emptied field as a change of mind, not as priority zero", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    const recorder = RECORDERS[1];
    if (!recorder) throw new Error("the fixtures lost the warm spare");
    renderWithProviders(<RecorderPolicyStrip recorder={recorder} canAdmin />);

    const input = (await screen.findByLabelText(`Priority for ${recorder.instance_id}`)) as HTMLInputElement;
    const before = useToastStore.getState().toasts.length;
    await user.clear(input);
    await user.tab();
    expect(input.value).toBe(String(recorder.priority));
    expect(useToastStore.getState().toasts.length).toBe(before); // nothing saved, nothing said
  });
});

/**
 * The band editor: what a tuner is allowed to record inside its capture span.
 *
 * The cases that matter are the ones where two things look alike — an empty list against no
 * list at all, megahertz on the page against hertz on the wire, and a desired list the
 * recorder has not taken up yet — because each pair is a different instruction to a
 * headless box that nobody is standing next to.
 */
describe("the tuner band editor", () => {
  async function openDrawer(tunerKey: string): Promise<void> {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const row = (await screen.findByText(tunerKey)).closest("tr") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog");
  }

  it("shows the five band labels, with the tuner's own list already on", async () => {
    await openDrawer("rtl-1");
    const dialog = screen.getByRole("dialog");
    for (const label of ["VHF", "UHF", "700", "800", "900"]) {
      expect(within(dialog).getByRole("button", { name: label })).toBeInTheDocument();
    }
    // The fixture's voice tuner is 800-only, and that is what the chip row says.
    expect(within(dialog).getByRole("button", { name: "800" })).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByRole("button", { name: "VHF" })).toHaveAttribute("aria-pressed", "false");
  });

  it("shows an explicit range in MHz and sends it back in hertz", async () => {
    await openDrawer("rtl-1");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    await user.type(within(dialog).getByLabelText("Range low edge, MHz"), "856");
    await user.type(within(dialog).getByLabelText("Range high edge, MHz"), "866.5");
    await user.click(within(dialog).getByRole("button", { name: "Add range" }));

    // Shown in the unit it was typed in…
    expect(within(dialog).getByText("856–866.5 MHz")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    // …and stored in whole hertz, which is what the recorder parses.
    const tuner = (await apiGet<Recorder[]>("/admin/recorders"))
      .flatMap((recorder) => recorder.tuners)
      .find((entry) => entry.tuner_key === "rtl-1");
    expect(tuner?.desired?.bands).toEqual(["800", "856000000-866500000"]);
  });

  it("removes a range chip again", async () => {
    await openDrawer("rtl-2");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    // The fixture's data tuner wants a narrow slice; the chip is there with its remove button.
    expect(within(dialog).getByText("858–859.5 MHz")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Remove 858–859.5 MHz" }));
    expect(within(dialog).queryByText("858–859.5 MHz")).not.toBeInTheDocument();
    expect(within(dialog).getByText(/Unrestricted — this tuner may record anything/)).toBeInTheDocument();
  });

  it("calls out a desired list the recorder has not applied yet", async () => {
    await openDrawer("rtl-2");
    const dialog = screen.getByRole("dialog");
    // Applied is the 800 band; desired is the narrower slice. That gap is the pending badge.
    const applied = within(dialog).getByTestId("bands-applied");
    expect(applied).toHaveTextContent("Applied: 800");
    expect(within(applied).getByText("pending")).toBeInTheDocument();
  });

  it("warns, without blocking, about a list that misses the tuner's span", async () => {
    await openDrawer("rtl-1");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    // The voice tuner captures 850.8–853.2 MHz; VHF cannot be reached from there.
    await user.click(within(dialog).getByRole("button", { name: "800" }));
    await user.click(within(dialog).getByRole("button", { name: "VHF" }));
    expect(within(dialog).getByText(/None of these reach the tuner's span/)).toBeInTheDocument();

    // A warning, not a refusal: trunkd is the one that decides, and the save goes through.
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));
  });

  it("clears to unrestricted with an empty list, not by dropping the key", async () => {
    await openDrawer("rtl-1");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Clear" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    const tuner = (await apiGet<Recorder[]>("/admin/recorders"))
      .flatMap((recorder) => recorder.tuners)
      .find((entry) => entry.tuner_key === "rtl-1");
    // `[]` is a value the recorder applies — "record anything in your span" — and is not
    // the same instruction as the key not being there at all.
    expect(tuner?.desired?.bands).toEqual([]);
  });

  it("hands the bands back to the recorder by removing the key entirely", async () => {
    await openDrawer("rtl-1");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Stop managing this tuner's bands" }));
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    const tuner = (await apiGet<Recorder[]>("/admin/recorders"))
      .flatMap((recorder) => recorder.tuners)
      .find((entry) => entry.tuner_key === "rtl-1");
    expect(tuner?.desired && "bands" in tuner.desired).toBe(false);
  });

  it("leaves a tuner keyup is not managing exactly as it found it", async () => {
    // Opening the drawer and saving something else must not start managing the bands: the
    // control tuner has none, and a body carrying `bands: []` would have silenced a
    // restriction the recorder may be holding in its own configuration.
    await openDrawer("airspy-0");
    const user = userEvent.setup();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    const tuner = (await apiGet<Recorder[]>("/admin/recorders"))
      .flatMap((recorder) => recorder.tuners)
      .find((entry) => entry.tuner_key === "airspy-0");
    expect(tuner?.desired && "bands" in tuner.desired).toBe(false);
  });

  it("draws the restriction under the span, and only where there is one", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const restricted = (await screen.findByText("rtl-1")).closest("tr") as HTMLElement;
    expect(within(restricted).getByTestId("tuner-band-strip")).toBeInTheDocument();
    // The control tuner records its whole span: a fully shaded rail would say nothing.
    const unrestricted = screen.getByText("airspy-0").closest("tr") as HTMLElement;
    expect(within(unrestricted).queryByTestId("tuner-band-strip")).not.toBeInTheDocument();
  });
});
