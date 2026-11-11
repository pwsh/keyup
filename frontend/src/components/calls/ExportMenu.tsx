import { Download } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiUrl, isMockMode, toQueryParams } from "../../api/client";
import { useCreateExport, useExportJob } from "../../api/hooks";
import type { Call, CallFilterParams } from "../../api/types";
import { HELP, useHelpText } from "../../help/help";
import { downloadCsv, downloadText } from "../../lib/csv";
import { callTalkgroupLabel, formatBytes, formatDateTime, formatNumber } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Popover } from "../ui/Popover";

/**
 * The four ways off this page.
 *
 * CSV, JSON and M3U are streamed straight from the API, so a filter matching a
 * hundred thousand calls costs the browser nothing — except in mock mode, where
 * there is no server to stream from and the rows on screen are written out
 * locally instead. The audio bundle is always a background job, because zipping
 * audio takes minutes.
 */
export function ExportMenu({
  filters,
  visible,
  selected,
}: {
  filters: CallFilterParams;
  /** The rows currently loaded — what mock mode falls back to writing out. */
  visible: Call[];
  /** The ids ticked in the table; when any are, the menu offers them as their own export. */
  selected?: ReadonlySet<number>;
}) {
  // An aria-label, outside a tooltip: resolved so it is never the loading placeholder.
  const progressLabel = useHelpText("calls.export.progress");
  const [open, setOpen] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  // Which set the buttons act on. It starts on the selection the moment there is one,
  // because ticking rows and then exporting the whole archive by accident is the mistake
  // this menu is most able to make on somebody's behalf.
  const [scope, setScope] = useState<"filters" | "selected">("filters");
  const createExport = useCreateExport();
  const job = useExportJob(jobId);

  const ids = useMemo(() => [...(selected ?? [])], [selected]);
  const hasSelection = ids.length > 0;
  const onSelection = hasSelection && scope === "selected";
  useEffect(() => {
    setScope(hasSelection ? "selected" : "filters");
  }, [hasSelection]);

  // The selection *narrows* the filters rather than replacing them, on both sides of the
  // wire: `ids` is one more filter, so a tick left over from a wider view can never
  // resurrect a call the current filters exclude.
  const scoped: CallFilterParams = onSelection ? { ...filters, ids } : filters;
  const params = toQueryParams(scoped);
  const rows = onSelection ? visible.filter((call) => selected?.has(call.id)) : visible;

  const streamed = (format: "csv" | "json") => {
    if (!isMockMode()) {
      window.open(apiUrl("/calls/export", { ...params, format }), "_blank", "noreferrer");
      setOpen(false);
      return;
    }
    if (format === "csv") {
      downloadCsv(
        "keyup-calls",
        ["id", "started_at", "talkgroup", "tgid", "radio", "duration_s", "quality", "transcript"],
        rows.map((call) => [
          call.id,
          formatDateTime(call.started_at),
          callTalkgroupLabel(call),
          call.tgid ?? "",
          call.radio_label ?? "",
          call.duration_s,
          call.transcript_quality ?? "",
          call.transcript_text ?? "",
        ]),
      );
    } else {
      downloadText("keyup-calls.json", JSON.stringify(rows, null, 2), "application/json");
    }
    setOpen(false);
  };

  const playlist = () => {
    if (!isMockMode()) {
      window.open(apiUrl("/calls/playlist.m3u", params), "_blank", "noreferrer");
      setOpen(false);
      return;
    }
    const lines = ["#EXTM3U"];
    for (const call of rows) {
      lines.push(
        `#EXTINF:${Math.round(call.duration_s)},${callTalkgroupLabel(call)} — ${call.radio_label ?? ""}`,
      );
      lines.push(`${window.location.origin}/api/calls/${call.id}/audio`);
    }
    downloadText("keyup.m3u", lines.join("\n"), "audio/x-mpegurl");
    setOpen(false);
  };

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      className="w-72"
      trigger={
        <Button tip={HELP["calls.export.menu"]} size="sm" variant="secondary">
          <Download size={14} aria-hidden="true" /> Export
        </Button>
      }
    >
      <div className="space-y-1.5">
        {hasSelection ? (
          <div className="mb-1 flex gap-1.5">
            <Button
              tip={HELP["calls.export.selected"]}
              size="sm"
              variant={onSelection ? "primary" : "secondary"}
              active={onSelection}
              aria-pressed={onSelection}
              className="flex-1 justify-center"
              onClick={() => setScope("selected")}
            >
              Selected ({formatNumber(ids.length)})
            </Button>
            <Button
              tip={HELP["calls.export.filtered"]}
              size="sm"
              variant={onSelection ? "secondary" : "primary"}
              active={!onSelection}
              aria-pressed={!onSelection}
              className="flex-1 justify-center"
              onClick={() => setScope("filters")}
            >
              All matching
            </Button>
          </div>
        ) : null}
        <Button
          tip={HELP["calls.export.csv"]}
          size="sm"
          className="w-full justify-start"
          onClick={() => streamed("csv")}
        >
          CSV — metadata and transcripts
        </Button>
        <Button
          tip={HELP["calls.export.json"]}
          size="sm"
          className="w-full justify-start"
          onClick={() => streamed("json")}
        >
          JSON — for scripting
        </Button>
        <Button tip={HELP["calls.export.m3u"]} size="sm" className="w-full justify-start" onClick={playlist}>
          M3U playlist
        </Button>
        <Button
          tip={HELP["calls.export.bundle"]}
          size="sm"
          variant="primary"
          className="w-full justify-start"
          disabled={createExport.isPending || job.data?.status === "running"}
          onClick={() =>
            createExport.mutate(scoped, {
              onSuccess: (created) => {
                setJobId(created.id);
                toast("Audio bundle queued", {
                  tone: "success",
                  body: `${created.call_count} calls. The download appears here when it is ready.`,
                });
              },
              onError: () => toast("Could not queue the bundle", { tone: "error" }),
            })
          }
        >
          Audio bundle (zip)
        </Button>

        {job.data ? (
          <div className="rounded-md border border-line p-2 text-xs text-ink-2">
            <p className="flex items-center justify-between gap-2">
              <span>{job.data.status}</span>
              <span className="tabular-nums">{Math.round(job.data.progress * 100)}%</span>
            </p>
            <div
              className="mt-1 h-1 overflow-hidden rounded-full bg-line"
              role="progressbar"
              aria-valuenow={Math.round(job.data.progress * 100)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={progressLabel}
            >
              <div className="h-full bg-accent" style={{ width: `${job.data.progress * 100}%` }} />
            </div>
            {job.data.download_url ? (
              <Button
                tip={HELP["calls.export.download"]}
                size="sm"
                variant="primary"
                className="mt-2 w-full"
                onClick={() => window.open(job.data?.download_url ?? "", "_blank", "noreferrer")}
              >
                Download {formatBytes(job.data.bytes)}
              </Button>
            ) : null}
          </div>
        ) : null}

        <p className="pt-1 text-[11px] text-muted">
          {onSelection
            ? `The file covers the ${formatNumber(ids.length)} calls you ticked.`
            : visible.length
              ? `${formatNumber(visible.length)} calls loaded; the file covers every match.`
              : null}
        </p>
      </div>
    </Popover>
  );
}
