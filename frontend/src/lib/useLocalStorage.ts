import { useCallback, useState } from "react";

/**
 * A JSON value remembered in `localStorage`, for a per-reader UI preference — which
 * columns the calls table shows, whether the transcript gets its own row. It is
 * deliberately *not* for anything that changes the result set: filters live in the URL
 * so a view is shareable, while these are quiet conveniences that only ever matter to
 * the one browser that set them.
 *
 * Every access is guarded: private-mode browsers throw on read and on write, and the
 * hook simply falls back to `initial` and stops trying to remember.
 */
export function useLocalStorage<T>(key: string, initial: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* private mode: the preference just won't persist */
      }
    },
    [key],
  );

  return [value, set];
}
