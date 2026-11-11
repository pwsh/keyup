/**
 * The live feed while something is playing — on both pages that have one.
 *
 * The report was that the live view keeps working right up until the first call is
 * played, on the Calls page and on the Live page alike. Whatever else a phone does, the
 * one thing that must never happen is the app tearing its own subscription down: the
 * socket belongs above the player in the tree and its effect must not depend on any
 * player state, so selecting, playing, pausing and walking the queue leave it untouched
 * and events keep landing on the list.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetLiveTransport } from "../../api/live";
import type { Call } from "../../api/types";
import { Calls } from "../../pages/Calls";
import { Live } from "../../pages/Live";
import { usePlayerStore } from "../../state/player";
import { Player } from "../player/Player";
import { TooltipProvider } from "../ui/Tooltip";

// The suite runs with `VITE_MOCK=1`, which would send the feed down the fixture
// replayer instead of opening a socket at all. The data hooks are unaffected —
// `client.ts` reads its own copy — so the pages still render against the fixtures.
vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  isMockMode: () => false,
}));

class StubSocket {
  static instances: StubSocket[] = [];
  static closes = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  readyState = 0;
  constructor(readonly url: string) {
    StubSocket.instances.push(this);
  }
  send(): void {}
  close(): void {
    StubSocket.closes += 1;
    this.readyState = 3;
  }
  connect(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

class StubEventSource {
  static instances: StubEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readonly listeners = new Map<string, (message: MessageEvent) => void>();
  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }
  addEventListener(name: string, handler: (message: MessageEvent) => void): void {
    this.listeners.set(name, handler);
  }
  close(): void {}
}

const CALL = (id: number): Call =>
  ({
    id,
    talkgroup_id: 1,
    talkgroup_alias: `Dispatch ${id}`,
    duration_s: 4,
    started_at: new Date(1_900_000_000_000 + id * 1_000).toISOString(),
    transcript_status: "done",
    transcript_quality: "good",
    transcript_text: null,
    radio_label: "Unit 1",
    has_audio: true,
    starred: false,
  }) as unknown as Call;

beforeEach(() => {
  resetLiveTransport();
  StubSocket.instances = [];
  StubSocket.closes = 0;
  StubEventSource.instances = [];
  vi.stubGlobal("WebSocket", StubSocket);
  vi.stubGlobal("EventSource", StubEventSource);
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    writable: true,
    value: () => Promise.resolve(),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    writable: true,
    value: () => {},
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetLiveTransport();
});

function mount(page: "calls" | "live") {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <MemoryRouter initialEntries={[page === "calls" ? "/calls" : "/live"]}>
        <TooltipProvider>
          {page === "calls" ? <Calls /> : <Live />}
          <Player />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Play a call, pause it, and step to the next one — the whole reported sequence. */
async function playThrough(): Promise<void> {
  act(() => {
    usePlayerStore.getState().setQueue([CALL(1), CALL(2)]);
    usePlayerStore.getState().select(CALL(1));
  });
  await act(async () => {});
  act(() => usePlayerStore.getState().toggle());
  await act(async () => {});
  act(() => usePlayerStore.getState().next());
  await act(async () => {});
}

describe.each(["live", "calls"] as const)("the %s page's feed under playback", (page) => {
  it("keeps the one subscription it opened", async () => {
    mount(page);
    await act(async () => {});
    expect(StubSocket.instances).toHaveLength(1);
    act(() => StubSocket.instances[0]?.connect());

    await playThrough();

    // Not a second socket, and not a closed one: nothing about the player is allowed
    // to reach the subscription's effect.
    expect(StubSocket.instances).toHaveLength(1);
    expect(StubSocket.closes).toBe(0);
    expect(StubEventSource.instances).toHaveLength(0);
  });

  it("goes on delivering calls after something has been played", async () => {
    mount(page);
    await act(async () => {});
    const socket = StubSocket.instances[0];
    act(() => socket?.connect());

    await playThrough();

    act(() => socket?.emit({ type: "call.new", call: CALL(42) }));
    await act(async () => {});

    expect(await screen.findAllByText(/Dispatch 42/)).not.toHaveLength(0);
  });
});
