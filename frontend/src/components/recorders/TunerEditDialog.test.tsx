import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import type { Recorder } from "../../api/types";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { RecorderTab } from "./RecorderTab";

/**
 * The tuner edit dialog, against the two front ends the fixtures describe.
 *
 * The cases worth writing are the ones where the page has to read the *tuner* rather than a
 * default: which gain controls exist at all, what bounds them, which fields the recorder
 * says restart the stream, and which of the last write it refused. Getting any of those from
 * a guess rather than from the reported status is how an operator ends up setting a value
 * that silently does nothing.
 */
async function openTuner(tunerKey: string): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
  const user = userEvent.setup();
  renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
  const row = (await screen.findByText(tunerKey)).closest("tr") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "Edit" }));
  await screen.findByRole("dialog");
}

async function tunerAfterSave(tunerKey: string) {
  const recorders = await apiGet<Recorder[]>("/admin/recorders");
  return recorders.flatMap((recorder) => recorder.tuners).find((t) => t.tuner_key === tunerKey);
}

describe("the tuner edit dialog, on a Soapy front end", () => {
  it("draws one row per reported gain element, bounded by the range it gave", async () => {
    await openTuner("airspy-0");
    const dialog = screen.getByRole("dialog");
    for (const name of ["LNA", "MIX", "IF"]) {
      const slider = within(dialog).getByLabelText(`${name} gain, dB`);
      expect(slider).toHaveAttribute("min", "0");
      expect(slider).toHaveAttribute("max", "15");
      expect(slider).toHaveAttribute("step", "1");
    }
    // Seeded from what the tuner says it is running, so opening it proposes no change.
    expect(within(dialog).getByLabelText("LNA gain in dB")).toHaveValue("12");
    // …and the HackRF-only controls are not offered at all.
    expect(within(dialog).queryByLabelText("LNA (dB)")).not.toBeInTheDocument();
  });

  it("offers AGC, overall gain and the antenna port, and no HackRF amplifier", async () => {
    await openTuner("airspy-0");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Hardware AGC")).toBeInTheDocument();
    expect(within(dialog).getByText("Overall gain (dB)")).toBeInTheDocument();
    expect(within(dialog).getByText("Antenna port")).toBeInTheDocument();
    expect(within(dialog).queryByText("Front-end amplifier")).not.toBeInTheDocument();
  });

  it("marks the fields the tuner says it cannot change live", async () => {
    await openTuner("airspy-0");
    const dialog = screen.getByRole("dialog");
    // The fixture restarts on sample_rate and ppm, and on nothing else.
    expect(within(dialog).getAllByText("restarts the tuner").length).toBe(2);
    const sampleRate = within(dialog).getByText("Sample rate (Hz)").closest("span") as HTMLElement;
    expect(within(sampleRate).getByText("restarts the tuner")).toBeInTheDocument();
  });

  it("shows the correction auto-ppm has actually settled on, read-only", async () => {
    await openTuner("airspy-0");
    expect(screen.getByTestId("auto-ppm-applied")).toHaveTextContent("1.82 ppm");
  });

  it("explains what following data grants is for", async () => {
    await openTuner("airspy-0");
    expect(
      screen.getByText(/Follow SNDCP data grants; needed for the unknown-data analysis/),
    ).toBeInTheDocument();
  });

  it("sends only the settings that were touched", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    await user.clear(within(dialog).getByLabelText("MIX gain in dB"));
    await user.type(within(dialog).getByLabelText("MIX gain in dB"), "9");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    const tuner = await tunerAfterSave("airspy-0");
    // The element that was dragged is written…
    expect(tuner?.desired?.gains).toEqual({ LNA: 12, MIX: 9, IF: 6 });
    // …and every setting nobody touched stays out of the desired state entirely, so the
    // recorder keeps running those on its own configuration.
    expect(tuner?.desired).not.toHaveProperty("ppm");
    expect(tuner?.desired).not.toHaveProperty("sample_rate");
    expect(tuner?.desired).not.toHaveProperty("agc");
  });

  it("reads an emptied box as 'stop saying anything about this'", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    // The tuner reports 1.5 ppm, so the box starts filled; clearing it is the explicit null.
    const ppm = within(dialog).getByLabelText(/Correction/);
    expect(ppm).toHaveValue("1.5");
    await user.clear(ppm);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));

    const tuner = await tunerAfterSave("airspy-0");
    expect(tuner?.desired).not.toHaveProperty("ppm");
  });
});

