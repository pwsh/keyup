/**
 * The live feed's transport discovery.
 *
 * `WS /api/live` only works if every hop in front of the app forwards the upgrade. An
 * external reverse proxy that does not (see docs/REVERSE-PROXY.md) makes the handshake
 * fail over and over, and the page used to sit on "Reconnecting" for ever. Two failures
 * inside 30 s now switch the feed to `GET /api/live/sse`, which is plain HTTP.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseFilters, toApiParams } from "../state/filters";
import { liveTransport, liveTransportJournal, resetLiveTransport, useLiveFeed } from "./live";

// The suite runs with `VITE_MOCK=1`, which would send the hook down the fixture
// replayer instead of opening anything at all.
vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./client")>()),
  isMockMode: () => false,
}));

/** A socket that never opens — what a proxy refusing the upgrade looks like. */
class StubSocket {
  static instances: StubSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  /** `CONNECTING`; `connect` moves it to `OPEN` (1) and a close to `CLOSED` (3). */
  readyState = 0;
  /** Every frame the page sent — the first one is the filter. */
  readonly sent: string[] = [];
  constructor(readonly url: string) {
    StubSocket.instances.push(this);
  }
  send(frame: string): void {
    this.sent.push(frame);
  }
  close(): void {
    this.readyState = 3;
  }
  /** A handshake that succeeded. */
  connect(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  /** The browser fires `error` and then `close` on a handshake that was refused. */
  fail(): void {
    this.readyState = 3;
    this.onerror?.();
    this.onclose?.();
  }
  /** An established socket the network took away — a locked phone, a lost signal. */
  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

/** Drive `document.visibilityState`, which jsdom hard-codes to "visible". */
function setVisibility(value: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
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
  emit(name: string, payload: unknown): void {
    this.listeners.get(name)?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  resetLiveTransport();
  StubSocket.instances = [];
  StubEventSource.instances = [];
  vi.stubGlobal("WebSocket", StubSocket);
  vi.stubGlobal("EventSource", StubEventSource);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetLiveTransport();
});

/**
 * Runs the retry timer the socket path schedules after a failed handshake — the first
 * one is 1 s. Well short of the 30 s window, so the next failure still counts as
 * consecutive; advancing past the window would reset the tally instead.
 */
function runBackoff() {
  act(() => {
    vi.advanceTimersByTime(2_000);
  });
}

describe("the live feed's WebSocket fallback", () => {
  it("stays on the socket after a single failure", () => {
    renderHook(() => useLiveFeed({}, true));
    expect(StubSocket.instances).toHaveLength(1);

    act(() => StubSocket.instances[0]?.fail());
    runBackoff();

    // A second socket, not an EventSource: one failure is an outage, not a broken proxy.
    expect(liveTransport()).toBe("websocket");
    expect(StubEventSource.instances).toHaveLength(0);
    expect(StubSocket.instances.length).toBeGreaterThan(1);
  });

  it("switches to the SSE endpoint after two failures inside the window", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));

    act(() => StubSocket.instances[0]?.fail());
    runBackoff();
    act(() => StubSocket.instances[1]?.fail());

    expect(liveTransport()).toBe("sse");
    expect(StubEventSource.instances).toHaveLength(1);
    expect(StubEventSource.instances[0]?.url).toContain("/api/live/sse");
    expect(result.current.state).toBe("connecting");
  });

  it("feeds calls through once it has fallen back", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.fail());
    runBackoff();
    act(() => StubSocket.instances[1]?.fail());

    const source = StubEventSource.instances[0];
    act(() => source?.onopen?.());
    expect(result.current.state).toBe("open");

    act(() => source?.emit("call.new", { type: "call.new", call: { id: 7 } }));
    expect(result.current.events).toHaveLength(1);
    // Protocol chatter is dropped on this path exactly as it is on the socket's.
    act(() => source?.emit("call.new", { type: "filter.ok" }));
    expect(result.current.events).toHaveLength(1);
  });

  it("does not walk back into the socket once it has given up on it", () => {
    const first = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.fail());
    runBackoff();
    act(() => StubSocket.instances[1]?.fail());
    first.unmount();

    const opened = StubSocket.instances.length;
    renderHook(() => useLiveFeed({ talkgroup: [1] }, true));

    expect(StubSocket.instances).toHaveLength(opened);
    expect(StubEventSource.instances).toHaveLength(2);
  });
});

/**
 * What a phone does to a live feed.
 *
 * The reported fault was "the live view breaks as soon as you play anything", and the
 * mechanism is not React at all: playing a call is what makes somebody put the phone
 * down, and iOS keeps the audio running while it suspends the page's networking. The
 * socket dies, and the backoff `setTimeout` that was meant to bring it back is frozen
 * with everything else. These cases pin the two rules that follow: come back the moment
 * the page does, and never mistake a suspension for a broken reverse proxy.
 */
