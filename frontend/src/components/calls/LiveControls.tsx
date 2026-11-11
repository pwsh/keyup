/**
 * The two bits of chrome the live list needs: a quiet indicator with the pause
 * toggle, and the pill that appears when calls have arrived above where the reader
 * is looking.
 */
import { Pause, Play, WifiOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { formatNumber } from "../../lib/format";
import { Tip } from "../ui/Tooltip";
import type { LiveStatus } from "./useLiveCalls";

const LABEL: Record<LiveStatus, string> = {
  live: "Live",
  reconnecting: "Reconnecting",
  paused: "Paused",
};

const STATUS_HELP: Record<LiveStatus, string> = {
  live: HELP["calls.live.live"],
  reconnecting: HELP["calls.live.reconnecting"],
  paused: HELP["calls.live.paused"],
};

/**
 * "Live / reconnecting / paused", and the button that stops the list moving at all.
 *
 * Deliberately small and grey: this says whether the page is telling the truth about
 * the archive, which matters, but it is not what anybody came to the page to read.
 */
export function LiveIndicator({
  status,
  paused,
  onPausedChange,
  reason,
  className,
}: {
  status: LiveStatus;
  paused: boolean;
  onPausedChange: (next: boolean) => void;
  /** Why the feed is paused when the reader did not pause it — a sort or a page. */
  reason?: string | null;
  className?: string;
}) {
  // Live is squelch amber, the colour reserved for it; reconnecting is a neutral wait.
  const tone =
    status === "live"
      ? "border-squelch text-squelch-ink"
      : status === "reconnecting"
        ? "border-control-border text-ink-2"
        : "border-line text-ink-2";
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <Tip label={reason ? reason : STATUS_HELP[status]}>
        <span
          tabIndex={0}
          role="status"
          aria-label={`Live updates: ${LABEL[status]}`}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium",
            tone,
          )}
        >
          {status === "paused" ? (
            <WifiOff size={11} aria-hidden="true" />
          ) : (
            <span
              className={cn(
                "inline-block h-1.5 w-1.5 rounded-full",
                status === "live" ? "bg-squelch" : "bg-muted",
              )}
              aria-hidden="true"
            />
          )}
          {LABEL[status]}
        </span>
      </Tip>
      <Tip label={paused ? HELP["calls.live.resume"] : HELP["calls.live.pause"]}>
        <button
          type="button"
          aria-pressed={paused}
          aria-label={paused ? "Resume live updates" : "Pause live updates"}
          onClick={() => onPausedChange(!paused)}
          // 24 px square (WCAG 2.5.8): the 13 px glyph with p-1 was 21 px.
          className="inline-flex size-6 items-center justify-center rounded text-ink-2 hover:bg-surface-2 hover:text-ink"
        >
          {paused ? <Play size={13} aria-hidden="true" /> : <Pause size={13} aria-hidden="true" />}
        </button>
      </Tip>
    </span>
  );
}

/** The shortest gap between two "N new calls" announcements. */
export const ANNOUNCE_INTERVAL_MS = 10_000;

const newCallsText = (count: number): string =>
  `${formatNumber(count)} new ${count === 1 ? "call" : "calls"}`;

/**
 * The words a screen reader should hear for `count`, changing at most once every
 * {@link ANNOUNCE_INTERVAL_MS}. On a busy system the count moves every second or
 * two; a live region that said every one of them would talk over whatever the
 * reader was listening to. The first arrival is said at once, later ones are held
 * and the latest count said when the interval is up. Back to zero (the reader
 * jumped to the top) clears the text without a word.
 */
export function useThrottledAnnouncement(count: number, interval = ANNOUNCE_INTERVAL_MS): string {
  const [text, setText] = useState("");
  const lastAt = useRef<number | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const clear = () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    };
    if (count <= 0) {
      clear();
      setText("");
      return;
    }
    const say = () => {
      timer.current = null;
      lastAt.current = Date.now();
      setText(newCallsText(count));
    };
    const wait = lastAt.current === null ? 0 : lastAt.current + interval - Date.now();
    clear();
    if (wait <= 0) say();
    else timer.current = window.setTimeout(say, wait);
  }, [count, interval]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return text;
}

/**
 * "N new calls", above the list.
 *
 * It exists because the alternative — scrolling the reader to the new rows — is the
 * thing the operator explicitly did not want. The count is what arrived while they
 * were reading something else; pressing it is them asking to go and look.
 *
 * On a wide screen (the table, `lg` and up) it floats over the top of the rows, so it
 * never moves the list. Below that it is a bar in the flow above the cards: a floating
 * pill there sat on top of the first card, which is the one the reader most wants.
 *
 * The visible pill is not a live region — it mounts and unmounts, and a region that
 * appears with its text already in it is not reliably announced. A separate
 * always-mounted `role="status"` carries the count, throttled (see
 * {@link useThrottledAnnouncement}).
 */
export function NewCallsPill({ count, onClick }: { count: number; onClick: () => void }) {
  const announcement = useThrottledAnnouncement(count);
  return (
    <>
      <div role="status" className="sr-only">
        {announcement}
      </div>
      {count > 0 ? (
        <div
          data-testid="new-calls-bar"
          className={cn(
            "sticky top-0 z-10 flex shrink-0 justify-center border-b border-line bg-surface px-3 py-1.5",
            "lg:pointer-events-none lg:absolute lg:inset-x-0 lg:top-1.5 lg:border-0 lg:bg-transparent lg:p-0",
          )}
        >
          <Tip label={HELP["calls.live.new"]}>
            <button
              type="button"
              onClick={onClick}
              className="pointer-events-auto inline-flex min-h-6 items-center gap-1.5 rounded-full border border-accent bg-accent-soft px-3 py-1 text-xs font-medium text-accent-ink shadow-sm"
            >
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-accent" aria-hidden="true" />
              {newCallsText(count)}
            </button>
          </Tip>
        </div>
      ) : null}
    </>
  );
}
