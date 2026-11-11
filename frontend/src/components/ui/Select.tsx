import * as RadixSelect from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * What an empty value is called on the wire to Radix.
 *
 * Radix reads `value=""` as "nothing is selected" and refuses an `Item` whose
 * value is the empty string, so an optional select written the obvious way —
 * `value=""` with a `{ value: "", label: "None" }` option — rendered as an empty
 * box with no option to pick it back. The empty string is this component's
 * contract with its callers; the sentinel never leaves it.
 */
const NONE = "__none__";

const toRadix = (value: string): string => (value === "" ? NONE : value);
const fromRadix = (value: string): string => (value === NONE ? "" : value);

export function Select({
  value,
  onValueChange,
  options,
  tip,
  ariaLabel,
  className,
  size = "md",
  disabled = false,
  placeholder = "None",
}: {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  tip: string;
  ariaLabel: string;
  className?: string;
  size?: "sm" | "md";
  /** Shown, grayed, and unopenable — the same read-only treatment `Button` gives. */
  disabled?: boolean;
  /**
   * Shown when the value is empty *and* no option claims the empty value — the
   * last line of defence against a blank control. An optional select should
   * normally offer an explicit `{ value: "", label: "None" }` option instead, so
   * the state can be chosen again once it has been left.
   */
  placeholder?: string;
}) {
  return (
    <RadixSelect.Root
      value={toRadix(value)}
      onValueChange={(next) => onValueChange(fromRadix(next))}
      disabled={disabled}
    >
      <Tip label={tip}>
        <RadixSelect.Trigger
          aria-label={ariaLabel}
          className={cn(
            "inline-flex items-center justify-between gap-2 rounded-md border border-control-border bg-surface px-2.5 text-sm text-ink hover:bg-surface-2",
            "disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-surface",
            size === "sm" ? "h-8" : "h-9",
            className,
          )}
        >
          <RadixSelect.Value placeholder={placeholder} />
          <RadixSelect.Icon>
            <ChevronDown size={14} aria-hidden="true" />
          </RadixSelect.Icon>
        </RadixSelect.Trigger>
      </Tip>
      <RadixSelect.Portal>
        <RadixSelect.Content
          position="popper"
          sideOffset={4}
          className="z-50 max-h-72 overflow-hidden rounded-md border border-line bg-surface shadow-xl"
        >
          <RadixSelect.Viewport className="p-1">
            {options.map((opt) => (
              <RadixSelect.Item
                key={opt.value}
                value={toRadix(opt.value)}
                className="flex cursor-pointer items-center justify-between gap-3 rounded px-2 py-1.5 text-sm text-ink outline-none data-[highlighted]:bg-surface-2"
              >
                <RadixSelect.ItemText>{opt.label}</RadixSelect.ItemText>
                <RadixSelect.ItemIndicator>
                  <Check size={14} aria-hidden="true" />
                </RadixSelect.ItemIndicator>
              </RadixSelect.Item>
            ))}
          </RadixSelect.Viewport>
        </RadixSelect.Content>
      </RadixSelect.Portal>
    </RadixSelect.Root>
  );
}
