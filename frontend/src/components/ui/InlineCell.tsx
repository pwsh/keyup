/**
 * Table cells that are the edit.
 *
 * The directory pages used to put every change behind a pencil, a modal, a Save
 * and a close — four interactions to rename a channel. These cells collapse that
 * to one: click (or focus the row and press Enter), type, leave. They are for the
 * short, single-value fields whose meaning is obvious from the column header;
 * anything that needs a sentence of explanation, or that has an invalid
 * intermediate state, still belongs in the dialog.
 *
 * The saving rules are the ones `RecorderPolicyStrip` already established, made
 * reusable:
 *
 * * **Commit on blur and on Enter**, never per keystroke — typing "10" over "100"
 *   must not fire three saves on the way through "1" and "10".
 * * **Escape reverts** to the server's value and gives focus back to the cell, so
 *   a wrong edit costs nothing.
 * * **Nothing is sent when nothing changed.** Tabbing across a row is free.
 * * **The failure stays in the cell.** A per-cell spinner while the save is in
 *   flight, and on rejection the API's own sentence underneath it plus a revert —
 *   a toast that has scrolled away cannot tell you which of forty rows refused.
 *
 * Text and number cells render as a *button* until they are being edited, rather
 * than as forty live inputs: a page of always-on inputs makes tabbing through the
 * table impossible and asks the browser to lay out hundreds of controls nobody is
 * using. Selects, switches and the color swatch are already one-interaction
 * controls, so they are rendered live.
 */

import { LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { cn } from "../../lib/cn";
import { errorDetail } from "../../state/toasts";
import { inputClass } from "./Field";
import { Select, type SelectOption } from "./Select";
import { Switch } from "./Switch";
import { Tip } from "./Tooltip";

/** What every cell in the family needs: the value, how to save it, and its name. */
export interface InlineCellProps<T> {
  value: T;
  /**
   * Sends exactly this field. Reject to make the cell show why — `mutateAsync`
   * from a react-query mutation does that for free.
   */
  onSave: (next: T) => Promise<unknown>;
  /** The control's accessible name; a column header alone does not name a control. */
  label: string;
  /** The tooltip, from `HELP`. */
  tip: string;
  disabled?: boolean;
  className?: string;
}

/**
 * The save state one cell owns.
 *
 * `onSave` is called only when the value actually changed, so the caller never has
 * to compare; and a rejection is caught here rather than escaping as an unhandled
 * promise, which is what a bare `mutateAsync` would do.
 */
function useInlineSave<T>(
  value: T,
  onSave: (next: T) => Promise<unknown>,
  onRevert?: () => void,
): {
  pending: boolean;
  error: string | null;
  commit: (next: T) => void;
  clearError: () => void;
} {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The cell can unmount while a save is in flight (the list refetches and
  // re-keys); setting state on the way back would warn and change nothing.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const commit = useCallback(
    (next: T) => {
      setError(null);
      if (Object.is(next, value)) return;
      setPending(true);
      void onSave(next)
        .catch((cause: unknown) => {
          if (!alive.current) return;
          setError(errorDetail(cause) ?? "The server refused this change");
          onRevert?.();
        })
        .finally(() => {
          if (alive.current) setPending(false);
        });
    },
    [value, onSave, onRevert],
  );

  return { pending, error, commit, clearError: useCallback(() => setError(null), []) };
}

/** The spinner and the refusal, in the flow of the cell rather than in a toast. */
function CellStatus({ pending, error, id }: { pending: boolean; error: string | null; id: string }) {
  return (
    <>
      {pending ? (
        <LoaderCircle
          size={12}
          className="shrink-0 animate-spin text-muted"
          aria-label="Saving"
          role="status"
        />
      ) : null}
      {error ? (
        <span id={id} role="alert" className="block max-w-56 break-words text-[11px] text-critical-ink">
          {error}
        </span>
      ) : null}
    </>
  );
}

/**
 * The resting look of a text or number cell: a value you can click straight into.
 * The focus ring comes from the global `:focus-visible` rule in `index.css`.
 */
const triggerClass =
  "w-full truncate rounded px-1 py-0.5 text-left hover:bg-surface-2 hover:ring-1 hover:ring-line";

/* --------------------------------------------------------------------- text */

/**
 * A short free-text field — an alias, a name, a rank.
 *
 * `emptyAsNull` is the difference between a column the API models as "not set"
 * (`agency_code`, `alias`) and one it models as an empty string; clearing the box
 * has to send whichever of the two that column means.
 *
 * `placeholder` is the example inside the *open* input and nothing else. It used
 * to double as what an unset cell showed at rest, which meant every radio without
 * a unit number read as the unit "E1" — the example, rendered in the same place,
 * the same size and (before this) the same weight as a real value, in a column of
 * rows where some of the neighbours genuinely are called E1. An unset cell now
 * shows a muted em dash, which cannot be read as a value, and the invitation
 * (`hint`) is a `title` the reader gets by pointing at it or tabbing to it.
 */
export function InlineText({
  value,
  onSave,
  label,
  tip,
  disabled,
  className,
  placeholder,
  hint,
  emptyAsNull = false,
  mono = false,
}: Omit<InlineCellProps<string | null>, "value"> & {
  value: string | null;
  /** The example shown inside the input while editing. Never rendered at rest. */
  placeholder?: string;
  /** What an empty cell offers, as a `title` — "assign a unit", "name it". */
  hint?: string;
  /** Send `null` rather than `""` when the field is emptied. */
  emptyAsNull?: boolean;
  mono?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const errorId = useId();

  // Adopt whatever the server now says: a list that refetched under this cell,
  // or a save made from the dialog, must not be masked by stale draft text.
  useEffect(() => setDraft(value ?? ""), [value]);

  const revert = useCallback(() => setDraft(value ?? ""), [value]);
  const { pending, error, commit, clearError } = useInlineSave(value, onSave, revert);

  const finish = () => {
    setEditing(false);
    const text = draft.trim();
    commit(emptyAsNull && text === "" ? null : text);
  };

  if (disabled) {
    return <span className={cn("block truncate px-1 text-ink-2", className)}>{value || "—"}</span>;
  }

  return (
    <span className={cn("block min-w-0", className)}>
      {editing ? (
        <input
          // Autofocus is the whole point of the swap: the click that opened the
          // cell was the click that meant "edit this".
          // biome-ignore lint/a11y/noAutofocus: the cell only exists once activated
          autoFocus
          aria-label={label}
          aria-invalid={error !== null}
          aria-describedby={error ? errorId : undefined}
          value={draft}
          placeholder={placeholder}
          disabled={pending}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={finish}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              finish();
              triggerRef.current?.focus();
            } else if (event.key === "Escape") {
              event.preventDefault();
              revert();
              clearError();
              setEditing(false);
              triggerRef.current?.focus();
            }
          }}
          className={cn(inputClass, "h-7 py-0 text-sm", mono && "font-mono", error && "border-critical")}
        />
      ) : (
        <span className="flex items-center gap-1">
          <Tip label={tip}>
            <button
              ref={triggerRef}
              type="button"
              aria-label={`${label}: ${value || "not set"}. Press Enter to edit`}
              // Only when there is nothing there: a `title` on a filled cell would
              // pop a tip that merely repeats the word already under the pointer.
              title={value ? undefined : hint}
              onClick={() => setEditing(true)}
              className={cn(triggerClass, mono && "font-mono text-xs", !value && "text-muted")}
            >
              {value || "—"}
            </button>
          </Tip>
          <CellStatus pending={pending} error={error} id={errorId} />
        </span>
      )}
      {editing ? <CellStatus pending={pending} error={error} id={errorId} /> : null}
    </span>
  );
}

