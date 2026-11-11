/**
 * "40 selected · Edit · Clear · Select all loaded" — the bar that turns a set of
 * ticks into an action.
 *
 * It is sticky to the bottom of the list rather than pinned under the toolbar,
 * because the selection is made by scrolling *down* a table and the bar has to
 * stay where the reader's eye already is. It renders nothing at all when nothing
 * is ticked: a permanent empty bar would cost every one of these pages a row of
 * height to say "you have selected nothing", which the empty checkboxes already
 * say.
 *
 * The count is of the whole selection, not of this page. Ticking rows, paging,
 * and ticking more is the normal way to gather forty channels out of six hundred,
 * so the number has to outlive the page — which is why the set lives on the page
 * component and not in the table (see `Talkgroups`).
 */

import { Pencil, X } from "lucide-react";
import { HELP } from "../../help/help";
import { formatNumber } from "../../lib/format";
import { Button } from "../ui/Button";
import { Tip } from "../ui/Tooltip";

export function SelectionBar({
  count,
  noun,
  onEdit,
  onClear,
  onSelectAllLoaded,
  loadedCount,
  canWrite,
  atLimit = false,
}: {
  count: number;
  /** Plural, for the sentence: "talkgroups", "radios", "units", "people". */
  noun: string;
  onEdit: () => void;
  onClear: () => void;
  onSelectAllLoaded: () => void;
  /** How many rows this page is showing, so "select all loaded" can say how many. */
  loadedCount: number;
  canWrite: boolean;
  /** The selection has hit the server's cap; Edit would 422, so it says so instead. */
  atLimit?: boolean;
}) {
  if (count === 0) return null;
  return (
    <div className="sticky bottom-0 z-20 -mx-4 mt-2 flex flex-wrap items-center gap-2 border-t border-line bg-surface px-4 py-2 shadow-[0_-2px_8px_rgba(0,0,0,0.06)] md:-mx-6 md:px-6">
      <Tip label={HELP["app.bulk.selected"]}>
        <p tabIndex={0} className="text-sm tabular-nums text-ink-2">
          <span className="font-semibold text-ink">{formatNumber(count)}</span> {noun} selected
        </p>
      </Tip>

      <Button
        tip={!canWrite ? HELP["app.readOnly"] : atLimit ? HELP["app.bulk.limit"] : HELP["app.bulk.edit"]}
        size="sm"
        variant="primary"
        disabled={!canWrite || atLimit}
        onClick={onEdit}
      >
        <Pencil size={14} aria-hidden="true" /> Edit {formatNumber(count)}
      </Button>

      <Button tip={HELP["app.bulk.selectAllLoaded"]} size="sm" onClick={onSelectAllLoaded}>
        Select all {formatNumber(loadedCount)} loaded
      </Button>

      <Button tip={HELP["app.bulk.clear"]} size="sm" variant="ghost" onClick={onClear}>
        <X size={14} aria-hidden="true" /> Clear
      </Button>

      {atLimit ? <p className="text-[11px] text-warning-ink">One edit covers at most 500 rows.</p> : null}
    </div>
  );
}

/**
 * The header checkbox for a selectable grid: on when the whole page is ticked,
 * indeterminate when only some of it is.
 *
 * `indeterminate` is a DOM property with no HTML attribute, so it is set through a
 * ref rather than declared in JSX — the same arrangement `CallsTable` uses.
 */
export function SelectAllCheckbox({
  allChecked,
  someChecked,
  onChange,
  disabled = false,
}: {
  allChecked: boolean;
  someChecked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <Tip label={HELP["app.bulk.selectPage"]}>
      <input
        type="checkbox"
        aria-label={allChecked ? "Clear the selection on this page" : "Select every row on this page"}
        checked={allChecked}
        disabled={disabled}
        ref={(node) => {
          if (node) node.indeterminate = someChecked && !allChecked;
        }}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3.5 w-3.5 accent-[var(--accent)]"
      />
    </Tip>
  );
}

/** One row's checkbox. `label` names the row, because the column header is blank. */
export function SelectRowCheckbox({
  checked,
  label,
  onChange,
  disabled = false,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <Tip label={HELP["app.bulk.select"]}>
      <input
        type="checkbox"
        aria-label={`Select ${label}`}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3.5 w-3.5 accent-[var(--accent)]"
      />
    </Tip>
  );
}

/**
 * The reducer every one of the four pages shares: tick, untick, and tick a whole
 * page. Written once here so "select all" cannot come to mean two different things
 * on two of them.
 */
export function toggleInSet(current: ReadonlySet<number>, ids: number[], on: boolean): ReadonlySet<number> {
  const next = new Set(current);
  for (const id of ids) {
    if (on) next.add(id);
    else next.delete(id);
  }
  return next;
}
