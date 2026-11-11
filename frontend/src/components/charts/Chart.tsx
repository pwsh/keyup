import type { ECElementEvent, EChartsOption } from "echarts";
import { BarChart, HeatmapChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import ReactEChartsCoreModule from "echarts-for-react/lib/core";
import { useMemo } from "react";
import { useTheme } from "../../state/theme";
import { ChartCard, type ChartCardProps } from "./ChartCard";

// echarts-for-react ships CommonJS; some bundler paths hand back the module
// namespace instead of the class, so unwrap `.default` when it is there.
const ReactEChartsCore =
  (ReactEChartsCoreModule as unknown as { default?: typeof ReactEChartsCoreModule }).default ??
  ReactEChartsCoreModule;

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export type { ChartData } from "./ChartCard";

/**
 * ECharts wrapper: theme-synced, inside the shared `ChartCard` frame (CSV export and a
 * table view). A page that only needs a plain bar chart should use `BarChart` instead,
 * which draws SVG and does not pull ECharts (≈ 210 kB gzipped) into its chunk.
 */
export function Chart({
  title,
  tip,
  option,
  data,
  filename,
  height = 280,
  isLoading,
  error,
  isEmpty,
  actions,
  hint,
  className,
  onPointClick,
}: ChartCardProps & {
  option: EChartsOption;
  onPointClick?: (params: ECElementEvent) => void;
}) {
  const mode = useTheme();
  // echarts-for-react disposes and re-creates the chart whenever `onEvents` is
  // not reference-equal, so it has to be stable across renders.
  const onEvents = useMemo(() => (onPointClick ? { click: onPointClick } : undefined), [onPointClick]);

  return (
    <ChartCard
      title={title}
      tip={tip}
      data={data}
      filename={filename}
      height={height}
      isLoading={isLoading}
      error={error}
      isEmpty={isEmpty}
      actions={actions}
      hint={hint}
      className={className}
    >
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        style={{ height }}
        notMerge
        lazyUpdate
        onEvents={onEvents}
        // Re-created on a theme change so every color is re-read from the tokens.
        key={mode}
        // A canvas says nothing to assistive tech, so the container is one image with a
        // name; an aria-label on a role-less div is prohibited and was not read. The
        // numbers themselves are in the card's table view.
        role="img"
        aria-label={`${title}. ${tip}`}
      />
    </ChartCard>
  );
}
