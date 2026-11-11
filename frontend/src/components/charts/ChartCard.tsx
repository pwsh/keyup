import { Table2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { type CsvValue, downloadCsv } from "../../lib/csv";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { SkeletonChart } from "../ui/Skeleton";
import { EmptyState, ErrorState } from "../ui/States";

export interface ChartData {
  headers: string[];
  rows: CsvValue[][];
}

export interface ChartCardProps {
  title: string;
  /** What the chart shows and how to read it. */
  tip: string;
  data: ChartData;
  filename: string;
  height?: number;
  isLoading?: boolean;
  error?: unknown;
  isEmpty?: boolean;
  actions?: ReactNode;
  hint?: string;
  className?: string;
}

/**
 * The frame every chart sits in: title, CSV export, and a table view so no value on the
 * page is reachable only through color or a hover. It knows nothing about how the plot
 * is drawn — `Chart` puts ECharts inside it, `BarChart` plain SVG — so a page with one
 * small chart does not have to load the charting library to get the same card.
 */
export function ChartCard({
  title,
  tip,
  data,
  filename,
  height = 280,
  isLoading,
  error,
  isEmpty,
  actions,
  hint,
  className,
  children,
}: ChartCardProps & { children: ReactNode }) {
  const [showTable, setShowTable] = useState(false);

  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader
        title={title}
        hint={hint}
        actions={
          <>
            {actions}
            <Button
              tip={HELP["stats.chart.table"]}
              size="iconSm"
              variant="ghost"
              active={showTable}
              aria-label={showTable ? "Show the chart" : "Show the numbers as a table"}
              onClick={() => setShowTable((previous) => !previous)}
            >
              <Table2 size={14} aria-hidden="true" />
            </Button>
            <Button
              tip={HELP["stats.chart.download"]}
              size="sm"
              variant="ghost"
              onClick={() => downloadCsv(filename, data.headers, data.rows)}
            >
              CSV
            </Button>
          </>
        }
      />
      <div className="p-2" style={{ minHeight: height }}>
        {error ? (
          <ErrorState error={error} />
        ) : isLoading ? (
          <SkeletonChart className="h-full" />
        ) : isEmpty ? (
          <EmptyState title="Nothing to chart" hint="No calls match the current filters." />
        ) : showTable ? (
          <ChartTable data={data} caption={tip} />
        ) : (
          children
        )}
      </div>
    </Card>
  );
}

function ChartTable({ data, caption }: { data: ChartData; caption: string }) {
  return (
    <div className="max-h-80 overflow-auto scrollbar-thin">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">{caption}</caption>
        <thead className="sticky top-0 bg-surface">
          <tr>
            {data.headers.map((header) => (
              <th key={header} scope="col" className="border-b border-line px-2 py-1.5 font-medium text-ink">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.join("|")} className="border-b border-line last:border-0">
              {row.map((cell, index) => (
                <td
                  // biome-ignore lint/suspicious/noArrayIndexKey: a table cell's identity is its column position.
                  key={index}
                  className={cn("px-2 py-1 text-ink-2", typeof cell === "number" && "tabular-nums")}
                >
                  {cell ?? "—"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
