import { MoreHorizontal, SlidersHorizontal } from "lucide-react";
import { type ReactNode, useState } from "react";
import { HELP } from "../../help/help";
import { Button } from "../ui/Button";
import { Popover } from "../ui/Popover";
import { Switch } from "../ui/Switch";
import { CALL_COLUMN_KEYS, CALL_COLUMN_LABELS, type CallColumnKey, DEFAULT_CALL_COLUMNS } from "./CallsTable";

/**
 * The column checkboxes: one per available column, so the reader can trim the table to
 * what they care about. The choice is the caller's to persist (it lives in
 * `localStorage`, not the URL — it changes the view, not the result set).
 */
export function ColumnPicker({
  columns,
  onChange,
}: {
  columns: CallColumnKey[];
  onChange: (next: CallColumnKey[]) => void;
}) {
  const toggle = (key: CallColumnKey) => {
    const next = CALL_COLUMN_KEYS.filter((candidate) =>
      candidate === key ? !columns.includes(key) : columns.includes(candidate),
    );
    onChange(next);
  };

  return (
    <fieldset className="flex flex-col gap-0.5">
      <legend className="mb-1 px-1 text-xs font-medium text-ink-2">Columns</legend>
      {CALL_COLUMN_KEYS.map((key) => {
        const checked = columns.includes(key);
        // The last remaining column cannot be unticked, so the table never empties.
        const isLast = checked && columns.length === 1;
        return (
          <label
            key={key}
            className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm text-ink hover:bg-surface-2"
          >
            <input
              type="checkbox"
              checked={checked}
              disabled={isLast}
              onChange={() => toggle(key)}
              className="h-3.5 w-3.5 accent-[var(--accent)] disabled:opacity-50"
            />
            {CALL_COLUMN_LABELS[key]}
          </label>
        );
      })}
      <button
        type="button"
        onClick={() => onChange(DEFAULT_CALL_COLUMNS)}
        className="mt-1 rounded px-1.5 py-1 text-left text-xs text-accent-ink hover:bg-surface-2"
      >
        Reset to defaults
      </button>
    </fieldset>
  );
}

/**
 * "View": how the table is laid out — the transcript on its own row, and which columns
 * show. These were two toolbar controls ("Transcript row" and "Columns"); at 1440 px with
 * the sidebar and the filter rail open they pushed Export and Feed off the right edge.
 */
export function ViewMenu({
  columns,
  onColumnsChange,
  transcriptRow,
  onTranscriptRowChange,
}: {
  columns: CallColumnKey[];
  onColumnsChange: (next: CallColumnKey[]) => void;
  transcriptRow: boolean;
  onTranscriptRowChange: (next: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      align="end"
      className="w-64"
      trigger={
        <Button tip={HELP["calls.toolbar.columns"]} size="sm" variant="ghost" active={open}>
          <SlidersHorizontal size={14} aria-hidden="true" /> View
        </Button>
      }
    >
      <div className="flex flex-col gap-3">
        <Switch
          checked={transcriptRow}
          onCheckedChange={onTranscriptRowChange}
          label="Transcript row"
          tip={HELP["calls.toolbar.transcriptRow"]}
        />
        <ColumnPicker columns={columns} onChange={onColumnsChange} />
      </div>
    </Popover>
  );
}

/**
 * The phone toolbar's "More": everything that is not Filters, the count or Live. One
 * row of toolbar on a phone instead of three, so the first call starts near the top.
 */
export function MoreMenu({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      align="end"
      className="w-64"
      trigger={
        <Button
          tip="More options for this list"
          size="iconSm"
          variant="ghost"
          active={open}
          aria-label="More options"
        >
          <MoreHorizontal size={16} aria-hidden="true" />
        </Button>
      }
    >
      <div className="flex flex-col items-start gap-3">{children}</div>
    </Popover>
  );
}
