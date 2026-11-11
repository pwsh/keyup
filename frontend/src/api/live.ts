/**
 * The live feed. Real mode opens `WS /api/live` and sends the same filter object
 * `GET /calls` takes; mock mode replays the fixture corpus at a plausible rate.
 * Either way the caller gets a rolling array of calls and a connection state.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { apiUrl, isMockMode, liveSocketUrl, loadMockLive, loadMockSpectrum, toQueryParams } from "./client";
import { reportFeedState } from "./liveStreamState";
import type { ActivityEvent, CallFilterParams, ConnectionState, LiveEvent, SpectrumFrame } from "./types";

export {
  liveStreamState,
  resetLiveStreamState,
  subscribeLiveStreamState,
  useLiveStreamState,
} from "./liveStreamState";

const CALL_EVENTS = new Set(["call.new", "call.transcribed", "call.updated"]);

/**
 * The one call event with no call attached: reconciliation demoted a row, and the
 * server sends the two ids because the demoted copy no longer passes any filter.
 */
export const SUPERSEDED_EVENT = "call.superseded";

/** Narrows a decoded frame to the events `LiveEvent` describes. */
function isLiveEvent(frame: { type?: string }): frame is LiveEvent {
  if (frame.type === "ingest.status") return "status" in frame;
  if (frame.type === SUPERSEDED_EVENT) return "id" in frame;
  return typeof frame.type === "string" && CALL_EVENTS.has(frame.type) && "call" in frame;
}

/** How many calls the rolling feed keeps before dropping the oldest. */
export const LIVE_BUFFER = 200;

/**
 * The server's heartbeat interval until it says otherwise (`filter.ok` carries
 * `heartbeat_s`). `LIVE_STATUS_SECONDS` defaults to 15.
 */
export const DEFAULT_HEARTBEAT_S = 15;

/**
 * How many heartbeat intervals of silence mean the connection is dead rather than quiet.
 * The server sends `ingest.status` on a wall clock whatever the filter, so three missed
 * beats are not a slow system: a half-open socket (a NAT that forgot us, a proxy that
 * stopped forwarding) that no `close` event will ever report.
 */
export const WATCHDOG_BEATS = 3;

/** How often the watchdog looks. Coarse on purpose: it is a backstop, not a clock. */
const WATCHDOG_TICK_MS = 5_000;

/**
 * Protocol frames that are not feed content but that the hook acts on: the server lost
 * events on the way to us (`gap`), or acknowledged a filter and said how often it beats.
 */
interface ControlFrame {
  type?: string;
  heartbeat_s?: unknown;
}

/* ----------------------------------------------------------- transport */

/**
 * Which channel the live feed is actually using.
 *
 * `WS /api/live` is the primary one, but an external reverse proxy that forwards the
 * request without `proxy_http_version 1.1` and the `Upgrade`/`Connection` headers kills
 * the handshake — the socket closes without ever opening, over and over, and the page
 * sits on "Reconnecting" for ever. Two such failures close together are enough evidence
 * that the hop in front of us does not do WebSockets, so the feed gives up on the socket
 * and tails `GET /api/live/sse` instead, which is plain HTTP and survives any proxy.
 *
 * The decision is module-level on purpose: it outlives one hook's effect, so changing a
 * filter (which tears the connection down and back up) does not walk back into the socket
 * and start the whole discovery over. It is also what the header badge reads to say, in
 * its tooltip, that the app fell back.
 */
export type LiveTransport = "websocket" | "sse";

/** Two failed handshakes this close together mean the socket is not coming up. */
const WS_FAILURE_WINDOW_MS = 30_000;
const WS_FAILURE_LIMIT = 2;

let transport: LiveTransport = "websocket";
let socketFailures: number[] = [];
const transportListeners = new Set<() => void>();

export function liveTransport(): LiveTransport {
  return transport;
}

