import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, type RefObject, useLayoutEffect, useRef } from "react";
import { cn } from "../../lib/cn";

/**
 * Remembers what had focus when an overlay opened, and puts focus back there when it
 * closes (WCAG 2.4.3).
 *
 * Radix does this itself only for an overlay opened through its own `Trigger`. Ours are
 * opened from state, so it fell back to whatever its focus scope saw on mount, and a
 * closed Filters sheet left focus on `<body>` — the reader's next Tab started from the
 * top of the page. The opener is read in a layout effect, which runs before Radix's
 * focus scope (a passive effect) has moved focus into the overlay.
 *
 * `returnTo`, when given, wins: for an opener that is replaced while the overlay is up.
 */
export function useReturnFocus(open: boolean, returnTo?: RefObject<HTMLElement | null>) {
  const opener = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!open) return;
    const active = document.activeElement;
    opener.current = active instanceof HTMLElement && active !== document.body ? active : null;
  }, [open]);
  return (event: Event) => {
    const target = returnTo?.current ?? opener.current;
    opener.current = null;
    if (target?.isConnected) {
      event.preventDefault();
      target.focus();
    }
  };
}

/**
 * Escape closes the overlay even when a tooltip inside it is open.
 *
 * Every control in the app has a tooltip, and a tooltip opens when its control takes
 * focus — which is what happens to the first item when an overlay opens. Radix gives
 * Escape to the topmost dismissable layer only, so the first Escape closed that
 * tooltip and the overlay stayed up, with focus still on its first item. Here an Escape
 * whose target is inside the overlay's own DOM closes the overlay too. A select or
 * popover nested inside it is portalled out of that DOM, so its own Escape is left
 * alone.
 */
export function escapeCloses(close: () => void) {
  return (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "Escape") return;
    if (!(event.target instanceof Node) || !event.currentTarget.contains(event.target)) return;
    close();
  };
}

/**
 * Everything a controlled Radix dialog (this one, `Drawer`) needs to close well: focus
 * back on the opener, and Escape that is not swallowed by a tooltip inside.
 *
 * `change` goes to `Root.onOpenChange`. It records the latest state ahead of the next
 * render because, when the dialog is itself the top layer, Radix has already closed it
 * by the time the keydown reaches React, and the caller should hear "closed" once.
 */
export function useModalClose(
  open: boolean,
  onOpenChange: (open: boolean) => void,
  returnTo?: RefObject<HTMLElement | null>,
) {
  const onCloseAutoFocus = useReturnFocus(open, returnTo);
  const openRef = useRef(open);
  openRef.current = open;
  const change = (next: boolean) => {
    openRef.current = next;
    onOpenChange(next);
  };
  const onKeyDown = escapeCloses(() => {
    if (openRef.current) change(false);
  });
  return { change, onCloseAutoFocus, onKeyDown };
}

export function Dialog({
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
  footer?: ReactNode;
  className?: string;
  /** Where focus goes on close, when the element that opened the dialog may be gone. */
  returnFocusTo?: RefObject<HTMLElement | null>;
}) {
  const { change, onCloseAutoFocus, onKeyDown } = useModalClose(open, onOpenChange, returnFocusTo);
  return (
    <RadixDialog.Root open={open} onOpenChange={change}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/45" />
        <RadixDialog.Content
          onCloseAutoFocus={onCloseAutoFocus}
          onKeyDown={onKeyDown}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(38rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2",
            "max-h-[85vh] overflow-auto rounded-lg border border-line bg-surface p-5 shadow-2xl",
            className,
          )}
        >
          <div className="mb-3 flex items-start justify-between gap-4">
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
              aria-label="Close dialog"
            >
              <X size={16} aria-hidden="true" />
            </RadixDialog.Close>
          </div>
          {children}
          {footer ? <div className="mt-5 flex justify-end gap-2">{footer}</div> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
