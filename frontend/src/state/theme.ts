import { useSyncExternalStore } from "react";

export type ThemeMode = "light" | "dark";
const STORAGE_KEY = "keyup-theme";
const listeners = new Set<() => void>();

function read(): ThemeMode {
  if (typeof document === "undefined") return "light";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

let current: ThemeMode = read();

/** Paint a theme without recording it as the reader's own choice. */
function apply(mode: ThemeMode): void {
  if (current === mode && document.documentElement.dataset.theme === mode) return;
  current = mode;
  document.documentElement.dataset.theme = mode;
  for (const l of listeners) l();
}

/** The theme the reader picked, or null while they are still following the OS. */
export function storedTheme(): ThemeMode | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    // Private mode throws rather than returning null.
    return null;
  }
}

export function setTheme(mode: ThemeMode): void {
  apply(mode);
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* private mode: the theme just won't persist */
  }
}

/**
 * Follow the operating system for as long as the reader has not chosen.
 *
 * `index.html` reads `prefers-color-scheme` once, before the app loads, and then
 * nothing looked at it again — so a laptop that switches to dark at sunset left this
 * one tab bright until it was reloaded. This keeps listening.
 *
 * An explicit choice always wins: the moment `keyup-theme` is in `localStorage` the OS
 * stops being consulted, which is what "I picked light" has to mean even on a machine
 * that is in dark mode. Returns its own teardown so a React effect can hand it back.
 */
export function watchSystemTheme(): () => void {
  const media = globalThis.matchMedia?.("(prefers-color-scheme: dark)");
  if (!media) return () => {};
  const onChange = (event: MediaQueryListEvent | MediaQueryList) => {
    if (storedTheme() !== null) return;
    apply(event.matches ? "dark" : "light");
  };
  // `addListener` is the deprecated spelling; Safari below 14 has only that one.
  if (media.addEventListener) {
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }
  media.addListener(onChange);
  return () => media.removeListener(onChange);
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Current theme, re-rendering the component when it changes. */
export function useTheme(): ThemeMode {
  return useSyncExternalStore(
    subscribe,
    () => current,
    () => "light" as ThemeMode,
  );
}
