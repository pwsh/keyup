import { ArrowDownRight, ArrowRight, ArrowUpRight, HardDrive, HeartPulse } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { useLiveActivity, useSystemStatus } from "../../api/hooks";
import { useActivityStream } from "../../api/live";
import type {
  ActivityEvent,
  ActivityWindowCount,
  DiskStatus,
  HealthCondition,
  LiveActivitySnapshot,
  LiveMonitor,
} from "../../api/types";
import { HELP, type HelpId } from "../../help/help";
import { cn } from "../../lib/cn";
import { formatBytes, formatLag, formatNumber, formatRelative } from "../../lib/format";
import { Card, CardHeader } from "../ui/Card";
import { Select } from "../ui/Select";
import { SkeletonText } from "../ui/Skeleton";
import { ErrorState } from "../ui/States";
import { Tip } from "../ui/Tooltip";

/**
 * "Is anything happening?" — the one question the status page could not answer.
 *
 * Every number here is a rate, not a total: what the pipeline did in the last window and
 * what it did in the window before that, so the arrow beside a tile is a fact from the
 * same request rather than something the page has to remember. Colour is reserved for
 * problems — a failed upload, a recorder that has gone quiet, a queue that is growing —
 * because a panel where everything is coloured is a panel where nothing stands out.
 */

/** The windows on offer. A minute is "right now"; an hour is as far as this view reaches. */
const WINDOWS = [
  { value: "60", label: "1 min" },
  { value: "300", label: "5 min" },
  { value: "900", label: "15 min" },
  { value: "3600", label: "1 hour" },
];

/** Green while calls are landing, amber while they might be, red once they are not. */
export type Heartbeat = "live" | "recent" | "quiet" | "silent";

/**
 * A recorder's heartbeat from the seconds since its last call.
 *
 * The thresholds are about traffic, not about the recorder: 60 s of silence on a busy
 * system is nothing, five minutes is worth a glance, and longer than that on a system
 * that was talking means something has stopped. `null` — no call within the endpoint's
 * look-back at all — is its own state rather than the worst one, because "this spare has
 * never been heard from" and "this recorder just died" are different problems.
 */
export function heartbeatTone(secondsSinceLastCall: number | null | undefined): Heartbeat {
  if (secondsSinceLastCall === null || secondsSinceLastCall === undefined) return "silent";
  if (secondsSinceLastCall <= 60) return "live";
  if (secondsSinceLastCall < 300) return "recent";
  return "quiet";
}

const HEARTBEAT_DOT: Record<Heartbeat, string> = {
  live: "bg-good",
  recent: "bg-warning",
  quiet: "bg-critical",
  silent: "bg-muted",
};

const HEARTBEAT_WORDS: Record<Heartbeat, string> = {
  live: "carrying traffic",
  recent: "quiet for a few minutes",
  quiet: "no call in over five minutes",
  silent: "no call in the last day",
};

/** The panel's own anchor for the monitor, replacing whatever fragment was reported. */
function monitorHref(url: string): string {
  return `${url.split("#")[0]}#monitor`;
}

/**
 * The recorder's own live monitor, offered as a link and nothing more.
 *
 * Keyup does not proxy this audio and the link deliberately leaves the app: the panel is
 * the recorder's, it is the thing that knows how to stream from its tuners, and a page
 * that pretended to own that stream would be lying about where the audio comes from the
 * moment it stopped working. The two caveats are said in the row rather than discovered by
 * clicking — a loopback URL opens nothing anywhere but on the recorder itself, and a panel
 * behind a token asks for one before it plays.
 */
