import type { EChartsOption } from "echarts";
import { Activity, Download, Headphones, Radio, Signal, TrendingUp } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import {
  useCurrentUser,
  useFeedArchives,
  useFeedSamples,
  useFeedSummary,
  useFetchFeedPublicPage,
  useImportFeedStats,
  usePollFeed,
  useProbeFeed,
  useSaveSettings,
  useSettings,
  useSyncFeedArchives,
} from "../api/hooks";
import type { FeedImportReport, FeedProbeResult, HotHour, SettingDefinition } from "../api/types";
import { WEEKDAYS } from "../api/types";
import { Chart } from "../components/charts/Chart";
import { StatTile } from "../components/charts/StatTile";
import { HourOfWeekHeatmap } from "../components/feedstats/HourOfWeekHeatmap";
import { InfoDot } from "../components/help/InfoDot";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Select } from "../components/ui/Select";
import { EmptyState, ErrorState } from "../components/ui/States";
import { HELP, useHelpText } from "../help/help";
import { barSeries, baseOption, categoryAxis, lineSeries, valueAxis } from "../lib/chart";
import { seriesColor } from "../lib/colors";
import { formatDate, formatDateTime, formatNumber } from "../lib/format";
import { useTheme } from "../state/theme";
import { mutationErrorToast, toast } from "../state/toasts";
import { SettingField } from "./admin/SettingField";

/** The runtime settings this page owns, in the order an operator fills them in. */
const FEED_KEYS = [
  "bcfy_feed_id",
  "bcfy_owner_username",
  "bcfy_owner_password",
  "bcfy_stats_enabled",
  "bcfy_stats_poll_seconds",
  "bcfy_public_page_poll_seconds",
  "bcfy_archives_sync_enabled",
  "bcfy_archives_sync_hours",
  "bcfy_archives_backfill_days_per_sync",
];

/**
 * The window options. The long ones are honest about the *calls* half — this archive
 * goes back to 2024 and a year of hourly buckets is two index range scans — and the
 * listener half fills in as the poller runs, which is why an unmeasured cell is drawn
 * as "no data" rather than as a quiet hour.
 */
const WINDOWS = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "180", label: "180 days" },
  { value: "365", label: "1 year" },
];

/**
 * One ranked hour-of-week list, shared by the hottest and quietest cards so the two
 * read as the two ends of one ranking rather than as two different statistics.
 *
 * `listeners_avg` is `null` for a cell nobody polled, and it is rendered as "no
 * listener data" rather than as 0 — the quietest card would otherwise be a list of
 * hours whose only distinction is that nobody has looked at them.
 */
function HourList({ cells, empty }: { cells: HotHour[]; empty: string }) {
  if (cells.length === 0) return <EmptyState title="No history yet" hint={empty} />;
  return (
    <ul className="divide-y divide-line">
      {cells.map((cell) => (
        <li
          key={`${cell.weekday}-${cell.hour}`}
          className="flex items-center justify-between gap-3 px-4 py-2 text-sm"
        >
          <span className="text-ink">
            {WEEKDAYS[cell.weekday] ?? "?"} {String(cell.hour).padStart(2, "0")}:00
          </span>
          <span className="tabular-nums text-ink-2">
            {cell.listeners_avg === null
              ? "no listener data"
              : `${formatNumber(cell.listeners_avg)} listeners`}{" "}
            · {formatNumber(cell.calls)} calls
          </span>
        </li>
      ))}
    </ul>
  );
}

/** What one archive sync did, in a sentence a toast can hold. */
function syncWords(report: Record<string, unknown>): string {
  const rows = Number(report.rows ?? 0);
  const backfilled = Number(report.backfilled_days ?? 0);
  const remaining = Number(report.backfill_remaining ?? 0);
  const parts = [`${formatNumber(rows)} block${rows === 1 ? "" : "s"}`];
  if (backfilled > 0) parts.push(`${formatNumber(backfilled)} day(s) backfilled`);
  if (remaining > 0) parts.push(`${formatNumber(remaining)} day(s) still to fetch`);
  return `Archives synced: ${parts.join(", ")}`;
}

