import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { formatChartValue } from "../../lib/chart";
import { CHART_INK } from "../../lib/colors";
import { useTheme } from "../../state/theme";
import { ChartCard, type ChartCardProps } from "./ChartCard";

/**
 * A single-series bar chart drawn as plain SVG, in the same `ChartCard` frame as `Chart`.
 *
 * For the detail pages, which each show one or two small bar charts: through `Chart` each
 * of them loaded all of ECharts (≈ 210 kB gzipped) for what is a couple of dozen
 * rectangles. It follows the same mark specs as `barSeries` (lib/chart.ts) — bars at most
 * 24 px thick with a 4 px rounded data end, hairline gridlines in the chart ink, text in
 * ink tokens and never the series color — and has a per-bar hover tooltip whose hit
 * target is the whole band, not just the bar. The table view and CSV come from the card.
 *
 * `horizontal` draws category labels on the left and the value at the end of each bar —
 * the "top N" layout — instead of a value axis.
 */
export function BarChart({
  labels,
  values,
  color,
  seriesName,
  horizontal = false,
  height = 280,
  ...card
}: Omit<ChartCardProps, "height"> & {
  labels: string[];
  values: number[];
  color: string;
  /** What one value counts ("Calls"), for the tooltip. */
  seriesName: string;
  horizontal?: boolean;
  height?: number;
}) {
  return (
    <ChartCard {...card} height={height}>
      <BarPlot
        labels={labels}
        values={values}
        color={color}
        seriesName={seriesName}
        horizontal={horizontal}
        height={height}
        label={`${card.title}. ${card.tip}`}
      />
    </ChartCard>
  );
}

/** Round up to 1, 2 or 5 × 10^n and split into about four steps. */
export function niceTicks(max: number, target = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? 10 * magnitude;
  const whole = Math.max(1, Math.ceil(step));
  const size = step < 1 ? whole : step;
  const ticks: number[] = [];
  for (let value = 0; value < max + size; value += size) ticks.push(value);
  return ticks;
}

