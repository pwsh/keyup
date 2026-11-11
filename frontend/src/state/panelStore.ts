/**
 * The panels' shared store, without React or media queries, so the test setup can reset
 * it without loading anything a test file might mock.
 */
function read(key: string): boolean | null {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : raw === "open";
  } catch {
    return null;
  }
}

/**
 * One store for every panel, shared by every component that reads one.
 *
 * Each reader used to hold its own `useState` copy, so collapsing the sidebar with `[`
 * updated the sidebar but not the player, which kept laying itself out for the old
 * width and left a strip of the page showing. Like `state/theme.ts`, the value lives
 * once at module level and every subscriber re-renders together.
 */
const cache = new Map<string, boolean | null>();
const listeners = new Set<() => void>();

export function stored(key: string): boolean | null {
  if (!cache.has(key)) cache.set(key, read(key));
  return cache.get(key) ?? null;
}

export function write(key: string, next: boolean): void {
  cache.set(key, next);
  try {
    localStorage.setItem(key, next ? "open" : "closed");
  } catch {
    /* private mode: the panel just won't remember */
  }
  for (const l of listeners) l();
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Forget the cached values, so a test can start from what `localStorage` holds. */
export function resetPanelsForTest(): void {
  cache.clear();
  for (const l of listeners) l();
}
