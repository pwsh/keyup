import { useSyncExternalStore } from "react";

const stores = new Map<string, { list: MediaQueryList; listeners: Set<() => void> }>();

function entry(query: string) {
  const existing = stores.get(query);
  if (existing) return existing;
  const created = { list: window.matchMedia(query), listeners: new Set<() => void>() };
  created.list.addEventListener("change", () => {
    for (const listener of created.listeners) listener();
  });
  stores.set(query, created);
  return created;
}

/** `true` while the media query matches; re-renders when that changes. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (listener) => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
      const store = entry(query);
      store.listeners.add(listener);
      return () => {
        store.listeners.delete(listener);
      };
    },
    () => {
      if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
      return entry(query).list.matches;
    },
    () => false,
  );
}
