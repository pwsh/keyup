import { useCallback, useSyncExternalStore } from "react";
import { useMediaQuery } from "../lib/useMediaQuery";
import { stored, subscribe, write } from "./panelStore";

export { resetPanelsForTest } from "./panelStore";

/** Tailwind's `md`. Below it the sidebar does not exist at all. */
const DESKTOP = "(min-width: 768px)";

export const SIDEBAR_PANEL = "keyup-sidebar";
export const FILTER_RAIL_PANEL = "keyup-filters";
export const FACETS_PANEL = "keyup-facets";

function useStored(key: string): [boolean | null, (next: boolean) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => stored(key),
    () => null,
  );
  const setOpen = useCallback((next: boolean) => write(key, next), [key]);
  return [value, setOpen];
}

/**
 * Open/closed state for a panel: collapsed by default on a phone, where a rail
 * and a facet column would cover the results they describe, and open from `md`
 * up. Once the reader has said which they want, that wins on every width.
 */
export function usePanelOpen(key: string): [boolean, (next: boolean) => void] {
  const desktop = useMediaQuery(DESKTOP);
  const [value, setOpen] = useStored(key);
  return [value ?? desktop, setOpen];
}

/** Open/closed remembered with a fixed default, for a section inside a panel. */
export function useStoredOpen(key: string, defaultOpen: boolean): [boolean, (next: boolean) => void] {
  const [value, setOpen] = useStored(key);
  return [value ?? defaultOpen, setOpen];
}