describe("the tuner edit dialog, on a HackRF", () => {
  it("offers the two fixed-step stages and the amplifier, and no element rows", async () => {
    await openTuner("hackrf-0");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("LNA (dB)")).toBeInTheDocument();
    expect(within(dialog).getByText("VGA (dB)")).toBeInTheDocument();
    expect(within(dialog).getByText("Front-end amplifier")).toBeInTheDocument();
    expect(within(dialog).queryByLabelText("LNA gain, dB")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("Hardware AGC")).not.toBeInTheDocument();
  });

  it("states each stage's step, so an off-step value is never offered", async () => {
    await openTuner("hackrf-0");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/8 dB steps to 40 dB/)).toBeInTheDocument();
    expect(within(dialog).getByText(/2 dB steps to 62 dB/)).toBeInTheDocument();
  });

  it("shows the recorder's reason for a field it refused, under that field", async () => {
    await openTuner("hackrf-0");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("rejected-lna_db")).toHaveTextContent(
      "The recorder refused this: the front end is fixed on this board",
    );
    expect(within(dialog).getByTestId("rejected-summary")).toHaveTextContent("refused 1 field");
  });
});

describe("the dialog's controls themselves", () => {
  it("says 'None' where nothing is chosen, rather than showing an empty box", async () => {
    await openTuner("hackrf-0");
    const dialog = screen.getByRole("dialog");
    // The HackRF fixture is focused on no site. An unset select used to render as a blank
    // box with no way back to the unset state — Radix reads value="" as "no value" and
    // refuses an option that claims it, so the option was silently dropped.
    expect(within(dialog).getByLabelText("Focus site")).toHaveTextContent("None");
  });

  it("offers the empty state as a real option, so a chosen site can be taken back", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByLabelText("Focus site"));
    const none = await screen.findByRole("option", { name: "None" });
    await user.click(none);
    expect(within(dialog).getByLabelText("Focus site")).toHaveTextContent("None");
  });

  it("gives every info icon a hit target big enough to aim at, and opens it from the keyboard", async () => {
    await openTuner("airspy-0");
    const dots = within(screen.getByRole("dialog")).getAllByTestId("info-dot");
    expect(dots.length).toBeGreaterThan(5);
    for (const dot of dots) {
      // 24 px square around a 16 px glyph: h-6/w-6 in Tailwind's scale.
      expect(dot.className).toContain("h-6");
      expect(dot.className).toContain("w-6");
      expect(dot.tagName).toBe("BUTTON");
      // Focusable: nothing has taken it out of the tab order.
      expect(dot).not.toHaveAttribute("tabindex", "-1");
    }
    // …and the explanation is reachable without a mouse at all.
    const first = dots[0] as HTMLElement;
    fireEvent.focus(first);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/\w/);
  });

  it("groups the form into sections and keeps the rarely used ones folded away", async () => {
    await openTuner("airspy-0");
    const dialog = screen.getByRole("dialog");
    for (const title of ["Identity", "Tuning", "Gain", "Corrections", "Data & spectrum", "Advanced"]) {
      expect(within(dialog).getByRole("heading", { name: title })).toBeInTheDocument();
    }
    // Advanced opens itself here: this tuner already carries a candidate list, and state
    // hidden behind a disclosure is state nobody reads.
    const advanced = within(dialog).getByRole("button", { name: /Hide|Show/ });
    expect(advanced).toHaveAttribute("aria-expanded", "true");
    const user = userEvent.setup();
    await user.click(advanced);
    expect(advanced).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByLabelText("Control frequency, MHz")).not.toBeInTheDocument();
  });
});

