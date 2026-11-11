import type { ReactNode } from "react";
import { ResolvedHelp } from "../../help/help";
import { cn } from "../../lib/cn";

export function Card({
  className,
  children,
  id,
  role,
  "aria-labelledby": ariaLabelledBy,
}: {
  className?: string;
  children: ReactNode;
  id?: string;
  role?: "tabpanel";
  "aria-labelledby"?: string;
}) {
  return (
    <section
      id={id}
      role={role}
      aria-labelledby={ariaLabelledBy}
      className={cn("rounded-md border border-line bg-surface", className)}
    >
      {children}
    </section>
  );
}

export function CardHeader({
  title,
  hint,
  actions,
  className,
}: {
  title: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "flex flex-wrap items-start justify-between gap-2 border-b border-line px-4 py-3",
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {/* Hints are usually `HELP[...]`, which is a placeholder until the help text loads. */}
        {hint ? (
          <p className="mt-0.5 text-xs text-ink-2">
            <ResolvedHelp>{hint}</ResolvedHelp>
          </p>
        ) : null}
      </div>
      {/* Not `shrink-0`: a wide action group would otherwise widen the whole page
          on a phone. It wraps instead. */}
      {actions ? <div className="flex min-w-0 flex-wrap items-center gap-1.5">{actions}</div> : null}
    </header>
  );
}
