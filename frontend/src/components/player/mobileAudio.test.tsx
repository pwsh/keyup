/**
 * The player on a phone.
 *
 * Two faults were reported together, and they share a cause: "auto play does not play
 * new calls, and the live view breaks as soon as you play anything". Both come from the
 * same place — a mobile browser will only start audio on an element a *user gesture*
 * approved, and the player used to create a fresh `<audio>` every time it opened, so
 * there was never an approved element for the live feed's arrivals to play through.
 *
 * These cases model the policy rather than assert on jsdom's (non-existent) media
 * support: `play()` rejects with `NotAllowedError` until it is called inside a gesture,
 * and resolves for the element for ever after — which is exactly what WebKit does.
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Call } from "../../api/types";
import { usePlayerStore } from "../../state/player";
import { Providers, renderWithProviders } from "../../test/render";
import { Player } from "./Player";
import { isAudioUnlocked, resetAudioUnlock } from "./unlock";

const CALL = (id: number): Call =>
  ({
    id,
    talkgroup_alias: `TG ${id}`,
    duration_s: 4,
    started_at: new Date(1_800_000_000_000 + id * 1_000).toISOString(),
    transcript_status: "done",
    transcript_quality: "good",
  }) as unknown as Call;

/* ------------------------------------------------ a browser with an autoplay policy */

/** True only while a synthetic user gesture is being dispatched. */
let insideGesture = false;
/** Set the first time `play()` is called inside a gesture; never unset. */
let approved = false;
const plays = vi.fn();

function notAllowed(): Error {
  const error = new Error("play() failed because the user didn't interact with the document first");
  error.name = "NotAllowedError";
  return error;
}

beforeEach(() => {
  insideGesture = false;
  approved = false;
  plays.mockClear();
  resetAudioUnlock();
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    writable: true,
    value: function play(this: HTMLMediaElement) {
      plays(this);
      if (insideGesture) approved = true;
      return approved ? Promise.resolve() : Promise.reject(notAllowed());
    },
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    writable: true,
    value: () => {},
  });
  Object.defineProperty(HTMLMediaElement.prototype, "load", {
    configurable: true,
    writable: true,
    value: () => {},
  });
});

afterEach(() => {
  resetAudioUnlock();
});

/** A tap, with the transient activation a real one would carry. */
async function tap(element: Element): Promise<void> {
  insideGesture = true;
  await userEvent.click(element);
  insideGesture = false;
}

describe("the player's media element", () => {
  it("is one element for the life of the app, even while nothing is loaded", () => {
    const { container } = renderWithProviders(<Player />, "/calls");
    const idle = container.querySelector("audio");
    expect(idle).not.toBeNull();

    act(() => usePlayerStore.getState().select(CALL(1), { play: false }));
    expect(container.querySelector("audio")).toBe(idle);

    // A different call is a new source on the same element, never a new element:
    // the phone's permission is attached to the element, not to the page.
    act(() => usePlayerStore.getState().select(CALL(2), { play: false }));
    expect(container.querySelector("audio")).toBe(idle);

    // Closing the player takes the bar away and must leave the element behind.
    act(() => usePlayerStore.getState().clear());
    expect(screen.queryByLabelText("Audio player")).toBeNull();
    expect(container.querySelector("audio")).toBe(idle);

    act(() => usePlayerStore.getState().select(CALL(3), { play: false }));
    expect(container.querySelector("audio")).toBe(idle);
  });

  it("spends the first tap anywhere in the app on priming that element", async () => {
    renderWithProviders(
      <>
        <button type="button">Somewhere else entirely</button>
        <Player />
      </>,
      "/calls",
    );
    expect(isAudioUnlocked()).toBe(false);

    await tap(screen.getByRole("button", { name: "Somewhere else entirely" }));

    expect(isAudioUnlocked()).toBe(true);
    expect(approved).toBe(true);
  });

  it("auto-plays a call that arrives later, with no second gesture", async () => {
    renderWithProviders(
      <>
        <button type="button">Live</button>
        <Player />
      </>,
      "/calls",
    );
    await tap(screen.getByRole("button", { name: "Live" }));
    plays.mockClear();

    // Exactly what the live feed does when `autoPlayNew` is on, long after the tap.
    act(() => {
      usePlayerStore.setState({ live: true, autoPlayNew: true });
      usePlayerStore.getState().liveArrived(CALL(9));
    });
    await act(async () => {});

    expect(plays).toHaveBeenCalled();
    expect(usePlayerStore.getState().playing).toBe(true);
    expect(screen.queryByRole("button", { name: /Tap to enable audio/ })).toBeNull();
  });
});

describe("a refused play()", () => {
  it("offers a prompt instead of leaving the bar claiming to play", async () => {
    renderWithProviders(<Player />, "/calls");

    act(() => usePlayerStore.getState().select(CALL(4)));
    await act(async () => {});

    // The store told the truth: nothing is playing.
    expect(usePlayerStore.getState().playing).toBe(false);
    expect(await screen.findByRole("button", { name: /Tap to enable audio/ })).toBeInTheDocument();
  });

  it("clears the prompt and starts the call when the prompt is tapped", async () => {
    renderWithProviders(<Player />, "/calls");
    act(() => usePlayerStore.getState().select(CALL(5)));
    await act(async () => {});

    await tap(await screen.findByRole("button", { name: /Tap to enable audio/ }));
    await act(async () => {});

    expect(usePlayerStore.getState().playing).toBe(true);
    expect(screen.queryByRole("button", { name: /Tap to enable audio/ })).toBeNull();
  });

  it("says nothing for a failure that is not the autoplay policy", async () => {
    Object.defineProperty(HTMLMediaElement.prototype, "play", {
      configurable: true,
      writable: true,
      value: () => Promise.reject(Object.assign(new Error("gone"), { name: "NotSupportedError" })),
    });
    renderWithProviders(<Player />, "/calls");

    act(() => usePlayerStore.getState().select(CALL(6)));
    await act(async () => {});

    expect(usePlayerStore.getState().playing).toBe(false);
    expect(screen.queryByRole("button", { name: /Tap to enable audio/ })).toBeNull();
  });
});

describe("the player and the rest of the app", () => {
  it("does not remount anything around it when the queue is walked", async () => {
    const { container } = render(
      <Providers route="/calls">
        <Player />
      </Providers>,
    );
    const element = container.querySelector("audio");
    act(() => {
      usePlayerStore.getState().setQueue([CALL(1), CALL(2), CALL(3)]);
      usePlayerStore.getState().select(CALL(1), { play: false });
    });
    await act(async () => {});

    act(() => usePlayerStore.getState().next());
    act(() => usePlayerStore.getState().toggle());
    act(() => usePlayerStore.getState().previous());
    await act(async () => {});

    expect(container.querySelector("audio")).toBe(element);
  });
});
