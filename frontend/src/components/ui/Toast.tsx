import { CircleCheck, TriangleAlert, X } from "lucide-react";
import { cn } from "../../lib/cn";
import { dismissToast, useToastStore } from "../../state/toasts";

export { toast } from "../../state/toasts";

/** Live region for background results (job runs, exports, saves). */
export function Toaster() {
  const items = useToastStore((state) => state.toasts);
  return (
    <div
      className="pointer-events-none fixed bottom-20 right-4 z-[60] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-2 md:bottom-4"
      role="status"
      aria-live="polite"
    >
      {items.map((t) => (
        <div
          key={t.id}
          className={cn(
            "pointer-events-auto flex items-start gap-2 rounded-md border bg-surface p-3 shadow-lg",
            t.tone === "error" ? "border-critical" : t.tone === "success" ? "border-good" : "border-line",
          )}
        >
          <span
            className={cn("mt-0.5", t.tone === "error" ? "text-critical-ink" : "text-good-ink")}
            aria-hidden="true"
          >
            {t.tone === "error" ? <TriangleAlert size={16} /> : <CircleCheck size={16} />}
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink">{t.title}</p>
            {t.body ? (
              <p className="mt-0.5 whitespace-pre-line break-words text-xs text-ink-2">{t.body}</p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => dismissToast(t.id)}
            className="rounded p-0.5 text-ink-2 hover:bg-surface-2 hover:text-ink"
            aria-label="Dismiss notification"
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  );
}
