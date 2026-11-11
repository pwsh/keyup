import { cn } from "../../lib/cn";

const LINE_KEYS = ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9", "l10", "l11", "l12"];
const BAR_HEIGHTS = [38, 62, 48, 80, 55, 70, 44, 90, 60, 35];

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-surface-2", className)} aria-hidden="true" />;
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("space-y-2", className)} role="status" aria-label="Loading">
      {LINE_KEYS.slice(0, lines).map((key, i) => (
        <Skeleton key={key} className={cn("h-3.5", i === lines - 1 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}

/** Title and body widths per row, varied so the stand-in reads as a list, not a grey block. */
const ROW_SHAPES = [
  ["w-28", "w-3/4"],
  ["w-36", "w-1/2"],
  ["w-24", "w-2/3"],
  ["w-32", "w-5/6"],
  ["w-20", "w-3/5"],
  ["w-40", "w-2/5"],
] as const;

/**
 * A list still loading, drawn in the shape of its rows: the channel edge on the left, a
 * title line and a body line, and a short time at the right — so the page does not jump
 * when the real rows arrive. One status for the whole list; the shapes are hidden.
 */
export function SkeletonRows({
  rows = 8,
  label = "Loading",
  className,
}: {
  rows?: number;
  label?: string;
  className?: string;
}) {
  return (
    <div className={cn("divide-y divide-line", className)} role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => {
        const [title, body] = ROW_SHAPES[i % ROW_SHAPES.length] ?? ROW_SHAPES[0];
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed, never-reordered placeholders
          <div key={i} className="flex items-start gap-3 py-2.5 pl-0 pr-3" aria-hidden="true">
            <div className="w-[3px] self-stretch rounded-full bg-surface-2" />
            <div className="min-w-0 flex-1 space-y-1.5">
              <Skeleton className={cn("h-3.5", title)} />
              <Skeleton className={cn("h-3", body)} />
            </div>
            <Skeleton className="h-3 w-12 shrink-0" />
          </div>
        );
      })}
    </div>
  );
}

export function SkeletonChart({ className }: { className?: string }) {
  return (
    <div
      className={cn("flex h-full w-full items-end gap-1.5 p-4", className)}
      role="status"
      aria-label="Loading chart"
    >
      {BAR_HEIGHTS.map((h) => (
        <div
          key={h}
          className="flex-1 animate-pulse rounded-t bg-surface-2"
          style={{ height: `${h}%` }}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}