export function ListenLive({ monitor }: { monitor: LiveMonitor }) {
  if (!monitor.enabled || !monitor.url) return null;
  const notes = [
    monitor.local_only
      ? "this address is the recorder's own loopback, so it opens only in a browser on that machine"
      : null,
    monitor.token_required ? "the panel asks for the recorder's token before it plays" : null,
  ].filter((note): note is string => note !== null);
  return (
    <Tip label={[HELP["status.live.listen"], ...notes].join(" — ")}>
      <span className="inline-flex items-center gap-1 text-xs">
        <a
          href={monitorHref(monitor.url)}
          target="_blank"
          rel="noreferrer"
          className="rounded text-accent-ink underline decoration-dotted underline-offset-2 hover:decoration-solid"
        >
          Listen live
        </a>
        {monitor.local_only ? <span className="text-muted">(on that machine)</span> : null}
      </span>
    </Tip>
  );
}

/** `up` / `down` / `flat` against the previous window of the same length. */
export function trendOf(count: ActivityWindowCount): "up" | "down" | "flat" {
  if (count.current > count.previous) return "up";
  if (count.current < count.previous) return "down";
  return "flat";
}

function Trend({ count }: { count: ActivityWindowCount }) {
  const direction = trendOf(count);
  const Icon = direction === "up" ? ArrowUpRight : direction === "down" ? ArrowDownRight : ArrowRight;
  const delta = count.current - count.previous;
  const words =
    direction === "flat"
      ? `unchanged from the previous window (${formatNumber(count.previous)})`
      : `${delta > 0 ? "up" : "down"} ${formatNumber(Math.abs(delta))} from the previous window (${formatNumber(count.previous)})`;
  return (
    <span className="inline-flex items-center gap-0.5 text-[11px] text-muted" title={words}>
      <Icon size={11} aria-hidden="true" />
      <span className="tabular-nums">{formatNumber(count.previous)}</span>
      <span className="sr-only">{words}</span>
    </span>
  );
}

/**
 * One counter tile. `tone` is only ever a *problem*: the neutral border is the healthy
 * state, and the reason is always said in words as well as in colour.
 */
function CountTile({
  label,
  count,
  help,
  tone = "neutral",
  toneReason,
}: {
  label: string;
  count: ActivityWindowCount;
  help: HelpId;
  tone?: "neutral" | "warning" | "critical";
  toneReason?: string;
}) {
  return (
    <Tip label={toneReason ? `${HELP[help]} — ${toneReason}` : HELP[help]}>
      <div
        tabIndex={0}
        className={cn(
          // Unframed, like `StatTile`: the status is the 3 px edge, the panel is the frame.
          "border-l-[3px] py-1 pl-2.5",
          tone === "critical"
            ? "border-critical"
            : tone === "warning"
              ? "border-warning"
              : "border-line-strong",
        )}
      >
        <p className="truncate text-xs font-medium text-ink-2">{label}</p>
        <p className="mt-0.5 flex items-baseline gap-1.5">
          <span
            className={cn(
              "text-xl font-semibold tabular-nums leading-none",
              tone === "critical"
                ? "text-critical-ink"
                : tone === "warning"
                  ? "text-warning-ink"
                  : "text-ink",
            )}
          >
            {formatNumber(count.current)}
          </span>
          <Trend count={count} />
        </p>
        {toneReason ? <span className="sr-only">{toneReason}</span> : null}
      </div>
    </Tip>
  );
}

/** A plain depth — a queue is a level, not a rate, so it gets no trend arrow. */
function DepthTile({
  label,
  value,
  help,
  tone = "neutral",
  toneReason,
}: {
  label: string;
  value: number;
  help: HelpId;
  tone?: "neutral" | "warning" | "critical";
  toneReason?: string;
}) {
  return (
    <CountTile
      label={label}
      count={{ current: value, previous: value }}
      help={help}
      tone={tone}
      toneReason={toneReason}
    />
  );
}

const KIND_CHIP: Record<string, string> = {
  ingest: "border-accent text-accent-ink",
  call: "border-accent text-accent-ink",
  export: "border-line text-ink-2",
  audio: "border-line text-ink-2",
  stt: "border-line text-ink-2",
  transcribe: "border-line text-ink-2",
  site: "border-line text-ink-2",
  recorder: "border-line text-ink-2",
};

