import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Call } from "../../api/types";
import { usePlayerStore } from "../../state/player";
import { renderWithProviders } from "../../test/render";
import {
  BRICKWALL,
  COMPRESSOR,
  MAKEUP_DB,
  MAX_MAKEUP_DB,
  OUTPUT_CEILING,
  resetAudioGraph,
} from "./normalize";
import { Player } from "./Player";

const CALL = {
  id: 1,
  talkgroup_alias: "TG 1",
  duration_s: 4,
  started_at: new Date(1_800_000_000_000).toISOString(),
  transcript_status: "done",
  transcript_quality: "good",
} as unknown as Call;

/* ------------------------------------------------------------ a fake Web Audio */

/**
 * Just enough of the Web Audio API to see the shape of the graph.
 *
 * jsdom has none of it, so this doubles as the mock-safety fixture: the tests
 * that leave it uninstalled are the ones proving the player does not throw in a
 * browser (or a test run) with no `AudioContext` at all.
 */
interface FakeNode {
  kind: string;
  connectedTo: FakeNode[];
  connect: (target: FakeNode) => void;
  disconnect: () => void;
}

interface FakeContext {
  state: "running" | "suspended" | "closed";
  destination: FakeNode;
  created: FakeNode[];
  sources: number;
  resumes: number;
  suspends: number;
  /** The leveling compressor's params — the first one built. */
  compressorParams: Record<string, number>;
  /** The brickwall limiter's params — the second. See `BRICKWALL` in `normalize.ts`. */
  brickwallParams: Record<string, number>;
  /** One entry per gain node past the first, in creation order: ceiling, then tone dip. */
  gains: Record<string, number>;
  /** The make-up gain — the first gain node built. */
  gain: number;
  createMediaElementSource: (element: HTMLMediaElement) => FakeNode;
  createDynamicsCompressor: () => FakeNode;
  createGain: () => FakeNode;
  resume: () => Promise<void>;
  suspend: () => Promise<void>;
}

function node(kind: string): FakeNode {
  const self: FakeNode = {
    kind,
    connectedTo: [],
    connect: (target) => {
      self.connectedTo.push(target);
    },
    disconnect: () => {
      self.connectedTo.length = 0;
    },
  };
  return self;
}

/** A write-only `AudioParam` that records what the graph builder set it to. */
function param(into: Record<string, number>, name: string): AudioParam {
  return {
    set value(next: number) {
      into[name] = next;
    },
  } as unknown as AudioParam;
}

function fakeContext(): FakeContext {
  const created: FakeNode[] = [];
  const track = (made: FakeNode): FakeNode => {
    created.push(made);
    return made;
  };
  const context: FakeContext = {
    state: "suspended",
    destination: node("destination"),
    created,
    sources: 0,
    resumes: 0,
    suspends: 0,
    compressorParams: {},
    brickwallParams: {},
    gains: {},
    gain: 0,
    createMediaElementSource: () => {
      context.sources += 1;
      return track(node("source"));
    },
    createDynamicsCompressor: () => {
      const compressor = track(node("compressor")) as FakeNode & Record<string, unknown>;
      // Two compressors now — the leveler and the brickwall behind the make-up gain — and
      // a shared slot would have the second silently overwrite the first's params.
      const index = context.created.filter((made) => made.kind === "compressor").length - 1;
      const into = index === 0 ? context.compressorParams : context.brickwallParams;
      for (const name of ["threshold", "knee", "ratio", "attack", "release"]) {
        compressor[name] = param(into, name);
      }
      return compressor;
    },
    createGain: () => {
      const gain = track(node("gain")) as FakeNode & Record<string, unknown>;
      // Each gain node gets its own slot — there are three now: the make-up gain, the
      // output ceiling and the tone dip, and one shared slot would have each quietly
      // overwrite the last.
      // Counted by node, not by keys written: an `AudioParam` nobody has assigned to yet
      // has left no key behind, and both nodes would land in the same slot.
      const index = context.created.filter((made) => made.kind === "gain").length - 1;
      gain.gain =
        index === 0
          ? param(context as unknown as Record<string, number>, "gain")
          : param(context.gains, `gain${index}`);
      return gain;
    },
    resume: () => {
      context.resumes += 1;
      context.state = "running";
      return Promise.resolve();
    },
    suspend: () => {
      context.suspends += 1;
      context.state = "suspended";
      return Promise.resolve();
    },
  };
  return context;
}

