import { AlertTriangle, CircleAlert, Info, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useActivityLog } from "../api/hooks";
import { useActivityStream } from "../api/live";
import type { ActivityEvent } from "../api/types";
import { PageHeader } from "../components/layout/PageHeader";
import { ConnectionBadge } from "../components/layout/StatusBadges";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Select } from "../components/ui/Select";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { cn } from "../lib/cn";
import { formatRelative } from "../lib/format";

/** How many rows the merged (backfill + live) view keeps in memory. */
const MAX_ROWS = 500;

const SOURCE_OPTIONS = [
  { value: "", label: "All sources" },
  { value: "ingest", label: "Ingest" },
  { value: "upload", label: "Upload" },
  { value: "transcribe", label: "Transcribe" },
  { value: "stt", label: "STT engine" },
  { value: "rr", label: "RadioReference" },
  { value: "import", label: "Import" },
  { value: "system", label: "System" },
  // The standby arbiter is its own source, not part of "System": "why did the spare come on
  // last night" is a question an operator answers by filtering, in one click.
  { value: "arbiter", label: "Arbiter" },
];

const LEVEL_OPTIONS = [
  { value: "", label: "All levels" },
  { value: "info", label: "Info" },
  { value: "warn", label: "Warnings" },
  { value: "error", label: "Errors" },
];

/** Color + icon for a row's severity, reusing the shared soft/ink tokens. */
function levelStyle(level: string): { icon: typeof Info; chip: string; dot: string } {
  if (level === "error") {
    return { icon: CircleAlert, chip: "bg-critical-soft text-critical-ink", dot: "bg-critical" };
  }
  if (level === "warn") {
    return { icon: AlertTriangle, chip: "bg-warning-soft text-warning-ink", dot: "bg-warning" };
  }
  return { icon: Info, chip: "bg-good-soft text-good-ink", dot: "bg-good" };
}

function ActivityRow({ event }: { event: ActivityEvent }) {
  const style = levelStyle(event.level);
  const Icon = style.icon;
  const body = (
    <div className="flex items-start gap-3 px-3 py-2">
      <span
        className={cn("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full", style.chip)}
        aria-hidden="true"
      >
        <Icon size={14} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-mono text-[11px] uppercase tracking-wide text-ink-2">{event.source}</span>
          <span className="font-mono text-[11px] text-muted">{event.kind}</span>
        </div>
        <p className="mt-0.5 break-words text-sm text-ink">{event.message}</p>
      </div>
      <time className="shrink-0 whitespace-nowrap text-xs text-ink-2" dateTime={event.at} title={event.at}>
        {formatRelative(event.at)}
      </time>
    </div>
  );
  return (
    <li className="border-b border-line last:border-b-0">
      {event.call_id != null ? (
        <Link
          to={`/calls/${event.call_id}`}
          className="block transition-colors hover:bg-surface-2 focus-visible:bg-surface-2"
        >
          {body}
        </Link>
      ) : (
        body
      )}
    </li>
  );
}

/** Admin → Activity: the operational log, backfilled from the API then tailed live. */
export function Activity() {
  const [source, setSource] = useState("");
  const [level, setLevel] = useState("");
  const [live, setLive] = useState(true);
  const [paused, setPaused] = useState(false);

  const backfill = useActivityLog({
    source: source || undefined,
    level: level || undefined,
    limit: 200,
  });
  const stream = useActivityStream(live && !paused);

  const rows = useMemo(() => {
    const merged = new Map<number, ActivityEvent>();
    for (const event of stream.events) merged.set(event.id, event);
    for (const event of backfill.data ?? []) merged.set(event.id, event);
    return [...merged.values()]
      .filter((event) => (!source || event.source === source) && (!level || event.level === level))
      .sort((a, b) => b.id - a.id)
      .slice(0, MAX_ROWS);
  }, [backfill.data, stream.events, source, level]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Activity"
        description="The ingest and transcription pipeline as it runs: calls accepted, duplicates, transcriptions and errors."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <ConnectionBadge state={live && !paused ? stream.state : "closed"} />
            <Switch
              checked={live}
              onCheckedChange={setLive}
              label="Follow new activity"
              tip="Add new activity to the top of the list as it happens"
            />
            <Button
              tip={
                paused
                  ? "Start adding new activity to the list again"
                  : "Stop adding new activity so you can read what is there"
              }
              size="sm"
              active={paused}
              onClick={() => setPaused(!paused)}
            >
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button
              tip="Empty the list and start collecting from now"
              size="sm"
              variant="ghost"
              onClick={stream.clear}
            >
              <Trash2 size={14} aria-hidden="true" /> Clear
            </Button>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-2 px-4 pb-2 md:px-6">
        <Select
          value={source}
          onValueChange={setSource}
          options={SOURCE_OPTIONS}
          tip="Filter by pipeline stage"
          ariaLabel="Filter by source"
          size="sm"
          className="w-44"
        />
        <Select
          value={level}
          onValueChange={setLevel}
          options={LEVEL_OPTIONS}
          tip="Filter by severity"
          ariaLabel="Filter by level"
          size="sm"
          className="w-36"
        />
      </div>

      <div className="min-h-0 flex-1 p-4 md:p-6">
        <Card className="flex h-full min-h-0 flex-col overflow-hidden">
          {backfill.isLoading ? (
            <SkeletonText lines={8} className="p-4" />
          ) : backfill.isError ? (
            <ErrorState error={backfill.error} onRetry={() => void backfill.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState
              title="No activity yet"
              hint="Pipeline events appear here as calls are ingested and transcribed. Adjust the filters or wait for the next call."
            />
          ) : (
            <ul className="min-h-0 flex-1 overflow-y-auto">
              {rows.map((event) => (
                <ActivityRow key={event.id} event={event} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
