import {
  Children,
  type CSSProperties,
  cloneElement,
  type FocusEvent,
  isValidElement,
  type PointerEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
  type RefCallback,
  type SyntheticEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { createPortal } from "react-dom";
import { useLocation } from "react-router";
import { hasHelpPlaceholder, loadHelp, resolveHelp, resolveHelpNode, useHelpLoaded } from "../../help/help";
import { cn } from "../../lib/cn";

/** How long a tooltip opened by a tap stays up before closing itself. */
export const TOUCH_TOOLTIP_MS = 2500;
/** Hover delay before a tooltip opens, and the window in which the next one opens at once. */
const OPEN_DELAY_MS = 300;
const SKIP_DELAY_MS = 200;
/** Grace period for moving the pointer from the control onto its bubble (WCAG 1.4.13). */
const CLOSE_GRACE_MS = 100;

/* ------------------------------------------------------------------ pointer */

/**
 * A touch screen has no hover-out, so a tooltip opened by a tap would sit over
 * the content until something else moved it. `(hover: none)` catches the devices whose
 * primary pointer reports itself as fine but cannot hover.
 */
const COARSE_QUERIES = ["(pointer: coarse)", "(hover: none)"];

let pointerLists: MediaQueryList[] | null = null;
let coarseValue: boolean | null = null;
const pointerListeners = new Set<() => void>();

function mediaLists(): MediaQueryList[] {
  if (pointerLists) return pointerLists;
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    pointerLists = [];
    return pointerLists;
  }
  try {
    pointerLists = COARSE_QUERIES.map((query) => window.matchMedia(query));
  } catch {
    pointerLists = [];
  }
  return pointerLists;
}

/**
 * Re-read the pointer media queries. Called when the queries themselves change (a tablet
 * docked to a mouse) and by the unit tests, which stub `window.matchMedia` after this
 * module has been loaded.
 */
export function refreshPointerType(): void {
  const attached = pointerListeners.size > 0;
  if (attached) for (const list of mediaLists()) list.removeEventListener("change", refreshPointerType);
  pointerLists = null;
  coarseValue = mediaLists().some((list) => list.matches);
  if (attached) for (const list of mediaLists()) list.addEventListener("change", refreshPointerType);
  for (const listener of pointerListeners) listener();
}

function getCoarse(): boolean {
  if (coarseValue === null) coarseValue = mediaLists().some((list) => list.matches);
  return coarseValue;
}

function subscribePointer(listener: () => void): () => void {
  pointerListeners.add(listener);
  if (pointerListeners.size === 1) {
    for (const list of mediaLists()) list.addEventListener("change", refreshPointerType);
  }
  return () => {
    pointerListeners.delete(listener);
    if (pointerListeners.size === 0) {
      for (const list of mediaLists()) list.removeEventListener("change", refreshPointerType);
    }
  };
}

/** True on touch devices — where tooltips are tap-toggled rather than hovered. */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(subscribePointer, getCoarse, () => false);
}

/* --------------------------------------------------------------- the one host */

type Side = "top" | "right" | "bottom" | "left";

/** What a `Tip` hands the host. Mutable: the latest render's label is always read. */
interface TipRecord {
  label: ReactNode;
  side: Side;
  className: string | undefined;
}

interface HostApi {
  show: (el: HTMLElement, record: TipRecord, mode: "hover" | "focus" | "touch") => void;
  hide: (el?: HTMLElement) => void;
  hideSoon: (el: HTMLElement) => void;
  toggleTouch: (el: HTMLElement, record: TipRecord) => void;
  refresh: (record: TipRecord) => void;
}

/** The mounted `TooltipProvider`'s host. One bubble for the whole app. */
let host: HostApi | null = null;

/** Close every open tooltip in the app, wherever it lives. */
export function closeAllTooltips(): void {
  host?.hide();
}

/* ------------------------------------------------- descriptions for info anchors */

/**
 * The text of every informational anchor's tooltip, as hidden nodes its anchor points at
 * with `aria-describedby`. A screen reader reaches the explanation where the text is,
 * without the anchor having to be a tab stop; strings are de-duplicated, so a column of
 * 200 cells with one explanation costs one node.
 */
