import { Tip } from "../ui/Tooltip";

/** Inline 24-hour trend for a table row — no axes, no labels, just the shape. */
export function Sparkline({
  values,
  color,
  label,
  width = 96,
  height = 22,
  loading = false,
}: {
  values: number[];
  color: string;
  label: string;
  width?: number;
  height?: number;
  /**
   * The numbers have not arrived yet. Drawn as a faint bar rather than "—", which is
   * what a row with no traffic shows: the two used to look identical, so a column still
   * loading read as "nothing was heard on any channel".
   */
  loading?: boolean;
}) {
  if (loading)
    return (
      <span
        role="img"
        aria-label="Loading the last 24 hours"
        className="inline-block h-1.5 animate-pulse rounded-full bg-surface-2"
        style={{ width }}
      />
    );
  if (values.length < 2) return <span className="text-xs text-ink-2">—</span>;
  const max = Math.max(...values, 1);
  const step = width / (values.length - 1);
  const points = values.map(
    (value, index) =>
      `${(index * step).toFixed(1)},${(height - (value / max) * (height - 2) - 1).toFixed(1)}`,
  );
  return (
    <Tip label={label}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={label}
        className="overflow-visible"
        tabIndex={0}
      >
        <polyline
          points={points.join(" ")}
          fill="none"
          stroke={color}
          strokeWidth={2}
          strokeLinejoin="round"
        />
      </svg>
    </Tip>
  );
}
