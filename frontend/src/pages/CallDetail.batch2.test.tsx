import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { CALLS } from "../api/mock/dataset";
import type { Call } from "../api/types";
import { Player } from "../components/player/Player";
import { usePlayerStore } from "../state/player";
import { renderWithProviders } from "../test/render";
import { CallDetail } from "./CallDetail";

/** Review 2026-10-29 #19 (phone order and the actions menu) and #20 (one player). */

const call = CALLS.find(
  (entry) => entry.transcript_status === "done" && entry.audio_state === "present",
) as Call;

function renderDetail() {
  return renderWithProviders(
    <>
      <CallDetail />
      <Player />
    </>,
    `/calls/${call.id}`,
    "/calls/:id",
  );
}

function setWidth(width: number) {
  window.innerWidth = width;
}

// `useMediaQuery` caches one MediaQueryList per query, and the shared stub answers once.
// This one answers from the current width every time it is read, so a test can resize.
const stub = window.matchMedia;
window.matchMedia = ((query: string) => {
  const base = stub(query);
  const min = /\(min-width:\s*(\d+)px\)/.exec(query);
  return Object.defineProperty({ ...base }, "matches", {
    get: () => (min ? window.innerWidth >= Number(min[1]) : base.matches),
  });
}) as typeof window.matchMedia;

afterEach(() => setWidth(1440));

describe("call detail order (#19)", () => {
  it("reads title, then Play, then the transcript, then the recording details", async () => {
    renderDetail();
    const title = await screen.findByRole("heading", { level: 1 });
    const play = screen.getByRole("button", { name: "Play this call" });
    const transcript = screen.getByRole("heading", { name: "Transcript" });
    const details = screen.getByRole("heading", { name: "Recording details" });
    const follows = (a: Element, b: Element) =>
      Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(title, play)).toBe(true);
    expect(follows(play, transcript)).toBe(true);
    expect(follows(transcript, details)).toBe(true);
  });

  it("folds the recording details on a phone and opens them from md up", async () => {
    setWidth(390);
    const { unmount } = renderDetail();
    const summary = (await screen.findByRole("heading", { name: "Recording details" })).closest("details");
    expect(summary).not.toHaveAttribute("open");
    unmount();
    setWidth(1440);
    renderDetail();
    const wide = (await screen.findByRole("heading", { name: "Recording details" })).closest("details");
    expect(wide).toHaveAttribute("open");
  });

  it("keeps the copy/audio actions in one menu that returns focus on Escape", async () => {
    const user = userEvent.setup();
    renderDetail();
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByRole("button", { name: /Copy link/ })).not.toBeInTheDocument();
    const more = screen.getByRole("button", { name: "More actions for this call" });
    more.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("button", { name: /Copy link/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open audio file/ })).toBeInTheDocument();
    // A guest is offered nothing they cannot do.
    expect(screen.queryByRole("button", { name: /Re-transcribe/ })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("button", { name: /Copy link/ })).not.toBeInTheDocument());
    await waitFor(() => expect(more).toHaveFocus());
  });
});

describe("one player per call (#20)", () => {
  it("seeks the global player from the waveform, loading the call on the first press", async () => {
    renderDetail();
    const wave = await screen.findByRole("slider", { name: "Playback position" });
    wave.getBoundingClientRect = () => ({
      left: 0,
      width: 200,
      top: 0,
      height: 64,
      right: 200,
      bottom: 64,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    expect(usePlayerStore.getState().current).toBeNull();

    fireEvent.pointerDown(wave, { button: 0, clientX: 100, pointerId: 1 });
    const state = usePlayerStore.getState();
    expect(state.current?.id).toBe(call.id);
    expect(state.playing).toBe(true);
    expect(state.currentTime).toBeCloseTo(call.duration_s / 2, 5);

    // Dragging scrubs the same player.
    fireEvent.pointerMove(wave, { clientX: 50, pointerId: 1 });
    expect(usePlayerStore.getState().currentTime).toBeCloseTo(call.duration_s / 4, 5);
    fireEvent.pointerUp(wave, { pointerId: 1 });

    // Once the call is in the bar, the page has no Play of its own.
    expect(screen.queryByRole("button", { name: "Play this call" })).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Audio player" })).getByRole("button", { name: "Pause" }),
    ).toBeInTheDocument();
  });

  it("says the same length in the page and in the bar", async () => {
    renderDetail();
    await screen.findByRole("heading", { level: 1 });
    act(() => {
      usePlayerStore.getState().select(call, { play: false });
      // The element reports its own length, a little under the recorder's.
      usePlayerStore.getState().setTime(1, 6.96);
    });
    const bar = screen.getByRole("region", { name: "Audio player" });
    expect(within(bar).getByText("1.0s / 7.0s")).toBeInTheDocument();
    expect(screen.getByTestId("call-playhead")).toHaveTextContent("1.0s / 7.0s");
  });
});
