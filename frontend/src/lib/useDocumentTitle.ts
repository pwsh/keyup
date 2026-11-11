import { useEffect } from "react";

export const SITE_TITLE = "Keyup";

/** "Calls" → "Calls · Keyup"; parts that are empty are skipped; nothing at all is just "Keyup". */
export function documentTitle(...parts: (string | null | undefined)[]): string {
  return [...parts.filter((part): part is string => Boolean(part?.trim())), SITE_TITLE].join(" · ");
}

/**
 * The browser tab's title for the page on screen (WCAG 2.4.2).
 *
 * Every page used to be titled "Keyup", so a reader with five tabs open — or a screen
 * reader announcing the page after navigation — could not tell them apart. `PageHeader`
 * calls this with its own title; pages without one (a call, sign-in, not found) call it
 * directly. Leaving the page puts the bare site name back, so a route that sets nothing
 * never inherits the previous page's title.
 */
export function useDocumentTitle(...parts: (string | null | undefined)[]): void {
  const title = documentTitle(...parts);
  useEffect(() => {
    document.title = title;
  }, [title]);
  useEffect(
    () => () => {
      document.title = SITE_TITLE;
    },
    [],
  );
}