/** What one dashboard import did, in a sentence a toast can hold. */
function importWords(report: FeedImportReport): string {
  const what = report.format === "hourly" ? "hour" : "day";
  const parts = [
    `${formatNumber(report.rows_written)} of ${formatNumber(report.rows_parsed)} ${what}s stored`,
  ];
  // The gap between parsed and written is not a failure and must not read as one: it is
  // the hours this deployment had already polled, which an import never overwrites.
  if (report.rows_written < report.rows_parsed) parts.push("the rest were already polled here");
  if (report.rows_skipped > 0) {
    const reasons = Object.entries(report.skipped_reasons)
      .map(([reason, count]) => `${formatNumber(count)} ${reason}`)
      .join(", ");
    parts.push(`${formatNumber(report.rows_skipped)} row(s) skipped: ${reasons}`);
  }
  if (report.first) parts.push(`back to ${formatDate(String(report.first))}`);
  return parts.join(" · ");
}

/** "Mon 02:00 - 05:00", the way an operator writes down a maintenance window. */
function windowLabel(weekday: number, startHour: number, endHour: number): string {
  const clock = (hour: number) => `${String(hour).padStart(2, "0")}:00`;
  return `${WEEKDAYS[weekday] ?? "?"} ${clock(startHour)}–${clock(endHour)}`;
}

/** How to read an r, in the four words a person actually wants. */
function correlationWords(r: number | null): string {
  if (r === null) return "not enough history yet";
  const strength = Math.abs(r) < 0.2 ? "little" : Math.abs(r) < 0.5 ? "some" : "strong";
  return `${strength} ${r < 0 ? "inverse " : ""}association`;
}

/**
 * Admin → Feed stats: how the operator's own Broadcastify **feed** is doing.
 *
 * A different product from the Exports tab beside it, which publishes *calls*.
 * This page reads: who is listening, when they listen, whether that follows the
 * radio traffic, and which archived blocks exist.
 *
 * Everything is admin-only on the server, so the queries are disabled for anybody
 * else rather than firing and collecting 403s. With no feed configured the page is
 * the settings form and nothing else, which is the correct first screen.
 */
