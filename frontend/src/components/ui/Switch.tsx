import * as RadixSwitch from "@radix-ui/react-switch";
import { type ReactNode, useId } from "react";
import { resolveHelp, useHelpLoaded } from "../../help/help";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

/**
 * A labeled on/off toggle.
 *
 * The visible text is not a `<label>`: Radix renders the switch as a `<button>`,
 * which a label cannot name. It is a `<span>` the switch points at with
 * `aria-labelledby` instead, so the accessible name is the same words the reader
 * sees — and a toggle with no visible text (a table cell) still gets one from
 * its tooltip.
 */
export function Switch({
  checked,
  onCheckedChange,
  label,
  tip,
  disabled,
  className,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: ReactNode;
  tip: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  const labelId = useId();
  const hasLabel = label !== "" && label !== null && label !== undefined;
  // A `HELP[...]` tip is a placeholder until the help text loads; the name must be words.
  useHelpLoaded();
  return (
    <Tip label={tip}>
      <span
        className={cn("inline-flex items-center gap-2 text-xs text-ink", disabled && "opacity-50", className)}
      >
        <RadixSwitch.Root
          checked={checked}
          onCheckedChange={onCheckedChange}
          disabled={disabled}
          {...(hasLabel
            ? { "aria-labelledby": labelId }
            : { "aria-label": (typeof tip === "string" && resolveHelp(tip)) || "Toggle" })}
          className={cn(
            "relative h-[18px] w-8 shrink-0 rounded-full border transition-colors",
            checked ? "border-accent bg-accent" : "border-control-border bg-surface-2",
            disabled ? "cursor-not-allowed" : "cursor-pointer",
          )}
        >
          <RadixSwitch.Thumb
            className={cn(
              "block h-3.5 w-3.5 rounded-full shadow transition-transform",
              "translate-x-[2px] data-[state=checked]:translate-x-[16px]",
              checked ? "bg-white" : "bg-control-border",
            )}
          />
        </RadixSwitch.Root>
        {hasLabel ? (
          <span id={labelId} className="select-none">
            {label}
          </span>
        ) : null}
      </span>
    </Tip>
  );
}
