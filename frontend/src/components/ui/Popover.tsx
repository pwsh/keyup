import * as RadixPopover from "@radix-ui/react-popover";
import { isValidElement, type ReactElement, type ReactNode, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { escapeCloses } from "./Dialog";

/**
 * The words a trigger shows or is named by — its `aria-label`, else its text — which is
 * the natural name for the panel it opens.
 */
function triggerName(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(triggerName).join("");
  if (isValidElement<{ children?: ReactNode; "aria-label"?: unknown; "aria-hidden"?: unknown }>(node)) {
    const label = node.props["aria-label"];
    if (typeof label === "string" && label.trim()) return label;
    if (node.props["aria-hidden"] === true || node.props["aria-hidden"] === "true") return "";
    return triggerName(node.props.children);
  }
  return "";
}

export function Popover({
  trigger,
  children,
  align = "start",
  className,
  open,
  onOpenChange,
  label,
}: {
  /**
   * The panel's accessible name (Radix gives it `role="dialog"`, and a dialog needs one).
   * Defaults to the trigger's own name or text, so "Export" opens a dialog called "Export".
   */
  label?: string;
  trigger: ReactElement;
  children: ReactNode;
  align?: "start" | "center" | "end";
  className?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  // Held here as well as by Radix so Escape can close it from inside (see
  // `escapeCloses`); a caller that passes `open` still owns the state.
  const [ownOpen, setOwnOpen] = useState(false);
  const isOpen = open ?? ownOpen;
  // The latest state, ahead of the next render: when the popover is itself the top
  // layer Radix has already closed it by the time the keydown reaches React, and the
  // caller should hear "closed" once, not twice.
  const openRef = useRef(isOpen);
  openRef.current = isOpen;
  const setOpen = (next: boolean) => {
    openRef.current = next;
    if (open === undefined) setOwnOpen(next);
    onOpenChange?.(next);
  };
  return (
    <RadixPopover.Root open={isOpen} onOpenChange={setOpen}>
      <RadixPopover.Trigger asChild>{trigger}</RadixPopover.Trigger>
      <RadixPopover.Portal>
        <RadixPopover.Content
          aria-label={label ?? (triggerName(trigger).replace(/\s+/g, " ").trim() || "Options")}
          onKeyDown={escapeCloses(() => {
            if (openRef.current) setOpen(false);
          })}
          align={align}
          sideOffset={6}
          collisionPadding={12}
          className={cn(
            "z-50 rounded-md border border-line bg-surface p-3 text-ink shadow-xl",
            "max-h-[min(28rem,80vh)] overflow-auto",
            className,
          )}
        >
          {children}
        </RadixPopover.Content>
      </RadixPopover.Portal>
    </RadixPopover.Root>
  );
}
