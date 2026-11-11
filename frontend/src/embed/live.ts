/**
 * The embed's two conversations with the API, without the app's client.
 *
 * `fetchRecent` reads the first screen from `GET /api/transcripts/recent`; `connectLive`
 * tails `WS /api/live` with the filter that answer handed back, reconnecting with backoff
 * and falling back to `GET /api/live/sse` when a proxy in front of the site eats the
 * WebSocket upgrade — the same protocol and the same fallback rule as `src/api/live.ts`,
 * reimplemented in a few dozen lines because that module pulls in the app's client.
 */

/** One row as `GET /api/transcripts/recent` sends it. A live `call` object is a superset. */
export interface TranscriptRow {
  id: number;
  started_at: string;
  started_local: string;
  duration_s: number;
  tgid: number | null;
  talkgroup_alias: string | null;
  radio_label: string | null;
  transcript_text: string | null;
  transcript_status: string;
  transcript_quality: string | null;
}

export interface PresetChoice {
  slug: string;
  name: string;
}

export interface RecentAnswer {
  minutes: number;
  since: string;
  preset: string | null;
  filter: Record<string, unknown>;
  /** Pass back as `cursor` for the rows before this answer's last one; null at the end. */
  next_cursor?: string | null;
  items: TranscriptRow[];
  presets: PresetChoice[] | null;
}

/** A failed read, with the status so the page can tell "sign in" from "try again". */
export class RecentError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function fetchRecent(query: string, signal?: AbortSignal): Promise<RecentAnswer> {
  const response = await fetch(`/api/transcripts/recent?${query}`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* a body that is not JSON keeps the status text */
    }
    throw new RecentError(response.status, detail);
  }
  return (await response.json()) as RecentAnswer;
}

/** A frame the embed acts on. Everything else the server sends is ignored. */
export type LiveFrame =
  | { type: "call.new" | "call.transcribed" | "call.updated"; call: TranscriptRow }
  | { type: "call.superseded"; id: number; by: number | null };

const FRAME_TYPES = new Set(["call.new", "call.transcribed", "call.updated", "call.superseded"]);

function decode(raw: string): LiveFrame | null {
  try {
    const frame = JSON.parse(raw) as { type?: string; call?: unknown; id?: unknown };
    if (!frame.type || !FRAME_TYPES.has(frame.type)) return null;
    if (frame.type === "call.superseded") return typeof frame.id === "number" ? (frame as LiveFrame) : null;
    return frame.call && typeof frame.call === "object" ? (frame as LiveFrame) : null;
  } catch {
    return null;
  }
}

export type LiveState = "connecting" | "live" | "reconnecting";

export interface LiveHandlers {
  onFrame: (frame: LiveFrame) => void;
  onState: (state: LiveState) => void;
  /** The connection came back after a drop or a sleep: whatever happened meanwhile is lost. */
  onResume: () => void;
}

/** The filter as `/api/live/sse` query parameters: arrays repeat. */
function sseQuery(filter: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filter)) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== null && item !== undefined) params.append(key, String(item));
    }
  }
  return params.toString();
}

/** Two handshakes refused inside this window mean the socket is not coming up. */
const FAILURE_WINDOW_MS = 30_000;
const FAILURE_LIMIT = 2;
const MAX_BACKOFF_MS = 15_000;

/** Open the live feed. Returns the function that closes it for good. */
export function connectLive(filter: Record<string, unknown>, handlers: LiveHandlers): () => void {
  let closed = false;
  let everOpen = false;
  let socket: WebSocket | null = null;
  let source: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let failures: number[] = [];

  const opened = () => {
    attempt = 0;
    handlers.onState("live");
    if (everOpen) handlers.onResume();
    everOpen = true;
  };

  const startSse = () => {
    const events = new EventSource(`/api/live/sse?${sseQuery(filter)}`);
    source = events;
    events.onopen = opened;
    events.onerror = () => handlers.onState("reconnecting");
    for (const name of FRAME_TYPES) {
      events.addEventListener(name, (message) => {
        const frame = decode((message as MessageEvent).data as string);
        if (frame) handlers.onFrame(frame);
      });
    }
  };

  const connect = () => {
    if (closed) return;
    retry = undefined;
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${window.location.host}/api/live`);
    socket = ws;
    let wasOpen = false;
    ws.onopen = () => {
      wasOpen = true;
      // The server holds every event until this first frame names what we want.
      ws.send(JSON.stringify(filter));
      opened();
    };
    ws.onmessage = (message) => {
      const frame = decode(message.data as string);
      if (frame) handlers.onFrame(frame);
    };
    ws.onclose = () => {
      if (closed || socket !== ws) return;
      handlers.onState(everOpen ? "reconnecting" : "connecting");
      if (!wasOpen && document.visibilityState !== "hidden") {
        const now = Date.now();
        failures = [...failures, now].filter((at) => now - at < FAILURE_WINDOW_MS);
        if (failures.length >= FAILURE_LIMIT) {
          socket = null;
          startSse();
          return;
        }
      }
      attempt += 1;
      retry = setTimeout(connect, Math.min(MAX_BACKOFF_MS, 500 * 2 ** attempt));
    };
  };

  /** Back from a sleep or a lost network: reconnect now instead of after a frozen timer. */
  const wake = () => {
    if (closed || document.visibilityState === "hidden") return;
    if (source !== null || (socket !== null && socket.readyState === 1)) return;
    if (retry) clearTimeout(retry);
    attempt = 0;
    const stale = socket;
    socket = null;
    if (stale) {
      stale.onclose = null;
      stale.close();
    }
    connect();
  };

  handlers.onState("connecting");
  connect();
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("online", wake);

  return () => {
    closed = true;
    if (retry) clearTimeout(retry);
    document.removeEventListener("visibilitychange", wake);
    window.removeEventListener("online", wake);
    socket?.close();
    source?.close();
  };
}
