import type { EChartsOption } from "echarts";
import {
  Activity,
  ArrowLeftRight,
  Clock,
  Database,
  Gauge,
  HardDrive,
  ListChecks,
  Radio,
  RadioTower,
  ScrollText,
  TriangleAlert,
} from "lucide-react";
import { useMemo, useState } from "react";
import {
  useActivity,
  useCurrentUser,
  useGaps,
  useRadioStatsList,
  useStatsOverview,
  useTalkgroupStatsList,
  useTranscriptionStats,
} from "../api/hooks";
import { Chart } from "../components/charts/Chart";
import { StatTile } from "../components/charts/StatTile";
import { PageHeader } from "../components/layout/PageHeader";
import { LiveActivity } from "../components/status/LiveActivity";
import { Card, CardHeader } from "../components/ui/Card";
import { Chip } from "../components/ui/Chip";
import { SkeletonRows } from "../components/ui/Skeleton";
import { EmptyState } from "../components/ui/States";
import { HELP } from "../help/help";
import { barSeries, baseOption, categoryAxis, chartInk, pieSeries, valueAxis } from "../lib/chart";
import { QUALITY_COLORS, sequentialRamp, seriesColor } from "../lib/colors";
import {
  formatBytes,
  formatDateTime,
  formatLag,
  formatNumber,
  formatPercent,
  radioLabel,
  talkgroupLabel,
} from "../lib/format";
import { useTheme } from "../state/theme";