interface Description {
  label: ReactNode;
  count: number;
}
const descriptions = new Map<string, Description>();
let descriptionVersion = 0;
let descriptionNotify = false;
const descriptionListeners = new Set<() => void>();

function notifyDescriptions(): void {
  descriptionVersion += 1;
  if (descriptionNotify) return;
  descriptionNotify = true;
  // Coalesced: a table mounting 200 anchors re-renders the list once, not 200 times.
  queueMicrotask(() => {
    descriptionNotify = false;
    for (const listener of descriptionListeners) listener();
  });
}

function retainDescription(id: string, label: ReactNode): () => void {
  const entry = descriptions.get(id);
  if (entry) {
    entry.count += 1;
    if (entry.label !== label) {
      entry.label = label;
      notifyDescriptions();
    }
  } else {
    descriptions.set(id, { label, count: 1 });
    notifyDescriptions();
  }
  return () => {
    const current = descriptions.get(id);
    if (!current) return;
    current.count -= 1;
    if (current.count <= 0) {
      descriptions.delete(id);
      notifyDescriptions();
    }
  };
}

function subscribeDescriptions(listener: () => void): () => void {
  descriptionListeners.add(listener);
  return () => {
    descriptionListeners.delete(listener);
  };
}

function hashString(text: string): string {
  let hash = 5381;
  for (let index = 0; index < text.length; index++) hash = ((hash << 5) + hash + text.charCodeAt(index)) | 0;
  return `${(hash >>> 0).toString(36)}${text.length.toString(36)}`;
}

function Descriptions() {
  useSyncExternalStore(
    subscribeDescriptions,
    () => descriptionVersion,
    () => descriptionVersion,
  );
  useHelpLoaded();
  const items = [...descriptions.entries()];
  // Portalled into <body>, and absent while there is nothing to describe: the provider adds
  // nothing to the tree it wraps, so "renders nothing" still means nothing.
  if (items.length === 0 || typeof document === "undefined") return null;
  return createPortal(
    <div hidden data-testid="tip-descriptions">
      {items.map(([id, entry]) => (
        <span key={id} id={id}>
          {resolveHelpNode(entry.label)}
        </span>
      ))}
    </div>,
    document.body,
  );
}

/* -------------------------------------------------------------------- Provider */

interface Open {
  el: HTMLElement;
  record: TipRecord;
  touch: boolean;
}

/**
 * The app's single tooltip. Every `Tip` is a few event handlers on its child; this is
 * the only component that ever renders a bubble. A Radix tooltip per control cost a
 * Root, a Popper and a Presence for each of hundreds of table cells, mounted and
 * re-rendered with every row; one shared bubble costs nothing per cell.
 */
