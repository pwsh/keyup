import type { InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from "react";
import { useId } from "react";
import type { HelpId } from "../../help/help";
import { cn } from "../../lib/cn";
import { InfoDot } from "../help/InfoDot";

export const inputClass =
  "h-9 w-full rounded-md border border-control-border bg-surface px-2.5 text-sm text-ink placeholder:text-muted";

/**
 * A labeled control. The explanation is an `InfoDot` beside the label rather
 * than a tooltip on the input itself, because a tooltip over a text field fights
 * with typing.
 *
 * The dot is a *sibling* of the `<label>`, never inside it: a `<button>` within a
 * label is itself labelable, so nesting it would give the page two controls both
 * called "Username" and leave a screen-reader user guessing which is the field.
 */
export function Field({
  label,
  help,
  helpTo,
  children,
  hint,
  badge,
  className,
}: {
  label: ReactNode;
  help: HelpId;
  /**
   * Where the dot links, for a field whose answer does not fit in a tooltip —
   * `/help#help-categories` and friends. Absent, the dot is a plain button and
   * the tooltip is the whole explanation.
   */
  helpTo?: string;
  children: (id: string) => ReactNode;
  hint?: ReactNode;
  /**
   * A status pill — "pending", "restarts the tuner" — on the label's own line,
   * pushed to its right. It belongs there rather than under the input: a badge
   * on a line of its own reads as another control and costs a row of height in
   * a form that already has too many.
   */
  badge?: ReactNode;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("min-w-0", className)}>
      <span className="mb-1 flex min-h-6 items-center gap-1 text-xs font-medium text-ink-2">
        <label htmlFor={id}>{label}</label>
        <InfoDot id={help} to={helpTo} />
        {badge ? <span className="ml-auto flex items-center">{badge}</span> : null}
      </span>
      {children(id)}
      {hint ? <p className="mt-1 text-[11px] text-ink-2">{hint}</p> : null}
    </div>
  );
}

/**
 * The two-column grid the dialogs lay their short fields out on.
 *
 * Numbers, toggles and selects are all about a third of a line wide, so one per
 * row left the dialogs mostly white space. Anything genuinely long — a peer
 * list, a key, a band editor — takes `className="sm:col-span-2"` and spans both.
 */
export function FieldGrid({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2", className)}>{children}</div>;
}

export function TextInput({
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { className?: string }) {
  return <input {...props} className={cn(inputClass, className)} />;
}

export function TextArea({
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { className?: string }) {
  return (
    <textarea
      {...props}
      className={cn(
        "w-full rounded-md border border-control-border bg-surface px-2.5 py-2 text-sm text-ink placeholder:text-muted",
        className,
      )}
    />
  );
}
