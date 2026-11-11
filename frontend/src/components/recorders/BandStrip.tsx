import { useMemo } from "react";
import type { OccupancyChannel, SiteOccupancy } from "../../api/types";
import { bandSpan, formatBands, mhz, sameBands } from "../../lib/bands";
import { cn } from "../../lib/cn";
import { formatFrequency } from "../../lib/format";

/** A channel is "active now" if it was seen within this many ms. */
export const ACTIVE_MS = 30_000;
/** …and "recent" within this many ms; older than that it is drawn faint. */
export const RECENT_MS = 5 * 60_000;

type Recency = "active" | "recent" | "stale" | "idle";

function recencyOf(lastSeen: string | null, nowMs: number): Recency {
  if (!lastSeen) return "idle";
  const age = nowMs - new Date(lastSeen).getTime();
  if (age <= ACTIVE_MS) return "active";
  if (age <= RECENT_MS) return "recent";
  return "stale";
}

// Recency drives the hue; role drives the tick's height so control/voice/data read apart.
const RECENCY_FILL: Record<Recency, string> = {
  active: "fill-good-ink",
  recent: "fill-warning",
  stale: "fill-[var(--line-strong)]",
  idle: "fill-[var(--line-strong)]",
};
const ROLE_HEIGHT: Record<string, number> = { control: 1, voice: 0.72, data: 0.5 };

/** A gap wider than this between neighboring channels starts a new cluster. */
export const CLUSTER_GAP_HZ = 5_000_000;
/** Clusters are shown until they cover this much of the site's activity. */
const CLUSTER_COVERAGE = 0.9;
/** Padding around each drawn segment: this fraction of its span… */
const SEGMENT_PAD_FRACTION = 0.02;
/** …but never less than this. */
const SEGMENT_PAD_MIN_HZ = 12_500;

interface Cluster {
  lo: number;
  hi: number;
  weight: number;
  channels: OccupancyChannel[];
}

/**
 * Group channels into contiguous clusters, splitting wherever neighbours are more than
 * `CLUSTER_GAP_HZ` apart. A 700+800 site yields two clusters; a lone corrupt frequency
 * announced by a control channel yields a cluster of its own, which is the point.
 */
export function clusterChannels(channels: readonly OccupancyChannel[]): Cluster[] {
  const sorted = [...channels].sort((a, b) => a.frequency_hz - b.frequency_hz);
  // Activity if the fold carried any, otherwise every channel counts once: a site whose
  // observation has no call counts must still be scaled by where its channels *are*.
  const activity = (c: OccupancyChannel) => (c.calls ?? 0) + (c.grants ?? 0);
  const weighByActivity = sorted.some((c) => activity(c) > 0);
  const weightOf = (c: OccupancyChannel) => (weighByActivity ? activity(c) : 1);

  const clusters: Cluster[] = [];
  for (const channel of sorted) {
    const last = clusters[clusters.length - 1];
    if (last && channel.frequency_hz - last.hi <= CLUSTER_GAP_HZ) {
      last.hi = channel.frequency_hz;
      last.weight += weightOf(channel);
      last.channels.push(channel);
    } else {
      clusters.push({
        lo: channel.frequency_hz,
        hi: channel.frequency_hz,
        weight: weightOf(channel),
        channels: [channel],
      });
    }
  }
  return clusters;
}

/**
 * The clusters worth drawing: the heaviest ones, taken until they hold `CLUSTER_COVERAGE`
 * of the site's activity. On the usual single-band site that is one cluster and a stray
 * frequency is left out; on a real 700+800 site both bands are needed to clear the bar and
 * both are drawn. Returned in frequency order, with whatever was left over beside them.
 */