function TooltipHost() {
  const [open, setOpen] = useState<Open | null>(null);
  const [, setTick] = useState(0);
  const openRef = useRef<Open | null>(null);
  openRef.current = open;
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const timers = useRef<{ open?: number; close?: number; lastClosed: number }>({ lastClosed: 0 });
  const bubbleId = useId();
  const helpLoaded = useHelpLoaded();
  const [position, setPosition] = useState<{ top: number; left: number; arrow: number; side: Side } | null>(
    null,
  );

  useEffect(() => {
    const clearTimers = () => {
      window.clearTimeout(timers.current.open);
      window.clearTimeout(timers.current.close);
    };
    const doHide = (el?: HTMLElement) => {
      clearTimers();
      const current = openRef.current;
      if (!current || (el && current.el !== el)) return;
      timers.current.lastClosed = Date.now();
      openRef.current = null;
      setOpen(null);
    };
    const doShow = (el: HTMLElement, record: TipRecord, touch: boolean) => {
      clearTimers();
      const next = { el, record, touch };
      openRef.current = next;
      setOpen(next);
      if (record.label !== null && typeof record.label === "string" && hasHelpPlaceholder(record.label)) {
        void loadHelp().catch(() => {});
      } else if (record.label !== null && typeof record.label === "object") {
        void loadHelp().catch(() => {});
      }
    };
    const api: HostApi = {
      show: (el, record, mode) => {
        window.clearTimeout(timers.current.close);
        const current = openRef.current;
        if (current?.el === el) return;
        window.clearTimeout(timers.current.open);
        const instant =
          mode !== "hover" || current !== null || Date.now() - timers.current.lastClosed < SKIP_DELAY_MS;
        if (instant) doShow(el, record, mode === "touch");
        else timers.current.open = window.setTimeout(() => doShow(el, record, false), OPEN_DELAY_MS);
      },
      hide: doHide,
      hideSoon: (el) => {
        window.clearTimeout(timers.current.open);
        if (openRef.current?.el !== el) return;
        window.clearTimeout(timers.current.close);
        timers.current.close = window.setTimeout(() => doHide(el), CLOSE_GRACE_MS);
      },
      toggleTouch: (el, record) => {
        if (openRef.current?.el === el) doHide();
        else doShow(el, record, true);
      },
      refresh: (record) => {
        if (openRef.current?.record === record) setTick((tick) => tick + 1);
      },
    };
    host = api;
    return () => {
      clearTimers();
      if (host === api) host = null;
    };
  }, []);

  // While open: the anchor says what describes it, and the bubble follows the anchor.
  useEffect(() => {
    if (!open) return;
    const { el } = open;
    const previous = el.getAttribute("aria-describedby");
    const tokens = new Set((previous ?? "").split(/\s+/).filter(Boolean));
    tokens.add(bubbleId);
    el.setAttribute("aria-describedby", [...tokens].join(" "));
    return () => {
      const now = (el.getAttribute("aria-describedby") ?? "")
        .split(/\s+/)
        .filter((token) => token && token !== bubbleId);
      if (now.length) el.setAttribute("aria-describedby", now.join(" "));
      else el.removeAttribute("aria-describedby");
    };
  }, [open, bubbleId]);

  useEffect(() => {
    if (!open) return;
    const close = () => host?.hide();
    const onKeyDown = (event: KeyboardEvent) => {
      // Never stops the event: the dialog or popover under it closes on the same Escape.
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKeyDown, true);
    if (!open.touch) return () => document.removeEventListener("keydown", onKeyDown, true);

    // Touch has no hover-out: close on a tap or scroll elsewhere, on a timer, and when a
    // dialog, select or popover appears or goes (the page moved under the bubble).
    const timer = window.setTimeout(close, TOUCH_TOOLTIP_MS);
    const onOutside = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && open.el.contains(target)) return;
      close();
    };
    document.addEventListener("pointerdown", onOutside, true);
    document.addEventListener("touchstart", onOutside, true);
    document.addEventListener("scroll", close, true);
    const observer =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver((records) => {
            const bubble = bubbleRef.current;
            for (const record of records) {
              const moved = [...record.addedNodes, ...record.removedNodes];
              if (moved.some((node) => !(bubble && node.contains(bubble)))) {
                close();
                return;
              }
            }
          });
    observer?.observe(document.body, { childList: true });
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onOutside, true);
      document.removeEventListener("touchstart", onOutside, true);
      document.removeEventListener("scroll", close, true);
      observer?.disconnect();
    };
  }, [open]);

  const content = open
    ? typeof open.record.label === "string"
      ? resolveHelp(open.record.label)
      : resolveHelpNode(open.record.label)
    : null;
  void helpLoaded;
  const visible = open !== null && content !== "" && content !== null && content !== undefined;

  // Placement: the preferred side, flipped when it does not fit, clamped to the viewport.
  useLayoutEffect(() => {
    if (!visible || !open) {
      setPosition(null);
      return;
    }
    const place = () => {
      const bubble = bubbleRef.current;
      if (!bubble) return;
      if (!open.el.isConnected) {
        host?.hide();
        return;
      }
      const anchor = open.el.getBoundingClientRect();
      const width = bubble.offsetWidth;
      const height = bubble.offsetHeight;
      const gap = 8;
      const pad = 12;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let side = open.record.side;
      const fits: Record<Side, boolean> = {
        top: anchor.top - gap - height >= pad,
        bottom: anchor.bottom + gap + height <= vh - pad,
        left: anchor.left - gap - width >= pad,
        right: anchor.right + gap + width <= vw - pad,
      };
      const opposite: Record<Side, Side> = { top: "bottom", bottom: "top", left: "right", right: "left" };
      if (!fits[side] && fits[opposite[side]]) side = opposite[side];
      let top: number;
      let left: number;
      if (side === "top" || side === "bottom") {
        top = side === "top" ? anchor.top - gap - height : anchor.bottom + gap;
        left = anchor.left + anchor.width / 2 - width / 2;
      } else {
        left = side === "left" ? anchor.left - gap - width : anchor.right + gap;
        top = anchor.top + anchor.height / 2 - height / 2;
      }
      left = Math.max(pad, Math.min(left, vw - pad - width));
      top = Math.max(pad, Math.min(top, vh - pad - height));
      const arrow =
        side === "top" || side === "bottom"
          ? Math.max(8, Math.min(anchor.left + anchor.width / 2 - left, width - 8))
          : Math.max(8, Math.min(anchor.top + anchor.height / 2 - top, height - 8));
      setPosition((prev) =>
        prev && prev.top === top && prev.left === left && prev.arrow === arrow && prev.side === side
          ? prev
          : { top, left, arrow, side },
      );
    };
    // Placed every frame while open, not just on scroll and resize: an anchor inside a
    // popover or menu that is still being positioned (Radix measures, then moves its
    // content) would otherwise pin the bubble to wherever the anchor was on the first
    // frame — the window's corner. One getBoundingClientRect a frame, only while a
    // tooltip is up.
    let frame = 0;
    const loop = () => {
      place();
      frame = requestAnimationFrame(loop);
    };
    loop();
    return () => cancelAnimationFrame(frame);
  });

  const arrowStyle: CSSProperties | undefined = position
    ? position.side === "top"
      ? { left: position.arrow, bottom: -5 }
      : position.side === "bottom"
        ? { left: position.arrow, top: -5 }
        : position.side === "left"
          ? { top: position.arrow, right: -5 }
          : { top: position.arrow, left: -5 }
    : undefined;

  return (
    <>
      <Descriptions />
      {visible
        ? createPortal(
            <div
              ref={bubbleRef}
              id={bubbleId}
              role="tooltip"
              data-side={position?.side ?? open.record.side}
              onPointerEnter={() => window.clearTimeout(timers.current.close)}
              onPointerLeave={() => host?.hideSoon(open.el)}
              style={{
                position: "fixed",
                top: position?.top ?? 0,
                left: position?.left ?? 0,
                visibility: position ? "visible" : "hidden",
              }}
              className={cn(
                "z-50 max-w-xs rounded-md border border-line bg-surface px-2.5 py-1.5",
                "text-xs leading-snug text-ink shadow-lg",
                // On touch the bubble sits over the very content it explains, so let taps
                // through: the next tap reaches the page, not the tooltip.
                open.touch && "pointer-events-none",
                open.record.className,
              )}
            >
              {content}
              <span
                aria-hidden="true"
                className="absolute h-2.5 w-2.5 -translate-x-1/2 rotate-45 border border-line bg-surface data-[v=true]:-translate-y-1/2 data-[v=true]:translate-x-0"
                data-v={position?.side === "left" || position?.side === "right"}
                style={{
                  ...arrowStyle,
                  clipPath:
                    position?.side === "top"
                      ? "polygon(100% 0, 100% 100%, 0 100%)"
                      : position?.side === "bottom"
                        ? "polygon(0 0, 100% 0, 0 100%)"
                        : position?.side === "left"
                          ? "polygon(100% 0, 100% 100%, 0 0)"
                          : "polygon(0 0, 0 100%, 100% 100%)",
                }}
              />
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

export const TooltipProvider = ({ children }: { children: ReactNode }) => {
  const { pathname, search } = useLocation();
  // A tooltip must never outlive the view it explains. Navigating (or changing a filter,
  // which is also a URL change here) closes it.
  useEffect(() => {
    void pathname;
    void search;
    closeAllTooltips();
  }, [pathname, search]);

  // The help text is fetched once the first screen is up, so it never competes with the
  // route and its data; a tooltip opened before then fetches it on the spot.
  useEffect(() => {
    const idle = (
      window as { requestIdleCallback?: (cb: () => void, options?: { timeout: number }) => number }
    ).requestIdleCallback;
    const load = () => void loadHelp().catch(() => {});
    if (idle) {
      const handle = idle(load, { timeout: 2000 });
      return () => (window as { cancelIdleCallback?: (handle: number) => void }).cancelIdleCallback?.(handle);
    }
    const handle = window.setTimeout(load, 500);
    return () => window.clearTimeout(handle);
  }, []);

  return (
    <>
      {children}
      <TooltipHost />
    </>
  );
};

/* -------------------------------------------------------------------------- Tip */

export interface TipProps extends Record<string, unknown> {
  /** Plain description of what the control does — every control in the app has one. */
  label: ReactNode;
  children: ReactElement;
  side?: Side;
  className?: string;
  ref?: Ref<HTMLElement>;
}

const INTERACTIVE_TAGS = new Set([
  "a",
  "button",
  "input",
  "select",
  "textarea",
  "summary",
  "details",
  "label",
  "audio",
  "video",
  "iframe",
]);
const INTERACTIVE_ROLES =
  /^(button|link|checkbox|radio|switch|slider|spinbutton|tab|menuitem|menuitemcheckbox|menuitemradio|option|combobox|textbox|searchbox|scrollbar|treeitem|gridcell)$/;

/**
 * Is this child a plain piece of text that only carries a tooltip — a column header's
 * label, a score, a "backfilled" badge — rather than a control? Such an anchor used to be
 * made a tab stop (`tabIndex={0}`) just so its tooltip could open on focus: 200-300
 * extra Tab presses on a directory page. It is now taken out of the tab
 * order, and its explanation is attached with `aria-describedby` instead, which a screen
 * reader announces where the text is. Sighted keyboard users have every sentence on
 * `/help`.
 */
function isInformational(child: ReactElement<Record<string, unknown>>): boolean {
  if (typeof child.type !== "string") return false;
  if (INTERACTIVE_TAGS.has(child.type)) return false;
  const role = child.props.role;
  if (typeof role === "string" && INTERACTIVE_ROLES.test(role)) return false;
  if (child.props.contentEditable) return false;
  return true;
}

function composeRefs<T>(...refs: (Ref<T> | undefined)[]): RefCallback<T> | undefined {
  const live = refs.filter(Boolean);
  if (live.length === 0) return undefined;
  if (live.length === 1 && typeof live[0] === "function") return live[0] as RefCallback<T>;
  return (value: T) => {
    for (const ref of live) {
      if (typeof ref === "function") ref(value);
      else if (ref) (ref as { current: T | null }).current = value;
    }
  };
}

type Handler = (event: SyntheticEvent) => void;

/**
 * Merge props the way Radix's `Slot` does, so a `Tip` can itself be the `asChild` target
 * of another trigger (`<Popover.Trigger asChild><Tip>…`): handlers run child-first then
 * ours, classes and styles combine, and anything else the child sets wins.
 */
function mergeProps(slot: Record<string, unknown>, child: Record<string, unknown>): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...slot };
  for (const [key, childValue] of Object.entries(child)) {
    const slotValue = slot[key];
    if (/^on[A-Z]/.test(key) && typeof childValue === "function" && typeof slotValue === "function") {
      merged[key] = (...args: unknown[]) => {
        const result = (childValue as (...a: unknown[]) => unknown)(...args);
        (slotValue as (...a: unknown[]) => unknown)(...args);
        return result;
      };
    } else if (key === "style" && slotValue && childValue) {
      merged[key] = { ...(slotValue as object), ...(childValue as object) };
    } else if (key === "className" && slotValue && childValue) {
      merged[key] = `${slotValue as string} ${childValue as string}`;
    } else if (childValue !== undefined || !(key in slot)) {
      merged[key] = childValue;
    }
  }
  return merged;
}

function chain(first: unknown, second: Handler): Handler {
  if (typeof first !== "function") return second;
  return (event) => {
    (first as Handler)(event);
    second(event);
  };
}

/**
 * Give `children` a tooltip: shown after 300 ms of hover or at once on keyboard focus,
 * closed on pointer-out, blur, click, Escape and any URL change. On a **coarse pointer**
 * a tap toggles it, and it closes itself after {@link TOUCH_TOOLTIP_MS} or on a tap or
 * scroll anywhere else.
 *
 * `Tip` renders nothing of its own — just its child with a few handlers — and the one
 * bubble lives in `TooltipProvider`. Any other prop is merged onto the child (see
 * {@link mergeProps}), and a `ref` given to the `Tip` reaches the child.
 *
 * `label` may hold `HELP[...]` placeholders; they are resolved when the bubble shows.
 */
export function Tip({ label, children, side = "top", className, ref, ...triggerProps }: TipProps) {
  const child = Children.only(children) as ReactElement<Record<string, unknown>>;
  const record = useRef<TipRecord>({ label, side, className }).current;
  record.label = label;
  record.side = side;
  record.className = className;
  // A label that changes while its bubble is up (Play → Pause, a ticking count) is
  // re-read by the host; for every other Tip this is one comparison.
  useEffect(() => {
    host?.refresh(record);
  });

  const informational = isInformational(child);
  const ownId = useId();
  const descriptionId = informational
    ? typeof label === "string"
      ? `tipd-${hashString(label)}`
      : `tipd-${ownId.replace(/:/g, "")}`
    : null;
  useEffect(() => {
    if (descriptionId === null) return;
    return retainDescription(descriptionId, label);
  }, [descriptionId, label]);

  const pointerDown = useRef(false);

  const own: Record<string, unknown> = {
    onPointerEnter: chain(triggerProps.onPointerEnter, (event) => {
      const pointer = event as PointerEvent<HTMLElement>;
      if (pointer.pointerType === "touch" || getCoarse()) return;
      host?.show(pointer.currentTarget, record, "hover");
    }),
    onPointerLeave: chain(triggerProps.onPointerLeave, (event) => {
      if (getCoarse()) return;
      host?.hideSoon(event.currentTarget as HTMLElement);
    }),
    onPointerDown: chain(triggerProps.onPointerDown, (event) => {
      if (getCoarse()) return;
      // A click is not a request for the explanation: close, and do not reopen on the
      // focus this press is about to cause.
      pointerDown.current = true;
      host?.hide(event.currentTarget as HTMLElement);
      document.addEventListener(
        "pointerup",
        () => {
          pointerDown.current = false;
        },
        { once: true },
      );
    }),
    onFocus: chain(triggerProps.onFocus, (event) => {
      if (pointerDown.current || getCoarse()) return;
      const target = (event as FocusEvent<HTMLElement>).currentTarget;
      // Only keyboard focus explains itself; a focus moved by script on open does too
      // (Radix menus focus their first item), matching the old behaviour.
      host?.show(target, record, "focus");
    }),
    onBlur: chain(triggerProps.onBlur, (event) => {
      host?.hide(event.currentTarget as HTMLElement);
    }),
    onClick: chain(triggerProps.onClick, (event) => {
      if (getCoarse()) host?.toggleTouch(event.currentTarget as HTMLElement, record);
    }),
  };

  const slot = { ...triggerProps, ...own };
  const props = mergeProps(slot, child.props);
  const childRef = (child.props as { ref?: Ref<HTMLElement> }).ref;
  props.ref = composeRefs(childRef, ref);
  if (descriptionId !== null) {
    // Out of the tab order (see `isInformational`), described where it is read.
    if (props.tabIndex === 0 || props.tabIndex === "0") props.tabIndex = undefined;
    const existing =
      typeof props["aria-describedby"] === "string" ? (props["aria-describedby"] as string) : "";
    props["aria-describedby"] = existing ? `${existing} ${descriptionId}` : descriptionId;
  }
  return isValidElement(child) ? cloneElement(child, props) : child;
}
