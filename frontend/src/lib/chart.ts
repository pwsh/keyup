/**
 * Shared ECharts option fragments so every chart reads as one system: hairline
 * gridlines one step off the surface, recessive axes, ink-token text (never the
 * series color), 2px lines and thin bars.
 */
import type { EChartsOption } from "echarts";
import { CHART_INK, type ThemeMode } from "./colors";
import { formatNumber } from "./format";

export const chartInk = (mode: ThemeMode) => CHART_INK[mode];

/** Respect the OS "reduce motion" setting for chart transitions too. */
const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

/** Whole numbers as-is, anything fractional to one decimal place. */
export function formatChartValue(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? formatNumber(value) : value.toFixed(1);
}

export function baseOption(mode: ThemeMode): EChartsOption {
  const ink = chartInk(mode);
  const reduced = prefersReducedMotion();
  return {
    backgroundColor: "transparent",
    animation: !reduced,
    animationDuration: reduced ? 0 : 260,
    textStyle: {
      fontFamily: '"Keyup Sans", system-ui, -apple-system, "Segoe UI", sans-serif',
      color: ink.secondary,
    },
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
    tooltip: {
      trigger: "axis",
      backgroundColor: ink.surface,
      borderColor: ink.grid,
      borderWidth: 1,
      textStyle: { color: ink.primary, fontSize: 12 },
      axisPointer: { type: "line", lineStyle: { color: ink.axis, width: 1 } },
      valueFormatter: (value: unknown) =>
        typeof value === "number" ? formatChartValue(value) : String(value ?? ""),
    },
    legend: {
      type: "scroll",
      bottom: 0,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: ink.secondary, fontSize: 11 },
      pageTextStyle: { color: ink.secondary },
    },
  };
}

export function categoryAxis(mode: ThemeMode, data: string[]): Record<string, unknown> {
  const ink = chartInk(mode);
  return {
    type: "category",
    data,
    axisLine: { lineStyle: { color: ink.axis } },
    axisTick: { show: false },
    axisLabel: { color: ink.muted, fontSize: 11, hideOverlap: true },
    splitLine: { show: false },
  };
}

export function valueAxis(mode: ThemeMode, name?: string): Record<string, unknown> {
  const ink = chartInk(mode);
  return {
    type: "value",
    name,
    nameTextStyle: { color: ink.muted, fontSize: 11 },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: ink.muted, fontSize: 11 },
    splitLine: { lineStyle: { color: ink.grid, type: "solid", width: 1 } },
  };
}

/** Bar series spec: capped thickness, 4px rounded data-end, 2px surface gap. */
export function barSeries(
  name: string,
  color: string,
  data: number[],
  mode: ThemeMode,
  opts: { horizontal?: boolean; stack?: string } = {},
): Record<string, unknown> {
  const ink = chartInk(mode);
  const radius = opts.horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0];
  return {
    name,
    type: "bar",
    data,
    barMaxWidth: 24,
    barMinWidth: 2,
    ...(opts.stack ? { stack: opts.stack } : {}),
    itemStyle: {
      color,
      borderRadius: opts.stack ? 0 : radius,
      borderColor: ink.surface,
      borderWidth: opts.stack ? 2 : 0,
    },
    emphasis: { focus: "series" },
  };
}

/** Line series spec: 2px line, >= 8px markers, surface ring on the end dot. */
export function lineSeries(
  name: string,
  color: string,
  data: (number | null)[],
  mode: ThemeMode,
  opts: { area?: boolean; dashed?: boolean } = {},
): Record<string, unknown> {
  const ink = chartInk(mode);
  return {
    name,
    type: "line",
    data,
    smooth: false,
    showSymbol: false,
    symbolSize: 8,
    connectNulls: true,
    lineStyle: { width: 2, color, type: opts.dashed ? "dashed" : "solid" },
    itemStyle: { color, borderColor: ink.surface, borderWidth: 2 },
    ...(opts.area ? { areaStyle: { color, opacity: 0.1 } } : {}),
    emphasis: { focus: "series" },
  };
}

/** One donut slice: identity in `name`, magnitude in `value`, hue already resolved. */
export interface PieSlice {
  name: string;
  value: number;
  itemStyle: { color: string };
}

/**
 * Donut series: a thin ring (the hole carries the total), a 2px surface gap
 * between slices and no slice labels — identity lives in the legend and tooltip.
 */
export function pieSeries(name: string, data: PieSlice[], mode: ThemeMode): Record<string, unknown> {
  const ink = chartInk(mode);
  return {
    name,
    type: "pie",
    data,
    center: ["50%", "44%"],
    radius: ["54%", "78%"],
    avoidLabelOverlap: false,
    label: { show: false },
    labelLine: { show: false },
    itemStyle: { borderColor: ink.surface, borderWidth: 2 },
    emphasis: { scaleSize: 4 },
  };
}