function TickerRow({ event }: { event: ActivityEvent }) {
  const family = event.kind.split(".")[0] ?? "";
  const tone =
    event.level === "error"
      ? "border-critical text-critical-ink"
      : event.level === "warn"
        ? "border-warning text-warning-ink"
        : (KIND_CHIP[family] ?? "border-line text-ink-2");
  const body = (
    <>
      <span className="w-16 shrink-0 tabular-nums text-[11px] text-muted">{formatRelative(event.at)}</span>
      <span className={cn("shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-medium", tone)}>
        {event.kind}
      </span>
      <span className="truncate text-xs text-ink-2">{event.message}</span>
    </>
  );
  return (
    <li className="border-b border-line/60 last:border-0">
      {event.call_id ? (
        <Link
          to={`/calls/${event.call_id}`}
          className="flex items-center gap-2 px-3 py-1.5 hover:bg-surface-2"
          title="Open the call this line is about"
        >
          {body}
        </Link>
      ) : (
        <div className="flex items-center gap-2 px-3 py-1.5">{body}</div>
      )}
    </li>
  );
}

/** The export buckets a guest never sees: *why* an upload was skipped is operator business. */
const EXPORT_DETAIL_KEYS = [
  ["skipped_duplicate", "dupe"],
  ["skipped_no_speech", "no speech"],
  ["skipped_static", "static"],
  ["skipped_late", "late"],
  ["claimed_by_recorder", "claimed"],
  ["skipped_other", "other"],
] as const;

function ExportRow({
  service,
  counts,
  detail,
}: {
  service: string;
  counts: Record<string, ActivityWindowCount>;
  detail: boolean;
}) {
  const failed = counts.failed?.current ?? 0;
  const pending = counts.pending?.current ?? 0;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line px-3 py-2 first:border-0">
      <span className="w-28 shrink-0 truncate text-xs font-medium capitalize text-ink">{service}</span>
      <Tip label={HELP["status.live.exportUploaded"]}>
        <span tabIndex={0} className="text-xs text-ink-2">
          <span className="font-semibold tabular-nums text-ink">
            {formatNumber(counts.uploaded?.current ?? 0)}
          </span>{" "}
          uploaded
        </span>
      </Tip>
      <Tip label={HELP["status.live.exportFailed"]}>
        <span tabIndex={0} className={cn("text-xs", failed > 0 ? "text-critical-ink" : "text-muted")}>
          <span className="font-semibold tabular-nums">{formatNumber(failed)}</span> failed
        </span>
      </Tip>
      <Tip label={HELP["status.live.exportPending"]}>
        <span tabIndex={0} className="text-xs text-muted">
          <span className="font-semibold tabular-nums">{formatNumber(pending)}</span> pending
        </span>
      </Tip>
      {detail ? (
        <span className="flex flex-wrap items-center gap-x-2 text-[11px] text-muted">
          {EXPORT_DETAIL_KEYS.map(([key, label]) =>
            (counts[key]?.current ?? 0) > 0 ? (
              <Tip key={key} label={HELP["status.live.exportSkipped"]}>
                <span tabIndex={0} className="tabular-nums">
                  {formatNumber(counts[key]?.current ?? 0)} {label}
                </span>
              </Tip>
            ) : null,
          )}
        </span>
      ) : null}
    </div>
  );
}

