/**
 * The simulated live feed. Real mode opens `WS /api/live`; in mock mode this
 * replays calls from the fixture corpus at a plausible rate, re-stamped to now,
 * so the Live page, the auto-play toggle and the "who's active" panel all have
 * something to do with no backend running.
 */
import type { QueryParams } from "../client";
import type { Call, LiveEvent } from "../types";
import { CALLS } from "./dataset";
import { filterCalls, toCall } from "./filter";

let counter = 0;

/** A fixture call, re-stamped as if it had just happened. */
function freshen(source: Call): Call {
  const endedAt = Date.now();
  const startedAt = endedAt - source.duration_s * 1000;
  const local = new Date(startedAt - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
  return {
    ...source,
    id: 900_000 + ++counter,
    started_at: new Date(startedAt).toISOString(),
    started_local: local,
    ended_at: new Date(endedAt).toISOString(),
    recorded_at: new Date(endedAt + 700).toISOString(),
    ingested_at: new Date(endedAt + 2000).toISOString(),
    updated_at: new Date(endedAt + 2000).toISOString(),
    audio_url: `/api/calls/${source.id}/audio`,
  };
}

/**
 * Emit `call.new` every few seconds, and an `ingest.status` heartbeat every
 * fifteen. Returns the unsubscribe the hook hands back to React.
 */
export function subscribeMockLive(params: QueryParams, onEvent: (event: LiveEvent) => void): () => void {
  const pool = filterCalls(params);
  const source = pool.length ? pool : CALLS;
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout>;

  const tick = () => {
    if (cancelled) return;
    const call = source[Math.floor(Math.random() * source.length)];
    if (call) {
      const fresh = freshen(toCall(call));
      onEvent({ type: "call.new", call: fresh });
      // The transcript lands a moment later, exactly as the worker delivers it.
      if (fresh.transcript_status === "done") {
        setTimeout(() => {
          if (!cancelled) onEvent({ type: "call.transcribed", call: fresh });
        }, 1500);
      }
    }
    timer = setTimeout(tick, 2500 + Math.random() * 6500);
  };

  timer = setTimeout(tick, 900);
  return () => {
    cancelled = true;
    clearTimeout(timer);
  };
}