/* ------------------------------------------------------------------- number */

/**
 * A whole number in a fixed range — a queue priority, a day count.
 *
 * Out-of-range text is rejected here rather than sent, because the API's 422 for
 * it is a paragraph of Pydantic and the cell can say the same thing in a line.
 */
export function InlineNumber({
  value,
  onSave,
  label,
  tip,
  disabled,
  className,
  min,
  max,
}: Omit<InlineCellProps<number>, "value"> & { value: number; min?: number; max?: number }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(value));
  const [local, setLocal] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const errorId = useId();

  useEffect(() => setDraft(String(value)), [value]);

  const revert = useCallback(() => setDraft(String(value)), [value]);
  const { pending, error, commit, clearError } = useInlineSave(value, onSave, revert);

  const finish = () => {
    setEditing(false);
    setLocal(null);
    const text = draft.trim();
    // `Number("")` is 0, and an emptied box means "I changed my mind", not "zero".
    if (text === "") {
      revert();
      return;
    }
    const next = Number(text);
    const bounded =
      Number.isInteger(next) && (min === undefined || next >= min) && (max === undefined || next <= max);
    if (!bounded) {
      revert();
      setLocal(
        min !== undefined && max !== undefined
          ? `Whole number between ${min} and ${max}`
          : "Has to be a whole number",
      );
      return;
    }
    commit(next);
  };

  const shown = local ?? error;

  if (disabled) {
    return <span className={cn("block px-1 tabular-nums text-ink-2", className)}>{value}</span>;
  }

  return (
    <span className={cn("block min-w-0", className)}>
      {editing ? (
        <input
          // biome-ignore lint/a11y/noAutofocus: the cell only exists once activated
          autoFocus
          inputMode="numeric"
          aria-label={label}
          aria-invalid={shown !== null}
          aria-describedby={shown ? errorId : undefined}
          value={draft}
          disabled={pending}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={finish}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              finish();
              triggerRef.current?.focus();
            } else if (event.key === "Escape") {
              event.preventDefault();
              revert();
              clearError();
              setLocal(null);
              setEditing(false);
              triggerRef.current?.focus();
            }
          }}
          className={cn(inputClass, "h-7 w-16 py-0 text-sm tabular-nums", shown && "border-critical")}
        />
      ) : (
        <span className="flex items-center gap-1">
          <Tip label={tip}>
            <button
              ref={triggerRef}
              type="button"
              aria-label={`${label}: ${value}. Press Enter to edit`}
              onClick={() => {
                setLocal(null);
                setEditing(true);
              }}
              className={cn(triggerClass, "tabular-nums")}
            >
              {value}
            </button>
          </Tip>
          <CellStatus pending={pending} error={shown} id={errorId} />
        </span>
      )}
      {editing ? <CellStatus pending={pending} error={shown} id={errorId} /> : null}
    </span>
  );
}