/** True while this tab is the one the operator is looking at. */
function useTabVisible(): boolean {
  const [visible, setVisible] = useState(() =>
    typeof document === "undefined" ? true : document.visibilityState !== "hidden",
  );
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

export function LiveActivity({
  /** Show *why* uploads were skipped. Operator business; a guest gets the totals. */
  detail = false,
  className,
}: {
  detail?: boolean;
  className?: string;
}) {
  const [windowSeconds, setWindowSeconds] = useState("300");
  const visible = useTabVisible();
  const query = useLiveActivity(Number(windowSeconds), visible);
  const snapshot = query.data;

  // The ticker prefers the SSE stream and falls back to the five-second poll: `recent`
  // rides along on the same snapshot, so a browser (or a proxy) with no working
  // `EventSource` still gets a moving feed, just a little coarser.
  const streaming = snapshot?.recent_visible === true && visible;
  const stream = useActivityStream(streaming);
  const ticker = useMemo(() => {
    const merged = new Map<number, ActivityEvent>();
    for (const event of snapshot?.recent ?? []) merged.set(event.id, event);
    for (const event of stream.events) merged.set(event.id, event);
    return [...merged.values()].sort((a, b) => b.id - a.id).slice(0, 30);
  }, [snapshot?.recent, stream.events]);

  return (
    <Card className={className}>
      <CardHeader
        title="Live activity"
        hint="What the pipeline has done in this window, and what it did in the one before it."
        actions={
          <div className="flex items-center gap-2">
            {snapshot ? <LastCall snapshot={snapshot} /> : null}
            <Select
              value={windowSeconds}
              onValueChange={setWindowSeconds}
              options={WINDOWS}
              tip={HELP["status.live.window"]}
              ariaLabel="Activity window"
              size="sm"
              className="w-28"
            />
          </div>
        }
      />
      <DiskLowBanner />
      <HealthBanner />
      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : !snapshot ? (
        <SkeletonText lines={6} className="p-4" />
      ) : (
        <Body snapshot={snapshot} detail={detail} ticker={ticker} />
      )}
    </Card>
  );
}

const DISK_RANK: Record<DiskStatus["level"], number> = { ok: 0, warn: 1, critical: 2 };

/** The filesystem in the worst shape, or `null` while every one is fine. */
export function worstDisk(volumes: readonly DiskStatus[] | null | undefined): DiskStatus | null {
  let worst: DiskStatus | null = null;
  for (const volume of volumes ?? []) {
    if (volume.level === "ok") continue;
    if (!worst || DISK_RANK[volume.level] > DISK_RANK[worst.level]) worst = volume;
  }
  return worst;
}

/** "28 GB free (11 %) on /srv/keyup/audio — full in about 6 days". */
export function diskSentence(disk: DiskStatus): string {
  const days =
    disk.days_to_full === null || disk.days_to_full === undefined
      ? ""
      : ` — full in about ${disk.days_to_full < 1 ? "less than a day" : `${Math.round(disk.days_to_full)} days`}`;
  return `${formatBytes(disk.free_bytes)} free (${disk.free_percent.toFixed(0)} %) on ${disk.path}${days}`;
}

/**
 * One line, and only when a disk is low: the warning the host did not give on 2026-09-22.
 * Everything else about the disk lives on the Admin page; this is here because it is the
 * panel an operator actually looks at.
 */
export function DiskLowBanner() {
  const { data } = useSystemStatus();
  const disk = worstDisk(data?.disk_volumes);
  if (!disk) return null;
  const critical = disk.level === "critical";
  return (
    <Tip label={HELP["status.diskLow"]}>
      <p
        role="status"
        tabIndex={0}
        className={cn(
          "mx-4 mt-2 flex items-center gap-2 rounded border px-3 py-1.5 text-xs",
          critical
            ? "border-critical bg-critical-soft text-critical-ink"
            : "border-warning bg-warning-soft text-warning-ink",
        )}
      >
        <HardDrive size={13} aria-hidden="true" />
        <span>
          Disk space {critical ? "critical" : "low"}: {diskSentence(disk)}
        </span>
      </p>
    </Tip>
  );
}

/** "Recorder silent (signal): recorder signal has not reported for 12 min …". */
export function healthSentence(condition: HealthCondition): string {
  const subject = condition.key.includes(":") ? ` (${condition.key.split(":").slice(1).join(":")})` : "";
  const what = HEALTH_TITLE[condition.check] ?? condition.check;
  return `${what}${subject}: ${condition.message ?? "raised"}`;
}

