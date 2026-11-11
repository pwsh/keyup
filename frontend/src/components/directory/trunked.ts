import { useMemo } from "react";
import { useSystems } from "../../api/hooks";

/**
 * Which systems Broadcastify **Calls** may publish, for the screens that show a
 * per-talkgroup Calls checkbox.
 *
 * Calls files every upload under a system id issued for a *trunked* system, so a
 * conventional one is refused by the gate whatever the checkbox says (docs/EXPORTERS.md).
 * Rather than let a box be ticked and then silently ignored, every place that renders one
 * asks here and shows it fixed off with the reason. `systems.is_trunked` is the API's own
 * answer — the operator's `trunked` column when it is set, a guess from the protocol when
 * it is not — so no screen re-derives it from a protocol string of its own.
 */
export function useTrunkedSystemIds(enabled = true): Set<number> {
  const systems = useSystems(enabled);
  return useMemo(
    () => new Set((systems.data ?? []).filter((row) => row.is_trunked).map((row) => row.id)),
    [systems.data],
  );
}

/** Why the Calls checkbox is fixed off, in the one wording every screen uses. */
export const NOT_TRUNKED_TIP =
  "This channel's system is not trunked, and Broadcastify Calls publishes trunked systems only. Mark the system trunked on the Systems list if that is wrong.";
