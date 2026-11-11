import type { ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

/** The chip's own classes, so a chip-shaped control that is not a `Chip` matches. */
export const chipClass = (active: boolean, className?: string): string =>
  cn(
    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
    active
      ? "border-accent bg-accent-soft text-accent-ink"
      : "border-line bg-surface text-ink-2 hover:bg-surface-2 hover:text-ink",
    className,
  );

/** A toggleable filter chip; the pressed state is exposed to screen readers. */
export function Chip({
  label,
  tip,
  active,
  onClick,
  count,
  className,
}: {
  label: ReactNode;
  tip: ReactNode;
  active: boolean;
  onClick: () => void;
  count?: number;
  className?: string;
}) {
  return (
    <Tip label={tip}>
      <button type="button" aria-pressed={active} onClick={onClick} className={chipClass(active, className)}>
        {label}
        {count !== undefined ? <span className="tabular-nums text-[11px] opacity-80">{count}</span> : null}
      </button>
    </Tip>
  );
}

const TONES: Record<string, string> = {
  ok: "border-good text-good-ink",
  done: "border-good text-good-ink",
  sent: "border-good text-good-ink",
  good: "border-good text-good-ink",
  ingested: "border-good text-good-ink",
  healthy: "border-good text-good-ink",
  failed: "border-critical text-critical-ink",
  error: "border-critical text-critical-ink",
  noise: "border-critical text-critical-ink",
  down: "border-critical text-critical-ink",
  running: "border-warning text-ink",
  queued: "border-warning text-ink",
  pending: "border-warning text-ink",
  suspect: "border-warning text-ink",
  degraded: "border-warning text-ink",
  duplicate: "border-line-strong text-ink-2",
  skipped: "border-line-strong text-ink-2",
  empty: "border-line-strong text-ink-2",
};

/** A read-only status pill. The border carries the hue; the text stays readable. */
export function StatusChip({
  status,
  label,
  tip,
  className,
}: {
  status: string;
  /** The words to show when the status is a code (`file_write`); the status picks the tone. */
  label?: string;
  tip: ReactNode;
  className?: string;
}) {
  return (
    <Tip label={tip}>
      <span
        tabIndex={0}
        className={cn(
          "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium",
          TONES[status] ?? "border-line text-ink-2",
          className,
        )}
      >
        {label ?? status}
      </span>
    </Tip>
  );
}