describe("the live feed across a suspended page", () => {
  afterEach(() => setVisibility("visible"));

  it("reconnects the moment the page comes back, without waiting for the backoff", () => {
    renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    expect(StubSocket.instances).toHaveLength(1);

    setVisibility("hidden");
    act(() => StubSocket.instances[0]?.drop());
    // The backoff timer has not run and must not have to: nothing has advanced.
    expect(StubSocket.instances).toHaveLength(1);

    setVisibility("visible");
    expect(StubSocket.instances).toHaveLength(2);
  });

  it("tells the page it resumed, so it can refetch what the stream never replayed", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    expect(result.current.resumedAt).toBeNull();

    setVisibility("hidden");
    act(() => StubSocket.instances[0]?.drop());
    setVisibility("visible");
    // The refetch is asked for once the replacement is up and new arrivals flow again,
    // so nothing can fall between the refetch and the reopen.
    expect(result.current.resumedAt).toBeNull();
    act(() => StubSocket.instances[1]?.connect());

    expect(result.current.resumedAt).not.toBeNull();
  });

  it("asks for a refetch at once when the socket survived the sleep", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());

    setVisibility("hidden");
    setVisibility("visible");

    expect(StubSocket.instances).toHaveLength(1);
    expect(result.current.resumedAt).not.toBeNull();
  });

  it("does not blame the proxy for sockets that died while the page was hidden", () => {
    renderHook(() => useLiveFeed({}, true));
    setVisibility("hidden");

    // Two handshakes refused in a row — the exact pattern that latches the app onto
    // SSE for the rest of the session. A radio that is off is not a broken proxy.
    act(() => StubSocket.instances[0]?.fail());
    runBackoff();
    act(() => StubSocket.instances[1]?.fail());

    expect(liveTransport()).toBe("websocket");
    expect(StubEventSource.instances).toHaveLength(0);
  });

  it("leaves a socket that survived the sleep alone", () => {
    renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());

    setVisibility("hidden");
    setVisibility("visible");

    expect(StubSocket.instances).toHaveLength(1);
  });

  it("comes back from the bfcache", () => {
    renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    act(() => StubSocket.instances[0]?.drop());

    const restored = new Event("pageshow");
    Object.defineProperty(restored, "persisted", { value: true });
    act(() => {
      window.dispatchEvent(restored);
    });

    expect(StubSocket.instances.length).toBeGreaterThan(1);
  });

  it("keeps a journal of transitions for the badge's tooltip", () => {
    renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    setVisibility("hidden");
    act(() => StubSocket.instances[0]?.drop());
    setVisibility("visible");

    const notes = liveTransportJournal().map((entry) => entry.note);
    expect(notes).toContain("woke from a suspended page");
    expect(notes).toContain("websocket open");
  });
});

/**
 * The filter frame is the page's params, and the server applies every bound in it
 * to each arrival: a `date_to` frozen at page load kept every later call off the feed.
 */
describe("the live filter frame", () => {
  const frameFor = (query: string) => {
    const params = toApiParams(parseFilters(new URLSearchParams(query)));
    renderHook(() => useLiveFeed(params, true));
    act(() => StubSocket.instances[0]?.connect());
    return JSON.parse(StubSocket.instances[0]?.sent[0] ?? "null") as Record<string, unknown>;
  };

  it.each(["24h", "today", "7d"])("carries a start and no end for the rolling %s preset", (preset) => {
    const frame = frameFor(`preset=${preset}`);
    expect(frame.date_from).toBeTruthy();
    expect(frame).not.toHaveProperty("date_to");
  });

  it("carries a custom range exactly as given", () => {
    const frame = frameFor("preset=custom&date_from=2026-01-01&date_to=2026-01-31");
    expect(frame.date_from).toBe("2026-01-01");
    expect(frame.date_to).toBe("2026-01-31");
  });
});

/** A frame from the server, the way the browser hands it to `onmessage`. */
function serverSends(socket: StubSocket | undefined, frame: unknown): void {
  act(() => socket?.onmessage?.({ data: JSON.stringify(frame) }));
}

/**
 * A stream replays nothing, and it is not only a suspended phone that
 * loses events: an API restart, a Wi-Fi blip, the server's own LISTEN reconnecting. Each
 * one used to leave a hole in the Calls and Live pages until a reload.
 */