/* ------------------------------------------------------------------- select */

/**
 * One of a fixed set — a category, a kind, a service tag.
 *
 * Rendered live rather than behind a trigger: a select is already one interaction,
 * and swapping it in on click would cost the reader a second one.
 */
export function InlineSelect({
  value,
  onSave,
  label,
  tip,
  disabled,
  className,
  options,
}: Omit<InlineCellProps<string>, "value"> & { value: string; options: SelectOption[] }) {
  const { pending, error, commit } = useInlineSave(value, onSave);
  const errorId = useId();
  return (
    <span className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="flex items-center gap-1">
        <Select
          value={value}
          onValueChange={commit}
          options={options}
          tip={tip}
          ariaLabel={label}
          size="sm"
          disabled={disabled || pending}
          className="h-7 w-full min-w-0 text-xs"
        />
        {pending ? <CellStatus pending error={null} id={errorId} /> : null}
      </span>
      <CellStatus pending={false} error={error} id={errorId} />
    </span>
  );
}

/* ------------------------------------------------------------------- switch */

/** An on/off column — transcribe, hidden, on the roster. */
export function InlineSwitch({
  value,
  onSave,
  label,
  tip,
  disabled,
  className,
}: Omit<InlineCellProps<boolean>, "value"> & { value: boolean }) {
  const { pending, error, commit } = useInlineSave(value, onSave);
  const errorId = useId();
  return (
    <span className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="flex items-center gap-1">
        <Switch
          checked={value}
          onCheckedChange={commit}
          label={null}
          tip={`${label}. ${tip}`}
          disabled={disabled || pending}
        />
        {pending ? <CellStatus pending error={null} id={errorId} /> : null}
      </span>
      <CellStatus pending={false} error={error} id={errorId} />
    </span>
  );
}

/* -------------------------------------------------------------------- color */

/**
 * A channel's color, as the swatch itself.
 *
 * The native `<input type="color">` *is* the swatch — a separate button that opens
 * a hidden input would be two controls where the platform already gives one, and
 * the native picker is the one the reader's OS knows how to drive. It fires while
 * the picker is being dragged, so the value is only sent when the picker closes
 * and focus leaves.
 */
export function InlineColor({
  value,
  onSave,
  label,
  tip,
  disabled,
  className,
  fallback = "#2a78d6",
}: Omit<InlineCellProps<string | null>, "value"> & { value: string | null; fallback?: string }) {
  const [draft, setDraft] = useState(value ?? fallback);
  useEffect(() => setDraft(value ?? fallback), [value, fallback]);
  const revert = useCallback(() => setDraft(value ?? fallback), [value, fallback]);
  const { pending, error, commit } = useInlineSave(value, onSave, revert);
  const errorId = useId();

  return (
    <span className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="flex items-center gap-1">
        <Tip label={tip}>
          <input
            type="color"
            aria-label={label}
            aria-invalid={error !== null}
            aria-describedby={error ? errorId : undefined}
            value={draft}
            disabled={disabled || pending}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => commit(draft)}
            className={cn(
              "h-6 w-8 cursor-pointer rounded border border-control-border bg-surface p-0.5",
              "disabled:cursor-not-allowed disabled:opacity-50",
              // A channel with no color of its own shows the fallback dimmed, so
              // "inherits the category color" reads differently from "is blue".
              value === null && "opacity-50",
            )}
          />
        </Tip>
        {pending ? <CellStatus pending error={null} id={errorId} /> : null}
      </span>
      <CellStatus pending={false} error={error} id={errorId} />
    </span>
  );
}
