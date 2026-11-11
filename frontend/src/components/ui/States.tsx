import { Inbox, RefreshCw, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";
import { describeError } from "../../api/client";
import { ResolvedHelp } from "../../help/help";
import { cn } from "../../lib/cn";
import { Button } from "./Button";

export function EmptyState({
  title,
  hint,
  icon,
  action,
  className,
}: {
  title: string;
  hint?: string;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-6 py-12 text-center", className)}>
      <span className="text-muted" aria-hidden="true">
        {icon ?? <Inbox size={28} />}
      </span>
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint ? (
        <p className="max-w-md text-xs text-ink-2">
          <ResolvedHelp>{hint}</ResolvedHelp>
        </p>
      ) : null}
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  // A sentence first; the status and the server's own words only behind "Details".
  const { message, detail } = describeError(error);
  return (
    <div
      className={cn("flex flex-col items-center justify-center gap-2 px-6 py-10 text-center", className)}
      role="alert"
    >
      <span className="text-critical-ink" aria-hidden="true">
        <TriangleAlert size={26} />
      </span>
      <p className="text-sm font-medium text-ink">Could not load this data</p>
      <p className="max-w-md break-words text-xs text-ink-2">{message}</p>
      {detail ? (
        <details className="max-w-md text-[11px] text-muted">
          <summary className="cursor-pointer">Details</summary>
          <p className="mt-1 break-words font-mono">{detail}</p>
        </details>
      ) : null}
      {onRetry ? (
        <Button tip="Run the request again" onClick={onRetry} size="sm">
          <RefreshCw size={14} aria-hidden="true" /> Retry
        </Button>
      ) : null}
    </div>
  );
}
