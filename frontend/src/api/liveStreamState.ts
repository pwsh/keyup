/**
 * App-wide live-stream state, kept apart from `live.ts` so it imports nothing but React:
 * the test setup resets it between tests, and importing `live.ts` there would load it
 * before a test file's `vi.mock("./client")` could take effect.
 */
import { useSyncExternalStore } from "react";
import type { ConnectionState } from "./types";

/**
 * What every mounted call feed is doing right now, keyed by the hook instance.
 *
 * The header pill used to report the *player's* "follow live" toggle and call that the
 * connection, so it read "Offline" on the Calls page while calls streamed in. Each
 * `useLiveFeed` now reports its real state here and the header reads the summary.
 */
const feedStates = new Map<string, ConnectionState>();
const feedListeners = new Set<() => void>();
const RANK: Record<ConnectionState, number> = { closed: 0, connecting: 1, mock: 2, open: 3 };

export function reportFeedState(id: string, state: ConnectionState | null): void {
  if (state === null || state === "closed") feedStates.delete(id);
  else feedStates.set(id, state);
  for (const listener of feedListeners) listener();
}

/** The best state any feed in the app is in: open beats connecting; none at all is "closed". */
export function liveStreamState(): ConnectionState {
  let best: ConnectionState = "closed";
  for (const state of feedStates.values()) if (RANK[state] > RANK[best]) best = state;
  return best;
}

export function subscribeLiveStreamState(listener: () => void): () => void {
  feedListeners.add(listener);
  return () => feedListeners.delete(listener);
}

/** The app's live-stream state, re-rendering when any feed opens, drops or closes. */
export function useLiveStreamState(): ConnectionState {
  return useSyncExternalStore(subscribeLiveStreamState, liveStreamState, () => "closed" as const);
}

/** Forget every reported feed, so one test's feed does not leak into the next. */
export function resetLiveStreamState(): void {
  feedStates.clear();
  for (const listener of feedListeners) listener();
}
