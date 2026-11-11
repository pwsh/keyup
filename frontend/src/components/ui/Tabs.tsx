import { Fragment, type KeyboardEvent, type ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

/**
 * The one tab bar this app uses: a real `role="tablist"` with `aria-controls`,
 * a roving tabindex and arrow-key navigation, so a screen-reader user who is
 * told there is a tablist can actually drive it. Pages keep their own look by
 * passing their classes, or their own renderer for a bar built out of `Button`
 * or `Chip`.
 *
 * Ids come from a caller-supplied prefix rather than `useId` so the panel —
 * which is rarely a sibling of the bar — can name the tab that labels it
 * without a context or a hook.
 */
export interface TabItem {
  id: string;
  label: ReactNode;
  tip?: ReactNode;
}

/** Everything a tab button must carry; spread onto whatever renders it. */
export interface TabButtonProps {
  id: string;
  role: "tab";
  type: "button";
  "aria-selected": boolean;
  "aria-controls": string;
  tabIndex: 0 | -1;
  onClick: () => void;
}

export const tabDomId = (prefix: string, id: string): string => `${prefix}-tab-${id}`;
export const tabPanelDomId = (prefix: string, id: string): string => `${prefix}-panel-${id}`;

/** The three attributes the panel for `value` needs, for a host that is not a plain div. */
export function tabPanelProps(
  prefix: string,
  value: string,
): { id: string; role: "tabpanel"; "aria-labelledby": string } {
  return {
    id: tabPanelDomId(prefix, value),
    role: "tabpanel",
    "aria-labelledby": tabDomId(prefix, value),
  };
}

/** The default pill look. */
export const pillTabClass = (active: boolean): string =>
  cn(
    "whitespace-nowrap rounded-md px-3 py-1.5 text-sm",
    active ? "bg-accent-soft font-medium text-accent-ink" : "text-ink-2 hover:bg-surface-2 hover:text-ink",
  );

export function Tabs({
  idPrefix,
  items,
  value,
  onChange,
  label,
  className,
  listClassName,
  tabClassName = pillTabClass,
  renderTab,
  after,
}: {
  /** Namespace for the generated tab and panel ids; one per tab bar. */
  idPrefix: string;
  items: readonly TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Accessible name of the tablist, e.g. "Admin sections". */
  label: string;
  /** Wrapper around the tablist — the scroll box or the bordered bar. */
  className?: string;
  listClassName?: string;
  tabClassName?: (active: boolean) => string;
  /** For bars built out of `Button` or `Chip`; must spread the props it is given. */
  renderTab?: (item: TabItem, props: TabButtonProps, active: boolean) => ReactNode;
  /** Rendered after the tablist but outside it — a tablist may only contain tabs. */
  after?: ReactNode;
}) {
  const focusTab = (index: number) => {
    const next = items[((index % items.length) + items.length) % items.length];
    if (!next) return;
    onChange(next.id);
    // Every tab is mounted, so the element to move to already exists.
    document.getElementById(tabDomId(idPrefix, next.id))?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const current = items.findIndex((item) => item.id === value);
    if (current < 0) return;
    if (event.key === "ArrowRight") focusTab(current + 1);
    else if (event.key === "ArrowLeft") focusTab(current - 1);
    else if (event.key === "Home") focusTab(0);
    else if (event.key === "End") focusTab(items.length - 1);
    else return;
    event.preventDefault();
  };

  return (
    <div className={className}>
      <div role="tablist" aria-label={label} className={listClassName} onKeyDown={onKeyDown}>
        {items.map((item) => {
          const active = item.id === value;
          const props: TabButtonProps = {
            id: tabDomId(idPrefix, item.id),
            role: "tab",
            type: "button",
            "aria-selected": active,
            "aria-controls": tabPanelDomId(idPrefix, item.id),
            // Roving tabindex: one stop for the whole bar, arrows move inside it.
            tabIndex: active ? 0 : -1,
            onClick: () => onChange(item.id),
          };
          const button = renderTab ? (
            renderTab(item, props, active)
          ) : item.tip ? (
            <Tip label={item.tip}>
              <button {...props} className={tabClassName(active)}>
                {item.label}
              </button>
            </Tip>
          ) : (
            <button {...props} className={tabClassName(active)}>
              {item.label}
            </button>
          );
          return <Fragment key={item.id}>{button}</Fragment>;
        })}
      </div>
      {after}
    </div>
  );
}

/** The panel the selected tab controls; `aria-labelledby` points back at that tab. */
export function TabPanel({
  idPrefix,
  value,
  className,
  children,
}: {
  idPrefix: string;
  value: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div {...tabPanelProps(idPrefix, value)} className={className}>
      {children}
    </div>
  );
}