function chooseClusters(clusters: Cluster[]): { shown: Cluster[]; outside: OccupancyChannel[] } {
  const total = clusters.reduce((sum, c) => sum + c.weight, 0);
  const byWeight = [...clusters].sort((a, b) => b.weight - a.weight || a.lo - b.lo);
  const shown: Cluster[] = [];
  let covered = 0;
  for (const cluster of byWeight) {
    if (shown.length > 0 && covered >= total * CLUSTER_COVERAGE) break;
    shown.push(cluster);
    covered += cluster.weight;
  }
  const outside = byWeight.filter((c) => !shown.includes(c)).flatMap((c) => c.channels);
  return {
    shown: shown.sort((a, b) => a.lo - b.lo),
    outside: outside.sort((a, b) => a.frequency_hz - b.frequency_hz),
  };
}

interface Segment {
  /** Padded frequency bounds, which are what the segment's ends mean. */
  lo: number;
  hi: number;
  /** Position within the viewBox. */
  x: number;
  width: number;
  channels: OccupancyChannel[];
}

/** Total viewBox width, and the blank left between two segments as the visible break. */
const W = 1000;
const SEGMENT_GAP = 26;

/** Lay the shown clusters across the viewBox, proportional to span but never hair-thin. */
function layOut(shown: Cluster[]): Segment[] {
  const padded = shown.map((cluster) => {
    const pad = Math.max((cluster.hi - cluster.lo) * SEGMENT_PAD_FRACTION, SEGMENT_PAD_MIN_HZ);
    return { ...cluster, lo: cluster.lo - pad, hi: cluster.hi + pad };
  });
  const available = W - SEGMENT_GAP * (padded.length - 1);
  const spans = padded.map((c) => c.hi - c.lo);
  const totalSpan = spans.reduce((sum, s) => sum + s, 0) || 1;
  // A narrow band beside a wide one still has to be readable, so give every segment a floor
  // and take the difference out of the ones that are over it.
  const floor = Math.min(available / padded.length, available * 0.15);
  const raw = spans.map((s) => (s / totalSpan) * available);
  const deficit = raw.reduce((sum, w) => sum + Math.max(0, floor - w), 0);
  const surplus = raw.reduce((sum, w) => sum + Math.max(0, w - floor), 0) || 1;
  const widths = raw.map((w) => (w < floor ? floor : w - ((w - floor) / surplus) * deficit));

  let x = 0;
  return padded.map((cluster, i) => {
    const width = widths[i] ?? 0;
    const segment: Segment = { lo: cluster.lo, hi: cluster.hi, x, width, channels: cluster.channels };
    x += width + SEGMENT_GAP;
    return segment;
  });
}

/**
 * A per-site band strip: one tick per known channel, positioned by frequency within the
 * band and colored by how recently it was active. SVG rather than canvas — there are at
 * most a few hundred ticks (as with `Waveform`), and SVG stays crisp and needs no resize
 * observer. Read-only: a fold over the observation data keyup already stores.
 *
 * The scale comes from the *channels*, never from the band plan's `base_hz`: a P25 IDEN
 * base is a numbering origin, not a frequency the site transmits on, and a 769 MHz base
 * under an 851–860 MHz site squeezed every real channel into a sliver. Channels are
 * clustered instead (see `clusterChannels`), the clusters holding the site's activity are
 * drawn as separate segments, and anything outside them — a corrupt frequency announced
 * once by the control channel — is pinned at the nearest edge as a hollow marker rather
 * than either stretching the scale or vanishing.
 */
