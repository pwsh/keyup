/**
 * A right-anchored panel: `Dialog`'s twin, for the edits you make *while looking at
 * the list*.
 *
 * A modal centered on the viewport covers the rows it is about. That is the right
 * trade for editing one row — the row is on the clipboard of your short-term memory
 * and the form is the whole task — but it is the wrong one for a bulk edit, where
 * "which forty channels did I tick?" is the question the form keeps raising and the
 * table behind it is the only answer. The drawer takes the right-hand third and
 * leaves the selection visible.
 *
 * It is still modal in every other sense — Radix's focus trap, Escape, the overlay,
 * `aria-modal` — because a half-filled bulk form that the reader can wander away
 * from is a half-applied edit waiting to happen.
 */

import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode, RefObject } from "react";
import { cn } from "../../lib/cn";
import { useModalClose } from "./Dialog";

export function Drawer({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
  returnFocusTo,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  /** Pinned to the bottom of the panel: a long form must not scroll its Save away. */
  footer?: ReactNode;
  className?: string;
  /** Where focus goes on close, when the element that opened the drawer may be gone. */
  returnFocusTo?: RefObject<HTMLElement | null>;
}) {
  const { change, onCloseAutoFocus, onKeyDown } = useModalClose(open, onOpenChange, returnFocusTo);
  return (
    <RadixDialog.Root open={open} onOpenChange={change}>
      <RadixDialog.Portal>
        {/* Lighter than the Dialog's overlay: the point of the drawer is that you can
            still read the rows it is about. */}
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/25" />
        <RadixDialog.Content
          onCloseAutoFocus={onCloseAutoFocus}
          onKeyDown={onKeyDown}
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-[min(26rem,100vw)] flex-col",
            "border-l border-line bg-surface shadow-2xl",
            className,
          )}
        >
          <div className="flex shrink-0 items-start justify-between gap-4 border-b border-line px-5 py-4">
            <div>
              <RadixDialog.Title className="text-base font-semibold text-ink">{title}</RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="mt-1 text-xs text-ink-2">
                  {description}
                </RadixDialog.Description>
              ) : null}
            </div>
            <RadixDialog.Close
              className="rounded p-1 text-ink-2 hover:bg-surface-2 hover:text-ink"
              aria-label="Close panel"
            >
              <X size={16} aria-hidden="true" />
            </RadixDialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-auto scrollbar-thin px-5 py-4">{children}</div>
          {footer ? (
            <div className="flex shrink-0 justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>
          ) : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