/** Volume, patterns, transcription health and recorder uptime. */
export function Stats() {
  const mode = useTheme();
  const ink = chartInk(mode);
  const [groupBy, setGroupBy] = useState<"day" | "talkgroup">("talkgroup");

  const overview = useStatsOverview();
  const activity = useActivity(groupBy);
  const talkgroups = useTalkgroupStatsList();
  const radios = useRadioStatsList();
  // Two of the cards are about how the recorder itself is running — which backend
  // transcribed what, and where the archive has holes. That is operator business, not
  // part of what the page shows a guest, so for them the cards go and the two requests
  // behind them are never made.
  const { canWrite } = useCurrentUser();
  // The transcription response also carries the quality slices every visitor sees, so
  // it is always fetched; only the per-backend card below is operator business.
  const transcription = useTranscriptionStats(true);
  const gaps = useGaps(canWrite);

  const data = overview.data;
  const matrix = activity.data;

  // Three facts about audio we did not keep, joined into the one line under "Disk free".
  // `no_speech_24h` counts calls nobody spoke on and `static_24h` calls that were noise
  // rather than a voice; with `audio_discard_no_speech` on both become discards a day
  // later, so the numbers are related but never the same number and are worth reading side
  // by side.  Static is called out on its own because a rising static count is a radio or
  // an RF fault, and a rising key-up count is only quiet traffic.
  const discardFootnote = useMemo(() => {
    const plural = (n: number, word: string) => `${formatNumber(n)} ${word}${n === 1 ? "" : "s"}`;
    const parts: string[] = [];
    const discarded = data?.audio_discarded_24h ?? 0;
    const noSpeech = data?.no_speech_24h ?? 0;
    if (discarded > 0) parts.push(`${plural(discarded, "call")} discarded in 24 h`);
    const staticCalls = data?.static_24h ?? 0;
    if (noSpeech > 0) parts.push(`${plural(noSpeech, "call")} with no speech`);
    if (staticCalls > 0) parts.push(`${plural(staticCalls, "call")} of static`);
    return parts.length > 0 ? parts.join(", ") : undefined;
  }, [data?.audio_discarded_24h, data?.no_speech_24h, data?.static_24h]);

  const heatmapOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    const rows = matrix?.rows ?? [];
    const buckets = matrix?.buckets ?? [];
    const points: [number, number, number][] = [];
    (matrix?.matrix ?? []).forEach((row, rowIndex) => {
      row.forEach((value, bucketIndex) => {
        points.push([bucketIndex, rowIndex, value]);
      });
    });
    return {
      ...base,
      tooltip: {
        ...(base.tooltip as object),
        trigger: "item",
        formatter: (params: { value: [number, number, number] }) =>
          `${rows[params.value[1]] ?? ""} · ${buckets[params.value[0]] ?? ""}:00 — ${params.value[2]} calls`,
      },
      legend: { show: false },
      grid: { left: 8, right: 12, top: 10, bottom: 54, containLabel: true },
      xAxis: { ...categoryAxis(mode, buckets), splitArea: { show: false } },
      yAxis: { ...categoryAxis(mode, rows), splitArea: { show: false } },
      visualMap: {
        min: 0,
        max: Math.max(1, matrix?.max ?? 1),
        orient: "horizontal",
        left: "center",
        bottom: 0,
        itemWidth: 12,
        itemHeight: 90,
        text: ["More", "Fewer"],
        textStyle: { color: ink.secondary, fontSize: 11 },
        inRange: { color: sequentialRamp(6, mode) },
      },
      series: [
        {
          type: "heatmap",
          data: points,
          // A 2px surface gap between cells, so adjacent magnitudes never bleed.
          itemStyle: { borderColor: ink.surface, borderWidth: 2 },
          emphasis: { itemStyle: { borderColor: ink.primary, borderWidth: 2 } },
        },
      ],
    } as unknown as EChartsOption;
  }, [matrix, mode, ink]);

  const topTalkgroups = (talkgroups.data ?? []).slice(0, 12);
  const talkgroupOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    const ordered = [...topTalkgroups].reverse();
    return {
      ...base,
      legend: { show: false },
      grid: { left: 8, right: 32, top: 10, bottom: 8, containLabel: true },
      xAxis: valueAxis(mode),
      yAxis: categoryAxis(
        mode,
        ordered.map((stat) => talkgroupLabel(stat)),
      ),
      series: [
        barSeries(
          "Calls",
          seriesColor("talkgroup-volume", mode),
          ordered.map((stat) => stat.call_count),
          mode,
          { horizontal: true },
        ),
      ],
    } as unknown as EChartsOption;
  }, [topTalkgroups, mode]);

  const quality = transcription.data?.quality ?? [];
  const qualityOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), trigger: "item" },
      legend: { ...(base.legend as object), show: true },
      series: [
        pieSeries(
          "Transcript quality",
          quality.map((slice) => ({
            name: slice.quality,
            value: slice.count,
            itemStyle: { color: QUALITY_COLORS[slice.quality] ?? "#898781" },
          })),
          mode,
        ),
      ],
    } as unknown as EChartsOption;
  }, [quality, mode]);

  const topRadios = (radios.data ?? []).slice(0, 12);
  const radioOption = useMemo<EChartsOption>(() => {
    const base = baseOption(mode);
    const ordered = [...topRadios].reverse();
    return {
      ...base,
      legend: { show: false },
      grid: { left: 8, right: 32, top: 10, bottom: 8, containLabel: true },
      xAxis: valueAxis(mode),
      yAxis: categoryAxis(
        mode,
        ordered.map((stat) => radioLabel({ ...stat, radio_id: stat.radio_id_raw })),
      ),
      series: [
        barSeries(
          "Transmissions",
          seriesColor("radio-volume", mode),
          ordered.map((stat) => stat.call_count),
          mode,
          { horizontal: true },
        ),
      ],
    } as unknown as EChartsOption;
  }, [topRadios, mode]);

  // Until the overview answers, no tile has a status: a red "0%" coverage or a green "—"
  // speed while loading reads as a verdict about numbers nobody has seen yet.
  const pending = data === undefined;
  const coverage = data?.transcript_coverage ?? 0;
  const lag = data?.ingest_lag_s ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Statistics"
        description="How much traffic there is, when it happens, how well it is being transcribed, and whether the recorder has been up."
      />

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        {/* The same panel the Admin page leads with. Everything on this page below it is
            a day or a week; this is the last few minutes, and for an operator it belongs at
            the top for the same reason it does there. A guest came for the traffic, not the
            pipeline's counters (sixteen tiles, mostly zero), so for them it goes to the end,
            and they see the counts but not the reasons an upload was skipped. */}
        {canWrite ? <LiveActivity detail /> : null}
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
          <StatTile
            label="Calls today"
            value={formatNumber(data?.calls_today)}
            help="stats.tile.callsToday"
            icon={Radio}
            footnote={`${formatNumber(data?.calls_total)} in the archive`}
          />
          <StatTile
            label="Audio recorded"
            value={(data?.audio_hours_today ?? 0).toFixed(1)}
            unit="h"
            help="stats.tile.audioHours"
            icon={Clock}
            footnote={`${(data?.audio_hours_total ?? 0).toFixed(1)} h in total`}
          />
          <StatTile
            label="Transcript coverage"
            pending={pending}
            value={formatPercent(coverage, 0)}
            help="stats.tile.coverage"
            icon={ScrollText}
            tone={coverage > 0.95 ? "good" : coverage > 0.8 ? "warning" : "critical"}
            toneReason={
              coverage > 0.95
                ? "Nearly everything eligible has a transcript"
                : coverage > 0.8
                  ? "A noticeable share of calls is still untranscribed"
                  : "Most calls have no transcript — check the backends"
            }
          />
          <StatTile
            label="STT speed"
            pending={pending}
            value={data?.stt_rtf === null || data?.stt_rtf === undefined ? "—" : data.stt_rtf.toFixed(2)}
            unit="RTF"
            help="stats.tile.rtf"
            icon={Gauge}
            tone={data?.stt_rtf == null ? "neutral" : data.stt_rtf < 1 ? "good" : "warning"}
            toneReason={
              data?.stt_rtf == null
                ? undefined
                : data.stt_rtf < 1
                  ? "Transcription is keeping up with the recorder"
                  : "Transcription is slower than real time and the queue will grow"
            }
          />
          <StatTile
            label="Queue depth"
            pending={pending}
            value={formatNumber(data?.queue_depth)}
            help="stats.tile.queue"
            icon={ListChecks}
            tone={(data?.queue_depth ?? 0) > 200 ? "warning" : "neutral"}
            toneReason={(data?.queue_depth ?? 0) > 200 ? "The backlog is unusually deep" : undefined}
          />
          <StatTile
            label="Ingest lag"
            pending={pending}
            value={formatLag(lag)}
            help="stats.tile.lag"
            icon={Activity}
            tone={lag === null ? "neutral" : lag > 3600 ? "critical" : lag > 600 ? "warning" : "good"}
            toneReason={
              lag === null
                ? undefined
                : lag > 3600
                  ? "Nothing has been recorded for over an hour"
                  : lag > 600
                    ? "The newest call is more than ten minutes old"
                    : "The archive is up to date"
            }
          />
          {canWrite ? (
            <StatTile
              label="Recorder gaps"
              pending={pending}
              value={formatNumber(data?.recorder_gaps_24h)}
              help="stats.tile.gaps"
              icon={TriangleAlert}
              tone={(data?.recorder_gaps_24h ?? 0) > 0 ? "warning" : "good"}
              toneReason={
                (data?.recorder_gaps_24h ?? 0) > 0
                  ? "There were quiet stretches that look like an outage"
                  : "No suspicious silences in the last day"
              }
            />
          ) : null}
          <StatTile
            label="Talkgroups active"
            value={formatNumber(data?.talkgroups_active_24h)}
            help="stats.tile.talkgroups"
            icon={Database}
          />
          <StatTile
            label="Radios active"
            value={formatNumber(data?.radios_active_24h)}
            help="stats.tile.radios"
            icon={RadioTower}
          />
          {/* A subset of the day's calls, not an addition to it. Its own tile because it is
              the traffic an operator most wants to know is being recorded, and inside the
              total it is a rounding error nobody would see appear or stop. */}
          <StatTile
            label="Unit calls"
            value={formatNumber(data?.unit_calls_24h)}
            help="stats.tile.unitCalls"
            icon={ArrowLeftRight}
          />
          {/* The audio we chose not to keep, reported against the space it would have
              taken: it belongs beside the disk number and nowhere else. Two reasons, and
              both are shown when both apply — the Record toggle said not to keep it, and
              the audio pass found nobody had said anything (a mic key-up). Silent when
              neither has happened, which is most deployments. */}
          <StatTile
            label="Disk free"
            value={formatBytes(data?.disk_free_bytes)}
            help="stats.tile.disk"
            icon={HardDrive}
            footnote={discardFootnote}
          />
        </div>

        <Chart
          title="Activity heatmap"
          tip={HELP["stats.heatmap"]}
          hint="Darker means more calls, in both themes"
          option={heatmapOption}
          height={groupBy === "talkgroup" ? 460 : 220}
          isLoading={activity.isLoading}
          error={activity.error}
          isEmpty={(matrix?.rows.length ?? 0) === 0}
          filename={`activity-by-${groupBy}`}
          data={{
            headers: ["Row", ...(matrix?.buckets ?? [])],
            rows: (matrix?.rows ?? []).map((row, index) => [
              row,
              ...((matrix?.matrix[index] ?? []) as number[]),
            ]),
          }}
          actions={
            <>
              <Chip
                label="By channel"
                tip={HELP["stats.heatmap.groupBy"]}
                active={groupBy === "talkgroup"}
                onClick={() => setGroupBy("talkgroup")}
              />
              <Chip
                label="By day"
                tip={HELP["stats.heatmap.groupBy"]}
                active={groupBy === "day"}
                onClick={() => setGroupBy("day")}
              />
            </>
          }
        />

        <div className="grid gap-4 lg:grid-cols-2">
          <Chart
            title="Busiest talkgroups"
            tip={HELP["stats.byTalkgroup"]}
            option={talkgroupOption}
            height={320}
            isLoading={talkgroups.isLoading}
            isEmpty={topTalkgroups.length === 0}
            filename="calls-per-talkgroup"
            data={{
              headers: ["Talkgroup", "Calls", "Audio (s)"],
              rows: topTalkgroups.map((stat) => [stat.alias, stat.call_count, stat.audio_seconds]),
            }}
          />
          <Chart
            title="Transcript quality"
            tip={HELP["stats.quality"]}
            option={qualityOption}
            height={320}
            isLoading={transcription.isLoading}
            isEmpty={quality.length === 0}
            filename="transcript-quality"
            data={{
              headers: ["Quality", "Calls"],
              rows: quality.map((slice) => [slice.quality, slice.count]),
            }}
          />
          <Chart
            title="Busiest radios"
            tip={HELP["stats.topRadios"]}
            option={radioOption}
            height={320}
            isLoading={radios.isLoading}
            isEmpty={topRadios.length === 0}
            filename="top-radios"
            data={{
              headers: ["Radio", "Transmissions", "Audio (s)"],
              rows: topRadios.map((stat) => [
                radioLabel({ ...stat, radio_id: stat.radio_id_raw }),
                stat.call_count,
                stat.audio_seconds,
              ]),
            }}
          />

          {canWrite ? (
            <Card>
              <CardHeader title="Speech-to-text backends" hint={HELP["stats.backends"]} />
              {transcription.isLoading ? (
                <SkeletonRows rows={4} label="Loading the backends" />
              ) : (
                <table className="w-full text-left text-xs">
                  <caption className="sr-only">
                    Each backend's share of the last day's work and how fast it ran
                  </caption>
                  <thead>
                    <tr className="text-xs text-ink-2">
                      <th scope="col" className="px-3 py-1.5">
                        Backend
                      </th>
                      <th scope="col" className="px-3 py-1.5">
                        Engine
                      </th>
                      <th scope="col" className="px-3 py-1.5 text-right">
                        Calls
                      </th>
                      <th scope="col" className="px-3 py-1.5 text-right">
                        RTF
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(transcription.data?.backends ?? []).map((backend) => (
                      <tr key={backend.backend_name} className="border-t border-line">
                        <td className="px-3 py-1.5 text-ink">{backend.backend_name}</td>
                        <td className="px-3 py-1.5 text-ink-2">{backend.engine}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">
                          {formatNumber(backend.calls)}
                        </td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">
                          {backend.rtf?.toFixed(2) ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          ) : null}
        </div>

        {canWrite ? (
          <Card>
            <CardHeader title="Recorder gaps" hint={HELP["stats.gaps"]} />
            {gaps.isLoading ? (
              <SkeletonRows rows={5} label="Loading recorder gaps" />
            ) : (gaps.data ?? []).length === 0 ? (
              <EmptyState
                title="No gaps detected"
                hint="A gap is a stretch with no traffic at all on a system that normally has some."
              />
            ) : (
              <table className="w-full text-left text-xs">
                <caption className="sr-only">Periods with no recorded traffic</caption>
                <thead>
                  <tr className="text-xs text-ink-2">
                    <th scope="col" className="px-3 py-1.5">
                      System
                    </th>
                    <th scope="col" className="px-3 py-1.5">
                      From
                    </th>
                    <th scope="col" className="px-3 py-1.5">
                      To
                    </th>
                    <th scope="col" className="px-3 py-1.5 text-right">
                      Minutes
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {(gaps.data ?? []).map((gap) => (
                    <tr key={gap.id} className="border-t border-line">
                      <td className="px-3 py-1.5 text-ink">{gap.system_name}</td>
                      <td className="px-3 py-1.5 tabular-nums text-ink-2">
                        {formatDateTime(gap.started_at)}
                      </td>
                      <td className="px-3 py-1.5 tabular-nums text-ink-2">{formatDateTime(gap.ended_at)}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{gap.minutes}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        ) : null}

        {canWrite ? null : <LiveActivity detail={false} />}
      </div>
    </div>
  );
}