export function FeedStats() {
  // Read into a title, outside a tooltip: resolved so it is never the loading placeholder.
  const coverageHelp = useHelpText("admin.feedstats.coverage");
  const { isAdmin, canWrite } = useCurrentUser();
  const mode = useTheme();
  const [days, setDays] = useState("30");
  const [step, setStep] = useState<"hour" | "day">("hour");
  const [day, setDay] = useState<string>("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [probed, setProbed] = useState<FeedProbeResult | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const settings = useSettings();
  const save = useSaveSettings();
  const summary = useFeedSummary(Number(days), isAdmin);
  const samples = useFeedSamples({ step }, isAdmin);
  const archives = useFeedArchives(day || undefined, isAdmin);
  const probe = useProbeFeed();
  const poll = usePollFeed();
  const publicPage = useFetchFeedPublicPage();
  const importCsv = useImportFeedStats();
  const syncArchives = useSyncFeedArchives();

  /**
   * Read the chosen CSV in the browser and post its text.
   *
   * The API decides which of the two exports it is from the file's own header, so there
   * is nothing to ask the operator and nothing to get wrong: they pick either file and it
   * lands in the right table. The input is cleared afterwards so re-choosing the same file
   * — the ordinary thing to do after a fresh export — fires `change` again.
   */
  const onFile = async (input: HTMLInputElement) => {
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    const content = await file.text();
    importCsv.mutate(content, {
      onSuccess: (report) => toast(importWords(report), { tone: "success" }),
      onError: (error) => mutationErrorToast(error, "Could not import that file"),
    });
  };

  const byKey = useMemo(() => {
    const map = new Map<string, SettingDefinition>();
    for (const row of settings.data ?? []) map.set(row.key, row);
    return map;
  }, [settings.data]);

  const value = (key: string) => draft[key] ?? byKey.get(key)?.value ?? "";
  const configured = value("bcfy_feed_id").trim() !== "";
  const dirty = Object.keys(draft).length > 0;
  const data = summary.data;

  const commit = () =>
    save.mutate(draft, {
      onSuccess: () => {
        toast("Feed settings saved", { tone: "success" });
        setDraft({});
        void summary.refetch();
      },
      onError: (error) => mutationErrorToast(error, "Could not save the feed settings"),
    });

  // What the two halves of the grid actually cover.  The calls always cover the whole
  // window; the listeners cover the time since the first poll, and on a year-long window
  // that is usually a corner of it — which is a thing to say plainly rather than to leave
  // the reader to infer from a mostly-hatched grid.
  //
  // …and of those hours, some were not polled here at all: some were reconstructed from
  // the public feed page's 24-hour sparkline, and some read out of the dashboard's hourly
  // CSV export. All three are real listener figures and belong in the grid, but only one
  // of them is this deployment's own measurement, so the line names the other two rather
  // than letting them pass as polls.
  //
  // The daily export is a separate history and gets its own clause: it reaches back
  // months where the hourly one reaches back a week, so "hourly history from" and "daily
  // history from" are two different dates and saying only one of them would mislead.
  const basis = data?.quietest_basis ?? "listeners";
  const coverage =
    data && data.window_hours > 0 ? (
      <span className="text-[11px] text-muted" title={coverageHelp}>
        {data.listeners_hours_covered === 0
          ? "no listener samples yet"
          : `hourly history from ${formatDate(data.first_sample_at)} · ${formatNumber(
              data.listeners_hours_covered,
            )} of ${formatNumber(data.window_hours)} hours`}
        {data.hours_from_import > 0 || data.hours_from_page > 0
          ? ` (${[
              data.hours_from_import > 0 ? `${formatNumber(data.hours_from_import)} h imported` : null,
              data.hours_from_page > 0
                ? `${formatNumber(data.hours_from_page)} h from the public page`
                : null,
            ]
              .filter(Boolean)
              .join(", ")})`
          : ""}
        {data.daily_days_covered > 0
          ? `, daily history from ${formatDate(`${String(data.history_from)}T00:00:00Z`)}`
          : ""}
      </span>
    ) : null;

  const points = samples.data ?? [];
  const listenersOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    return {
      ...base,
      legend: { show: false },
      grid: { left: 8, right: 16, top: 10, bottom: 8, containLabel: true },
      xAxis: categoryAxis(
        mode,
        points.map((point) => formatDateTime(point.bucket)),
      ),
      yAxis: valueAxis(mode, "listeners"),
      series: [
        lineSeries(
          "Average",
          seriesColor("feed-listeners", mode),
          points.map((point) => point.listeners_avg),
          mode,
          { area: true },
        ),
        lineSeries(
          "Peak",
          seriesColor("feed-peak", mode),
          points.map((point) => point.listeners_max),
          mode,
          { dashed: true },
        ),
      ],
    } as unknown as EChartsOption;
  }, [points, mode]);

  /**
   * The imported daily history: a bar per day's peak listeners, a line for its uptime.
   *
   * Two axes because the two series share nothing but a date — listeners are unbounded
   * and uptime is a percentage that lives in its top few points — and the uptime axis is
   * pinned to 0–100 rather than fitted, so a week of perfect uptime draws a flat line at
   * the top instead of a noisy one filling the panel.
   *
   * This is the only chart on the page that can cover a long window, because the daily
   * export is the only history that reaches back months.
   */
  const daily = data?.daily ?? [];
  const dailyOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    return {
      ...base,
      legend: { ...(base.legend as object), show: true },
      grid: { left: 8, right: 16, top: 10, bottom: 24, containLabel: true },
      xAxis: categoryAxis(
        mode,
        daily.map((row) => row.day),
      ),
      yAxis: [
        valueAxis(mode, "listeners"),
        { ...valueAxis(mode, "uptime %"), min: 0, max: 100, splitLine: { show: false } },
      ],
      series: [
        barSeries(
          "Peak listeners",
          seriesColor("feed-peak", mode),
          daily.map((row) => row.listeners_max ?? 0),
          mode,
        ),
        {
          ...lineSeries(
            "Uptime %",
            seriesColor("feed-uptime", mode),
            daily.map((row) => row.uptime_pct),
            mode,
          ),
          yAxisIndex: 1,
        },
      ],
    } as unknown as EChartsOption;
  }, [daily, mode]);

  const archiveDays = useMemo(() => {
    const seen = new Set((archives.data ?? []).map((row) => row.day));
    return ["", ...Array.from(seen).sort().reverse()];
  }, [archives.data]);

  if (!isAdmin) {
    return (
      <div>
        <PageHeader
          title="Feed stats"
          description="Broadcastify feed listener statistics and archive listings."
        />
        <div className="px-4 md:px-6">
          <EmptyState
            title="Admin only"
            hint="A feed's listener numbers are the operator's own Broadcastify account data, so this page needs the admin scope."
          />
        </div>
      </div>
    );
  }

  return (
    <div className="pb-8">
      <PageHeader
        title="Feed stats"
        description="How the Broadcastify live feed is doing: who is listening, when they listen, whether that follows the radio traffic, and which archives exist."
        actions={
          <>
            <Select
              value={days}
              onValueChange={setDays}
              options={WINDOWS}
              tip={HELP["admin.feedstats.window"]}
              ariaLabel="Window"
            />
            <Button
              tip={HELP["admin.feedstats.probe"]}
              onClick={() =>
                probe.mutate(undefined, {
                  onSuccess: (result) => setProbed(result),
                  onError: (error) => mutationErrorToast(error, "Could not reach Broadcastify"),
                })
              }
              disabled={!configured || probe.isPending}
            >
              Probe
            </Button>
            <Button
              tip={HELP["admin.feedstats.pollnow"]}
              onClick={() =>
                poll.mutate(undefined, {
                  onSuccess: () => toast("Sample stored", { tone: "success" }),
                  onError: (error) => mutationErrorToast(error, "Could not poll the feed"),
                })
              }
              disabled={!configured || poll.isPending}
            >
              Poll now
            </Button>
            <Button
              tip={HELP["admin.feedstats.pollpage"]}
              onClick={() =>
                publicPage.mutate(undefined, {
                  onSuccess: (result) =>
                    toast(
                      result.bars.length === 0
                        ? "The public page carried no listener sparkline"
                        : `${formatNumber(result.hours_written)} of ${formatNumber(
                            result.bars.length,
                          )} hours filled in from the public page`,
                      { tone: result.bars.length === 0 ? "info" : "success" },
                    ),
                  onError: (error) => mutationErrorToast(error, "Could not fetch the public feed page"),
                })
              }
              disabled={!configured || publicPage.isPending}
            >
              Fetch public page
            </Button>
            {/*
              The import control. A hidden file input driven by a Button, so it matches the
              three buttons beside it rather than being a stray browser widget in a row of
              them — and so the accessible name is the label, which is what a test and a
              screen reader both ask for.
            */}
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv"
              aria-label="Dashboard CSV export"
              className="hidden"
              onChange={(event) => void onFile(event.currentTarget)}
            />
            <Button
              tip={HELP["admin.feedstats.import"]}
              onClick={() => fileInput.current?.click()}
              disabled={!configured || importCsv.isPending}
            >
              Import dashboard export
            </Button>
          </>
        }
      />

      <div className="space-y-4 px-4 md:px-6">
        {/* ------------------------------------------------------------ status */}
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <StatTile
            label="Listening now"
            value={formatNumber(data?.current_listeners)}
            help="admin.feedstats.listeners"
            icon={Headphones}
          />
          <StatTile
            label="Feed"
            value={
              data?.online === null || data?.online === undefined ? "—" : data.online ? "online" : "offline"
            }
            help="admin.feedstats.online"
            icon={Signal}
            tone={data?.online === true ? "good" : data?.online === false ? "critical" : "neutral"}
            toneReason={
              data?.online === true
                ? "the last poll found the feed up"
                : data?.online === false
                  ? "the last poll found the feed down"
                  : "no poll has succeeded yet"
            }
            footnote={data?.last_poll_at ? `last poll ${formatDateTime(data.last_poll_at)}` : "never polled"}
          />
          <StatTile
            label="Peak"
            value={formatNumber(data?.peak_listeners)}
            help="admin.feedstats.peak"
            icon={TrendingUp}
            footnote={data?.peak_at ? formatDateTime(data.peak_at) : undefined}
          />
          <StatTile
            label="Calls in window"
            value={formatNumber(data?.calls_total)}
            help="admin.feedstats.calls"
            icon={Radio}
          />
          <StatTile
            label="Traffic vs listeners"
            value={
              data?.correlation === null || data?.correlation === undefined
                ? "—"
                : data.correlation.toFixed(2)
            }
            help="admin.feedstats.correlation"
            icon={Activity}
            footnote={correlationWords(data?.correlation ?? null)}
          />
        </div>

        {summary.isError ? <ErrorState error={summary.error} /> : null}

        {/* ------------------------------------------------------------- chart */}
        <Chart
          title="Listeners over time"
          tip={HELP["admin.feedstats.chart"]}
          option={listenersOption}
          data={{
            headers: ["bucket", "listeners_avg", "listeners_max", "samples"],
            rows: points.map((point) => [
              point.bucket,
              point.listeners_avg,
              point.listeners_max,
              point.samples,
            ]),
          }}
          filename="feed-listeners"
          isLoading={samples.isLoading}
          error={samples.error}
          isEmpty={points.length === 0}
          actions={
            <Select
              value={step}
              onValueChange={(next) => setStep(next as "hour" | "day")}
              options={[
                { value: "hour", label: "Hourly" },
                { value: "day", label: "Daily" },
              ]}
              tip={HELP["admin.feedstats.step"]}
              ariaLabel="Bucket"
            />
          }
        />

        {/* ------------------------------------------------------ daily history */}
        <Chart
          title="Daily peak listeners and uptime"
          tip={HELP["admin.feedstats.daily"]}
          hint="From the feed dashboard's daily CSV export — the only listener history that reaches back months. Broadcastify → Manage → your feed → Statistics offers two exports: hourly listeners (about a week) and uptime (about 180 days). Import either with the button above; the format is read from the file."
          option={dailyOption}
          data={{
            headers: ["day", "listeners_max", "uptime_pct"],
            rows: daily.map((row) => [row.day, row.listeners_max, row.uptime_pct]),
          }}
          filename="feed-daily"
          isLoading={summary.isLoading}
          error={summary.error}
          isEmpty={daily.length === 0}
        />

        {/* ---------------------------------------------------------- heatmaps */}
        <div className="grid gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader
              title="Listeners by hour of week"
              hint="Average listeners per cell, on the local clock, Monday first. A hatched cell is an hour no poll has fallen in yet — not a quiet one."
              actions={coverage}
            />
            <div className="p-4">
              <HourOfWeekHeatmap
                matrix={data?.listeners_hour_of_week ?? []}
                maxMatrix={data?.listeners_max_hour_of_week ?? undefined}
                samplesMatrix={data?.listeners_samples_hour_of_week ?? undefined}
                label="Average listeners by weekday and hour"
                unit="listeners"
              />
            </div>
          </Card>
          <Card>
            <CardHeader
              title="Call volume by hour of week"
              hint="The same grid for this system's own traffic on the talkgroups the feed carries. This half covers the whole window: the calls are ours."
            />
            <div className="p-4">
              <HourOfWeekHeatmap
                matrix={data?.calls_hour_of_week ?? []}
                label="Average calls by weekday and hour"
                unit="calls"
              />
            </div>
          </Card>
        </div>

        {/* ------------------------------------------- hot hours, quiet hours, windows */}
        <div className="grid gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader
              title="Hottest hours"
              hint="The ten busiest cells of the grid above, ranked by listeners and tie-broken by call volume."
            />
            <HourList
              cells={data?.hottest_hours ?? []}
              empty="Turn the poller on, or press Poll now, and the hot hours appear as samples accumulate."
            />
          </Card>

          <Card>
            <CardHeader
              title="Quietest hours"
              hint={
                basis === "calls"
                  ? "No listener sample exists yet, so these are the ten quietest cells by radio traffic — a guess at when nobody is listening, not a measurement of it."
                  : "The ten least busy cells that have actually been polled. An hour with no samples is not eligible: never having looked is not the same as nobody listening."
              }
              actions={<InfoDot id="admin.feedstats.quietest" />}
            />
            <HourList
              cells={data?.quietest_hours ?? []}
              empty="Nothing measured yet. The quiet hours appear alongside the hot ones."
            />
          </Card>

          <Card>
            <CardHeader
              title="Suggested maintenance windows"
              hint={
                basis === "calls"
                  ? "The quietest runs of two hours or more, by radio traffic — the listener history has not started yet."
                  : "The quietest runs of two hours or more: long enough to restart a feed in, which the single quietest hour is not. A window may wrap midnight."
              }
              actions={<InfoDot id="admin.feedstats.quietwindows" />}
            />
            {(data?.quiet_windows ?? []).length === 0 ? (
              <EmptyState
                title="No window to suggest"
                hint="Two consecutive hours of history are needed before a window means anything."
              />
            ) : (
              <ul className="divide-y divide-line">
                {(data?.quiet_windows ?? []).map((window) => (
                  <li
                    key={`${window.weekday}-${window.start_hour}`}
                    className="flex items-center justify-between gap-3 px-4 py-2 text-sm"
                  >
                    <span className="text-ink">
                      {windowLabel(window.weekday, window.start_hour, window.end_hour)}
                    </span>
                    <span className="tabular-nums text-ink-2">
                      {window.listeners_avg === null
                        ? `${formatNumber(window.calls_avg)} calls/h`
                        : `${formatNumber(window.listeners_avg)} listeners · ${formatNumber(window.calls_avg)} calls/h`}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {/* --------------------------------------------------------- archives */}
        <Card>
          <CardHeader
            title="Archives"
            hint="Blocks Broadcastify is holding for this feed. Download links are time-limited; a stale one is replaced by the next sync."
            actions={
              <>
                <Select
                  value={day}
                  onValueChange={setDay}
                  options={archiveDays.map((value) => ({ value, label: value || "All days" }))}
                  tip={HELP["admin.feedstats.archiveday"]}
                  ariaLabel="Archive day"
                />
                <Button
                  tip={HELP["admin.feedstats.sync"]}
                  onClick={() =>
                    syncArchives.mutate(
                      { full: false },
                      {
                        onSuccess: (report) => toast(syncWords(report), { tone: "success" }),
                        onError: (error) => mutationErrorToast(error, "Could not sync the listings"),
                      },
                    )
                  }
                  disabled={!configured || syncArchives.isPending}
                >
                  Sync now
                </Button>
                <Button
                  tip={HELP["admin.feedstats.backfill"]}
                  onClick={() =>
                    syncArchives.mutate(
                      { full: true },
                      {
                        onSuccess: (report) => toast(syncWords(report), { tone: "success" }),
                        onError: (error) => mutationErrorToast(error, "Could not sync the listings"),
                      },
                    )
                  }
                  disabled={!configured || syncArchives.isPending}
                >
                  Backfill all
                </Button>
              </>
            }
          />
          {(archives.data ?? []).length === 0 ? (
            <EmptyState
              title="No listings stored"
              hint="Turn the archive sync on, or press Sync now for the last week. Backfill all works back through every day Broadcastify still lists."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs text-muted">
                  <tr>
                    <th className="px-4 py-2 font-medium">Day</th>
                    <th className="px-4 py-2 font-medium">From</th>
                    <th className="px-4 py-2 font-medium">To</th>
                    <th className="px-4 py-2 font-medium">Size</th>
                    <th className="px-4 py-2 font-medium">Audio</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {(archives.data ?? []).map((row) => (
                    <tr key={row.start_at}>
                      <td className="px-4 py-2 tabular-nums">{row.day}</td>
                      <td className="px-4 py-2 tabular-nums">{formatDateTime(row.start_at)}</td>
                      <td className="px-4 py-2 tabular-nums">
                        {row.end_at ? formatDateTime(row.end_at) : "—"}
                      </td>
                      <td className="px-4 py-2 tabular-nums text-ink-2">
                        {row.size_bytes ? `${Math.round(row.size_bytes / 1024 / 1024)} MB` : "—"}
                      </td>
                      <td className="px-4 py-2">
                        {row.url ? (
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-accent hover:underline"
                          >
                            <Download size={14} aria-hidden="true" />
                            Download
                          </a>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {/* ---------------------------------------------------------- settings */}
        <Card>
          <CardHeader
            title="Feed credentials and polling"
            hint="The feed-owner API authenticates with your Broadcastify site login, not with the Calls API key on the Exports tab. Both switches ship off."
            actions={
              <Button
                tip={HELP["admin.feedstats.save"]}
                variant="primary"
                disabled={!dirty || !canWrite || save.isPending}
                onClick={commit}
              >
                Save
              </Button>
            }
          />
          <div className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEED_KEYS.map((key) => {
              const setting = byKey.get(key);
              if (!setting) return null;
              return (
                <SettingField
                  key={key}
                  setting={setting}
                  value={value(key)}
                  dirty={key in draft}
                  disabled={!canWrite}
                  onChange={(next) => setDraft((current) => ({ ...current, [key]: next }))}
                />
              );
            })}
          </div>
        </Card>

        {/* ------------------------------------------------------------- probe */}
        {probed ? (
          <Card>
            <CardHeader
              title="Probe result"
              hint="One live request, stored nowhere. The field names Broadcastify actually uses are undocumented — read `unmapped_fields` and see docs/BCFY_STATS.md."
            />
            <div className="space-y-3 p-4 text-xs">
              {probed.ok ? null : <ErrorState error={new Error(probed.error ?? "failed")} />}
              {probed.unmapped_fields.length > 0 ? (
                <p className="text-ink-2">
                  Not recognized by the field mapping:{" "}
                  <span className="font-mono text-ink">{probed.unmapped_fields.join(", ")}</span>
                </p>
              ) : null}
              <pre className="max-h-80 overflow-auto rounded-md bg-surface-2 p-3 font-mono text-[11px] text-ink">
                {JSON.stringify(probed.raw, null, 2)}
              </pre>
            </div>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