const contexts: FakeContext[] = [];

function installWebAudio(): void {
  (globalThis as { AudioContext?: unknown }).AudioContext = function AudioContextStub() {
    const context = fakeContext();
    contexts.push(context);
    return context;
  } as unknown as typeof AudioContext;
}

function removeWebAudio(): void {
  (globalThis as { AudioContext?: unknown }).AudioContext = undefined;
}

const graph = () => contexts[contexts.length - 1] as FakeContext;
const nodesOfKind = (kind: string) => graph().created.filter((made) => made.kind === kind);
const chainFrom = (start: FakeNode): string[] => {
  const path = [start.kind];
  let at = start;
  while (at.connectedTo[0]) {
    at = at.connectedTo[0];
    path.push(at.kind);
  }
  return path;
};

beforeEach(() => {
  contexts.length = 0;
  resetAudioGraph();
  localStorage.clear();
  // The store is a module singleton and Normalize is *on* by default, so a case that
  // switches it off would otherwise hand the next case a player that starts off.
  usePlayerStore.setState({ normalize: true });
  usePlayerStore.getState().select(CALL);
});

afterEach(() => {
  removeWebAudio();
  resetAudioGraph();
  localStorage.clear();
  usePlayerStore.setState({ normalize: true });
});

/**
 * The player with its settings popover open: the switches live there since the phone
 * layout.
 */
async function renderWithSettings() {
  const result = renderWithProviders(<Player />, "/calls");
  await userEvent.click(await screen.findByRole("button", { name: "Player settings" }));
  return result;
}

