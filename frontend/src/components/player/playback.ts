import { formatDuration } from "../../lib/format";
import { usePlayerStore } from "../../state/player";

/**
 * How long a call is, from one source for every screen that says so.
 *
 * The detail page used to print the recorder's `duration_s` ("7.0s") while the player
 * printed the `<audio>` element's own length, floored to whole seconds ("0:06"), for the
 * same call at the same moment. Now: while the call is loaded in the player
 * and the element has reported a length, that length — it is what the seek bar spans,
 * and the processed copy can be a little shorter than the recording; otherwise the
 * recorder's.
 */
export function playbackDuration(
  call: { id: number; duration_s: number },
  state: { current: { id: number } | null; duration: number },
): number {
  if (state.current?.id === call.id && state.duration > 0 && Number.isFinite(state.duration)) {
    return state.duration;
  }
  return call.duration_s;
}

/** {@link playbackDuration}, kept current. */
export function usePlaybackDuration(call: { id: number; duration_s: number } | null | undefined): number {
  return usePlayerStore((state) => (call ? playbackDuration(call, state) : 0));
}

/** The one format for a playhead and a length: "6.9s" under a minute, "1:05" above. */
export const formatPlayTime = (seconds: number): string => formatDuration(Math.max(0, seconds));