/** The element's width, kept current. jsdom and first paint get a sensible default. */
function useWidth(fallback = 560) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const next = element.clientWidth;
      if (next > 0) setWidth(next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** A bar's outline with only its data end rounded (top, or right when horizontal). */
function barPath(x: number, y: number, w: number, h: number, horizontal: boolean): string {
  const r = Math.max(0, Math.min(4, horizontal ? h / 2 : w / 2, horizontal ? w : h));
  if (horizontal) {
    return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${h - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}z`;
  }
  return `M${x},${y + h}v-${h - r}a${r},${r} 0 0 1 ${r},-${r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${h - r}z`;
}

const FONT = 11;
const LABEL_CHARS = 18;

function BarPlot({
  labels,
  values,
  color,
  seriesName,
  horizontal,
  height,
  label,
}: {
  labels: string[];
  values: number[];
  color: string;
  seriesName: string;
  horizontal: boolean;
  height: number;
  label: string;
}) {
  const ink = CHART_INK[useTheme()];
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(0, ...values);
  const ticks = useMemo(() => niceTicks(max), [max]);
  const top = ticks[ticks.length - 1] || 1;
  const count = Math.max(1, values.length);

  let bars: { x: number; y: number; w: number; h: number; band: [number, number, number, number] }[];
  let decorations: ReactNode;

  if (horizontal) {
    const short = labels.map((text) =>
      text.length > LABEL_CHARS ? `${text.slice(0, LABEL_CHARS - 1)}…` : text,
    );
    const left = Math.min(width * 0.45, Math.max(...short.map((text) => text.length), 1) * 6.4 + 12);
    const right = 48;
    const plot = Math.max(10, width - left - right);
    const band = height / count;
    const thick = Math.min(24, band * 0.7);
    bars = values.map((value, index) => {
      const w = (value / Math.max(1, max)) * plot;
      const y = index * band + (band - thick) / 2;
      return { x: left, y, w, h: thick, band: [0, index * band, width, band] };
    });
    decorations = (
      <>
        <line x1={left} x2={left} y1={0} y2={height} stroke={ink.axis} strokeWidth={1} />
        {short.map((text, index) => (
          <text
            // biome-ignore lint/suspicious/noArrayIndexKey: a row's identity is its position.
            key={index}
            x={left - 8}
            y={index * band + band / 2}
            textAnchor="end"
            dominantBaseline="central"
            fontSize={FONT}
            fill={ink.secondary}
          >
            {text}
          </text>
        ))}
        {bars.map((bar, index) => (
          <text
            // biome-ignore lint/suspicious/noArrayIndexKey: a row's identity is its position.
            key={index}
            x={bar.x + bar.w + 6}
            y={bar.y + bar.h / 2}
            dominantBaseline="central"
            fontSize={FONT}
            fill={ink.muted}
            className="tabular-nums"
          >
            {formatChartValue(values[index] ?? 0)}
          </text>
        ))}
      </>
    );
  } else {
    const left = Math.max(...ticks.map((tick) => formatChartValue(tick).length), 1) * 6.4 + 10;
    const bottom = 22;
    const plotW = Math.max(10, width - left - 8);
    const plotH = Math.max(10, height - bottom - 8);
    const band = plotW / count;
    const thick = Math.max(2, Math.min(24, band - 2, band * 0.7));
    // Label every nth bar so the texts never overlap (ECharts' hideOverlap).
    const every = Math.max(
      1,
      Math.ceil((Math.max(...labels.map((text) => text.length), 1) * 6.4 + 8) / band),
    );
    bars = values.map((value, index) => {
      const h = (value / top) * plotH;
      return {
        x: left + index * band + (band - thick) / 2,
        y: 8 + plotH - h,
        w: thick,
        h,
        band: [left + index * band, 0, band, height],
      };
    });
    decorations = (
      <>
        {ticks.map((tick) => {
          const y = 8 + plotH - (tick / top) * plotH;
          return (
            <g key={tick}>
              <line
                x1={left}
                x2={width - 8}
                y1={y}
                y2={y}
                stroke={tick === 0 ? ink.axis : ink.grid}
                strokeWidth={1}
              />
              <text
                x={left - 6}
                y={y}
                textAnchor="end"
                dominantBaseline="central"
                fontSize={FONT}
                fill={ink.muted}
              >
                {formatChartValue(tick)}
              </text>
            </g>
          );
        })}
        {labels.map((text, index) =>
          index % every === 0 ? (
            <text
              // biome-ignore lint/suspicious/noArrayIndexKey: a column's identity is its position.
              key={index}
              x={left + index * band + band / 2}
              y={height - 6}
              textAnchor="middle"
              fontSize={FONT}
              fill={ink.muted}
            >
              {text}
            </text>
          ) : null,
        )}
      </>
    );
  }

  const active = hover === null ? null : bars[hover];
  return (
    <div ref={ref} className="relative w-full" style={{ height }}>
      <svg width={width} height={height} role="img" aria-label={label} className="block overflow-visible">
        {decorations}
        {bars.map((bar, index) => (
          <path
            // biome-ignore lint/suspicious/noArrayIndexKey: a bar's identity is its position.
            key={index}
            d={barPath(bar.x, bar.y, bar.w, bar.h, horizontal)}
            fill={color}
            opacity={hover === null || hover === index ? 1 : 0.55}
          />
        ))}
        {bars.map((bar, index) => (
          // The hit target is the whole band, so a one-call bar is as easy to hover as the tallest.
          // biome-ignore lint/a11y/noStaticElementInteractions: a pointer nicety only; every value is in the card's table view.
          <rect
            // biome-ignore lint/suspicious/noArrayIndexKey: a band's identity is its position.
            key={index}
            x={bar.band[0]}
            y={bar.band[1]}
            width={bar.band[2]}
            height={bar.band[3]}
            fill="transparent"
            onMouseEnter={() => setHover(index)}
            onMouseLeave={() => setHover((current) => (current === index ? null : current))}
          />
        ))}
      </svg>
      {active && hover !== null ? (
        <div
          role="tooltip"
          className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md border border-line bg-surface px-2 py-1 text-xs text-ink shadow-sm"
          style={{
            left: Math.min(Math.max(0, active.x + active.w / 2 - 60), Math.max(0, width - 140)),
            top: Math.max(0, (horizontal ? active.y + active.h : active.y) + (horizontal ? 4 : -50)),
          }}
        >
          <div className="text-ink-2">{labels[hover]}</div>
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block h-2 w-2 rounded-sm"
              style={{ background: color }}
              aria-hidden="true"
            />
            {seriesName}
            <span className="font-medium tabular-nums">{formatChartValue(values[hover] ?? 0)}</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