describe("the Normalize toggle", () => {
  it("renders alongside the other player switches", async () => {
    installWebAudio();
    await renderWithSettings();
    expect(await screen.findByRole("switch", { name: /Normalize/ })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Continuous/ })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Auto-play new/ })).toBeInTheDocument();
  });

  it("is on by default, and claims the element as soon as a call is loaded", async () => {
    installWebAudio();
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    expect(toggle).toBeChecked();
    expect(usePlayerStore.getState().normalize).toBe(true);
    // Leveling is the default, so the one-way claim happens on load rather than on a
    // click. Exactly one context all the same: the trap is claiming *twice*, not early.
    expect(contexts).toHaveLength(1);
    expect(graph().sources).toBe(1);
  });

  it("honours a stored preference of off and builds nothing", async () => {
    // The default is on; a listener who has switched it off must not be claimed at all,
    // because a claimed element can never be handed back.
    localStorage.setItem("keyup-player", JSON.stringify({ normalize: false }));
    usePlayerStore.setState({ normalize: false });
    installWebAudio();
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    expect(toggle).not.toBeChecked();
    expect(contexts).toHaveLength(0);
  });

  it("remembers being switched off across a reload, and being switched back on", async () => {
    installWebAudio();
    const user = userEvent.setup();
    await renderWithSettings();
    // On by default, so the first click is the one that turns it *off* — and that is the
    // choice worth persisting, since the default would otherwise put it back every load.
    await user.click(await screen.findByRole("switch", { name: /Normalize/ }));
    expect(usePlayerStore.getState().normalize).toBe(false);
    expect(JSON.parse(localStorage.getItem("keyup-player") as string).normalize).toBe(false);
    await user.click(screen.getByRole("switch", { name: /Normalize/ }));
    expect(usePlayerStore.getState().normalize).toBe(true);
    expect(JSON.parse(localStorage.getItem("keyup-player") as string).normalize).toBe(true);
  });

  it("builds source -> compressor -> makeup -> brickwall -> ceiling -> tone on load", async () => {
    installWebAudio();
    await renderWithSettings();
    await screen.findByRole("switch", { name: /Normalize/ });

    expect(contexts).toHaveLength(1);
    expect(graph().sources).toBe(1);
    // The make-up gain is made safe before anything else sees it, and the tone dip is last
    // of all — it only attenuates, so it can never put a peak back over the ceiling.
    expect(chainFrom(nodesOfKind("source")[0] as FakeNode)).toEqual([
      "source",
      "compressor",
      "gain",
      "compressor",
      "gain",
      "gain",
      "destination",
    ]);
    expect(graph().compressorParams).toEqual({
      threshold: COMPRESSOR.threshold,
      knee: COMPRESSOR.knee,
      ratio: COMPRESSOR.ratio,
      attack: COMPRESSOR.attack,
      release: COMPRESSOR.release,
    });
    // Make-up gain: enough to come out level with the bypass, and inside the
    // clamp so it cannot turn into an earful.
    expect(graph().gain).toBeCloseTo(10 ** (MAKEUP_DB / 20), 5);
    expect(MAKEUP_DB).toBeLessThanOrEqual(MAX_MAKEUP_DB);
    expect(graph().gain).toBeLessThanOrEqual(10 ** (MAX_MAKEUP_DB / 20));
    // …and the brickwall that makes that lift safe: hard knee, fast attack, top ratio.
    expect(graph().brickwallParams).toEqual({
      threshold: BRICKWALL.threshold,
      knee: BRICKWALL.knee,
      ratio: BRICKWALL.ratio,
      attack: BRICKWALL.attack,
      release: BRICKWALL.release,
    });
    expect(BRICKWALL.attack).toBeLessThan(COMPRESSOR.attack);
    // The output trim, on the second gain node; the tone dip is the third and starts at 1.
    expect(graph().gains.gain1).toBeCloseTo(OUTPUT_CEILING, 6);
    expect(graph().gains.gain2).toBe(1);
    // The whole point: even a full-scale sample cannot reach 1.0 at the destination.
    expect(OUTPUT_CEILING).toBeLessThan(1);
    // The context is woken whenever leveling is switched on — on load here, and inside the
    // click when a listener turns it back on, which is the gesture the autoplay policy wants.
    expect(graph().resumes).toBeGreaterThan(0);
  });

  it("switching off bypasses the compressor rather than tearing the graph down", async () => {
    installWebAudio();
    const user = userEvent.setup();
    await renderWithSettings();
    // On by default: one click is off.
    await user.click(await screen.findByRole("switch", { name: /Normalize/ }));

    // Same context, same source: the element can never be handed back.
    expect(contexts).toHaveLength(1);
    expect(graph().sources).toBe(1);
    expect(chainFrom(nodesOfKind("source")[0] as FakeNode)).toEqual(["source", "destination"]);
    // Dangling nodes would be silence, so the compressor path is disconnected.
    expect((nodesOfKind("compressor")[0] as FakeNode).connectedTo).toHaveLength(0);
  });

  it("re-enabling restores the compressor path without a second source", async () => {
    installWebAudio();
    const user = userEvent.setup();
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    await user.click(toggle); // off
    await user.click(toggle); // and on again
    expect(contexts).toHaveLength(1);
    expect(graph().sources).toBe(1);
    expect(chainFrom(nodesOfKind("source")[0] as FakeNode)).toEqual([
      "source",
      "compressor",
      "gain",
      "compressor",
      "gain",
      "gain",
      "destination",
    ]);
  });

  it("leaves element volume and mute where they were", async () => {
    installWebAudio();
    const { container } = await renderWithSettings();
    await screen.findByRole("switch", { name: /Normalize/ });
    act(() => {
      usePlayerStore.getState().setVolume(0.25);
      usePlayerStore.getState().toggleMute();
    });
    const audio = container.querySelector("audio") as HTMLAudioElement;
    expect(audio.volume).toBeCloseTo(0.25);
    expect(audio.muted).toBe(true);
  });
});

describe("without Web Audio", () => {
  it("offers the toggle disabled and never throws", async () => {
    // jsdom has no `AudioContext` at all, which is the case this covers.
    removeWebAudio();
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
  });

  it("survives a stored preference of on with no Web Audio to honour it", async () => {
    removeWebAudio();
    usePlayerStore.setState({ normalize: true });
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    // Nothing crashed, and the switch does not claim to be leveling anything.
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
  });

  it("survives a browser whose AudioContext constructor throws", async () => {
    // The graph is attempted at load now, not on a click, so this is the throw a listener
    // meets on the very first render — and it must cost them nothing but the leveling.
    (globalThis as { AudioContext?: unknown }).AudioContext = class {
      constructor() {
        throw new Error("no audio hardware");
      }
    };
    const user = userEvent.setup();
    await renderWithSettings();
    const toggle = await screen.findByRole("switch", { name: /Normalize/ });
    // Nothing crashed, and the preference is still what it was: the player simply plays
    // natively, because nothing was ever rewired.
    expect(usePlayerStore.getState().normalize).toBe(true);
    // …and the switch still works, in the direction that needs no audio graph at all.
    await user.click(toggle);
    expect(usePlayerStore.getState().normalize).toBe(false);
  });
});