describe("the reported hardware", () => {
  it("shows the operator's description in full, above the settings it constrains", async () => {
    await openTuner("rtl-1");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Hardware" })).toBeInTheDocument();
    expect(within(dialog).getByText("RTL-SDR v3")).toBeInTheDocument();
    expect(within(dialog).getByText("RG-58, 6 m")).toBeInTheDocument();

    // The notes are the point: nothing is truncated, both lines are there, and the line
    // breaks the operator typed are kept.
    const notes = within(dialog).getByTestId("hardware-notes");
    expect(notes).toHaveTextContent("USB bus 1 port 5");
    expect(notes).toHaveTextContent("never move this tuner onto it");
    expect(notes).toHaveClass("whitespace-pre-wrap");
    expect(notes.className).not.toContain("truncate");
  });

  it("repeats the constraint beside the controls it constrains", async () => {
    await openTuner("rtl-1");
    const dialog = screen.getByRole("dialog");
    // The antenna under the band editor, and the first line of the notes under the role:
    // scrolling back to the top to re-read either is the step an operator skips.
    expect(within(dialog).getByTestId("bands-antenna-note")).toHaveTextContent("806-960 MHz");
    expect(within(dialog).getByTestId("role-hardware-note")).toHaveTextContent("USB bus 1 port 5");
  });

  it("is absent entirely on a tuner that described none", async () => {
    // The data tuner reports no description at all, as every tuner on an older binary does.
    await openTuner("rtl-2");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("heading", { name: "Hardware" })).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId("hardware-notes")).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId("bands-antenna-note")).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId("role-hardware-note")).not.toBeInTheDocument();
  });
});

describe("the control-channel candidates", () => {
  it("lists them in megahertz and adds one on the 12.5 kHz grid", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByText("851.025 MHz")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Control frequency, MHz"), "852.1625");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    expect(within(dialog).getByText("852.1625 MHz")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("Desired state saved"));
    const tuner = await tunerAfterSave("airspy-0");
    // Typed in megahertz, stored in whole hertz — what the recorder parses.
    expect(tuner?.desired?.control_frequencies).toEqual([851_025_000, 852_162_500]);
  });

  it("refuses one that is off the channel grid, before it is ever sent", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Control frequency, MHz"), "851.0251");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    expect(within(dialog).getByText(/12.5 kHz grid/)).toBeInTheDocument();
  });

  it("warns, without blocking, about one outside the tuner's span", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    // The fixture's span is 850.5–860.5 MHz; 774 MHz is nowhere near it.
    await user.type(within(dialog).getByLabelText("Control frequency, MHz"), "774");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    expect(within(dialog).getByText("774 MHz")).toBeInTheDocument();
    expect(within(dialog).getByText("outside the tuner's span")).toBeInTheDocument();
  });

  it("removes one again", async () => {
    await openTuner("airspy-0");
    const user = userEvent.setup();
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove 851.025 MHz" }));
    expect(within(dialog).queryByText("851.025 MHz")).not.toBeInTheDocument();
    expect(within(dialog).getByText(/hunts whatever its own configuration says/)).toBeInTheDocument();
  });

  it("cannot fill from a site until one is chosen", async () => {
    await openTuner("hackrf-0");
    const dialog = screen.getByRole("dialog");
    // The HackRF fixture is focused on no site by id, so there is nowhere to read from.
    expect(within(dialog).getByRole("button", { name: "Use site's known control channels" })).toBeDisabled();
  });
});

describe("the recorder card's link and event-log panels", () => {
  it("counts the peers a recorder reports and says what each is doing", async () => {
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    // Each of the two fixtures names the other, so both cards say "1 peer".
    expect((await screen.findAllByText("Link: 1 peer")).length).toBe(2);
    expect(screen.getByText("trunkd-central-02")).toBeInTheDocument();
    expect(screen.getByText("claims site:Central")).toBeInTheDocument();
    // The roof box's peer claims nothing, and that is a different sentence from no peers.
    expect(screen.getByText("no claims")).toBeInTheDocument();
  });

  it("shows the archived days with a download link, and the recorder's own backlog", async () => {
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    const link = await screen.findByRole("link", { name: "2026-09-01" });
    expect(link).toHaveAttribute("href", "/api/admin/event-logs/1/download");
    expect(screen.getByText("6.5 MiB")).toBeInTheDocument();
    // The spare says it is two days behind, which is the figure the list cannot show.
    expect(screen.getByText("2 days pending")).toBeInTheDocument();
    expect(screen.getByText("nothing pending")).toBeInTheDocument();
  });

  it("keeps the archive list to the recorder whose card it is on", async () => {
    await apiPost("/auth/login", { username: "alex", password: "hunter2" });
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    await screen.findByRole("link", { name: "2026-09-01" });
    // 2026-08-30 was shipped by both recorders, so it appears once on each card and
    // nowhere else — three days on the roof box, one on the spare.
    expect(screen.getAllByRole("link", { name: "2026-08-30" }).length).toBe(2);
    expect(screen.getAllByRole("link", { name: "2026-08-31" }).length).toBe(1);
  });
});