const HEALTH_TITLE: Record<HealthCondition["check"], string> = {
  recorder_silent: "Recorder silent",
  ingest_lag: "No new calls",
  stt_backlog: "Transcription stalled",
  backup_stale: "Backup overdue",
};

/**
 * One line per health condition the worker holds raised (app/health.py) — and nothing at
 * all while everything is fine, which is almost always.
 */
export function HealthBanner() {
  const { data } = useSystemStatus();
  const raised = data?.health ?? [];
  if (raised.length === 0) return null;
  return (
    <Tip label={HELP["status.health"]}>
      <ul
        role="status"
        tabIndex={0}
        className="mx-4 mt-2 space-y-0.5 rounded border border-critical bg-critical-soft px-3 py-1.5 text-xs text-critical-ink"
      >
        {raised.map((condition) => (
          <li key={condition.key} className="flex items-center gap-2">
            <HeartPulse size={13} aria-hidden="true" />
            <span>{healthSentence(condition)}</span>
          </li>
        ))}
      </ul>
    </Tip>
  );
}

function LastCall({ snapshot }: { snapshot: LiveActivitySnapshot }) {
  const tone = heartbeatTone(snapshot.seconds_since_last_call);
  return (
    <Tip label={HELP["status.live.lastCall"]}>
      <span tabIndex={0} className="inline-flex items-center gap-1.5 text-xs text-ink-2">
        <span className={cn("size-2 rounded-full", HEARTBEAT_DOT[tone])} aria-hidden="true" />
        {snapshot.seconds_since_last_call === null
          ? "no calls"
          : `last call ${formatLag(snapshot.seconds_since_last_call)} ago`}
        <span className="sr-only">— {HEARTBEAT_WORDS[tone]}</span>
      </span>
    </Tip>
  );
}