export function BandStrip({
  occupancy,
  height = 44,
  className,
}: {
  occupancy: SiteOccupancy;
  height?: number;
  className?: string;
}) {
  const nowMs = Date.now();
  const { channels } = occupancy;

  const { segments, outside } = useMemo(() => {
    if (channels.length === 0) return { segments: [] as Segment[], outside: [] as OccupancyChannel[] };
    const { shown, outside: left } = chooseClusters(clusterChannels(channels));
    return { segments: layOut(shown), outside: left };
  }, [channels]);

  if (channels.length === 0 || segments.length === 0) {
    return <p className={cn("text-xs text-ink-2", className)}>No channels observed for this site yet.</p>;
  }

  const xOf = (segment: Segment, hz: number) =>
    segment.x + ((hz - segment.lo) / (segment.hi - segment.lo || 1)) * segment.width;

  // Every outlier is pinned to the end of the segment nearest it, and the markers that land
  // on the same end share one tooltip so a handful of junk frequencies stay one small mark.
  const pins = new Map<string, { x: number; channels: OccupancyChannel[] }>();
  for (const channel of outside) {
    let best: Segment | undefined;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (const segment of segments) {
      const distance = Math.max(segment.lo - channel.frequency_hz, channel.frequency_hz - segment.hi, 0);
      if (distance < bestDistance) {
        best = segment;
        bestDistance = distance;
      }
    }
    if (!best) continue;
    const atStart = channel.frequency_hz < best.lo;
    const x = atStart ? best.x + 3 : best.x + best.width - 3;
    const key = `${best.x}-${atStart ? "lo" : "hi"}`;
    const pin = pins.get(key);
    if (pin) pin.channels.push(channel);
    else pins.set(key, { x, channels: [channel] });
  }

  return (
    <div className={cn("w-full", className)}>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        height={height}
        className="w-full"
        role="img"
        aria-label={`Band occupancy for ${occupancy.site_name ?? "site"}: ${channels.length} channels`}
      >
        {segments.map((segment, i) => (
          <g key={`segment-${segment.lo}`} data-testid="band-strip-segment">
            {/* Baseline, drawn per segment so the gap between two bands reads as a break. */}
            <line
              x1={segment.x}
              x2={segment.x + segment.width}
              y1={height - 1}
              y2={height - 1}
              className="stroke-line"
              strokeWidth={1}
            />
            {i > 0 ? (
              <g className="stroke-line" strokeWidth={1}>
                <line x1={segment.x - 15} x2={segment.x - 9} y1={height - 5} y2={height + 2} />
                <line x1={segment.x - 11} x2={segment.x - 5} y1={height - 5} y2={height + 2} />
              </g>
            ) : null}
            {segment.channels.map((channel) => {
              const recency = recencyOf(channel.last_seen, nowMs);
              const barHeight = (height - 3) * (ROLE_HEIGHT[channel.role] ?? 0.6);
              const x = xOf(segment, channel.frequency_hz);
              const label = `${formatFrequency(channel.frequency_hz)} · ${channel.role}${
                channel.channel ? ` · ${channel.channel}` : ""
              } · ${channel.grants} grants${
                recency === "active" ? " · active now" : channel.last_seen ? "" : " · idle"
              }`;
              return (
                <rect
                  key={channel.frequency_hz}
                  x={Math.max(segment.x, Math.min(segment.x + segment.width - 4, x - 2))}
                  width={4}
                  y={height - 2 - barHeight}
                  height={barHeight}
                  rx={1.5}
                  className={cn(
                    RECENCY_FILL[recency],
                    recency === "stale" || recency === "idle" ? "opacity-60" : "",
                  )}
                  // A ring for the control channel so it is unmistakable even when idle.
                  stroke={channel.role === "control" ? "var(--accent)" : "none"}
                  strokeWidth={channel.role === "control" ? 1 : 0}
                >
                  <title>{label}</title>
                </rect>
              );
            })}
          </g>
        ))}
        {[...pins.values()].map((pin) => (
          <rect
            key={`outside-${pin.x}`}
            data-testid="band-strip-outside"
            x={pin.x - 3}
            width={6}
            y={height - 9}
            height={7}
            rx={1.5}
            fill="none"
            className="stroke-[var(--line-strong)]"
            strokeWidth={1}
          >
            <title>{`outside band: ${pin.channels.map((c) => formatFrequency(c.frequency_hz)).join(", ")}`}</title>
          </rect>
        ))}
      </svg>
      <div className="relative mt-1 h-3 text-[10px] tabular-nums text-ink-2">
        {segments.map((segment) => (
          <span
            key={`labels-${segment.lo}`}
            data-testid="band-strip-labels"
            className="absolute flex justify-between"
            style={{ left: `${(segment.x / W) * 100}%`, width: `${(segment.width / W) * 100}%` }}
          >
            <span>{formatFrequency(segment.lo)}</span>
            <span>{formatFrequency(segment.hi)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** A small legend explaining the strip's colors, reusable next to any `BandStrip`. */
export function BandStripLegend({ className }: { className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-2", className)}>
      <span className="inline-flex items-center gap-1">
        <span className="inline-block h-2 w-2 rounded-sm bg-good-ink" /> active
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="inline-block h-2 w-2 rounded-sm bg-warning" /> recent
      </span>
      <span className="inline-flex items-center gap-1">
        <span className="inline-block h-2 w-2 rounded-sm bg-[var(--line-strong)]" /> quiet
      </span>
      <span className="text-muted">·</span>
      <span>tall = control, mid = voice, short = data</span>
    </div>
  );
}

/**
 * One tuner's capture span with its band restrictions drawn over it.
 *
 * The strip above is the *site's* band plan — one tick per known channel, folded from the
 * observations — and a tuner's capture span is a different axis entirely, so this is a
 * second small strip rather than an overlay on that one: reusing it would have meant
 * plotting two unrelated ranges on one scale and calling the result a band strip.
 *
 * The scale is the tuner's own span (`center ± span/2`). Applied bands (what the recorder
 * says it is restricted to) are shaded blocks; a desired list that differs is outlined on
 * top of them, which is the same "pending" fact the badge states, drawn where you can see
 * *what* is pending. A tuner with no bands at all records its whole span, and gets one
 * shaded block across the lot rather than an empty rail.
 */
export function TunerBandStrip({
  centerHz,
  spanHz,
  applied,
  desired,
  className,
}: {
  centerHz: number;
  spanHz: number;
  /** The bands the recorder reports it has applied; empty means unrestricted. */
  applied: readonly string[];
  /** The bands keyup wants, when they differ from `applied`; otherwise null. */
  desired?: readonly string[] | null;
  className?: string;
}) {
  const lo = centerHz - spanHz / 2;
  const hi = centerHz + spanHz / 2;
  const W = 200;
  const H = 10;
  // Clamp each band to the span and drop the ones that miss it entirely: a band outside the
  // capture is exactly the case trunkd refuses, and drawing it off-strip would say nothing.
  const blocks = (bands: readonly string[]): { x: number; width: number; key: string }[] => {
    const ranges: [number, number][] =
      bands.length === 0
        ? [[lo, hi]]
        : bands
            .map((band) => bandSpan(band))
            .filter((span): span is [number, number] => span !== null)
            .map(([bandLo, bandHi]): [number, number] => [Math.max(lo, bandLo), Math.min(hi, bandHi)])
            .filter(([blockLo, blockHi]) => blockHi > blockLo);
    return ranges.map(([blockLo, blockHi]) => ({
      key: `${blockLo}-${blockHi}`,
      x: ((blockLo - lo) / (hi - lo)) * W,
      width: Math.max(1, ((blockHi - blockLo) / (hi - lo)) * W),
    }));
  };

  if (spanHz <= 0) return null;
  const pending = desired != null && !sameBands([...desired], [...applied]);
  const label = `Capture ${mhz(lo)}–${mhz(hi)} MHz · recording ${formatBands(applied)}${
    pending ? ` · pending ${formatBands([...(desired ?? [])])}` : ""
  }`;

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className={cn("block h-2.5 w-full max-w-[12rem]", className)}
      role="img"
      aria-label={label}
      data-testid="tuner-band-strip"
    >
      <title>{label}</title>
      {/* The capture span itself, as the rail everything else is drawn inside. */}
      <rect x={0} y={0} width={W} height={H} rx={2} className="fill-line" />
      {blocks(applied).map((block) => (
        <rect
          key={`applied-${block.key}`}
          x={block.x}
          y={0}
          width={block.width}
          height={H}
          rx={2}
          className="fill-accent opacity-60"
        />
      ))}
      {pending
        ? blocks(desired ?? []).map((block) => (
            <rect
              key={`desired-${block.key}`}
              x={block.x + 0.5}
              y={0.5}
              width={Math.max(1, block.width - 1)}
              height={H - 1}
              rx={2}
              fill="none"
              className="stroke-warning"
              strokeWidth={1}
            />
          ))
        : null}
    </svg>
  );
}
