import { Info } from "lucide-react";
import { type ReactNode, useEffect, useId, useState } from "react";
import { cn } from "../../lib/cn";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { Tip } from "../ui/Tooltip";

/** Where each page remembers whether its description is unfolded: `true`/`false` per title. */
export const pageInfoKey = (title: string) => `keyup-page-info:${title}`;

/** Wide enough for the description to sit beside the controls (Tailwind's `md`). */
const WIDE_QUERY = "(min-width: 768px)";

function isWide(): boolean {
  try {
    return window.matchMedia(WIDE_QUERY).matches;
  } catch {
    return true;
  }
}

/**
 * Open or folded, as this browser last left it. The first visit on a wide screen shows
 * the description once, so a new reader learns what the page is for; on a phone it starts
 * folded (the row is for the title and the actions) and the first wide visit still gets it.
 */
function readInitial(key: string): { open: boolean; firstVisit: boolean } {
  try {
    const raw = localStorage.getItem(key);
    if (raw !== null) return { open: raw === "true", firstVisit: false };
  } catch {
    return { open: false, firstVisit: false };
  }
  const wide = isWide();
  return { open: wide, firstVisit: wide };
}

function remember(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, String(open));
  } catch {
    /* private mode: the description just starts folded next time */
  }
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  useDocumentTitle(title);
  const key = pageInfoKey(title);
  const [initial] = useState(() => readInitial(key));
  const [open, setOpen] = useState(initial.open);
  const descriptionId = useId();
  // Shown once: from now on it opens only when the reader asks.
  useEffect(() => {
    if (initial.firstVisit) remember(key, false);
  }, [initial.firstVisit, key]);

  const toggle = () => {
    setOpen(!open);
    remember(key, !open);
  };

  return (
    // One compact row (title, an (i) and the actions). The description folds behind the
    // (i): useful on the first visit, ~50 px of noise every day after. Folded, it stays in
    // the page as screen-reader text, so nobody loses it by not opening it.
    <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 md:items-start md:gap-3 md:px-6 md:pb-3 md:pt-4">
      <div className="min-w-0">
        <div className="flex items-center gap-1">
          <h1 className="text-base font-semibold text-ink md:text-lg">{title}</h1>
          <Tip label={open ? "Hide what this page is for" : "What this page is for"}>
            <button
              type="button"
              aria-expanded={open}
              aria-controls={descriptionId}
              aria-label={`About ${title}`}
              onClick={toggle}
              className={cn(
                "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full hover:bg-surface-2 hover:text-ink",
                open ? "text-accent-ink" : "text-ink-2",
              )}
            >
              <Info size={15} aria-hidden="true" />
            </button>
          </Tip>
        </div>
        <p
          id={descriptionId}
          data-testid="page-description"
          className={cn("max-w-[70ch] text-xs text-ink-2", open ? "mt-0.5" : "sr-only")}
        >
          {description}
        </p>
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