function Body({
  snapshot,
  detail,
  ticker,
}: {
  snapshot: LiveActivitySnapshot;
  detail: boolean;
  ticker: ActivityEvent[];
}) {
  const counters = snapshot.counters;
  const queues = snapshot.queues;
  const failedExports = Object.values(snapshot.exports).reduce(
    (total, counts) => total + (counts.failed?.current ?? 0),
    0,
  );
  const services = Object.keys(snapshot.exports).sort();
  // A queue is only news when it is *growing*: a deep queue that is draining is the
  // system working, and colouring it teaches the operator to ignore the colour.
  const backlog = queues.audio_queued + queues.transcription_queued;
  const throughput = counters.audio_processed.current + counters.transcribed.current;
  const queueTone = backlog > 0 && backlog > throughput ? "warning" : "neutral";

  return (
    <div className="space-y-3 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <CountTile label="Ingested" count={counters.ingested} help="status.live.ingested" />
        <CountTile label="Merged dupes" count={counters.duplicates_merged} help="status.live.duplicates" />
        <CountTile label="Speech" count={counters.speech} help="status.live.speech" />
        <CountTile label="Pages" count={counters.pages} help="status.live.pages" />
        <CountTile label="Key-ups" count={counters.no_speech} help="status.live.noSpeech" />
        <CountTile label="Static" count={counters.static} help="status.live.static" />
        <CountTile label="Audio done" count={counters.audio_processed} help="status.live.audioProcessed" />
        <CountTile label="Discarded" count={counters.audio_discarded} help="status.live.audioDiscarded" />
        <CountTile label="Transcribed" count={counters.transcribed} help="status.live.transcribed" />
        <CountTile
          label="Rejected"
          count={counters.transcripts_rejected}
          help="status.live.transcriptsRejected"
          tone={counters.transcripts_rejected.current > 0 ? "warning" : "neutral"}
          toneReason={
            counters.transcripts_rejected.current > 0
              ? "the engine produced text that was thrown away as a hallucination"
              : undefined
          }
        />
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <DepthTile
          label="Audio queue"
          value={queues.audio_queued}
          help="status.live.audioQueue"
          tone={queueTone}
          toneReason={queueTone === "warning" ? "more work queued than finished this window" : undefined}
        />
        <DepthTile
          label="STT queue"
          value={queues.transcription_queued}
          help="status.live.sttQueue"
          tone={queueTone}
          toneReason={queueTone === "warning" ? "more work queued than finished this window" : undefined}
        />
        <DepthTile label="Exports pending" value={queues.exports_pending} help="status.live.exportsPending" />
        <CountTile
          label="Webhooks"
          count={snapshot.webhooks.delivered}
          help="status.live.webhooks"
          tone={snapshot.webhooks.failed.current > 0 ? "critical" : "neutral"}
          toneReason={
            snapshot.webhooks.failed.current > 0
              ? `${snapshot.webhooks.failed.current} delivery(s) failed`
              : undefined
          }
        />
      </div>

      {/* The three lists below are parts of this panel, not panels of their own: a
          hairline and a heading each, no second frame inside the card. */}
      <section aria-label="Recorders" className="-mx-3 border-t border-line">
        <h3 className="px-3 pt-2 pb-1 text-xs font-medium text-ink-2">Recorders</h3>
        {snapshot.recorders.length === 0 ? (
          <p className="px-3 py-2 text-xs text-muted">No recorder has been heard from.</p>
        ) : (
          <ul>
            {snapshot.recorders.map((recorder) => {
              const tone = heartbeatTone(recorder.seconds_since_last_call);
              return (
                <li
                  key={recorder.instance}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line px-3 py-2 first:border-0"
                >
                  <Tip label={`${HELP["status.live.heartbeat"]} — ${HEARTBEAT_WORDS[tone]}`}>
                    <span tabIndex={0} className="inline-flex items-center gap-1.5">
                      <span className={cn("size-2 rounded-full", HEARTBEAT_DOT[tone])} aria-hidden="true" />
                      <span className="text-xs font-medium text-ink">
                        {recorder.label || recorder.instance}
                      </span>
                      <span className="sr-only">{HEARTBEAT_WORDS[tone]}</span>
                    </span>
                  </Tip>
                  <span className="text-xs tabular-nums text-ink-2">
                    {formatNumber(recorder.calls_in_window)} calls
                  </span>
                  <span className="text-xs text-muted">
                    {recorder.seconds_since_last_call === null
                      ? "no call in the last day"
                      : `last call ${formatLag(recorder.seconds_since_last_call)} ago`}
                  </span>
                  {recorder.monitor ? <ListenLive monitor={recorder.monitor} /> : null}
                  {recorder.registered ? null : (
                    <span className="rounded-full border border-warning px-1.5 py-0.5 text-[10px] text-warning-ink">
                      unregistered
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {services.length > 0 ? (
        <section
          aria-label="Exports"
          className={cn("-mx-3 border-t", failedExports > 0 ? "border-critical" : "border-line")}
        >
          <h3
            className={cn(
              "px-3 pt-2 pb-1 text-xs font-medium",
              failedExports > 0 ? "text-critical-ink" : "text-ink-2",
            )}
          >
            Exports
          </h3>
          <div>
            {services.map((service) => (
              <ExportRow
                key={service}
                service={service}
                counts={snapshot.exports[service] ?? {}}
                detail={detail}
              />
            ))}
          </div>
        </section>
      ) : null}

      {snapshot.recent_visible ? (
        <section aria-label="Recent events" className="-mx-3 -mb-3 border-t border-line">
          <h3 className="px-3 pt-2 pb-1 text-xs font-medium text-ink-2">Recent events</h3>
          {ticker.length === 0 ? (
            <p className="px-3 py-2 text-xs text-muted">Nothing yet — events appear as calls arrive.</p>
          ) : (
            <ul className="max-h-56 overflow-y-auto scrollbar-thin">
              {ticker.map((event) => (
                <TickerRow key={event.id} event={event} />
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </div>
  );
}