export function subscribeLiveTransport(listener: () => void): () => void {
  transportListeners.add(listener);
  return () => {
    transportListeners.delete(listener);
  };
}

/* ------------------------------------------------------- the transport journal */

/**
 * The last few things the transport did, newest first, for the header badge's tooltip.
 *
 * A live feed that stops updating on a phone is otherwise completely opaque: the badge
 * says "Live" or "Connecting" and nothing says whether the socket dropped, whether the
 * page was suspended while audio played, or whether the app quietly fell back to SSE.
 * On a device with no developer console attached, this journal *is* the console.
 */
export interface LiveTransportEntry {
  at: number;
  note: string;
}

/** Short on purpose: a tooltip, not a log file. */
const JOURNAL_LIMIT = 6;
let journal: LiveTransportEntry[] = [];

export function liveTransportJournal(): LiveTransportEntry[] {
  return journal;
}

function notifyTransport(): void {
  for (const listener of transportListeners) listener();
}

/** Record a transport transition and wake the badge. Repeats are collapsed. */
export function noteLiveTransport(note: string): void {
  if (journal[0]?.note === note) return;
  journal = [{ at: Date.now(), note }, ...journal].slice(0, JOURNAL_LIMIT);
  notifyTransport();
}

/** Puts the module back on the WebSocket path. For tests, and for a manual retry. */
export function resetLiveTransport(): void {
  transport = "websocket";
  socketFailures = [];
  journal = [];
  notifyTransport();
}

/** A socket that closed without ever opening. Two inside the window switches to SSE. */
function recordSocketFailure(): void {
  if (transport === "sse") return;
  const now = Date.now();
  socketFailures = [...socketFailures, now].filter((at) => now - at < WS_FAILURE_WINDOW_MS);
  if (socketFailures.length < WS_FAILURE_LIMIT) return;
  transport = "sse";
  noteLiveTransport("fell back to server-sent events");
  notifyTransport();
}

/* --------------------------------------------------- suspension and wake-ups */

/** `WebSocket.OPEN`, without depending on the constructor being the real one. */
const SOCKET_OPEN = 1;

/** Whether the document is hidden right now. Safe where there is no `document`. */
function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/** The frame names the SSE endpoint uses — the same `type`s the socket sends untyped. */
const SSE_EVENT_NAMES = [...CALL_EVENTS, SUPERSEDED_EVENT, "ingest.status", "gap"];

export interface LiveFeed {
  events: LiveEvent[];
  state: ConnectionState;
  /** Wall-clock of the last event, so the page can show "quiet for 4 min". */
  lastEventAt: number | null;
  /**
   * Bumped every time the feed may have missed something: it came back from a suspension
   * (the tab was hidden, the phone was locked while a call played, the page came out of
   * the bfcache, the network returned), it reopened after any drop, or the server said
   * events were lost on the way (a `gap` frame). A stream replays nothing, so whatever
   * arrived in between is simply gone; the pages watch this and refetch the window.
   */
  resumedAt: number | null;
  clear: () => void;
}

/**
 * `questionKey` names the question the buffer answers when that is not simply `filters`:
 * the pages pass their serialised filter state, so a rolling window whose start was re-read
 * from the clock on resume is the same question and keeps what the buffer holds, while a
 * new talkgroup or search clears it.
 */
