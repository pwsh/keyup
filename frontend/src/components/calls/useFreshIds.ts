import { useCallback, useEffect, useRef, useState } from "react";

/**
 * How long a just-arrived call keeps its amber key-up edge. Each call counts its own
 * time from its own arrival, so a busy channel does not cut the previous call short.
 */
export const HIGHLIGHT_MS = 4000;

const EMPTY_IDS: ReadonlySet<number> = new Set();

/**
 * The calls that arrived live in the last `HIGHLIGHT_MS`: `mark` them as they arrive,
 * `clear` on a new question. The set changes only on an arrival and when a call's time
 * runs out — one timer, for whichever runs out first — so a memoised row's `fresh` prop
 * flips exactly twice and every other row stays still.
 */
export function useFreshIds(): {
  freshIds: ReadonlySet<number>;
  mark: (ids: Iterable<number>) => void;
  clear: () => void;
} {
  const [freshIds, setFreshIds] = useState<ReadonlySet<number>>(EMPTY_IDS);
  /** When each fresh call stops being fresh (ms since the epoch). */
  const untilRef = useRef<Map<number, number>>(new Map());

  const mark = useCallback((ids: Iterable<number>) => {
    const until = Date.now() + HIGHLIGHT_MS;
    let added = false;
    for (const id of ids) {
      untilRef.current.set(id, until);
      added = true;
    }
    if (added) setFreshIds(new Set(untilRef.current.keys()));
  }, []);

  const clear = useCallback(() => {
    untilRef.current.clear();
    setFreshIds(EMPTY_IDS);
  }, []);

  useEffect(() => {
    if (freshIds.size === 0) return;
    const until = untilRef.current;
    const next = Math.min(...until.values());
    const timer = setTimeout(
      () => {
        const now = Date.now();
        for (const [id, at] of until) if (at <= now) until.delete(id);
        setFreshIds(until.size === 0 ? EMPTY_IDS : new Set(until.keys()));
      },
      Math.max(0, next - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [freshIds]);

  return { freshIds, mark, clear };
}
