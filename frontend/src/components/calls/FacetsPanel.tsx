import { X } from "lucide-react";
import type { Facets } from "../../api/types";
import { cn } from "../../lib/cn";
import { formatNumber } from "../../lib/format";
import { InfoDot } from "../help/InfoDot";
import { Button } from "../ui/Button";
import { CategoryIcon } from "../ui/CategoryIcon";
import { SkeletonText } from "../ui/Skeleton";
import { Tip } from "../ui/Tooltip";

/**
 * Counts for the current filter, and the fastest way to narrow one more step.
 * Each row is a bar whose width is its share of the results, so the shape of the
 * traffic is readable before any number is.
 */
export function FacetsPanel({
  facets,
  isLoading,
  selectedTalkgroups,
  onToggleTalkgroup,
  onClose,
  className,
}: {
  facets: Facets | undefined;
  isLoading: boolean;
  selectedTalkgroups: number[];
  onToggleTalkgroup: (id: number) => void;
  onClose?: () => void;
  className?: string;
}) {
  const maxTalkgroup = Math.max(1, ...(facets?.talkgroup ?? []).map((bucket) => bucket.count));
  const maxHour = Math.max(1, ...(facets?.hour ?? []).map((bucket) => bucket.count));

  return (
    <aside className={cn("flex min-h-0 flex-col bg-surface", className)} aria-label="Facet counts">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <h2 className="flex items-center gap-1 text-sm font-semibold text-ink">
          Breakdown
          <InfoDot id="calls.facets.panel" />
        </h2>
        {onClose ? (
          <Button
            tip="Hide the breakdown and give the results the width"
            size="iconSm"
            variant="ghost"
            aria-label="Hide the breakdown"
            onClick={onClose}
          >
            <X size={15} aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin p-3">
        {isLoading && !facets ? (
          <SkeletonText lines={8} />
        ) : (
          <>
            <h3 className="mb-1.5 flex items-center gap-1 text-xs font-medium text-ink-2">
              By talkgroup
              <InfoDot id="calls.facets.talkgroup" />
            </h3>
            <ul className="mb-4 space-y-0.5">
              {(facets?.talkgroup ?? []).slice(0, 20).map((bucket) => {
                const id = Number(bucket.key);
                const active = selectedTalkgroups.includes(id);
                return (
                  <li key={bucket.key}>
                    <Tip
                      label={`${bucket.label}: ${formatNumber(bucket.count)} calls. Click to filter to it`}
                    >
                      <button
                        type="button"
                        aria-pressed={active}
                        onClick={() => onToggleTalkgroup(id)}
                        className={cn(
                          "relative flex w-full items-center justify-between gap-2 overflow-hidden rounded px-1.5 py-1 text-left text-xs",
                          active ? "text-accent-ink" : "text-ink-2 hover:text-ink",
                        )}
                      >
                        <span
                          aria-hidden="true"
                          className={cn(
                            "absolute inset-y-0 left-0 rounded",
                            active ? "bg-accent-soft" : "bg-surface-2",
                          )}
                          style={{ width: `${(bucket.count / maxTalkgroup) * 100}%` }}
                        />
                        <span className="relative min-w-0 truncate">{bucket.label}</span>
                        <span className="relative shrink-0 tabular-nums">{formatNumber(bucket.count)}</span>
                      </button>
                    </Tip>
                  </li>
                );
              })}
            </ul>

            <h3 className="mb-1.5 flex items-center gap-1 text-xs font-medium text-ink-2">
              By hour
              <InfoDot id="calls.facets.hour" />
            </h3>
            <div className="flex h-24 items-end gap-px" aria-hidden="true">
              {(facets?.hour ?? []).map((bucket) => (
                <Tip key={bucket.key} label={`${bucket.label} — ${formatNumber(bucket.count)} calls`}>
                  <div
                    tabIndex={0}
                    className="flex-1 rounded-t bg-accent"
                    style={{ height: `${Math.max(2, (bucket.count / maxHour) * 100)}%` }}
                  />
                </Tip>
              ))}
            </div>
            <div className="mt-1 flex justify-between text-[10px] tabular-nums text-muted">
              <span>00</span>
              <span>06</span>
              <span>12</span>
              <span>18</span>
              <span>23</span>
            </div>
            {/* The bars above are decorative; the numbers themselves stay reachable. */}
            <p className="sr-only">
              {(facets?.hour ?? []).map((bucket) => `${bucket.label}: ${bucket.count} calls. `).join("")}
            </p>

            {facets?.category.length ? (
              <>
                <h3 className="mb-1.5 mt-4 text-xs font-medium text-ink-2">By category</h3>
                <ul className="space-y-0.5 text-xs text-ink-2">
                  {facets.category.map((bucket) => (
                    <li key={bucket.key} className="flex items-center justify-between gap-2 px-1.5">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <CategoryIcon category={bucket.key} />
                        <span className="truncate">{bucket.label}</span>
                      </span>
                      <span className="tabular-nums">{formatNumber(bucket.count)}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </>
        )}
      </div>
    </aside>
  );
}