export function useLiveFeed(filters: CallFilterParams, enabled: boolean, questionKey?: string): LiveFeed {
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("closed");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [resumedAt, setResumedAt] = useState<number | null>(null);
  // The filter object is rebuilt on every render; serializing it keeps the effect
  // from tearing the socket down and back up on each keystroke.
  const filterKey = JSON.stringify(toQueryParams(filters));
  const push = useRef((event: LiveEvent) => {
    setEvents((previous) => [event, ...previous].slice(0, LIVE_BUFFER));
    setLastEventAt(Date.now());
  }).current;
  const question = questionKey ?? filterKey;
  /** The question the buffer was filled under. */
  const bufferedFor = useRef(question);

  // A new filter is a new question. The buffer still holds the last one's calls, and a
  // page rendering it (Live) would go on showing talkgroups the reader just filtered out.
  // Only a *question* change clears it: pausing and resuming keeps what was heard.
  useEffect(() => {
    if (bufferedFor.current === question) return;
    bufferedFor.current = question;
    setEvents([]);
  }, [question]);

  useEffect(() => {
    if (!enabled) {
      setState("closed");
      return;
    }
    const params = JSON.parse(filterKey) as Record<string, string | number | boolean>;

    // `loadMockLive` is `null` in a production build without `VITE_MOCK=1`, which is
    // what keeps the fixture corpus out of this route's chunk.
    if (isMockMode() && loadMockLive !== null) {
      setState("mock");
      let stop = () => {};
      let cancelled = false;
      void loadMockLive().then(({ subscribeMockLive }) => {
        if (cancelled) return;
        stop = subscribeMockLive(params, push);
      });
      return () => {
        cancelled = true;
        stop();
      };
    }

    setState("connecting");
    let closed = false;
    // Reassigned when the socket path gives up and hands over to the SSE one, so the
    // effect's cleanup always tears down whichever connection is current.
    let stop = () => {};
    /**
     * Bring the connection back *now*, skipping whatever backoff is pending.
     *
     * Reassigned by whichever transport is current, for the same reason `stop` is.
     */
    let reconnectNow = (_force?: boolean): boolean => false;
    /**
     * The page was put to sleep — hidden, frozen, or bfcached — since the last wake-up.
     *
     * This is the mobile case the feed used to lose: a phone playing a call is a phone
     * whose screen locks, and iOS keeps the audio going while it suspends the page's
     * networking. The socket dies, the backoff `setTimeout` is frozen with it, and on
     * return nothing asks for a reconnection — the feed simply never comes back, which
     * reads exactly like "the live view breaks as soon as you play anything".
     *
     * It also has to gate `recordSocketFailure`: a handshake that fails because the
     * radio is off is not a reverse proxy eating the upgrade, and counting it would
     * latch the whole app onto SSE for the rest of the session.
     */
    let suspended = false;
    /**
     * Whether a connection has been up at least once under this filter. The first open
     * is the start of the feed; every later one is a *re*-open, and whatever was sent
     * while it was down never reached us.
     */
    let everOpened = false;
    /** Whether a connection is up right now — the only state the watchdog judges. */
    let live = false;
    /** When the last frame of any kind arrived: a heartbeat counts as much as a call. */
    let lastFrameAt = Date.now();
    let heartbeatMs = DEFAULT_HEARTBEAT_S * 1000;

    /** Tell the pages to refetch the window the stream may have skipped. */
    const resume = (why: string) => {
      noteLiveTransport(why);
      setResumedAt(Date.now());
    };

    /** A connection came up. After the first, it is a reopen and there is a hole. */
    const opened = (note: string) => {
      noteLiveTransport(note);
      setState("open");
      live = true;
      lastFrameAt = Date.now();
      if (everOpened) resume("reopened; refetching what was missed");
      everOpened = true;
    };

    /**
     * Everything the server sends, before it is narrowed to feed content: any frame
     * proves the connection is alive, and two kinds of protocol frame are acted on.
     */
    const received = (frame: ControlFrame) => {
      lastFrameAt = Date.now();
      if (frame.type === "gap") {
        resume("server lost events; refetching");
        return;
      }
      if (frame.type === "filter.ok" && typeof frame.heartbeat_s === "number" && frame.heartbeat_s > 0) {
        heartbeatMs = frame.heartbeat_s * 1000;
      }
    };

    // The fallback. The filter goes in the query string — SSE has no upstream channel
    // to send a first frame on — and is fixed for the life of the connection, which is
    // fine because a filter change re-runs this effect anyway.
    const startSse = () => {
      const source = new EventSource(apiUrl("/live/sse", params));
      // `EventSource` reconnects on its own and fires `open` again when it has, so a
      // reopen after a drop is caught here without any retry logic of ours.
      source.onopen = () => opened("server-sent events open");
      source.onerror = () => {
        live = false;
        setState("connecting");
      };
      reconnectNow = () => {
        // `EventSource` reconnects on its own, but a browser that froze the page can
        // leave it in a state it never retries out of. Replacing it is cheap.
        live = false;
        source.close();
        startSse();
        return true;
      };
      for (const name of SSE_EVENT_NAMES) {
        source.addEventListener(name, (message) => {
          try {
            const frame = JSON.parse((message as MessageEvent).data as string) as { type?: string };
            received(frame);
            if (!isLiveEvent(frame)) return;
            push(frame);
          } catch {
            /* a frame we cannot parse is a frame we ignore */
          }
        });
      }
      stop = () => source.close();
    };

    const startSocket = () => {
      let socket: WebSocket | null = null;
      let retry: ReturnType<typeof setTimeout> | undefined;
      let attempt = 0;
      let handshook = false;

      const connect = () => {
        if (closed) return;
        if (retry) {
          clearTimeout(retry);
          retry = undefined;
        }
        socket = new WebSocket(liveSocketUrl());
        socket.onopen = () => {
          handshook = true;
          attempt = 0;
          // The server expects the filter as the first frame, in `GET /calls` shape.
          socket?.send(JSON.stringify(params));
          opened("websocket open");
        };
        socket.onmessage = (message) => {
          try {
            const frame = JSON.parse(message.data as string) as { type?: string };
            received(frame);
            // The server also sends `filter.ok` acknowledgments and `error` frames.
            // They are protocol chatter, not feed content, and the feed's consumers
            // read `event.call` without checking — so only the documented event
            // types get through.
            if (!isLiveEvent(frame)) return;
            push(frame);
          } catch {
            /* a frame we cannot parse is a frame we ignore */
          }
        };
        socket.onerror = () => setState("connecting");
        socket.onclose = () => {
          if (closed) return;
          live = false;
          setState("connecting");
          // Closing without ever opening is a handshake something refused — almost
          // always a reverse proxy that does not forward the upgrade. A socket that
          // opened and then dropped is an ordinary outage and does not count, and
          // neither does one the operating system pulled out from under a suspended
          // page: see `suspended`.
          if (!handshook && !suspended && !documentHidden()) {
            noteLiveTransport("websocket handshake refused");
            recordSocketFailure();
          }
          handshook = false;
          if (transport === "sse") {
            startSse();
            return;
          }
          // Exponential backoff, capped at 15 s so a long outage does not hammer.
          attempt += 1;
          retry = setTimeout(connect, Math.min(15_000, 500 * 2 ** attempt));
        };
      };

      reconnectNow = (force = false) => {
        // A socket that is genuinely still open survived the sleep; leave it alone —
        // unless the watchdog has decided it only *looks* open.
        if (!force && socket !== null && socket.readyState === SOCKET_OPEN) return false;
        attempt = 0;
        live = false;
        const stale = socket;
        socket = null;
        // Detached first: the close we are about to cause is ours, not an outage,
        // and must not schedule a second backoff on top of the reconnection.
        if (stale) {
          stale.onopen = null;
          stale.onclose = null;
          stale.onerror = null;
          stale.onmessage = null;
          try {
            stale.close();
          } catch {
            /* already gone */
          }
        }
        connect();
        return true;
      };

      connect();

      stop = () => {
        if (retry) clearTimeout(retry);
        socket?.close();
      };
    };

    if (transport === "sse") startSse();
    else startSocket();

    /**
     * Coming back from a sleep. Reconnect at once rather than waiting out a backoff
     * timer that was frozen with the page, and tell the pages to refetch the window
     * they were away for — a stream has no replay, so those calls are otherwise lost.
     */
    const wake = () => {
      if (closed || documentHidden()) return;
      if (!suspended) return;
      suspended = false;
      noteLiveTransport("woke from a suspended page");
      // A fresh connection says so itself when it opens (`opened`), and the refetch is
      // better made then, once new arrivals are flowing again. A socket that survived
      // the sleep will not reopen, so that case asks for the refetch here.
      if (!reconnectNow()) resume("woke with the connection intact; refetching");
    };

    /**
     * The watchdog. A connection can die without a `close`: a NAT that dropped the
     * mapping, a proxy that stopped forwarding, a laptop lid. The server beats on a wall
     * clock whatever the filter, so a connection that has said nothing for
     * `WATCHDOG_BEATS` heartbeats is not quiet, it is gone — tear it down and reconnect.
     * A hidden page is left to the suspension logic, which owns that case.
     */
    const watchdog = setInterval(() => {
      if (closed || !live || documentHidden()) return;
      if (Date.now() - lastFrameAt <= heartbeatMs * WATCHDOG_BEATS) return;
      noteLiveTransport("no heartbeat; reconnecting");
      lastFrameAt = Date.now();
      reconnectNow(true);
    }, WATCHDOG_TICK_MS);

    const sleep = () => {
      suspended = true;
    };

    const onVisibility = () => {
      if (documentHidden()) sleep();
      else wake();
    };
    /** bfcache: `persisted` means the page was frozen whole and every socket is gone. */
    const onPageShow = (event: Event) => {
      if ((event as PageTransitionEvent).persisted) sleep();
      wake();
    };
    /** A network that has come back is the same situation as a page that has. */
    const onOnline = () => {
      sleep();
      wake();
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pageshow", onPageShow);
    window.addEventListener("pagehide", sleep);
    window.addEventListener("freeze", sleep);
    window.addEventListener("online", onOnline);

    return () => {
      closed = true;
      clearInterval(watchdog);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pageshow", onPageShow);
      window.removeEventListener("pagehide", sleep);
      window.removeEventListener("freeze", sleep);
      window.removeEventListener("online", onOnline);
      stop();
      setState("closed");
    };
  }, [enabled, filterKey, push]);

  const clear = useCallback(() => {
    setEvents([]);
  }, []);

  // Tell the header what this feed is doing; an unmounted feed stops counting.
  const feedId = useId();
  useEffect(() => {
    reportFeedState(feedId, state);
  }, [feedId, state]);
  useEffect(() => () => reportFeedState(feedId, null), [feedId]);

  return { events, state, lastEventAt, resumedAt, clear };
}

/* --------------------------------------------------------------- activity */

/** How many activity events the live tail keeps in memory before dropping the oldest. */
export const ACTIVITY_BUFFER = 500;

export interface ActivityStream {
  /** Live events, newest first — prepend these onto the backfilled page. */
  events: ActivityEvent[];
  state: ConnectionState;
  clear: () => void;
}

/**
 * Tails `GET /api/activity/stream` over an `EventSource`. Server-Sent Events, not a
 * WebSocket: the feed is one-way and the browser reconnects on its own, so this hook only
 * owns decoding, a bounded rolling buffer and the connection badge. In mock mode there is
 * no stream — the page's backfill query carries the fixtures — so it simply reports "mock".
 */
export function useActivityStream(enabled: boolean): ActivityStream {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [state, setState] = useState<ConnectionState>("closed");
  const push = useRef((event: ActivityEvent) => {
    setEvents((previous) => {
      if (previous.some((existing) => existing.id === event.id)) return previous;
      return [event, ...previous].slice(0, ACTIVITY_BUFFER);
    });
  }).current;

  useEffect(() => {
    if (!enabled) {
      setState("closed");
      return;
    }
    if (isMockMode()) {
      setState("mock");
      return;
    }
    setState("connecting");
    const source = new EventSource(apiUrl("/activity/stream"));
    source.onopen = () => setState("open");
    source.onerror = () => setState("connecting");
    source.addEventListener("activity", (message) => {
      try {
        push(JSON.parse((message as MessageEvent).data as string) as ActivityEvent);
      } catch {
        /* a frame we cannot parse is a frame we ignore */
      }
    });
    return () => {
      source.close();
      setState("closed");
    };
  }, [enabled, push]);

  const clear = useCallback(() => setEvents([]), []);
  return { events, state, clear };
}

/* --------------------------------------------------------------- recorders */

/** One `tuner.status` frame: identifiers only, so the page re-reads the recorder. */
export interface TunerStatusEvent {
  recorder_id: number;
}

/**
 * Tails `GET /api/admin/recorders/stream` over an `EventSource`. Each `tuner.status` frame
 * carries only a `recorder_id`; the caller's `onEvent` re-reads `GET /admin/recorders`
 * (like the activity feed re-reads its row). No stream in mock mode.
 */
export function useRecorderStream(
  enabled: boolean,
  onEvent: (event: TunerStatusEvent) => void,
): ConnectionState {
  const [state, setState] = useState<ConnectionState>("closed");
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (!enabled) {
      setState("closed");
      return;
    }
    if (isMockMode()) {
      setState("mock");
      return;
    }
    setState("connecting");
    const source = new EventSource(apiUrl("/admin/recorders/stream"));
    source.onopen = () => setState("open");
    source.onerror = () => setState("connecting");
    source.addEventListener("tuner.status", (message) => {
      try {
        handler.current(JSON.parse((message as MessageEvent).data as string) as TunerStatusEvent);
      } catch {
        /* a frame we cannot parse is a frame we ignore */
      }
    });
    return () => {
      source.close();
      setState("closed");
    };
  }, [enabled]);

  return state;
}

