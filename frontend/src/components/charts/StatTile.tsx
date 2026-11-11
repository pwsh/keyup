import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { HELP, type HelpId } from "../../help/help";
import { cn } from "../../lib/cn";
import { Tip } from "../ui/Tooltip";

/**
 * A headline number. No plot, because one number is not a chart — the value is
 * the whole message, so it gets the size and the label sits under it.
 *
 * `tone` is a *status*, never decoration, and it is never the only cue: the
 * tooltip and the screen-reader text say what the state is in words.
 */
export function StatTile({
  label,
  value,
  unit,
  help,
  icon: Icon,
  tone = "neutral",
  toneReason,
  footnote,
  pending = false,
  className,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  help: HelpId;
  icon?: LucideIcon;
  tone?: "neutral" | "good" | "warning" | "critical";
  /** Said aloud and in the tooltip, so the color is never load-bearing. */
  toneReason?: string;
  footnote?: string;
  /** The value is not known yet: the tile takes no status until it is. */
  pending?: boolean;
  className?: string;
}) {
  const shown = pending ? "neutral" : tone;
  const reason = pending ? undefined : toneReason;
  // No frame of its own: a tile sits inside a card or a page section already, and a
  // box in a box was the nesting the console restyle took out. The status, when there
  // is one, is a 3 px edge down the left, like a call row's key-up edge.
  const border =
    shown === "good"
      ? "border-good"
      : shown === "warning"
        ? "border-warning"
        : shown === "critical"
          ? "border-critical"
          : "border-line-strong";
  return (
    <Tip label={reason ? `${HELP[help]} — ${reason}` : HELP[help]}>
      <div tabIndex={0} className={cn("border-l-[3px] py-1 pl-3", border, className)}>
        {/* A term and its value, not a heading over a paragraph — which is how a big bold
            <p> above a small one reads (axe p-as-heading). */}
        <dl>
          <dt className="flex items-center gap-1.5 text-xs font-medium text-ink-2">
            {Icon ? <Icon size={12} aria-hidden="true" /> : null}
            {label}
          </dt>
          <dd className="mt-1 text-2xl font-semibold tabular-nums leading-none text-ink">
            {value}
            {unit ? <span className="ml-1 text-sm font-normal text-ink-2">{unit}</span> : null}
          </dd>
          {footnote ? <dd className="mt-1 text-[11px] text-muted">{footnote}</dd> : null}
        </dl>
        {reason ? <span className="sr-only">{reason}</span> : null}
      </div>
    </Tip>
  );
}