describe("the live feed's backfill signals", () => {
  it("does not ask for a refetch on the first open", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    expect(result.current.resumedAt).toBeNull();
  });

  it("asks for a refetch whenever the socket reopens after an ordinary drop", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());
    act(() => StubSocket.instances[0]?.drop());
    runBackoff();
    expect(result.current.resumedAt).toBeNull();

    act(() => StubSocket.instances[1]?.connect());
    const first = result.current.resumedAt;
    expect(first).not.toBeNull();

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    act(() => StubSocket.instances[1]?.drop());
    runBackoff();
    act(() => StubSocket.instances[2]?.connect());
    expect(result.current.resumedAt).toBeGreaterThan(first as number);
  });

  it("asks for a refetch when the server says it lost events", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    const socket = StubSocket.instances[0];
    act(() => socket?.connect());

    serverSends(socket, { type: "gap", reason: "reconnected" });

    expect(result.current.resumedAt).not.toBeNull();
    // Protocol, not feed content.
    expect(result.current.events).toHaveLength(0);
  });

  it("hears a gap on the server-sent events path too", () => {
    const { result } = renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.fail());
    runBackoff();
    act(() => StubSocket.instances[1]?.fail());
    const source = StubEventSource.instances[0];
    act(() => source?.onopen?.());

    act(() => source?.emit("gap", { type: "gap", reason: "overflow" }));

    expect(result.current.resumedAt).not.toBeNull();
  });
});

describe("the live feed's watchdog", () => {
  it("reconnects a socket that has said nothing for three heartbeats", () => {
    renderHook(() => useLiveFeed({}, true));
    const socket = StubSocket.instances[0];
    act(() => socket?.connect());
    serverSends(socket, { type: "filter.ok", filter: {}, heartbeat_s: 2 });

    // Two beats of silence is a quiet system, not a dead socket.
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(StubSocket.instances).toHaveLength(1);

    // Past three (6 s) the next watchdog tick tears it down and dials again, even though
    // the socket still reports itself open: no `close` is coming for a half-open socket.
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(StubSocket.instances).toHaveLength(2);
    expect(socket?.readyState).toBe(3);
    expect(liveTransportJournal().map((entry) => entry.note)).toContain("no heartbeat; reconnecting");
  });

  it("leaves a socket alone while its heartbeats keep coming", () => {
    renderHook(() => useLiveFeed({}, true));
    const socket = StubSocket.instances[0];
    act(() => socket?.connect());
    serverSends(socket, { type: "filter.ok", filter: {}, heartbeat_s: 2 });

    for (let beat = 0; beat < 15; beat += 1) {
      act(() => {
        vi.advanceTimersByTime(2_000);
      });
      serverSends(socket, { type: "ingest.status", status: {} });
    }
    expect(StubSocket.instances).toHaveLength(1);
  });

  it("uses the default interval until the server names one", () => {
    renderHook(() => useLiveFeed({}, true));
    act(() => StubSocket.instances[0]?.connect());

    act(() => {
      vi.advanceTimersByTime(40_000);
    });
    expect(StubSocket.instances).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    expect(StubSocket.instances).toHaveLength(2);
  });
});

/** The Live page rendered the buffer, and a filter change kept it. */
describe("the live buffer across a filter change", () => {
  const call7 = { type: "call.new", call: { id: 7 } };

  it("clears when the filter changes", () => {
    const { result, rerender } = renderHook(
      (props: { filters: Record<string, unknown> }) => useLiveFeed(props.filters, true),
      { initialProps: { filters: {} } },
    );
    act(() => StubSocket.instances[0]?.connect());
    serverSends(StubSocket.instances[0], call7);
    expect(result.current.events).toHaveLength(1);

    rerender({ filters: { talkgroup: [1101] } });

    expect(result.current.events).toHaveLength(0);
  });

  it("keeps what it holds when only the rolling window moved under the same question", () => {
    const { result, rerender } = renderHook(
      (props: { filters: Record<string, unknown> }) => useLiveFeed(props.filters, true, "preset=24h"),
      { initialProps: { filters: { date_from: "2026-09-22T10:00:00Z" } } },
    );
    act(() => StubSocket.instances[0]?.connect());
    serverSends(StubSocket.instances[0], call7);

    rerender({ filters: { date_from: "2026-09-22T10:05:00Z" } });

    expect(result.current.events).toHaveLength(1);
  });

  it("keeps what it holds across a pause", () => {
    const { result, rerender } = renderHook((props: { enabled: boolean }) => useLiveFeed({}, props.enabled), {
      initialProps: { enabled: true },
    });
    act(() => StubSocket.instances[0]?.connect());
    serverSends(StubSocket.instances[0], call7);

    rerender({ enabled: false });
    rerender({ enabled: true });

    expect(result.current.events).toHaveLength(1);
  });
});