/* --------------------------------------------------------------- spectrum */

export interface SpectrumFeed {
  /** The most recent FFT frame, or `null` before the first one arrives. */
  frame: SpectrumFrame | null;
  state: ConnectionState;
}

/**
 * Tails `GET /api/admin/recorders/spectrum/stream?instance=&tuner=` over an `EventSource`,
 * keeping only the latest frame — a waterfall consumes each frame once and forgets it, so
 * there is no buffer to keep. The stream is opened **only while `enabled`** (the spectrum
 * view is actually on screen), so nothing streams when nobody is looking. In mock mode it
 * replays synthetic frames instead. Passing an empty `instance`/`tuner` keeps it closed.
 */
export function useSpectrumStream(instance: string, tuner: string, enabled: boolean): SpectrumFeed {
  const [frame, setFrame] = useState<SpectrumFrame | null>(null);
  const [state, setState] = useState<ConnectionState>("closed");
  const active = enabled && instance !== "" && tuner !== "";

  useEffect(() => {
    // A new tuner selection is a new signal — drop the frame the old one left behind.
    setFrame(null);
    if (!active) {
      setState("closed");
      return;
    }

    if (isMockMode() && loadMockSpectrum !== null) {
      setState("mock");
      let stop = () => {};
      let cancelled = false;
      void loadMockSpectrum().then(({ subscribeMockSpectrum }) => {
        if (cancelled) return;
        stop = subscribeMockSpectrum(instance, tuner, setFrame);
      });
      return () => {
        cancelled = true;
        stop();
      };
    }

    setState("connecting");
    const source = new EventSource(apiUrl("/admin/recorders/spectrum/stream", { instance, tuner }));
    source.onopen = () => setState("open");
    source.onerror = () => setState("connecting");
    source.addEventListener("spectrum", (message) => {
      try {
        setFrame(JSON.parse((message as MessageEvent).data as string) as SpectrumFrame);
      } catch {
        /* a frame we cannot parse is a frame we ignore */
      }
    });
    return () => {
      source.close();
      setState("closed");
    };
  }, [active, instance, tuner]);

  return { frame, state };
}
